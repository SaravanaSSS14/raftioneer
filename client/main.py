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

# A pool of named bidders instead of one identity pinging every
# server — makes a bidding war on scripts/seed_demo_data.py's campus
# clearance items look like several real people competing, not one
# account testing itself against 3 replicas. Login auto-registers on
# first use (same account for the whole demo run after that). Override
# BIDDER_USERNAME/BIDDER_PASSWORD to pin a single account instead.
BIDDER_PERSONAS = [
    ("deepika_hostel3", "deepika123"),
    ("karan_hostel5", "karan123"),
    ("ananya_ug1", "ananya123"),
    ("farhan_hostel8", "farhan123"),
]
if os.environ.get("BIDDER_USERNAME"):
    BIDDER_PERSONAS = [(os.environ["BIDDER_USERNAME"], os.environ.get("BIDDER_PASSWORD", ""))]

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [%(levelname)s] node={NODE_ID} %(message)s",
)
log = logging.getLogger(__name__)


async def run_bidder_cycle(address: str) -> None:
    """Walks the real client flow against one app server: Login ->
    ListAuctions -> PlaceBid. Written against the frozen .proto contract,
    not today's stub responses — every step just logs how far it got,
    so this same code keeps working (and does more) as real business
    logic lands, with no changes needed here."""
    username, password = random.choice(BIDDER_PERSONAS)
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
            # A real bidder checks the reserve, not just the current
            # highest bid — otherwise every early bid on a seeded item
            # with a reserve_price would bid too low to ever succeed.
            floor = max(auction.current_highest_bid, auction.reserve_price)
            bid_amount = floor + round(random.uniform(1, 10), 2)

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
