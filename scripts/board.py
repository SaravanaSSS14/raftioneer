#!/usr/bin/env python3
"""The auction board: every active auction, across every replica, one
screen. A single ListAuctions call per replica (no new RPC, no polling
loop) - meant to be run once, mid-demo, instead of narrating over
scrolling client logs.

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
    rows = []  # (seconds_left, replica, auction_id, title, highest_bid, reserve)
    unreachable = []

    for address in servers:
        try:
            for a in fetch_active(address):
                seconds_left = max(0, int((a.end_time_ms - now_ms) / 1000))
                rows.append((seconds_left, address, a.auction_id, a.item.title, a.current_highest_bid, a.reserve_price))
        except grpc.RpcError as exc:
            unreachable.append((address, exc.code()))

    rows.sort(key=lambda r: r[0])  # soonest-closing first

    print(f"{'REPLICA':<16} {'ID':<9} {'ITEM':<38} {'HIGH BID':>10} {'RESERVE':>9} {'CLOSES IN':>10}")
    print("-" * 96)
    for seconds_left, address, auction_id, title, highest_bid, reserve in rows:
        title_short = title if len(title) <= 36 else title[:35] + "…"
        reserve_str = f"{reserve:.2f}" if reserve else "—"
        print(
            f"{address:<16} {auction_id:<9} {title_short:<38} {highest_bid:>10.2f} {reserve_str:>9} "
            f"{format_time_left(seconds_left):>10}"
        )

    if not rows:
        print("(no active auctions on any reachable replica - try scripts/seed_demo_data.py)")

    for address, code in unreachable:
        print(f"\n! {address}: unreachable ({code})")

    return 0 if not unreachable else 1


if __name__ == "__main__":
    raise SystemExit(main())
