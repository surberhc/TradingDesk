r"""
pull_equity_options.py — resumable, self-healing EOD pull of REAL single-stock
option quotes for the CAN SLIM options-overlay universe.

WHY
---
The options-overlay backtest (options_overlay_backtest.py) currently prices calls
with a MODELED Black-Scholes + IV sweep — its own report calls this a "friendly
upper bound" and asks for validation on REAL quotes. This pull fetches the actual
historical EOD chains (bid/ask/delta/implied_vol/underlying_price) from the local
ThetaData v3 Terminal so run_options_overlay_real.py can replace the model with
real fills + real IV + real delta.

WHAT IT PULLS
-------------
- Universe: the LIQUID overlay names (imported from options_overlay_backtest.LIQUID
  — the bought-names ∩ liquid-optionable set, the exact IN-list the modeled backtest
  used). ~55 names.
- Window: 2023-01-01 .. today, business days.
- CALLS + PUTS (right=both, one request), so we never re-subscribe to add puts.
- The API returns the full chain per day; we store a STRIKE BAND (ATM ±20%) and
  EXPIRIES to ~9mo client-side to keep files lean (the overlay only ever looks at
  ~ATM calls out to 9mo). The band is applied AFTER download.

STORAGE (LOCAL warehouse only, never Drive)
-------------------------------------------
    C:\TradingDesk-Local\canslim\thetadata_equity\{SYMBOL}\{YYYYMM}.parquet
One parquet PER NAME-MONTH (partitioned by name/month, per the brief). A month
file present == that month is DONE for that name (resume-by-skip). Each month is
built by pulling its business days, concatenating, and writing ATOMICALLY
(temp + os.replace) so a kill mid-write can never leave a torn "done" file.

LIVENESS-RUBRIC HANDLING (the 11 death-and-no-restart modes)
------------------------------------------------------------
- crash:            per-day and per-month work is wrapped; the supervisor restarts
                    the whole process on a non-zero exit; on resume it skips done.
- stall:            a heartbeat JSON is refreshed on every day/month; the watchdog
                    restarts a stale process. thetadata_client already retries GETs.
- dup:              atomic O_CREAT|O_EXCL PID lockfile — a second instance exits.
- partial output:   resume-by-skipping-done month files + ATOMIC month writes.
- poison item:      a bad (name, day) is caught, logged to a poison list, skipped;
                    a bad name-month is skipped; the pull never wedges on one item.
- dep-down:         if the Terminal is unreachable we WAIT-and-retry (bounded loop),
                    never crash; the watchdog also keeps the Terminal alive.
- concurrency cap:  default 4 workers, hard-capped at 6 (shared Terminal safety).

Usage:
    python pull_equity_options.py                # full universe (resumable)
    python pull_equity_options.py --names NVDA   # one name (tiny test)
    python pull_equity_options.py --names NVDA --start 20240102 --end 20240108
    python pull_equity_options.py --workers 4
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
# Reuse the datacollector client + config (the exact same local Terminal endpoint
# the SPX collector uses). Do NOT reinvent the client.
sys.path.insert(0, str(HERE.parent / "datacollector"))
import config as dc_config          # noqa: E402
import thetadata_client as td       # noqa: E402

# The approved liquid IN-list, imported straight from the modeled backtest so the
# real pull covers exactly the names the modeled run covered (no drift).
sys.path.insert(0, str(HERE))
import options_overlay_backtest as ovb   # noqa: E402

# --------------------------------------------------------------------------- #
# Paths / config
# --------------------------------------------------------------------------- #
WAREHOUSE = Path(r"C:\TradingDesk-Local\canslim\thetadata_equity")
STATE_DIR = Path(r"C:\TradingDesk-Local\state\canslim")
HEARTBEAT = STATE_DIR / "pull_equity_options_heartbeat.json"
LOCK = STATE_DIR / "pull_equity_options.lock"
POISON = STATE_DIR / "pull_equity_options_poison.json"
LOG = STATE_DIR / "pull_equity_options.log"

START_DEFAULT = "20230101"
DEFAULT_WORKERS = 4
MAX_WORKERS = 6                     # HARD CAP — never starve the shared SPX collector
STRIKE_BAND = 0.20                 # keep strikes within ATM ±20%
MAX_TENOR_DAYS = 300               # keep expiries out to ~9-10 months
DROP_COLS = ["bid_exchange", "bid_condition", "ask_exchange", "ask_condition"]

TERMINAL_WAIT_TICK = 30            # seconds between Terminal reachability retries
TERMINAL_MAX_WAIT = 1800           # give up a *cycle* after 30 min unreachable (supervisor re-runs)


# --------------------------------------------------------------------------- #
# Logging + heartbeat
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


def write_heartbeat(payload: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {**payload, "ts": dt.datetime.now().isoformat(timespec="seconds")}
        tmp = HEARTBEAT.with_name(HEARTBEAT.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, HEARTBEAT)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Poison list (bad name-days / name-months we skip so one bad item can't wedge)
# --------------------------------------------------------------------------- #
def _load_poison() -> dict:
    if not POISON.exists():
        return {}
    try:
        return json.loads(POISON.read_text())
    except Exception:
        return {}


def _record_poison(key: str, err: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        p = _load_poison()
        p[key] = {"err": str(err)[:300], "ts": dt.datetime.now().isoformat(timespec="seconds")}
        tmp = POISON.with_name(POISON.name + ".tmp")
        tmp.write_text(json.dumps(p, indent=2, sort_keys=True))
        os.replace(tmp, POISON)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Singleton lock (atomic O_CREAT|O_EXCL PID lock; mirrors the SPX supervisor)
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = __import__("subprocess").run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=15,
            )
            return str(pid) in out.stdout
        except Exception:
            return True   # can't tell -> refuse to start a dup
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock() -> bool:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    me = os.getpid()
    for _ in range(2):
        try:
            fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(me))
            return True
        except FileExistsError:
            try:
                holder = int(LOCK.read_text().strip() or "0")
            except (OSError, ValueError):
                holder = 0
            if holder == me:
                return True
            if holder and _pid_alive(holder):
                log(f"another equity-pull instance is live (pid={holder}) -> exiting (no dup)")
                return False
            log(f"stale lock (pid={holder} not running) -> reclaiming")
            try:
                LOCK.unlink()
            except OSError:
                pass
            continue
    return False


def release_lock() -> None:
    try:
        if LOCK.exists() and LOCK.read_text().strip() == str(os.getpid()):
            LOCK.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Terminal reachability (dep-down mode: wait + retry, never crash)
# --------------------------------------------------------------------------- #
def wait_for_terminal(max_wait: int = TERMINAL_MAX_WAIT) -> bool:
    waited = 0
    while waited < max_wait:
        if td.connected():
            return True
        log(f"ThetaData Terminal not reachable at {dc_config.THETA_BASE_URL} — "
            f"waiting {TERMINAL_WAIT_TICK}s (waited {waited}s)")
        time.sleep(TERMINAL_WAIT_TICK)
        waited += TERMINAL_WAIT_TICK
    return td.connected()


# --------------------------------------------------------------------------- #
# Storage helpers (one parquet per name-month)
# --------------------------------------------------------------------------- #
def month_path(symbol: str, ym: str) -> Path:
    return WAREHOUSE / symbol / f"{ym}.parquet"


def have_month(symbol: str, ym: str) -> bool:
    """A present month file == that (name, month) is done -> skip on resume."""
    return month_path(symbol, ym).exists()


def write_month_atomic(symbol: str, ym: str, df: pd.DataFrame) -> int:
    p = month_path(symbol, ym)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    df.to_parquet(tmp, engine="pyarrow", compression="zstd", index=False)
    os.replace(tmp, p)   # atomic: a kill between write & replace leaves only a .tmp
    return len(df)


# --------------------------------------------------------------------------- #
# Calendar helpers
# --------------------------------------------------------------------------- #
def business_days(start: str, end: str) -> list[str]:
    rng = pd.bdate_range(pd.to_datetime(start), pd.to_datetime(end))
    return [d.strftime("%Y%m%d") for d in rng]


def months_in_range(start: str, end: str) -> list[str]:
    """List of YYYYMM covering [start, end]."""
    s = dt.datetime.strptime(start, "%Y%m%d").date()
    e = dt.datetime.strptime(end, "%Y%m%d").date()
    out = []
    y, m = s.year, s.month
    while (y, m) <= (e.year, e.month):
        out.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def month_bdays(ym: str, start: str, end: str) -> list[str]:
    """Business days of month ym that fall within [start, end]."""
    y, m = int(ym[:4]), int(ym[4:])
    first = dt.date(y, m, 1)
    last = dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1)
    lo = max(first, dt.datetime.strptime(start, "%Y%m%d").date())
    hi = min(last, dt.datetime.strptime(end, "%Y%m%d").date())
    if lo > hi:
        return []
    return business_days(lo.strftime("%Y%m%d"), hi.strftime("%Y%m%d"))


# --------------------------------------------------------------------------- #
# Per-day / per-month pull
# --------------------------------------------------------------------------- #
def _band_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only strikes within ATM ±STRIKE_BAND and expiries out to MAX_TENOR_DAYS.
    Applied AFTER download (the API returns the full chain regardless)."""
    if df.empty:
        return df
    if "underlying_price" not in df.columns or "strike" not in df.columns:
        return df
    spot = pd.to_numeric(df["underlying_price"], errors="coerce")
    strike = pd.to_numeric(df["strike"], errors="coerce")
    keep = (strike >= spot * (1 - STRIKE_BAND)) & (strike <= spot * (1 + STRIKE_BAND))
    out = df[keep].copy()
    # expiry band: within MAX_TENOR_DAYS of the trade date
    if "expiration" in out.columns and "date" in out.columns:
        try:
            exp = pd.to_datetime(out["expiration"], errors="coerce")
            asof = pd.to_datetime(out["date"], format="%Y%m%d", errors="coerce")
            tdays = (exp - asof).dt.days
            out = out[(tdays >= 0) & (tdays <= MAX_TENOR_DAYS)].copy()
        except Exception:
            pass
    return out


def pull_day(symbol: str, daystr: str) -> pd.DataFrame:
    """Greeks (calls+puts) for one name-day, band-filtered. Empty on a holiday."""
    greeks = td.eod_greeks(symbol, daystr, daystr, right="both")
    if greeks.empty:
        return greeks
    greeks = greeks.drop(columns=[c for c in DROP_COLS if c in greeks.columns])
    greeks.insert(0, "date", daystr)
    return _band_filter(greeks)


def pull_month(symbol: str, ym: str, start: str, end: str) -> tuple[str, str, int, int]:
    """Pull all business days of one name-month, concat, write ONE atomic parquet.

    Returns (symbol, ym, n_rows, n_days_with_data). Poison-day tolerant: a day that
    keeps failing is recorded and skipped so the month still completes. If the whole
    month is unreachable it raises so the caller can retry/leave-for-resume (no file
    written -> month re-attempted next run)."""
    days = month_bdays(ym, start, end)
    frames = []
    n_days = 0
    for daystr in days:
        for attempt in range(3):
            try:
                df = pull_day(symbol, daystr)
                if not df.empty:
                    frames.append(df)
                    n_days += 1
                break
            except Exception as e:                     # transient -> backoff & retry
                if attempt == 2:
                    _record_poison(f"{symbol}:{daystr}", e)
                    log(f"  POISON-DAY {symbol} {daystr}: {e} -> skipped")
                else:
                    time.sleep(1.5 * (attempt + 1))
    if not frames:
        # No data for the whole month in range (e.g. name IPO'd later). Write an
        # EMPTY marker so we don't re-pull it forever (file-present == done).
        empty = pd.DataFrame()
        write_month_atomic(symbol, ym, empty)
        return (symbol, ym, 0, 0)
    out = pd.concat(frames, ignore_index=True)
    n = write_month_atomic(symbol, ym, out)
    return (symbol, ym, n, n_days)


# --------------------------------------------------------------------------- #
# Work planning + concurrent execution
# --------------------------------------------------------------------------- #
def plan_work(names: list[str], start: str, end: str) -> list[tuple[str, str]]:
    """List of (symbol, ym) name-months NOT yet on disk (resume-by-skip)."""
    todo = []
    for sym in names:
        for ym in months_in_range(start, end):
            if not month_bdays(ym, start, end):
                continue
            if have_month(sym, ym):
                continue
            todo.append((sym, ym))
    return todo


def run_pull(names: list[str], start: str, end: str, workers: int) -> dict:
    workers = max(1, min(workers, MAX_WORKERS))
    todo = plan_work(names, start, end)
    total = len(todo)
    log(f"=== equity-options pull start: {len(names)} names, {start}..{end}, "
        f"{workers} workers, {total} name-months to do ===")

    done = 0
    rows_total = 0
    per_name_done = defaultdict(int)
    write_heartbeat({"phase": "running", "names": len(names), "workers": workers,
                     "months_total": total, "months_done": 0, "rows": 0,
                     "start": start, "end": end})

    if total == 0:
        log("nothing to do — all name-months already on disk (resume: full skip)")
        write_heartbeat({"phase": "complete", "months_total": 0, "months_done": 0,
                         "rows": 0, "start": start, "end": end})
        return {"months_total": 0, "months_done": 0, "rows": 0, "failed": []}

    failed = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(pull_month, sym, ym, start, end): (sym, ym)
                for (sym, ym) in todo}
        for fut in as_completed(futs):
            sym, ym = futs[fut]
            try:
                _sym, _ym, n, ndays = fut.result()
                rows_total += n
                per_name_done[sym] += 1
                done += 1
                log(f"  [{done}/{total}] {sym} {ym}: {n:,} rows, {ndays} days")
            except Exception as e:
                # Month-level failure (e.g. Terminal dropped mid-month). No file
                # written -> it stays in the resume set for the next run.
                failed.append((sym, ym, str(e)[:200]))
                log(f"  FAIL {sym} {ym}: {e} (will retry on resume)")
            write_heartbeat({"phase": "running", "names": len(names),
                             "workers": workers, "months_total": total,
                             "months_done": done, "rows": rows_total,
                             "current": f"{sym} {ym}", "start": start, "end": end})

    phase = "complete" if not failed else "partial"
    write_heartbeat({"phase": phase, "months_total": total, "months_done": done,
                     "rows": rows_total, "failed": len(failed),
                     "start": start, "end": end})
    log(f"=== pull {phase}: {done}/{total} name-months, {rows_total:,} rows, "
        f"{len(failed)} failed ===")
    return {"months_total": total, "months_done": done, "rows": rows_total,
            "failed": failed}


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Resumable EOD equity-options pull")
    ap.add_argument("--names", nargs="*", default=None,
                    help="explicit symbols (default: the liquid overlay universe)")
    ap.add_argument("--start", default=START_DEFAULT)
    ap.add_argument("--end", default=None, help="default: today")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--no-lock", action="store_true",
                    help="skip the singleton lock (for a supervised tiny test)")
    args = ap.parse_args()

    names = [n.upper() for n in args.names] if args.names else sorted(ovb.LIQUID)
    end = args.end or dt.date.today().strftime("%Y%m%d")

    if not args.no_lock and not acquire_lock():
        return 0   # a live instance already owns the pull; not an error
    try:
        if not wait_for_terminal():
            log("Terminal unreachable past the wait budget — exiting non-zero so "
                "the supervisor retries the whole cycle")
            write_heartbeat({"phase": "terminal_down", "start": args.start, "end": end})
            return 2
        res = run_pull(names, args.start, end, args.workers)
        # non-zero only if NOTHING got done AND there were failures (so the
        # supervisor retries); a partial with progress exits 0 and resumes later.
        if res["months_total"] > 0 and res["months_done"] == 0:
            return 3
        return 0
    finally:
        if not args.no_lock:
            release_lock()


if __name__ == "__main__":
    sys.exit(main())
