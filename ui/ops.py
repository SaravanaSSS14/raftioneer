"""What the demo UI does outside gRPC: fault injection through Docker (the
same commands scripts/chaos.py uses) and running the test suites.

Neither is part of the auction system - they are the presenter's remote
control: kill or freeze a container to show Raft recovering, and run the
checks live to show the claims hold.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass, field

from backend import ROOT

COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy" / "docker-compose.yml")]


def service_of(node_id: str) -> str:
    return node_id.replace("-", "_")  # app-server-1 -> app_server_1


async def run(*args: str, timeout: float = 30) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=str(ROOT)
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode, out.decode(errors="replace")


# ---------------------------------------------------------------- containers

async def container_states() -> dict[str, str]:
    """compose service -> state (running, paused, exited, ...)."""
    code, out = await run(*COMPOSE, "ps", "-a", "--format", "{{.Service}}|{{.State}}", timeout=10)
    if code != 0:
        return {}
    return dict(line.split("|", 1) for line in out.splitlines() if "|" in line)


async def kill(node_id: str) -> tuple[int, str]:
    """SIGKILL - a crash, not a clean shutdown. The node's Raft log stays in
    its container, so `start` brings it back with its state."""
    return await run(*COMPOSE, "kill", service_of(node_id))


async def start(node_id: str) -> tuple[int, str]:
    return await run(*COMPOSE, "start", service_of(node_id))


async def freeze(node_id: str) -> tuple[int, str]:
    """docker pause: the process is alive but gets no CPU - a "zombie" that
    wakes up later still believing whatever it believed."""
    return await run(*COMPOSE, "pause", service_of(node_id))


async def unfreeze(node_id: str) -> tuple[int, str]:
    return await run(*COMPOSE, "unpause", service_of(node_id))


async def set_simulated_bidders(on: bool) -> tuple[int, str]:
    """Start or stop the client node (Node 5: the bidder personas)."""
    return await run(*COMPOSE, "start" if on else "stop", "client")


# ---------------------------------------------------------------- test suites

PY = sys.executable
OFFLINE_TESTS = [
    "tests/test_raft.py", "tests/test_state_machine.py", "tests/test_assistant.py",
    "tests/test_llm_service.py", "tests/test_pricing_trend_db.py", "tests/test_retrieval.py",
]


@dataclass
class Suite:
    key: str
    title: str
    what: str
    command: list[str]
    icon: str
    live: bool = True  # needs the running cluster
    # filled in by a run
    state: str = "idle"  # idle, running, passed, failed
    progress: float = 0.0
    summary: str = ""
    seconds: float = 0.0
    lines: list[str] = field(default_factory=list)
    version: int = 0

    @property
    def shown_command(self) -> str:
        return " ".join("python" if part == PY else part for part in self.command)


SUITES = [
    Suite("unit", "Raft and unit tests",
          "Raft in-process with crashes and partitions, the state machine replayed byte for byte, "
          "wallet and escrow rules, the assistant, handbook retrieval.",
          [PY, "-m", "pytest", "-q", *OFFLINE_TESTS], "science", live=False),
    Suite("churn", "Randomized Raft churn",
          "Five random seeds of crashes, restarts and partitions on a simulated 5-node cluster: "
          "at most one leader per term, no committed entry lost, identical logs.",
          [PY, "scripts/raft_churn.py", "--seeds", "5"], "shuffle", live=False),
    Suite("consistency", "Replicas are identical",
          "Every node's applied digest at the same index, and every auction, bid and outcome, compared.",
          [PY, "scripts/consistency_check.py"], "fact_check"),
    Suite("stress", "400 simultaneous bids",
          "400 bids at once on one auction: the log puts them in one order and every replica agrees "
          "on the winner.",
          [PY, "scripts/stress_bids.py"], "bolt"),
    Suite("failover", "Kill the leader",
          "SIGKILL the leader mid-auction: a new leader is elected, no committed bid is lost, sessions "
          "still work, and the old leader rejoins and catches up.",
          [PY, "scripts/failover_check.py"], "heart_broken"),
    Suite("chaos", "Chaos: two rounds of faults",
          "Kill, freeze, partition and lose-majority faults while a bidder keeps bidding: one leader per "
          "term, identical replicas, every bid exactly once.",
          [PY, "scripts/chaos.py", "--rounds", "2", "--seed", "3"], "storm"),
    Suite("contract", "Live contract tests",
          "Every RPC against the running cluster on every node: accounts, bidding rules, funds, escrow, "
          "post/get, leader redirects. Creates test auctions.",
          [PY, "-m", "pytest", "-q", "tests/test_contract.py"], "verified"),
]

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PYTEST_PROGRESS = re.compile(r"\[\s*(\d+)%\]")
_PYTEST_SUMMARY = re.compile(r"^=*\s*(\d+ passed.*|\d+ failed.*)$")


async def run_suite(suite: Suite) -> None:
    suite.state, suite.progress, suite.summary, suite.lines = "running", 0.0, "", []
    suite.version += 1
    started = time.time()
    env = dict(os.environ, NO_COLOR="1", PYTHONUNBUFFERED="1")
    proc = await asyncio.create_subprocess_exec(
        *suite.command, cwd=str(ROOT), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = _ANSI.sub("", raw.decode(errors="replace").rstrip())
        if not line:
            continue
        suite.lines.append(line)
        if (m := _PYTEST_PROGRESS.search(line)):
            suite.progress = int(m.group(1)) / 100
        elif line.lstrip().startswith(("PASS", "FAIL", "OK")) or (m := _PYTEST_SUMMARY.match(line.strip())):
            suite.summary = line.strip().strip("= ")
        suite.version += 1
    code = await proc.wait()
    suite.seconds = time.time() - started
    suite.state = "passed" if code == 0 else "failed"
    suite.progress = 1.0
    if not suite.summary:
        suite.summary = "passed" if code == 0 else f"exit code {code}"
    suite.version += 1
