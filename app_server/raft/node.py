import asyncio
import enum
import hashlib
import logging
import random
import time
from typing import Callable, Optional

import raft_pb2

from .storage import RaftStorage

log = logging.getLogger("raft")

# At most this many entries per AppendEntries, so a follower catching up after
# a long outage gets a series of bounded RPCs rather than one enormous one.
MAX_ENTRIES_PER_RPC = 64

# Group commit: how many times the flusher yields to let in-flight proposals
# join a batch before writing it (each yield costs microseconds).
MAX_BATCH_WAIT_YIELDS = 50

# How often the election timer is checked. Small next to the election timeout.
TICK_SECONDS = 0.02


class Role(enum.Enum):
    FOLLOWER = "FOLLOWER"
    CANDIDATE = "CANDIDATE"
    LEADER = "LEADER"


class NotLeader(Exception):
    """This node is not the leader (or stopped being it before the request
    finished). leader_id is who it believes the leader is, or None."""

    def __init__(self, leader_id: Optional[str] = None):
        super().__init__(f"not the leader (current leader: {leader_id or 'unknown'})")
        self.leader_id = leader_id


class ProposalTimeout(Exception):
    """A command was not committed in time - typically because no majority is
    reachable. It sits in the leader's log and may still commit later."""


class RaftNode:
    """One Raft server, following Figure 2 of the paper.

    Concurrency model: everything runs on one asyncio event loop, so a handler
    runs uninterrupted until its next `await`. That means no locks - but any
    state checked before an `await` must be checked again after it, because
    the node may have changed term or role in between (see the "still the
    same term?" checks after every RPC below).

    The application supplies:
      transport  request_vote(peer_id, req) / append_entries(peer_id, req),
                 each returning the reply message or raising if the peer
                 can't be reached
      apply      apply(command: bytes) -> result. Called exactly once per
                 committed entry, in log order, on every node. It must be
                 deterministic, so every node ends up in the same state.
    """

    def __init__(
        self,
        node_id: str,
        peer_ids: list[str],
        storage: RaftStorage,
        transport,
        apply: Callable[[bytes], object],
        election_timeout: tuple[float, float] = (0.8, 1.6),
        heartbeat_interval: float = 0.2,
        rng: Optional[random.Random] = None,
        pre_vote: bool = True,
    ):
        self.node_id = node_id
        self.peer_ids = list(peer_ids)
        self.storage = storage
        self.transport = transport
        self.apply = apply
        self.election_timeout = election_timeout
        self.heartbeat_interval = heartbeat_interval
        self.rng = rng or random.Random()
        # Pre-vote: a node whose timer fires first checks that it could win
        # before it bumps its term. A voter refuses while it has heard from a
        # leader within `leader_lease` - several heartbeats, well inside the
        # shortest election timeout - so a live leader is never disrupted.
        self.pre_vote = pre_vote
        self.leader_lease = election_timeout[0] * 0.75

        # Persistent state on all servers - restored from disk.
        self.current_term, self.voted_for, stored = storage.load()
        self.log: list[raft_pb2.LogEntry] = [
            raft_pb2.LogEntry(term=term, index=i + 1, command=command) for i, (term, command) in enumerate(stored)
        ]

        # Volatile state on all servers. After a restart nothing is known to be
        # committed until a leader says so; the application rebuilds its state
        # by re-applying the log from index 1.
        self.role = Role.FOLLOWER
        self.leader_id: Optional[str] = None
        self.commit_index = 0
        self.last_applied = 0
        # SHA-256 chained over every applied entry (term + command). Nodes that
        # have applied the same entries in the same order have the same digest -
        # a cheap way for an outside checker to compare replicas.
        self.applied_digest = b""

        # Volatile state on leaders, reinitialised after every election win.
        self.next_index: dict[str, int] = {}
        self.match_index: dict[str, int] = {}
        self._term_start_index = 0  # index of the no-op this leader appended on election

        self._votes = 0
        self._pre_votes = 0
        self._pre_vote_round = 0
        self._last_leader_contact = float("-inf")  # when we last heard from a current leader
        self._deadline = 0.0
        self._waiters: dict[int, tuple[int, asyncio.Future]] = {}  # log index -> (term, future)
        # Group commit: proposals queue here until the flusher writes them to
        # disk together - one transaction, one fsync for the whole batch - and
        # only then add them to the log. So everything in self.log is always on
        # disk, exactly as with one write per entry, at a fraction of the cost.
        self._pending: list[tuple[bytes, asyncio.Future]] = []
        self._pending_ready = asyncio.Event()
        self.batches_flushed = 0
        self._wakeups: dict[str, asyncio.Event] = {}
        self._commit_advanced = asyncio.Event()
        self._leader_tasks: list[asyncio.Task] = []
        self._election_tasks: list[asyncio.Task] = []
        self._background: list[asyncio.Task] = []
        self._running = False

    # ------------------------------------------------------------------ helpers

    def _log(self, level: int, message: str, *args) -> None:
        """Every Raft log line carries node id, term and role."""
        log.log(level, f"[{self.node_id} term={self.current_term} {self.role.value}] {message}", *args)

    @property
    def last_index(self) -> int:
        return len(self.log)

    def term_at(self, index: int) -> int:
        """Term of the entry at `index`; 0 for index 0, the empty prefix."""
        return self.log[index - 1].term if index > 0 else 0

    @property
    def majority(self) -> int:
        cluster_size = len(self.peer_ids) + 1
        return cluster_size // 2 + 1

    @property
    def is_leader(self) -> bool:
        return self.role == Role.LEADER

    def _now(self) -> float:
        return time.monotonic()

    def _reset_election_timer(self) -> None:
        # Randomised per reset, so nodes rarely time out together (split votes).
        self._deadline = self._now() + self.rng.uniform(*self.election_timeout)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._running = True
        self._reset_election_timer()
        self._background = [
            asyncio.create_task(self._election_timer_loop()),
            asyncio.create_task(self._apply_loop()),
            asyncio.create_task(self._flush_loop()),
        ]
        self._log(logging.INFO, "started with %d log entries; peers %s", self.last_index, self.peer_ids)

    async def stop(self) -> None:
        """Stop all activity. Tests use this to simulate a crash."""
        self._running = False
        tasks = self._background + self._leader_tasks + self._election_tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._background, self._leader_tasks, self._election_tasks = [], [], []
        self._fail_waiters()

    async def _election_timer_loop(self) -> None:
        """Failure detection: a follower (or candidate) that hears from no
        leader for a whole election timeout assumes the leader is gone."""
        while self._running:
            await asyncio.sleep(TICK_SECONDS)
            if self.role != Role.LEADER and self._now() >= self._deadline:
                if self.pre_vote:
                    self._start_pre_vote()
                else:
                    self._start_election()

    # ------------------------------------------------------------------ elections

    def _become_follower(self, term: int) -> None:
        """Step down. Called on discovering a higher term, or (same term) on
        hearing from that term's leader while a candidate."""
        was_leader = self.role == Role.LEADER
        if term > self.current_term:
            self.current_term = term
            self.voted_for = None
            self.storage.save_term_and_vote(self.current_term, self.voted_for)
        if self.role != Role.FOLLOWER:
            self.role = Role.FOLLOWER
            self._log(logging.INFO, "stepped down")
        for task in self._leader_tasks + self._election_tasks:
            task.cancel()
        self._leader_tasks, self._election_tasks = [], []
        if was_leader:
            self.leader_id = None
            self._fail_waiters()

    def _start_pre_vote(self) -> None:
        """Before a real election: ask whether we could win one, without
        changing our term. Why it matters: a node that was frozen, cut off or
        just restarted would otherwise bump its term the moment its timer
        fired, and that higher term forces a healthy leader to step down.
        With pre-vote it asks first, the others - still hearing from their
        leader - say no, and nothing is disrupted. If the leader really is
        gone, the others have stopped hearing from it too, they say yes, and
        the election goes ahead one round trip later."""
        for task in self._election_tasks:
            task.cancel()
        self._reset_election_timer()  # if this round fails, try again after another timeout
        self.leader_id = None
        self._pre_vote_round += 1
        self._pre_votes = 1  # our own
        self._log(logging.INFO, "election timeout - asking for pre-votes for term %d", self.current_term + 1)
        if self._pre_votes >= self.majority:  # a one-node cluster
            self._start_election()
            return
        round_id, term = self._pre_vote_round, self.current_term
        self._election_tasks = [
            asyncio.create_task(self._request_pre_vote_from(peer, round_id, term)) for peer in self.peer_ids
        ]

    async def _request_pre_vote_from(self, peer: str, round_id: int, term: int) -> None:
        request = raft_pb2.RequestVoteRequest(
            term=term + 1,  # the term we would stand in; our own term is unchanged
            candidate_id=self.node_id,
            last_log_index=self.last_index,
            last_log_term=self.term_at(self.last_index),
            to_id=peer,
            pre_vote=True,
        )
        try:
            reply = await self.transport.request_vote(peer, request)
        except Exception:
            return  # unreachable: a pre-vote we don't get
        if round_id != self._pre_vote_round or self.current_term != term or self.role == Role.LEADER:
            return  # that round is over
        if reply.term > self.current_term:
            self._become_follower(reply.term)  # the cluster has moved on to a later term
            return
        if reply.vote_granted:
            self._pre_votes += 1
            if self._pre_votes >= self.majority:
                self._log(logging.INFO, "pre-vote won (%d of %d) - starting the election",
                          self._pre_votes, len(self.peer_ids) + 1)
                self._start_election()

    def _start_election(self) -> None:
        for task in self._election_tasks:  # a previous, failed election (or the pre-vote round)
            task.cancel()
        self.role = Role.CANDIDATE
        self.leader_id = None
        self.current_term += 1
        self.voted_for = self.node_id
        self.storage.save_term_and_vote(self.current_term, self.voted_for)
        self._reset_election_timer()
        self._votes = 1  # our own
        self._log(logging.INFO, "starting an election")

        if self._votes >= self.majority:  # a one-node cluster
            self._become_leader()
            return
        term = self.current_term
        self._election_tasks = [asyncio.create_task(self._request_vote_from(peer, term)) for peer in self.peer_ids]

    async def _request_vote_from(self, peer: str, term: int) -> None:
        request = raft_pb2.RequestVoteRequest(
            term=term,
            candidate_id=self.node_id,
            last_log_index=self.last_index,
            last_log_term=self.term_at(self.last_index),
            to_id=peer,
        )
        try:
            reply = await self.transport.request_vote(peer, request)
        except Exception:
            return  # unreachable: a vote we don't get
        if self.role != Role.CANDIDATE or self.current_term != term:
            return  # that election is already over
        if reply.term > self.current_term:
            self._become_follower(reply.term)
            return
        if reply.vote_granted:
            self._votes += 1
            if self._votes >= self.majority:
                self._become_leader()

    def _become_leader(self) -> None:
        for task in self._election_tasks:
            task.cancel()
        self._election_tasks = []
        self.role = Role.LEADER
        self.leader_id = self.node_id
        self._log(logging.INFO, "won the election with %d of %d votes", self._votes, len(self.peer_ids) + 1)

        self.next_index = {peer: self.last_index + 1 for peer in self.peer_ids}
        self.match_index = {peer: 0 for peer in self.peer_ids}
        # A leader may only commit an entry from its own term by counting
        # replicas (paper section 5.4.2). Appending a no-op now lets it commit
        # straight away - and that commits every older entry before it.
        self._term_start_index = self._append_to_own_log(b"")
        self._wakeups = {peer: asyncio.Event() for peer in self.peer_ids}
        self._leader_tasks = [
            asyncio.create_task(self._replicate_to(peer, self.current_term)) for peer in self.peer_ids
        ]
        self._advance_commit_index()  # a one-node cluster commits on its own

    # ------------------------------------------------------------------ RPC handlers

    def handle_request_vote(self, req: raft_pb2.RequestVoteRequest) -> raft_pb2.RequestVoteResponse:
        if req.pre_vote:
            return self._handle_pre_vote(req)
        if req.term < self.current_term:
            return self._vote_reply(req, False)
        if req.term > self.current_term:
            self._become_follower(req.term)

        # Election restriction (5.4.1): only vote for a candidate whose log is at
        # least as up to date as ours - a later last term, or the same last term
        # and at least as long. This is what keeps committed entries safe.
        candidate_log = (req.last_log_term, req.last_log_index)
        our_log = (self.term_at(self.last_index), self.last_index)
        granted = self.voted_for in (None, req.candidate_id) and candidate_log >= our_log
        if granted:
            self.voted_for = req.candidate_id
            self.storage.save_term_and_vote(self.current_term, self.voted_for)
            self._reset_election_timer()
        self._log(
            logging.INFO, "vote request from %s (last log term %d, index %d): %s",
            req.candidate_id, req.last_log_term, req.last_log_index, "granted" if granted else "refused",
        )
        return self._vote_reply(req, granted)

    def _handle_pre_vote(self, req: raft_pb2.RequestVoteRequest) -> raft_pb2.RequestVoteResponse:
        """Would we vote for this candidate in req.term? Answered without
        changing anything: no term change, no vote recorded, timer untouched.
        Yes only if that term is later than ours, the candidate's log is at
        least as up to date (the same election restriction as a real vote),
        and we have not heard from a leader recently."""
        candidate_log = (req.last_log_term, req.last_log_index)
        our_log = (self.term_at(self.last_index), self.last_index)
        leader_alive = self.role == Role.LEADER or self._now() - self._last_leader_contact < self.leader_lease
        granted = req.term > self.current_term and candidate_log >= our_log and not leader_alive
        self._log(
            logging.INFO, "pre-vote request from %s for term %d: %s", req.candidate_id, req.term,
            "granted" if granted else ("refused - the leader is alive" if leader_alive else "refused"),
        )
        return self._vote_reply(req, granted)

    def _vote_reply(self, req: raft_pb2.RequestVoteRequest, granted: bool) -> raft_pb2.RequestVoteResponse:
        return raft_pb2.RequestVoteResponse(
            term=self.current_term, vote_granted=granted, from_id=self.node_id, to_id=req.candidate_id
        )

    def _append_reply(self, req: raft_pb2.AppendEntriesRequest, success: bool, match_index: int):
        return raft_pb2.AppendEntriesResponse(
            term=self.current_term, success=success, match_index=match_index,
            from_id=self.node_id, to_id=req.leader_id,
        )

    def handle_append_entries(self, req: raft_pb2.AppendEntriesRequest) -> raft_pb2.AppendEntriesResponse:
        if req.term < self.current_term:  # from a deposed leader
            return self._append_reply(req, False, 0)
        # A legitimate leader for this term: follow it.
        if req.term > self.current_term or self.role != Role.FOLLOWER:
            self._become_follower(req.term)
        if self.leader_id != req.leader_id:
            self.leader_id = req.leader_id
            self._log(logging.INFO, "following leader %s", req.leader_id)
        self._last_leader_contact = self._now()
        self._reset_election_timer()

        # Consistency check: we must already hold the entry just before the new
        # ones, with the same term. If not, refuse; the leader backs up and
        # retries. match_index tells it where to retry from.
        if req.prev_log_index > self.last_index:
            return self._append_reply(req, False, self.last_index)
        if self.term_at(req.prev_log_index) != req.prev_log_term:
            return self._append_reply(req, False, req.prev_log_index - 1)

        # Append, first deleting any entry that conflicts with the leader's.
        # Entries we already hold are skipped rather than rewritten, so a late,
        # duplicated RPC can never cut a log back.
        new_entries = []
        for entry in req.entries:
            if entry.index <= self.last_index:
                if self.log[entry.index - 1].term == entry.term:
                    continue
                self._truncate_log_from(entry.index)
            new_entries.append(entry)
        if new_entries:
            self.log.extend(new_entries)
            self.storage.append([(e.index, e.term, e.command) for e in new_entries])

        last_new_index = req.prev_log_index + len(req.entries)
        if req.leader_commit > self.commit_index:
            self.commit_index = min(req.leader_commit, last_new_index)
            self._commit_advanced.set()
        return self._append_reply(req, True, last_new_index)

    def _truncate_log_from(self, index: int) -> None:
        # Raft guarantees a committed entry is never replaced. If this fires,
        # something is badly wrong - better to stop than diverge silently.
        assert index > self.commit_index, f"refusing to delete committed entry {index}"
        self._log(logging.WARNING, "deleting conflicting entries from index %d", index)
        del self.log[index - 1:]
        self.storage.truncate_from(index)

    # ------------------------------------------------------------------ leader: replication

    def _append_to_own_log(self, command: bytes) -> int:
        entry = raft_pb2.LogEntry(term=self.current_term, index=self.last_index + 1, command=command)
        self.log.append(entry)
        self.storage.append([(entry.index, entry.term, entry.command)])
        return entry.index

    async def _replicate_to(self, peer: str, term: int) -> None:
        """Keeps one follower in sync for as long as we lead this term: sends
        new entries as soon as they're proposed, and an empty AppendEntries (a
        heartbeat) whenever there's nothing new for a heartbeat interval."""
        wakeup = self._wakeups[peer]
        while self.role == Role.LEADER and self.current_term == term:
            if await self._send_append_entries(peer, term):
                continue  # follower still behind - keep going straight away
            try:
                await asyncio.wait_for(wakeup.wait(), self.heartbeat_interval)
            except asyncio.TimeoutError:
                pass
            wakeup.clear()

    async def _send_append_entries(self, peer: str, term: int) -> bool:
        """One AppendEntries to `peer`. Returns True if another should follow
        immediately (we made progress and it's still behind, or we must back up)."""
        next_index = self.next_index[peer]
        prev_index = next_index - 1
        request = raft_pb2.AppendEntriesRequest(
            term=term,
            leader_id=self.node_id,
            prev_log_index=prev_index,
            prev_log_term=self.term_at(prev_index),
            entries=self.log[prev_index: prev_index + MAX_ENTRIES_PER_RPC],
            leader_commit=self.commit_index,
            to_id=peer,
        )
        try:
            reply = await self.transport.append_entries(peer, request)
        except Exception:
            return False  # unreachable; retried on the next heartbeat
        if self.role != Role.LEADER or self.current_term != term:
            return False  # deposed while the RPC was in flight

        if reply.term > self.current_term:
            self._log(logging.INFO, "%s is in a later term (%d)", peer, reply.term)
            self._become_follower(reply.term)
            return False
        if reply.success:
            self.match_index[peer] = max(self.match_index[peer], reply.match_index)
            self.next_index[peer] = self.match_index[peer] + 1
            self._advance_commit_index()
            return self.next_index[peer] <= self.last_index
        # Logs disagree before next_index: back up. The follower's match_index
        # hint lets us skip straight to where its log could still match.
        self.next_index[peer] = max(1, min(next_index - 1, reply.match_index + 1))
        return True

    def _advance_commit_index(self) -> None:
        """An entry is committed once a majority holds it - counted only for an
        entry from the current term (5.4.2); earlier ones commit along with it."""
        for n in range(self.last_index, self.commit_index, -1):
            if self.term_at(n) != self.current_term:
                break
            replicas = 1 + sum(1 for peer in self.peer_ids if self.match_index.get(peer, 0) >= n)
            if replicas >= self.majority:
                self.commit_index = n
                self._commit_advanced.set()
                # Tell the followers now rather than at the next heartbeat, so
                # they apply it (and reads they serve see it) within one round trip.
                for wakeup in self._wakeups.values():
                    wakeup.set()
                break

    # ------------------------------------------------------------------ applying committed entries

    async def _apply_loop(self) -> None:
        """Feeds committed entries to the application in order. Runs as its own
        task so a long catch-up never holds up heartbeats or votes."""
        while self._running:
            await self._commit_advanced.wait()
            self._commit_advanced.clear()
            applied = 0
            while self.last_applied < self.commit_index:
                self.last_applied += 1
                self._apply_entry(self.log[self.last_applied - 1])
                applied += 1
                if applied % 100 == 0:
                    await asyncio.sleep(0)  # let RPCs in during a long replay

    def _apply_entry(self, entry: raft_pb2.LogEntry) -> None:
        self.applied_digest = hashlib.sha256(
            self.applied_digest + entry.term.to_bytes(8, "big") + entry.command
        ).digest()
        result: object = None
        if entry.command:  # empty = a leader's no-op
            try:
                result = self.apply(entry.command)
            except Exception as exc:  # apply must not raise - but never let it wedge the node
                self._log(logging.ERROR, "applying entry %d failed: %r", entry.index, exc)
                result = exc
        waiter = self._waiters.pop(entry.index, None)
        if waiter is None:
            return
        term, future = waiter
        if future.done():
            return
        if term == entry.term:
            future.set_result(result)
        else:  # our entry at this index was replaced by a later leader's
            future.set_exception(NotLeader(self.leader_id))

    def _fail_waiters(self) -> None:
        waiters, self._waiters = self._waiters, {}
        pending, self._pending = self._pending, []
        for future in [f for _, f in waiters.values()] + [f for _, f in pending]:
            if not future.done():
                future.set_exception(NotLeader(self.leader_id))

    def _forget(self, future: asyncio.Future) -> None:
        """Drop a proposal its caller has stopped waiting for. If it was still
        queued it is never written; if it was written, it may yet commit."""
        self._pending = [(c, f) for c, f in self._pending if f is not future]
        for index in [i for i, (_, f) in self._waiters.items() if f is future]:
            del self._waiters[index]

    async def _flush_loop(self) -> None:
        """Group commit, on the leader: write every queued proposal to disk in
        one transaction, then append the batch to the log and replicate it.
        Proposals that arrive while one batch is being written wait for the
        next, so under load many entries share each fsync."""
        while self._running:
            await self._pending_ready.wait()
            # Requests already in flight should join this batch: gRPC hands them
            # to the event loop one at a time, so keep yielding while proposals
            # are still arriving (bounded). A lone request yields once and goes.
            for _ in range(MAX_BATCH_WAIT_YIELDS):
                queued = len(self._pending)
                await asyncio.sleep(0)
                if len(self._pending) == queued:
                    break
            self._pending_ready.clear()
            batch, self._pending = self._pending, []
            if not batch:
                continue
            if self.role != Role.LEADER:  # deposed before the batch was written
                for _, future in batch:
                    if not future.done():
                        future.set_exception(NotLeader(self.leader_id))
                continue
            first = self.last_index + 1
            entries = [
                raft_pb2.LogEntry(term=self.current_term, index=first + i, command=command)
                for i, (command, _) in enumerate(batch)
            ]
            self.storage.append([(e.index, e.term, e.command) for e in entries])  # on disk first
            self.log.extend(entries)
            self.batches_flushed += 1
            for entry, (_, future) in zip(entries, batch):
                if not future.done():
                    self._waiters[entry.index] = (entry.term, future)
            for wakeup in self._wakeups.values():
                wakeup.set()
            self._advance_commit_index()  # a one-node cluster needs no replies

    # ------------------------------------------------------------------ the application's API

    async def wait_until_ready(self, timeout: float) -> None:
        """Wait until this leader has applied everything committed before its
        term (signalled by its own no-op being applied). Until then, its local
        state can be missing writes from the previous leader, so it must not
        validate requests against it. Raises NotLeader if we aren't the leader."""
        deadline = self._now() + timeout
        while True:
            if self.role != Role.LEADER:
                raise NotLeader(self.leader_id)
            if self.last_applied >= self._term_start_index:
                return
            if self._now() >= deadline:
                raise ProposalTimeout("leader has not caught up with its own log yet")
            await asyncio.sleep(0.01)

    async def propose(self, command: bytes, timeout: float = 2.0):
        """Replicate `command`; once it is committed and applied here, return
        what apply() returned for it.

        Raises NotLeader if this node isn't the leader, or loses leadership
        before the entry commits (it may still commit under the next leader).
        Raises ProposalTimeout if no majority acknowledges it in time.
        """
        if self.role != Role.LEADER:
            raise NotLeader(self.leader_id)
        future = asyncio.get_running_loop().create_future()
        # If the caller has gone (e.g. its RPC was cancelled), still consume the
        # outcome so asyncio doesn't warn about an unretrieved exception.
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        self._pending.append((command, future))  # written by the next group commit
        self._pending_ready.set()
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            self._forget(future)
            raise ProposalTimeout(f"not committed within {timeout}s") from None

    def tail(self, count: int) -> list[raft_pb2.LogEntry]:
        """The last `count` entries of this node's log, oldest first."""
        return self.log[-count:] if count > 0 else []

    def status(self) -> dict:
        return {
            "node_id": self.node_id,
            "role": self.role.value,
            "term": self.current_term,
            "leader_id": self.leader_id or "",
            "commit_index": self.commit_index,
            "last_applied": self.last_applied,
            "last_log_index": self.last_index,
            "applied_digest": self.applied_digest.hex(),
        }
