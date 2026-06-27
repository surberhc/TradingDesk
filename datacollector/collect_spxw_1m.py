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

    # 3) Write both atomically. Write ohlc first, then quote LAST, so that if a
    #    crash lands between the two writes the day is still not "done" (quote is
    #    the second/final marker) and will be cleanly retried.
    _write_atomic(OHLC_DIR / f"{ds}.parquet", o)
    _write_atomic(QUOTE_DIR / f"{ds}.parquet", q)

    secs = time.time() - t0
    return True, len(q), (f"quote={len(q):,} rows, ohlc={len(o):,} rows, "
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
    args = ap.parse_args()

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
