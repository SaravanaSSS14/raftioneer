import asyncio
import datetime
import logging
import os
import uuid

import grpc

import auction_pb2
import auction_pb2_grpc
from store import database, auth


NODE_ID = os.environ.get("NODE_ID", "app-server-unknown")
PORT = os.environ.get("PORT", "50051")

logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [%(levelname)s] node={NODE_ID} %(message)s")
log = logging.getLogger(__name__)


class AuctionServiceServicer(auction_pb2_grpc.AuctionServiceServicer):
    """Business logic implementation for auction operations."""

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

            password_hash = auth.hash_password(password)
            existing_user = database.get_user(username)

            if existing_user is None:
                user_id = f"user_{username}"
                database.create_user(username, user_id, password_hash)
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
            return auction_pb2.LoginResponse(success=False, error_message=str(e))

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

            created = database.create_item(
                item_id=auction_id,
                title=request.title,
                description=request.description,
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
            return auction_pb2.CreateAuctionResponse(success=False, error_message=str(e))

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
                    leader_hint=os.environ.get("PEERS", ""),
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
                    leader_hint=os.environ.get("PEERS", ""),
                    updated_auction=auction_pb2.Auction()
                )
            
            # Fetch updated auction after successful bid
            auction = database.get_auction(request.auction_id)
            
            return auction_pb2.PlaceBidResponse(
                accepted=True, 
                error_message="",
                leader_hint=os.environ.get("PEERS", ""),
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
                error_message=str(e),
                leader_hint=os.environ.get("PEERS", ""),
                updated_auction=auction_pb2.Auction()
            )


TIMER_TICK_SECONDS = 1


async def run_auction_timer():
    """Closes auctions whose clock has run out and logs the winner.

    Previously this didn't exist at all - an auction only closed
    reactively, if someone happened to try bidding on it after
    expiry (see create_bid). If nobody did, it stayed ACTIVE forever
    and no winner was ever recorded.

    M1 scope: runs in-process, local to this single app_server - no
    Raft yet. Week 5+ replaces this with Raft-replicated timer state
    per CLAUDE.md's architecture constraint (timer state must not
    live in local process memory once Raft exists).
    """
    while True:
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
        await asyncio.sleep(TIMER_TICK_SECONDS)


async def serve():
    """Start gRPC server."""
    # database.init_database() was defined but never called anywhere -
    # the schema never got applied, so every query would fail with
    # "no such table" before any business logic even ran.
    database.init_database()
    server = grpc.aio.server()
    auction_pb2_grpc.add_AuctionServiceServicer_to_server(AuctionServiceServicer(), server)
    server.add_insecure_port(f"[::]:{PORT}")
    log.info("app server starting on port %s", PORT)
    await server.start()

    timer_task = asyncio.create_task(run_auction_timer())
    try:
        await server.wait_for_termination()
    finally:
        timer_task.cancel()


if __name__ == "__main__":
    asyncio.run(serve())
