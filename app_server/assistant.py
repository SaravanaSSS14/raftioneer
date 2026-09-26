"""Text and arithmetic for the assistant and the post-auction settlement.

Deliberately free of I/O and gRPC so it can be unit tested without a
running stack. Two jobs:

* build the context injected into every LLM call (the LLM node holds no
  auction state of its own - it only sees what we hand it per call), and
* the plain-text fallbacks used when the LLM node is unreachable, so the
  AI layer being slow or down never breaks a feature, let alone a bid.
"""
import os
from typing import Optional

# Mock commission added on top of the winning bid at settlement.
BUYERS_PREMIUM_PERCENT = float(os.environ.get("BUYERS_PREMIUM_PERCENT", "5"))

ESCROW_HELD = "HELD (mock)"
ESCROW_NO_SALE = "NO_SALE"


def money(amount: float) -> str:
    return f"Rs {amount:,.2f}"


def platform_rules() -> str:
    return (
        "How this auction works: a bid must be higher than the current highest bid, "
        "and at least the reserve price if the seller set one. An auction closes "
        "automatically at its end time and the highest bid wins. The winner pays their "
        f"bid plus a {BUYERS_PREMIUM_PERCENT:g}% buyer's premium; payment is a mock "
        "escrow hold, so no real money moves."
    )


def auction_facts(auction: dict, bid_count: int, now_ms: float) -> str:
    """One line per fact about a single auction, in plain text."""
    lines = [
        f"Item: {auction['title']}",
        f"Description: {auction['description'] or '(none given)'}",
        f"Status: {auction['status']}",
    ]
    highest = float(auction["current_highest_bid"] or 0.0)
    lines.append(f"Current highest bid: {money(highest)}" if highest > 0 else "Current highest bid: none yet")
    lines.append(f"Number of bids: {bid_count}")
    reserve = float(auction.get("reserve_price") or 0.0)
    lines.append(f"Reserve price: {money(reserve)}" if reserve > 0 else "Reserve price: none")
    if auction["status"] == "ACTIVE":
        seconds_left = max(0, int((auction["end_time_ms"] - now_ms) / 1000))
        lines.append(f"Time remaining: {seconds_left} seconds")
    return "\n".join(lines)


def build_context(auction: Optional[dict], bid_count: int, now_ms: float) -> str:
    """Reference context for an LLM answer: the rules, plus the auction if given."""
    if auction is None:
        return platform_rules()
    return f"{auction_facts(auction, bid_count, now_ms)}\n\n{platform_rules()}"


def fallback_answer(auction: Optional[dict], bid_count: int, now_ms: float) -> str:
    return (
        "The assistant is offline right now, so here is what I can tell you directly.\n"
        + build_context(auction, bid_count, now_ms)
    )


def fallback_description(title: str) -> str:
    return f"{title.strip()} - listed by a student for the campus clearance auction."


def compute_settlement(final_price: float, has_winner: bool) -> tuple[float, str]:
    """(buyer's premium, escrow status) for a closed auction. Mock: nothing is charged."""
    if not has_winner:
        return 0.0, ESCROW_NO_SALE
    return round(final_price * BUYERS_PREMIUM_PERCENT / 100.0, 2), ESCROW_HELD


def fallback_summary(title: str, bid_amounts: list[float], final_price: float, winner_name: Optional[str]) -> str:
    if not bid_amounts:
        return f"{title} closed with no bids."
    n = len(bid_amounts)
    bids = "1 bid" if n == 1 else f"{n} bids"
    winner = f" Winner: {winner_name}." if winner_name else ""
    return f"{title} sold for {money(final_price)} after {bids}, opening at {money(bid_amounts[0])}.{winner}"
