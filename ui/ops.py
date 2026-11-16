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


async def set_service(service: str, on: bool, current_state: str = "") -> tuple[int, str]:
    """Start or stop a container that is not a Raft member: the client node
    (the bidder personas) or the LLM node's gRPC server. One left paused by
    an interrupted chaos run is unpaused, not started."""
    if on and current_state == "paused":
        return await run(*COMPOSE, "unpause", service)
    return await run(*COMPOSE, "start" if on else "stop", service)


# ---------------------------------------------------------------- test suites

PY = sys.executable
OFFLINE_TESTS = [
    "tests/test_raft.py", "tests/test_state_machine.py", "tests/test_assistant.py",
    "tests/test_llm_service.py", "tests/test_pricing_trend_db.py", "tests/test_retrieval.py",
]


@dataclass
class Suite:
    key: str
    claim: str        # what we say on the slides
    title: str        # the check that backs it
    command: list[str]
    group: str        # "live": a quick check against the cluster; "before": a full suite, longer
    about: str        # roughly how long it takes
    live: bool = True  # needs the running cluster
    # filled in by a run
    state: str = "idle"  # idle, running, passed, failed
    progress: float = 0.0
    summary: str = ""
    last_line: str = ""
    seconds: float = 0.0
    finished_at: float = 0.0
    lines: list[str] = field(default_factory=list)
    version: int = 0

    @property
    def shown_command(self) -> str:
        parts = ["python" if part == PY else part for part in self.command]
        files = [part for part in parts if part.startswith("tests/")]
        if len(files) > 2:  # keep a long list of test files to one line
            parts = [part for part in parts if part not in files[1:]] + [f"(+ {len(files) - 1} more files)"]
        return " ".join(parts)


# Each entry is a claim and the script that checks it - the same scripts the
# README lists for the terminal; the dashboard only runs them and shows the verdict.
SUITES = [
    Suite("health", "All five nodes are up and honour the gRPC contract",
          "Health check: every RPC on each app server, one agreed leader, the LLM node answering",
          [PY, "scripts/smoke_test.py"], "live", "2 s"),
    Suite("consistency", "The three replicas hold exactly the same state",
          "Applied-log digests at the same index, then every auction, bid and outcome compared",
          [PY, "scripts/consistency_check.py"], "live", "1 s"),
    Suite("stress", "400 bids at the same instant: none lost, none overwritten",
          "400 simultaneous bids on one auction; the final bid must be the highest accepted, on every replica",
          [PY, "scripts/stress_bids.py"], "live", "3 s"),
    Suite("failover", "The leader can crash mid-auction and no committed bid is lost",
          "SIGKILL the leader: a new one is elected, the bids are intact, the old leader rejoins and catches up",
          [PY, "scripts/failover_check.py"], "live", "3 s"),
    Suite("chaos", "Under kills, freezes and partitions: one leader per term, every bid exactly once",
          "Two rounds of random faults while a bidder keeps bidding and retrying",
          [PY, "scripts/chaos.py", "--rounds", "2", "--seed", "3"], "live", "15 s"),
    Suite("unit", "The Raft implementation and the auction rules are correct",
          "116 tests: Raft in-process with crashes and partitions, the state machine replayed byte for byte, "
          "wallet and escrow rules, the assistant",
          [PY, "-m", "pytest", "-q", *OFFLINE_TESTS], "before", "20 s", live=False),
    Suite("churn", "Raft's safety properties survive random failures",
          "Five random seeds of crashes, restarts and partitions on a simulated 5-node cluster",
          [PY, "scripts/raft_churn.py", "--seeds", "5"], "before", "25 s", live=False),
    Suite("contract", "Every RPC behaves as the .proto says, on every node",
          "48 live tests: accounts, bidding rules, funds, escrow, post/get, leader redirects",
          [PY, "-m", "pytest", "-q", "tests/test_contract.py"], "before", "40-80 s"),
    Suite("tour", "The whole story works end to end, LLM included",
          "The terminal walkthrough: ask Tapri, an LLM-written listing, anti-sniping, a retried bid, the recap",
          [PY, "scripts/demo_queries.py"], "before", "1-2 min"),
]

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PYTEST_PROGRESS = re.compile(r"\[\s*(\d+)%\]")
_PYTEST_SUMMARY = re.compile(r"^=*\s*(\d+ passed.*|\d+ failed.*)$")


async def run_suite(suite: Suite) -> None:
    suite.state, suite.progress, suite.summary, suite.lines, suite.last_line = "running", 0.0, "", [], ""
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
        suite.last_line = line.strip()
        if (m := _PYTEST_PROGRESS.search(line)):
            suite.progress = int(m.group(1)) / 100
        elif line.lstrip().startswith(("PASS", "FAIL", "OK")) or (m := _PYTEST_SUMMARY.match(line.strip())):
            suite.summary = line.strip().strip("= ")
        suite.version += 1
    code = await proc.wait()
    suite.finished_at = time.time()
    suite.seconds = suite.finished_at - started
    suite.state = "passed" if code == 0 else "failed"
    suite.progress = 1.0
    if not suite.summary:
        suite.summary = "passed" if code == 0 else f"exit code {code}"
    suite.version += 1
