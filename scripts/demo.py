#!/usr/bin/env python3
"""The Milestone 2 demo, act by act: one command for a live presentation or
the recorded video.

Each act says what it is about to show, runs the script that shows it, and
waits for Enter before the next (--auto runs straight through). Every act is
one of the ordinary scripts, so anything here can also be run on its own.

  1. The cluster        who leads, and how far each log has got
  2. One write, three   seed once through the leader; every replica agrees
     copies
  3. The auction        the guided tour: assistant, anti-sniping, a retried bid
  4. Kill the leader    a crash mid-auction; a new leader; nothing lost
  5. Chaos              two rounds of faults, then every safety check
  6. Under load         400 simultaneous bids, every replica agrees

Tip: keep `python scripts/cluster.py --watch` open in a second terminal to
show elections happening live.

Usage (stack freshly started with `docker compose ... up`, run from the
repository root):
    python scripts/demo.py
    python scripts/demo.py --auto            # no pauses
    python scripts/demo.py --serious         # no narrator asides
    python scripts/demo.py --from 4          # start at act 4
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import narrate

ACTS = [
    ("The cluster",
     "Three app servers, one Raft cluster. One of them won an election when the stack started; "
     "the others follow it.",
     ["scripts/cluster.py"]),
    ("One write, three copies",
     "Six auctions are created once, through the leader - and every replica lists all six.",
     ["scripts/seed_demo_data.py"], ["scripts/board.py"]),
    ("The auction",
     "The whole auction flow through the leader: the assistant (with fine print from the replicated "
     "state), an LLM-written listing, a bidding war with anti-sniping, a retried bid that counts once, "
     "and the close.",
     ["scripts/demo_queries.py"]),
    ("Kill the leader",
     "The leader's container is killed mid-auction - a crash, not a clean stop. Watch who takes over, "
     "and check that every committed bid is still there.",
     ["scripts/failover_check.py"]),
    ("Chaos",
     "Two rounds of deliberate damage while a bidder keeps bidding, then the checks: one leader per "
     "term, identical replicas, every bid exactly once.",
     ["scripts/chaos.py", "--rounds", "2", "--seed", "3"]),
    ("Under load",
     "400 bids at once on one auction. The log puts them in one order, and every replica ends up "
     "agreeing on the winner.",
     ["scripts/stress_bids.py"]),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--auto", action="store_true", help="run every act without pausing")
    parser.add_argument("--serious", action="store_true", help="no narrator asides")
    parser.add_argument("--from", dest="start", type=int, default=1, help="act to start from (1-6)")
    args = parser.parse_args()

    env = dict(os.environ)
    if args.serious:
        env["RAFTIONEER_SERIOUS"] = "1"

    failed = []
    for number, (title, intro, *commands) in enumerate(ACTS, start=1):
        if number < args.start:
            continue
        narrate.heading(f"Act {number}: {title}")
        print(intro)
        if not args.auto:
            input("  [Enter] to run it ")
        for command in commands:
            print(flush=True)  # our text must reach the screen (or a recording) before the act's own
            result = subprocess.run([sys.executable, *command], env=env)
            if result.returncode != 0:
                failed.append(f"act {number} ({' '.join(command)})")
        if not args.auto and number < len(ACTS):
            input("\n  [Enter] for the next act ")

    narrate.verdict(not failed, "the Milestone 2 demo" + (" ran cleanly" if not failed else f" - {', '.join(failed)}"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
