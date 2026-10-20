import asyncio
import logging
import os
import random
import uuid

import grpc

import auction_pb2
import auction_pb2_grpc

NODE_ID = os.environ.get("NODE_ID", "client")
APP_SERVERS = [s for s in os.environ.get("APP_SERVERS", "").split(",") if s]
CYCLE_INTERVAL_SECONDS = 5

# Named bidders, each with a bidding style (how much they raise the price by)
# and a budget they won't go past, so a bidding war on the seeded campus items
# reads like real people competing - and runs out of steam like one. Accounts
# register themselves on first login. Set BIDDER_USERNAME / BIDDER_PASSWORD to
# bid as a single account instead.
BIDDER_PERSONAS = [
    # username, password, smallest and largest raise in Rs, budget in Rs
    ("nisargh", "nisargh123", 25, 60, 4000),  # goes in big
    ("nahush", "nahush123", 10, 30, 3000),
    ("karan", "karan123", 3, 12, 2500),
    ("mrudul", "mrudul123", 1, 5, 2000),  # inches the price up
]
if os.environ.get("BIDDER_USERNAME"):
    BIDDER_PERSONAS = [(os.environ["BIDDER_USERNAME"], os.environ.get("BIDDER_PASSWORD", ""), 1, 10, 5000)]

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [%(levelname)s] node={NODE_ID} %(message)s",
)
log = logging.getLogger(__name__)

# Writes (Login, PlaceBid) must go to the Raft leader; reads can go to any app
# server. The client remembers the leader, follows a follower's leader_hint
# when the leader changes, and asks the cluster again if the leader goes quiet.
_channels: dict[str, grpc.aio.Channel] = {}
_leader: str | None = None
_sessions: dict[str, str] = {}  # username -> session token


def stub(address: str) -> auction_pb2_grpc.AuctionServiceStub:
    if address not in _channels:
        _channels[address] = grpc.aio.insecure_channel(address)
    return auction_pb2_grpc.AuctionServiceStub(_channels[address])


async def find_leader() -> str | None:
    for address in APP_SERVERS:
        try:
            status = await stub(address).GetClusterStatus(auction_pb2.ClusterStatusRequest(), timeout=1)
        except grpc.aio.AioRpcError:
            continue
        if status.role == "LEADER":
            return address
    return None


def follow_hint(hint: str) -> None:
    """A follower told us where the leader is: use that from now on."""
    global _leader
    _leader = hint if hint in APP_SERVERS else None
    if _leader:
        log.info("redirected: the leader is now %s", _leader)


async def session_for(leader: str, username: str, password: str) -> str | None:
    if username in _sessions:
        return _sessions[username]
    resp = await stub(leader).Login(auction_pb2.LoginRequest(username=username, password=password), timeout=3)
    if not resp.success:
        if resp.leader_hint:
            follow_hint(resp.leader_hint)
        log.info("%s: login failed for %s (%s)", leader, username, resp.error_message or "no reason given")
        return None
    log.info("%s: logged in as %s", leader, username)
    _sessions[username] = resp.session_token
    return resp.session_token


async def run_bidder_cycle(persona: tuple) -> None:
    """One persona's turn: Login (leader) -> ListAuctions (any node) -> PlaceBid (leader)."""
    global _leader
    username, password, min_raise, max_raise, budget = persona
    try:
        leader = _leader or await find_leader()
        if leader is None:
            log.info("no leader elected right now - waiting for the cluster")
            return
        _leader = leader

        token = await session_for(leader, username, password)
        if token is None:
            return

        reader = random.choice(APP_SERVERS)  # any replica can serve reads
        list_resp = await stub(reader).ListAuctions(
            auction_pb2.ListAuctionsRequest(status_filter=auction_pb2.ACTIVE), timeout=3
        )
        if not list_resp.auctions:
            log.info("%s: no active auctions to bid on", reader)
            return

        # Start from the reserve if nobody has bid yet - a bid below it is refused.
        affordable = [
            a for a in list_resp.auctions
            if max(a.current_highest_bid, a.reserve_price) + min_raise <= budget
        ]
        if not affordable:
            log.info("%s: nothing %s can afford right now", reader, username)
            return

        auction = random.choice(affordable)
        floor = max(auction.current_highest_bid, auction.reserve_price)
        bid_amount = round(min(floor + random.uniform(min_raise, max_raise), budget), 2)

        bid_resp = await stub(leader).PlaceBid(
            auction_pb2.PlaceBidRequest(
                session_token=token, auction_id=auction.auction_id, amount=bid_amount,
                request_id=uuid.uuid4().hex,  # makes a retry safe (exactly-once)
            ),
            timeout=3,
        )
        if bid_resp.accepted:
            log.info(
                "%s: %s bid %.2f, accepted on auction=%s (item=%s)",
                leader, username, bid_amount, auction.auction_id, auction.item.title,
            )
            return
        if bid_resp.leader_hint:
            follow_hint(bid_resp.leader_hint)
        elif "session" in bid_resp.error_message.lower():
            _sessions.pop(username, None)  # expired or replaced: log in again next time
        log.info("%s: %s's bid rejected (%s)", leader, username, bid_resp.error_message or "no reason given")

    except grpc.aio.AioRpcError as exc:
        log.warning("app server unreachable (%s) - looking for the leader again", exc.code())
        _leader = None


async def main():
    if not APP_SERVERS:
        log.warning("APP_SERVERS is empty; nothing to do")
        return
    while True:
        # As many bidders per cycle as there are app servers, as before - each
        # a different persona, so they never fight over one session.
        personas = random.sample(BIDDER_PERSONAS, min(len(APP_SERVERS), len(BIDDER_PERSONAS)))
        await asyncio.gather(*(run_bidder_cycle(p) for p in personas))
        await asyncio.sleep(CYCLE_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
