import asyncio
import datetime
import json
import logging
import math
import os
import uuid

import grpc

import assistant
import auction_pb2
import auction_pb2_grpc
import raft_pb2_grpc
import state_machine
from llm_client import LLMClient
from raft import NotLeader, ProposalTimeout, RaftNode, RaftStorage
from raft.transport import GrpcTransport, RaftServicer
from store import database, auth


def parse_peers(value: str) -> dict[str, str]:
    """"app-server-2=app_server_2:50051,..." -> {"app-server-2": "app_server_2:50051", ...}"""
    peers = {}
    for item in filter(None, (part.strip() for part in value.split(","))):
        node_id, _, address = item.partition("=")
        peers[node_id] = address or node_id
    return peers


NODE_ID = os.environ.get("NODE_ID", "app-server-unknown")
PORT = os.environ.get("PORT", "50051")

# The other app servers in the Raft cluster. Raft RPCs share the client port.
PEERS = parse_peers(os.environ.get("PEERS", ""))
# How other nodes and clients reach this one; followers hand it out as the
# leader_hint when this node is the leader.
SELF_ADDRESS = os.environ.get("SELF_ADDRESS", f"localhost:{PORT}")
ADDRESSES = {NODE_ID: SELF_ADDRESS, **PEERS}

# Where this node keeps its Raft log and its copy of the auction database.
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", NODE_ID))

# Raft timing. A follower that hears nothing from a leader for a random time
# in [min, max] starts an election; the leader sends heartbeats well inside
# the minimum, so a healthy leader is never suspected.
ELECTION_TIMEOUT = (
    float(os.environ.get("RAFT_ELECTION_TIMEOUT_MIN", "0.8")),
    float(os.environ.get("RAFT_ELECTION_TIMEOUT_MAX", "1.6")),
)
HEARTBEAT_SECONDS = float(os.environ.get("RAFT_HEARTBEAT_SECONDS", "0.2"))
# How long a write waits to be committed before the client is told it failed.
COMMIT_TIMEOUT_SECONDS = float(os.environ.get("RAFT_COMMIT_TIMEOUT_SECONDS", "2.0"))

# Anti-sniping, applied to auctions as they are listed: a bid in the last
# WINDOW seconds pushes the end to WINDOW seconds after that bid, never more
# than MAX_EXTENSION seconds past the original end. WINDOW=0 turns it off.
ANTI_SNIPE_WINDOW_SECONDS = int(os.environ.get("ANTI_SNIPE_WINDOW_SECONDS", "10"))
ANTI_SNIPE_MAX_EXTENSION_SECONDS = int(os.environ.get("ANTI_SNIPE_MAX_EXTENSION_SECONDS", "60"))

MAX_REQUEST_ID_LENGTH = 64

# The LLM node is optional infrastructure: if it isn't configured, or is slow
# or down, every LLM-backed feature falls back to plain text and bidding is
# unaffected. The timeout is generous because the model runs on CPU.
LLM_SERVER = os.environ.get("LLM_SERVER", "")
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))

MAX_AUCTION_SECONDS = 30 * 24 * 3600

COMMIT_TIMEOUT_MESSAGE = (
    "The cluster could not confirm this in time (is a majority of servers down?). "
    "It may still go through - check before retrying."
)

logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [%(levelname)s] node={NODE_ID} %(message)s")
log = logging.getLogger(__name__)


def now_ms() -> int:
    return int(datetime.datetime.now().timestamp() * 1000)


def auction_to_proto(record: dict) -> auction_pb2.Auction:
    return auction_pb2.Auction(
        auction_id=record["auction_id"],
        item=auction_pb2.Item(
            item_id=record["item_id"],
            title=record["title"],
            description=record["description"],
            seller_id=record["seller_id"],
        ),
        status=auction_pb2.AuctionStatus.Value(record["status"]),
        current_highest_bid=float(record["current_highest_bid"]),
        current_highest_bidder_id=record["current_highest_bidder_id"] or "",
        end_time_ms=int(record["end_time_ms"]),
        reserve_price=float(record.get("reserve_price") or 0.0),
        original_end_time_ms=int(record.get("original_end_ms") or 0),
    )


class AuctionServiceServicer(auction_pb2_grpc.AuctionServiceServicer):
    """The auction service: auth, listings, bidding, and the assistant.

    Writes (Login, Logout, CreateAuction, PlaceBid) go through Raft: only the
    leader accepts them, turns each into a command in the replicated log, and
    answers once a majority has stored it and it has been applied. A follower
    answers a write with leader_hint instead. Reads are served by any node
    from its own copy of the state, which may trail the leader's by a
    heartbeat or so.
    """

    def __init__(self, node: RaftNode, llm: LLMClient | None = None):
        self.node = node
        self.llm = llm

    # ---- Raft plumbing shared by the write RPCs ----

    def _leader_hint(self) -> str:
        return ADDRESSES.get(self.node.leader_id or "", "")

    def _not_leader_message(self) -> str:
        hint = self._leader_hint()
        if hint:
            return f"Not the leader - send this to {hint}"
        return "No leader elected right now - please retry in a moment"

    async def _leader_ready(self) -> None:
        """Raise NotLeader unless this node is the leader and its state is
        caught up with everything committed before its term."""
        await self.node.wait_until_ready(COMMIT_TIMEOUT_SECONDS)

    async def _commit(self, op: str, **fields) -> dict:
        """Replicate one command and return its result once it is applied."""
        result = await self.node.propose(
            state_machine.encode(op, now_ms=now_ms(), **fields), timeout=COMMIT_TIMEOUT_SECONDS
        )
        if isinstance(result, Exception):
            raise result
        return result

    # ---- Auth ----

    async def Login(self, request, context):
        """Log in, registering the username on first use.

        The contract has no separate Register RPC, so the first successful
        login for a username sets its password (stored as a salted hash) and
        every later login must match it.
        """
        try:
            username = request.username.strip() if request.username else ""
            password = request.password or ""

            if not username or not password:
                log.info("Login rejected - empty credentials")
                return auction_pb2.LoginResponse(
                    success=False, error_message="Username and password required",
                    session_token="", user_id=""
                )

            await self._leader_ready()
            existing_user = database.get_user(username)

            # Password hashing is deliberately slow (PBKDF2); run it off the
            # event loop so a burst of logins can't delay Raft heartbeats long
            # enough for followers to think the leader has died.
            if existing_user is None:
                user_id = f"user_{username}"
                password_hash = await asyncio.to_thread(auth.hash_password, password)
            else:
                matches = await asyncio.to_thread(
                    auth.validate_password_stored, password, existing_user["password_hash"]
                )
                if not matches:
                    log.info("Login rejected - wrong password for %s", username)
                    return auction_pb2.LoginResponse(
                        success=False, error_message="Invalid username or password",
                        session_token="", user_id=""
                    )
                user_id = existing_user["user_id"]
                password_hash = ""  # existing user: nothing to register

            session_token = auth.generate_session_token()
            result = await self._commit(
                "LOGIN", username=username, user_id=user_id, password_hash=password_hash, session_token=session_token
            )
            if not result["ok"]:
                log.info("Login rejected for %s (%s)", username, result["error"])
                return auction_pb2.LoginResponse(success=False, error_message=result["error"])

            return auction_pb2.LoginResponse(
                success=True, session_token=session_token, user_id=user_id, error_message=""
            )

        except NotLeader:
            return auction_pb2.LoginResponse(
                success=False, error_message=self._not_leader_message(), leader_hint=self._leader_hint()
            )
        except ProposalTimeout:
            return auction_pb2.LoginResponse(success=False, error_message=COMMIT_TIMEOUT_MESSAGE)
        except Exception:
            log.exception("Login failed for %s", request.username)
            return auction_pb2.LoginResponse(success=False, error_message="Login failed, please try again")

    async def Logout(self, request, context):
        """End a session. An empty token is a no-op on any node; otherwise this
        is a write, so it needs the leader. Succeeds for an unknown token."""
        try:
            if not request.session_token:
                return auction_pb2.LogoutResponse(success=True)
            await self._leader_ready()
            session = database.validate_session(request.session_token)
            if session:
                await self._commit("LOGOUT", session_token=request.session_token)
                log.info("Logged out %s", session["username"])
            return auction_pb2.LogoutResponse(success=True)

        except NotLeader:
            return auction_pb2.LogoutResponse(success=False, leader_hint=self._leader_hint())
        except ProposalTimeout:
            return auction_pb2.LogoutResponse(success=False)
        except Exception:
            log.exception("Logout failed")
            return auction_pb2.LogoutResponse(success=False)

    # ---- Browsing (served by any node) ----

    async def ListAuctions(self, request, context):
        """All auctions, optionally filtered by status."""
        try:
            # A protobuf enum field is a plain int on the message; its name has
            # to be looked up explicitly.
            status_filter = None
            if request.status_filter:
                status_filter = auction_pb2.AuctionStatus.Name(request.status_filter)

            auctions = database.get_all_auctions(status_filter=status_filter)
            return auction_pb2.ListAuctionsResponse(auctions=[auction_to_proto(a) for a in auctions])

        except Exception:
            log.exception("ListAuctions failed")
            return auction_pb2.ListAuctionsResponse(auctions=[])

    async def GetAuction(self, request, context):
        """One auction by id; found=False if it doesn't exist."""
        try:
            auction = database.get_auction(request.auction_id)
            if not auction:
                return auction_pb2.GetAuctionResponse(found=False)
            return auction_pb2.GetAuctionResponse(found=True, auction=auction_to_proto(auction))

        except Exception:
            log.exception("GetAuction failed for %s", request.auction_id)
            return auction_pb2.GetAuctionResponse(found=False)

    # ---- Writes ----

    async def CreateAuction(self, request, context):
        """List an item as an immediately-active auction.

        Session-gated - any logged-in user may sell - rather than a separate
        seller role. A blank description is written by the LLM node (before
        the command is proposed: the log carries the finished text, so every
        node stores the same description).
        """
        try:
            await self._leader_ready()
            session = database.validate_session(request.session_token)
            if not session:
                return auction_pb2.CreateAuctionResponse(
                    success=False, error_message="Invalid or expired session token"
                )

            if not request.title.strip():
                return auction_pb2.CreateAuctionResponse(success=False, error_message="Title is required")
            if not math.isfinite(request.reserve_price) or request.reserve_price < 0:
                return auction_pb2.CreateAuctionResponse(
                    success=False, error_message="Reserve price must be zero or a positive number"
                )
            if request.duration_seconds > MAX_AUCTION_SECONDS:
                return auction_pb2.CreateAuctionResponse(success=False, error_message="An auction can run for at most 30 days")

            duration_seconds = request.duration_seconds if request.duration_seconds > 0 else 3600
            auction_id = str(uuid.uuid4())[:8]

            description = request.description.strip()
            if not description:
                description = await self._describe_item(request.title)

            result = await self._commit(
                "CREATE_AUCTION",
                auction_id=auction_id,
                title=request.title,
                description=description,
                seller_id=session["user_id"],
                duration_seconds=duration_seconds,
                reserve_price=request.reserve_price,
                snipe_window_seconds=ANTI_SNIPE_WINDOW_SECONDS,
                max_extension_seconds=ANTI_SNIPE_MAX_EXTENSION_SECONDS,
            )
            if not result["ok"]:
                return auction_pb2.CreateAuctionResponse(success=False, error_message="Could not create auction")

            return auction_pb2.CreateAuctionResponse(success=True, auction_id=auction_id)

        except NotLeader:
            return auction_pb2.CreateAuctionResponse(
                success=False, error_message=self._not_leader_message(), leader_hint=self._leader_hint()
            )
        except ProposalTimeout:
            return auction_pb2.CreateAuctionResponse(success=False, error_message=COMMIT_TIMEOUT_MESSAGE)
        except Exception:
            log.exception("CreateAuction failed")
            return auction_pb2.CreateAuctionResponse(success=False, error_message="Could not create auction, please try again")

    async def PlaceBid(self, request, context):
        """Place a bid: it must beat the highest bid and meet any reserve.

        Bids are judged when they are applied from the log, so simultaneous
        bids are decided in log order - identically on every node. A bid that
        carries a request_id is exactly-once: a retry gets the original outcome
        (replayed=True) instead of being placed again.
        """
        try:
            await self._leader_ready()
            session = database.validate_session(request.session_token)
            if not session:
                return auction_pb2.PlaceBidResponse(
                    accepted=False,
                    error_message="Invalid or expired session token",
                    updated_auction=auction_pb2.Auction()
                )
            if len(request.request_id) > MAX_REQUEST_ID_LENGTH:
                return auction_pb2.PlaceBidResponse(
                    accepted=False, error_message="request_id is too long", updated_auction=auction_pb2.Auction()
                )

            # A retry of a request already applied is answered straight from
            # the request table, without a new log entry (paper, section 8).
            # One still in flight is caught when its duplicate is applied.
            stored = (
                database.get_request_result(session["user_id"], request.request_id) if request.request_id else None
            )
            if stored is not None:
                result = {**json.loads(stored), "replayed": True}
            else:
                result = await self._commit(
                    "PLACE_BID",
                    bid_id=str(uuid.uuid4())[:8],
                    auction_id=request.auction_id,
                    bidder_id=session["user_id"],
                    amount=request.amount,
                    request_id=request.request_id,
                )
            replayed = bool(result.get("replayed"))
            if not result["ok"]:
                return auction_pb2.PlaceBidResponse(
                    accepted=False,
                    error_message=result["message"],
                    updated_auction=auction_pb2.Auction(),
                    replayed=replayed,
                )

            return auction_pb2.PlaceBidResponse(
                accepted=True,
                error_message="",
                updated_auction=auction_to_proto(database.get_auction(request.auction_id)),
                replayed=replayed,
            )

        except NotLeader:
            return auction_pb2.PlaceBidResponse(
                accepted=False,
                error_message=self._not_leader_message(),
                leader_hint=self._leader_hint(),
                updated_auction=auction_pb2.Auction()
            )
        except ProposalTimeout:
            return auction_pb2.PlaceBidResponse(
                accepted=False, error_message=COMMIT_TIMEOUT_MESSAGE, updated_auction=auction_pb2.Auction()
            )
        except Exception:
            log.exception("PlaceBid failed for %s", request.auction_id)
            return auction_pb2.PlaceBidResponse(
                accepted=False,
                error_message="Could not place bid, please try again",
                updated_auction=auction_pb2.Auction()
            )

    async def ListBids(self, request, context):
        """Every accepted bid on an auction, oldest first."""
        try:
            if not database.get_auction(request.auction_id):
                return auction_pb2.ListBidsResponse(found=False)
            return auction_pb2.ListBidsResponse(
                found=True,
                bids=[
                    auction_pb2.Bid(
                        bid_id=b["bid_id"], auction_id=b["auction_id"], bidder_id=b["bidder_id"],
                        amount=float(b["amount"]), timestamp_ms=int(b["timestamp_ms"]),
                    )
                    for b in database.get_bids_for_auction(request.auction_id)
                ],
            )
        except Exception:
            log.exception("ListBids failed for %s", request.auction_id)
            return auction_pb2.ListBidsResponse(found=False)

    # ---- Cluster status ----

    async def GetClusterStatus(self, request, context):
        status = self.node.status()
        return auction_pb2.ClusterStatusResponse(
            node_id=status["node_id"],
            role=status["role"],
            term=status["term"],
            leader_id=status["leader_id"],
            leader_address=self._leader_hint(),
            commit_index=status["commit_index"],
            last_applied=status["last_applied"],
            last_log_index=status["last_log_index"],
            applied_digest=status["applied_digest"],
        )

    # ------------------------------------------------------------------
    # LLM-backed features. None of these sit on the bidding path, and every
    # one degrades to plain text if the LLM node is missing, slow, or down.
    # ------------------------------------------------------------------

    async def _describe_item(self, title: str) -> str:
        """The seller left the description blank: have the LLM node write one."""
        if self.llm is not None:
            try:
                text = await self.llm.generate_item_description(title.strip())
                log.info("description for %r written by the LLM node", title)
                return text
            except grpc.aio.AioRpcError as exc:
                log.warning("LLM node unavailable for a description (%s) - using fallback", exc.code())
        return assistant.fallback_description(title)

    async def AskAssistant(self, request, context):
        """Bidder FAQ, answered by the LLM node with the auction's details
        injected as context (the LLM node holds no auction state itself)."""
        try:
            if not database.validate_session(request.session_token):
                return auction_pb2.AskAssistantResponse(
                    success=False, error_message="Invalid or expired session token"
                )

            question = request.question.strip()
            if not question:
                return auction_pb2.AskAssistantResponse(success=False, error_message="Question is required")

            auction, bid_count = None, 0
            if request.auction_id:
                auction = database.get_auction(request.auction_id)
                if not auction:
                    return auction_pb2.AskAssistantResponse(success=False, error_message="Auction not found")
                bid_count = len(database.get_bids_for_auction(request.auction_id))

            now = now_ms()

            if self.llm is not None:
                try:
                    answer = await self.llm.get_answer(question, assistant.build_context(auction, bid_count, now))
                    return auction_pb2.AskAssistantResponse(success=True, answer=answer, from_fallback=False)
                except grpc.aio.AioRpcError as exc:
                    log.warning("LLM node unavailable for AskAssistant (%s) - answering from facts", exc.code())

            return auction_pb2.AskAssistantResponse(
                success=True, answer=assistant.fallback_answer(auction, bid_count, now), from_fallback=True
            )

        except Exception:
            log.exception("AskAssistant failed")
            return auction_pb2.AskAssistantResponse(success=False, error_message="Assistant error, please try again")

    async def GetAuctionSummary(self, request, context):
        """Outcome of a closed auction: winner, mock escrow settlement, recap."""
        try:
            auction = database.get_auction(request.auction_id)
            if not auction:
                return auction_pb2.GetAuctionSummaryResponse(found=False)
            if auction["status"] != "CLOSED":
                return auction_pb2.GetAuctionSummaryResponse(found=True, closed=False)

            settlement = database.get_settlement(request.auction_id)
            if settlement is None:
                # Closed, but the timer hasn't settled it yet (at most one tick).
                return auction_pb2.GetAuctionSummaryResponse(found=True, closed=True)

            summary = database.get_summary(request.auction_id)
            return auction_pb2.GetAuctionSummaryResponse(
                found=True,
                closed=True,
                winner_id=settlement["winner_id"] or "",
                final_price=float(settlement["final_price"]),
                buyers_premium=float(settlement["buyers_premium"]),
                escrow_status=settlement["escrow_status"],
                summary=summary["summary"] if summary else "",
                summary_from_fallback=bool(summary["from_fallback"]) if summary else False,
                summary_ready=summary is not None,
            )

        except Exception:
            log.exception("GetAuctionSummary failed for %s", request.auction_id)
            return auction_pb2.GetAuctionSummaryResponse(found=False)


TIMER_TICK_SECONDS = 1

# Recaps are written in the background; keep a reference so a running task
# isn't garbage-collected before it finishes.
_background_tasks: set = set()


async def write_recap(node: RaftNode, llm: LLMClient | None, record: dict) -> None:
    """Write the post-auction recap - the LLM node if it answers, a templated
    one if it doesn't - and commit it through Raft so every node stores the
    same text. Runs in the background so a slow model never delays the timer."""
    auction_id = record["auction_id"]
    try:
        winner_id = record["winner_id"]
        final_price = float(record["final_price"])
        amounts = [float(b["amount"]) for b in database.get_bids_for_auction(auction_id)]
        average_price = database.get_average_sale_price(exclude_auction_id=auction_id)
        text, from_fallback = None, True

        if llm is not None:
            try:
                text = await llm.summarize_auction(auction_id, record["title"], amounts, final_price, average_price)
                from_fallback = False
            except grpc.aio.AioRpcError as exc:
                log.warning("LLM node unavailable for the recap of %s (%s) - using template", auction_id, exc.code())

        if text is None:
            winner = winner_id.removeprefix("user_") if winner_id else None
            text = assistant.fallback_summary(record["title"], amounts, final_price, winner, average_price)

        await node.propose(
            state_machine.encode(
                "SAVE_SUMMARY", auction_id=auction_id, summary=text, from_fallback=from_fallback, now_ms=now_ms()
            ),
            timeout=COMMIT_TIMEOUT_SECONDS,
        )
        log.info("recap auction=%s (%s): %s", auction_id, "template" if from_fallback else "LLM", text)
    except (NotLeader, ProposalTimeout):
        log.info("recap for %s not committed (leadership changed) - the next leader will write it", auction_id)
    except Exception:
        log.exception("could not write the recap for auction %s", auction_id)


async def run_auction_timer(node: RaftNode, llm: LLMClient | None = None):
    """The auction clock, run by whichever node is currently the leader.

    Once a second the leader checks for auctions past their end time (or
    closed but unsettled), and if there are any it proposes one CLOSE_EXPIRED
    command stamped with its clock; every node then closes and settles the
    same auctions when it applies that command. It also starts a recap for
    each settled auction that doesn't have one yet.

    Because the decision is a log entry and not local process state, a leader
    crash loses nothing: the next leader sees the same auctions still open (or
    unsettled, or un-recapped) and finishes the job.
    """
    recaps_in_progress: set[str] = set()
    while True:
        await asyncio.sleep(TIMER_TICK_SECONDS)
        if not node.is_leader:
            continue
        try:
            await node.wait_until_ready(COMMIT_TIMEOUT_SECONDS)
            now = now_ms()
            if database.has_expired_active(now) or database.get_closed_unsettled():
                command = state_machine.encode(
                    "CLOSE_EXPIRED", now_ms=now, premium_percent=assistant.BUYERS_PREMIUM_PERCENT
                )
                await node.propose(command, timeout=COMMIT_TIMEOUT_SECONDS)

            for record in database.get_unsummarized():
                auction_id = record["auction_id"]
                if auction_id in recaps_in_progress:
                    continue
                recaps_in_progress.add(auction_id)
                task = asyncio.create_task(write_recap(node, llm, record))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
                task.add_done_callback(lambda _t, aid=auction_id: recaps_in_progress.discard(aid))
        except (NotLeader, ProposalTimeout):
            pass  # lost leadership or quorum mid-tick; the next leader carries on
        except Exception:
            # One bad tick must never kill the timer for the life of the process.
            log.exception("auction timer tick failed")


async def serve():
    """Start the Raft node, the gRPC server (auction + Raft services on one
    port) and the auction timer."""
    os.makedirs(DATA_DIR, exist_ok=True)

    # The auction database is a cache of the replicated log: start it empty and
    # let Raft re-apply every committed entry into it. The log itself (raft.db)
    # is what survives a restart.
    database.DB_PATH = os.path.join(DATA_DIR, "auctions.db")
    database.DURABLE = False
    database.reset_database()

    storage = RaftStorage(os.path.join(DATA_DIR, "raft.db"))
    transport = GrpcTransport(PEERS)
    node = RaftNode(
        NODE_ID, list(PEERS), storage, transport, state_machine.apply,
        election_timeout=ELECTION_TIMEOUT, heartbeat_interval=HEARTBEAT_SECONDS,
    )

    llm = LLMClient(LLM_SERVER, timeout_seconds=LLM_TIMEOUT_SECONDS) if LLM_SERVER else None
    log.info("LLM node: %s", LLM_SERVER or "not configured (assistant features use plain-text fallbacks)")

    server = grpc.aio.server()
    auction_pb2_grpc.add_AuctionServiceServicer_to_server(AuctionServiceServicer(node, llm), server)
    raft_pb2_grpc.add_RaftServiceServicer_to_server(RaftServicer(node), server)
    server.add_insecure_port(f"[::]:{PORT}")
    log.info("app server starting on port %s (peers: %s)", PORT, PEERS or "none - single-node cluster")
    await server.start()

    node.start()
    timer_task = asyncio.create_task(run_auction_timer(node, llm))
    try:
        await server.wait_for_termination()
    finally:
        timer_task.cancel()
        await node.stop()
        await transport.close()
        storage.close()
        if llm is not None:
            await llm.close()


if __name__ == "__main__":
    asyncio.run(serve())
