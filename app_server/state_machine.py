"""The replicated state machine: what a committed Raft log entry *does*.

Every change to auction state - registering a user, a session, a new auction,
a bid, closing and settling auctions, saving a recap - is a command in the
Raft log. Each node applies the committed commands in log order to its own
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


def apply(command: bytes) -> dict:
    cmd = json.loads(command)
    handler = _HANDLERS.get(cmd.get("op"))
    if handler is None:
        log.error("unknown command %r - ignored", cmd.get("op"))
        return {"ok": False, "error": "unknown command"}
    return handler(cmd)


def _login(cmd: dict) -> dict:
    """Start a session, registering the user first if this is their first login.

    The leader has already checked the password. One race is settled here:
    two first logins for the same new username can both reach the log before
    either is applied. The first registers the name; the second carries a
    different password hash and is refused rather than logged in.
    """
    user = database.get_user(cmd["username"])
    if user is None:
        if not cmd.get("password_hash"):
            return {"ok": False, "error": "Invalid username or password"}
        database.create_user(cmd["username"], cmd["user_id"], cmd["password_hash"], now_ms=cmd["now_ms"])
        log.info("registered new user %s", cmd["username"])
    elif cmd.get("password_hash") and cmd["password_hash"] != user["password_hash"]:
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
    accepted, message = database.create_bid(
        auction_id=cmd["auction_id"],
        bidder_id=cmd["bidder_id"],
        amount=cmd["amount"],
        now_ms=cmd["now_ms"],
        bid_id=cmd["bid_id"],
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
    """Mock escrow settlement. Nothing is charged: it records what a real
    payment step would hold."""
    winner_id = record["current_highest_bidder_id"] or None
    final_price = float(record["current_highest_bid"]) if winner_id else 0.0
    premium, escrow_status = assistant.compute_settlement(final_price, winner_id is not None, premium_percent)
    database.create_settlement(
        record["auction_id"], winner_id, record["seller_id"], final_price, premium, escrow_status, now_ms=now_ms
    )
    if winner_id:
        log.info(
            "settlement %s: %s escrowed from %s (%s + %s buyer's premium), released to %s on hand-over [mock]",
            record["auction_id"], assistant.money(final_price + premium), winner_id,
            assistant.money(final_price), assistant.money(premium), record["seller_id"],
        )
    else:
        log.info("settlement %s: no sale, nothing escrowed", record["auction_id"])


def _save_summary(cmd: dict) -> dict:
    """Store an auction's recap. The first one committed wins: if leadership
    changed while a recap was being written, the new leader may write a second
    one, and every node must keep the same."""
    if database.get_summary(cmd["auction_id"]) is None:
        database.save_summary(cmd["auction_id"], cmd["summary"], cmd["from_fallback"], now_ms=cmd["now_ms"])
    return {"ok": True}


_HANDLERS = {
    "LOGIN": _login,
    "LOGOUT": _logout,
    "CREATE_AUCTION": _create_auction,
    "PLACE_BID": _place_bid,
    "CLOSE_EXPIRED": _close_expired,
    "SAVE_SUMMARY": _save_summary,
}
