"""
db.py — the conductor's SQLite connection helper + idempotent schema init.

Replaces hand-edited conductor/STATUS.md prose + conductor/DECISIONS.md rows with a
small local SQLite database. Mirrors the plain-dict-and-function style of
connections/connections/clientids.py: no ORM, no framework, just a thin helper.

The DB lives OFF Drive at C:\\TradingDesk-Local\\conductor\\conductor.db (never synced —
Drive-sync mid-write corrupts files, a documented problem for other local state in this
repo; see connections/README.md / CLAUDE.md "Where things live").
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_DIR = Path(r"C:\TradingDesk-Local\conductor")
DB_PATH = DB_DIR / "conductor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS log_entries (
    id INTEGER PRIMARY KEY,
    date TEXT,
    session_tag TEXT,
    title TEXT,
    body_md TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    title TEXT,
    area TEXT,
    status TEXT CHECK(status IN ('open', 'blocked', 'done')),
    opened_date TEXT,
    last_touched TEXT,
    closed_date TEXT,
    session_tag TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    lane TEXT,
    question TEXT,
    options TEXT,
    status TEXT CHECK(status IN ('pending', 'answered')),
    answer TEXT,
    decided_date TEXT
);
"""


def get_connection() -> sqlite3.Connection:
    """Open (creating the folder/file/schema if needed) a connection in WAL mode."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Idempotent — safe to call on every connect. CREATE TABLE IF NOT EXISTS only."""
    conn.executescript(SCHEMA)
    conn.commit()


if __name__ == "__main__":
    # Manual smoke check: `python conductor/db.py` creates the DB + prints table list.
    c = get_connection()
    tables = [r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
    )]
    print(f"DB at {DB_PATH}: tables = {tables}")
    c.close()
