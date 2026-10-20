"""Tests for the Raft implementation itself (app_server/raft), no Docker needed.

Nodes run in one process on one event loop, connected by an in-memory
network that can crash nodes and cut links. Timeouts are scaled down (an
election in a few hundred ms) so the whole file runs in seconds.
"""
import asyncio
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app_server"))

import raft_pb2  # noqa: E402
from raft import NotLeader, ProposalTimeout, RaftNode, RaftStorage, Role  # noqa: E402

ELECTION_TIMEOUT = (0.15, 0.30)
HEARTBEAT = 0.04


class Unreachable(Exception):
    pass


class Network:
    """Delivers RPCs between in-process nodes. A crashed node or a cut link
    makes the call raise, exactly as a failed gRPC call would."""

    def __init__(self):
        self.nodes: dict[str, RaftNode] = {}
        self.cut: set[frozenset] = set()

    def _check(self, src: str, dst: str) -> None:
        if src not in self.nodes or dst not in self.nodes or frozenset((src, dst)) in self.cut:
            raise Unreachable(f"{src} -> {dst}")

    async def deliver(self, src: str, dst: str, handler: str, request):
        await asyncio.sleep(0.002)
        self._check(src, dst)
        reply = getattr(self.nodes[dst], handler)(request)
        await asyncio.sleep(0.002)
        self._check(src, dst)  # the reply can be lost too
        return reply


class Transport:
    def __init__(self, network: Network, node_id: str):
        self.network, self.node_id = network, node_id

    async def request_vote(self, peer, request):
        return await self.network.deliver(self.node_id, peer, "handle_request_vote", request)

    async def append_entries(self, peer, request):
        return await self.network.deliver(self.node_id, peer, "handle_append_entries", request)


class Cluster:
    def __init__(self, tmp_path, size: int = 3, seed: int = 0):
        self.tmp_path = tmp_path
        self.ids = [f"n{i}" for i in range(1, size + 1)]
        self.network = Network()
        self.storages: dict[str, RaftStorage] = {}
        self.applied: dict[str, list[bytes]] = {}  # each node's state machine: the commands applied, in order
        self.rng = random.Random(seed)

    def start(self, node_id: str) -> RaftNode:
        storage = RaftStorage(str(self.tmp_path / f"{node_id}.db"))
        self.storages[node_id] = storage
        self.applied[node_id] = []  # a restarted node rebuilds its state from the log

        def apply(command: bytes, applied=self.applied[node_id]):
            applied.append(command)
            return len(applied)

        node = RaftNode(
            node_id, [p for p in self.ids if p != node_id], storage, Transport(self.network, node_id), apply,
            election_timeout=ELECTION_TIMEOUT, heartbeat_interval=HEARTBEAT,
            rng=random.Random(self.rng.random()),
        )
        self.network.nodes[node_id] = node
        node.start()
        return node

    def start_all(self) -> None:
        for node_id in self.ids:
            self.start(node_id)

    async def crash(self, node_id: str) -> None:
        node = self.network.nodes.pop(node_id)
        await node.stop()
        self.storages.pop(node_id).close()

    async def crash_all(self) -> None:
        for node_id in list(self.network.nodes):
            await self.crash(node_id)

    def isolate(self, node_id: str) -> None:
        for other in self.ids:
            if other != node_id:
                self.network.cut.add(frozenset((node_id, other)))

    def heal(self) -> None:
        self.network.cut.clear()

    def node(self, node_id: str) -> RaftNode:
        return self.network.nodes[node_id]

    def leaders(self, among=None) -> list[RaftNode]:
        nodes = [self.network.nodes[i] for i in (among or self.network.nodes)]
        return [n for n in nodes if n.role == Role.LEADER]

    async def wait_for_leader(self, among=None, timeout: float = 5.0) -> RaftNode:
        """A leader that a majority-reachable group agrees on, and that is ready."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            leaders = self.leaders(among)
            if leaders:
                top = max(leaders, key=lambda n: n.current_term)
                try:
                    await top.wait_until_ready(0.5)
                    return top
                except (NotLeader, ProposalTimeout):
                    pass
            await asyncio.sleep(0.02)
        raise AssertionError("no leader elected in time")

    async def wait_applied(self, expected: list[bytes], among=None, timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        ids = among or list(self.network.nodes)
        while asyncio.get_running_loop().time() < deadline:
            if all(self.applied[i] == expected for i in ids):
                return
            await asyncio.sleep(0.02)
        raise AssertionError({i: self.applied[i] for i in ids})


def run(coro):
    return asyncio.run(coro)


def cmd(i) -> bytes:
    return f"cmd-{i}".encode()


# ---------------------------------------------------------------- leader election


def test_a_single_leader_is_elected_and_followers_know_it(tmp_path):
    async def scenario():
        cluster = Cluster(tmp_path)
        cluster.start_all()
        leader = await cluster.wait_for_leader()
        await asyncio.sleep(0.3)  # several heartbeats later, still the same single leader
        assert cluster.leaders() == [leader]
        for node in cluster.network.nodes.values():
            assert node.leader_id == leader.node_id
            assert node.current_term == leader.current_term
        await cluster.crash_all()

    run(scenario())


def test_a_one_node_cluster_elects_itself_and_commits(tmp_path):
    async def scenario():
        cluster = Cluster(tmp_path, size=1)
        cluster.start_all()
        leader = await cluster.wait_for_leader()
        assert await leader.propose(cmd(1)) == 1
        await cluster.crash_all()

    run(scenario())


# ---------------------------------------------------------------- log replication


def test_commands_are_applied_on_every_node_in_the_same_order(tmp_path):
    async def scenario():
        cluster = Cluster(tmp_path)
        cluster.start_all()
        leader = await cluster.wait_for_leader()
        commands = [cmd(i) for i in range(30)]
        results = await asyncio.gather(*(leader.propose(c) for c in commands))
        assert sorted(results) == list(range(1, 31))  # each applied exactly once
        await cluster.wait_applied(cluster.applied[leader.node_id])
        assert sorted(cluster.applied[leader.node_id]) == sorted(commands)
        await cluster.crash_all()

    run(scenario())


def test_replicas_that_applied_the_same_entries_report_the_same_digest(tmp_path):
    async def scenario():
        cluster = Cluster(tmp_path)
        cluster.start_all()
        leader = await cluster.wait_for_leader()
        before = leader.applied_digest
        for i in range(10):
            await leader.propose(cmd(i))
        assert leader.applied_digest != before
        for _ in range(100):  # until every node has applied what the leader has
            nodes = list(cluster.network.nodes.values())
            if all(n.last_applied == leader.last_applied for n in nodes):
                break
            await asyncio.sleep(0.02)
        assert len({n.applied_digest for n in cluster.network.nodes.values()}) == 1
        await cluster.crash_all()

    run(scenario())


def test_a_follower_refuses_writes_and_names_the_leader(tmp_path):
    async def scenario():
        cluster = Cluster(tmp_path)
        cluster.start_all()
        leader = await cluster.wait_for_leader()
        follower = next(n for n in cluster.network.nodes.values() if n is not leader)
        with pytest.raises(NotLeader) as info:
            await follower.propose(cmd(1))
        assert info.value.leader_id == leader.node_id
        await cluster.crash_all()

    run(scenario())


# ---------------------------------------------------------------- failure detection & recovery


def test_leader_crash_elects_a_new_leader_and_loses_nothing(tmp_path):
    async def scenario():
        cluster = Cluster(tmp_path)
        cluster.start_all()
        old = await cluster.wait_for_leader()
        before = [cmd(i) for i in range(5)]
        for c in before:
            await old.propose(c)

        old_id, old_term = old.node_id, old.current_term
        await cluster.crash(old_id)
        survivors = [i for i in cluster.ids if i != old_id]
        new = await cluster.wait_for_leader(among=survivors)
        assert new.node_id != old_id and new.current_term > old_term

        after = [cmd(i) for i in range(5, 10)]
        for c in after:
            await new.propose(c)
        await cluster.wait_applied(before + after, among=survivors)

        cluster.start(old_id)  # comes back with its log on disk and catches up
        await cluster.wait_applied(before + after)
        assert cluster.node(old_id).role == Role.FOLLOWER
        await cluster.crash_all()

    run(scenario())


def test_a_minority_cannot_commit_but_a_majority_still_can(tmp_path):
    async def scenario():
        cluster = Cluster(tmp_path)
        cluster.start_all()
        leader = await cluster.wait_for_leader()
        await leader.propose(cmd(0))
        followers = [i for i in cluster.ids if i != leader.node_id]
        await cluster.crash(followers[0])
        await leader.propose(cmd(1))  # 2 of 3 is still a majority
        await cluster.crash(followers[1])
        with pytest.raises(ProposalTimeout):
            await leader.propose(cmd(2), timeout=0.5)  # 1 of 3 is not
        await cluster.crash_all()

    run(scenario())


def test_partitioned_leader_steps_down_and_its_uncommitted_entry_is_discarded(tmp_path):
    async def scenario():
        cluster = Cluster(tmp_path)
        cluster.start_all()
        old = await cluster.wait_for_leader()
        await old.propose(cmd("committed"))

        cluster.isolate(old.node_id)
        with pytest.raises(ProposalTimeout):
            await old.propose(cmd("lost"), timeout=0.4)  # can't reach a majority
        majority = [i for i in cluster.ids if i != old.node_id]
        new = await cluster.wait_for_leader(among=majority)
        await new.propose(cmd("after"))

        cluster.heal()
        expected = [cmd("committed"), cmd("after")]
        await cluster.wait_applied(expected)
        assert old.role == Role.FOLLOWER
        assert all(e.command != cmd("lost") for e in old.log)  # overwritten by the new leader's log
        await cluster.crash_all()

    run(scenario())


def test_full_cluster_restart_recovers_everything_from_disk(tmp_path):
    async def scenario():
        cluster = Cluster(tmp_path)
        cluster.start_all()
        leader = await cluster.wait_for_leader()
        commands = [cmd(i) for i in range(10)]
        for c in commands:
            await leader.propose(c)
        term_before = leader.current_term
        await cluster.wait_applied(commands)
        await cluster.crash_all()

        cluster.start_all()  # every node starts with an empty state machine
        leader = await cluster.wait_for_leader()
        assert leader.current_term > term_before
        await cluster.wait_applied(commands)  # rebuilt purely from the persisted logs
        await cluster.crash_all()

    run(scenario())


def test_one_leader_per_term_and_identical_logs_through_repeated_crashes(tmp_path):
    """Election safety and log matching, checked continuously while nodes are
    crashed and restarted in random order."""

    async def scenario():
        cluster = Cluster(tmp_path, size=5, seed=7)
        cluster.start_all()
        leaders_by_term: dict[int, set] = {}
        committed: list[bytes] = []

        async def watch():
            while True:
                for node in list(cluster.network.nodes.values()):
                    if node.role == Role.LEADER:
                        leaders_by_term.setdefault(node.current_term, set()).add(node.node_id)
                await asyncio.sleep(0.005)

        watcher = asyncio.create_task(watch())
        rng = random.Random(1)
        for round_no in range(6):
            leader = await cluster.wait_for_leader(timeout=8)
            for i in range(3):
                c = cmd(f"{round_no}.{i}")
                await leader.propose(c)
                committed.append(c)
            victims = rng.sample(cluster.ids, 2)  # at most 2 of 5 down: a majority survives
            for v in victims:
                await cluster.crash(v)
            await asyncio.sleep(0.4)
            for v in victims:
                cluster.start(v)

        await cluster.wait_for_leader(timeout=8)
        await cluster.wait_applied(committed, timeout=8)
        watcher.cancel()
        assert all(len(ids) == 1 for ids in leaders_by_term.values()), leaders_by_term
        await cluster.crash_all()

    run(scenario())


# ---------------------------------------------------------------- the RPC rules, one at a time


class NoTransport:
    async def request_vote(self, *_):
        raise Unreachable

    async def append_entries(self, *_):
        raise Unreachable


def make_node(tmp_path, log_terms=(), term=0):
    storage = RaftStorage(str(tmp_path / "solo.db"))
    storage.append([(i + 1, t, b"x") for i, t in enumerate(log_terms)])
    storage.save_term_and_vote(term, None)
    return RaftNode("me", ["a", "b"], storage, NoTransport(), lambda c: None)


def vote(term, candidate, last_index, last_term):
    return raft_pb2.RequestVoteRequest(
        term=term, candidate_id=candidate, last_log_index=last_index, last_log_term=last_term
    )


def test_votes_once_per_term(tmp_path):
    node = make_node(tmp_path, term=1)
    assert node.handle_request_vote(vote(2, "a", 0, 0)).vote_granted
    assert not node.handle_request_vote(vote(2, "b", 0, 0)).vote_granted
    assert node.handle_request_vote(vote(2, "a", 0, 0)).vote_granted  # a retry from the same candidate
    assert node.handle_request_vote(vote(3, "b", 0, 0)).vote_granted  # new term, new vote


def test_refuses_stale_terms_and_less_up_to_date_logs(tmp_path):
    node = make_node(tmp_path, log_terms=(1, 1, 2), term=2)
    assert not node.handle_request_vote(vote(1, "a", 9, 9)).vote_granted  # stale term
    assert not node.handle_request_vote(vote(3, "a", 5, 1)).vote_granted  # longer, but older last term
    assert not node.handle_request_vote(vote(3, "a", 2, 2)).vote_granted  # same last term, shorter
    assert node.handle_request_vote(vote(3, "a", 3, 2)).vote_granted


def test_vote_survives_a_restart(tmp_path):
    node = make_node(tmp_path, term=1)
    assert node.handle_request_vote(vote(2, "a", 0, 0)).vote_granted
    node.storage.close()
    restarted = RaftNode("me", ["a", "b"], RaftStorage(str(tmp_path / "solo.db")), NoTransport(), lambda c: None)
    assert restarted.current_term == 2 and restarted.voted_for == "a"
    assert not restarted.handle_request_vote(vote(2, "b", 0, 0)).vote_granted


def entries(*pairs):
    return [raft_pb2.LogEntry(index=i, term=t, command=b"y") for i, t in pairs]


def append(term, prev_index, prev_term, new_entries, commit=0):
    return raft_pb2.AppendEntriesRequest(
        term=term, leader_id="a", prev_log_index=prev_index, prev_log_term=prev_term,
        entries=new_entries, leader_commit=commit,
    )


def test_append_entries_consistency_check(tmp_path):
    node = make_node(tmp_path, log_terms=(1, 1), term=1)
    assert not node.handle_append_entries(append(0, 2, 1, [])).success  # stale leader
    reply = node.handle_append_entries(append(1, 5, 1, []))  # we don't have index 5
    assert not reply.success and reply.match_index == 2
    assert not node.handle_append_entries(append(1, 2, 3, [])).success  # index 2 has a different term
    assert node.handle_append_entries(append(1, 2, 1, entries((3, 1)))).success
    assert node.last_index == 3


def test_conflicting_suffix_is_replaced_and_duplicates_are_harmless(tmp_path):
    node = make_node(tmp_path, log_terms=(1, 1, 1), term=1)
    reply = node.handle_append_entries(append(2, 1, 1, entries((2, 2))))  # leader's index 2 is from term 2
    assert reply.success and [e.term for e in node.log] == [1, 2]  # old 2 and 3 are gone
    node.handle_append_entries(append(2, 1, 1, entries((2, 2), (3, 2))))
    node.handle_append_entries(append(2, 1, 1, entries((2, 2))))  # a late duplicate of the first RPC
    assert [e.term for e in node.log] == [1, 2, 2]  # did not shorten the log
    node.storage.close()
    assert [t for t, _ in RaftStorage(str(tmp_path / "solo.db")).load()[2]] == [1, 2, 2]  # and on disk too
