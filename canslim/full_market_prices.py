"""
full_market_prices.py — CAN SLIM full-market SELECTION, Phase 1: the survivorship-free
DAILY PRICE + VOLUME leg.

Pulls daily OHLCV (raw + split/dividend-ADJUSTED) for every candidate symbol in the FULL
survivorship-free universe (canslim/universe/candidate_tickers.csv, built by
full_market_universe.py), 2010-2026, into the local warehouse. One parquet per symbol.

SOURCE ORDER (minimize IBKR/Gateway contention; free-first)
-----------------------------------------------------------
  1. STOOQ bulk (the delisted backbone) — the intended whole-universe path IF a usable bulk
     archive is available on the machine. NOTE (verified 2026-07-02): Stooq's per-ticker CSV
     endpoint and its bulk-download page are now JS/anti-bot/login gated and return an HTML
     interstitial (per-ticker) or HTTP 401 (bulk) to a headless client, so Stooq is NOT
     scriptable from this environment right now. This module will USE a Stooq bulk archive if
     one has been placed on disk manually (STOOQ_BULK_DIR), and otherwise SKIPS Stooq
     cleanly — it never blocks the pull. (Documented limit, not a silent gap.)
  2. TIINGO free tier — the working headless source. Full OHLCV + adjClose/adjVolume +
     split/div factors; RETAINS delisted symbols (permaTicker). This is the primary puller
     here. FREE-TIER CAPS are the binding constraint: ~50 req/hr, ~1,000 req/day, and 500
     UNIQUE symbols/month. A 16.7k-symbol universe therefore spans WEEKS of daily runs — the
     puller is built to be run every day and RESUME (skip-done). Honest and reported.
  3. IBKR gap-fill — READ-ONLY reqHistoricalData for symbols the free sources missed. Wired
     as a SEPARATE later leg (canslim_price_gapfill clientId 43): it must take the Gateway
     mutex (paperbot.gateway_lock) and YIELD to AccountMonitorDaily, so it is intentionally
     NOT auto-run here. Run it deliberately, off-hours, against the leftover-misses list this
     module writes. (Stub + contract documented at the bottom of this file.)

SELF-HEALING / RESUMABLE (this is a LARGE, multi-day pull)
----------------------------------------------------------
  * SKIP DONE      — a symbol whose parquet already exists (and is non-empty) is skipped.
  * ATOMIC WRITES  — write to <sym>.parquet.tmp then os.replace() so a killed run never
                     leaves a half-written parquet that looks "done".
  * RETRY          — transient HTTP errors get a few bounded retries with backoff.
  * RATE-LIMIT     — on HTTP 429 / hourly-cap, the run stops cleanly (state saved) so it can
                     resume on the next invocation; nothing is lost.
  * HEARTBEAT      — a progress line every N symbols (done / remaining / rate / last symbol),
                     and a JSON heartbeat file so a supervisor can see liveness.
  * MISS LEDGER    — symbols that returned no data (delisted-before-2010, foreign, or truly
                     absent) are recorded in misses.csv for the IBKR gap-fill leg — an honest,
                     counted coverage gap, never a silent drop.

DATA (local warehouse, never on Drive):
    C:/TradingDesk-Local/canslim/universe/candidate_tickers.csv   (input — what to pull)
    C:/TradingDesk-Local/canslim/prices/<SYMBOL>.parquet          (output — one per symbol)
    C:/TradingDesk-Local/canslim/prices/_state/                   (heartbeat, misses, log)

Only this CODE lives in the Drive repo. Prices are warehouse data (never committed).

USAGE
    python full_market_prices.py               # pull (resumable) until day-cap / done
    python full_market_prices.py status        # coverage report, pull nothing
    python full_market_prices.py --limit 400   # cap this run to N new symbols (free-tier safe)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

# ---- paths -------------------------------------------------------------------------------
UNIVERSE = Path(r"C:\TradingDesk-Local\canslim\universe")
PRICES = Path(r"C:\TradingDesk-Local\canslim\prices")
STATE = PRICES / "_state"
CANDIDATES_CSV = UNIVERSE / "candidate_tickers.csv"
HEARTBEAT_JSON = STATE / "heartbeat.json"
MISSES_CSV = STATE / "misses.csv"
LOG_TXT = STATE / "pull_log.txt"

STOOQ_BULK_DIR = Path(r"C:\TradingDesk-Local\canslim\stooq_bulk")  # optional manual bulk drop

# ---- window ------------------------------------------------------------------------------
START = "2010-01-01"
END = "2026-12-31"

# ---- Tiingo ------------------------------------------------------------------------------
TIINGO_BASE = "https://api.tiingo.com/tiingo/daily"
# Free-tier-friendly defaults; a single run stops well before the daily cap so it always
# resumes cleanly the next day rather than dying on a 429.
DEFAULT_RUN_LIMIT = 400          # new symbols per run (well under the ~1000/day request cap)
REQ_PAUSE_SECS = 1.2            # gentle spacing (~50/min ceiling on the free hourly window)
MAX_RETRIES = 3
HEARTBEAT_EVERY = 20            # symbols between heartbeat/progress lines


def _tiingo_key() -> str:
    key = os.environ.get("TIINGO_API_KEY")
    if not key:
        raise RuntimeError("TIINGO_API_KEY env var not set (Windows user env var, off Drive).")
    return key


def _atomic_write_parquet(df: pd.DataFrame, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, dest)   # atomic on same filesystem


def _log(msg: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {msg}"
    print(line, flush=True)
    with open(LOG_TXT, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _heartbeat(done: int, total: int, remaining: int, last: str, note: str = "") -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "done_on_disk": done, "total": total, "remaining": remaining,
           "last_symbol": last, "note": note}
    tmp = HEARTBEAT_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=2))
    os.replace(tmp, HEARTBEAT_JSON)


# ==========================================================================================
# STOOQ bulk (optional; used only if a bulk archive was placed on disk)
# ==========================================================================================

def _stooq_bulk_frame(symbol: str) -> pd.DataFrame | None:
    """
    Read one symbol from a locally-placed Stooq bulk archive (STOOQ_BULK_DIR), if present.
    Stooq's txt bundle stores <sym>.us.txt with columns Date,Open,High,Low,Close,Volume.
    Returns a normalized frame (date,open,high,low,close,volume) or None if absent.

    NOTE: Stooq is not scriptable headless right now (see module docstring). This reads a
    manually-provided bulk drop only; it never hits the network.
    """
    if not STOOQ_BULK_DIR.exists():
        return None
    for cand in (STOOQ_BULK_DIR / f"{symbol.lower()}.us.txt",
                 STOOQ_BULK_DIR / f"{symbol.lower()}.txt"):
        if cand.exists():
            try:
                d = pd.read_csv(cand)
            except Exception:
                return None
            d.columns = [c.strip().lower() for c in d.columns]
            need = {"date", "open", "high", "low", "close", "volume"}
            if not need.issubset(set(d.columns)):
                return None
            d = d[["date", "open", "high", "low", "close", "volume"]].copy()
            d["date"] = pd.to_datetime(d["date"], errors="coerce")
            d = d.dropna(subset=["date"])
            d = d[(d["date"] >= START) & (d["date"] <= END)]
            if d.empty:
                return None
            # bulk txt is unadjusted; mirror close->adjClose so schema is uniform
            d["adj_close"] = d["close"]
            d["source"] = "stooq_bulk"
            return d.sort_values("date").reset_index(drop=True)
    return None


# ==========================================================================================
# TIINGO
# ==========================================================================================

class RateLimited(Exception):
    """Raised when Tiingo signals the free-tier cap so the run can stop-and-resume."""


def _tiingo_frame(symbol: str, key: str) -> pd.DataFrame | None:
    """
    Pull one symbol's daily OHLCV from Tiingo. Returns a normalized frame or None if the
    symbol legitimately has no data (delisted-before-window / not covered). Raises
    RateLimited on a 429 / cap so the caller stops cleanly.
    """
    url = f"{TIINGO_BASE}/{symbol}/prices"
    params = {"startDate": START, "endDate": END, "format": "json", "token": key}
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=45)
        except requests.RequestException as e:
            last_exc = e
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            raise RateLimited(f"429 on {symbol}")
        if r.status_code == 404:
            return None            # symbol not in Tiingo — an honest miss
        if r.status_code >= 500:
            last_exc = RuntimeError(f"{r.status_code} on {symbol}")
            time.sleep(2 * (attempt + 1))
            continue
        if not r.ok:
            # 400-class (e.g. bad symbol) — a miss, not a retry
            txt = r.text.lower()
            if "not found" in txt or "no data" in txt or "supported" in txt:
                return None
            if "limit" in txt or "exceeded" in txt:
                raise RateLimited(f"cap text on {symbol}: {r.text[:80]}")
            return None
        rows = r.json()
        if not rows:
            return None            # covered but empty in-window — honest miss
        d = pd.DataFrame(rows)
        d["date"] = pd.to_datetime(d["date"]).dt.tz_localize(None)
        out = pd.DataFrame({
            "date": d["date"],
            "open": d.get("open"), "high": d.get("high"),
            "low": d.get("low"), "close": d.get("close"),
            "volume": d.get("volume"),
            # adjusted close = split+dividend adjusted (what RS/base detection needs)
            "adj_close": d.get("adjClose"),
        })
        out["source"] = "tiingo"
        return out.sort_values("date").reset_index(drop=True)
    raise RuntimeError(f"tiingo failed for {symbol}: {last_exc}")


# ==========================================================================================
# Driver
# ==========================================================================================

def _candidates() -> list[str]:
    if not CANDIDATES_CSV.exists():
        raise SystemExit(f"missing {CANDIDATES_CSV} — run full_market_universe.py candidates")
    df = pd.read_csv(CANDIDATES_CSV)
    return sorted(df["ticker"].astype(str).str.upper().unique().tolist())


def _done_symbols() -> set[str]:
    """Symbols already pulled (non-empty parquet on disk)."""
    out: set[str] = set()
    if not PRICES.exists():
        return out
    for p in PRICES.glob("*.parquet"):
        if p.stat().st_size > 0:
            out.add(p.stem.upper())
    return out


def _record_miss(symbol: str, why: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    exists = MISSES_CSV.exists()
    with open(MISSES_CSV, "a", encoding="utf-8") as fh:
        if not exists:
            fh.write("ticker,why,ts\n")
        fh.write(f"{symbol},{why},{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")


def status() -> None:
    cands = _candidates()
    done = _done_symbols()
    n_miss = 0
    if MISSES_CSV.exists():
        n_miss = max(0, sum(1 for _ in open(MISSES_CSV, encoding="utf-8")) - 1)
    remaining = [c for c in cands if c not in done]
    print("FULL-MARKET PRICE COVERAGE")
    print(f"  candidate symbols   : {len(cands):,}")
    print(f"  pulled (on disk)    : {len(done):,}  ({100*len(done)/max(1,len(cands)):.1f}%)")
    print(f"  recorded misses     : {n_miss:,}")
    print(f"  remaining to pull   : {len(remaining):,}")
    if HEARTBEAT_JSON.exists():
        print(f"  last heartbeat      : {HEARTBEAT_JSON.read_text()[:200]}")


def pull(run_limit: int = DEFAULT_RUN_LIMIT) -> None:
    PRICES.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    key = _tiingo_key()
    cands = _candidates()
    done = _done_symbols()
    remaining = [c for c in cands if c not in done]
    _log(f"PULL start: {len(cands):,} candidates, {len(done):,} done, "
         f"{len(remaining):,} remaining, run_limit={run_limit}")

    pulled = 0
    misses = 0
    for i, sym in enumerate(remaining):
        if pulled >= run_limit:
            _log(f"run_limit {run_limit} reached — stopping cleanly (resumable).")
            break
        try:
            frame = _stooq_bulk_frame(sym)          # 1) free bulk backbone if present
            if frame is None:
                frame = _tiingo_frame(sym, key)     # 2) Tiingo (working headless source)
        except RateLimited as e:
            _log(f"RATE-LIMITED ({e}) — stopping cleanly, resume next run.")
            break
        except Exception as e:
            _log(f"  [err] {sym}: {e} (skipping; will retry next run)")
            continue

        if frame is None or frame.empty:
            _record_miss(sym, "no_data")
            misses += 1
        else:
            _atomic_write_parquet(frame, PRICES / f"{sym}.parquet")
            pulled += 1

        if (pulled + misses) % HEARTBEAT_EVERY == 0:
            ndone = len(done) + pulled
            _heartbeat(ndone, len(cands), len(cands) - ndone, sym,
                       note=f"this run: +{pulled} pulled, {misses} misses")
            _log(f"  progress: +{pulled} pulled, {misses} misses this run; "
                 f"{ndone:,}/{len(cands):,} on disk; last={sym}")
        time.sleep(REQ_PAUSE_SECS)

    ndone = len(done) + pulled
    _heartbeat(ndone, len(cands), len(cands) - ndone, "(run end)",
               note=f"run complete: +{pulled} pulled, {misses} misses")
    _log(f"PULL end: +{pulled} pulled, {misses} misses this run; "
         f"{ndone:,}/{len(cands):,} total on disk ({100*ndone/max(1,len(cands)):.1f}%).")


# ==========================================================================================
# IBKR gap-fill — SEPARATE, DELIBERATE, off-hours leg (documented contract; not auto-run)
# ==========================================================================================

def ibkr_gapfill_stub() -> None:
    """
    CONTRACT for the IBKR gap-fill leg (run deliberately, off-hours, NOT from pull()):

      * Read misses.csv (symbols the free sources could not cover).
      * clientId = connections.clientids.CLIENT_IDS['canslim_price_gapfill'] (43), READ-ONLY.
      * Acquire the Gateway mutex: paperbot.gateway_lock(purpose='canslim_price_gapfill',
        client_id=43, on_busy='skip') so it YIELDS to AccountMonitorDaily / rebalance.
      * reqHistoricalData(TRADES, durationStr covering 2010-2026, barSizeSetting='1 day').
      * Write survivors to the same warehouse schema (date/open/high/low/close/volume/
        adj_close/source='ibkr'); leave delisted-with-no-IB-history symbols in misses as an
        honest coverage gap (IB history is keyed to tradable contracts and vanishes on delist).

    Left as a stub on purpose: it touches the paper Gateway, so it is a deliberate action,
    not something the free-source puller should trigger automatically.
    """
    raise NotImplementedError("IBKR gap-fill is a separate deliberate off-hours leg; see contract.")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "status":
        status(); return
    run_limit = DEFAULT_RUN_LIMIT
    if "--limit" in args:
        run_limit = int(args[args.index("--limit") + 1])
    pull(run_limit=run_limit)


if __name__ == "__main__":
    main()
