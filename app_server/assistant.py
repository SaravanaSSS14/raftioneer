"""Text and arithmetic for the assistant and the post-auction settlement.

Deliberately free of I/O and gRPC so it can be unit tested without a
running stack. Three jobs:

* build the context injected into every LLM call (the LLM node holds no
  auction state of its own - it only sees what we hand it per call),
* the plain-text fallbacks used when the LLM node is unreachable, so the
  AI layer being slow or down never breaks a feature, let alone a bid, and
* keep the model honest: the fine print appended to its answers and the
  fact check on its recaps, both computed from the auction itself.

Tapri's deterministic lines have a dry streak - facts first, personality
second, and never a fact that isn't true.
"""
import os
import re
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


def next_bid_rule(highest: float, reserve: float) -> str:
    """What a new bid has to be, stated outright - small models otherwise
    work it out for themselves and, for example, call a reserve the
    "current price" before anyone has bid."""
    if highest > 0:
        return f"more than {money(highest)}"
    if reserve > 0:
        return f"at least {money(reserve)} (the reserve price)"
    return "any amount above Rs 0.00"


def auction_facts(auction: dict, bid_count: int, now_ms: float) -> str:
    """One line per fact about a single auction, in plain text."""
    lines = [
        f"Item: {auction['title']}",
        f"Description: {auction['description'] or '(none given)'}",
        f"Status: {auction['status']}",
    ]
    highest = float(auction["current_highest_bid"] or 0.0)
    lines.append(f"Current highest bid: {money(highest)}" if highest > 0 else "Current highest bid: none yet - nobody has bid")
    lines.append(f"Number of bids: {bid_count}")
    reserve = float(auction.get("reserve_price") or 0.0)
    lines.append(f"Reserve price: {money(reserve)}" if reserve > 0 else "Reserve price: none")
    if auction["status"] == "ACTIVE":
        lines.append(f"Next bid must be: {next_bid_rule(highest, reserve)}")
        seconds_left = max(0, int((auction["end_time_ms"] - now_ms) / 1000))
        lines.append(f"Time remaining: {seconds_left} seconds")
        window = int(auction.get("snipe_window_ms") or 0) // 1000
        if window and auction.get("original_end_ms"):
            cap = int((auction["max_end_ms"] - auction["original_end_ms"]) / 1000)
            lines.append(
                f"Anti-sniping: a bid in the last {window} seconds moves the end time to {window} seconds "
                f"after that bid, up to {cap} seconds past the original end time"
            )
            extended = int((auction["end_time_ms"] - auction["original_end_ms"]) / 1000)
            if extended > 0:
                lines.append(f"Clock extended so far: {extended} seconds")
    return "\n".join(lines)


def build_context(auction: Optional[dict], bid_count: int, now_ms: float) -> str:
    """Reference context for an LLM answer: the rules, plus the auction if given."""
    if auction is None:
        return platform_rules()
    return f"{auction_facts(auction, bid_count, now_ms)}\n\n{platform_rules()}"


def fallback_answer(auction: Optional[dict], bid_count: int, now_ms: float) -> str:
    return (
        "Tapri is offline on a chai break (the LLM node isn't answering), so here are the plain "
        "facts - less charming, entirely accurate.\n"
        + build_context(auction, bid_count, now_ms)
    )


def fallback_description(title: str) -> str:
    return (
        f"{title.strip()} - leaving a hostel room near you. The description writer is offline; "
        "the seller is presumably busy packing."
    )


def fine_print(auction: dict, bid_count: int, now_ms: float) -> str:
    """The facts that matter for a bid, read from the auction itself and
    appended to the model's answer - so even when the model gets one wrong
    (it once told a bidder to go "a little above 0" past a Rs 800 reserve),
    the correct figure is right there underneath."""
    if auction["status"] != "ACTIVE":
        return f"Fine print, from the auction rather than the model: this auction is {auction['status'].lower()}."
    highest = float(auction["current_highest_bid"] or 0.0)
    reserve = float(auction.get("reserve_price") or 0.0)
    seconds_left = max(0, int((auction["end_time_ms"] - now_ms) / 1000))
    bids = "1 bid" if bid_count == 1 else f"{bid_count} bids"
    return (
        "Fine print, from the auction rather than the model: next bid must be "
        f"{next_bid_rule(highest, reserve)}; {bids} so far; closes in {seconds_left} seconds."
    )


def compute_settlement(final_price: float, has_winner: bool,
                       premium_percent: float = BUYERS_PREMIUM_PERCENT) -> tuple[float, str]:
    """(buyer's premium, escrow status) for a closed auction. Mock: nothing is charged.

    Under Raft the percentage comes from the replicated command, so every node
    settles with the leader's figure even if their own configuration differs."""
    if not has_winner:
        return 0.0, ESCROW_NO_SALE
    return round(final_price * premium_percent / 100.0, 2), ESCROW_HELD


def describe_trend(final_price: float, average_sale_price: Optional[float]) -> str:
    """One short clause comparing a sale to the running average, or "" if
    there's no prior average to compare against yet. Deterministic - this
    is what the fallback path uses instead of asking the LLM to judge it.
    """
    if average_sale_price is None or average_sale_price <= 0:
        return ""
    diff_pct = (final_price - average_sale_price) / average_sale_price * 100
    if abs(diff_pct) < 5:
        return f" That's in line with the {money(average_sale_price)} average sale price so far."
    direction = "above" if diff_pct > 0 else "below"
    return f" That's {abs(diff_pct):.0f}% {direction} the {money(average_sale_price)} average sale price so far."


def fallback_summary(
    title: str,
    bid_amounts: list[float],
    final_price: float,
    winner_name: Optional[str],
    average_sale_price: Optional[float] = None,
) -> str:
    if not bid_amounts:
        return f"{title} closed with no bids. It stays in the hostel, unloved and, more to the point, unsold."
    n = len(bid_amounts)
    bids = "1 bid" if n == 1 else f"{n} bids"
    winner = f" Winner: {winner_name}." if winner_name else ""
    trend = describe_trend(final_price, average_sale_price)
    return f"{title} sold for {money(final_price)} after {bids}, opening at {money(bid_amounts[0])}.{winner}{trend}"


_COUNT_WORDS = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
                "nineteen", "twenty"]


def recap_matches_facts(text: str, bid_count: int, final_price: float) -> bool:
    """Does an LLM-written recap state the right number of bids and the right
    final price, and no other bid count? The model writes, the code checks:
    a recap that fails is replaced by the template (which can't miscount)."""
    plain = text.lower().replace(",", "")
    counts = set()
    for match in re.finditer(r"\b(\d+|[a-z]+)\s+(?:recorded\s+|accepted\s+|total\s+)?bids?\b", plain):
        word = match.group(1)
        if word.isdigit():
            counts.add(int(word))
        elif word in _COUNT_WORDS:
            counts.add(_COUNT_WORDS.index(word))
        elif word == "zero":
            counts.add(0)
        elif word == "single":
            counts.add(1)
    if counts != {bid_count}:
        return False
    if bid_count == 0:
        return True
    forms = [re.escape(f"{final_price:.2f}")]
    if final_price == int(final_price):
        forms.append(rf"{int(final_price)}(?!\.\d)")
    return any(re.search(rf"(?<![\d.]){form}(?!\d)", plain) for form in forms)
