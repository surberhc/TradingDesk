"""deskfeeds.py — pure, cached, READ-ONLY data helpers for the Feeds page.

Answers one owner question in plain English: "is my data correct, coming in, and
being logged?" Nothing here places, arms, or transmits an order, and nothing
writes to any store or config. Gateway checks are cheap TCP port probes only (the
same socket pattern app.py / deskdata use) — never an ib_async connection. The
recorded-tick reader mirrors app.py's overlay: an EPHEMERAL in-memory DuckDB over
the newest parquet parts only, never opening the on-disk catalog and never
scanning the tens of thousands of tiny part files. Every reader degrades to an
honest, safe result rather than raising.

PLAIN-ENGLISH RULE (#1): user-facing strings are full phrases; tier is color only.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

import deskdata as dd

CT_ZONE = ZoneInfo("America/Chicago")
S8_ROOT_FALLBACK = Path(r"C:\TradingDesk-Local\s8_pilot")

# The tick store writes a fresh ~50-row parquet part per buffer flush (tens of
# thousands of tiny files per session). We only ever read the newest handful, so
# the newest recorded quote and "is it logging right now?" are cheap to answer.
_RECENT_PARTS_TO_READ = 6
_LOGGING_FRESH_SECS = 90            # newest part written within this -> logging now
_ROWS_PER_PART_ESTIMATE = 50       # parts are ~50 rows; used only for an estimate


def _ct_now() -> datetime:
    return datetime.now(tz=CT_ZONE)


def _today_ct() -> str:
    return _ct_now().strftime("%Y%m%d")


def _ticks_root() -> Path:
    """s8_pilot root via the shared store if importable, else the known path."""
    try:
        import s8_store
        return Path(s8_store.get_root())
    except Exception:
        return S8_ROOT_FALLBACK


def _age_phrase(secs: float | None) -> str:
    if secs is None:
        return "time unknown"
    secs = int(secs)
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs} second{'s' if secs != 1 else ''} ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    hours = mins // 60
    rem = mins % 60
    if rem == 0:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return f"{hours} hour{'s' if hours != 1 else ''} {rem} minutes ago"


def _clock_ct(dt: datetime) -> str:
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour12}:{dt.minute:02d}:{dt.second:02d} {ampm} Central"


# --------------------------------------------------------------------------- #
# 1. S8 recorded-tick (live option-quote) feed status.                         #
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=15)
def s8_tick_feed_status() -> dict:
    """Is the pilot recording live option quotes to disk right now, and when was
    the last one? Reads ONLY the newest few parquet parts for today (never the
    whole day, never the catalog). Degrades to a safe empty result on any failure.

    Returns keys:
      has_data (bool), newest_dt (datetime|None), newest_phrase (str),
      seconds_since (int|None), age_phrase (str), files_today (int),
      records_estimate (int), logging_now (bool), logging_phrase (str),
      tier (str), phrase (str).
    """
    empty = {
        "has_data": False, "newest_dt": None,
        "newest_phrase": "no quote recorded yet today",
        "seconds_since": None, "age_phrase": "",
        "files_today": 0, "records_estimate": 0,
        "logging_now": False, "logging_phrase": "no",
        "tier": "unknown",
        "phrase": ("No live option quotes have been recorded yet today "
                   "(this is normal before the pilot session opens)."),
    }

    today = _today_ct()
    ticks_dir = _ticks_root() / "ticks" / f"date={today}"
    try:
        parts = [(e.path, e.stat().st_mtime) for e in os.scandir(ticks_dir)
                 if e.name.endswith(".parquet")]
    except (FileNotFoundError, NotADirectoryError, OSError):
        return empty
    if not parts:
        return empty

    files_today = len(parts)
    records_estimate = files_today * _ROWS_PER_PART_ESTIMATE
    newest_mtime = max(m for _, m in parts)
    # "Logging right now?" is answered by the newest part's write time — cheap.
    logging_now = (datetime.now().timestamp() - newest_mtime) <= _LOGGING_FRESH_SECS

    # Newest recorded quote timestamp: read the max ts over only the newest parts.
    recent = [p for p, _m in sorted(parts, key=lambda x: x[1], reverse=True)
              ][:_RECENT_PARTS_TO_READ]
    newest_dt: datetime | None = None
    try:
        import duckdb
        con = duckdb.connect(":memory:")
        try:
            files_sql = ",".join(
                "'" + p.replace("\\", "/").replace("'", "''") + "'" for p in recent)
            res = con.execute(
                f"SELECT max(ts) FROM read_parquet([{files_sql}], "
                "union_by_name=true)").fetchone()
        finally:
            con.close()
        raw = res[0] if res else None
        if raw is not None:
            newest_dt = _coerce_ts(raw)
    except Exception:
        newest_dt = None

    seconds_since = None
    newest_phrase = "recorded, but its time could not be read"
    age_phrase = ""
    if newest_dt is not None:
        newest_phrase = _clock_ct(newest_dt)
        try:
            ref = _ct_now()
            nd = newest_dt if newest_dt.tzinfo else newest_dt.replace(tzinfo=CT_ZONE)
            seconds_since = int((ref - nd).total_seconds())
            age_phrase = _age_phrase(seconds_since)
        except Exception:
            seconds_since = None

    if logging_now:
        tier = "good"
        phrase = (
            f"Recording live option quotes to disk right now — last one at "
            f"{newest_phrase}"
            + (f" ({age_phrase})" if age_phrase else "") + "."
        )
        logging_word = "yes — quotes are being written to disk right now"
    else:
        # Has data but the newest part is older than the freshness window.
        market = dd._is_market_hours(_ct_now())
        tier = "warn" if market else "unknown"
        if market:
            phrase = (
                "Live option-quote recording has paused during market hours — the "
                f"most recent quote was at {newest_phrase}"
                + (f" ({age_phrase})" if age_phrase else "") + "."
            )
        else:
            phrase = (
                "Live option-quote recording is not running now (expected outside "
                f"the pilot session) — the last quote today was at {newest_phrase}"
                + (f" ({age_phrase})" if age_phrase else "") + "."
            )
        logging_word = "no — nothing is being written to disk at the moment"

    return {
        "has_data": True, "newest_dt": newest_dt, "newest_phrase": newest_phrase,
        "seconds_since": seconds_since, "age_phrase": age_phrase,
        "files_today": files_today, "records_estimate": records_estimate,
        "logging_now": logging_now, "logging_phrase": logging_word,
        "tier": tier, "phrase": phrase,
    }


def _coerce_ts(raw) -> datetime | None:
    """Best-effort: a DuckDB max(ts) result (datetime or ISO string) -> datetime."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 2. End-of-day data feeds (reuse the freshness layer).                        #
# --------------------------------------------------------------------------- #
def eod_feed_status() -> list[dict]:
    """The nightly end-of-day feeds with a plain freshness phrase + tier. Reuses
    deskdata.data_freshness() (the same status JSONs the Pulse page reads)."""
    try:
        return dd.data_freshness()
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# 3. Gateway focus — the LIVE trading gateway (4003) gets prominence.          #
# --------------------------------------------------------------------------- #
def gateway_feed_focus() -> dict:
    """Cheap TCP probes of the live-trading gateway (4003) and the market-data
    gateway (4001), each as a plain phrase. The live-trading gateway (the future
    real-trading platform) is the headline. TCP probe only — no session opened."""
    now = _ct_now()
    market = dd._is_market_hours(now)
    weekend = dd._is_weekend(now)

    live_up = dd._port_open("127.0.0.1", 4003)
    if live_up:
        live = {
            "port": 4003, "up": True, "tier": "good",
            "headline": "Connected",
            "phrase": ("The live trading gateway is connected and responding — "
                       "this is the real-account gateway the desk will trade "
                       "through in the future."),
        }
    else:
        if weekend:
            reason = ("not up right now (expected — the pilot session is closed on "
                      "weekends).")
        elif market:
            reason = ("not up right now, during market hours — if the pilot should "
                      "be running, this needs a look (often a morning login still "
                      "waiting for approval).")
        else:
            reason = ("not up right now (expected — the pilot session opens in the "
                      "morning and closes in the afternoon).")
        live = {
            "port": 4003, "up": False,
            "tier": "warn" if market else "unknown",
            "headline": "Not connected",
            "phrase": "The live trading gateway is " + reason,
        }

    data_up = dd._port_open("127.0.0.1", 4001)
    if data_up:
        data = {
            "port": 4001, "up": True, "tier": "good",
            "headline": "Connected",
            "phrase": ("The market-data gateway is connected and responding — it "
                       "feeds the evening end-of-day data pulls."),
        }
    else:
        data = {
            "port": 4001, "up": False, "tier": "unknown",
            "headline": "Not connected",
            "phrase": ("The market-data gateway is not up right now — it is normally "
                       "brought up on its own before the evening data pull, so this "
                       "is expected during the day."),
        }

    return {"live_trade": live, "market_data": data,
            "market_hours": market, "weekend": weekend}
