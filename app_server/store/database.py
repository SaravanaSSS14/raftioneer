import sqlite3
import os
from typing import Optional
from datetime import datetime


DB_PATH = os.path.join(os.path.dirname(__file__), "..", "auctions.db")


def get_connection() -> sqlite3.Connection:
    """Get a thread-safe connection with row factory."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    """Initialize all tables. Called once at startup."""
    conn = get_connection()
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        
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
        
        conn.commit()
        print(f"[DB] Database initialized at {DB_PATH}")
        
    except sqlite3.Error as e:
        print(f"[DB] Error initializing database: {e}")
        raise
    finally:
        conn.close()


def create_session(username: str, session_token: str, user_id: str, 
                   expires_in_minutes: int = 60) -> Optional[str]:
    """Create a new session and return the token if successful."""
    conn = get_connection()
    try:
        now_ms = datetime.now().timestamp() * 1000
        expires_at_ms = now_ms + (expires_in_minutes * 60 * 1000)
        
        conn.execute("""
            INSERT INTO sessions (session_token, user_id, username, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
        """, (session_token, user_id, username, now_ms, expires_at_ms))
        
        conn.commit()
        return session_token
    except sqlite3.IntegrityError:
        return None
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
    """Create a new item and an immediately-active auction for it.

    Bug fixes applied here (see Raftioneer_Bug_Solutions_Guide.pdf, Bug 6):
    the INSERT INTO auctions statement named 9 columns but supplied only
    6 values (a placeholder-count mismatch) - fixed by matching column
    and value lists 1:1. Separately, this function never called
    conn.commit(), so even correct SQL would have been silently rolled
    back when the connection closed - added below.
    """
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
    """Create a new bid. Returns (success, error_message or bid_id)."""
    conn = get_connection()
    try:
        now_ms = datetime.now().timestamp() * 1000
        
        auction = conn.execute("""SELECT * FROM auctions WHERE auction_id = ?""", (auction_id,)).fetchone()

        if not auction:
            return False, "Auction not found"

        # sqlite3.Row supports [] but not .get() - convert once so both
        # work below (bug: auction.get(...) on a raw Row raised AttributeError
        # on every bid that was otherwise valid, before the INSERT ever ran).
        auction = dict(auction)

        if auction["status"] != "ACTIVE":
            return False, f"Auction is {auction['status']}, cannot place bid"
        
        if now_ms >= auction["end_time_ms"]:
            conn.execute("""UPDATE auctions SET status = 'CLOSED' WHERE auction_id = ?""", (auction_id,))
            return False, "Auction has ended"
        
        current_bid = auction["current_highest_bid"] or 0.0
        
        if amount <= current_bid:
            return False, f"Bid amount must be greater than current highest bid ({current_bid})"
        
        if amount < auction.get("reserve_price", 0):
            return False, f"Bid does not meet reserve price ({auction['reserve_price']})"
        
        import uuid
        bid_id = str(uuid.uuid4())[:8]
        
        conn.execute("""
            INSERT INTO bids (bid_id, auction_id, bidder_id, amount, timestamp_ms)
            VALUES (?, ?, ?, ?, ?)
        """, (bid_id, auction_id, bidder_id, amount, now_ms))
        
        conn.execute("""
            UPDATE auctions 
            SET current_highest_bid = ?, current_highest_bidder_id = ?
            WHERE auction_id = ?
        """, (amount, bidder_id, auction_id))
        
        conn.commit()
        return True, bid_id
        
    except sqlite3.Error as e:
        return False, str(e)
    finally:
        conn.close()


def get_bids_for_auction(auction_id: str) -> list[dict]:
    """Get all bids for an auction."""
    conn = get_connection()
    try:
        result = conn.execute("""
            SELECT b.*, s.username as bidder_name
            FROM bids b
            JOIN sessions s ON b.bidder_id = s.user_id
            WHERE b.auction_id = ?
            ORDER BY b.timestamp_ms DESC
        """, (auction_id,)).fetchall()
        
        return [dict(b) for b in result]
    finally:
        conn.close()


def get_user_by_username(username: str) -> Optional[str]:
    """Get user ID by username."""
    conn = get_connection()
    try:
        result = conn.execute("""SELECT user_id FROM sessions WHERE username = ? LIMIT 1""", (username,)).fetchone()
        
        return result["user_id"] if result else None
    finally:
        conn.close()
