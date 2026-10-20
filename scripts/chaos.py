#!/usr/bin/env python3
"""Chaos run: inject failures into the Raft cluster while bids keep coming,
then prove nothing went wrong.

Throughout the run:
  * a chaos bidder places rising bids, one at a time, through whoever is
    leader. Each bid has its own request_id, and is retried - with the SAME
    id - through timeouts, redirects and elections until it gets a definite
    answer. That is exactly what exactly-once bidding is for.
  * an observer polls every node's role and term 20 times a second, to catch
    two nodes ever claiming leadership of the same term.
  * the client node's personas keep bidding on the other auctions as usual.

Each round injects one fault, heals it, and waits for the cluster to settle:
  kill-leader     SIGKILL the leader's container (a crash), restart it later
  kill-follower   SIGKILL one follower, restart it later
  freeze-leader   docker pause the leader (a "zombie": alive but silent, like a
                  long GC pause), unpause it after a new leader has taken over
  lose-majority   SIGKILL both followers: the leader is left alone, appends bids
                  it can't commit, and they time out; the bidder retries with
                  the same request_id, and when the followers return the
                  original entries commit - the retries must not count twice
  partition       cut the leader off the network while it keeps running, then
                  reconnect it

At the end, with the client node paused so nothing moves:
  1. no term ever had two leaders
  2. scripts/consistency_check.py passes (identical digests, identical data,
     sane bid histories)
  3. every bid the chaos bidder was told was accepted is in the history
     exactly once, and every one of its bids in the history was reported to
     it as accepted - none lost, none doubled, none wrongly reported as failed

Usage (stack up via docker compose, run from the repository root):
    python scripts/chaos.py                      # 6 rounds, random faults
    python scripts/chaos.py --rounds 10 --seed 7
    python scripts/chaos.py --faults kill-leader,freeze-leader
"""
import argparse
import asyncio
import random
import sys
import time
import uuid

import grpc

sys.path.insert(0, "generated")

import auction_pb2
import auction_pb2_grpc
from cluster import DEFAULT_SERVERS
from consistency_check import check

COMPOSE = ["docker", "compose", "-f", "deploy/docker-compose.yml"]
FAULTS = ["kill-leader", "kill-follower", "freeze-leader", "lose-majority", "partition"]
START_AMOUNT = 100_000.0  # far above the personas' budgets, so they leave this auction alone
BID_INTERVAL = 0.25
GIVE_UP_AFTER = 45.0  # seconds one bid may keep retrying before it is recorded as unknown


def service_of(node_id: str) -> str:
    return node_id.replace("-", "_")  # app-server-1 -> compose service app_server_1


async def run_cmd(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {err.decode().strip()}")
    return out.decode().strip()


class Cluster:
    def __init__(self, servers: list[str]):
        self.servers = servers
        self.channels = {a: grpc.aio.insecure_channel(a) for a in servers}

    def stub(self, address: str):
        return auction_pb2_grpc.AuctionServiceStub(self.channels[address])

    async def statuses(self, timeout: float = 0.5) -> dict[str, object]:
        async def one(address):
            try:
                s = await self.stub(address).GetClusterStatus(auction_pb2.ClusterStatusRequest(), timeout=timeout)
                return address, s
            except grpc.aio.AioRpcError:
                return address, None
        return dict(await asyncio.gather(*(one(a) for a in self.servers)))

    async def leader(self, among=None, timeout: float = 30.0):
        """(address, status) of the single leader among `among`; waits for one."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = await self.statuses()
            leaders = [(a, s) for a, s in found.items() if s and s.role == "LEADER" and (among is None or a in among)]
            if leaders:
                return max(leaders, key=lambda pair: pair[1].term)
            await asyncio.sleep(0.1)
        raise TimeoutError("no leader")

    async def wait_stable(self, timeout: float = 45.0) -> float:
        """Until all nodes are reachable, agree on one leader and term, and
        have applied everything it committed. Returns how long that took."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            found = list((await self.statuses()).values())
            if all(found):
                leaders = [s for s in found if s.role == "LEADER"]
                if (len(leaders) == 1 and all(s.leader_id == leaders[0].node_id for s in found)
                        and all(s.last_applied >= leaders[0].commit_index for s in found)):
                    return time.monotonic() - start
            await asyncio.sleep(0.2)
        raise TimeoutError("the cluster did not settle")


class ChaosBidder:
    """Bids on one auction, retrying each bid with its own request_id until
    the cluster gives a definite answer."""

    def __init__(self, cluster: Cluster, token: str, auction_id: str):
        self.cluster, self.token, self.auction_id = cluster, token, auction_id
        self.amount = START_AMOUNT
        self.outcomes: dict[float, str] = {}  # amount -> accepted / refused / unknown
        self.retried = 0  # bids that needed more than one attempt
        self.replayed = 0  # retries the cluster recognised as already applied
        self.running = True

    async def place(self, amount: float) -> None:
        request_id = uuid.uuid4().hex
        deadline = time.monotonic() + GIVE_UP_AFTER
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            try:
                address, _ = await self.cluster.leader(timeout=5)
                resp = await self.cluster.stub(address).PlaceBid(
                    auction_pb2.PlaceBidRequest(
                        session_token=self.token, auction_id=self.auction_id, amount=amount, request_id=request_id,
                    ),
                    timeout=3,
                )
            except (grpc.aio.AioRpcError, TimeoutError):
                await asyncio.sleep(0.2)
                continue  # unreachable or no leader yet: same request, again
            if resp.accepted or not (resp.leader_hint or "could not confirm" in resp.error_message
                                     or "No leader" in resp.error_message):
                self.outcomes[amount] = "accepted" if resp.accepted else f"refused: {resp.error_message}"
                self.retried += attempts > 1
                self.replayed += resp.replayed
                return
            await asyncio.sleep(0.2)  # redirected, or the commit timed out: same request, again
        self.outcomes[amount] = "unknown"

    async def run(self) -> None:
        # One bid at a time: each waits for its definite answer before the next,
        # so a refusal can only mean something went wrong (a bid racing a higher
        # bid of our own would be refused legitimately, and prove nothing).
        while self.running:
            self.amount += 10
            await self.place(self.amount)
            await asyncio.sleep(BID_INTERVAL)


class Observer:
    """Samples every node's role and term, to catch two leaders in one term."""

    def __init__(self, cluster: Cluster):
        self.cluster = cluster
        self.leaders_by_term: dict[int, set] = {}
        self.samples = 0
        self.running = True

    async def run(self) -> None:
        while self.running:
            for status in (await self.cluster.statuses(timeout=0.3)).values():
                if status is not None and status.role == "LEADER":
                    self.leaders_by_term.setdefault(status.term, set()).add(status.node_id)
            self.samples += 1
            await asyncio.sleep(0.05)


async def network_of(container: str) -> str:
    return (await run_cmd("docker", "inspect", "-f",
                          "{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{end}}", container))


async def inject(fault: str, cluster: Cluster, rng: random.Random) -> str:
    """Inject one fault, heal it, and describe what happened."""
    leader_address, leader = await cluster.leader()
    found = await cluster.statuses()
    followers = [s.node_id for s in found.values() if s and s.node_id != leader.node_id]
    others = [a for a in cluster.servers if a != leader_address]

    if fault == "kill-leader":
        await run_cmd(*COMPOSE, "kill", service_of(leader.node_id))
        t0 = time.monotonic()
        _, new = await cluster.leader(among=others)
        took = time.monotonic() - t0
        await asyncio.sleep(1.5)
        await run_cmd(*COMPOSE, "start", service_of(leader.node_id))
        return (f"killed leader {leader.node_id} (term {leader.term}) -> {new.node_id} leads term {new.term} "
                f"after {took:.2f}s; {leader.node_id} restarted")

    if fault == "kill-follower":
        victim = rng.choice(followers)
        await run_cmd(*COMPOSE, "kill", service_of(victim))
        await asyncio.sleep(3)
        await run_cmd(*COMPOSE, "start", service_of(victim))
        return f"killed follower {victim} for 3s; {leader.node_id} kept leading on a 2-of-3 majority"

    if fault == "freeze-leader":
        await run_cmd(*COMPOSE, "pause", service_of(leader.node_id))
        t0 = time.monotonic()
        _, new = await cluster.leader(among=others)
        took = time.monotonic() - t0
        await asyncio.sleep(1.0)
        await run_cmd(*COMPOSE, "unpause", service_of(leader.node_id))
        return (f"froze leader {leader.node_id} (term {leader.term}) -> {new.node_id} leads term {new.term} "
                f"after {took:.2f}s; the zombie woke up in a stale term and stepped down")

    if fault == "lose-majority":
        await run_cmd(*COMPOSE, "kill", *map(service_of, followers))
        await asyncio.sleep(4)
        await run_cmd(*COMPOSE, "start", *map(service_of, followers))
        return (f"killed both followers - {leader.node_id} was alone for 4s and could not commit, "
                f"bids timed out and were retried; followers restarted")

    if fault == "partition":
        container = await run_cmd(*COMPOSE, "ps", "-q", service_of(leader.node_id))
        network = await network_of(container)
        await run_cmd("docker", "network", "disconnect", network, container)
        t0 = time.monotonic()
        try:
            _, new = await cluster.leader(among=others)
            took = time.monotonic() - t0
            await asyncio.sleep(1.5)
        finally:
            await run_cmd("docker", "network", "connect", "--alias", service_of(leader.node_id), network, container)
        return (f"cut leader {leader.node_id} (term {leader.term}) off the network -> {new.node_id} leads term "
                f"{new.term} after {took:.2f}s; reconnected")

    raise ValueError(fault)


async def main_async(args) -> int:
    servers = [s for s in args.servers.split(",") if s]
    faults = [f for f in args.faults.split(",") if f]
    rng = random.Random(args.seed)
    cluster = Cluster(servers)
    failures = []

    print("Setting up")
    await cluster.wait_stable()
    leader_address, leader = await cluster.leader()
    stub = cluster.stub(leader_address)
    seller = await stub.Login(auction_pb2.LoginRequest(username="chaos_seller", password="chaos12345"), timeout=5)
    bidder = await stub.Login(auction_pb2.LoginRequest(username="chaos_bidder", password="chaos12345"), timeout=5)
    created = await stub.CreateAuction(
        auction_pb2.CreateAuctionRequest(
            session_token=seller.session_token, title="Chaos-run item",
            description="Created by scripts/chaos.py", duration_seconds=1800,
        ),
        timeout=5,
    )
    if not (seller.success and bidder.success and created.success):
        print("  could not set up the chaos auction")
        return 1
    auction_id = created.auction_id
    print(f"  leader {leader.node_id} (term {leader.term}); chaos auction {auction_id}; "
          f"{args.rounds} rounds of {', '.join(faults)}")

    chaos_bidder = ChaosBidder(cluster, bidder.session_token, auction_id)
    observer = Observer(cluster)
    tasks = [asyncio.create_task(chaos_bidder.run()), asyncio.create_task(observer.run())]

    started = time.monotonic()
    for round_no in range(1, args.rounds + 1):
        await asyncio.sleep(2)  # let bids flow between faults
        fault = rng.choice(faults)
        try:
            what = await inject(fault, cluster, rng)
            settled = await cluster.wait_stable()
            print(f"  round {round_no}: {what}; stable again in {settled:.1f}s")
        except Exception as exc:  # report and carry on; the final checks will judge the damage
            print(f"  round {round_no}: {fault} FAILED - {exc}")
            failures.append(f"round {round_no} ({fault}): {exc}")
            await cluster.wait_stable(timeout=60)

    print(f"\nStopping the load ({time.monotonic() - started:.0f}s of chaos)")
    chaos_bidder.running = False
    observer.running = False
    await asyncio.gather(*tasks)
    try:
        await run_cmd(*COMPOSE, "pause", "client")  # freeze the personas so nothing moves while we compare
        client_paused = True
    except RuntimeError:
        client_paused = False  # client node not running: nothing to pause
    try:
        await cluster.wait_stable()
        print("\nChecking")
        doubled = {t: ids for t, ids in observer.leaders_by_term.items() if len(ids) > 1}
        if doubled:
            failures.append(f"two leaders in one term: {doubled}")
        print(f"  {'FAIL' if doubled else 'PASS'}  one leader per term - {len(observer.leaders_by_term)} terms "
              f"seen in {observer.samples} samples")

        problems = await asyncio.to_thread(check, servers)
        failures += problems
        print(f"  {'FAIL' if problems else 'PASS'}  every replica identical: same applied digest, auctions, "
              f"bids and outcomes" + "".join(f"\n        {p}" for p in problems))

        leader_address, _ = await cluster.leader()
        history = (await cluster.stub(leader_address).ListBids(
            auction_pb2.ListBidsRequest(auction_id=auction_id), timeout=5)).bids
        ours = [b.amount for b in history if b.bidder_id == bidder.user_id]
        accepted = sorted(a for a, o in chaos_bidder.outcomes.items() if o == "accepted")
        unknown = [a for a, o in chaos_bidder.outcomes.items() if o == "unknown"]
        refused = {a: o for a, o in chaos_bidder.outcomes.items() if o.startswith("refused")}
        exact = sorted(ours) == accepted and len(ours) == len(set(ours))
        if not exact:
            failures.append("bid history does not match what the chaos bidder was told")
        print(f"  {'PASS' if exact else 'FAIL'}  exactly once: {len(accepted)} bids reported accepted, "
              f"{len(ours)} in the history, none doubled, none missing")
        print(f"        {chaos_bidder.retried} bids needed retries through the faults; "
              f"{chaos_bidder.replayed} of those had already been applied and were answered from the request table")
        if unknown or refused:
            failures.append(f"{len(unknown)} bids never got an answer, {len(refused)} were refused")
            print(f"  FAIL  {len(unknown)} bids never got an answer, {len(refused)} refused: {list(refused.values())[:3]}")
    finally:
        if client_paused:
            await run_cmd(*COMPOSE, "unpause", "client")

    print(f"\n{'PASS' if not failures else 'FAIL'}: {args.rounds} rounds of chaos" +
          (" - no split brain, no divergence, every bid exactly once" if not failures
           else f" - {len(failures)} problem(s)"))
    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--servers", default=",".join(DEFAULT_SERVERS), help="comma-separated app-server addresses")
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--seed", type=int, default=None, help="random seed, to replay a run's choice of faults")
    parser.add_argument("--faults", default=",".join(FAULTS), help=f"comma-separated subset of: {', '.join(FAULTS)}")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
