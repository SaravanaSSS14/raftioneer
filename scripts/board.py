#!/usr/bin/env python3
"""The auction board: every active auction, one screen, plus whether the
replicas agree on it. A single ListAuctions call per replica (no polling
loop) - meant to be run once, mid-demo, instead of narrating over
scrolling client logs. The AGREE column counts the replicas reporting the
same highest bid as the leader; a follower that is a heartbeat behind can
briefly show one fewer.

Usage (stack up, ./scripts/generate_protos.sh already run):
    python scripts/board.py
    python scripts/board.py --servers localhost:50051,localhost:50052
"""
import argparse
import sys
import time

import grpc

sys.path.insert(0, "generated")

import auction_pb2
import auction_pb2_grpc
from cluster import statuses

DEFAULT_SERVERS = "localhost:50051,localhost:50052,localhost:50053"
TIMEOUT_SECONDS = 3


def fetch_active(address: str) -> list:
    with grpc.insecure_channel(address) as channel:
        stub = auction_pb2_grpc.AuctionServiceStub(channel)
        resp = stub.ListAuctions(
            auction_pb2.ListAuctionsRequest(status_filter=auction_pb2.ACTIVE), timeout=TIMEOUT_SECONDS
        )
        return list(resp.auctions)


def format_time_left(seconds: int) -> str:
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--servers", default=DEFAULT_SERVERS, help="comma-separated app-server addresses")
    args = parser.parse_args()
    servers = [s for s in args.servers.split(",") if s]

    now_ms = time.time() * 1000
    by_replica = {}  # address -> {auction_id: Auction}
    unreachable = []

    for address in servers:
        try:
            by_replica[address] = {a.auction_id: a for a in fetch_active(address)}
        except grpc.RpcError as exc:
            unreachable.append((address, exc.code()))

    # Show the leader's view of each auction (or any replica's, if no leader answers).
    roles = {address: s.role for address, s in statuses(servers) if s is not None}
    order = sorted(by_replica, key=lambda address: roles.get(address) != "LEADER")
    auctions = {}
    for address in reversed(order):
        auctions.update(by_replica[address])

    rows = sorted(auctions.values(), key=lambda a: a.end_time_ms)  # soonest-closing first
    leader = next((address for address in order if roles.get(address) == "LEADER"), None)
    print(f"leader: {leader or 'none'}   replicas answering: {len(by_replica)}/{len(servers)}\n")
    print(f"{'ID':<9} {'ITEM':<38} {'HIGH BID':>10} {'RESERVE':>9} {'CLOSES IN':>10} {'EXT':>5} {'AGREE':>6}")
    print("-" * 93)
    for a in rows:
        seconds_left = max(0, int((a.end_time_ms - now_ms) / 1000))
        title_short = a.item.title if len(a.item.title) <= 36 else a.item.title[:35] + "…"
        reserve_str = f"{a.reserve_price:.2f}" if a.reserve_price else "—"
        agree = sum(
            1 for seen in by_replica.values()
            if a.auction_id in seen and seen[a.auction_id].current_highest_bid == a.current_highest_bid
        )
        # how far anti-sniping has pushed the end back
        extended = (a.end_time_ms - a.original_end_time_ms) // 1000 if a.original_end_time_ms else 0
        print(
            f"{a.auction_id:<9} {title_short:<38} {a.current_highest_bid:>10.2f} {reserve_str:>9} "
            f"{format_time_left(seconds_left):>10} {f'+{extended}s' if extended else '':>5} "
            f"{f'{agree}/{len(by_replica)}':>6}"
        )

    if not rows:
        print("(no active auctions on any reachable replica - try scripts/seed_demo_data.py)")

    for address, code in unreachable:
        print(f"\n! {address}: unreachable ({code})")

    return 0 if not unreachable else 1


if __name__ == "__main__":
    raise SystemExit(main())
