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
    assert text.startswith("Table fan closed with no bids.") and "average" not in text


def test_fallback_summary_uses_singular_for_one_bid():
    assert "1 bid," in assistant.fallback_summary("Table fan", [50.0], 50.0, "priya")


def test_fallback_summary_with_no_bids():
    assert assistant.fallback_summary("Table fan", [], 0.0, None).startswith("Table fan closed with no bids.")


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


# ---- keeping the model honest ----


def test_fine_print_states_the_next_valid_bid_from_the_auction():
    text = assistant.fine_print(_auction(reserve_price=800.0), 0, NOW_MS)
    assert "next bid must be at least Rs 800.00 (the reserve price)" in text
    assert "0 bids so far" in text and "closes in 90 seconds" in text


def test_fine_print_for_a_closed_auction_says_so():
    assert "closed" in assistant.fine_print(_auction(status="CLOSED"), 3, NOW_MS)


# The recap the model actually wrote on Oct 20, for 4 bids ending at 170.00:
MISCOUNTED = ("This Raftioneer auction concluded with the study lamp with an adjustable arm selling for "
              "170.00 rupees. There were three bids recorded: 120.00, 150.00, and 155.00, leading to the "
              "final price of 170.00 rupees.")


def test_recap_fact_check_catches_the_miscount_from_the_tour():
    assert not assistant.recap_matches_facts(MISCOUNTED, 4, 170.0)


def test_recap_fact_check_accepts_a_correct_recap_in_digits_or_words():
    assert assistant.recap_matches_facts("Four bids came in and the lamp sold for Rs 170.", 4, 170.0)
    assert assistant.recap_matches_facts("After 4 bids it sold at 170.00 rupees.", 4, 170.0)
    assert assistant.recap_matches_facts("A single bid of Rs 1,553.03 took it.", 1, 1553.03)


def test_recap_fact_check_rejects_a_wrong_price_or_a_missing_count():
    assert not assistant.recap_matches_facts("After 4 bids it sold at Rs 175.", 4, 170.0)
    assert not assistant.recap_matches_facts("It sold for Rs 1700 after 4 bids.", 4, 170.0)
    assert not assistant.recap_matches_facts("The lamp sold for Rs 170 after a lively contest.", 4, 170.0)


def test_recap_fact_check_with_no_bids():
    assert assistant.recap_matches_facts("The fan closed with no bids.", 0, 0.0)
    # the model's own wording on Oct 20, wrongly rejected by the first version of the check
    assert assistant.recap_matches_facts("This auction saw zero bids, resulting in a final price of 0.00.", 0, 0.0)
    assert not assistant.recap_matches_facts("The fan sold after 2 bids.", 0, 0.0)



def test_wallet_facts_state_the_largest_affordable_bid():
    text = assistant.wallet_facts("asha", balance=5000.0, committed=4725.0, in_escrow=0.0, premium_percent=5.0)
    assert "Available for a new bid: Rs 275.00" in text
    assert "Largest new bid they can afford, including the 5% premium: Rs 261.90" in text


def test_the_askers_wallet_reaches_the_context():
    wallet = assistant.wallet_facts("asha", 1000.0, 0.0, 0.0)
    text = assistant.build_context(_auction(), 0, NOW_MS, wallet)
    assert "The person asking: asha" in text and "Item:" in text


# ---- what Tapri is told about history: bids, results, the floor ----

def test_bid_history_lists_bids_in_order_and_says_how_many():
    bids = [{"bidder_name": f"b{i}", "bidder_id": f"user_b{i}", "amount": 100.0 + i} for i in range(8)]
    text = assistant.bid_history_facts(bids, limit=3)
    assert "the last 3 of 8" in text
    assert text.index("b5 bid") < text.index("b7 bid")
    assert "b4 bid" not in text
    assert assistant.bid_history_facts([]) == "Bid history: no bids were placed"


def test_outcome_of_a_sold_lot_names_the_winner_price_and_escrow():
    settlement = {"winner_id": "user_nahush", "final_price": 250.0, "buyers_premium": 12.5,
                  "escrow_status": assistant.ESCROW_HELD}
    text = assistant.outcome_facts(_auction(status="CLOSED"), settlement)
    assert "sold to nahush for Rs 250.00" in text
    assert "Rs 12.50" in text and "HELD" in text
    released = assistant.outcome_facts(_auction(status="CLOSED"), dict(settlement, escrow_status="RELEASED"))
    assert "the seller has been paid" in released


def test_outcome_covers_no_sale_and_open_lots():
    no_sale = {"winner_id": None, "final_price": 0.0, "buyers_premium": 0.0, "escrow_status": assistant.ESCROW_NO_SALE}
    assert "no sale" in assistant.outcome_facts(_auction(status="CLOSED"), no_sale)
    assert assistant.outcome_facts(_auction(), None) == ""


def test_floor_facts_cover_open_and_closed_lots_and_skip_script_lots():
    auctions = [
        dict(_auction(current_highest_bid=120.0), auction_id="a1", title="Table fan"),
        dict(_auction(status="CLOSED"), auction_id="a2", title="Fest hoodie"),
        dict(_auction(status="CLOSED"), auction_id="a3", title="Stress-test item"),
    ]
    settlements = {"a2": {"winner_id": "user_karan", "final_price": 300.0}}
    text = assistant.floor_facts(auctions, settlements)
    assert "Lots still open (running now, no winner yet): 1" in text
    assert "OPEN: Table fan - highest bid so far Rs 120.00" in text
    assert "Lots that have closed: 1" in text and "CLOSED: Fest hoodie - won by karan for Rs 300.00" in text
    assert "Stress-test" not in text


def test_fine_print_of_a_closed_lot_states_the_result():
    text = assistant.fine_print(_auction(status="CLOSED"), 2, NOW_MS, "Result: sold to karan for Rs 300.00")
    assert "closed" in text and "sold to karan" in text


def test_extra_facts_go_into_the_context():
    text = assistant.build_context(None, 0, NOW_MS, extra="Closed lots so far: 2")
    assert "Closed lots so far: 2" in text and "buyer's premium" in text


def test_outcome_says_whether_the_asker_is_the_winner():
    settlement = {"winner_id": "user_nahush", "final_price": 250.0, "buyers_premium": 12.5,
                  "escrow_status": assistant.ESCROW_HELD}
    assert "The winner is nahush, not parm" in assistant.outcome_facts(_auction(status="CLOSED"), settlement, "parm")
    assert "(nahush) is the winner" in assistant.outcome_facts(_auction(status="CLOSED"), settlement, "nahush")


def test_result_check_wants_the_real_winner_and_not_the_asker():
    settlement = {"winner_id": "user_mrudul", "final_price": 1.85}
    assert assistant.result_answer_ok("Mrudul won it for Rs 1.85.", settlement, "parm")
    assert not assistant.result_answer_ok("Parm won the auction for Rs 1.85.", settlement, "parm")
    assert not assistant.result_answer_ok("Mrudul beat Parm to it.", settlement, "parm")
    assert assistant.result_answer_ok("Parm won it for Rs 1.85.", {"winner_id": "user_parm"}, "parm")


def test_result_check_catches_a_seller_paid_while_escrow_is_held():
    held = {"winner_id": "user_nisargh", "final_price": 253.16, "escrow_status": assistant.ESCROW_HELD}
    assert not assistant.result_answer_ok("Nisargh won it, and the seller was paid.", held, "parm")
    assert assistant.result_answer_ok("Nisargh won it; the seller is paid when nisargh confirms receipt.", held, "parm")
    released = dict(held, escrow_status="RELEASED")
    assert assistant.result_answer_ok("Nisargh won it, and the seller has been paid.", released, "parm")


def test_result_check_for_a_no_sale():
    no_sale = {"winner_id": None}
    assert assistant.result_answer_ok("No sale - nobody won, no bid met the reserve.", no_sale, "parm")
    assert not assistant.result_answer_ok("Karan won it for Rs 9,000.", no_sale, "parm")


def test_general_answer_must_not_crown_the_asker():
    assert assistant.wrongly_crowns_asker("Parm won these lots.", "parm", asker_won_something=False)
    assert not assistant.wrongly_crowns_asker("Parm won these lots.", "parm", asker_won_something=True)
    assert not assistant.wrongly_crowns_asker("Mrudul won the kettle.", "parm", asker_won_something=False)


def test_floor_fine_print_lists_the_closed_lots():
    auctions = [dict(_auction(status="CLOSED"), auction_id="a2", title="Fest hoodie")]
    text = assistant.floor_fine_print(auctions, {"a2": {"winner_id": "user_karan", "final_price": 300.0}})
    assert "Fest hoodie - won by karan for Rs 300.00" in text
    assert "no lot has closed yet" in assistant.floor_fine_print([], {})


def test_question_routing_words():
    assert assistant.about_results("What sold recently?") and not assistant.about_results("How does escrow work?")
    assert assistant.about_wallet("How much can I bid with my wallet?")
    assert not assistant.about_wallet("Which lots have closed?")
