"""Unit tests for database.get_average_sale_price - the DB-layer half of
the pricing-trend feature. Runs directly against the real store module
(no live stack needed): foreign keys aren't enforced, so a settlement row
can be inserted without a real auction/item existing first. Uses a unique
auction_id prefix per test run and cleans up afterward, since this is the
same auctions.db a locally-run server would use.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app_server"))

from store import database  # noqa: E402


def _settle(auction_id, winner_id, final_price, prefix):
    database.create_settlement(
        auction_id=f"{prefix}-{auction_id}",
        winner_id=winner_id,
        seller_id="pytest-seller",
        final_price=final_price,
        buyers_premium=round(final_price * 0.05, 2),
        escrow_status="HELD (mock)" if winner_id else "NO_SALE",
    )


def _cleanup(prefix):
    conn = database.get_connection()
    try:
        conn.execute("DELETE FROM settlements WHERE auction_id LIKE ?", (f"{prefix}-%",))
        conn.commit()
    finally:
        conn.close()


def test_average_excludes_the_auction_itself_and_no_sales():
    database.init_database()
    prefix = f"trend-{uuid.uuid4().hex[:8]}"
    try:
        _settle("a", "priya", 100.0, prefix)
        _settle("b", "karan", 200.0, prefix)
        _settle("c", None, 0.0, prefix)  # NO_SALE - must not pull the average toward 0

        avg = database.get_average_sale_price(exclude_auction_id=f"{prefix}-c")
        assert avg == 150.0  # (100 + 200) / 2, "c" excluded anyway (it's a no-sale, not a sale)

        avg_excluding_a = database.get_average_sale_price(exclude_auction_id=f"{prefix}-a")
        assert avg_excluding_a == 200.0  # only "b" remains once "a" is excluded
    finally:
        _cleanup(prefix)


def test_average_is_none_with_no_other_sales_yet():
    database.init_database()
    prefix = f"trend-{uuid.uuid4().hex[:8]}"
    try:
        _settle("only", "priya", 100.0, prefix)
        # excluding the only sale that exists leaves nothing to average
        assert database.get_average_sale_price(exclude_auction_id=f"{prefix}-only") is None
    finally:
        _cleanup(prefix)


def test_average_with_no_settlements_at_all_is_none():
    database.init_database()
    assert database.get_average_sale_price(exclude_auction_id="pytest-nonexistent-id") is None
