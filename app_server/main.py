import asyncio
import datetime
import logging
import os
import uuid

import grpc

import assistant
import auction_pb2
import auction_pb2_grpc
from llm_client import LLMClient
from store import database, auth


NODE_ID = os.environ.get("NODE_ID", "app-server-unknown")
PORT = os.environ.get("PORT", "50051")

# The LLM node is optional infrastructure: if it isn't configured, or is slow
# or down, every LLM-backed feature falls back to plain text and bidding is
# unaffected. The timeout is generous because the model runs on CPU.
LLM_SERVER = os.environ.get("LLM_SERVER", "")
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))

# M1 has no leader election - every replica accepts writes - so there is no
# leader to point a client at. (The field stays in the contract for when
# replicas need to redirect writes.)
LEADER_HINT = ""

logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [%(levelname)s] node={NODE_ID} %(message)s")
log = logging.getLogger(__name__)


class AuctionServiceServicer(auction_pb2_grpc.AuctionServiceServicer):
    """Business logic implementation for auction operations."""

    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm

    async def Login(self, request, context):
        """Handle user login with real password verification.

        No separate Register RPC exists in the contract, so this uses
        an auto-register-on-first-use pattern: the first successful
        login for a username sets its password (hashed); every login
        after that must match it. Previously this method computed a
        hash and then never checked it against anything - any
        username/password pair succeeded, including impersonating an
        existing username.

        Args:
            request: Contains username and password fields
            context: gRPC metadata including any auth headers

        Returns:
            LoginResponse with session token on success
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

            existing_user = database.get_user(username)

            if existing_user is None:
                user_id = f"user_{username}"
                database.create_user(username, user_id, auth.hash_password(password))
                log.info("Registered new user %s", username)
            else:
                if not auth.validate_password_stored(password, existing_user["password_hash"]):
                    log.info("Login rejected - wrong password for %s", username)
                    return auction_pb2.LoginResponse(
                        success=False, error_message="Invalid username or password",
                        session_token="", user_id=""
                    )
                user_id = existing_user["user_id"]

            conn = database.get_connection()

            session_token = auth.generate_session_token()
            now_ms = datetime.datetime.now().timestamp() * 1000

            conn.execute("""
                INSERT OR REPLACE INTO sessions
                (session_token, user_id, username, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
            """, (session_token, user_id, username, now_ms, now_ms + 3600000))

            conn.commit()
            conn.close()

            return auction_pb2.LoginResponse(
                success=True, session_token=session_token, user_id=user_id, error_message=""
            )

        except Exception as e:
            log.exception(f"Login failed for {request.username}")
            return auction_pb2.LoginResponse(success=False, error_message="Login failed, please try again")

    async def Logout(self, request, context):
        """Handle user logout by deleting session.
        
        Args:
            request: Contains session_token to invalidate
            context: gRPC context
        
        Returns:
            LogoutResponse indicating success/failure
        """
        try:
            if not request.session_token:
                return auction_pb2.LogoutResponse(success=True)
            
            result = database.delete_session(request.session_token)
            
            if result:
                log.info(f"Logged out session {request.session_token}")
            else:
                log.info(f"Invalid session token: {request.session_token}")
            
            return auction_pb2.LogoutResponse(success=True)
        
        except Exception as e:
            log.exception(f"Logout failed for {request.session_token}")
            return auction_pb2.LogoutResponse(success=False)

    async def ListAuctions(self, request, context):
        """List all auctions with optional status filter.
        
        Args:
            request: Contains optional status_filter field  
            context: gRPC context
        
        Returns:
            ListAuctionsResponse with auction list
        """
        try:
            # Get status filter if present. request.status_filter is a
            # plain int on the message (protobuf enum fields are never
            # wrapped in an object with .name/.value) - the string name
            # has to be looked up explicitly via AuctionStatus.Name().
            status_filter = None
            if request.status_filter:
                status_filter = auction_pb2.AuctionStatus.Name(request.status_filter)
            
            auctions = database.get_all_auctions(status_filter=status_filter)
            
            return auction_pb2.ListAuctionsResponse(auctions=[
                auction_pb2.Auction(
                    auction_id=a["auction_id"],
                    item=auction_pb2.Item(
                        item_id=a["item_id"], 
                        title=a["title"],
                        description=a["description"], 
                        seller_id=a["seller_id"]
                    ),
                    status=auction_pb2.AuctionStatus.Value(a["status"]),
                    current_highest_bid=float(a["current_highest_bid"]),
                    current_highest_bidder_id=a["current_highest_bidder_id"] or "",
                    end_time_ms=int(a["end_time_ms"]),
                    reserve_price=float(a.get("reserve_price", 0.0))
                ) for a in auctions
            ])
        
        except Exception as e:
            log.exception("ListAuctions failed")
            return auction_pb2.ListAuctionsResponse(auctions=[])

    async def GetAuction(self, request, context):
        """Get a single auction by ID.
        
        Args:
            request: Contains auction_id to fetch
            context: gRPC context
        
        Returns:
            GetAuctionResponse with auction details or not_found=True
        """
        try:
            auction = database.get_auction(request.auction_id)
            
            if not auction:
                return auction_pb2.GetAuctionResponse(found=False)
            
            return auction_pb2.GetAuctionResponse(
                found=True,
                auction=auction_pb2.Auction(
                    auction_id=auction["auction_id"],
                    item=auction_pb2.Item(
                        item_id=auction["item_id"], 
                        title=auction["title"],
                        description=auction["description"], 
                        seller_id=auction["seller_id"]
                    ),
                    status=auction_pb2.AuctionStatus.Value(auction["status"]),
                    current_highest_bid=float(auction["current_highest_bid"]),
                    current_highest_bidder_id=auction["current_highest_bidder_id"] or "",
                    end_time_ms=int(auction["end_time_ms"]),
                    reserve_price=float(auction.get("reserve_price", 0.0))
                )
            )

        except Exception as e:
            log.exception(f"GetAuction failed for {request.auction_id}")
            return auction_pb2.GetAuctionResponse(found=False)

    async def CreateAuction(self, request, context):
        """Create an item and an immediately-active auction for it.

        Added post-freeze: there was previously no way for any client
        to get real data into the system at all (see
        Raftioneer_Bug_Solutions_Guide.pdf, Bug 6). Session-gated
        (any logged-in user may list an item) rather than a separate
        seller role, to keep this addition minimal.
        """
        try:
            session = database.validate_session(request.session_token)
            if not session:
                return auction_pb2.CreateAuctionResponse(
                    success=False, error_message="Invalid or expired session token"
                )

            if not request.title.strip():
                return auction_pb2.CreateAuctionResponse(success=False, error_message="Title is required")

            duration_seconds = request.duration_seconds if request.duration_seconds > 0 else 3600
            auction_id = str(uuid.uuid4())[:8]

            description = request.description.strip()
            if not description:
                description = await self._describe_item(request.title)

            created = database.create_item(
                item_id=auction_id,
                title=request.title,
                description=description,
                seller_id=session["user_id"],
                duration_seconds=duration_seconds,
                reserve_price=request.reserve_price,
            )
            if not created:
                return auction_pb2.CreateAuctionResponse(success=False, error_message="Could not create auction")

            log.info("CreateAuction(auction_id=%s, title=%s) by %s", auction_id, request.title, session["user_id"])
            return auction_pb2.CreateAuctionResponse(success=True, auction_id=auction_id)

        except Exception as e:
            log.exception("CreateAuction failed")
            return auction_pb2.CreateAuctionResponse(success=False, error_message="Could not create auction, please try again")

    async def PlaceBid(self, request, context):
        """Place a bid on an auction.
        
        Args:
            request: Contains session_token, auction_id, and amount fields
            context: gRPC context
        
        Returns:
            PlaceBidResponse with acceptance status and updated auction
        """
        try:
            # Validate session token
            session = database.validate_session(request.session_token)
            
            if not session:
                return auction_pb2.PlaceBidResponse(
                    accepted=False, 
                    error_message="Invalid or expired session token",
                    leader_hint=LEADER_HINT,
                    updated_auction=auction_pb2.Auction()
                )
            
            # Check bid amount against current highest and reserve price
            successful, result = database.create_bid(
                auction_id=request.auction_id,
                bidder_id=session["user_id"],
                amount=request.amount
            )
            
            if not successful:
                return auction_pb2.PlaceBidResponse(
                    accepted=False, 
                    error_message=result,
                    leader_hint=LEADER_HINT,
                    updated_auction=auction_pb2.Auction()
                )
            
            # Fetch updated auction after successful bid
            auction = database.get_auction(request.auction_id)
            
            return auction_pb2.PlaceBidResponse(
                accepted=True, 
                error_message="",
                leader_hint=LEADER_HINT,
                updated_auction=auction_pb2.Auction(
                    auction_id=request.auction_id,
                    item=auction_pb2.Item(
                        item_id=auction["item_id"], 
                        title=auction["title"],
                        description=auction["description"], 
                        seller_id=auction["seller_id"]
                    ),
                    status=auction_pb2.AuctionStatus.Value(auction["status"]),
                    current_highest_bid=float(auction["current_highest_bid"]),
                    current_highest_bidder_id=auction["current_highest_bidder_id"] or "",
                    end_time_ms=int(auction["end_time_ms"]),
                    reserve_price=float(auction.get("reserve_price", 0.0))
                )
            )

        except Exception as e:
            log.exception(f"PlaceBid failed for {request.auction_id}")
            return auction_pb2.PlaceBidResponse(
                accepted=False, 
                error_message="Could not place bid, please try again",
                leader_hint=LEADER_HINT,
                updated_auction=auction_pb2.Auction()
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

            now_ms = datetime.datetime.now().timestamp() * 1000

            if self.llm is not None:
                try:
                    answer = await self.llm.get_answer(question, assistant.build_context(auction, bid_count, now_ms))
                    return auction_pb2.AskAssistantResponse(success=True, answer=answer, from_fallback=False)
                except grpc.aio.AioRpcError as exc:
                    log.warning("LLM node unavailable for AskAssistant (%s) - answering from facts", exc.code())

            return auction_pb2.AskAssistantResponse(
                success=True, answer=assistant.fallback_answer(auction, bid_count, now_ms), from_fallback=True
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


def settle_auction(record: dict) -> dict:
    """Mock escrow settlement for a closed auction. Nothing is charged: it
    records what a real payment step would hold, and logs it."""
    winner_id = record["current_highest_bidder_id"] or None
    final_price = float(record["current_highest_bid"]) if winner_id else 0.0
    premium, escrow_status = assistant.compute_settlement(final_price, winner_id is not None)

    database.create_settlement(
        record["auction_id"], winner_id, record["seller_id"], final_price, premium, escrow_status
    )

    if winner_id:
        log.info(
            "settlement auction=%s: %s escrowed for %s (%s + %s buyer's premium) -> released to %s once handed over [mock]",
            record["auction_id"], assistant.money(final_price + premium), winner_id,
            assistant.money(final_price), assistant.money(premium), record["seller_id"],
        )
    else:
        log.info("settlement auction=%s: no sale, nothing escrowed", record["auction_id"])

    return {"winner_id": winner_id, "final_price": final_price}


async def summarize_auction(llm: LLMClient | None, record: dict, settlement: dict) -> None:
    """Write the post-auction recap: the LLM node if it answers, a templated
    one if it doesn't. Runs in the background so a slow model never delays
    the timer or anything else."""
    auction_id = record["auction_id"]
    try:
        amounts = [float(b["amount"]) for b in database.get_bids_for_auction(auction_id)]
        text, from_fallback = None, True

        if llm is not None:
            try:
                text = await llm.summarize_auction(auction_id, record["title"], amounts, settlement["final_price"])
                from_fallback = False
            except grpc.aio.AioRpcError as exc:
                log.warning("LLM node unavailable for the recap of %s (%s) - using template", auction_id, exc.code())

        if text is None:
            winner = settlement["winner_id"].removeprefix("user_") if settlement["winner_id"] else None
            text = assistant.fallback_summary(record["title"], amounts, settlement["final_price"], winner)

        database.save_summary(auction_id, text, from_fallback)
        log.info("recap auction=%s (%s): %s", auction_id, "template" if from_fallback else "LLM", text)
    except Exception:
        log.exception("could not write the recap for auction %s", auction_id)


async def run_auction_timer(llm: LLMClient | None = None):
    """Closes auctions whose clock has run out, then settles every closed
    auction that has no settlement yet (winner determination + mock escrow)
    and kicks off its recap.

    Previously this didn't exist at all - an auction only closed
    reactively, if someone happened to try bidding on it after
    expiry (see create_bid). If nobody did, it stayed ACTIVE forever
    and no winner was ever recorded.

    Settling "closed and unsettled" (rather than only what this tick just
    closed) also covers auctions closed by that reactive path, and survives
    a restart. Timer state is local to this replica - each app server owns
    its own auctions in M1.
    """
    while True:
        try:
            now_ms = int(datetime.datetime.now().timestamp() * 1000)
            for record in database.close_expired_auctions(now_ms):
                if record["current_highest_bidder_id"]:
                    log.info(
                        "auction %s closed - winner=%s amount=%s (item=%s)",
                        record["auction_id"], record["current_highest_bidder_id"],
                        record["current_highest_bid"], record["title"],
                    )
                else:
                    log.info("auction %s closed - no bids placed (item=%s)", record["auction_id"], record["title"])

            for record in database.get_closed_unsettled():
                settlement = settle_auction(record)
                task = asyncio.create_task(summarize_auction(llm, record, settlement))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
        except Exception:
            # One bad tick must never kill the timer for the life of the process.
            log.exception("auction timer tick failed")
        await asyncio.sleep(TIMER_TICK_SECONDS)


async def serve():
    """Start gRPC server."""
    # database.init_database() was defined but never called anywhere -
    # the schema never got applied, so every query would fail with
    # "no such table" before any business logic even ran.
    database.init_database()

    llm = LLMClient(LLM_SERVER, timeout_seconds=LLM_TIMEOUT_SECONDS) if LLM_SERVER else None
    log.info("LLM node: %s", LLM_SERVER or "not configured (assistant features use plain-text fallbacks)")

    server = grpc.aio.server()
    auction_pb2_grpc.add_AuctionServiceServicer_to_server(AuctionServiceServicer(llm), server)
    server.add_insecure_port(f"[::]:{PORT}")
    log.info("app server starting on port %s", PORT)
    await server.start()

    timer_task = asyncio.create_task(run_auction_timer(llm))
    try:
        await server.wait_for_termination()
    finally:
        timer_task.cancel()
        if llm is not None:
            await llm.close()


if __name__ == "__main__":
    asyncio.run(serve())
