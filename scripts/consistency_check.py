#!/usr/bin/env python3
"""Consistency check: do all app servers hold exactly the same auction state,
and does that state make sense? Uses only public RPCs.

  1. Raft level: once every node has applied the same number of entries, the
     nodes' applied digests (a running hash of every entry they applied) must
     be identical - same input, in the same order, to every state machine.
  2. Data level: every replica must report the same auctions (status, highest
     bid and bidder, end times), the same bid history for each, and the same
     outcome for each closed auction.
  3. Invariants, per auction: every accepted bid beats the one before it, the
     highest bid is the last accepted bid, no bid id appears twice, and a
     settled auction's winner and price are its highest bid.

The client node keeps bidding while this runs, so a write can land between
reading two replicas; a mismatch is only reported if it is still there after
a few attempts. (scripts/chaos.py pauses the client node first.)

Usage (stack up, run from the repository root):
    python scripts/consistency_check.py
"""
import argparse
import sys
import time

import grpc

sys.path.insert(0, "generated")

import auction_pb2
import auction_pb2_grpc
from cluster import DEFAULT_SERVERS, statuses
import narrate

TIMEOUT = 5


def _stub(address: str):
    return auction_pb2_grpc.AuctionServiceStub(grpc.insecure_channel(address))


def check_digests(servers: list[str], timeout: float = 15.0) -> list[str]:
    """Raft level. Waits for a moment when every node has applied the same
    number of entries, then compares their digests."""
    deadline = time.time() + timeout
    last_seen = ""
    while time.time() < deadline:
        reachable = [s for _, s in statuses(servers) if s is not None]
        if len(reachable) < len(servers):
            last_seen = f"only {len(reachable)} of {len(servers)} app servers reachable"
        elif len({s.last_applied for s in reachable}) == 1:
            digests = {s.node_id: s.applied_digest for s in reachable}
            if len(set(digests.values())) == 1:
                return []
            return [f"different applied digests at index {reachable[0].last_applied}: {digests}"]
        else:
            last_seen = "nodes never settled on the same applied index"
        time.sleep(0.1)
    return [f"could not compare digests: {last_seen}"]


def snapshot(address: str) -> dict:
    """Everything a replica reports about every auction."""
    stub = _stub(address)
    auctions = stub.ListAuctions(auction_pb2.ListAuctionsRequest(), timeout=TIMEOUT).auctions
    state = {}
    for a in auctions:
        bids = stub.ListBids(auction_pb2.ListBidsRequest(auction_id=a.auction_id), timeout=TIMEOUT).bids
        summary = stub.GetAuctionSummary(
            auction_pb2.GetAuctionSummaryRequest(auction_id=a.auction_id), timeout=TIMEOUT
        )
        state[a.auction_id] = {
            "auction": (a.status, a.current_highest_bid, a.current_highest_bidder_id,
                        a.end_time_ms, a.original_end_time_ms, a.reserve_price),
            "bids": [(b.bid_id, b.bidder_id, b.amount, b.timestamp_ms) for b in bids],
            "outcome": (summary.closed, summary.winner_id, summary.final_price, summary.buyers_premium,
                        summary.escrow_status, summary.summary),
        }
    return state


def check_replicas_agree(servers: list[str]) -> list[str]:
    snapshots = {address: snapshot(address) for address in servers}
    reference_address, reference = next(iter(snapshots.items()))
    problems = []
    for address, state in snapshots.items():
        if address == reference_address:
            continue
        if state.keys() != reference.keys():
            missing = sorted(reference.keys() - state.keys())
            extra = sorted(state.keys() - reference.keys())
            problems.append(f"{address} vs {reference_address}: auctions differ (missing {missing}, extra {extra})")
        for auction_id in state.keys() & reference.keys():
            for part in ("auction", "bids", "outcome"):
                if state[auction_id][part] != reference[auction_id][part]:
                    problems.append(f"{address} vs {reference_address}: auction {auction_id} differs in {part}")
    return problems


def check_invariants(address: str) -> list[str]:
    problems = []
    for auction_id, data in snapshot(address).items():
        status, highest, highest_bidder = data["auction"][:3]
        bids = data["bids"]
        ids = [b[0] for b in bids]
        if len(ids) != len(set(ids)):
            problems.append(f"auction {auction_id}: a bid id appears twice")
        amounts = [b[2] for b in bids]
        if any(later <= earlier for earlier, later in zip(amounts, amounts[1:])):
            problems.append(f"auction {auction_id}: an accepted bid did not beat the one before it")
        if bids and (highest != bids[-1][2] or highest_bidder != bids[-1][1]):
            problems.append(f"auction {auction_id}: highest bid {highest} is not the last accepted bid {bids[-1][2]}")
        if not bids and highest != 0:
            problems.append(f"auction {auction_id}: highest bid {highest} with no bids recorded")
        closed, winner, final_price = data["outcome"][:3]
        if closed and data["outcome"][4]:  # settled
            expected = (bids[-1][1], bids[-1][2]) if bids else ("", 0.0)
            if (winner, final_price) != expected:
                problems.append(f"auction {auction_id}: settled to {winner} at {final_price}, expected {expected}")
    return problems


def check(servers: list[str], attempts: int = 3) -> list[str]:
    """Every check; [] means consistent."""
    problems = check_digests(servers)
    if problems:
        return problems
    for attempt in range(attempts):
        problems = check_replicas_agree(servers)
        if not problems:
            break
        time.sleep(0.5)  # likely a write landing mid-read: look again
    return problems + check_invariants(servers[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--servers", default=",".join(DEFAULT_SERVERS), help="comma-separated app-server addresses")
    args = parser.parse_args()
    servers = [s for s in args.servers.split(",") if s]

    problems = check(servers)
    reachable = [s for _, s in statuses(servers) if s is not None]
    if reachable:
        s = reachable[0]
        print(f"applied index {s.last_applied}, digest {s.applied_digest[:16]}... on {len(reachable)} node(s)")
    if problems:
        for p in problems:
            narrate.fail(p)
        narrate.verdict(False, "the replicas are not consistent")
        return 1
    narrate.verdict(True, "every replica holds the same state, and every auction's bids, highest bid and outcome agree")
    narrate.aside("consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
