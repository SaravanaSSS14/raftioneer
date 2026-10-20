#!/usr/bin/env python3
"""Raft cluster status: each app server's role, term, view of the leader and
how far its log has got. The other scripts import it to find the leader.

Usage (stack up, ./scripts/generate_protos.sh already run):
    python scripts/cluster.py
    python scripts/cluster.py --watch      # live, twice a second - keep it open while you kill the leader
    python scripts/cluster.py --servers localhost:50051,localhost:50052
"""
import argparse
import sys
import time

import grpc

sys.path.insert(0, "generated")

import auction_pb2
import auction_pb2_grpc
import narrate

DEFAULT_SERVERS = ["localhost:50051", "localhost:50052", "localhost:50053"]


def statuses(servers: list[str], timeout: float = 1.0) -> list[tuple[str, object]]:
    """[(address, ClusterStatusResponse or None if unreachable), ...]"""
    result = []
    for address in servers:
        try:
            with grpc.insecure_channel(address) as channel:
                stub = auction_pb2_grpc.AuctionServiceStub(channel)
                result.append((address, stub.GetClusterStatus(auction_pb2.ClusterStatusRequest(), timeout=timeout)))
        except grpc.RpcError:
            result.append((address, None))
    return result


def find_leader(servers: list[str] = DEFAULT_SERVERS, timeout: float = 15.0) -> str:
    """The address (as given in `servers`) of the current leader. Waits for an
    election if there is none yet; raises TimeoutError if none appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        leaders = [address for address, s in statuses(servers) if s is not None and s.role == "LEADER"]
        if len(leaders) == 1:
            return leaders[0]
        # None yet, or (briefly, during a change of leader) an old one that
        # hasn't heard about the new term: ask again in a moment.
        time.sleep(0.2)
    raise TimeoutError(f"no leader among {servers} after {timeout:.0f}s")


def wait_until_replicated(servers: list[str], timeout: float = 10.0) -> bool:
    """Wait until every reachable node has applied everything the leader has
    committed - so a read from any of them sees the latest writes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        reachable = [s for _, s in statuses(servers) if s is not None]
        leaders = [s for s in reachable if s.role == "LEADER"]
        if leaders and all(s.last_applied >= leaders[0].commit_index for s in reachable):
            return True
        time.sleep(0.1)
    return False


def show(servers: list[str], timeout: float = 1.0) -> bool:
    """Print the status table once. True if there is a leader."""
    print(f"{'ADDRESS':<17} {'NODE':<14} {'ROLE':<10} {'TERM':>5} {'LEADER':<14} {'LOG':>6} {'COMMIT':>7} {'APPLIED':>8}")
    print("-" * 87)
    rows = statuses(servers, timeout=timeout)
    for address, s in rows:
        if s is None:
            print(f"{address:<17} {'-':<14} {narrate.role('DOWN', 10)}")
            continue
        print(
            f"{address:<17} {s.node_id:<14} {narrate.role(s.role, 10)} {s.term:>5} {s.leader_id or '-':<14} "
            f"{s.last_log_index:>6} {s.commit_index:>7} {s.last_applied:>8}"
        )
    leaders = [s for _, s in rows if s is not None and s.role == "LEADER"]
    if leaders:
        print(f"\nleader: {leaders[0].node_id} (term {leaders[0].term})")
    else:
        print("\nno leader right now")
        narrate.aside("no-leader")
    return bool(leaders)


def watch(servers: list[str]) -> int:
    """Redraw the table twice a second until Ctrl-C."""
    try:
        while True:
            print("\033[H\033[2J", end="")  # clear the terminal
            print(f"Raft cluster - {time.strftime('%H:%M:%S')}   (Ctrl-C to stop)\n")
            show(servers, timeout=0.3)
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--servers", default=",".join(DEFAULT_SERVERS), help="comma-separated app-server addresses")
    parser.add_argument("--watch", action="store_true", help="keep redrawing the table, twice a second")
    args = parser.parse_args()
    servers = [s for s in args.servers.split(",") if s]
    if args.watch:
        return watch(servers)
    return 0 if show(servers) else 1


if __name__ == "__main__":
    raise SystemExit(main())
