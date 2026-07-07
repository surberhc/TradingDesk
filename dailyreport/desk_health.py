"""
desk_health.py — small shared computations used by BOTH the Streamlit dashboard
(dashboard/app.py) and the nightly EOD email (dailyreport/eod_report.py).

A sibling to status.py: dependency-light (stdlib + pandas), pure/read-only,
no Streamlit/mailer imports. Each caller does its own presentation formatting
on top of these shared results — this module only computes.

Contents:
  - edgar_coverage(edgar_dir, fundamentals_path, stale_days)  — EDGAR fundamentals
    freshness/coverage walk (was duplicated in app.py::edgar_coverage() and
    eod_report.py::build_edgar()).
  - fmt_magnitude(value, ...)  — shared $/B/M/K magnitude formatter (was
    app.py::_fmt_big() and eod_report.py::_gex_fmt_mag()).
  - GAMMA_STATE_TIER  — gamma_state ("Positive"/"Neutral"/"Negative") -> tier
    ("ok"/"info"/"warn") mapping. Negative gamma is a market-condition/awareness
    signal, not a pipeline failure, so it maps to "warn", not "bad".
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


def edgar_coverage(edgar_dir: Path, fundamentals_path: Path,
                    stale_days: int = 45) -> dict:
    """Walk the EDGAR point-in-time fundamentals warehouse dir and compute its
    freshness/coverage. Pure computation, no presentation formatting — callers
    shape this into Streamlit tiles or an email section dict.

    Returns a plain dict:
      {
        "table_present": bool,
        "dir_present": bool,
        "n_files": int,
        "size_bytes": int,
        "newest_file": str | None,
        "newest_mtime": float,           # 0.0 if none
        "recent_activity": bool,         # newest file touched in the last 15 min
        "n_companies": int | None,
        "refresh_dt": datetime | None,
        "age_days": int | None,
        "tier": "ok" | "info" | "warn",
        "state": "fresh" | "stale" | "refresh in progress" | "build landing" | "not present",
      }

    tier/state logic (identical to the prior duplicated versions):
      recent_activity          -> tier "info", state "refresh in progress"
      not table_present        -> tier "info", state "build landing"
      age_days > stale_days    -> tier "warn", state "stale"
      else                     -> tier "ok",   state "fresh"
    A missing warehouse dir entirely is its own state "not present" (tier "info").
    Never raises.
    """
    if not edgar_dir.exists():
        return {
            "table_present": False, "dir_present": False, "n_files": 0,
            "size_bytes": 0, "newest_file": None, "newest_mtime": 0.0,
            "recent_activity": False, "n_companies": None, "refresh_dt": None,
            "age_days": None, "tier": "info", "state": "not present",
        }

    size = 0
    newest_mtime = 0.0
    newest_name = None
    n_files = 0
    for f in edgar_dir.rglob("*"):
        if not f.is_file():
            continue
        try:
            stt = f.stat()
        except OSError:
            continue
        n_files += 1
        size += stt.st_size
        if stt.st_mtime > newest_mtime:
            newest_mtime = stt.st_mtime
            newest_name = f.name

    now = datetime.now()
    recent_activity = bool(newest_mtime) and (now.timestamp() - newest_mtime) < 15 * 60

    n_companies = None
    table_mtime = None
    table_present = fundamentals_path.exists()
    if table_present:
        try:
            table_mtime = datetime.fromtimestamp(fundamentals_path.stat().st_mtime)
        except OSError:
            table_mtime = None
        try:
            import pandas as pd
            cols = pd.read_parquet(fundamentals_path, columns=None).columns
            key = next((c for c in ("ticker", "cik", "symbol") if c in cols), None)
            if key is not None:
                n_companies = int(pd.read_parquet(
                    fundamentals_path, columns=[key])[key].nunique())
        except Exception:
            n_companies = None

    refresh_dt = table_mtime or (datetime.fromtimestamp(newest_mtime)
                                 if newest_mtime else None)
    age_days = (now - refresh_dt).days if refresh_dt else None

    if recent_activity:
        tier, state = "info", "refresh in progress"
    elif not table_present:
        tier, state = "info", "build landing"
    elif age_days is not None and age_days > stale_days:
        tier, state = "warn", "stale"
    else:
        tier, state = "ok", "fresh"

    return {
        "table_present": table_present, "dir_present": True, "n_files": n_files,
        "size_bytes": size, "newest_file": newest_name, "newest_mtime": newest_mtime,
        "recent_activity": recent_activity, "n_companies": n_companies,
        "refresh_dt": refresh_dt, "age_days": age_days,
        "tier": tier, "state": state,
    }


def fmt_magnitude(value, *, dollar_sign: bool = False, show_plus: bool = False,
                  nd: int | None = None) -> str:
    """Shared $/B/M/K magnitude formatter.

    Keyword params let each caller reproduce its exact prior output:
      dashboard (app.py::_fmt_big):     fmt_magnitude(x, nd=2)             -> "1.23B"
      email (eod_report.py::_gex_fmt_mag): fmt_magnitude(x, dollar_sign=True,
                                            show_plus=True, nd=None)       -> "+$1.23B"

    nd=None uses the per-magnitude precision the email formatter used
    (2dp for B, 0dp for M/K/raw); an explicit nd applies uniformly (the
    dashboard formatter's behavior).
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v != v:  # NaN
        return "—"

    a = abs(v)
    sign = ""
    if show_plus:
        sign = "+" if v >= 0 else "−"  # minus sign, matches prior email output
    elif v < 0:
        sign = "-"
    dollar = "$" if dollar_sign else ""

    if nd is not None:
        if a >= 1e9:
            body = f"{a/1e9:,.{nd}f}B"
        elif a >= 1e6:
            body = f"{a/1e6:,.{nd}f}M"
        elif a >= 1e3:
            body = f"{a/1e3:,.{nd}f}K"
        else:
            body = f"{a:,.{nd}f}"
    else:
        if a >= 1e9:
            body = f"{a/1e9:.2f}B"
        elif a >= 1e6:
            body = f"{a/1e6:.0f}M"
        elif a >= 1e3:
            body = f"{a/1e3:.0f}K"
        else:
            body = f"{a:,.0f}"

    return f"{sign}{dollar}{body}"


# gamma_state -> tier. Standardized on the more thought-through mapping (this
# surface treats GEX as situational awareness, not a hard pipeline failure):
# negative gamma is a market-condition signal ("warn"/amber), not "bad"/red.
GAMMA_STATE_TIER = {"Positive": "ok", "Neutral": "info", "Negative": "warn"}
