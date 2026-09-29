import asyncio
import logging
import os
import random

import grpc

import auction_pb2
import auction_pb2_grpc

NODE_ID = os.environ.get("NODE_ID", "client")
APP_SERVERS = [s for s in os.environ.get("APP_SERVERS", "").split(",") if s]
CYCLE_INTERVAL_SECONDS = 5

# Named bidders, each with a bidding style (how much they raise the price by),
# so a bidding war on the seeded campus items reads like real people competing.
# Accounts register themselves on first login. Set BIDDER_USERNAME /
# BIDDER_PASSWORD to bid as a single account instead.
BIDDER_PERSONAS = [
    # username, password, smallest and largest raise in Rs
    ("nisargh", "nisargh123", 25, 60),  # goes in big
    ("nahush", "nahush123", 10, 30),
    ("karan", "karan123", 3, 12),
    ("mrudul", "mrudul123", 1, 5),  # inches the price up
]
if os.environ.get("BIDDER_USERNAME"):
    BIDDER_PERSONAS = [(os.environ["BIDDER_USERNAME"], os.environ.get("BIDDER_PASSWORD", ""), 1, 10)]

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [%(levelname)s] node={NODE_ID} %(message)s",
)
log = logging.getLogger(__name__)


async def run_bidder_cycle(address: str) -> None:
    """One persona's turn against one app server: Login -> ListAuctions -> PlaceBid."""
    username, password, min_raise, max_raise = random.choice(BIDDER_PERSONAS)
    try:
        async with grpc.aio.insecure_channel(address) as channel:
            stub = auction_pb2_grpc.AuctionServiceStub(channel)

            login_resp = await stub.Login(
                auction_pb2.LoginRequest(username=username, password=password),
                timeout=3,
            )
            if not login_resp.success:
                log.info("%s: login failed for %s (%s)", address, username, login_resp.error_message or "no reason given")
                return
            log.info("%s: logged in as %s", address, username)

            list_resp = await stub.ListAuctions(
                auction_pb2.ListAuctionsRequest(status_filter=auction_pb2.ACTIVE),
                timeout=3,
            )
            if not list_resp.auctions:
                log.info("%s: no active auctions to bid on", address)
                return

            auction = random.choice(list_resp.auctions)
            # Start from the reserve if nobody has bid yet - a bid below it is refused.
            floor = max(auction.current_highest_bid, auction.reserve_price)
            bid_amount = round(floor + random.uniform(min_raise, max_raise), 2)

            bid_resp = await stub.PlaceBid(
                auction_pb2.PlaceBidRequest(
                    session_token=login_resp.session_token,
                    auction_id=auction.auction_id,
                    amount=bid_amount,
                ),
                timeout=3,
            )
            if bid_resp.accepted:
                log.info(
                    "%s: %s bid %.2f, accepted on auction=%s (item=%s)",
                    address, username, bid_amount, auction.auction_id, auction.item.title,
                )
            else:
                log.info("%s: %s's bid rejected (%s)", address, username, bid_resp.error_message or "no reason given")

    except grpc.aio.AioRpcError as exc:
        log.warning("%s: unreachable (%s)", address, exc.code())


async def main():
    if not APP_SERVERS:
        log.warning("APP_SERVERS is empty; nothing to do")
    while True:
        await asyncio.gather(*(run_bidder_cycle(addr) for addr in APP_SERVERS))
        await asyncio.sleep(CYCLE_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
