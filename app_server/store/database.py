import sqlite3
import os
import uuid
import logging
from typing import Optional
from datetime import datetime

log = logging.getLogger(__name__)


DB_PATH = os.path.join(os.path.dirname(__file__), "..", "auctions.db")


def get_connection() -> sqlite3.Connection:
    """A new connection per call, with rows readable by column name."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


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
                reserve_price REAL DEFAULT 0.0
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
                created_at INTEGER NOT NULL
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


def create_user(username: str, user_id: str, password_hash: str) -> bool:
    """Register a new user with a hashed password. Returns False if
    the username or user_id already exists."""
    conn = get_connection()
    try:
        now_ms = datetime.now().timestamp() * 1000
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


def create_session(session_token: str, user_id: str, username: str) -> None:
    """Start a session, replacing the user's previous one (one per user)."""
    conn = get_connection()
    try:
        now_ms = datetime.now().timestamp() * 1000
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
                 duration_seconds: int = 3600, reserve_price: float = 0.0) -> bool:
    """Create a new item and an immediately-active auction for it."""
    conn = get_connection()
    try:
        existing = conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()

        if not existing:
            conn.execute("""
                INSERT INTO items (item_id, title, description, seller_id)
                VALUES (?, ?, ?, ?)
            """, (item_id, title, description, seller_id))

            now_ms = datetime.now().timestamp() * 1000
            end_time_ms = now_ms + duration_seconds * 1000
            conn.execute("""
                INSERT INTO auctions (auction_id, item_id, title, description, seller_id,
                                     status, current_highest_bid, current_highest_bidder_id, end_time_ms, reserve_price)
                VALUES (?, ?, ?, ?, ?, 'ACTIVE', 0.0, ?, ?, ?)
            """, (item_id, item_id, title, description, seller_id, None, end_time_ms, reserve_price))

        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def create_bid(auction_id: str, bidder_id: str, amount: float) -> tuple[bool, str]:
    """Record a bid if it is valid. Returns (accepted, bid_id or the reason it was refused).

    The highest bid is only ever raised by a compare-and-set UPDATE ("set it to X
    where it is still below X"), so bids racing for the same auction can never let
    a lower one overwrite a higher one, however they are scheduled.
    """
    conn = get_connection()
    try:
        now_ms = datetime.now().timestamp() * 1000
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

        current_bid = auction["current_highest_bid"] or 0.0
        if amount <= current_bid:
            return False, f"Bid amount must be greater than current highest bid ({current_bid})"

        if amount < (auction["reserve_price"] or 0.0):
            return False, f"Bid does not meet reserve price ({auction['reserve_price']})"

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

        bid_id = str(uuid.uuid4())[:8]
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


def get_bids_for_auction(auction_id: str) -> list[dict]:
    """All bids on an auction, oldest first, with the bidder's username.

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
            ORDER BY b.timestamp_ms ASC, b.rowid ASC
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
                      final_price: float, buyers_premium: float, escrow_status: str) -> bool:
    """Record the mock escrow settlement for a closed auction. Idempotent:
    returns False (and changes nothing) if one already exists."""
    conn = get_connection()
    try:
        now_ms = datetime.now().timestamp() * 1000
        cur = conn.execute("""
            INSERT OR IGNORE INTO settlements
            (auction_id, winner_id, seller_id, final_price, buyers_premium, escrow_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (auction_id, winner_id, seller_id, final_price, buyers_premium, escrow_status, now_ms))
        conn.commit()
        return cur.rowcount == 1
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


def save_summary(auction_id: str, summary: str, from_fallback: bool) -> None:
    conn = get_connection()
    try:
        now_ms = datetime.now().timestamp() * 1000
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
