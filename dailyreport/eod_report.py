"""
eod_report.py — the end-of-day digest.

Runs LAST in the day (after the forward collector finishes) and emails ONE concise
status email covering every daily activity. It does not re-run anything — it READS
each subsystem's status artifact (the small JSONs in status.py, plus native files
like the supervisor heartbeat and the Tiingo manifest) and renders a section per
job. A job that crashed or never ran shows as ❌/stale rather than taking the
report down.

Trimmed to S0-only sections on 2026-07-07 per Andrew's request — the email had
become cluttered across strategies. Other sections (forward collector, EDGAR,
gamma, system/gateway health, staleness alarm, the old generic Tiingo section)
remain defined in this file, just out of SECTIONS — add them back (or build a
per-strategy digest) when another strategy needs its own reporting.

Run manually any time:  <venv python> eod_report.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import threading
from pathlib import Path

import desk_health
import mailer
import status

# S0 (Adaptive All-Weather Core) reads the same shared-brain data path the
# backtester and paperbot use — see paperbot\strategy_target.py for the
# original pattern. The backtester is a local `src` package, not an installed
# dependency; add its folder to sys.path exactly like strategy_target.py does.
_BACKTESTER = Path(__file__).resolve().parent.parent / "backtester"
if str(_BACKTESTER) not in sys.path:
    sys.path.insert(0, str(_BACKTESTER))

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
LOG = Path(r"C:\TradingDesk-Local\state\dailyreport\eod_report.log")

TODAY = dt.date.today()
TODAY_STR = TODAY.strftime("%Y%m%d")

# Freshness anchor — the most recent session whose EOD data should already exist
# when this nightly report runs (after the close). On a trading day that's today;
# on a weekend/holiday it's the last real session. Every "is this fresh?" check
# below measures against THIS, not the literal calendar day, so holidays and
# weekends no longer false-flag as stale. See connections/market_calendar.py.
try:
    from connections import market_calendar as _mktcal
    _IS_TRADING_TODAY = _mktcal.is_trading_day(TODAY)
    EXPECTED_SESSION = _mktcal.last_trading_day(TODAY)
    _HOLIDAY_TODAY = _mktcal.holiday_name(TODAY)          # None unless full closure
    _EARLY_CLOSE_TODAY = _mktcal.early_close_name(TODAY)  # None unless a 1pm close
    _CAL_ERR = None
except Exception as _e:  # unknown year / import issue -> degrade to a weekday rule, loudly
    _IS_TRADING_TODAY = TODAY.weekday() < 5
    EXPECTED_SESSION = TODAY
    _HOLIDAY_TODAY = None
    _EARLY_CLOSE_TODAY = None
    _CAL_ERR = f"{type(_e).__name__}: {_e}"
EXPECTED_SESSION_STR = EXPECTED_SESSION.strftime("%Y%m%d")


def _is_fresh(date_str) -> bool:
    """A status/heartbeat date (YYYYMMDD string) is fresh if it is from the expected
    session or later. Uses '>=' not '==' so a job that ALSO ran today on a holiday
    (stamping today) still counts, while a genuinely old status still fails."""
    if not date_str:
        return False
    return str(date_str) >= EXPECTED_SESSION_STR

# status -> (dot color, label). Severity order used for the overall headline.
DOT = {"ok": "#22c55e", "info": "#3b82f6", "stale": "#9ca3af",
       "partial": "#fbbf24", "warn": "#f59e0b", "fail": "#ef4444"}
SEVERITY = ["ok", "info", "stale", "partial", "warn", "fail"]


def _log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _sec(key, title, st, headline, rows):
    return {"key": key, "title": title, "status": st, "headline": headline, "rows": rows}


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


# --------------------------------------------------------------------------- #
# S0 (Adaptive All-Weather Core regime engine) sections — added 2026-07-07.
# Both read the shared brain's OWN price loader + regime scorer directly
# (strategies.parts.regime.market_health_score over backtester/src/data_loader's
# frame) rather than running a full run_backtest() — cheap file reads + one
# vectorized score computation, safe to do inline in the nightly email job.
# --------------------------------------------------------------------------- #


def build_s0_regime():
    """S0 (Adaptive All-Weather Core) — today's Market Health Score + regime.

    Computes the score the SAME way the shared strategy brain does (see
    strategies\\parts\\regime.py market_health_score(), the exact function
    strategies.all_weather.AdaptiveAllWeather.warmup() calls) over the SAME
    price/macro data paperbot\\strategy_target.py loads for the live target
    book. This is NOT a second run_backtest() — it is the underlying causal,
    vectorized score computation alone (sub-second), giving TODAY's raw daily
    reading. The regime that actually governs today's portfolio (after the
    confirmation-buffer/dead-zone hysteresis, SPEC §4) is also shown, since a
    raw score can wiggle across a band boundary for a day or two without the
    confirmed regime (and therefore the traded band) actually changing.

    Never raises: any failure (missing data, import issue) degrades to a
    'fail' section rather than taking the whole report down."""
    title = "S0 Regime (Adaptive All-Weather Core)"
    try:
        from src import data_loader
        from strategies import config as s_config
        from strategies.parts import regime as s_regime

        prices = data_loader.load_prices()
        hyg = data_loader.load_prices([s_config.CREDIT_PROXY[0]])[s_config.CREDIT_PROXY[0]]
        denom_t = s_config.CREDIT_PROXY[1]
        credit_denom = (prices[denom_t] if denom_t in prices.columns
                        else data_loader.load_prices([denom_t])[denom_t])
        vix, vix_src = data_loader.load_vix()
        hy_oas, hy_oas_src = data_loader.load_hy_oas()

        score_df = s_regime.market_health_score(
            prices, hyg=hyg, credit_denom=credit_denom, vix=vix, hy_oas=hy_oas)
        confirmed = s_regime.apply_hysteresis(score_df["score"])

        last = score_df.iloc[-1]
        as_of = score_df.index[-1]
        raw_regime = last["regime"]
        confirmed_regime = confirmed.iloc[-1]
        fresh = _is_fresh(as_of.strftime("%Y%m%d"))

        band_lo, band_hi = s_regime.equity_band(confirmed_regime)

        rows = [
            ("Data as-of", as_of.strftime("%Y-%m-%d")),
            ("Score (0-100)", f"{last['score']:.1f}"),
            ("Raw regime (today's score)", raw_regime),
            ("Confirmed regime (governs the book)", confirmed_regime),
            ("Equity band (confirmed regime)", f"{band_lo:.0%}-{band_hi:.0%}"),
            ("Trend component", f"{last['trend']:.2f}"),
            ("Breadth component", f"{last['breadth']:.2f}"),
            ("Stress component", f"{last['stress']:.2f}"),
            ("VIX source", vix_src),
            ("HY OAS source", hy_oas_src),
        ]

        if not fresh:
            st = "stale"
            headline = (f"score {last['score']:.1f} / {raw_regime} as of "
                        f"{as_of.strftime('%Y-%m-%d')}  ⚠ not fresh (expected "
                        f"session {EXPECTED_SESSION_STR})")
        else:
            st = "ok"
            if raw_regime == confirmed_regime:
                headline = (f"score {last['score']:.1f} — {confirmed_regime} "
                            f"(equity band {band_lo:.0%}-{band_hi:.0%})")
            else:
                headline = (f"score {last['score']:.1f} reads {raw_regime} today, "
                            f"but the CONFIRMED/traded regime is still "
                            f"{confirmed_regime} (equity band {band_lo:.0%}-{band_hi:.0%}) "
                            f"— hysteresis hasn't confirmed the move yet")

        status.write("s0_regime", st, metrics={
            "score": float(last["score"]), "raw_regime": raw_regime,
            "confirmed_regime": confirmed_regime, "as_of": as_of.strftime("%Y%m%d"),
        }, message=headline, day=as_of.strftime("%Y%m%d"))

        return _sec("s0_regime", title, st, headline, rows)
    except Exception as e:
        return _sec("s0_regime", title, "fail",
                    f"could not compute S0 regime: {type(e).__name__}: {e}", [])


# S0's required tickers (strategies\config.py): the equity core + 11 sector
# ETFs used for trend/breadth, plus HYG/IEF (credit proxy) — VIX and HY OAS
# are checked separately since they're not per-ticker parquet files.
_S0_TICKERS = ["SPY", "RSP", "XLC", "XLY", "XLP", "XLE", "XLF", "XLV",
              "XLI", "XLB", "XLRE", "XLK", "XLU", "HYG", "IEF"]


def build_s0_data():
    """Tiingo/macro data freshness SCOPED to S0's own inputs only (SPY, RSP, the
    11 sector ETFs, HYG, IEF, plus VIX and HY OAS) — not a generic warehouse-wide
    check (that's build_tiingo, left defined but out of SECTIONS). Reads the
    SAME manifest file strategies\\config.py / backtester\\src\\data_loader.py
    resolve (config.MANIFEST_FILE, the authoritative LOCAL path — NOT the stale
    Drive path build_tiingo's TIINGO_MANIFEST points at, see CLAUDE.md 2026-06-27
    'data moved off Drive'). Never raises."""
    title = "S0 Data Freshness"
    try:
        from strategies import config as s_config

        manifest_path = Path(s_config.MANIFEST_FILE)
        if not manifest_path.exists():
            return _sec("s0_data", title, "fail",
                        f"manifest not found at {manifest_path}", [])
        mani = json.loads(manifest_path.read_text())
        tickers = mani.get("tickers", {})

        rows = []
        problems = []
        oldest_date = None
        for t in _S0_TICKERS:
            info = tickers.get(t)
            if not info:
                problems.append(f"{t} missing from manifest")
                rows.append((t, "MISSING"))
                continue
            last_date = info.get("last_date", "")
            qc = info.get("qc_flags") or []
            fresh = _is_fresh(last_date.replace("-", "")) if last_date else False
            if not fresh:
                problems.append(f"{t} stale (last_date={last_date})")
            if qc:
                problems.append(f"{t} has QC flags: {qc}")
            rows.append((t, f"{last_date}{'  QC:' + str(qc) if qc else ''}"))
            if last_date and (oldest_date is None or last_date < oldest_date):
                oldest_date = last_date

        for name, key in (("VIX", "_vix"), ("HY OAS", "_hy_oas")):
            info = tickers.get(key)
            if not info:
                problems.append(f"{name} missing from manifest")
                rows.append((name, "MISSING"))
                continue
            last_date = info.get("last_date", "")
            qc = info.get("qc_flags") or []
            fresh = _is_fresh(last_date.replace("-", "")) if last_date else False
            if not fresh:
                problems.append(f"{name} stale (last_date={last_date})")
            if qc:
                problems.append(f"{name} has QC flags: {qc}")
            rows.append((name, f"{last_date}  ({info.get('source', '?')})"
                               f"{'  QC:' + str(qc) if qc else ''}"))

        gen = mani.get("generated_at", "")
        pulled_today = gen[:10] >= EXPECTED_SESSION.isoformat() if gen else False
        rows.append(("Manifest generated", gen[:19] if gen else "—"))
        rows.append(("Pulled today", "yes" if pulled_today else "no"))

        if problems:
            st = "warn"
            headline = f"{len(problems)} issue(s): " + "; ".join(problems[:4])
            if len(problems) > 4:
                headline += f" (+{len(problems) - 4} more)"
        else:
            st = "ok"
            headline = (f"S0's {len(_S0_TICKERS)} tickers + VIX/HY OAS all fresh, "
                        f"no QC flags (oldest last_date {oldest_date})")

        status.write("s0_data", st, metrics={
            "n_tickers": len(_S0_TICKERS), "n_problems": len(problems),
            "pulled_today": pulled_today,
        }, message=headline, day=TODAY_STR)

        return _sec("s0_data", title, st, headline, rows)
    except Exception as e:
        return _sec("s0_data", title, "fail",
                    f"could not check S0 data freshness: {type(e).__name__}: {e}", [])


def build_account():
    """Account cash-flow monitor — the propose-only, read-only per-account cycle
    (paperbot\\account_monitor_run.py) that runs ~4:30 PM CT. It writes an
    'account_monitor' status JSON (status.write) on both its success and failure
    paths: metrics={'rc': <int|None>} plus a human message. This section MIRRORS the
    other status-backed builders (read → freshness → ok/stale/fail) and NEVER raises.

    fresh + rc==0 → ok; a non-zero rc / raised cycle → fail (carried in the status
    'status' field); a status from a previous day → stale. Missing entirely → a
    graceful 'not yet reported' line (the monitor may not have run today)."""
    title = "Account Cash-Flow Monitor"
    s = status.read("account_monitor")
    if not s:
        return _sec("account", title, "stale",
                    "No status written yet — did the 4:30 PM monitor cycle run? "
                    "(paperbot\\account_monitor_run.py writes this on every run.)", [])
    fresh = _is_fresh(s.get("date"))
    st = s.get("status", "fail") if fresh else "stale"
    m = s.get("metrics", {})
    rc = m.get("rc")
    rc_label = {0: "0 (clean cycle or clean skip)"}.get(rc, rc)
    # Surface any richer metrics the monitor may add later (deposits/withdrawals
    # detected, buffer %, proposals) without hard-coding a schema it doesn't yet
    # write — anything beyond 'rc' is rendered generically.
    extra = [(k, v) for k, v in m.items() if k != "rc"]
    rows = ([("Run date", s.get("date")),
             ("Return code", rc_label if rc_label is not None else "—")]
            + extra
            + [("Posture", "read-only / propose-only (transmits nothing)"),
               ("Last update", s.get("ts"))])
    headline = s.get("message", "") + ("" if fresh else "  ⚠ status is from a previous day")
    return _sec("account", title, st, headline, rows)


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


# Trimmed to S0-only 2026-07-07 (see module docstring). build_forward, build_edgar,
# build_gex, build_system, build_tiingo, build_alarm are all still
# defined above/below — just not wired in — so they're one line away from coming back.
SECTIONS = [build_s0_regime, build_s0_data, build_account]


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def _overall(sections):
    worst = "ok"
    for s in sections:
        st = s.get("status", "fail")
        idx = SEVERITY.index(st) if st in SEVERITY else len(SEVERITY) - 1
        if idx > SEVERITY.index(worst):
            worst = st if st in SEVERITY else "fail"
    return worst


def _session_banner() -> str:
    """A one-line context banner shown when today isn't a normal full session, so a
    report that looks quiet on a holiday/weekend reads as expected, not broken."""
    exp = EXPECTED_SESSION.strftime("%a %b %d")
    if _CAL_ERR is not None:
        msg = (f"⚠ No verified market calendar for {TODAY.year} — using a weekday-only "
               f"rule (holidays may mis-flag). Update connections/market_calendar.py. [{_CAL_ERR}]")
        bg, fg = "#fef3c7", "#92400e"
    elif _HOLIDAY_TODAY:
        msg = (f"\U0001f3e6 Market holiday today — {_HOLIDAY_TODAY}. No new session; freshness "
               f"is measured against the last session ({exp}).")
        bg, fg = "#e0f2fe", "#075985"
    elif not _IS_TRADING_TODAY:
        msg = (f"\U0001f5d3 Weekend — no session today. Freshness is measured against the "
               f"last session ({exp}).")
        bg, fg = "#e0f2fe", "#075985"
    elif _EARLY_CLOSE_TODAY:
        msg = f"\U0001f550 Early close today (1:00pm ET) — {_EARLY_CLOSE_TODAY}."
        bg, fg = "#e0f2fe", "#075985"
    else:
        return ""
    return (f'<div style="font-size:12px;background:{bg};color:{fg};border-radius:6px;'
            f'padding:7px 10px;margin:6px 0;">{msg}</div>')


def render_html(sections, overall):
    def dot(st):
        return (f'<span style="display:inline-block;width:11px;height:11px;'
                f'border-radius:50%;background:{DOT.get(st, "#9ca3af")};margin-right:8px;"></span>')

    blocks = []
    for s in sections:
        rows_html = "".join(
            f'<tr><td style="padding:2px 14px 2px 0;color:#6b7280;white-space:nowrap;">{k}</td>'
            f'<td style="padding:2px 0;color:#111827;">{"" if v is None else v}</td></tr>'
            for k, v in s["rows"])
        table = (f'<table style="border-collapse:collapse;font-size:13px;margin-top:6px;">'
                 f'{rows_html}</table>') if s["rows"] else ""
        blocks.append(
            f'<div style="border:1px solid #e5e7eb;border-radius:8px;padding:12px 14px;'
            f'margin:10px 0;background:#fff;">'
            f'<div style="font-size:15px;font-weight:600;color:#111827;">'
            f'{dot(s["status"])}{s["title"]}'
            f'<span style="float:right;font-size:11px;text-transform:uppercase;'
            f'letter-spacing:.05em;color:{DOT.get(s["status"], "#9ca3af")};">{s["status"]}</span></div>'
            f'<div style="font-size:13px;color:#374151;margin-top:4px;">{s["headline"]}</div>'
            f'{table}</div>')

    stamp = dt.datetime.now().strftime("%A %b %d, %Y  %I:%M %p")
    return (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
        f'max-width:640px;margin:0 auto;color:#111827;">'
        f'<div style="font-size:18px;font-weight:700;">{dot(overall)}Trading Desk — End of Day</div>'
        f'<div style="font-size:12px;color:#6b7280;margin:2px 0 6px;">{stamp} · '
        f'overall: <b style="color:{DOT.get(overall, "#9ca3af")};text-transform:uppercase;">{overall}</b></div>'
        f'{_session_banner()}'
        f'{"".join(blocks)}'
        f'<div style="font-size:11px;color:#9ca3af;margin-top:10px;">'
        f'Automated end-of-day digest · TradingDesk\\dailyreport\\eod_report.py</div></div>')


def _run_section(build, timeout: float = 30.0):
    """Run one section builder with a hard timeout so no single builder (a slow file
    read, a wedged probe) can stall or crash the whole report. Returns a section dict;
    never raises. A builder that overruns is abandoned (daemon thread) and rendered as
    a 'fail: timed out' section so the email still goes out."""
    result: dict = {}

    def worker():
        try:
            result["sec"] = build()
        except Exception as e:
            result["err"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return _sec(build.__name__, build.__name__, "fail",
                    f"section timed out after {int(timeout)}s (skipped so the report could send)", [])
    if "sec" in result:
        return result["sec"]
    return _sec(build.__name__, build.__name__, "fail",
                f"section error: {result.get('err', 'unknown')}", [])


def main() -> bool:
    """Build + email the EOD digest. Returns whether the email was actually sent
    (so __main__ can exit non-zero -> Task Scheduler shows the run red)."""
    _log(f"=== EOD report {TODAY_STR} start ===")
    sent = False
    try:
        sections = [_run_section(build) for build in SECTIONS]
        overall = _overall(sections)
        html = render_html(sections, overall)
        subject = f"Trading Desk EOD — {TODAY.strftime('%b %d')} — {overall.upper()}"
        sent = mailer.send_html(subject, html)
        _log(f"sections={[s['status'] for s in sections]} overall={overall} "
             f"emailed={'YES' if sent else 'NO'} -> {mailer.recipient()}")
        status.write("eod_report", "ok" if sent else "fail",
                     metrics={"overall": overall, "emailed": sent}, day=TODAY_STR)
    except Exception as e:
        # Last-resort guard: the generator itself failed. Still send SOMETHING and
        # record a fail status so the independent watchdog also alarms.
        import traceback
        tb = traceback.format_exc()
        _log(f"FATAL in main(): {type(e).__name__}: {e}\n{tb}")
        sent = False   # generator failed; the fallback below re-computes this
        try:
            fb_html = (
                f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
                f'max-width:640px;margin:0 auto;color:#111827;">'
                f'<div style="font-size:16px;font-weight:700;color:#ef4444;">'
                f'Trading Desk EOD — generator error</div>'
                f'<div style="font-size:13px;margin-top:8px;">The end-of-day report failed to '
                f'build. Raw error below.</div>'
                f'<pre style="font-size:12px;white-space:pre-wrap;background:#f9fafb;'
                f'border:1px solid #e5e7eb;border-radius:6px;padding:10px;">'
                f'{type(e).__name__}: {e}\n\n{tb}</pre></div>')
            sent = mailer.send_html(f"Trading Desk EOD — {TODAY.strftime('%b %d')} — ERROR", fb_html)
        except Exception as e2:
            _log(f"FATAL fallback email also failed: {type(e2).__name__}: {e2}")
        try:
            status.write("eod_report", "fail",
                         metrics={"overall": "fail", "emailed": sent,
                                  "error": f"{type(e).__name__}: {e}"}, day=TODAY_STR)
        except Exception:
            pass
    _log(f"=== EOD report {TODAY_STR} done (emailed={'YES' if sent else 'NO'}) ===")
    return sent


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
