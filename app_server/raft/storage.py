import sqlite3


class RaftStorage:
    """The state a Raft node must never lose: currentTerm, votedFor and the log
    (Figure 2, "persistent state on all servers").

    Every change is on disk *before* the node replies to the RPC that caused
    it. Otherwise a node that crashes and restarts could vote twice in the same
    term, or forget an entry it already told the leader it had - both of which
    break Raft's safety guarantees.

    Log indexes start at 1, as in the paper; index 0 means "before the log".
    """

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        # synchronous=FULL: commit() returns only once the data is on disk.
        self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS log (idx INTEGER PRIMARY KEY, term INTEGER NOT NULL, command BLOB NOT NULL)"
        )
        self._conn.commit()

    def load(self) -> tuple[int, str | None, list[tuple[int, bytes]]]:
        """Returns (current_term, voted_for, [(term, command), ...] in log order)."""
        meta = dict(self._conn.execute("SELECT key, value FROM meta"))
        entries = [
            (term, bytes(command))
            for term, command in self._conn.execute("SELECT term, command FROM log ORDER BY idx")
        ]
        return int(meta.get("current_term", 0)), meta.get("voted_for") or None, entries

    def save_term_and_vote(self, term: int, voted_for: str | None) -> None:
        self._conn.execute("INSERT OR REPLACE INTO meta VALUES ('current_term', ?)", (str(term),))
        self._conn.execute("INSERT OR REPLACE INTO meta VALUES ('voted_for', ?)", (voted_for or "",))
        self._conn.commit()

    def append(self, entries: list[tuple[int, int, bytes]]) -> None:
        """Persist [(index, term, command), ...] in one transaction."""
        self._conn.executemany("INSERT OR REPLACE INTO log (idx, term, command) VALUES (?, ?, ?)", entries)
        self._conn.commit()

    def truncate_from(self, index: int) -> None:
        """Delete the entry at `index` and everything after it."""
        self._conn.execute("DELETE FROM log WHERE idx >= ?", (index,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
