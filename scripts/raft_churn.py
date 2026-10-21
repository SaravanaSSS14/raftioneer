#!/usr/bin/env python3
"""Randomized Raft churn: crash, restart and partition nodes of an in-process
cluster over and over, then check Raft's safety properties. No Docker needed.

Each seed runs a 5-node cluster (the same in-process network as
tests/test_raft.py) for 8 rounds. Every round: commit three commands through
whoever leads, then either crash one or two nodes for a random while, or cut
one node off the network, then bring it back. Throughout, an observer records
which nodes claim leadership in every term. At the end of a seed:

  * election safety  - no term ever had two leaders
  * no lost commits  - every command the leader confirmed is applied
  * state machine safety - every node applied exactly the same commands, in
                           the same order

Usage (from the repository root, after ./scripts/generate_protos.sh):
    python scripts/raft_churn.py                 # 30 seeds, about 5-8 minutes
    python scripts/raft_churn.py --seeds 5       # quicker
    python scripts/raft_churn.py --first-seed 100 --seeds 10
"""
import argparse
import asyncio
import os
import random
import sys
import tempfile
import pathlib

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "generated"), os.path.join(ROOT, "tests"), os.path.join(ROOT, "app_server")]

import test_raft as T  # noqa: E402  - reuses the test suite's in-process network and cluster

logging_quiet = True

async def churn(seed: int, tmp: pathlib.Path, rounds: int) -> int:
    cluster = T.Cluster(tmp, size=5, seed=seed)
    cluster.start_all()
    leaders: dict[int, set] = {}
    committed: list[bytes] = []

    async def watch():
        while True:
            for node in list(cluster.network.nodes.values()):
                if node.role == T.Role.LEADER:
                    leaders.setdefault(node.current_term, set()).add(node.node_id)
            await asyncio.sleep(0.003)

    watcher = asyncio.create_task(watch())
    rng = random.Random(seed)
    for r in range(rounds):
        leader = await cluster.wait_for_leader(timeout=10)
        for i in range(3):
            command = T.cmd(f"{r}.{i}")
            try:
                await leader.propose(command)
                committed.append(command)
            except (T.NotLeader, T.ProposalTimeout):
                pass  # outcome unknown, so not counted as committed
        if rng.random() < 0.5:
            victims = rng.sample(cluster.ids, rng.randint(1, 2))  # at most 2 of 5: a majority survives
            for v in victims:
                await cluster.crash(v)
            await asyncio.sleep(rng.uniform(0.1, 0.6))
            for v in victims:
                cluster.start(v)
        else:
            v = rng.choice(cluster.ids)
            cluster.isolate(v)
            await asyncio.sleep(rng.uniform(0.1, 0.6))
            cluster.heal()

    await cluster.wait_for_leader(timeout=10)
    await asyncio.sleep(1.0)  # let every node catch up
    watcher.cancel()
    reference = cluster.applied[cluster.ids[0]]
    problems = []
    if any(len(ids) > 1 for ids in leaders.values()):
        problems.append(f"two leaders in one term: { {t: ids for t, ids in leaders.items() if len(ids) > 1} }")
    for node_id in cluster.ids:
        if cluster.applied[node_id] != reference:
            problems.append(f"{node_id} applied a different sequence")
    missing = [c for c in committed if c not in reference]
    if missing:
        problems.append(f"{len(missing)} committed command(s) lost")
    await cluster.crash_all()
    if problems:
        raise AssertionError(f"seed {seed}: " + "; ".join(problems))
    return max(leaders)

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=int, default=30)
    parser.add_argument("--first-seed", type=int, default=0)
    parser.add_argument("--rounds", type=int, default=8)
    args = parser.parse_args()

    import logging
    logging.getLogger("raft").setLevel(logging.ERROR)  # the per-node log lines would bury the result

    passed = 0
    for seed in range(args.first_seed, args.first_seed + args.seeds):
        with tempfile.TemporaryDirectory() as d:
            try:
                terms = asyncio.run(churn(seed, pathlib.Path(d), args.rounds))
            except AssertionError as exc:
                print(f"  FAIL  {exc}")
                continue
        passed += 1
        print(f"  PASS  seed {seed}: {terms} terms, one leader in each, identical state everywhere")
    print(f"\n{'PASS' if passed == args.seeds else 'FAIL'}: {passed}/{args.seeds} seeds - "
          "election safety, no lost commits, identical state")
    return 0 if passed == args.seeds else 1

if __name__ == "__main__":
    raise SystemExit(main())
