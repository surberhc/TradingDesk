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

STRUCTURED DETAIL: a notice may carry an optional ``detail_json`` — a JSON blob the page can
expand into a real table (e.g. the per-account out-of-spec list) instead of cramming it into
the body string. Old notices carry NULL and render exactly as before (back-compat).

SNOOZE ("ignore for N days"): dismiss is NOT durable suppression — the next scheduled run of a
poster re-posts a fresh notice, so a daily check keeps re-nagging. ``snooze(dedup_key, days)``
stamps ``snoozed_until = now + days`` on the open notice; while that stamp is in the future the
notice is HIDDEN from the active list/badge, AND the posters call ``is_snoozed(dedup_key)`` and
SKIP posting — that poster-skip is what actually silences the re-nag. When the stamp passes,
the condition re-surfaces automatically on the next run. Un-snooze clears the stamp early.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
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
            notice_key    TEXT UNIQUE,
            ts            TEXT,
            day           TEXT,
            kind          TEXT,
            severity      TEXT,
            title         TEXT,
            body          TEXT,
            action_hint   TEXT,
            dedup_key     TEXT,
            status        TEXT,
            created_at    TEXT,
            dismissed_at  TEXT,
            detail_json   TEXT,
            snoozed_until TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS ix_notices_status ON notices(status)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_notices_dedup ON notices(dedup_key)")
    # Additive migration: older DBs predate detail_json / snoozed_until. Add any missing
    # column so an existing store upgrades cleanly; old rows read NULL (fully back-compat).
    have = {r[1] for r in con.execute("PRAGMA table_info(notices)").fetchall()}
    for col in ("detail_json", "snoozed_until"):
        if col not in have:
            con.execute(f"ALTER TABLE notices ADD COLUMN {col} TEXT")
    con.commit()
    return con


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _day_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def _notice_key(kind: str, dedup_key: str, ts: str) -> str:
    raw = f"{kind}\x1f{dedup_key}\x1f{ts}\x1f{uuid.uuid4().hex}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:20]


def _coerce_detail_json(detail_json) -> str | None:
    """Store detail as a JSON string. Accepts an already-serialized str (stored verbatim) or a
    list/dict (serialized here); None -> NULL. On any serialization error, NULL (never fatal)."""
    if detail_json is None:
        return None
    if isinstance(detail_json, str):
        return detail_json
    try:
        return json.dumps(detail_json, default=str)
    except Exception:
        return None


def post_notice(kind: str, title: str, body: str, *, severity: str = "info",
                action_hint: str = "", dedup_key: str | None = None,
                detail_json=None, ts: str | None = None) -> str | None:
    """Post ONE Action Center notice; returns its notice_key (or None on failure).

    If ``dedup_key`` is given and an OPEN (unread) notice already carries it, that notice is
    UPDATED in place (refreshed title/body/severity/detail/time) and its key is returned — no
    duplicate is stacked. Otherwise a new unread notice is inserted.

    ``detail_json`` (optional) is structured detail the page can expand into a table — pass a
    JSON string or a list/dict (serialized here). Old notices carry NULL and render as before.
    NOTE: a snoozed dedup_key is NOT suppressed here — the POSTER must call ``is_snoozed`` and
    skip; this keeps the store a dumb writer."""
    ts = ts or _now_str()
    day = _day_str()
    detail = _coerce_detail_json(detail_json)
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
                        "action_hint=?, detail_json=? WHERE notice_key=?",
                        (ts, day, severity, title, body, action_hint, detail, row[0]),
                    )
                    con.commit()
                    return row[0]
            key = _notice_key(kind, dedup_key or "", ts)
            con.execute(
                "INSERT OR IGNORE INTO notices "
                "(notice_key, ts, day, kind, severity, title, body, action_hint, "
                " dedup_key, status, created_at, dismissed_at, detail_json) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'unread', ?, NULL, ?)",
                (key, ts, day, kind, severity, title, body, action_hint,
                 dedup_key or "", ts, detail),
            )
            con.commit()
            return key
        finally:
            con.close()
    except Exception:
        return None


_SELECT_COLS = ("notice_key, ts, day, kind, severity, title, body, action_hint, "
                "status, dismissed_at, dedup_key, detail_json, snoozed_until")


def _row_to_dict(r) -> dict:
    return {"notice_key": r[0], "ts": r[1], "day": r[2], "kind": r[3], "severity": r[4],
            "title": r[5], "body": r[6], "action_hint": r[7], "status": r[8],
            "dismissed_at": r[9], "dedup_key": r[10], "detail_json": r[11],
            "snoozed_until": r[12]}


def read_notices(*, include_dismissed: bool = False, include_snoozed: bool = False,
                 limit: int = 200) -> list[dict]:
    """Return notices, newest first. Unread and NOT currently snoozed by default.

    - ``include_dismissed=True`` also returns the dismissed history (and any snoozed rows).
    - ``include_snoozed=True`` keeps unread rows whose snooze is still in the future in the
      active list (used by the page to show the snoozed section).
    Degrades to [] on error."""
    now = _now_str()
    try:
        con = _connect()
        try:
            if include_dismissed:
                rows = con.execute(
                    f"SELECT {_SELECT_COLS} FROM notices ORDER BY ts DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            elif include_snoozed:
                rows = con.execute(
                    f"SELECT {_SELECT_COLS} FROM notices WHERE status = 'unread' "
                    "ORDER BY ts DESC LIMIT ?", (int(limit),),
                ).fetchall()
            else:
                rows = con.execute(
                    f"SELECT {_SELECT_COLS} FROM notices WHERE status = 'unread' "
                    "AND (snoozed_until IS NULL OR snoozed_until <= ?) "
                    "ORDER BY ts DESC LIMIT ?", (now, int(limit)),
                ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    return [_row_to_dict(r) for r in rows]


def read_snoozed(*, limit: int = 200) -> list[dict]:
    """Unread notices currently snoozed (snoozed_until still in the future), newest first —
    the page's 'snoozed / ignored' section so the operator can see and un-snooze them."""
    now = _now_str()
    try:
        con = _connect()
        try:
            rows = con.execute(
                f"SELECT {_SELECT_COLS} FROM notices WHERE status = 'unread' "
                "AND snoozed_until IS NOT NULL AND snoozed_until > ? "
                "ORDER BY ts DESC LIMIT ?", (now, int(limit)),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    return [_row_to_dict(r) for r in rows]


def unread_count() -> int:
    """How many unread, NOT-currently-snoozed notices — the nav badge number. 0 on any error.
    Snoozed items don't light the badge; that's the point of snooze."""
    now = _now_str()
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM notices WHERE status = 'unread' "
                "AND (snoozed_until IS NULL OR snoozed_until <= ?)", (now,)).fetchone()
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


def snooze(dedup_key: str, days: int) -> bool:
    """Ignore a notice-family for ``days`` days: stamp ``snoozed_until = now + days`` on the
    OPEN (unread) notice(s) carrying ``dedup_key``. While that stamp is in the future the
    notice is hidden from the active list/badge and (crucially) the poster skips re-posting via
    ``is_snoozed``. Returns True if a row was stamped. Keeps the notice 'unread' (not
    dismissed) so the existing row simply re-surfaces when the snooze expires."""
    if not dedup_key or int(days) <= 0:
        return False
    until = (datetime.now() + timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    try:
        con = _connect()
        try:
            cur = con.execute(
                "UPDATE notices SET snoozed_until = ? "
                "WHERE dedup_key = ? AND status = 'unread'",
                (until, dedup_key),
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()
    except Exception:
        return False


def unsnooze(dedup_key: str) -> bool:
    """Clear the snooze on an OPEN notice-family early (bring it back to the active list).
    Returns True if a row changed."""
    if not dedup_key:
        return False
    try:
        con = _connect()
        try:
            cur = con.execute(
                "UPDATE notices SET snoozed_until = NULL "
                "WHERE dedup_key = ? AND status = 'unread'",
                (dedup_key,),
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()
    except Exception:
        return False


def is_snoozed(dedup_key: str) -> bool:
    """True iff an OPEN (unread) notice with this ``dedup_key`` is currently snoozed
    (snoozed_until still in the future). The POSTERS call this before posting and SKIP while
    it's True — this is what actually silences the daily re-nag (dismiss alone cannot, because
    the next run re-posts a fresh notice). False on any error (fail-open: better to nag than to
    silently swallow a real condition)."""
    if not dedup_key:
        return False
    now = _now_str()
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT 1 FROM notices WHERE dedup_key = ? AND status = 'unread' "
                "AND snoozed_until IS NOT NULL AND snoozed_until > ? LIMIT 1",
                (dedup_key, now)).fetchone()
            return row is not None
        finally:
            con.close()
    except Exception:
        return False
