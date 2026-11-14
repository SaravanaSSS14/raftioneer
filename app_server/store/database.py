import math
import sqlite3
import os
import uuid
import logging
from typing import Optional
from datetime import datetime

log = logging.getLogger(__name__)


DB_PATH = os.path.join(os.path.dirname(__file__), "..", "auctions.db")

# Under Raft this database is not the source of truth: it is rebuilt from the
# replicated log every time the node starts (see main.py), so main.py turns
# off the per-commit fsync for speed. Used on its own (the unit tests), it
# stays fully durable.
DURABLE = True


def get_connection() -> sqlite3.Connection:
    """A new connection per call, with rows readable by column name."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if not DURABLE:
        conn.execute("PRAGMA synchronous = OFF")
    return conn


def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def reset_database() -> None:
    """Start from empty tables. Called at startup under Raft, before the log
    is replayed into the database."""
    for path in (DB_PATH, DB_PATH + "-journal"):
        if os.path.exists(path):
            os.remove(path)
    init_database()


def init_database():
    """Initialize all tables. Called once at startup."""
    conn = get_connection()
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        # Registered users and their salted password hashes.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                user_id TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)

        # Create Sessions table for user authentication
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)
        
        # Create Auctions table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auctions (
                auction_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                seller_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PENDING', 'ACTIVE', 'CLOSED')),
                current_highest_bid REAL NOT NULL DEFAULT 0.0,
                current_highest_bidder_id TEXT,
                end_time_ms INTEGER NOT NULL,
                reserve_price REAL DEFAULT 0.0,
                -- anti-sniping: fixed when the auction is listed, so the rules
                -- can't change mid-auction (e.g. when the leader changes)
                original_end_ms INTEGER,
                snipe_window_ms INTEGER NOT NULL DEFAULT 0,
                max_end_ms INTEGER
            )
        """)
        
        # Create Bids table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bids (
                bid_id TEXT PRIMARY KEY,
                auction_id TEXT NOT NULL,
                bidder_id TEXT NOT NULL,
                amount REAL NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                FOREIGN KEY (auction_id) REFERENCES auctions(auction_id),
                FOREIGN KEY (bidder_id) REFERENCES sessions(user_id)
            )
        """)
        
        # Create Item table for item metadata
        conn.execute("""
            CREATE TABLE IF NOT EXISTS items (
                item_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                seller_id TEXT NOT NULL
            )
        """)

        # Mock escrow settlement, one row per closed auction. No money moves:
        # this records what a real payment step would hold and why.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settlements (
                auction_id TEXT PRIMARY KEY,
                winner_id TEXT,
                seller_id TEXT NOT NULL,
                final_price REAL NOT NULL,
                buyers_premium REAL NOT NULL,
                escrow_status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                released_at INTEGER
            )
        """)

        # Exactly-once bids: the outcome of every PlaceBid that carried a
        # request id, so a retry of the same request gets the same answer
        # instead of being applied a second time. Scoped per bidder.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS applied_requests (
                bidder_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                result TEXT NOT NULL,
                PRIMARY KEY (bidder_id, request_id)
            )
        """)

        # The recap written after an auction closes (by the LLM node, or a
        # templated fallback if it was unreachable).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auction_summaries (
                auction_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                from_fallback INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)

        # Mock payments. One wallet per user; `balance` is the money in it
        # (money paid into escrow has already left). Every change is also a
        # row in `transactions`, so a wallet can always be explained. Both
        # tables are only ever written by applying committed Raft commands,
        # in log order, so txn_id is the same on every node.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                user_id TEXT PRIMARY KEY,
                balance REAL NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                txn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                amount REAL NOT NULL,
                auction_id TEXT,
                balance_after REAL NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)

        conn.commit()
        log.info("database ready at %s", os.path.abspath(DB_PATH))
    except sqlite3.Error:
        log.exception("could not initialize the database")
        raise
    finally:
        conn.close()


def get_user(username: str) -> Optional[dict]:
    """Look up a user's stored credentials by username."""
    conn = get_connection()
    try:
        result = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(result) if result else None
    finally:
        conn.close()


def create_user(username: str, user_id: str, password_hash: str, now_ms: Optional[int] = None) -> bool:
    """Register a new user with a hashed password. Returns False if
    the username or user_id already exists."""
    conn = get_connection()
    try:
        now_ms = now_ms if now_ms is not None else _now_ms()
        conn.execute("""
            INSERT INTO users (username, user_id, password_hash, created_at)
            VALUES (?, ?, ?, ?)
        """, (username, user_id, password_hash, now_ms))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


SESSION_TTL_MS = 3_600_000  # one hour


def create_session(session_token: str, user_id: str, username: str, now_ms: Optional[int] = None) -> None:
    """Start a session, replacing the user's previous one (one per user)."""
    conn = get_connection()
    try:
        now_ms = now_ms if now_ms is not None else _now_ms()
        conn.execute("""
            INSERT OR REPLACE INTO sessions (session_token, user_id, username, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
        """, (session_token, user_id, username, now_ms, now_ms + SESSION_TTL_MS))
        conn.commit()
    finally:
        conn.close()


def validate_session(session_token: str) -> Optional[dict]:
    """Validate a session and return user info if valid."""
    conn = get_connection()
    try:
        now_ms = datetime.now().timestamp() * 1000
        
        result = conn.execute("""
            SELECT * FROM sessions 
            WHERE session_token = ? AND expires_at > ?
        """, (session_token, now_ms)).fetchone()
        
        return dict(result) if result else None
    finally:
        conn.close()


def delete_session(session_token: str) -> bool:
    """Delete a session (logout)."""
    conn = get_connection()
    try:
        conn.execute("""DELETE FROM sessions WHERE session_token = ?""", (session_token,))
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()



def get_all_auctions(status_filter: Optional[str] = None) -> list[dict]:
    """Get all auctions, optionally filtered by status."""
    conn = get_connection()
    try:
        query = """
            SELECT a.*, i.title, i.description, i.seller_id 
            FROM auctions a
            JOIN items i ON a.item_id = i.item_id
        """
        
        if status_filter and status_filter.upper():
            auctions = conn.execute(f"{query} WHERE a.status = ?", (status_filter,)).fetchall()
        else:
            auctions = conn.execute(query).fetchall()
        
        return [dict(a) for a in auctions]
    finally:
        conn.close()


def get_auction(auction_id: str) -> Optional[dict]:
    """Get a single auction by ID with full details."""
    conn = get_connection()
    try:
        result = conn.execute("""
            SELECT a.*, i.title, i.description, i.seller_id
            FROM auctions a
            JOIN items i ON a.item_id = i.item_id
            WHERE a.auction_id = ?
        """, (auction_id,)).fetchone()
        
        return dict(result) if result else None
    finally:
        conn.close()


def create_item(item_id: str, title: str, description: str, seller_id: str,
                 duration_seconds: int = 3600, reserve_price: float = 0.0,
                 now_ms: Optional[int] = None, snipe_window_seconds: int = 0,
                 max_extension_seconds: int = 0) -> bool:
    """Create a new item and an immediately-active auction for it.

    Anti-sniping (off when snipe_window_seconds is 0): a bid accepted in the
    last snipe_window_seconds moves the end time to snipe_window_seconds after
    that bid, but never more than max_extension_seconds past the original end.
    """
    conn = get_connection()
    try:
        existing = conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()

        if not existing:
            conn.execute("""
                INSERT INTO items (item_id, title, description, seller_id)
                VALUES (?, ?, ?, ?)
            """, (item_id, title, description, seller_id))

            now_ms = now_ms if now_ms is not None else _now_ms()
            end_time_ms = now_ms + duration_seconds * 1000
            conn.execute("""
                INSERT INTO auctions (auction_id, item_id, title, description, seller_id,
                                     status, current_highest_bid, current_highest_bidder_id, end_time_ms, reserve_price,
                                     original_end_ms, snipe_window_ms, max_end_ms)
                VALUES (?, ?, ?, ?, ?, 'ACTIVE', 0.0, ?, ?, ?, ?, ?, ?)
            """, (item_id, item_id, title, description, seller_id, None, end_time_ms, reserve_price,
                  end_time_ms, snipe_window_seconds * 1000, end_time_ms + max_extension_seconds * 1000))

        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def create_bid(auction_id: str, bidder_id: str, amount: float,
               now_ms: Optional[int] = None, bid_id: Optional[str] = None,
               available_funds: Optional[float] = None, premium_percent: float = 0.0) -> tuple[bool, str]:
    """Record a bid if it is valid. Returns (accepted, bid_id or the reason it was refused).

    available_funds, if given, is what the bidder can still spend (see
    available_funds()); a bid whose price plus buyer's premium is more than
    that is refused. None skips the check (the unit tests of the bid rules).

    Under Raft the time and bid id come from the replicated command, so every
    node reaches the same verdict on the same bid.

    The highest bid is only ever raised by a compare-and-set UPDATE ("set it to X
    where it is still below X"), so bids racing for the same auction can never let
    a lower one overwrite a higher one, however they are scheduled.
    """
    conn = get_connection()
    try:
        now_ms = now_ms if now_ms is not None else _now_ms()
        row = conn.execute("SELECT * FROM auctions WHERE auction_id = ?", (auction_id,)).fetchone()
        if not row:
            return False, "Auction not found"
        auction = dict(row)

        if auction["status"] != "ACTIVE":
            return False, f"Auction is {auction['status']}, cannot place bid"

        if now_ms >= auction["end_time_ms"]:
            # Past its deadline but the timer hasn't ticked yet: close it now.
            conn.execute("UPDATE auctions SET status = 'CLOSED' WHERE auction_id = ?", (auction_id,))
            conn.commit()
            return False, "Auction has ended"

        if auction["seller_id"] == bidder_id:
            return False, "Sellers cannot bid on their own auction"

        # NaN compares false with everything and +inf beats every bid, so either
        # would slip through the checks below; inf would win any auction outright.
        if not math.isfinite(amount) or amount <= 0:
            return False, "Bid amount must be a positive number"

        current_bid = auction["current_highest_bid"] or 0.0
        if amount <= current_bid:
            return False, f"Bid amount must be greater than current highest bid ({current_bid})"

        if amount < (auction["reserve_price"] or 0.0):
            return False, f"Bid does not meet reserve price ({auction['reserve_price']})"

        if available_funds is not None:
            needed = round(amount * (1 + premium_percent / 100.0), 2)
            if needed > available_funds + 0.005:
                return False, (
                    f"Insufficient funds: this bid needs Rs {needed:,.2f} with the buyer's premium, "
                    f"and you have Rs {max(available_funds, 0.0):,.2f} available"
                )

        raised = conn.execute("""
            UPDATE auctions SET current_highest_bid = ?, current_highest_bidder_id = ?
            WHERE auction_id = ? AND status = 'ACTIVE' AND current_highest_bid < ?
        """, (amount, bidder_id, auction_id, amount))
        if raised.rowcount != 1:  # a higher bid - or the close - landed first
            conn.rollback()
            latest = conn.execute(
                "SELECT status, current_highest_bid FROM auctions WHERE auction_id = ?", (auction_id,)
            ).fetchone()
            if latest["status"] != "ACTIVE":
                return False, f"Auction is {latest['status']}, cannot place bid"
            return False, f"Bid amount must be greater than current highest bid ({latest['current_highest_bid']})"

        # Anti-sniping: a bid in the closing window buys everyone else time to
        # answer it - the end moves to one full window after this bid, up to
        # the auction's cap. It's part of applying the bid, so every node moves
        # the clock identically (now_ms is the leader's, from the command).
        window = auction.get("snipe_window_ms") or 0
        if window and auction["end_time_ms"] - now_ms <= window:
            new_end = min(now_ms + window, auction["max_end_ms"] or auction["end_time_ms"])
            if new_end > auction["end_time_ms"]:
                conn.execute("UPDATE auctions SET end_time_ms = ? WHERE auction_id = ?", (new_end, auction_id))

        bid_id = bid_id or str(uuid.uuid4())[:8]
        conn.execute("""
            INSERT INTO bids (bid_id, auction_id, bidder_id, amount, timestamp_ms)
            VALUES (?, ?, ?, ?, ?)
        """, (bid_id, auction_id, bidder_id, amount, now_ms))
        conn.commit()
        return True, bid_id

    except sqlite3.Error:
        log.exception("create_bid failed for auction %s", auction_id)
        return False, "Could not record bid, please try again"
    finally:
        conn.close()


def close_expired_auctions(now_ms: int) -> list[dict]:
    """Close every ACTIVE auction past its end time and return the ones just
    closed. current_highest_bidder_id at close time is the winner (or empty if
    nobody bid); bids against a closed auction are refused, so it can't change
    afterwards.
    """
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT a.*, i.title, i.description, i.seller_id
            FROM auctions a
            JOIN items i ON a.item_id = i.item_id
            WHERE a.status = 'ACTIVE' AND a.end_time_ms <= ?
        """, (now_ms,)).fetchall()

        closed = []
        for row in rows:
            conn.execute("UPDATE auctions SET status = 'CLOSED' WHERE auction_id = ?", (row["auction_id"],))
            record = dict(row)
            record["status"] = "CLOSED"
            closed.append(record)

        conn.commit()
        return closed
    finally:
        conn.close()


def has_expired_active(now_ms: int) -> bool:
    """Is any ACTIVE auction past its end time? The leader checks this before
    proposing a close, so an idle cluster doesn't fill its log with no-ops."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM auctions WHERE status = 'ACTIVE' AND end_time_ms <= ? LIMIT 1", (now_ms,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_bids_for_auction(auction_id: str) -> list[dict]:
    """All bids on an auction in the order they were accepted, with the
    bidder's username.

    Ordered by rowid - the order the bids were applied from the Raft log,
    identical on every node - rather than by timestamp, which a clock
    adjustment on the leader could reorder.

    Joins users (a permanent identity), not sessions: a session row goes
    away on logout, and an INNER JOIN on it would silently drop that
    person's bids from the history.
    """
    conn = get_connection()
    try:
        result = conn.execute("""
            SELECT b.*, u.username AS bidder_name
            FROM bids b
            LEFT JOIN users u ON b.bidder_id = u.user_id
            WHERE b.auction_id = ?
            ORDER BY b.rowid ASC
        """, (auction_id,)).fetchall()

        return [dict(b) for b in result]
    finally:
        conn.close()


def get_closed_unsettled() -> list[dict]:
    """Closed auctions that have no settlement yet.

    An auction can close two ways - the background timer, or a late bid
    arriving after its deadline (create_bid's reactive path). Settling
    everything that is CLOSED-and-unsettled, rather than only what the
    timer itself just closed, covers both (and survives a restart).
    """
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT a.*, i.title, i.description, i.seller_id
            FROM auctions a
            JOIN items i ON a.item_id = i.item_id
            LEFT JOIN settlements s ON s.auction_id = a.auction_id
            WHERE a.status = 'CLOSED' AND s.auction_id IS NULL
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_settlement(auction_id: str, winner_id: Optional[str], seller_id: str,
                      final_price: float, buyers_premium: float, escrow_status: str,
                      now_ms: Optional[int] = None) -> bool:
    """Record the mock escrow settlement for a closed auction. Idempotent:
    returns False (and changes nothing) if one already exists."""
    conn = get_connection()
    try:
        now_ms = now_ms if now_ms is not None else _now_ms()
        cur = conn.execute("""
            INSERT OR IGNORE INTO settlements
            (auction_id, winner_id, seller_id, final_price, buyers_premium, escrow_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (auction_id, winner_id, seller_id, final_price, buyers_premium, escrow_status, now_ms))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def get_unsummarized() -> list[dict]:
    """Settled auctions that have no recap yet, with what the recap needs."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT a.*, i.title, i.description, i.seller_id, s.winner_id, s.final_price
            FROM settlements s
            JOIN auctions a ON a.auction_id = s.auction_id
            JOIN items i ON a.item_id = i.item_id
            LEFT JOIN auction_summaries m ON m.auction_id = s.auction_id
            WHERE m.auction_id IS NULL
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_request_result(bidder_id: str, request_id: str) -> Optional[str]:
    """The stored outcome (JSON) of a request already applied, or None."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT result FROM applied_requests WHERE bidder_id = ? AND request_id = ?", (bidder_id, request_id)
        ).fetchone()
        return row["result"] if row else None
    finally:
        conn.close()


def save_request_result(bidder_id: str, request_id: str, result: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO applied_requests (bidder_id, request_id, result) VALUES (?, ?, ?)",
            (bidder_id, request_id, result),
        )
        conn.commit()
    finally:
        conn.close()


def get_settlement(auction_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM settlements WHERE auction_id = ?", (auction_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_average_sale_price(exclude_auction_id: str) -> Optional[float]:
    """Average final_price across other settlements that actually sold
    (winner_id set - a NO_SALE settlement has none), this auction excluded
    so a sale is never averaged against itself. None if there are no
    other sales yet - the caller must not fabricate a trend from zero data.
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT AVG(final_price) AS avg_price FROM settlements
            WHERE winner_id IS NOT NULL AND auction_id != ?
        """, (exclude_auction_id,)).fetchone()
        return float(row["avg_price"]) if row and row["avg_price"] is not None else None
    finally:
        conn.close()


def save_summary(auction_id: str, summary: str, from_fallback: bool, now_ms: Optional[int] = None) -> None:
    conn = get_connection()
    try:
        now_ms = now_ms if now_ms is not None else _now_ms()
        conn.execute("""
            INSERT OR REPLACE INTO auction_summaries (auction_id, summary, from_fallback, created_at)
            VALUES (?, ?, ?, ?)
        """, (auction_id, summary, 1 if from_fallback else 0, now_ms))
        conn.commit()
    finally:
        conn.close()


def get_summary(auction_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM auction_summaries WHERE auction_id = ?", (auction_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------- payments (mock)
# Called only from the state machine, while applying committed commands - so
# the same calls happen in the same order on every node.

# Settlement states. HELD and NO_SALE match assistant.ESCROW_HELD / ESCROW_NO_SALE.
ESCROW_HELD = "HELD (mock)"
ESCROW_RELEASED = "RELEASED (mock)"
ESCROW_UNPAID = "UNPAID (mock)"


def create_wallet(user_id: str, opening_credit: float, now_ms: int) -> None:
    """Open a user's wallet, crediting the welcome amount (if any)."""
    conn = get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO wallets (user_id, balance) VALUES (?, 0)", (user_id,))
        if opening_credit > 0:
            _move_money(conn, user_id, opening_credit, "WELCOME_CREDIT", None, now_ms)
        conn.commit()
    finally:
        conn.close()


def _move_money(conn: sqlite3.Connection, user_id: str, amount: float, kind: str,
                auction_id: Optional[str], now_ms: int) -> float:
    """Add `amount` (negative = take it out) to a wallet and record it. Returns
    the new balance. The caller commits."""
    conn.execute("UPDATE wallets SET balance = ROUND(balance + ?, 2) WHERE user_id = ?", (amount, user_id))
    balance = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,)).fetchone()["balance"]
    conn.execute("""
        INSERT INTO transactions (user_id, kind, amount, auction_id, balance_after, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, kind, round(amount, 2), auction_id, balance, now_ms))
    return balance


def deposit(user_id: str, amount: float, now_ms: int) -> Optional[float]:
    """Mock top-up. Returns the new balance, or None if the user has no wallet."""
    conn = get_connection()
    try:
        if conn.execute("SELECT 1 FROM wallets WHERE user_id = ?", (user_id,)).fetchone() is None:
            return None
        balance = _move_money(conn, user_id, amount, "DEPOSIT", None, now_ms)
        conn.commit()
        return balance
    finally:
        conn.close()


def get_wallet_balance(user_id: str) -> Optional[float]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
        return float(row["balance"]) if row else None
    finally:
        conn.close()


def committed_funds(user_id: str, premium_percent: float, exclude_auction_id: Optional[str] = None) -> float:
    """Money promised to auctions this user is currently winning: each
    leading bid plus its buyer's premium. A new bid on one of them replaces
    the old promise, hence exclude_auction_id."""
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT COALESCE(SUM(current_highest_bid), 0) AS total FROM auctions
            WHERE status = 'ACTIVE' AND current_highest_bidder_id = ? AND auction_id != ?
        """, (user_id, exclude_auction_id or "")).fetchone()
        return round(float(row["total"]) * (1 + premium_percent / 100.0), 2)
    finally:
        conn.close()


def available_funds(user_id: str, premium_percent: float, exclude_auction_id: Optional[str] = None) -> float:
    """What a new bid can use: the balance less what is already promised. 0
    for a user with no wallet."""
    balance = get_wallet_balance(user_id)
    if balance is None:
        return 0.0
    return round(balance - committed_funds(user_id, premium_percent, exclude_auction_id), 2)


def in_escrow(user_id: str) -> float:
    """Money this user has paid into escrow for won auctions not yet handed over."""
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT COALESCE(SUM(final_price + buyers_premium), 0) AS total FROM settlements
            WHERE winner_id = ? AND escrow_status = ?
        """, (user_id, ESCROW_HELD)).fetchone()
        return round(float(row["total"]), 2)
    finally:
        conn.close()


def hold_in_escrow(auction_id: str, winner_id: str, amount: float, now_ms: int) -> bool:
    """Move the winner's price + premium from their wallet into escrow. False
    (nothing moved) if the wallet can't cover it - the funds check at bid time
    means that shouldn't happen, but a settlement must never go negative."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (winner_id,)).fetchone()
        if row is None or row["balance"] + 0.005 < amount:
            return False
        _move_money(conn, winner_id, -amount, "ESCROW_HOLD", auction_id, now_ms)
        conn.commit()
        return True
    finally:
        conn.close()


def release_escrow(auction_id: str, user_id: str, now_ms: int) -> tuple[bool, str]:
    """The winner confirms hand-over: escrow is released and the seller is
    paid the final price (the buyer's premium stays with the auction house).
    Returns (released, escrow status or the reason it was refused)."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM settlements WHERE auction_id = ?", (auction_id,)).fetchone()
        if row is None:
            return False, "This auction has not been settled yet"
        if row["winner_id"] != user_id:
            return False, "Only the winner can confirm receipt"
        if row["escrow_status"] != ESCROW_HELD:
            return False, f"Nothing to release: escrow is {row['escrow_status']}"
        conn.execute(
            "UPDATE settlements SET escrow_status = ?, released_at = ? WHERE auction_id = ?",
            (ESCROW_RELEASED, now_ms, auction_id),
        )
        conn.execute("INSERT OR IGNORE INTO wallets (user_id, balance) VALUES (?, 0)", (row["seller_id"],))
        _move_money(conn, row["seller_id"], float(row["final_price"]), "SALE_PROCEEDS", auction_id, now_ms)
        conn.commit()
        return True, ESCROW_RELEASED
    finally:
        conn.close()


def set_escrow_status(auction_id: str, status: str) -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE settlements SET escrow_status = ? WHERE auction_id = ?", (status, auction_id))
        conn.commit()
    finally:
        conn.close()


def list_transactions(user_id: str, limit: int = 50) -> list[dict]:
    """A user's wallet history, newest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE user_id = ? ORDER BY txn_id DESC LIMIT ?", (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_settlements_won_by(user_id: str) -> list[dict]:
    """Settled auctions this user won, newest first, with the item title."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT s.*, i.title FROM settlements s
            JOIN auctions a ON a.auction_id = s.auction_id
            JOIN items i ON a.item_id = i.item_id
            WHERE s.winner_id = ? ORDER BY s.created_at DESC
        """, (user_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
