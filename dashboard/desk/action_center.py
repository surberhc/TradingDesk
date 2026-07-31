"""action_center.py — the desk dashboard's in-app Action Center notice store.

The Action Center is the desk's propose-and-arm INBOX: a durable, plain-English list of
things that want the operator's attention but NEVER act on their own. Today it carries the
"idle cash — consider deploying" proposal (dailyreport/s0_cash_deploy_check.py); it is built
to hold any future propose-and-arm notice. A notice only ever POINTS the operator at a page
(e.g. the Control Plane) — it places, arms, and transmits nothing.

This mirrors eventlog.py's discipline: it is a WRITE module, and it writes to exactly one
place — its own SQLite store at
``C:\\TradingDesk-Local\\state\\desk_dashboard\\action_center.db``. It never touches the
trading warehouse, any strategy/regime config, the capture store, or the gateway arming state.

PLAIN-ENGLISH RULE (#1): every stored title/body is a full, non-technical sentence. Severity
carries color on the page; the words stand on their own.

STATE MODEL: a notice is 'unread' until the operator dismisses it (then 'dismissed').
De-duplication is by an optional ``dedup_key``: posting again with a dedup_key that already
has an OPEN (unread) notice UPDATES that notice in place (refreshing its numbers and time)
instead of stacking a duplicate — so a daily cash check that keeps finding the same idle cash
shows ONE current notice, not a growing pile. Once dismissed, a later post with the same
dedup_key creates a fresh notice (the operator acted; a new heads-up is legitimate).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

DB_DIR = Path(r"C:\TradingDesk-Local\state\desk_dashboard")
_DEFAULT_DB = DB_DIR / "action_center.db"


def db_path() -> Path:
    """Where the Action Center store lives. Env override lets a test point at a temp file so
    the real store is never touched."""
    env = os.environ.get("TRADINGDESK_ACTION_CENTER_DB")
    return Path(env) if env else _DEFAULT_DB


def _connect() -> sqlite3.Connection:
    """Open (creating the folder + table on first use) the Action Center DB. WRITE target is
    ONLY this file — never any trading store."""
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS notices (
            notice_key   TEXT UNIQUE,
            ts           TEXT,
            day          TEXT,
            kind         TEXT,
            severity     TEXT,
            title        TEXT,
            body         TEXT,
            action_hint  TEXT,
            dedup_key    TEXT,
            status       TEXT,
            created_at   TEXT,
            dismissed_at TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS ix_notices_status ON notices(status)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_notices_dedup ON notices(dedup_key)")
    return con


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _day_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def _notice_key(kind: str, dedup_key: str, ts: str) -> str:
    raw = f"{kind}\x1f{dedup_key}\x1f{ts}\x1f{uuid.uuid4().hex}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:20]


def post_notice(kind: str, title: str, body: str, *, severity: str = "info",
                action_hint: str = "", dedup_key: str | None = None,
                ts: str | None = None) -> str | None:
    """Post ONE Action Center notice; returns its notice_key (or None on failure).

    If ``dedup_key`` is given and an OPEN (unread) notice already carries it, that notice is
    UPDATED in place (refreshed title/body/severity/time) and its key is returned — no
    duplicate is stacked. Otherwise a new unread notice is inserted."""
    ts = ts or _now_str()
    day = _day_str()
    try:
        con = _connect()
        try:
            if dedup_key:
                row = con.execute(
                    "SELECT notice_key FROM notices "
                    "WHERE dedup_key = ? AND status = 'unread' LIMIT 1",
                    (dedup_key,),
                ).fetchone()
                if row:
                    con.execute(
                        "UPDATE notices SET ts=?, day=?, severity=?, title=?, body=?, "
                        "action_hint=? WHERE notice_key=?",
                        (ts, day, severity, title, body, action_hint, row[0]),
                    )
                    con.commit()
                    return row[0]
            key = _notice_key(kind, dedup_key or "", ts)
            con.execute(
                "INSERT OR IGNORE INTO notices "
                "(notice_key, ts, day, kind, severity, title, body, action_hint, "
                " dedup_key, status, created_at, dismissed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'unread', ?, NULL)",
                (key, ts, day, kind, severity, title, body, action_hint,
                 dedup_key or "", ts),
            )
            con.commit()
            return key
        finally:
            con.close()
    except Exception:
        return None


def read_notices(*, include_dismissed: bool = False, limit: int = 200) -> list[dict]:
    """Return notices, newest first. Unread only by default; pass include_dismissed=True to
    also get the dismissed history. Degrades to [] on error."""
    try:
        con = _connect()
        try:
            if include_dismissed:
                rows = con.execute(
                    "SELECT notice_key, ts, day, kind, severity, title, body, action_hint, "
                    "status, dismissed_at FROM notices ORDER BY ts DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT notice_key, ts, day, kind, severity, title, body, action_hint, "
                    "status, dismissed_at FROM notices WHERE status = 'unread' "
                    "ORDER BY ts DESC LIMIT ?", (int(limit),),
                ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    return [
        {"notice_key": r[0], "ts": r[1], "day": r[2], "kind": r[3], "severity": r[4],
         "title": r[5], "body": r[6], "action_hint": r[7], "status": r[8],
         "dismissed_at": r[9]}
        for r in rows
    ]


def unread_count() -> int:
    """How many unread notices — the nav badge number. 0 on any error."""
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM notices WHERE status = 'unread'").fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()
    except Exception:
        return 0


def has_open(dedup_key: str) -> bool:
    """Whether an OPEN (unread) notice already carries this dedup_key."""
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT 1 FROM notices WHERE dedup_key = ? AND status = 'unread' LIMIT 1",
                (dedup_key,)).fetchone()
            return row is not None
        finally:
            con.close()
    except Exception:
        return False


def dismiss(notice_key: str) -> bool:
    """Mark one notice dismissed. Returns True if a row changed."""
    try:
        con = _connect()
        try:
            cur = con.execute(
                "UPDATE notices SET status = 'dismissed', dismissed_at = ? "
                "WHERE notice_key = ? AND status = 'unread'",
                (_now_str(), notice_key),
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()
    except Exception:
        return False
