"""
archive/non_s0_sections.py — dormant EOD email sections for non-S0 strategies/systems.

The nightly EOD email (dailyreport\\eod_report.py) was trimmed to S0 (Adaptive
All-Weather Core) only on 2026-07-08, per Andrew's direction: it's the one strategy
actually live-paper-tested, and the email should read as a phone-concise S0 command
center rather than a cluttered multi-strategy digest. These 7 section builders
(build_forward, build_thetadata, build_edgar, build_tiingo, build_system, build_gex,
build_alarm) — covering the ThetaData forward collector, the one-time ThetaData
warehouse grab, CAN SLIM's EDGAR fundamentals, generic Tiingo status, system/gateway
health, dealer gamma/GEX, and the staleness-alarm watchdog — are dead-but-reversible,
not deleted. They're kept here for when another strategy needs its own EOD reporting;
each is one `SECTIONS.append(build_x)` (plus this import) away from coming back.

Moved out of eod_report.py 2026-07-08, following the same archive-not-delete pattern
used for the retired RRG pipeline (dailyreport/archive/rrg/, commit d6c396f).

These builders were never fully independent of eod_report.py's module-level state
(the freshness anchor, status DOT/severity tables, the _sec()/_is_fresh() helpers,
and desk_health for the GEX/EDGAR shared helpers) — to reinstate a section, import
it from here and call it from eod_report.py, where those shared pieces still live.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

import desk_health

# eod_report.py owns the shared module-level state (freshness anchor, _sec()/
# _is_fresh() helpers, status DOT table) that active S0 sections still use — import
# it as a module rather than duplicating that state here, so this file stays a thin,
# reversible extension of it. The path/warehouse constants below were used ONLY by
# the dormant builders in this file, so they're defined here directly (not pulled
# off eod_report.py, which no longer keeps them alive since nothing else needs them).
_DAILYREPORT = Path(__file__).resolve().parent.parent
if str(_DAILYREPORT) not in sys.path:
    sys.path.insert(0, str(_DAILYREPORT))
import eod_report as _er
import status

# --------------------------------------------------------------------------- #
# Constants used only by the sections below.
# --------------------------------------------------------------------------- #
WAREHOUSE = Path(r"C:\TradingDesk-Local\warehouse")
EDGAR = Path(r"C:\TradingDesk-Local\canslim\edgar")
# The full-market point-in-time fundamentals table (one row per company-quarter).
EDGAR_FUNDAMENTALS = EDGAR / "quarterly_fundamentals.parquet"
# Periodic-refresh threshold: EDGAR is a monthly-ish rebuild, not a daily feed.
EDGAR_STALE_DAYS = 45
RAW_OPTIONS = WAREHOUSE / "raw" / "options"
DERIVED = WAREHOUSE / "derived"
SUPERVISOR_HB = WAREHOUSE / "supervisor_heartbeat.txt"
FORWARD_HB = WAREHOUSE / "forward_heartbeat.txt"
ALARM_RAN_MARKER = WAREHOUSE / "heartbeat_alarm_ran.txt"
TIINGO_MANIFEST = Path(r"C:\Users\andre\My Drive (andrew@surberhc.com)"
                       r"\TradingDesk\backtester\data\_manifest.json")

_sec = _er._sec
_is_fresh = _er._is_fresh
EXPECTED_SESSION = _er.EXPECTED_SESSION


# --------------------------------------------------------------------------- #
# Section builders — each returns a section dict. Never raise.
# --------------------------------------------------------------------------- #
def _parse_forward_heartbeat():
    """Parse warehouse\\forward_heartbeat.txt into a dict, or None if absent/unparseable.

    Two shapes the collector writes (datacollector\\forward_daily.py):
      in-progress: "2026-06-26 23:55:10  20260626  43/50 roots  ok=13 skip=0 empty=0 fail=30"
      complete:    "2026-06-26 23:59:00  20260626  COMPLETE ok=.. skip=.. empty=.. fail=.."
    Returns: {ts, date, done(int|None), total(int|None), complete(bool),
              ok, skip, empty, fail}
    """
    if not FORWARD_HB.exists():
        return None
    try:
        text = FORWARD_HB.read_text().strip()
    except Exception:
        return None
    if not text:
        return None
    line = text.splitlines()[-1].strip()
    import re
    # "YYYY-MM-DD HH:MM:SS" then 8-digit run date
    m_head = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\d{8})", line)
    if not m_head:
        return None
    ts, date = m_head.group(1), m_head.group(2)
    complete = "COMPLETE" in line
    m_prog = re.search(r"(\d+)\s*/\s*(\d+)\s+roots", line)
    done = int(m_prog.group(1)) if m_prog else None
    total = int(m_prog.group(2)) if m_prog else None

    def _int(key):
        mm = re.search(rf"{key}=(\d+)", line)
        return int(mm.group(1)) if mm else None

    return {"ts": ts, "date": date, "done": done, "total": total,
            "complete": complete, "ok": _int("ok"), "skip": _int("skip"),
            "empty": _int("empty"), "fail": _int("fail")}


def _build_forward_from_heartbeat():
    """Fallback section for when forward.json is missing/stale: render from the
    heartbeat the collector writes after every root. Returns a section or None."""
    h = _parse_forward_heartbeat()
    if not h:
        return None
    fresh = _is_fresh(h["date"])
    fail = h.get("fail") or 0
    if not fresh:
        st = "stale"
    elif fail > 0 or not h["complete"]:
        st = "warn"
    else:
        st = "ok"

    if h["done"] is not None and h["total"] is not None:
        progress = f"{h['done']}/{h['total']} roots"
    elif h["complete"]:
        progress = "COMPLETE"
    else:
        progress = None

    parts = []
    if h["complete"]:
        parts.append("collector finished")
    elif progress:
        parts.append(f"partial run reached {progress}")
    else:
        parts.append("collector ran")
    parts.append(f"ok={h.get('ok')} fail={fail}")
    headline = "(from heartbeat — forward.json missing) " + " · ".join(parts)
    if not fresh:
        headline += "  ⚠ heartbeat is from a previous day"

    rows = [("Run date", h["date"]),
            ("Progress", progress or ("COMPLETE" if h["complete"] else "—")),
            ("Roots written", h.get("ok")),
            ("Already had", h.get("skip")),
            ("Empty/holiday", h.get("empty")),
            ("Failed roots", h.get("fail")),
            ("Last update", h["ts"]),
            ("Source", "forward_heartbeat.txt (forward.json absent)")]
    return _sec("forward", "IBKR Forward Collector", st, headline, rows)


def build_forward():
    s = status.read("forward")
    # Prefer forward.json when present AND fresh (it's richer). Otherwise fall back
    # to the heartbeat the collector writes after every root, so a partial/aborted
    # run (which never writes forward.json) still renders a meaningful line instead
    # of reading as "missing/stale".
    if not s or not _is_fresh(s.get("date")):
        hb_sec = _build_forward_from_heartbeat()
        if hb_sec is not None:
            return hb_sec
    if not s:
        return _sec("forward", "Daily Options Grab (ThetaData)", "stale",
                    "No status written — did the 5:30 PM run fire?", [])
    m = s.get("metrics", {})
    fresh = _is_fresh(s.get("date"))
    st = s.get("status", "fail") if fresh else "stale"
    rows = [("Run date", s.get("date")), ("Roots written", m.get("ok")),
            ("Already had", m.get("skip")), ("Empty/holiday", m.get("empty")),
            ("Failed roots", m.get("fail")), ("Real errors", m.get("real_errors")),
            ("Last update", s.get("ts"))]
    headline = s.get("message", "") + ("" if fresh else "  ⚠ status is from a previous day")
    return _sec("forward", "Daily Options Grab (ThetaData)", st, headline, rows)


def build_thetadata():
    files = 0
    size = 0
    if RAW_OPTIONS.exists():
        for root, _dirs, fnames in os.walk(RAW_OPTIONS):
            for fn in fnames:
                if fn.endswith(".parquet"):
                    files += 1
                    try:
                        size += os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        pass
    hb = SUPERVISOR_HB.read_text().strip() if SUPERVISOR_HB.exists() else "(no heartbeat)"
    complete = "COMPLETE" in hb
    st = "ok" if complete else ("info" if files > 0 else "warn")
    headline = "One-time grab COMPLETE" if complete else "One-time grab in progress"
    rows = [("Warehouse files", f"{files:,}"), ("Warehouse size", f"{size/1e9:.2f} GB"),
            ("Supervisor heartbeat", hb)]
    return _sec("thetadata", "ThetaData Warehouse (one-time grab)", st, headline, rows)


def build_edgar():
    """EDGAR point-in-time fundamentals — a PERIODIC (monthly-ish) refresh, monitored
    as freshness/coverage, NOT as a nightly download. Inspects the warehouse dir
    directly (never imports canslim/edgar_pipeline.py). Reports company/partition count
    in the full-market fundamentals table, warehouse size, and last-refresh age.

    Status:
      info  — a build is in progress (recent file activity; table not yet final),
              or the table hasn't landed yet ("build landing").
      ok    — table present and freshly refreshed (last refresh <= EDGAR_STALE_DAYS).
      stale — last refresh older than the periodic threshold (needs a rebuild).
    Never raises."""
    title = "EDGAR Fundamentals (point-in-time)"
    try:
        ec = desk_health.edgar_coverage(EDGAR, EDGAR_FUNDAMENTALS, EDGAR_STALE_DAYS)

        if not ec["dir_present"]:
            return _sec("edgar", title, "info",
                        "info: build landing — EDGAR warehouse dir not present yet.", [])

        companies = ec["n_companies"]
        size = ec["size_bytes"]
        n_files = ec["n_files"]
        table_present = ec["table_present"]
        refresh_dt = ec["refresh_dt"]
        age_days = ec["age_days"]
        newest_name = ec["newest_file"]
        newest_mtime = ec["newest_mtime"]
        newest_str = (dt.datetime.fromtimestamp(newest_mtime).strftime("%Y-%m-%d %H:%M:%S")
                      if newest_mtime else "—")

        rows = [
            ("Companies (partitions)", f"{companies:,}" if companies is not None else "—"),
            ("Warehouse size", f"{size/1e9:.2f} GB"),
            ("Warehouse files", f"{n_files:,}"),
            ("Fundamentals table", "present" if table_present else "not built yet"),
            ("Last refresh", refresh_dt.strftime("%Y-%m-%d %H:%M:%S") if refresh_dt else "—"),
            ("Refresh age", f"{age_days}d" if age_days is not None else "—"),
            ("Newest file", f"{newest_name} @ {newest_str}" if newest_name else "—"),
        ]

        # Map the shared computation's tier/state onto this file's status vocabulary
        # (ok/info/stale/fail) and headline text, unchanged from the prior wording.
        if ec["recent_activity"]:
            st = "info"
            headline = ("info: build/refresh in progress — files updated in the last "
                        f"15 min (newest {newest_name}). Table not final.")
        elif not table_present:
            st = "info"
            headline = "info: build landing — fundamentals table not written yet."
        elif ec["state"] == "stale":
            st = "stale"
            headline = (f"stale — last refresh was {age_days}d ago "
                        f"(> {EDGAR_STALE_DAYS}d periodic threshold). Time to re-pull EDGAR.")
        else:
            st = "ok"
            headline = (f"fresh — {companies:,} companies, "
                        f"last refresh {age_days}d ago." if companies is not None
                        else f"fresh — last refresh {age_days}d ago.")
        return _sec("edgar", title, st, headline, rows)
    except Exception as e:
        # Match the other builders: degrade, never raise.
        return _sec("edgar", title, "info",
                    f"info: build landing — could not read EDGAR warehouse "
                    f"({type(e).__name__}: {e}).", [])


def build_tiingo():
    s = status.read("tiingo")
    if s:
        fresh = _is_fresh(s.get("date"))
        st = s.get("status", "fail") if fresh else "stale"
        m = s.get("metrics", {})
        rows = [("Run date", s.get("date")), ("Tickers", m.get("tickers")),
                ("QC flags", m.get("qc_flags")), ("Data end", m.get("data_end")),
                ("Last update", s.get("ts"))]
        head = s.get("message", "") + ("" if fresh else "  ⚠ status is from a previous day")
        return _sec("tiingo", "Tiingo Data Refresh", st, head, rows)
    # Fallback: read the backtester manifest directly.
    if TIINGO_MANIFEST.exists():
        try:
            mani = json.loads(TIINGO_MANIFEST.read_text())
            gen = mani.get("generated_at", "")
            fresh = gen[:10] >= EXPECTED_SESSION.isoformat()
            st = "ok" if fresh else "stale"
            n = len(mani.get("tickers", {}))
            return _sec("tiingo", "Tiingo Data Refresh", st,
                        f"manifest generated {gen[:19]}",
                        [("Tickers", n), ("Data end", mani.get("data_end"))])
        except Exception:
            pass
    return _sec("tiingo", "Tiingo Data Refresh", "stale",
                "No Tiingo status or manifest found", [])


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Cheap 'is something listening?' TCP probe — milliseconds, no asyncio, no
    trading session. The EOD digest must NEVER open a live ib_async connection just
    to print gateway up/down: under the non-interactive scheduled-task context that
    full asyncio round-trip crashed the whole process before the email sent (silent
    for 5 nights, 2026-06-27..07-01). A port-open check is a safe proxy for 'up'."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_system():
    rows = []
    up = _port_open("127.0.0.1", 4002)
    rows.append(("IB Gateway (paper 4002)", "UP" if up else "DOWN"))
    try:
        _t, _u, free = shutil.disk_usage("C:\\")
        rows.append(("C: free space", f"{free/1e9:.0f} GB"))
    except Exception:
        free = 0
    st = "ok" if (up and free > 5e9) else ("warn" if up else "warn")
    headline = "Gateway up" if up else "Gateway DOWN (forward collector needs it)"
    return _sec("system", "System / Gateway Health", st, headline, rows)


# Dealer-gamma reads the derived GEX tables (features/gex.py output) directly.
# Index first (the validated MSR signal lives on SPX), ETF second for the cash tape.
GEX_INDEX = ["SPX", "SPXW"]   # use whichever derived table exists; SPX preferred
GEX_ETF = "SPY"
# gamma_state -> dot color. Negative gamma is the fragile/high-vol regime.
# Shared with dashboard/app.py — see desk_health.GAMMA_STATE_TIER.
GEX_STATE_DOT = desk_health.GAMMA_STATE_TIER


def _gex_latest(symbol: str):
    """Return the latest day's GEX row (dict) for a symbol, or None if no table yet."""
    import pandas as pd
    path = DERIVED / f"{symbol}_gex_daily.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    # tables are written sorted ascending by date; be defensive and sort anyway.
    df = df.sort_values("date")
    return df.iloc[-1].to_dict()


def _gex_fmt_mag(net_gex):
    """Human $GEX magnitude (per 1% move): $1.2B / $345M / $12K, signed.
    Thin wrapper over the shared formatter — see desk_health.fmt_magnitude."""
    return desk_health.fmt_magnitude(net_gex, dollar_sign=True, show_plus=True)


def _gex_num(v, suffix="", nd=2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f != f:
        return "—"
    return f"{f:,.{nd}f}{suffix}"


def _gex_oneliner(sym, r):
    """A compact spot/state/flip line for a single symbol's row."""
    state = r.get("gamma_state", "?")
    return (f"{sym} {state} · net {_gex_fmt_mag(r.get('net_gex'))} · "
            f"spot {_gex_num(r.get('spot'))} · flip {_gex_num(r.get('gamma_flip'))} "
            f"({_gex_num(r.get('dist_to_flip_pct'), '%', 2)} away) · "
            f"exp move {_gex_num(r.get('expected_move_pct'), '%', 2)}")


def build_gex():
    """Dealer Gamma — latest derived GEX for the index (SPX) + ETF (SPY)."""
    # pick the first index table that exists (SPX preferred over SPXW)
    idx_sym, idx = None, None
    for sym in GEX_INDEX:
        r = _gex_latest(sym)
        if r is not None:
            idx_sym, idx = sym, r
            break
    etf = _gex_latest(GEX_ETF)

    if idx is None and etf is None:
        return _sec("gex", "Dealer Gamma", "info",
                    "No derived GEX tables yet — warehouse build still landing.", [])

    primary = idx if idx is not None else etf
    primary_sym = idx_sym if idx is not None else GEX_ETF
    state = primary.get("gamma_state", "?")
    st = GEX_STATE_DOT.get(state, "info")

    flip_dir = "above" if primary.get("above_flip") else "below"
    headline = (f"{primary_sym} dealer gamma is {state.upper()} "
                f"(spot {flip_dir} the {_gex_num(primary.get('gamma_flip'))} flip)")
    if state == "Negative":
        headline += " — fragile / vol-amplifying regime"
    elif state == "Positive":
        headline += " — pinning / vol-dampening regime"

    rows = []
    if idx is not None:
        rows += [
            (f"{idx_sym} as-of", idx.get("date")),
            (f"{idx_sym} gamma state", idx.get("gamma_state")),
            (f"{idx_sym} net GEX (per 1%)", _gex_fmt_mag(idx.get("net_gex"))),
            (f"{idx_sym} spot", _gex_num(idx.get("spot"))),
            (f"{idx_sym} gamma flip", _gex_num(idx.get("gamma_flip"))),
            (f"{idx_sym} dist to flip", _gex_num(idx.get("dist_to_flip_pct"), "%", 2)),
            (f"{idx_sym} expected move", _gex_num(idx.get("expected_move_pct"), "%", 2)),
            (f"{idx_sym} focal strike", _gex_num(idx.get("focal_strike"), nd=0)),
        ]
    else:
        rows.append(("Index (SPX/SPXW)", "no table yet"))
    if etf is not None:
        rows.append((f"{GEX_ETF} line", _gex_oneliner(GEX_ETF, etf)))
    else:
        rows.append((f"{GEX_ETF} line", "no table yet"))

    # stale if the primary table's last day isn't today's run date.
    if not _is_fresh(primary.get("date")):
        st = "stale" if st in ("ok", "info") else st
        headline += f"  ⚠ latest GEX is {primary.get('date')}, not today"

    return _sec("gex", "Dealer Gamma", st, headline, rows)


def build_alarm():
    """Staleness-alarm watchdog (mutual coverage). The heartbeat_alarm task stamps
    warehouse\\heartbeat_alarm_ran.txt every sweep (~15 min). If that marker is stale
    (>30 min) or missing, the alarm itself may be dead — turn this section red so the
    digest the user reads flags it. This is the reciprocal of the alarm's own
    handle_deadline check on the EOD report."""
    now = dt.datetime.now()
    if not ALARM_RAN_MARKER.exists():
        return _sec("alarm", "Staleness Alarm (watchdog)", "fail",
                    "Staleness alarm has never run — heartbeat_alarm_ran.txt is missing. "
                    "The watchdog itself may be dead (task HeartbeatStalenessAlarm).", [])
    try:
        mtime = dt.datetime.fromtimestamp(ALARM_RAN_MARKER.stat().st_mtime)
        age_min = (now - mtime).total_seconds() / 60.0
        last_line = ALARM_RAN_MARKER.read_text().strip().splitlines()[-1] if \
            ALARM_RAN_MARKER.read_text().strip() else "(empty)"
    except Exception as e:
        return _sec("alarm", "Staleness Alarm (watchdog)", "fail",
                    f"Could not read the alarm marker: {type(e).__name__}: {e}", [])

    rows = [("Last ran", mtime.strftime("%Y-%m-%d %H:%M:%S")),
            ("Age", f"{int(age_min)}m"),
            ("Marker", str(ALARM_RAN_MARKER)),
            ("Owning task", "HeartbeatStalenessAlarm")]
    if age_min > 30:
        return _sec("alarm", "Staleness Alarm (watchdog)", "fail",
                    f"Staleness alarm has not run in {int(age_min)}m — the watchdog "
                    f"itself may be dead. Check task HeartbeatStalenessAlarm.", rows)
    return _sec("alarm", "Staleness Alarm (watchdog)", "ok",
                f"Alarm is alive — last ran {int(age_min)}m ago ({last_line}).", rows)
