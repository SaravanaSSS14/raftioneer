#!/usr/bin/env python3
"""Sample queries: a guided tour of what the running stack can do.

Walks one app server through the whole M1 story - browse, ask the LLM
assistant, list an item with no description (the LLM writes one), bid, and
read the outcome once the auction closes (winner, mock escrow settlement,
recap). Doubles as the "sample queries" for the README.

Usage (stack up, ./scripts/generate_protos.sh already run):
    python scripts/demo_queries.py                     # talks to the current Raft leader
    python scripts/demo_queries.py --server localhost:50052   # a specific node (writes need the leader)
"""
import argparse
import sys
import time
import uuid

import grpc

sys.path.insert(0, "generated")

import auction_pb2
import auction_pb2_grpc
from cluster import DEFAULT_SERVERS, find_leader

LLM_TIMEOUT_SECONDS = 120  # model runs on CPU; app server allows up to 60s per call


def heading(text: str) -> None:
    print(f"\n=== {text} ===")


def login(stub, username: str, password: str) -> str:
    resp = stub.Login(auction_pb2.LoginRequest(username=username, password=password), timeout=10)
    if not resp.success:
        raise SystemExit(f"login failed for {username}: {resp.error_message}")
    return resp.session_token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", help="app server to use (default: whichever is the Raft leader)")
    args = parser.parse_args()
    args.server = args.server or find_leader(DEFAULT_SERVERS)
    print(f"using {args.server}")

    with grpc.insecure_channel(args.server) as channel:
        stub = auction_pb2_grpc.AuctionServiceStub(channel)
        student = login(stub, "parm", "parm123")

        heading("1. Browse: active auctions")
        active = stub.ListAuctions(
            auction_pb2.ListAuctionsRequest(status_filter=auction_pb2.ACTIVE), timeout=10
        ).auctions
        if not active:
            print("(none - run scripts/seed_demo_data.py first)")
        for a in active[:6]:
            reserve = f", reserve {a.reserve_price:.0f}" if a.reserve_price else ""
            print(f"  [{a.auction_id}] {a.item.title}  - highest bid {a.current_highest_bid:.2f}{reserve}")

        if active:
            # a listing with a reserve price gives the assistant the most to explain
            target = next((a for a in active if a.reserve_price > 0), active[0])
            heading(f"2. Ask the assistant about '{target.item.title}'")
            q = "Is this worth bidding on, and can I just bid a little above the current price?"
            print(f"  Q: {q}")
            t0 = time.time()
            r = stub.AskAssistant(
                auction_pb2.AskAssistantRequest(session_token=student, auction_id=target.auction_id, question=q),
                timeout=LLM_TIMEOUT_SECONDS,
            )
            src = "plain-text fallback" if r.from_fallback else "LLM"
            print(f"  A ({src}, {time.time() - t0:.1f}s): {r.answer if r.success else r.error_message}")

        heading("3. Ask a general question (no auction)")
        q = "What is the buyer's premium and when do I pay it?"
        print(f"  Q: {q}")
        r = stub.AskAssistant(
            auction_pb2.AskAssistantRequest(session_token=student, question=q), timeout=LLM_TIMEOUT_SECONDS
        )
        print(f"  A ({'plain-text fallback' if r.from_fallback else 'LLM'}): {r.answer if r.success else r.error_message}")

        heading("4. List an item with NO description - the LLM writes one")
        seller = login(stub, "tanmay", "tanmay123")
        created = stub.CreateAuction(
            auction_pb2.CreateAuctionRequest(
                session_token=seller, title="Study lamp with adjustable arm", description="",
                duration_seconds=8, reserve_price=0.0,
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
        if not created.success:
            print(f"  could not create: {created.error_message}")
            return 1
        auction_id = created.auction_id
        item = stub.GetAuction(auction_pb2.GetAuctionRequest(auction_id=auction_id), timeout=10).auction
        print(f"  [{auction_id}] {item.item.title}\n  description: {item.item.description}")

        heading("5. Two students bid (8-second auction, with anti-sniping)")
        for username, password, amount in [
            ("nisargh", "nisargh123", 120.0),
            ("nahush", "nahush123", 150.0),
            ("nisargh", "nisargh123", 155.0),
            ("nahush", "nahush123", 100.0),  # too low - must be rejected
        ]:
            token = login(stub, username, password)
            resp = stub.PlaceBid(
                auction_pb2.PlaceBidRequest(session_token=token, auction_id=auction_id, amount=amount), timeout=10
            )
            verdict = "accepted" if resp.accepted else f"rejected ({resp.error_message})"
            print(f"  {username} bids {amount:.2f}: {verdict}")
        item = stub.GetAuction(auction_pb2.GetAuctionRequest(auction_id=auction_id), timeout=10).auction
        extended = (item.end_time_ms - item.original_end_time_ms) / 1000
        if extended > 0:
            print(f"  anti-sniping: bids in the closing seconds pushed the end back {extended:.1f}s "
                  "- nobody wins by bidding in the last instant")

        heading("5b. Nahush's phone loses signal and retries the same bid")
        token = login(stub, "nahush", "nahush123")
        request_id = uuid.uuid4().hex  # one id for the bid, reused on the retry
        for attempt in ("sent", "retried"):
            resp = stub.PlaceBid(
                auction_pb2.PlaceBidRequest(
                    session_token=token, auction_id=auction_id, amount=170.0, request_id=request_id
                ),
                timeout=10,
            )
            note = " - the original outcome, not a second bid" if resp.replayed else ""
            print(f"  {attempt}: {'accepted' if resp.accepted else resp.error_message} (replayed={resp.replayed}){note}")
        history = stub.ListBids(auction_pb2.ListBidsRequest(auction_id=auction_id), timeout=10).bids
        print(f"  bid history: {', '.join(f'{b.amount:.0f}' for b in history)} - the 170 is in it once")

        heading("6. Auction closes: winner, mock escrow, recap")
        deadline = time.time() + 150
        summary = None
        while time.time() < deadline:
            summary = stub.GetAuctionSummary(auction_pb2.GetAuctionSummaryRequest(auction_id=auction_id), timeout=10)
            if summary.closed and summary.summary_ready:
                break
            time.sleep(1)
        if not (summary and summary.closed and summary.summary_ready):
            print("  timed out waiting for the auction to close and be summarised")
            return 1
        print(f"  winner:        {summary.winner_id}")
        print(f"  final price:   {summary.final_price:.2f}  (+ buyer's premium {summary.buyers_premium:.2f})")
        print(f"  escrow:        {summary.escrow_status}")
        print(f"  recap ({'template' if summary.summary_from_fallback else 'LLM'}): {summary.summary}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
