"""Tests for the replicated state machine (app_server/state_machine.py), no
Docker needed. The key property: applying the same commands in the same order
gives the same database, byte for byte - which is what lets every Raft node
end up in the same state.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app_server"))

import state_machine  # noqa: E402
from store import database  # noqa: E402

T0 = 1_800_000_000_000  # a fixed "now" in ms; every command carries its own time


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point the store at an empty database file for the test."""

    def use(name="node.db"):
        monkeypatch.setattr(database, "DB_PATH", str(tmp_path / name))
        database.reset_database()

    use()
    return use


def apply(op, **fields):
    return state_machine.apply(state_machine.encode(op, **fields))


CREDIT = 1000.0  # the welcome credit in these tests
PREMIUM = 5.0     # buyer's premium, percent


def register(username, password_hash="hash-1", credit=CREDIT, now=T0):
    return apply(
        "REGISTER", username=username, user_id=f"user_{username}", password_hash=password_hash,
        welcome_credit=credit, now_ms=now,
    )


def login(username, token=None, now=T0):
    """Register the user if they're new, then sign in - most tests just need
    a user with a session and a wallet."""
    if database.get_user(username) is None:
        register(username, now=now)
    return apply(
        "LOGIN", username=username, user_id=f"user_{username}",
        session_token=token or f"tok-{username}", now_ms=now,
    )


def create(auction_id, seller="seller", duration=60, reserve=0.0, now=T0, snipe=0, cap=0):
    return apply(
        "CREATE_AUCTION", auction_id=auction_id, title=f"item {auction_id}", description="d",
        seller_id=f"user_{seller}", duration_seconds=duration, reserve_price=reserve, now_ms=now,
        snipe_window_seconds=snipe, max_extension_seconds=cap,
    )


def bid(auction_id, bidder, amount, bid_id, now=T0 + 1000, request_id=""):
    return apply(
        "PLACE_BID", auction_id=auction_id, bidder_id=f"user_{bidder}", amount=amount, bid_id=bid_id, now_ms=now,
        request_id=request_id, premium_percent=PREMIUM,
    )


SCRIPT = [
    lambda: login("seller"),
    lambda: login("asha"),
    lambda: login("ravi"),
    lambda: create("a1", reserve=50.0),
    lambda: create("a2", duration=5),
    lambda: bid("a1", "asha", 40.0, "b1"),  # below reserve
    lambda: bid("a1", "asha", 60.0, "b2"),
    lambda: bid("a1", "ravi", 55.0, "b3"),  # not higher
    lambda: bid("a1", "ravi", 70.0, "b4"),
    lambda: bid("a1", "seller", 90.0, "b5"),  # own auction
    lambda: bid("a2", "ravi", 10.0, "b6"),
    lambda: apply("CLOSE_EXPIRED", now_ms=T0 + 6000, premium_percent=5.0),  # a2 has ended
    lambda: apply("SAVE_SUMMARY", auction_id="a2", summary="sold", from_fallback=True, now_ms=T0 + 7000),
    lambda: apply("LOGOUT", session_token="tok-asha", now_ms=T0 + 8000),
    lambda: create("a3", duration=60, snipe=10, cap=30),
    lambda: bid("a3", "ravi", 20.0, "b7", now=T0 + 55_000, request_id="r1"),  # extends the clock
    lambda: bid("a3", "ravi", 20.0, "b8", now=T0 + 56_000, request_id="r1"),  # a retry: replayed
    lambda: apply("CONFIRM_RECEIPT", auction_id="a2", user_id="user_ravi", now_ms=T0 + 9000),  # escrow released
    lambda: apply("DEPOSIT", user_id="user_asha", amount=250.0, request_id="d1", now_ms=T0 + 9500),
    lambda: bid("a1", "asha", 1300.0, "b9", now=T0 + 9600),  # more than asha has
]


def dump() -> list[str]:
    conn = database.get_connection()
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def test_replaying_the_same_log_gives_identical_databases(fresh_db):
    results_1 = [step() for step in SCRIPT]
    state_1 = dump()
    fresh_db("other-node.db")
    results_2 = [step() for step in SCRIPT]
    assert results_2 == results_1
    assert dump() == state_1


def test_bid_rules_are_enforced_at_apply_time(fresh_db):
    results = [step() for step in SCRIPT[:11]]
    verdicts = [r["ok"] for r in results[5:11]]
    assert verdicts == [False, True, False, True, False, True]
    auction = database.get_auction("a1")
    assert auction["current_highest_bid"] == 70.0 and auction["current_highest_bidder_id"] == "user_ravi"


def test_close_expired_closes_and_settles_only_ended_auctions(fresh_db):
    for step in SCRIPT[:12]:
        step()
    assert database.get_auction("a1")["status"] == "ACTIVE"
    assert database.get_auction("a2")["status"] == "CLOSED"
    settlement = database.get_settlement("a2")
    assert settlement["winner_id"] == "user_ravi" and settlement["final_price"] == 10.0
    assert [r["auction_id"] for r in database.get_unsummarized()] == ["a2"]


def test_the_first_recap_committed_wins(fresh_db):
    for step in SCRIPT[:12]:
        step()
    apply("SAVE_SUMMARY", auction_id="a2", summary="first", from_fallback=False, now_ms=T0 + 7000)
    apply("SAVE_SUMMARY", auction_id="a2", summary="second", from_fallback=True, now_ms=T0 + 8000)
    assert database.get_summary("a2")["summary"] == "first"
    assert database.get_unsummarized() == []


def test_a_late_bid_closes_the_auction_instead_of_counting(fresh_db):
    login("seller"), login("asha")
    create("a1", duration=5)
    assert not bid("a1", "asha", 10.0, "b1", now=T0 + 6000)["ok"]
    assert database.get_auction("a1")["status"] == "CLOSED"


def test_racing_registrations_for_one_username_register_it_once(fresh_db):
    assert register("newbie", password_hash="hash-A")["ok"]
    assert register("newbie", password_hash="hash-B") == {"ok": False, "error": "Username already taken"}
    assert database.get_user("newbie")["password_hash"] == "hash-A"
    assert database.get_wallet_balance("user_newbie") == CREDIT  # credited once, not twice
    assert login("newbie", token="t1")["ok"]


def test_login_never_creates_an_account(fresh_db):
    result = apply("LOGIN", username="ghost", user_id="user_ghost", session_token="t", now_ms=T0)
    assert result == {"ok": False, "error": "Invalid username or password"}
    assert database.get_user("ghost") is None
    assert database.validate_session("t") is None


def test_unknown_commands_are_ignored(fresh_db):
    assert apply("DROP_TABLES")["ok"] is False


def test_impossible_bid_amounts_are_refused(fresh_db):
    login("seller"), login("asha")
    create("a1")
    for i, amount in enumerate([float("inf"), float("nan"), -5.0, 0.0]):
        assert not bid("a1", "asha", amount, f"bad{i}")["ok"], amount
    assert database.get_auction("a1")["current_highest_bid"] == 0.0


def test_settlement_uses_the_premium_in_the_command_not_local_config(fresh_db):
    login("seller"), login("asha")
    create("a1", duration=5)
    bid("a1", "asha", 200.0, "b1")
    apply("CLOSE_EXPIRED", now_ms=T0 + 6000, premium_percent=10.0)
    assert database.get_settlement("a1")["buyers_premium"] == 20.0


# ---- anti-sniping ----


def end_of(auction_id):
    return database.get_auction(auction_id)["end_time_ms"]


def test_a_bid_outside_the_closing_window_leaves_the_clock_alone(fresh_db):
    login("seller"), login("asha")
    create("a1", duration=60, snipe=10, cap=60)
    bid("a1", "asha", 10.0, "b1", now=T0 + 30_000)  # 30 s left
    assert end_of("a1") == T0 + 60_000


def test_a_bid_in_the_closing_window_extends_the_auction(fresh_db):
    login("seller"), login("asha")
    create("a1", duration=60, snipe=10, cap=60)
    bid("a1", "asha", 10.0, "b1", now=T0 + 55_000)  # 5 s left
    assert end_of("a1") == T0 + 65_000  # one full window after the bid
    assert database.get_auction("a1")["original_end_ms"] == T0 + 60_000


def test_extensions_stop_at_the_cap(fresh_db):
    login("seller"), login("asha"), login("ravi")
    create("a1", duration=60, snipe=10, cap=15)  # never past T0 + 75 s
    bid("a1", "asha", 10.0, "b1", now=T0 + 59_000)
    assert end_of("a1") == T0 + 69_000
    bid("a1", "ravi", 11.0, "b2", now=T0 + 68_000)
    assert end_of("a1") == T0 + 75_000
    bid("a1", "asha", 12.0, "b3", now=T0 + 74_500)
    assert end_of("a1") == T0 + 75_000
    assert not bid("a1", "ravi", 13.0, "b4", now=T0 + 75_000)["ok"]  # the cap is a hard end


def test_without_a_window_the_clock_never_moves(fresh_db):
    login("seller"), login("asha")
    create("a1", duration=60)
    bid("a1", "asha", 10.0, "b1", now=T0 + 59_900)
    assert end_of("a1") == T0 + 60_000


# ---- exactly-once bids ----


def bids_on(auction_id):
    return [(b["bidder_id"], b["amount"]) for b in database.get_bids_for_auction(auction_id)]


def test_a_retried_request_is_applied_once_and_gets_the_original_answer(fresh_db):
    login("seller"), login("asha"), login("ravi")
    create("a1")
    first = bid("a1", "asha", 10.0, "b1", request_id="req-1")
    assert first == {"ok": True, "message": "b1"}
    bid("a1", "ravi", 15.0, "b2")  # someone outbids in the meantime
    retry = bid("a1", "asha", 10.0, "b3", request_id="req-1")  # asha's client retries
    # Without deduplication the retry would be refused as "not higher than 15" -
    # telling asha her successful bid had failed.
    assert retry == {"ok": True, "message": "b1", "replayed": True}
    assert bids_on("a1") == [("user_asha", 10.0), ("user_ravi", 15.0)]


def test_a_refused_request_is_refused_again_on_retry(fresh_db):
    login("seller"), login("asha")
    create("a1", reserve=50.0)
    assert not bid("a1", "asha", 10.0, "b1", request_id="req-1")["ok"]
    retry = bid("a1", "asha", 10.0, "b2", request_id="req-1")
    assert retry["ok"] is False and retry["replayed"] is True


def test_request_ids_are_scoped_per_bidder(fresh_db):
    login("seller"), login("asha"), login("ravi")
    create("a1")
    assert bid("a1", "asha", 10.0, "b1", request_id="same")["ok"]
    second = bid("a1", "ravi", 15.0, "b2", request_id="same")
    assert second["ok"] and "replayed" not in second


def test_without_a_request_id_nothing_is_deduplicated(fresh_db):
    login("seller"), login("asha")
    create("a1")
    assert bid("a1", "asha", 10.0, "b1")["ok"]
    assert not bid("a1", "asha", 10.0, "b2")["ok"]  # judged afresh: not higher than 10



# ---- mock payments: wallets, the funds check, escrow ----


def balance(user):
    return database.get_wallet_balance(f"user_{user}")


def test_a_new_account_opens_with_the_welcome_credit(fresh_db):
    register("asha")
    assert balance("asha") == CREDIT
    [txn] = database.list_transactions("user_asha")
    assert (txn["kind"], txn["amount"], txn["balance_after"]) == ("WELCOME_CREDIT", CREDIT, CREDIT)


def test_a_bid_needs_funds_for_the_price_plus_the_premium(fresh_db):
    login("seller"), login("asha")
    create("a1")
    refused = bid("a1", "asha", 960.0, "b1")  # 960 + 5% = 1008 > 1000
    assert not refused["ok"] and refused["message"].startswith("Insufficient funds")
    assert bid("a1", "asha", 950.0, "b2")["ok"]  # 950 + 5% = 997.50


def test_money_promised_to_a_winning_bid_cannot_be_bid_again(fresh_db):
    login("seller"), login("asha")
    create("a1"), create("a2")
    assert bid("a1", "asha", 600.0, "b1")["ok"]  # promises 630
    assert not bid("a2", "asha", 400.0, "b2")["ok"]  # needs 420, only 370 left
    assert bid("a2", "asha", 350.0, "b3")["ok"]  # 367.50 fits
    # Raising your own winning bid replaces that promise rather than adding to it.
    assert not bid("a1", "asha", 650.0, "b4")["ok"]  # 682.50 > 1000 - 367.50
    assert bid("a1", "asha", 600.5, "b5")["ok"]  # 630.53 fits


def test_being_outbid_frees_the_money(fresh_db):
    login("seller"), login("asha"), login("ravi")
    create("a1"), create("a2")
    assert bid("a1", "asha", 900.0, "b1")["ok"]
    assert not bid("a2", "asha", 100.0, "b2")["ok"]
    assert bid("a1", "ravi", 910.0, "b3")["ok"]  # asha is no longer winning a1
    assert bid("a2", "asha", 100.0, "b4")["ok"]


def test_settlement_moves_price_and_premium_into_escrow(fresh_db):
    login("seller"), login("asha")
    create("a1", duration=5)
    bid("a1", "asha", 200.0, "b1")
    apply("CLOSE_EXPIRED", now_ms=T0 + 6000, premium_percent=PREMIUM)
    assert database.get_settlement("a1")["escrow_status"] == database.ESCROW_HELD
    assert balance("asha") == CREDIT - 210.0
    assert database.in_escrow("user_asha") == 210.0
    assert database.list_transactions("user_asha")[0]["kind"] == "ESCROW_HOLD"


def test_confirming_receipt_releases_escrow_to_the_seller(fresh_db):
    login("seller"), login("asha"), login("ravi")
    create("a1", duration=5)
    bid("a1", "asha", 200.0, "b1")
    apply("CLOSE_EXPIRED", now_ms=T0 + 6000, premium_percent=PREMIUM)

    not_winner = apply("CONFIRM_RECEIPT", auction_id="a1", user_id="user_ravi", now_ms=T0 + 7000)
    assert not_winner == {"ok": False, "error": "Only the winner can confirm receipt"}

    assert apply("CONFIRM_RECEIPT", auction_id="a1", user_id="user_asha", now_ms=T0 + 7000)["ok"]
    assert database.get_settlement("a1")["escrow_status"] == database.ESCROW_RELEASED
    assert balance("seller") == CREDIT + 200.0  # the price; the premium stays with the house
    assert database.in_escrow("user_asha") == 0.0

    again = apply("CONFIRM_RECEIPT", auction_id="a1", user_id="user_asha", now_ms=T0 + 8000)
    assert not again["ok"] and balance("seller") == CREDIT + 200.0  # paid once


def test_a_retried_deposit_counts_once(fresh_db):
    login("asha")
    first = apply("DEPOSIT", user_id="user_asha", amount=500.0, request_id="d1", now_ms=T0)
    retry = apply("DEPOSIT", user_id="user_asha", amount=500.0, request_id="d1", now_ms=T0 + 10)
    assert first == {"ok": True, "balance": 1500.0}
    assert retry == {"ok": True, "balance": 1500.0, "replayed": True}
    assert balance("asha") == 1500.0


def test_a_deposit_for_an_unknown_user_changes_nothing(fresh_db):
    assert apply("DEPOSIT", user_id="user_nobody", amount=5.0, now_ms=T0)["ok"] is False


def test_every_command_has_a_readable_label(fresh_db):
    assert state_machine.describe(b"") == {"op": "NOOP", "label": "new leader's no-op"}
    command = state_machine.encode("PLACE_BID", bidder_id="user_asha", amount=120.0)
    assert state_machine.describe(command) == {"op": "PLACE_BID", "label": "asha bids Rs 120.00"}
