"""The replicated state machine: what a committed Raft log entry *does*.

Every change to auction state - registering a user, a session, a new auction,
a bid, a deposit, closing and settling auctions (escrow), a confirmed
hand-over, saving a recap - is a command in the Raft log. Each node applies the committed commands in log order to its own
SQLite database, so all nodes end up with identical data.

For that to work, apply() must be deterministic: the same command must have
the same effect on every node. So anything that would differ between nodes -
the current time, a random id, a salted password hash, an LLM-written text,
a configured percentage - is decided once by the leader and carried inside
the command. Nothing here
reads the clock, generates randomness or calls another service.

Commands are small JSON objects: {"op": "PLACE_BID", ...fields}.
"""
import json
import logging

import assistant
from store import database

log = logging.getLogger(__name__)


def encode(op: str, **fields) -> bytes:
    return json.dumps({"op": op, **fields}, sort_keys=True).encode()


def describe(command: bytes) -> dict:
    """{"op": ..., "label": ...} for one log entry - a short line for people
    watching the log (the demo UI), never used to change state."""
    if not command:
        return {"op": "NOOP", "label": "new leader's no-op"}
    try:
        cmd = json.loads(command)
    except ValueError:
        return {"op": "?", "label": "unreadable entry"}
    op = cmd.get("op", "?")

    def name(user_id):
        return (user_id or "").removeprefix("user_")

    labels = {
        "REGISTER": lambda: f"register {cmd.get('username')}",
        "LOGIN": lambda: f"login {cmd.get('username')}",
        "LOGOUT": lambda: "logout",
        "CREATE_AUCTION": lambda: f"list \"{cmd.get('title', '')[:22]}\"",
        "PLACE_BID": lambda: f"{name(cmd.get('bidder_id'))} bids {assistant.money(cmd.get('amount', 0))}",
        "CLOSE_EXPIRED": lambda: "close ended auctions",
        "SAVE_SUMMARY": lambda: f"recap {cmd.get('auction_id')}",
        "DEPOSIT": lambda: f"{name(cmd.get('user_id'))} deposits {assistant.money(cmd.get('amount', 0))}",
        "CONFIRM_RECEIPT": lambda: f"{name(cmd.get('user_id'))} releases escrow",
    }
    return {"op": op, "label": labels[op]() if op in labels else op.lower()}


def apply(command: bytes) -> dict:
    cmd = json.loads(command)
    handler = _HANDLERS.get(cmd.get("op"))
    if handler is None:
        log.error("unknown command %r - ignored", cmd.get("op"))
        return {"ok": False, "error": "unknown command"}
    return handler(cmd)


def _register(cmd: dict) -> dict:
    """Create an account and open its wallet with the welcome credit.

    The leader has already hashed the password and checked the name is free,
    but two registrations for the same new name can both reach the log before
    either is applied: the first one applied wins, the second is refused - in
    the same order, so with the same result, on every node.
    """
    if not database.create_user(cmd["username"], cmd["user_id"], cmd["password_hash"], now_ms=cmd["now_ms"]):
        return {"ok": False, "error": "Username already taken"}
    database.create_wallet(cmd["user_id"], cmd.get("welcome_credit", 0.0), cmd["now_ms"])
    log.info("registered new user %s (welcome credit %s)", cmd["username"], cmd.get("welcome_credit", 0.0))
    return {"ok": True}


def _login(cmd: dict) -> dict:
    """Start a session for an existing user (the leader has checked the
    password). Accounts are only ever created by REGISTER."""
    if database.get_user(cmd["username"]) is None:
        return {"ok": False, "error": "Invalid username or password"}
    database.create_session(cmd["session_token"], cmd["user_id"], cmd["username"], now_ms=cmd["now_ms"])
    return {"ok": True}


def _logout(cmd: dict) -> dict:
    database.delete_session(cmd["session_token"])
    return {"ok": True}


def _create_auction(cmd: dict) -> dict:
    created = database.create_item(
        item_id=cmd["auction_id"],
        title=cmd["title"],
        description=cmd["description"],
        seller_id=cmd["seller_id"],
        duration_seconds=cmd["duration_seconds"],
        reserve_price=cmd["reserve_price"],
        now_ms=cmd["now_ms"],
        # the auction's anti-sniping rule, fixed at listing time by the leader
        snipe_window_seconds=cmd.get("snipe_window_seconds", 0),
        max_extension_seconds=cmd.get("max_extension_seconds", 0),
    )
    if created:
        log.info("auction %s created: %s (by %s)", cmd["auction_id"], cmd["title"], cmd["seller_id"])
    return {"ok": created}


def _place_bid(cmd: dict) -> dict:
    """The bid rules (beats the highest bid, meets the reserve, auction still
    open, seller can't bid) are checked here, at apply time, against the
    replicated state - so bids are judged in log order, the same on every node.

    Exactly-once: if the bid carries a request id this bidder has used before,
    the original outcome is returned and nothing is applied again. A client
    that retries after a timeout or a change of leader can't know whether its
    first attempt was committed; this makes the retry safe either way. The
    outcome is stored for refused bids too, so a retry always gets the same
    answer as the original.
    """
    request_id = cmd.get("request_id") or ""
    if request_id:
        stored = database.get_request_result(cmd["bidder_id"], request_id)
        if stored is not None:
            log.info("bid request %s from %s was already applied - original outcome returned", request_id, cmd["bidder_id"])
            return {**json.loads(stored), "replayed": True}

    before = database.get_auction(cmd["auction_id"])
    # Funds are judged here too, against the replicated wallets: what the
    # bidder has, less what they have already promised to auctions they are
    # winning. The premium percentage travels in the command (set by the
    # leader), like every other value that could differ between nodes.
    premium_percent = cmd.get("premium_percent", 0.0)
    accepted, message = database.create_bid(
        auction_id=cmd["auction_id"],
        bidder_id=cmd["bidder_id"],
        amount=cmd["amount"],
        now_ms=cmd["now_ms"],
        bid_id=cmd["bid_id"],
        available_funds=database.available_funds(cmd["bidder_id"], premium_percent, cmd["auction_id"]),
        premium_percent=premium_percent,
    )
    result = {"ok": accepted, "message": message}
    if accepted:
        log.info("bid %s: %s bid %.2f on auction %s", message, cmd["bidder_id"], cmd["amount"], cmd["auction_id"])
        after = database.get_auction(cmd["auction_id"])
        if before and after["end_time_ms"] > before["end_time_ms"]:
            log.info(
                "anti-sniping: auction %s extended by %.1fs", cmd["auction_id"],
                (after["end_time_ms"] - before["end_time_ms"]) / 1000,
            )
    if request_id:
        database.save_request_result(cmd["bidder_id"], request_id, json.dumps(result))
    return result


def _close_expired(cmd: dict) -> dict:
    """Close every auction whose end time has passed (as of the leader's clock
    when it proposed this), then settle every closed auction not yet settled -
    which also covers one closed by a bid that arrived after its deadline."""
    for record in database.close_expired_auctions(cmd["now_ms"]):
        if record["current_highest_bidder_id"]:
            log.info(
                "auction %s closed - winner %s at %.2f (%s)", record["auction_id"],
                record["current_highest_bidder_id"], record["current_highest_bid"], record["title"],
            )
        else:
            log.info("auction %s closed - no bids (%s)", record["auction_id"], record["title"])

    settled = []
    for record in database.get_closed_unsettled():
        _settle(record, cmd["now_ms"], cmd["premium_percent"])
        settled.append(record["auction_id"])
    return {"ok": True, "settled": settled}


def _settle(record: dict, now_ms: int, premium_percent: float) -> None:
    """Settle a closed auction: the winner's price plus the buyer's premium
    moves from their wallet into escrow (HELD), where it stays until they
    confirm the hand-over (CONFIRM_RECEIPT). Mock money: wallet numbers in the
    replicated state, nothing real is charged."""
    winner_id = record["current_highest_bidder_id"] or None
    final_price = float(record["current_highest_bid"]) if winner_id else 0.0
    premium, escrow_status = assistant.compute_settlement(final_price, winner_id is not None, premium_percent)
    if not database.create_settlement(
        record["auction_id"], winner_id, record["seller_id"], final_price, premium, escrow_status, now_ms=now_ms
    ):
        return  # already settled
    if winner_id is None:
        log.info("settlement %s: no sale, nothing escrowed", record["auction_id"])
        return
    due = round(final_price + premium, 2)
    if database.hold_in_escrow(record["auction_id"], winner_id, due, now_ms):
        log.info(
            "settlement %s: %s moved from %s's wallet into escrow (%s + %s buyer's premium) [mock]",
            record["auction_id"], assistant.money(due), winner_id,
            assistant.money(final_price), assistant.money(premium),
        )
    else:
        # The funds check at bid time should make this impossible; if it ever
        # happens the sale is marked, never paid from money that isn't there.
        database.set_escrow_status(record["auction_id"], database.ESCROW_UNPAID)
        log.warning("settlement %s: %s could not cover %s - marked unpaid", record["auction_id"], winner_id, due)


def _deposit(cmd: dict) -> dict:
    """Mock top-up. Exactly-once like bids: a retry with the same request id
    gets the original answer and adds nothing."""
    request_id = cmd.get("request_id") or ""
    if request_id:
        stored = database.get_request_result(cmd["user_id"], request_id)
        if stored is not None:
            return {**json.loads(stored), "replayed": True}
    balance = database.deposit(cmd["user_id"], cmd["amount"], cmd["now_ms"])
    result = {"ok": True, "balance": balance} if balance is not None else {"ok": False, "error": "No wallet for this user"}
    if request_id:
        database.save_request_result(cmd["user_id"], request_id, json.dumps(result))
    return result


def _confirm_receipt(cmd: dict) -> dict:
    """The winner confirms they received the item: escrow is released and the
    seller is paid."""
    released, message = database.release_escrow(cmd["auction_id"], cmd["user_id"], cmd["now_ms"])
    if released:
        log.info("escrow for auction %s released by %s - seller paid [mock]", cmd["auction_id"], cmd["user_id"])
        return {"ok": True, "escrow_status": message}
    return {"ok": False, "error": message}


def _save_summary(cmd: dict) -> dict:
    """Store an auction's recap. The first one committed wins: if leadership
    changed while a recap was being written, the new leader may write a second
    one, and every node must keep the same."""
    if database.get_summary(cmd["auction_id"]) is None:
        database.save_summary(cmd["auction_id"], cmd["summary"], cmd["from_fallback"], now_ms=cmd["now_ms"])
    return {"ok": True}


_HANDLERS = {
    "REGISTER": _register,
    "LOGIN": _login,
    "LOGOUT": _logout,
    "CREATE_AUCTION": _create_auction,
    "PLACE_BID": _place_bid,
    "CLOSE_EXPIRED": _close_expired,
    "SAVE_SUMMARY": _save_summary,
    "DEPOSIT": _deposit,
    "CONFIRM_RECEIPT": _confirm_receipt,
}
