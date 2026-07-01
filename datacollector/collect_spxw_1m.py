r"""
collect_spxw_1m.py — bulletproof, resumable collector for SPXW 1-minute option data.

Pulls 1-minute NBBO QUOTE and 1-minute OHLC bars for every SPXW contract, one
trading day at a time, from the LOCAL ThetaData v3 Terminal. Window:
2022-01-01 -> today (~880 trading days).

Why two endpoints, pulled differently (MEASURED, do not re-derive):
  * QUOTE  /option/history/quote  ACCEPTS expiration=*  -> ONE call/day
           (~47s, ~690 MB CSV/day, ~7.85M rows).
  * OHLC   /option/history/ohlc   REJECTS  expiration=*  -> must LOOP per
           expiration (~41 expirations/day, ~4-5s each). The day's expiration
           list comes from /option/history/greeks/eod (expiration=*).
  CRITICAL: v3 requires DASHED dates (start_date=2026-06-25), NOT 20260625.
  Valid interval token = "1m".

Output tree (NEW, never collides with the EOD warehouse):
    C:\TradingDesk-Local\warehouse\raw\options_1m\SPXW\quote\{YYYYMMDD}.parquet
    C:\TradingDesk-Local\warehouse\raw\options_1m\SPXW\ohlc\{YYYYMMDD}.parquet
  zstd-compressed, written atomically (temp file + os.replace).

SMART (LOSSLESS) STORAGE — read this before consuming the parquet files:
  The terminal returns a DENSE grid (every contract x all ~391 trading minutes).
  ~95% of those rows carry no information. We apply two LOSSLESS filters in memory
  AFTER each day's pull and BEFORE writing, so the files are ~20x smaller but every
  original value is exactly recoverable:

  1) OHLC = TRADE BARS ONLY. We KEEP only minutes that actually traded
     (volume > 0). No-trade minutes (volume==0, which here always coincides with
     count==0 and open/high/low/close==0) are DROPPED. They are intentionally
     ABSENT from the file. A minute missing from the OHLC file means "no trade in
     that minute" — do NOT treat the gap as missing/bad data. Do NOT forward-fill
     OHLC (a trade bar is point-in-time, not a state that persists).

  2) QUOTE (NBBO) = STORE-ON-CHANGE. Per contract
     (symbol, expiration, strike, right), rows are sorted by timestamp and a row is
     KEPT only when (bid, ask, bid_size, ask_size) differs from the previously kept
     row. Each contract's FIRST row of the day is ALWAYS kept as the baseline.
     RECONSTRUCTION: to get the NBBO for ANY minute, take the most recent kept row
     at or before that minute, i.e. FORWARD-FILL the last kept quote within each
     contract. A contract's quote is undefined before its first kept timestamp.
     Only (bid, ask, bid_size, ask_size) participate in the change test; the
     ancillary columns (bid_exchange, bid_condition, ask_exchange, ask_condition)
     ride along on the kept rows and are likewise valid until the next kept row.

Bulletproof / resumable contract:
  * A day is DONE only when BOTH its quote AND ohlc files exist AND are non-empty.
    Skip done days. A re-launch continues EXACTLY where it stopped.
  * Order: newest day -> oldest (recent 0DTE data lands first / usable soonest).
  * Per-day try/except + retry: one bad day/expiration never aborts the run. A
    failed day is logged and left un-done, so a later pass retries it.
  * Heartbeat JSON after each day (the monitor reads this) + an append-only log.

This module is driven (watchdogged / restarted) by spxw_1m_supervisor.py, but it
is fully runnable on its own and is the unit of resumability.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys
import time

import pandas as pd
import requests

import config

# --------------------------------------------------------------------------- #
# Paths — a NEW tree so nothing collides with the EOD warehouse.
# --------------------------------------------------------------------------- #
ROOT_1M = config.DATA_ROOT / "raw" / "options_1m" / "SPXW"
QUOTE_DIR = ROOT_1M / "quote"
OHLC_DIR = ROOT_1M / "ohlc"
PROGRESS = config.DATA_ROOT / "spxw_1m_progress.json"
LOG = config.DATA_ROOT / "spxw_1m.log"

SYMBOL = "SPXW"
INTERVAL = "1m"

# Window. Newest-first iteration is built in main().
START_DAY = dt.date(2022, 1, 1)

# Timeouts (generous — the terminal is also serving a GEX rebuild + another pull).
QUOTE_TIMEOUT = 300       # the big single-call QUOTE day-pull
LIST_TIMEOUT = 180        # greeks/eod expiration-list call
OHLC_TIMEOUT = 120        # per-expiration OHLC call
HTTP_RETRIES = 5          # per-call retry inside _get_csv


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
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


# --------------------------------------------------------------------------- #
# Low-level HTTP -> DataFrame (own retry; tolerant of a slow/busy terminal)
# --------------------------------------------------------------------------- #
def _get_csv(path: str, params: dict, timeout: int) -> pd.DataFrame:
    """GET a CSV endpoint on the local Terminal -> DataFrame, with retry/backoff.

    Returns an EMPTY DataFrame for a valid no-data response (472/404 or blank
    body). Raises RuntimeError only after exhausting retries on a real failure.
    """
    url = f"{config.THETA_BASE_URL}{path}"
    params = {**params, "format": "csv"}
    last = None
    for attempt in range(HTTP_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                if not r.text.strip():
                    return pd.DataFrame()
                return pd.read_csv(io.StringIO(r.text))
            if r.status_code in (472, 404):
                return pd.DataFrame()
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            last = repr(e)
        # exponential-ish backoff, capped — the terminal may be busy
        time.sleep(min(5 * (attempt + 1), 30))
    raise RuntimeError(f"GET {url} failed after {HTTP_RETRIES} tries: {last}")


def connected() -> bool:
    """Is the local Terminal answering at all? (Any HTTP reply counts.)"""
    try:
        requests.get(f"{config.THETA_BASE_URL}/option/history/quote",
                     params={"symbol": "X"}, timeout=5)
        return True
    except requests.RequestException:
        return False


def wait_for_terminal(max_wait: int = 600) -> bool:
    """Block until the terminal answers (or give up after max_wait seconds)."""
    waited = 0
    while waited < max_wait:
        if connected():
            return True
        log(f"terminal not answering — waiting (waited {waited}s)")
        time.sleep(15)
        waited += 15
    return connected()


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #
def daystr(d: dt.date) -> str:
    """YYYYMMDD — used for filenames (matches the EOD warehouse convention)."""
    return d.strftime("%Y%m%d")


def dashed(d: dt.date) -> str:
    """YYYY-MM-DD — the v3 API REQUIRES dashed dates."""
    return d.strftime("%Y-%m-%d")


def trading_days_newest_first(start: dt.date, end: dt.date) -> list[dt.date]:
    """Mon-Fri calendar days, newest -> oldest. Holidays aren't excluded here;
    a holiday simply returns no data and is handled as a (legit) empty day."""
    days: list[dt.date] = []
    d = end
    while d >= start:
        if d.weekday() < 5:        # 0=Mon .. 4=Fri
            days.append(d)
        d -= dt.timedelta(days=1)
    return days


# --------------------------------------------------------------------------- #
# Atomic parquet write (mirrors storage.write_day)
# --------------------------------------------------------------------------- #
def _write_atomic(path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, engine="pyarrow", compression="zstd", index=False)
    os.replace(tmp, path)


def _nonempty_file(path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def day_done(d: dt.date) -> bool:
    """A day is DONE only when BOTH quote and ohlc files exist and are non-empty.

    This is the heart of resumability/idempotency: a re-launch skips done days
    and resumes exactly where it stopped. A half-written day (only quote, or a
    0-byte file) is NOT done and gets retried.
    """
    return (_nonempty_file(QUOTE_DIR / f"{daystr(d)}.parquet")
            and _nonempty_file(OHLC_DIR / f"{daystr(d)}.parquet"))


# --------------------------------------------------------------------------- #
# Per-endpoint pulls
# --------------------------------------------------------------------------- #
def pull_quote(d: dt.date) -> pd.DataFrame:
    """1-min NBBO for ALL SPXW contracts that day — ONE call (expiration=*)."""
    return _get_csv("/option/history/quote", {
        "symbol": SYMBOL, "expiration": "*",
        "start_date": dashed(d), "end_date": dashed(d),
        "strike": "*", "right": "both", "interval": INTERVAL,
    }, timeout=QUOTE_TIMEOUT)


def day_expirations(d: dt.date) -> list[str]:
    """The day's live expirations (dashed strings), from greeks/eod expiration=*.

    OHLC rejects expiration=*, so we need the explicit list to loop. greeks/eod
    is the cheapest source that returns every expiration trading that day.
    """
    df = _get_csv("/option/history/greeks/eod", {
        "symbol": SYMBOL, "expiration": "*",
        "start_date": dashed(d), "end_date": dashed(d),
        "strike": "*", "right": "both",
        "rate_type": config.THETA_RATE_TYPE,
    }, timeout=LIST_TIMEOUT)
    if df.empty or "expiration" not in df.columns:
        return []
    exps = sorted({str(x) for x in df["expiration"].dropna().unique()})
    return exps


def pull_ohlc(d: dt.date, expirations: list[str]) -> pd.DataFrame:
    """1-min OHLC bars for the day — LOOP per expiration, concatenate.

    A single bad expiration is logged and skipped (it does not abort the day);
    the day is only marked done when its file lands, and an empty result for a
    given expiration is simply contributed as nothing.
    """
    frames: list[pd.DataFrame] = []
    failed: list[str] = []
    for exp in expirations:
        try:
            part = _get_csv("/option/history/ohlc", {
                "symbol": SYMBOL, "expiration": exp,
                "start_date": dashed(d), "end_date": dashed(d),
                "strike": "*", "right": "both", "interval": INTERVAL,
            }, timeout=OHLC_TIMEOUT)
            if not part.empty:
                frames.append(part)
        except Exception as e:          # noqa: BLE001 — one expiration must not kill the day
            failed.append(exp)
            log(f"  OHLC expiration {exp} failed: {e!r} (skipping this exp)")
    if failed:
        # Raise so the day is left un-done and retried on a later pass — we do NOT
        # want to persist a partial OHLC file as if it were complete.
        raise RuntimeError(f"{len(failed)} OHLC expirations failed: {failed[:5]}...")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------- #
# SMART (LOSSLESS) STORAGE FILTERS — applied in memory after the pull, before
# the write. See the module docstring for the full reconstruction contract.
# --------------------------------------------------------------------------- #
CONTRACT_KEYS = ["symbol", "expiration", "strike", "right"]
QUOTE_CHANGE_COLS = ["bid", "ask", "bid_size", "ask_size"]


def filter_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """Drop no-trade bars. LOSSLESS: a no-trade minute carries no information.

    Keep only minutes that actually traded (volume > 0). Empty == no trade, so a
    minute absent from the file simply means no trade happened then. Robust to a
    missing 'volume' column (then nothing is dropped).
    """
    if df.empty or "volume" not in df.columns:
        return df
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    return df[vol > 0].reset_index(drop=True)


def filter_quote_on_change(df: pd.DataFrame) -> pd.DataFrame:
    """STORE-ON-CHANGE per contract — LOSSLESS via forward-fill on read.

    Per contract (symbol, expiration, strike, right), sort by timestamp and keep a
    row only when (bid, ask, bid_size, ask_size) differs from the previously kept
    row. The first row of each contract's day is ALWAYS kept (baseline). Any minute
    is reconstructed by forward-filling the last kept quote within the contract.

    Vectorized: a single stable sort + a per-group "any-of-4-changed" mask via
    groupby().shift(), so it is fast over millions of rows (no Python per-row loop).
    """
    if df.empty:
        return df
    cols = CONTRACT_KEYS + QUOTE_CHANGE_COLS
    if any(c not in df.columns for c in cols + ["timestamp"]):
        return df

    # Stable sort by contract then timestamp so "previous kept row" is well-defined
    # and the original arrival order is preserved within identical timestamps.
    df = df.sort_values(CONTRACT_KEYS + ["timestamp"],
                        kind="stable").reset_index(drop=True)

    g = df.groupby(CONTRACT_KEYS, sort=False)
    # First row of each contract -> always keep (its shifted "prev" is NaN).
    prev = g[QUOTE_CHANGE_COLS].shift(1)
    is_first = prev[QUOTE_CHANGE_COLS[0]].isna()
    # Changed if ANY of the 4 fields differs from the previous kept row.
    # (NaN-safe: treat NaN!=NaN as unchanged so all-NaN runs collapse correctly.)
    changed = pd.Series(False, index=df.index)
    for c in QUOTE_CHANGE_COLS:
        a, b = df[c], prev[c]
        diff = (a != b) & ~(a.isna() & b.isna())
        changed = changed | diff
    keep = is_first | changed
    return df[keep].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Heartbeat
# --------------------------------------------------------------------------- #
def _dir_gb(root) -> float:
    total = 0
    try:
        for f in root.rglob("*.parquet"):
            try:
                total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return round(total / (1024 ** 3), 3)


def write_progress(days_done: int, days_total: int, current_day: str,
                   rows_last_day: int, errors_count: int, last_error: str,
                   avg_sec_per_day: float) -> None:
    remaining = max(days_total - days_done, 0)
    eta_secs = remaining * avg_sec_per_day if avg_sec_per_day > 0 else 0
    eta_str = ""
    if eta_secs > 0:
        finish = dt.datetime.now() + dt.timedelta(seconds=eta_secs)
        eta_str = (f"{eta_secs / 3600:.1f}h remaining "
                   f"(~{finish:%Y-%m-%d %H:%M})")
    payload = {
        "updated": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        "days_done": days_done,
        "days_total": days_total,
        "pct": round(100.0 * days_done / days_total, 2) if days_total else 0.0,
        "current_day": current_day,
        "rows_last_day": rows_last_day,
        "gb_on_disk_so_far": _dir_gb(ROOT_1M),
        "errors_count": errors_count,
        "last_error": last_error,
        "eta": eta_str,
        "avg_sec_per_day": round(avg_sec_per_day, 1),
    }
    tmp = PROGRESS.with_name(PROGRESS.name + ".tmp")
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, PROGRESS)


# --------------------------------------------------------------------------- #
# Per-day worker
# --------------------------------------------------------------------------- #
def collect_day(d: dt.date) -> tuple[bool, int, str]:
    """Collect one day. Returns (ok, rows_quote, note).

    Writes quote first, then ohlc; the day is only DONE when both land. If the
    day legitimately has no data (holiday / pre-listing), it is treated as done-
    skippable by writing nothing and returning ok with a note — but to keep
    day_done() simple (it requires non-empty files), a truly empty day is left
    un-done. SPXW trades every weekday in-window, so empty == holiday; those are
    rare and harmlessly retried (they stay fast: empty calls).
    """
    ds = daystr(d)
    t0 = time.time()

    # 1) QUOTE — single expiration=* call.
    q = pull_quote(d)
    if q.empty:
        return False, 0, "no quote data (holiday/pre-listing) — left un-done"

    # 2) OHLC — need expiration list, then loop.
    exps = day_expirations(d)
    if not exps:
        return False, len(q), "no expiration list — left un-done"
    o = pull_ohlc(d, exps)
    if o.empty:
        return False, len(q), "no ohlc data — left un-done"

    # 2b) SMART LOSSLESS FILTERS — shrink in memory before the write.
    #     OHLC: keep only traded minutes. QUOTE: store-on-change per contract.
    #     (Both fully recoverable — see module docstring.) Guard against a filter
    #     emptying everything (would otherwise write a 0-row file and mark the day
    #     done) — that should never happen on a real trading day.
    q_dense, o_dense = len(q), len(o)
    q = filter_quote_on_change(q)
    o = filter_ohlc(o)
    if q.empty or o.empty:
        return False, q_dense, ("filter emptied quote or ohlc — left un-done "
                                f"(quote {q_dense}->{len(q)}, ohlc {o_dense}->{len(o)})")

    # 3) Write both atomically. Write ohlc first, then quote LAST, so that if a
    #    crash lands between the two writes the day is still not "done" (quote is
    #    the second/final marker) and will be cleanly retried.
    _write_atomic(OHLC_DIR / f"{ds}.parquet", o)
    _write_atomic(QUOTE_DIR / f"{ds}.parquet", q)

    secs = time.time() - t0
    return True, len(q), (
        f"quote={len(q):,} rows (dense {q_dense:,}, "
        f"{100.0 * len(q) / q_dense:.1f}% kept), "
        f"ohlc={len(o):,} rows (dense {o_dense:,}, "
        f"{100.0 * len(o) / o_dense:.1f}% kept), "
        f"{len(exps)} exps, {secs:.0f}s")


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="SPXW 1-minute option collector")
    ap.add_argument("--start", default=None,
                    help="YYYY-MM-DD start (oldest) day; default 2022-01-01")
    ap.add_argument("--end", default=None,
                    help="YYYY-MM-DD end (newest) day; default today")
    ap.add_argument("--max-days", type=int, default=0,
                    help="stop after collecting this many NEW days (0 = no limit; "
                         "for testing)")
    ap.add_argument("--progress", default=None,
                    help="override path for the progress/heartbeat JSON; default "
                         "spxw_1m_progress.json. Use a DISTINCT path when running a "
                         "2nd (unsupervised) catch-up instance so it can't clobber "
                         "the primary's status / the staleness alarm.")
    ap.add_argument("--log", default=None,
                    help="override path for the append-only log; default spxw_1m.log. "
                         "Pair with --progress to fully isolate a 2nd instance.")
    args = ap.parse_args()

    # Isolation hooks: a 2nd (unsupervised) instance passes distinct --progress/--log
    # so it writes its own status + log and cannot corrupt the primary's files (which
    # the HeartbeatStalenessAlarm reads). Omitting the flags = byte-identical behavior
    # to the supervised primary. write_progress()/log() reference these MODULE globals,
    # so reassigning them here (before the loop) redirects all subsequent writes.
    global PROGRESS, LOG
    if args.progress:
        from pathlib import Path
        PROGRESS = Path(args.progress)
    if args.log:
        from pathlib import Path
        LOG = Path(args.log)

    start = (dt.date.fromisoformat(args.start) if args.start else START_DAY)
    end = (dt.date.fromisoformat(args.end) if args.end else dt.date.today())

    QUOTE_DIR.mkdir(parents=True, exist_ok=True)
    OHLC_DIR.mkdir(parents=True, exist_ok=True)

    all_days = trading_days_newest_first(start, end)
    days_total = len(all_days)
    already = sum(1 for d in all_days if day_done(d))
    log(f"=== collector start === window {start}..{end} | "
        f"{days_total} weekday-days | {already} already done | "
        f"newest-first | max_days={args.max_days or 'none'}")

    done_count = already
    errors_count = 0
    last_error = ""
    collected_this_run = 0
    durations: list[float] = []

    for d in all_days:
        if day_done(d):
            continue
        ds = daystr(d)

        # Terminal-up gate (tolerant of the busy/slow terminal warned about).
        if not connected():
            log("terminal down — waiting before this day")
            if not wait_for_terminal(max_wait=600):
                log("terminal still down after 10 min — leaving day un-done, "
                    "supervisor will recycle")
                errors_count += 1
                last_error = "terminal unreachable"
                write_progress(done_count, days_total, ds, 0,
                               errors_count, last_error,
                               sum(durations) / len(durations) if durations else 0)
                return 2     # non-zero -> supervisor restarts the cycle

        t0 = time.time()
        try:
            ok, rows, note = collect_day(d)
            if ok:
                done_count += 1
                collected_this_run += 1
                durations.append(time.time() - t0)
                avg = sum(durations) / len(durations)
                log(f"[{done_count}/{days_total}] {ds} DONE — {note}")
                write_progress(done_count, days_total, ds, rows,
                               errors_count, last_error, avg)
            else:
                # Legit-empty or transient — left un-done, recorded, moved past.
                log(f"{ds} SKIP/RETRY — {note}")
                write_progress(done_count, days_total, ds, rows,
                               errors_count, last_error,
                               sum(durations) / len(durations) if durations else 0)
        except Exception as e:          # noqa: BLE001 — one bad day never aborts the run
            errors_count += 1
            last_error = f"{ds}: {e!r}"
            log(f"{ds} ERROR — {e!r} (left un-done, will retry on a later pass)")
            write_progress(done_count, days_total, ds, 0,
                           errors_count, last_error,
                           sum(durations) / len(durations) if durations else 0)
            time.sleep(5)

        if args.max_days and collected_this_run >= args.max_days:
            log(f"reached --max-days={args.max_days}; stopping (test mode).")
            break

    # Recompute true completion (covers legit-empty days we couldn't fill).
    remaining = [d for d in all_days if not day_done(d)]
    log(f"=== pass complete === done={done_count}/{days_total} | "
        f"errors={errors_count} | remaining(un-done)={len(remaining)}")
    write_progress(done_count, days_total, "", 0, errors_count, last_error,
                   sum(durations) / len(durations) if durations else 0)

    # Exit 0 ONLY when every weekday-day in window has both files (or is a
    # confirmed empty day we keep retrying — but those would keep us non-zero,
    # so we treat "no NEW days collected AND remaining are all empty-result days"
    # as done to avoid an infinite supervisor loop on market holidays).
    if not remaining:
        log("ALL DAYS DONE — exiting 0")
        return 0
    if collected_this_run == 0 and args.max_days == 0:
        # We made a full pass and collected nothing new: the only remaining days
        # return no data (holidays / pre-listing). Treat as complete so the
        # supervisor stops cleanly rather than spinning forever.
        log(f"made a full pass with 0 new days; {len(remaining)} remaining return "
            "no data (holidays/pre-listing). Treating run as COMPLETE — exiting 0")
        return 0
    log("pass ended with days still to do — exiting 1 so supervisor re-runs")
    return 1


if __name__ == "__main__":
    sys.exit(main())
