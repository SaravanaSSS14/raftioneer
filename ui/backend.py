"""The demo UI's way into the system: a gRPC client node, like client/main.py.

It talks to the three app servers over gRPC only - no back doors. Writes go
to the Raft leader (following a follower's leader_hint, or asking the cluster
again if the leader stops answering); reads go to the leader when it is up,
any other node when it isn't.

Two background pollers keep a shared snapshot that every open browser tab
draws from, so ten open tabs don't mean ten times the RPCs:
  - the cluster: each node's GetClusterStatus, twice a second, plus the
    events worth showing (a node going down, a new leader, ...)
  - the auctions: ListAuctions once a second, plus each closed auction's
    result (GetAuctionSummary) until its recap is in.
"""
from __future__ import annotations

import asyncio
import collections
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import grpc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "generated"))

import auction_pb2  # noqa: E402
import auction_pb2_grpc  # noqa: E402

# node id -> address on this machine (the compose file publishes the ports)
NODES = {
    "app-server-1": "localhost:50051",
    "app-server-2": "localhost:50052",
    "app-server-3": "localhost:50053",
}
ADDRESS_OF = dict(NODES)
NODE_OF = {address: node for node, address in NODES.items()}
# leader_hint carries the compose-network address; map it to the host port.
HINT_TO_HOST = {f"app_server_{i}:50051": f"localhost:5005{i}" for i in (1, 2, 3)}

WRITE_TIMEOUT = 4.0
LLM_TIMEOUT = 120.0  # the model runs on CPU


class Cluster:
    """gRPC channels to every app server, and which one is the leader."""

    def __init__(self) -> None:
        self._channels: dict[str, grpc.aio.Channel] = {}
        self.leader: str | None = None  # address

    def _channel(self, address: str) -> grpc.aio.Channel:
        if address not in self._channels:
            self._channels[address] = grpc.aio.insecure_channel(address)
        return self._channels[address]

    def auctions(self, address: str) -> auction_pb2_grpc.AuctionServiceStub:
        return auction_pb2_grpc.AuctionServiceStub(self._channel(address))

    def payments(self, address: str) -> auction_pb2_grpc.PaymentServiceStub:
        return auction_pb2_grpc.PaymentServiceStub(self._channel(address))

    async def status(self, address: str, timeout: float = 0.4):
        try:
            return await self.auctions(address).GetClusterStatus(auction_pb2.ClusterStatusRequest(), timeout=timeout)
        except grpc.aio.AioRpcError:
            return None

    async def find_leader(self) -> str | None:
        found = await asyncio.gather(*(self.status(a) for a in NODES.values()))
        leaders = [s for s in found if s is not None and s.role == "LEADER"]
        if not leaders:
            return None
        # During a change of leader an old one can briefly still think it leads:
        # the highest term is the real one.
        best = max(leaders, key=lambda s: s.term)
        return ADDRESS_OF[best.node_id]

    async def write(self, call: Callable[[str], Awaitable], attempts: int = 8):
        """Send a write to the leader. On a leader_hint, or if the leader is
        unreachable, find the leader and send it again - with the same request
        (and the same request_id, if it has one), so a retry is safe."""
        reply = None
        for _ in range(attempts):
            address = self.leader or await self.find_leader()
            if address is None:
                await asyncio.sleep(0.4)  # mid-election
                continue
            try:
                reply = await call(address)
            except grpc.aio.AioRpcError as exc:
                if exc.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                    self.leader = None
                    await asyncio.sleep(0.3)
                    continue
                raise
            hint = getattr(reply, "leader_hint", "")
            if hint:
                self.leader = HINT_TO_HOST.get(hint)
                await asyncio.sleep(0.15)
                continue
            self.leader = address
            return reply
        return reply

    async def read(self, call: Callable[[str], Awaitable]):
        """A read from the leader if it answers (freshest), else any node."""
        order = [self.leader] if self.leader else []
        order += [a for a in NODES.values() if a not in order]
        last_error = None
        for address in order:
            try:
                return await call(address)
            except grpc.aio.AioRpcError as exc:
                last_error = exc
                if address == self.leader:
                    self.leader = None
        raise last_error  # every node is down


cluster = Cluster()


# --------------------------------------------------------------- snapshots

@dataclass
class ClusterSnapshot:
    at: float = 0.0
    nodes: dict = field(default_factory=dict)  # node id -> ClusterStatusResponse | None (down)
    leader: str | None = None                  # node id
    term: int = 0
    version: int = 0                           # bumps when roles / up-down / leader change


@dataclass
class Event:
    at: float
    kind: str   # leader, down, up, role, info
    text: str


class Snapshots:
    def __init__(self) -> None:
        self.cluster = ClusterSnapshot()
        self.events: collections.deque[Event] = collections.deque(maxlen=60)
        self.event_count = 0
        self.auctions: list = []           # auction_pb2.Auction
        self.auctions_version = 0
        self.summaries: dict[str, object] = {}  # auction id -> GetAuctionSummaryResponse (closed only)
        self.leaderless_since: float | None = None
        self.last_election_seconds: float | None = None

    def event(self, kind: str, text: str) -> None:
        self.events.appendleft(Event(time.time(), kind, text))
        self.event_count += 1

    # ---- cluster ----

    async def poll_cluster(self) -> None:
        while True:
            try:
                await self._cluster_once()
            except Exception as exc:  # never let the poller die
                self.event("info", f"status poll failed: {exc}")
            await asyncio.sleep(0.5)

    async def _cluster_once(self) -> None:
        results = await asyncio.gather(*(cluster.status(a) for a in NODES.values()))
        nodes = dict(zip(NODES, results))
        live = {n: s for n, s in nodes.items() if s is not None}
        leaders = [s for s in live.values() if s.role == "LEADER"]
        leader = max(leaders, key=lambda s: s.term) if leaders else None
        old = self.cluster
        signature = lambda snap: (  # noqa: E731
            tuple((n, s.role if s else "DOWN") for n, s in snap.nodes.items()), snap.leader, snap.term)

        new = ClusterSnapshot(
            at=time.time(), nodes=nodes, leader=leader.node_id if leader else None,
            term=max((s.term for s in live.values()), default=0), version=old.version,
        )
        if old.at:
            for node, status in nodes.items():
                before = old.nodes.get(node)
                if before is not None and status is None:
                    self.event("down", f"{node} is down (no reply)")
                elif before is None and status is not None:
                    self.event("up", f"{node} is back, as {status.role.lower()} in term {status.term}")
            if new.leader != old.leader:
                if new.leader is None:
                    self.leaderless_since = time.time()
                    self.event("down", "no leader - the followers' election timers are running")
                else:
                    took = ""
                    if self.leaderless_since is not None:
                        self.last_election_seconds = time.time() - self.leaderless_since
                        took = f" after {self.last_election_seconds:.2f}s without one"
                    self.leaderless_since = None
                    self.event("leader", f"{new.leader} is the leader of term {new.term}{took}")
            elif new.leader is not None and new.term > old.term:
                # Same leader, later term: there was an election and it won again
                # (for example, a restarted node timed out before hearing from it).
                self.event("leader", f"{new.leader} won a new election and leads term {new.term}")
        elif new.leader:
            self.event("leader", f"{new.leader} is the leader of term {new.term}")
        if new.leader:
            cluster.leader = ADDRESS_OF[new.leader]
        if not old.at or signature(new) != signature(old):
            new.version = old.version + 1
        self.cluster = new

    # ---- auctions ----

    async def poll_auctions(self) -> None:
        while True:
            try:
                await self._auctions_once()
            except grpc.aio.AioRpcError:
                pass  # whole cluster down: keep the last snapshot
            except Exception as exc:
                self.event("info", f"auction poll failed: {exc}")
            await asyncio.sleep(1.0)

    async def _auctions_once(self) -> None:
        reply = await cluster.read(
            lambda a: cluster.auctions(a).ListAuctions(auction_pb2.ListAuctionsRequest(), timeout=1.5)
        )
        auctions = list(reply.auctions)
        signature = [(a.auction_id, a.status, a.current_highest_bid, a.end_time_ms) for a in auctions]
        if signature != [(a.auction_id, a.status, a.current_highest_bid, a.end_time_ms) for a in self.auctions]:
            self.auctions = auctions
            self.auctions_version += 1
        for auction in auctions:
            if auction.status != auction_pb2.CLOSED:
                continue
            known = self.summaries.get(auction.auction_id)
            if known is not None and known.summary_ready:
                continue
            summary = await cluster.read(lambda a, aid=auction.auction_id: cluster.auctions(a).GetAuctionSummary(
                auction_pb2.GetAuctionSummaryRequest(auction_id=aid), timeout=1.5))
            if known is None or summary.summary_ready != known.summary_ready or summary.escrow_status != known.escrow_status:
                self.summaries[auction.auction_id] = summary
                self.auctions_version += 1

    async def refresh_summary(self, auction_id: str) -> None:
        """Re-read one result now (after a hand-over changes its escrow)."""
        summary = await cluster.read(lambda a: cluster.auctions(a).GetAuctionSummary(
            auction_pb2.GetAuctionSummaryRequest(auction_id=auction_id), timeout=1.5))
        self.summaries[auction_id] = summary
        self.auctions_version += 1


snapshots = Snapshots()


# --------------------------------------------------------------- one user's calls

class Session:
    """One browser tab's signed-in user."""

    def __init__(self) -> None:
        self.token = ""
        self.username = ""
        self.user_id = ""

    @property
    def signed_in(self) -> bool:
        return bool(self.token)

    async def register(self, username: str, password: str):
        return await cluster.write(lambda a: cluster.auctions(a).Register(
            auction_pb2.RegisterRequest(username=username, password=password), timeout=WRITE_TIMEOUT))

    async def login(self, username: str, password: str):
        reply = await cluster.write(lambda a: cluster.auctions(a).Login(
            auction_pb2.LoginRequest(username=username, password=password), timeout=WRITE_TIMEOUT))
        if reply is not None and reply.success:
            self.token, self.username, self.user_id = reply.session_token, username, reply.user_id
        return reply

    async def logout(self) -> None:
        token, self.token = self.token, ""
        if token:
            await cluster.write(lambda a: cluster.auctions(a).Logout(
                auction_pb2.LogoutRequest(session_token=token), timeout=WRITE_TIMEOUT))

    async def wallet(self):
        return await cluster.read(lambda a: cluster.payments(a).GetWallet(
            auction_pb2.WalletRequest(session_token=self.token), timeout=1.5))

    async def transactions(self):
        return await cluster.read(lambda a: cluster.payments(a).ListTransactions(
            auction_pb2.WalletRequest(session_token=self.token), timeout=1.5))

    async def deposit(self, amount: float, request_id: str):
        return await cluster.write(lambda a: cluster.payments(a).Deposit(
            auction_pb2.DepositRequest(session_token=self.token, amount=amount, request_id=request_id),
            timeout=WRITE_TIMEOUT))

    async def bid(self, auction_id: str, amount: float, request_id: str):
        return await cluster.write(lambda a: cluster.auctions(a).PlaceBid(auction_pb2.PlaceBidRequest(
            session_token=self.token, auction_id=auction_id, amount=amount, request_id=request_id),
            timeout=WRITE_TIMEOUT))

    async def create(self, title: str, description: str, duration_seconds: int, reserve: float):
        # A blank description is written by the LLM node first: allow for it.
        return await cluster.write(lambda a: cluster.auctions(a).CreateAuction(auction_pb2.CreateAuctionRequest(
            session_token=self.token, title=title, description=description,
            duration_seconds=duration_seconds, reserve_price=reserve), timeout=LLM_TIMEOUT), attempts=3)

    async def confirm_receipt(self, auction_id: str):
        return await cluster.write(lambda a: cluster.payments(a).ConfirmReceipt(
            auction_pb2.ConfirmReceiptRequest(session_token=self.token, auction_id=auction_id),
            timeout=WRITE_TIMEOUT))

    async def ask(self, question: str, auction_id: str = ""):
        # A read: any node can ask the LLM node.
        return await cluster.read(lambda a: cluster.auctions(a).AskAssistant(auction_pb2.AskAssistantRequest(
            session_token=self.token, auction_id=auction_id, question=question), timeout=LLM_TIMEOUT))


async def bid_history(auction_id: str):
    reply = await cluster.read(lambda a: cluster.auctions(a).ListBids(
        auction_pb2.ListBidsRequest(auction_id=auction_id), timeout=1.5))
    return list(reply.bids)


async def get_auction(auction_id: str):
    """The auction as the leader has it now (None if not found)."""
    reply = await cluster.read(lambda a: cluster.auctions(a).GetAuction(
        auction_pb2.GetAuctionRequest(auction_id=auction_id), timeout=1.5))
    return reply.auction if reply.found else None
