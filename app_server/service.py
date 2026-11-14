"""The client-facing gRPC service: accounts, listings, bidding, the
assistant, the cluster status, and the assignment's generic post/get.

Writes (Register, Login, Logout, CreateAuction, PlaceBid) go through Raft -
see RaftWriter. Reads are served from this node's own copy of the state.
"""
import asyncio
import datetime
import json
import logging
import math
import re
import uuid

import grpc

import assistant
import auction_pb2
import auction_pb2_grpc
import state_machine
from config import (
    ADDRESSES, ANTI_SNIPE_MAX_EXTENSION_SECONDS, ANTI_SNIPE_WINDOW_SECONDS, COMMIT_TIMEOUT_MESSAGE,
    COMMIT_TIMEOUT_SECONDS, MAX_AUCTION_SECONDS, MAX_REQUEST_ID_LENGTH, MIN_PASSWORD_LENGTH, STATUS_LOG_TAIL,
    USERNAME_PATTERN, WELCOME_CREDIT,
)
from llm_client import LLMClient
from raft import NotLeader, ProposalTimeout, RaftNode
from store import database, auth

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


class RaftWriter:
    """Raft plumbing shared by every service with write RPCs (the auction
    service and the payment service).

    A write goes through Raft: only the leader accepts it, turns it into a
    command in the replicated log, and answers once a majority has stored it
    and it has been applied. A follower answers a write with leader_hint
    instead. Reads are served by any node from its own copy of the state,
    which may trail the leader's by a heartbeat or so.
    """

    node: RaftNode

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


class AuctionServiceServicer(RaftWriter, auction_pb2_grpc.AuctionServiceServicer):
    """The auction service: accounts, listings, bidding, the assistant, the
    cluster status and the generic post/get front door."""

    def __init__(self, node: RaftNode, llm: LLMClient | None = None):
        self.node = node
        self.llm = llm
        self.business = None  # the post/get router, attached by main.py

    # ---- Accounts ----

    async def Register(self, request, context):
        """Create an account. Its wallet opens with the welcome credit."""
        try:
            username = (request.username or "").strip()
            password = request.password or ""
            if not re.fullmatch(USERNAME_PATTERN, username):
                return auction_pb2.RegisterResponse(
                    success=False, error_message="Username must be 3-32 letters, digits, '_', '-' or '.'"
                )
            if len(password) < MIN_PASSWORD_LENGTH:
                return auction_pb2.RegisterResponse(
                    success=False, error_message=f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
                )

            await self._leader_ready()
            if database.get_user(username) is not None:
                return auction_pb2.RegisterResponse(success=False, error_message="Username already taken")

            # PBKDF2 is deliberately slow: off the event loop, so it can't
            # delay Raft heartbeats.
            password_hash = await asyncio.to_thread(auth.hash_password, password)
            user_id = f"user_{username}"
            result = await self._commit(
                "REGISTER", username=username, user_id=user_id, password_hash=password_hash,
                welcome_credit=WELCOME_CREDIT,
            )
            if not result["ok"]:
                return auction_pb2.RegisterResponse(success=False, error_message=result["error"])
            log.info("registered %s", username)
            return auction_pb2.RegisterResponse(success=True, user_id=user_id)

        except NotLeader:
            return auction_pb2.RegisterResponse(
                success=False, error_message=self._not_leader_message(), leader_hint=self._leader_hint()
            )
        except ProposalTimeout:
            return auction_pb2.RegisterResponse(success=False, error_message=COMMIT_TIMEOUT_MESSAGE)
        except Exception:
            log.exception("Register failed for %s", request.username)
            return auction_pb2.RegisterResponse(success=False, error_message="Registration failed, please try again")

    async def Login(self, request, context):
        """Sign in to an existing account (create one with Register)."""
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
            matches = existing_user is not None and await asyncio.to_thread(
                auth.validate_password_stored, password, existing_user["password_hash"]
            )
            if not matches:
                log.info("Login rejected for %s (%s)", username, "wrong password" if existing_user else "no such user")
                return auction_pb2.LoginResponse(
                    success=False, error_message="Invalid username or password",
                    session_token="", user_id=""
                )
            user_id = existing_user["user_id"]

            session_token = auth.generate_session_token()
            result = await self._commit(
                "LOGIN", username=username, user_id=user_id, session_token=session_token
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
                    premium_percent=assistant.BUYERS_PREMIUM_PERCENT,
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
            recent_entries=[
                auction_pb2.LogEntryView(index=e.index, term=e.term, **state_machine.describe(e.command))
                for e in self.node.tail(STATUS_LOG_TAIL)
            ],
        )

    # ---- The assignment's post(token, type, data) / get(token, type, params) ----

    async def Post(self, request, context):
        return await self.business.post(request, context)

    async def Get(self, request, context):
        return await self.business.get(request, context)

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
            session = database.validate_session(request.session_token)
            if not session:
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
            # Context-aware: the asker's own wallet, so "how much can I bid?"
            # is answered for them, not in general.
            user_id = session["user_id"]
            wallet = assistant.wallet_facts(
                session["username"], database.get_wallet_balance(user_id) or 0.0,
                database.committed_funds(user_id, assistant.BUYERS_PREMIUM_PERCENT), database.in_escrow(user_id),
            )

            if self.llm is not None:
                request_id = uuid.uuid4().hex[:12]
                try:
                    reply = await self.llm.get_answer(
                        question, assistant.build_context(auction, bid_count, now, wallet), request_id=request_id
                    )
                    if reply.request_id and reply.request_id != request_id:
                        log.warning("LLM reply for request %s came back as %s", request_id, reply.request_id)
                    log.info("assistant request %s answered by the LLM node (sources: %s)",
                             request_id, ", ".join(reply.sources) or "none")
                    answer = reply.answer
                    if auction is not None:
                        # The model is fluent, not reliable: the facts a bidder acts on
                        # are restated underneath, straight from the auction.
                        answer = f"{answer}\n\n{assistant.fine_print(auction, bid_count, now)}"
                    return auction_pb2.AskAssistantResponse(
                        success=True, answer=answer, from_fallback=False, sources=list(reply.sources)
                    )
                except grpc.aio.AioRpcError as exc:
                    log.warning("LLM node unavailable for AskAssistant (%s) - answering from facts", exc.code())

            return auction_pb2.AskAssistantResponse(
                success=True, answer=assistant.fallback_answer(auction, bid_count, now, wallet), from_fallback=True
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
