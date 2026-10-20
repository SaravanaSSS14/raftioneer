#!/usr/bin/env python3
"""Leader failover check: kill the Raft leader mid-auction and verify that
nothing committed is lost.

  1. Bids are placed on a fresh auction through the current leader.
  2. The leader's container is killed (SIGKILL - a crash, not a clean stop).
  3. The surviving nodes elect a new leader; the time it takes is measured.
  4. The new leader must report the same highest bid, and the bidders'
     existing sessions keep working - both were replicated before the crash.
  5. Bidding continues on the new leader.
  6. The old leader is restarted: it rejoins as a follower, rebuilds its state
     from its own log on disk, catches up, and reports the same final state.

Usage (stack up via docker compose, run from the repository root):
    python scripts/failover_check.py
    python scripts/failover_check.py --no-restart      # leave the old leader down
"""
import argparse
import subprocess
import sys
import time

import grpc

sys.path.insert(0, "generated")

import auction_pb2
import auction_pb2_grpc
from cluster import DEFAULT_SERVERS, find_leader, statuses, wait_until_replicated
import narrate
from narrate import aside

COMPOSE = ["docker", "compose", "-f", "deploy/docker-compose.yml"]
TIMEOUT = 5


def stub_for(address: str):
    return auction_pb2_grpc.AuctionServiceStub(grpc.insecure_channel(address))


def login(stub, username: str) -> str:
    resp = stub.Login(auction_pb2.LoginRequest(username=username, password="failover123"), timeout=TIMEOUT)
    if not resp.success:
        raise SystemExit(f"login failed for {username}: {resp.error_message}")
    return resp.session_token


def bid(stub, token: str, auction_id: str, amount: float) -> bool:
    resp = stub.PlaceBid(
        auction_pb2.PlaceBidRequest(session_token=token, auction_id=auction_id, amount=amount), timeout=TIMEOUT
    )
    return resp.accepted


def highest_bid(address: str, auction_id: str) -> float | None:
    resp = stub_for(address).GetAuction(auction_pb2.GetAuctionRequest(auction_id=auction_id), timeout=TIMEOUT)
    return resp.auction.current_highest_bid if resp.found else None


def node_of(address: str) -> str:
    return next(s.node_id for a, s in statuses([address]) if s is not None)


def check(ok: bool, message: str, failures: list) -> None:
    if not narrate.check(ok, message):
        failures.append(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--servers", default=",".join(DEFAULT_SERVERS), help="comma-separated app-server addresses")
    parser.add_argument("--no-restart", action="store_true", help="leave the killed leader down at the end")
    args = parser.parse_args()
    servers = [s for s in args.servers.split(",") if s]
    failures: list[str] = []

    print("1. Bidding through the current leader")
    leader = find_leader(servers)
    leader_node = node_of(leader)
    term_before = next(s.term for a, s in statuses([leader]) if s is not None)
    print(f"   leader: {leader_node} at {leader} (term {term_before})")
    stub = stub_for(leader)
    seller = login(stub, "failover_seller")
    bidders = [login(stub, "failover_bidder_a"), login(stub, "failover_bidder_b")]
    created = stub.CreateAuction(
        auction_pb2.CreateAuctionRequest(
            session_token=seller, title="Failover-check item", description="Created by scripts/failover_check.py",
            duration_seconds=120,
        ),
        timeout=TIMEOUT,
    )
    if not created.success:
        raise SystemExit(f"could not create the auction: {created.error_message}")
    auction_id = created.auction_id
    amount = 10_000.0  # above the client node's budgets, so its personas leave this auction alone
    for i in range(10):
        amount += 10
        bid(stub, bidders[i % 2], auction_id, amount)
    before = highest_bid(leader, auction_id)
    wait_until_replicated(servers)
    print(f"   auction {auction_id}: 10 bids placed, highest {before:.2f}, replicated to every node")

    service = leader_node.replace("-", "_")  # app-server-1 -> compose service app_server_1
    print(f"\n2. Killing the leader ({service}) with SIGKILL")
    subprocess.run(COMPOSE + ["kill", service], check=True, capture_output=True)
    killed_at = time.time()
    aside("kill", node=leader_node)

    print("\n3. Waiting for the surviving nodes to elect a new leader")
    survivors = [s for s in servers if s != leader]
    new_leader = find_leader(survivors, timeout=20)
    elapsed = time.time() - killed_at
    new_status = next(s for a, s in statuses([new_leader]) if s is not None)
    print(f"   new leader: {new_status.node_id} at {new_leader} (term {new_status.term}), {elapsed:.2f}s after the kill")
    check(new_status.term > term_before, "the new leader was elected in a later term", failures)
    aside("elected", node=new_status.node_id, seconds=elapsed)

    print("\n4. Checking nothing committed was lost")
    after_failover = highest_bid(new_leader, auction_id)
    check(after_failover == before, f"new leader reports the same highest bid ({after_failover:.2f})", failures)
    new_stub = stub_for(new_leader)
    accepted = bid(new_stub, bidders[0], auction_id, amount + 10)
    check(accepted, "a bidder's existing session still works on the new leader (sessions are replicated)", failures)
    if accepted:
        aside("sessions")

    print("\n5. Bidding continues on the new leader")
    amount += 10
    for i in range(5):
        amount += 10
        bid(new_stub, bidders[(i + 1) % 2], auction_id, amount)
    final = highest_bid(new_leader, auction_id)
    check(final == amount, f"5 more bids committed, highest now {final:.2f}", failures)

    if args.no_restart:
        print(f"\n(old leader left down - bring it back with: docker compose -f deploy/docker-compose.yml start {service})")
    else:
        print(f"\n6. Restarting the old leader ({service})")
        subprocess.run(COMPOSE + ["start", service], check=True, capture_output=True)
        restarted_at = time.time()
        caught_up = False
        while time.time() - restarted_at < 30:
            status = next((s for a, s in statuses([leader]) if s is not None), None)
            if status is not None and wait_until_replicated(servers, timeout=1):
                caught_up = True
                break
            time.sleep(0.5)
        status = next((s for a, s in statuses([leader]) if s is not None), None)
        check(caught_up, f"{leader_node} rejoined and caught up in {time.time() - restarted_at:.1f}s", failures)
        if status is not None:
            check(status.role == "FOLLOWER", f"{leader_node} is now a follower of {status.leader_id}", failures)
            rejoined = highest_bid(leader, auction_id)
            check(rejoined == final, f"{leader_node} reports the same final highest bid ({rejoined:.2f})", failures)
            if status.role == "FOLLOWER":
                aside("rejoin", node=leader_node)

    narrate.verdict(not failures, "leader failover " + (
        "lost nothing" if not failures else f"- {len(failures)} check(s) failed"))
    if not failures:
        aside("nothing-lost")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
