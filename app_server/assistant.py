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
        f"bid plus a {BUYERS_PREMIUM_PERCENT:g}% buyer's premium from their wallet, so a bid "
        "needs that much in available funds. The payment is held in escrow until the "
        "winner confirms receipt of the item; then the seller is paid. All money is mock "
        "money - nothing real is charged."
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


def wallet_facts(username: str, balance: float, committed: float, in_escrow: float,
                 premium_percent: float = BUYERS_PREMIUM_PERCENT) -> str:
    """The asker's own wallet, with the largest bid they can afford already
    worked out - the model is not trusted with that arithmetic."""
    available = max(balance - committed, 0.0)
    largest = available / (1 + premium_percent / 100.0)
    return "\n".join([
        f"The person asking: {username}",
        f"Their wallet balance: {money(balance)}",
        f"Promised to auctions they are winning (bids + premium): {money(committed)}",
        f"Available for a new bid: {money(available)}",
        f"Largest new bid they can afford, including the {premium_percent:g}% premium: {money(largest)}",
        f"Held in escrow for items they won: {money(in_escrow)}",
    ])


def person(user_id: Optional[str]) -> str:
    """user_nahush -> nahush (ids are "user_" + the username)."""
    return (user_id or "").removeprefix("user_")


def bid_history_facts(bids: list[dict], limit: int = 6) -> str:
    """The bids on one auction, in the order they were accepted (the newest
    `limit` of them) - so "who bid, and how did it go?" has an answer."""
    if not bids:
        return "Bid history: no bids were placed"
    shown = bids[-limit:]
    lines = [f"{b.get('bidder_name') or person(b.get('bidder_id'))} bid {money(float(b['amount']))}" for b in shown]
    which = f"all {len(bids)}" if len(shown) == len(bids) else f"the last {len(shown)} of {len(bids)}"
    return f"Bid history, oldest first ({which}): " + "; ".join(lines)


def outcome_facts(auction: dict, settlement: Optional[dict], asker: str = "") -> str:
    """How a closed auction ended, from its settlement. With `asker` given,
    it also says outright whether the person asking is the winner - a small
    model otherwise tends to make the asker the hero of every answer."""
    if auction["status"] == "ACTIVE":
        return ""
    if settlement is None:
        return "Result: closed, settlement still being recorded"
    if not settlement.get("winner_id"):
        return "Result: no sale - no bid met the rules, nothing was charged"
    status = str(settlement["escrow_status"]).replace(" (mock)", "")
    paid = "the seller has been paid" if status == "RELEASED" else "the seller is paid when the winner confirms receipt"
    winner = person(settlement["winner_id"])
    text = (
        f"Result: sold to {winner} for {money(float(settlement['final_price']))} "
        f"plus {money(float(settlement['buyers_premium']))} buyer's premium. Escrow: {status} - {paid}"
    )
    if asker:
        text += (f". The person asking ({asker}) is the winner" if asker == winner
                 else f". The winner is {winner}, not {asker} (the person asking)")
    return text


# Lots created by the project's own check scripts (stress, chaos, failover,
# contract tests, benchmark). They are real auctions, but a question like
# "what sold recently?" is about the campus items, so the floor summary
# leaves them out. Asking about one of them directly still works.
SCRIPT_LOT_PREFIXES = ("pytest ", "Stress-test item", "Chaos-run item", "Failover-check item", "Benchmark item")


def floor_facts(auctions: list[dict], settlements: dict[str, dict], limit: int = 6) -> str:
    """The whole floor in a few lines, for a question that is not about one
    auction: what is open now, and how the most recent lots ended.
    `settlements` maps auction id -> settlement row (closed lots only)."""
    lots = [a for a in auctions if not str(a["title"]).startswith(SCRIPT_LOT_PREFIXES)]
    open_lots = sorted((a for a in lots if a["status"] == "ACTIVE"), key=lambda a: a["end_time_ms"])
    closed = sorted((a for a in lots if a["status"] != "ACTIVE"), key=lambda a: a["end_time_ms"], reverse=True)
    lines = [f"Lots still open (running now, no winner yet): {len(open_lots)}"]
    for a in open_lots[:limit]:
        highest = float(a["current_highest_bid"] or 0.0)
        lines.append(f"- OPEN: {a['title']} - " + (f"highest bid so far {money(highest)}" if highest > 0 else "no bids yet"))
    lines.append(f"Lots that have closed: {len(closed)}" + (f" (the latest {limit} listed)" if len(closed) > limit else ""))
    for line in closed_lot_lines(closed[:limit], settlements):
        lines.append(f"- CLOSED: {line}")
    return "\n".join(lines)


def closed_lot_lines(closed: list[dict], settlements: dict[str, dict]) -> list[str]:
    lines = []
    for a in closed:
        s = settlements.get(a["auction_id"])
        if s is None:
            lines.append(f"{a['title']} - being settled")
        elif not s.get("winner_id"):
            lines.append(f"{a['title']} - no sale")
        else:
            lines.append(f"{a['title']} - won by {person(s['winner_id'])} for {money(float(s['final_price']))}")
    return lines


def floor_fine_print(auctions: list[dict], settlements: dict[str, dict], limit: int = 6) -> str:
    """The closed lots, restated under an answer about results - the same
    idea as fine_print(): the record is shown next to the model's sentence."""
    lots = [a for a in auctions if not str(a["title"]).startswith(SCRIPT_LOT_PREFIXES)]
    closed = sorted((a for a in lots if a["status"] != "ACTIVE"), key=lambda a: a["end_time_ms"], reverse=True)
    if not closed:
        return "Fine print, from the floor rather than the model: no lot has closed yet."
    return ("Fine print, from the floor rather than the model: " + "; ".join(closed_lot_lines(closed[:limit], settlements))
            + ".")


_RESULT_WORDS = ("sold", "won", "win", "winner", "closed", "result", "recent", "history", "went for", "bought")
_WALLET_WORDS = ("wallet", "afford", "balance", "fund", "money", "escrow", "can i bid", "how much can", "my ", "pay")


def about_results(question: str) -> bool:
    """Is this question about how lots ended? (Decides whether the floor's
    fine print goes under the answer.)"""
    q = question.lower()
    return any(word in q for word in _RESULT_WORDS)


def about_wallet(question: str) -> bool:
    """Is the asker's own wallet relevant? It is left out of the context
    otherwise: handed a wallet, the model works it into every answer."""
    q = question.lower()
    return any(word in q for word in _WALLET_WORDS)


def result_answer_ok(answer: str, settlement: Optional[dict], asker: str) -> bool:
    """A fact check for an answer about a closed lot, like recap_matches_facts
    for recaps: it must name the real winner, and must not bring the asker in
    as one when they are not. A no-sale must not be described as a win."""
    text = answer.lower()
    # Escrow: while it is held the seller has not been paid yet - a small model
    # tends to finish the story ("...and the seller was paid") anyway.
    if settlement and "HELD" in str(settlement.get("escrow_status", "")) and re.search(
            r"seller (was|has been|got|has already been) paid|paid the seller|escrow (was|has been) released", text):
        return False
    winner = person(settlement.get("winner_id")) if settlement else ""
    if not winner:
        return not re.search(r"\bwon\b(?! nothing)", text.replace("did not win", "").replace("no one won", "")
                             .replace("nobody won", ""))
    if winner.lower() not in text:
        return False
    return asker == winner or asker.lower() not in text


def wrongly_crowns_asker(answer: str, asker: str, asker_won_something: bool) -> bool:
    """True if a general answer says the asker won a lot when they won none."""
    if asker_won_something or not asker:
        return False
    return re.search(rf"\b{re.escape(asker.lower())}\b[^.]*\bwon\b", answer.lower()) is not None


def build_context(auction: Optional[dict], bid_count: int, now_ms: float, wallet: str = "",
                  extra: str = "") -> str:
    """Reference context for an LLM answer: the rules, plus the auction and
    the asker's wallet if given. `extra` carries more live facts - an
    auction's bid history and result, or the floor summary."""
    parts = [auction_facts(auction, bid_count, now_ms)] if auction is not None else []
    if extra:
        parts.append(extra)
    if wallet:
        parts.append(wallet)
    parts.append(platform_rules())
    return "\n\n".join(parts)


def fallback_answer(auction: Optional[dict], bid_count: int, now_ms: float, wallet: str = "",
                    extra: str = "") -> str:
    return (
        "Tapri is offline on a chai break (the LLM node isn't answering), so here are the plain "
        "facts - less charming, entirely accurate.\n"
        + build_context(auction, bid_count, now_ms, wallet, extra)
    )


def fallback_description(title: str) -> str:
    return (
        f"{title.strip()} - leaving a hostel room near you. The description writer is offline; "
        "the seller is presumably busy packing."
    )


def fine_print(auction: dict, bid_count: int, now_ms: float, outcome: str = "") -> str:
    """The facts that matter for a bid, read from the auction itself and
    appended to the model's answer - so even when the model gets one wrong
    (it once told a bidder to go "a little above 0" past a Rs 800 reserve),
    the correct figure is right there underneath."""
    if auction["status"] != "ACTIVE":
        ending = f" {outcome}." if outcome else ""
        return f"Fine print, from the auction rather than the model: this auction is {auction['status'].lower()}.{ending}"
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
