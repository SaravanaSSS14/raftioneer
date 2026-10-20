"""Unit tests for app_server/assistant.py - pure text and arithmetic, so no
running stack is needed. Covers the mock settlement maths and the plain-text
fallbacks that keep every LLM-backed feature working when the LLM node is down.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app_server"))

import assistant  # noqa: E402

NOW_MS = 1_000_000_000.0


def _auction(**overrides):
    auction = {
        "title": "Yamaha F310 acoustic guitar",
        "description": "Strings changed last month.",
        "status": "ACTIVE",
        "current_highest_bid": 0.0,
        "reserve_price": 0.0,
        "end_time_ms": NOW_MS + 90_000,
    }
    auction.update(overrides)
    return auction


def test_settlement_with_a_winner_holds_escrow_and_charges_the_premium():
    premium, status = assistant.compute_settlement(200.0, has_winner=True)
    assert premium == round(200.0 * assistant.BUYERS_PREMIUM_PERCENT / 100.0, 2)
    assert status == assistant.ESCROW_HELD


def test_settlement_without_a_winner_is_a_no_sale():
    premium, status = assistant.compute_settlement(0.0, has_winner=False)
    assert premium == 0.0
    assert status == assistant.ESCROW_NO_SALE


def test_context_for_an_open_auction_includes_facts_and_rules():
    text = assistant.build_context(_auction(current_highest_bid=120.0, reserve_price=100.0), 3, NOW_MS)
    assert "Yamaha F310" in text
    assert "Rs 120.00" in text
    assert "Number of bids: 3" in text
    assert "Time remaining: 90 seconds" in text
    assert "buyer's premium" in text


def test_context_without_an_auction_is_just_the_rules():
    text = assistant.build_context(None, 0, NOW_MS)
    assert "buyer's premium" in text
    assert "Item:" not in text


def test_context_reports_no_bids_and_no_reserve_plainly():
    text = assistant.build_context(_auction(), 0, NOW_MS)
    assert "Current highest bid: none yet" in text
    assert "Reserve price: none" in text


def test_closed_auction_context_has_no_countdown():
    text = assistant.build_context(_auction(status="CLOSED"), 2, NOW_MS)
    assert "Time remaining" not in text
    assert "Next bid must be" not in text


def test_next_bid_before_any_bid_is_the_reserve():
    text = assistant.build_context(_auction(reserve_price=800.0), 0, NOW_MS)
    assert "Next bid must be: at least Rs 800.00 (the reserve price)" in text


def test_next_bid_after_a_bid_must_beat_it():
    text = assistant.build_context(_auction(current_highest_bid=805.0, reserve_price=800.0), 1, NOW_MS)
    assert "Next bid must be: more than Rs 805.00" in text


def test_next_bid_with_no_bids_and_no_reserve():
    assert assistant.next_bid_rule(0.0, 0.0) == "any amount above Rs 0.00"


def test_fallback_answer_is_never_empty_and_says_why():
    text = assistant.fallback_answer(_auction(), 0, NOW_MS)
    assert "offline" in text
    assert "Yamaha F310" in text


def test_fallback_summary_for_a_sale():
    text = assistant.fallback_summary("Table fan", [50.0, 60.0, 75.0], 75.0, "priya")
    assert "Rs 75.00" in text
    assert "3 bids" in text
    assert "opening at Rs 50.00" in text
    assert "Winner: priya" in text


def test_describe_trend_is_empty_with_no_prior_average():
    assert assistant.describe_trend(100.0, None) == ""
    assert assistant.describe_trend(100.0, 0.0) == ""


def test_describe_trend_above_average():
    text = assistant.describe_trend(150.0, 100.0)
    assert "50% above" in text
    assert "Rs 100.00 average sale price" in text


def test_describe_trend_below_average():
    text = assistant.describe_trend(60.0, 100.0)
    assert "40% below" in text


def test_describe_trend_close_to_average_reads_as_in_line():
    text = assistant.describe_trend(103.0, 100.0)
    assert "in line with" in text
    assert "%" not in text


def test_fallback_summary_includes_trend_when_average_given():
    text = assistant.fallback_summary("Table fan", [50.0, 60.0, 75.0], 150.0, "priya", average_sale_price=100.0)
    assert "50% above the Rs 100.00 average sale price so far" in text


def test_fallback_summary_omits_trend_without_an_average():
    text = assistant.fallback_summary("Table fan", [50.0, 60.0, 75.0], 75.0, "priya", average_sale_price=None)
    assert "average sale price" not in text


def test_fallback_summary_with_no_bids_never_mentions_trend():
    # No sale happened at all - a trend clause here would be nonsensical.
    text = assistant.fallback_summary("Table fan", [], 0.0, None, average_sale_price=100.0)
    assert text == "Table fan closed with no bids."


def test_fallback_summary_uses_singular_for_one_bid():
    assert "1 bid," in assistant.fallback_summary("Table fan", [50.0], 50.0, "priya")


def test_fallback_summary_with_no_bids():
    assert assistant.fallback_summary("Table fan", [], 0.0, None) == "Table fan closed with no bids."


def test_fallback_description_mentions_the_title():
    assert "Table fan" in assistant.fallback_description("  Table fan ")


def test_context_states_the_anti_sniping_rule_and_any_extension():
    original_end = NOW_MS + 60_000
    auction = _auction(
        end_time_ms=original_end + 12_000, original_end_ms=original_end,
        snipe_window_ms=10_000, max_end_ms=original_end + 60_000,
    )
    facts = assistant.auction_facts(auction, 3, NOW_MS)
    assert "a bid in the last 10 seconds" in facts
    assert "up to 60 seconds past the original end time" in facts
    assert "Clock extended so far: 12 seconds" in facts


def test_context_without_anti_sniping_does_not_mention_it():
    assert "Anti-sniping" not in assistant.auction_facts(_auction(), 0, NOW_MS)
