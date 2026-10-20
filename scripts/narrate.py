"""Presentation helpers shared by the demo scripts: headings, PASS/FAIL lines,
and the narrator's asides.

Facts are always printed plainly. The asides - a dry one-liner after a key
moment - sit on top and never replace a fact. RAFTIONEER_SERIOUS=1 (or
--serious on scripts/demo.py) switches them off, for an audience that
prefers its consensus without commentary.

Asides rotate in a fixed order rather than at random, so a recorded demo
reads the same every time. Colour is used only on a terminal, and never
when NO_COLOR is set.
"""
import itertools
import os
import sys

SERIOUS = os.environ.get("RAFTIONEER_SERIOUS", "") not in ("", "0")
_COLOUR = sys.stdout.isatty() and "NO_COLOR" not in os.environ

ASIDES = {
    # the guided tour
    "reject-low": [
        "A bid below the standing one. Optimism noted; bid not.",
        "Lower than the current bid. The log has a long memory and no mercy.",
    ],
    "fine-print": [
        "Tapri is fluent, not infallible - hence the fine print, read from the auction itself.",
    ],
    "snipe": [
        "Last-second sniping: an ancient art, defeated here by simple arithmetic.",
        "A bid in the final seconds, so the clock makes room. Everyone gets a chance to answer.",
    ],
    "retry": [
        "The phone panicked and asked twice. The ledger, being a ledger, remembered the first time.",
    ],
    "recap-template": [
        "This recap is the template's - Tapri was offline or failed the fact check. Dull, but it can count.",
    ],
    # leader failover
    "kill": [
        "{node} has left the chat. Nobody held a funeral; they held an election.",
        "{node} is gone. The survivors stop waiting for heartbeats and start campaigning.",
    ],
    "elected": [
        "{node} took over in {seconds:.2f}s - quicker than any committee on campus.",
        "New leader, new term, same bids. That's rather the point.",
    ],
    "sessions": [
        "Nobody had to log in again. The sessions were in the log too.",
    ],
    "rejoin": [
        "{node} is back, demoted to follower, and taking it remarkably well.",
    ],
    "nothing-lost": [
        "Every committed bid survived. Unlike most group-project promises.",
    ],
    # chaos
    "kill-leader": [
        "The leader went down mid-sentence. The cluster finished the sentence.",
    ],
    "kill-follower": [
        "A follower vanished; the leader carried on with two of three and barely noticed.",
    ],
    "freeze-leader": [
        "The leader froze like a laptop during a viva. It woke up no longer in charge, and took it gracefully.",
    ],
    "lose-majority": [
        "One node, alone, still convinced it's in charge. It can't commit a thing - and Raft is right not to let it.",
    ],
    "partition": [
        "The leader lost the Wi-Fi - a classic hostel experience. The rest of the cluster moved on without it.",
    ],
    "chaos-pass": [
        "{rounds} rounds of sabotage, no split brain. The replicas agree on everything, "
        "which is more than three people ever have.",
    ],
    # the rest
    "stress": [
        "{bids} bids at once, and the log made them queue - more than the mess manages at lunch.",
    ],
    "consistent": [
        "Three replicas, one story. Rarer than you'd think - ask any three witnesses.",
    ],
    "board-agree": [
        "Every replica agrees on every price. Consensus, finally, achieved somewhere on campus.",
    ],
    "no-leader": [
        "No leader right now: everyone's a follower and nobody's following. Give it a second.",
    ],
    "seed": [
        "Six listings from two students clearing out their rooms. The mini-fridge has seen things.",
    ],
}

_cycles: dict[str, itertools.cycle] = {}


def _style(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


def heading(text: str) -> None:
    print(f"\n{_style('1', f'=== {text} ===')}")


def ok(text: str) -> None:
    print(f"  {_style('32;1', 'PASS')}  {text}")


def fail(text: str) -> None:
    print(f"  {_style('31;1', 'FAIL')}  {text}")


def check(passed: bool, text: str) -> bool:
    (ok if passed else fail)(text)
    return passed


def verdict(passed: bool, text: str) -> None:
    label = _style("32;1", "PASS") if passed else _style("31;1", "FAIL")
    print(f"\n{label}: {text}")


def role(name: str, width: int = 0) -> str:
    """A Raft role, padded to `width` and then coloured (so columns still line
    up): LEADER green, CANDIDATE yellow, DOWN red."""
    colour = {"LEADER": "32;1", "CANDIDATE": "33;1", "DOWN": "31;1"}.get(name)
    padded = f"{name:<{width}}"
    return _style(colour, padded) if colour else padded


def aside(key: str, **facts) -> None:
    """The narrator's comment on what just happened - unless we're being serious."""
    if SERIOUS:
        return
    cycle = _cycles.setdefault(key, itertools.cycle(ASIDES[key]))
    print(_style("2;3", f"    ~ {next(cycle).format(**facts)}"))
