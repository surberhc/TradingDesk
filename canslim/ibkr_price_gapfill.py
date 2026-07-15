"""
ibkr_price_gapfill.py — CAN SLIM full-market SELECTION, Phase 1: the IBKR survivor
DAILY PRICE + VOLUME leg (the FREE half of the staged price plan).

WHAT THIS IS
------------
A resumable, crash-safe, READ-ONLY IBKR historical daily-bars puller that fills the
full-universe stock-PRICE gap for the currently-TRADABLE ("survivor") US equities that
IBKR can resolve. Delisted names that IBKR can no longer serve are DEFERRED to a paid
source later — survivors-only is the intended, honest scope. Every symbol IBKR cannot
resolve is LOGGED (never fabricated) to `_state/ibkr_unresolved.json` for that future
paid pull.

It writes into the EXACT SAME per-symbol parquet schema that the Tiingo puller
(full_market_prices.py) produces, so full_market_join.prices_asof reads IBKR-sourced
files with ZERO changes:

    columns : date(datetime64[ns], no tz), open, high, low, close (float64),
              volume(int64), adj_close(float64), source(str)
    written : df.to_parquet(index=False)   # one file per SYMBOL

ADJUSTMENT CONVENTION (documented divergence, closest match — verified live in the smoke
test, see MATCH NOTE below)
-------------------------------------------------------------------------------
Tiingo stores RAW `close` + split/dividend-ADJUSTED `adj_close` in one row. IBKR has no
single request that returns both, so we make TWO requests per symbol and merge on date:
  * whatToShow="TRADES"         -> raw OHLC + real traded VOLUME + raw `close`
                                   (IBKR TRADES is split-adjusted going back but NOT
                                    dividend-adjusted — this matches Tiingo's raw `close`,
                                    which is also split-adjusted-not-div-adjusted for the
                                    OHLCV leg the base detector consumes).
  * whatToShow="ADJUSTED_LAST"  -> split+DIVIDEND-adjusted close -> `adj_close`
                                   (this is the total-return-style adjusted close the RS /
                                    base-detection code reads; the closest IBKR analog to
                                    Tiingo's adjClose).
Volume and OHLC come from TRADES only (ADJUSTED_LAST rescales OHLC and is used solely for
its close). If ADJUSTED_LAST is unavailable for a symbol, `adj_close` falls back to the
raw close (same fallback the Stooq-bulk path in full_market_prices.py uses) and the symbol
is flagged in the run log — an explicit, counted degradation, never a silent one.

PACING (broker-wide, cannot be raised — see connections/IBKR_CAPABILITIES.md)
-----------------------------------------------------------------------------
<= 60 historical requests / rolling 10 min. We make 2 requests/symbol (TRADES +
ADJUSTED_LAST), so the ceiling is ~30 symbols / 10 min. A token-bucket throttle spaces
requests to ~1 / 10.5 s and NEVER lets more than 60 fire inside any 10-min window, with
generous margin, so we cannot trip a pacing violation. ~6k survivors * 2 req => ~35-40 h
of pull time; run it across nights, it resumes.

SAFE ALONGSIDE OTHER GATEWAY CLIENTS
------------------------------------
  * OWN clientId: connections.clientids['canslim_price_gapfill'] = 43 (registered).
  * readonly=True — the connection is physically incapable of transmitting an order.
  * Takes the paperbot Gateway MUTEX (gateway_lock, on_busy="skip") so it YIELDS to the
    AccountMonitorDaily / rebalance tasks and never contends. On a busy Gateway it exits
    cleanly (resumable) rather than fighting for the lock.

RESUMABLE + CRASH-SAFE + LIVENESS (this runs ~35-40 h unattended over 2-3 nights)
---------------------------------------------------------------------------------
  * SKIP DONE     — a symbol whose non-empty parquet already exists is skipped (works for
                    Tiingo-sourced files too, so IBKR only pulls what's genuinely missing).
  * ATOMIC WRITE  — write <sym>.parquet.tmp then os.replace() (atomic) so a mid-write kill
                    never leaves a half-written parquet that looks "done".
  * CHECKPOINT    — resolution results (resolved / unresolved) persisted to JSON so a
                    restart skips re-resolving; the pull itself resumes from on-disk files.
  * RECONNECT     — HMDS/farm drops and Gateway disconnects are caught; the puller
                    reconnects (bounded backoff) and continues the same symbol.
  * HEARTBEAT     — a JSON heartbeat (ts + done/total + last symbol + phase) rewritten
                    every HEARTBEAT_EVERY symbols; a supervisor detects a STALE heartbeat.
  * WATCHDOG      — `python ibkr_price_gapfill.py watchdog` relaunches the pull on crash
                    and treats a stale heartbeat as a hang (kills + relaunches). Survives a
                    reboot via the on-disk checkpoint + skip-done.

DATA (local warehouse, never on Drive):
    C:/TradingDesk-Local/canslim/universe/candidate_tickers.csv       (input universe)
    C:/TradingDesk-Local/canslim/prices/<SYMBOL>.parquet              (output, shared w/ Tiingo)
    C:/TradingDesk-Local/canslim/prices/_state/ibkr_resolved.json     (resolved symbols)
    C:/TradingDesk-Local/canslim/prices/_state/ibkr_unresolved.json   (deferred to paid pull)
    C:/TradingDesk-Local/canslim/prices/_state/ibkr_terminal_skip.json (resolved but permanently
                                                                        unpullable — IBKR err 162)
    C:/TradingDesk-Local/canslim/prices/_state/ibkr_heartbeat.json    (liveness)
    C:/TradingDesk-Local/canslim/prices/_state/ibkr_pull_log.txt      (append-only log)

Only this CODE lives in the Drive repo. Prices are warehouse data (never committed).

USAGE
    python ibkr_price_gapfill.py smoke [N]     # LIVE gateway smoke test on N (default 10)
                                               #   real universe symbols end-to-end; pulls
                                               #   nothing to the real warehouse (uses a
                                               #   throwaway temp dir), prints the proof.
    python ibkr_price_gapfill.py resolve       # resolve-only pass (fills resolved/unresolved)
    python ibkr_price_gapfill.py pull [--limit N]   # the resumable pull (default: all remaining)
    python ibkr_price_gapfill.py status        # coverage + resolution report, pull nothing
    python ibkr_price_gapfill.py watchdog       # supervised pull: relaunch on crash/stale
    python ibkr_price_gapfill.py launcher       # SCHEDULED-TASK entry: singleton-guarded +
                                                #   no-op-if-complete wrapper around watchdog.
                                                #   Safe to fire repeatedly (Task Scheduler
                                                #   AtStartup/AtLogOn/repeating) — at most one
                                                #   pull ever runs (cross-process lockfile).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# The shared connection layer owns the clientId registry + the Gateway-launch fix.
# Add both package roots so `connections` and `paperbot.gateway_lock` import cleanly
# regardless of the CWD the watchdog/scheduler launches us from.
_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO / "connections"), str(_REPO / "paperbot")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from connections import clientids                      # noqa: E402
from connections import ibkr_paper as gw               # noqa: E402

CLIENT = "canslim_price_gapfill"                        # clientId 43 (see connections.clientids)

# ---- paths -------------------------------------------------------------------------------
UNIVERSE = Path(r"C:\TradingDesk-Local\canslim\universe")
PRICES = Path(r"C:\TradingDesk-Local\canslim\prices")
STATE = PRICES / "_state"
CANDIDATES_CSV = UNIVERSE / "candidate_tickers.csv"

RESOLVED_JSON = STATE / "ibkr_resolved.json"
UNRESOLVED_JSON = STATE / "ibkr_unresolved.json"
HEARTBEAT_JSON = STATE / "ibkr_heartbeat.json"
LOG_TXT = STATE / "ibkr_pull_log.txt"
# TERMINAL-SKIP ledger: symbols that RESOLVE against IBKR but can NEVER return daily bars
# (IBKR error 162 — "No market data permissions" for PINK/ARCAEDGE names, or "No historical
# market data" for delisted-but-still-resolvable tickers). Without this ledger they resolve
# every run, return no bars, never land on disk, and so are counted as "remaining" forever —
# which starved the completion check and left the watchdog in a permanent kill+relaunch loop.
# Each empty pull bumps a counter; TERMINAL_SKIP_AFTER consecutive empties promotes the symbol
# to terminal-skip, and it is then excluded from the resolvable-work set (deferred to the paid
# source alongside the unresolved names). A genuinely-new survivor is unaffected (starts at 0).
TERMINAL_JSON = STATE / "ibkr_terminal_skip.json"

# Cross-process SINGLETON lock for the pull-SUPERVISION role (launcher/watchdog). This is
# ORTHOGONAL to the paperbot Gateway mutex the pull itself takes: the Gateway mutex serialises
# use of the shared Gateway across ALL desk processes, while THIS lock guarantees at most one
# canslim pull-SUPERVISOR (and therefore at most one live pull child on clientId 43) exists at
# a time — no matter how many scheduled-task launchers fire. It lives in the LOCAL state dir,
# never on Drive (Drive's non-atomic sync would break O_EXCL). We reuse the proven
# paperbot.gateway_lock reclaim machinery (atomic O_EXCL create + dead-PID / stale-heartbeat
# reclaim) pointed at this dedicated path, so a crashed supervisor's lock is auto-reclaimed.
SINGLETON_LOCK = STATE / "ibkr_pull_supervisor.lock"

# ---- window ------------------------------------------------------------------------------
START = "2010-01-01"
# durationStr covering 2010-01-01 -> today. IBKR caps a single daily request well above
# ~17 years of bars, so one request per whatToShow spans the whole window.
DURATION = "17 Y"
BAR_SIZE = "1 day"

# ---- pacing (never trip 60 req / 10 min) -------------------------------------------------
PACE_WINDOW_SECS = 600          # the broker's rolling 10-min window
PACE_MAX_REQS = 55              # stay under 60 with margin
MIN_REQ_GAP_SECS = 10.5         # ~1 request / 10.5 s => ~57 / 10 min ceiling (HISTORICAL only)
# reqContractDetails is NOT a historical-data request and does NOT consume the 60/10-min
# historical budget, so resolution gets its own light gap (avoid hammering, but ~85 min for
# the whole 16.7k universe, not 48 h at the historical pace).
RESOLVE_GAP_SECS = 0.25
HEARTBEAT_EVERY = 10            # symbols between heartbeat rewrites
CONNECT_RETRIES = 6             # reconnect attempts on a dropped Gateway
RECONNECT_BACKOFF = 10          # base backoff seconds between reconnects

# ---- terminal-skip (permanently-unpullable, e.g. IBKR error 162) -------------------------
# After this many CONSECUTIVE runs where a resolved symbol returns no bars, treat it as
# permanently unpullable and stop retrying it every run. 2 is enough: error 162 is a hard,
# deterministic permission/no-history denial, not a transient farm hiccup — but requiring two
# consecutive misses guards against a single Gateway/farm blip flagging a good symbol.
TERMINAL_SKIP_AFTER = 2

# ---- watchdog ----------------------------------------------------------------------------
STALE_HEARTBEAT_SECS = 900      # a heartbeat older than this = hung run -> relaunch
WATCHDOG_POLL_SECS = 60


# ==========================================================================================
# small utils
# ==========================================================================================

def _log(msg: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG_TXT, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _heartbeat(phase: str, done: int, total: int, last: str, note: str = "") -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "phase": phase, "done_on_disk": done, "total": total,
           "remaining": max(0, total - done), "last_symbol": last, "note": note,
           "pid": os.getpid()}
    tmp = HEARTBEAT_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=2))
    os.replace(tmp, HEARTBEAT_JSON)


def _atomic_write_parquet(df: pd.DataFrame, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, dest)   # atomic on same filesystem


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def _save_json(path: Path, obj) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def _candidates() -> list[str]:
    if not CANDIDATES_CSV.exists():
        raise SystemExit(f"missing {CANDIDATES_CSV} — run full_market_universe.py candidates")
    df = pd.read_csv(CANDIDATES_CSV)
    return sorted(df["ticker"].astype(str).str.upper().unique().tolist())


def _done_symbols(prices_dir: Path = PRICES) -> set[str]:
    """Symbols already on disk with a non-empty parquet (Tiingo OR IBKR sourced)."""
    out: set[str] = set()
    if not prices_dir.exists():
        return out
    for p in prices_dir.glob("*.parquet"):
        try:
            if p.stat().st_size > 0:
                out.add(p.stem.upper())
        except OSError:
            pass
    return out


def _terminal_skip() -> set[str]:
    """Symbols promoted to PERMANENTLY-UNPULLABLE (>= TERMINAL_SKIP_AFTER consecutive empty
    pulls, e.g. IBKR error 162). These resolve but can never yield bars, so they are NOT
    counted as outstanding work — they are deferred to the paid source with the unresolved set.
    The ledger stores {symbol: {"empties": n, "terminal": bool, "why": str, "ts": iso}}."""
    ledger = _load_json(TERMINAL_JSON, {})
    return {s for s, v in ledger.items() if isinstance(v, dict) and v.get("terminal")}


def _record_empty(symbol: str) -> bool:
    """Bump the consecutive-empty counter for `symbol`; promote to terminal-skip once it
    reaches TERMINAL_SKIP_AFTER. Returns True iff the symbol is now terminal (newly or
    already). Idempotent + crash-safe (atomic JSON write)."""
    ledger = _load_json(TERMINAL_JSON, {})
    rec = ledger.get(symbol) if isinstance(ledger.get(symbol), dict) else {}
    empties = int(rec.get("empties", 0)) + 1
    terminal = empties >= TERMINAL_SKIP_AFTER
    ledger[symbol] = {
        "empties": empties,
        "terminal": terminal,
        "why": "resolved but no bars (IBKR err 162: no data-permission / no history)",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save_json(TERMINAL_JSON, ledger)
    return terminal


def _clear_empty(symbol: str) -> None:
    """A symbol that finally returned bars is no longer a candidate for terminal-skip —
    drop it from the ledger so its counter cannot linger."""
    ledger = _load_json(TERMINAL_JSON, {})
    if symbol in ledger:
        del ledger[symbol]
        _save_json(TERMINAL_JSON, ledger)


def _remaining_symbols(cands: list[str] | None = None) -> list[str]:
    """The single source of truth for 'what real work is left': resolved survivors that are
    neither on disk NOR terminal-skipped. Every completion check (pull/watchdog/launcher/
    status) uses THIS so they can never disagree."""
    if cands is None:
        cands = _candidates()
    resolved = _load_json(RESOLVED_JSON, {})
    done = _done_symbols()
    terminal = _terminal_skip()
    return [s for s in cands if s in resolved and s not in done and s not in terminal]


# ==========================================================================================
# pacing throttle — a sliding-window token bucket over the last PACE_WINDOW_SECS
# ==========================================================================================

class Pacer:
    """Blocks so that (a) requests are >= MIN_REQ_GAP_SECS apart and (b) no more than
    PACE_MAX_REQS fire inside any rolling PACE_WINDOW_SECS window. One .wait() per request."""

    def __init__(self) -> None:
        self._stamps: deque[float] = deque()
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        # min-gap spacing
        gap = now - self._last
        if gap < MIN_REQ_GAP_SECS:
            time.sleep(MIN_REQ_GAP_SECS - gap)
            now = time.monotonic()
        # sliding-window cap
        while self._stamps and now - self._stamps[0] > PACE_WINDOW_SECS:
            self._stamps.popleft()
        if len(self._stamps) >= PACE_MAX_REQS:
            sleep_for = PACE_WINDOW_SECS - (now - self._stamps[0]) + 0.5
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            while self._stamps and now - self._stamps[0] > PACE_WINDOW_SECS:
                self._stamps.popleft()
        self._stamps.append(now)
        self._last = now


# ==========================================================================================
# IBKR connection + per-symbol pull
# ==========================================================================================

def _connect(readonly: bool = True):
    """Read-only connect to the paper Gateway on clientId 43 (launch if down)."""
    return gw.connect(CLIENT, readonly=readonly, launch=True, timeout=20)


def _resolve_symbol(ib, symbol: str):
    """Return a qualified US-equity Contract for `symbol` via reqContractDetails, or None
    if IBKR cannot resolve it (delisted / foreign / not a SMART US common). READ-ONLY."""
    from ib_async import Stock
    try:
        stk = Stock(symbol, "SMART", "USD")
        details = ib.reqContractDetails(stk)
    except Exception:
        return None
    if not details:
        return None
    # Prefer a common stock (STK) primary US listing; take the first qualified contract.
    for d in details:
        c = d.contract
        if c.secType == "STK":
            return c
    return details[0].contract


def _hist(ib, contract, what: str):
    """One reqHistoricalData call for the whole window. Returns list of bars (possibly []).
    Caller paces BEFORE calling. Raises on connection loss so the caller can reconnect."""
    return ib.reqHistoricalData(
        contract, endDateTime="", durationStr=DURATION, barSizeSetting=BAR_SIZE,
        whatToShow=what, useRTH=True, formatDate=1, timeout=60)


def _bars_to_frame(trades, adjusted) -> pd.DataFrame | None:
    """Merge TRADES (raw OHLCV+vol) with ADJUSTED_LAST (adj close) into the shared schema."""
    if not trades:
        return None
    tr = pd.DataFrame([{
        "date": pd.Timestamp(b.date),
        "open": float(b.open), "high": float(b.high),
        "low": float(b.low), "close": float(b.close),
        "volume": int(b.volume) if b.volume is not None and b.volume >= 0 else 0,
    } for b in trades])
    tr["date"] = pd.to_datetime(tr["date"]).dt.tz_localize(None)
    if adjusted:
        adj = pd.DataFrame([{"date": pd.Timestamp(b.date), "adj_close": float(b.close)}
                            for b in adjusted])
        adj["date"] = pd.to_datetime(adj["date"]).dt.tz_localize(None)
        out = tr.merge(adj, on="date", how="left")
        # any date missing an adjusted close falls back to raw close
        out["adj_close"] = out["adj_close"].fillna(out["close"])
    else:
        out = tr.copy()
        out["adj_close"] = out["close"]
    out = out[out["date"] >= pd.Timestamp(START)]
    if out.empty:
        return None
    out["source"] = "ibkr"
    out = out[["date", "open", "high", "low", "close", "volume", "adj_close", "source"]]
    return out.sort_values("date").reset_index(drop=True)


def _pull_one(ib, contract, pacer: Pacer) -> tuple[pd.DataFrame | None, bool]:
    """Pull TRADES + ADJUSTED_LAST for a resolved contract. Returns (frame, adj_ok).
    adj_ok False => adj_close fell back to raw close (logged by the caller)."""
    pacer.wait()
    trades = _hist(ib, contract, "TRADES")
    adj_ok = True
    adjusted = []
    try:
        pacer.wait()
        adjusted = _hist(ib, contract, "ADJUSTED_LAST")
        if not adjusted:
            adj_ok = False
    except Exception:
        adj_ok = False
        adjusted = []
    return _bars_to_frame(trades, adjusted), adj_ok


# ==========================================================================================
# resolve pass
# ==========================================================================================

def resolve(ib=None, symbols: list[str] | None = None) -> tuple[dict, dict]:
    """Resolve every not-yet-resolved candidate against IBKR. Persists results
    incrementally to RESOLVED_JSON / UNRESOLVED_JSON so a restart continues. Returns
    (resolved, unresolved) dicts keyed by symbol."""
    own = ib is None
    if own:
        ib = _connect()
    try:
        cands = symbols if symbols is not None else _candidates()
        resolved = _load_json(RESOLVED_JSON, {})
        unresolved = _load_json(UNRESOLVED_JSON, {})
        seen = set(resolved) | set(unresolved)
        todo = [s for s in cands if s not in seen]
        _log(f"RESOLVE start: {len(cands):,} candidates, {len(seen):,} already classified, "
             f"{len(todo):,} to resolve")
        # reqContractDetails does NOT count against the historical 60/10-min budget, so use a
        # light fixed gap (not the historical Pacer) — resolving 16.7k names in ~85 min.
        for i, sym in enumerate(todo):
            time.sleep(RESOLVE_GAP_SECS)
            c = _resolve_symbol(ib, sym)
            if c is None:
                unresolved[sym] = {"why": "unresolved",
                                   "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            else:
                resolved[sym] = {"conId": c.conId, "primaryExchange": c.primaryExchange,
                                 "currency": c.currency, "symbol": c.symbol}
            if (i + 1) % 50 == 0:
                _save_json(RESOLVED_JSON, resolved)
                _save_json(UNRESOLVED_JSON, unresolved)
                _heartbeat("resolve", len(resolved), len(cands), sym,
                           note=f"resolved {len(resolved)} / unresolved {len(unresolved)}")
                _log(f"  resolve progress: {i+1}/{len(todo)} classified; "
                     f"resolved={len(resolved)} unresolved={len(unresolved)} last={sym}")
        _save_json(RESOLVED_JSON, resolved)
        _save_json(UNRESOLVED_JSON, unresolved)
        _log(f"RESOLVE end: resolved={len(resolved)} unresolved={len(unresolved)}")
        return resolved, unresolved
    finally:
        if own:
            try:
                ib.disconnect()
            except Exception:
                pass


# ==========================================================================================
# pull pass (resumable)
# ==========================================================================================

def pull(run_limit: int | None = None) -> None:
    PRICES.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)

    try:
        from paperbot.gateway_lock import GatewayBusySkip, gateway_lock  # noqa
    except Exception:
        # paperbot on sys.path directly (added above); import flat
        from gateway_lock import GatewayBusySkip, gateway_lock  # type: ignore

    cands = _candidates()
    resolved = _load_json(RESOLVED_JSON, {})

    # Take the Gateway mutex for the WHOLE session so we yield to the monitor/rebalance.
    try:
        lock_cm = gateway_lock(purpose="canslim_price_gapfill",
                               client_id=clientids.get(CLIENT), on_busy="skip")
    except Exception as e:
        _log(f"could not build gateway lock ({e}); aborting to be safe.")
        return

    try:
        with lock_cm:
            ib = _connect()
            try:
                # resolve first (idempotent; skips already-classified)
                resolved, unresolved = resolve(ib=ib, symbols=cands)
                done = _done_symbols()
                terminal = _terminal_skip()
                # real work = resolved AND not-on-disk AND not permanently-unpullable.
                todo = _remaining_symbols(cands)
                total_target = len([s for s in cands if s in resolved])
                _log(f"PULL start: {len(resolved):,} resolved survivors, "
                     f"{len(done):,} already on disk, {len(terminal):,} terminal-skip "
                     f"(permanently unpullable), {len(todo):,} to pull "
                     f"(unresolved/deferred={len(unresolved):,})")
                # Fresh heartbeat BEFORE the first symbol. Critical: the watchdog kills a child
                # whose heartbeat is older than STALE_HEARTBEAT_SECS. If the previous run left a
                # stale heartbeat, this stamps it fresh immediately so a short (e.g. all-empty)
                # run is never killed before it can do — and record — its work. Without this the
                # watchdog killed each child at the first 60s poll, starving all forward progress.
                _heartbeat("pull", len(done), total_target, "(run start)",
                           note=f"{len(todo)} to pull, {len(terminal)} terminal-skip")
                pacer = Pacer()
                pulled = 0
                adj_fallbacks = 0
                empties = 0
                for i, sym in enumerate(todo):
                    if run_limit is not None and pulled >= run_limit:
                        _log(f"run_limit {run_limit} reached — stopping cleanly (resumable).")
                        break
                    meta = resolved[sym]
                    frame, adj_ok = _pull_with_reconnect(ib, sym, meta, pacer)
                    if frame is None or frame.empty:
                        empties += 1
                        now_terminal = _record_empty(sym)
                        if now_terminal:
                            _log(f"  [terminal-skip] {sym}: resolved but no bars after "
                                 f"{TERMINAL_SKIP_AFTER} runs (IBKR err 162: no data-permission"
                                 f" / no history) — will NOT retry; deferred to paid source")
                        else:
                            _log(f"  [empty] {sym}: resolved but no bars returned "
                                 f"(recorded, one more empty run promotes to terminal-skip)")
                    else:
                        _clear_empty(sym)
                        _atomic_write_parquet(frame, PRICES / f"{sym}.parquet")
                        pulled += 1
                        if not adj_ok:
                            adj_fallbacks += 1
                            _log(f"  [adj-fallback] {sym}: ADJUSTED_LAST empty, adj_close=raw close")
                    # Refresh the heartbeat EVERY symbol (cheap, atomic). A resolved-but-empty
                    # symbol writes no parquet, so a run that is ALL empties (the terminal-skip
                    # tail) would otherwise let the heartbeat age past STALE_HEARTBEAT_SECS and
                    # get needlessly killed. A per-symbol beat keeps a slow, honest run alive.
                    ndone = len(done) + pulled
                    _heartbeat("pull", ndone, total_target, sym,
                               note=f"this run +{pulled} pulled, {empties} empty, "
                                    f"{adj_fallbacks} adj-fallback")
                    if (pulled + empties) % HEARTBEAT_EVERY == 0:
                        _log(f"  progress: +{pulled} pulled, {empties} empty, "
                             f"{adj_fallbacks} adj-fallback this run; "
                             f"{ndone:,}/{total_target:,} survivors on disk; last={sym}")
                    ib.sleep(0)  # let ib_async service the event loop
                ndone = len(done) + pulled
                _heartbeat("pull", ndone, total_target, "(run end)",
                           note=f"run complete: +{pulled} pulled, {empties} empty, "
                                f"{adj_fallbacks} adj-fallback")
                _log(f"PULL end: +{pulled} pulled, {empties} empty, {adj_fallbacks} "
                     f"adj-fallback this run; {ndone:,}/{total_target:,} survivors on disk "
                     f"({100*ndone/max(1,total_target):.1f}%).")
            finally:
                try:
                    ib.disconnect()
                except Exception:
                    pass
    except GatewayBusySkip as e:
        _log(f"Gateway busy ({e}); yielding — will resume on the next run.")
        return


def _pull_with_reconnect(ib, sym: str, meta: dict, pacer: Pacer):
    """Pull one symbol; on a dropped connection, reconnect (bounded) and retry the symbol."""
    from ib_async import Stock
    contract = Stock(sym, "SMART", "USD")
    try:
        ib.qualifyContracts(contract)
    except Exception:
        pass
    for attempt in range(CONNECT_RETRIES):
        try:
            if not ib.isConnected():
                raise ConnectionError("gateway not connected")
            return _pull_one(ib, contract, pacer)
        except Exception as e:
            wait = RECONNECT_BACKOFF * (attempt + 1)
            _log(f"  [conn] {sym}: {type(e).__name__}: {e} — reconnect in {wait}s "
                 f"(attempt {attempt+1}/{CONNECT_RETRIES})")
            try:
                ib.disconnect()
            except Exception:
                pass
            time.sleep(wait)
            # reconnect the SAME ib object (readonly, launch the gateway if it fell over)
            try:
                if not gw.gateway_running():
                    gw.ensure_gateway()
                ib.connect(gw.HOST, gw.PAPER_PORT, clientId=clientids.get(CLIENT),
                           readonly=True, timeout=20)
                ib.qualifyContracts(contract)
            except Exception as e2:
                _log(f"  [conn] {sym}: reconnect attempt failed: {e2}")
    _log(f"  [give-up] {sym}: could not pull after {CONNECT_RETRIES} reconnect attempts")
    return None, True


# ==========================================================================================
# smoke test (LIVE gateway, throwaway output dir, prints the proof)
# ==========================================================================================

def smoke(n: int = 10) -> int:
    """End-to-end LIVE proof on N real universe symbols: resolve -> pull TRADES+ADJUSTED
    -> write parquet to a TEMP dir -> re-read and assert the schema matches the Tiingo
    files exactly and the data is sane. Returns 0 on clean, non-zero on any problem.
    Does NOT touch the real warehouse."""
    import tempfile
    from ib_async import Stock

    # pick real, liquid, obviously-tradable survivors that also exist in the universe
    universe = set(_candidates())
    preferred = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "JPM", "XOM", "KO", "PG", "WMT",
                 "AA", "A", "AAL", "AAON"]
    picks = [s for s in preferred if s in universe][:n]
    if len(picks) < n:
        picks += [s for s in sorted(universe) if s not in picks][: n - len(picks)]
    picks = picks[:n]

    # the exact reference schema, from a real Tiingo file on disk
    ref_cols = ["date", "open", "high", "low", "close", "volume", "adj_close", "source"]
    ref_file = None
    for cand in ("AAPL.parquet", "A.parquet"):
        if (PRICES / cand).exists():
            ref_file = PRICES / cand
            break
    ref_dtypes = None
    if ref_file is not None:
        rdf = pd.read_parquet(ref_file)
        ref_cols = list(rdf.columns)
        ref_dtypes = {c: str(rdf[c].dtype) for c in rdf.columns}

    print(f"SMOKE TEST — {len(picks)} symbols: {picks}", flush=True)
    print(f"reference schema (from {ref_file}): {ref_cols}", flush=True)
    if ref_dtypes:
        print(f"reference dtypes: {ref_dtypes}", flush=True)

    ib = _connect()
    tmpdir = Path(tempfile.mkdtemp(prefix="ibkr_smoke_"))
    problems: list[str] = []
    pacer = Pacer()
    try:
        for sym in picks:
            c = _resolve_symbol(ib, sym)
            if c is None:
                problems.append(f"{sym}: UNRESOLVED (unexpected for a liquid survivor)")
                print(f"  {sym}: UNRESOLVED", flush=True)
                continue
            contract = Stock(sym, "SMART", "USD")
            try:
                ib.qualifyContracts(contract)
            except Exception:
                pass
            frame, adj_ok = _pull_one(ib, contract, pacer)
            if frame is None or frame.empty:
                problems.append(f"{sym}: NO BARS returned")
                print(f"  {sym}: NO BARS", flush=True)
                continue
            dest = tmpdir / f"{sym}.parquet"
            _atomic_write_parquet(frame, dest)
            back = pd.read_parquet(dest)   # re-read from disk (round-trip proof)

            # ---- schema checks ----
            if list(back.columns) != ref_cols:
                problems.append(f"{sym}: COLUMN MISMATCH {list(back.columns)} != {ref_cols}")
            if ref_dtypes:
                for col, want in ref_dtypes.items():
                    got = str(back[col].dtype)
                    if got != want:
                        problems.append(f"{sym}: dtype {col} {got} != {want}")
            # ---- sanity checks ----
            n_rows = len(back)
            dmin, dmax = back["date"].min(), back["date"].max()
            nan_close = int(back["close"].isna().sum())
            nan_adj = int(back["adj_close"].isna().sum())
            allzero_vol = bool((back["volume"] == 0).all())
            if n_rows < 500:
                problems.append(f"{sym}: only {n_rows} rows (<500 for a 2010+ survivor)")
            if nan_close or nan_adj:
                problems.append(f"{sym}: NaN close={nan_close} adj_close={nan_adj}")
            if allzero_vol:
                problems.append(f"{sym}: all-zero volume")
            if pd.Timestamp(dmax) < pd.Timestamp("2026-01-01"):
                problems.append(f"{sym}: stale — max date {dmax} < 2026")

            print(f"  {sym}: rows={n_rows}  {str(dmin)[:10]}..{str(dmax)[:10]}  "
                  f"close[-1]={back['close'].iloc[-1]:.2f}  adj[-1]={back['adj_close'].iloc[-1]:.2f}  "
                  f"vol[-1]={int(back['volume'].iloc[-1]):,}  adj_ok={adj_ok}", flush=True)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    print("\n=== SMOKE RESULT ===", flush=True)
    if problems:
        print(f"FAIL — {len(problems)} problem(s):", flush=True)
        for p in problems:
            print(f"  - {p}", flush=True)
        return 1
    print("CLEAN — schema matches Tiingo exactly, data sane, no pacing violation, "
          "read-only connect.", flush=True)
    return 0


# ==========================================================================================
# status
# ==========================================================================================

def status() -> None:
    cands = _candidates()
    resolved = _load_json(RESOLVED_JSON, {})
    unresolved = _load_json(UNRESOLVED_JSON, {})
    done = _done_symbols()
    terminal = _terminal_skip()
    ibkr_on_disk = 0
    for p in PRICES.glob("*.parquet"):
        try:
            src = pd.read_parquet(p, columns=["source"])
            if len(src) and str(src["source"].iloc[0]) == "ibkr":
                ibkr_on_disk += 1
        except Exception:
            pass
    resolved_remaining = _remaining_symbols(cands)
    print("IBKR SURVIVOR PRICE GAP-FILL — STATUS")
    print(f"  universe candidates      : {len(cands):,}")
    print(f"  resolved (survivors)     : {len(resolved):,}")
    print(f"  unresolved (deferred)    : {len(unresolved):,}")
    print(f"  on disk (any source)     : {len(done):,}")
    print(f"  on disk sourced=ibkr     : {ibkr_on_disk:,}")
    print(f"  terminal-skip (err 162)  : {len(terminal):,}  (permanently unpullable, deferred)")
    print(f"  resolved & still to pull : {len(resolved_remaining):,}")
    print(f"  COMPLETE                 : {_is_complete()}")
    if HEARTBEAT_JSON.exists():
        hb = _load_json(HEARTBEAT_JSON, {})
        age = "?"
        try:
            ts = datetime.fromisoformat(hb["ts"])
            age = f"{(datetime.now(timezone.utc) - ts).total_seconds():.0f}s ago"
        except Exception:
            pass
        print(f"  last heartbeat           : {hb.get('phase')} {hb.get('last_symbol')} ({age})")


# ==========================================================================================
# singleton guard + launcher (the scheduled-task entry point)
# ==========================================================================================

def _supervisor_lock(wait_secs: float = 0.0):
    """A cross-process SINGLETON lock for the pull-supervisor role, built on the proven
    paperbot.gateway_lock reclaim machinery (atomic O_EXCL create + dead-PID / stale-heartbeat
    reclaim) but pointed at our OWN dedicated lock path (SINGLETON_LOCK), so it is orthogonal
    to the Gateway mutex the pull itself takes.

    wait_secs=0.0 => a single non-blocking attempt: if a LIVE supervisor already holds it
    (fresh heartbeat), we get GatewayBusySkip immediately and the caller no-ops. A crashed
    supervisor's lock (dead PID, or heartbeat silent > STALE_HEARTBEAT_SECS) is auto-reclaimed.
    """
    try:
        from paperbot.gateway_lock import gateway_lock  # noqa
    except Exception:
        from gateway_lock import gateway_lock  # type: ignore
    STATE.mkdir(parents=True, exist_ok=True)
    return gateway_lock(
        purpose="canslim_pull_supervisor",
        client_id=clientids.get(CLIENT),
        on_busy="skip",
        wait_secs=wait_secs,
        lock_path=str(SINGLETON_LOCK),
    )


def _pid_is_alive(pid) -> bool:
    """True if a process with this pid is running. Reuses the proven paperbot.gateway_lock
    liveness check (tasklist on Windows; assume-alive on ambiguity — conservative, so we
    never start a second pull when unsure)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        from paperbot.gateway_lock import _pid_alive  # noqa
    except Exception:
        from gateway_lock import _pid_alive  # type: ignore
    return _pid_alive(pid)


def _is_complete() -> bool:
    """True iff we've done a resolve pass AND nothing resolved-but-PULLABLE remains — i.e.
    every resolved survivor is either on disk OR terminal-skipped (permanently unpullable,
    deferred to the paid source). A leftover scheduled task then cleanly no-ops."""
    resolved = _load_json(RESOLVED_JSON, {})
    if not resolved:
        return False  # no resolve pass yet -> there is work to do
    return len(_remaining_symbols()) == 0


def launcher() -> None:
    """The SCHEDULED-TASK entry point. Idempotent + singleton-safe by design:

      1. If the pull is already COMPLETE (resolve done, nothing left) -> clean no-op exit.
      2. Acquire the cross-process SINGLETON lock with a NON-BLOCKING attempt. If a live
         supervisor (this session's watchdog, or a prior launcher) already holds it, we get
         GatewayBusySkip and NO-OP — we do NOT start a second competing pull. This is what
         makes a scheduled-task launcher fired while the session pull is alive a safe no-op,
         regardless of the 900s heartbeat-staleness logic (belt AND braces).
      3. Otherwise we hold the singleton and run watchdog() under it, which resumes from the
         on-disk checkpoint + skip-done. On exit the lock releases (context-manager, even on
         exception/crash), so the next scheduled trigger can take over cleanly.
    """
    try:
        from paperbot.gateway_lock import GatewayBusySkip  # noqa
    except Exception:
        from gateway_lock import GatewayBusySkip  # type: ignore

    if _is_complete():
        _log("LAUNCHER: survivor set already complete — nothing to do (no-op).")
        return

    # FRESH-HEARTBEAT PRE-CHECK — catches a live pull that does NOT hold the singleton lock.
    # A pull started before this launcher existed (e.g. this session's watchdog+pull, spawned
    # before the singleton guard was added) will not hold SINGLETON_LOCK, so the lock acquire
    # below would succeed and race it. But a live pull ALWAYS rewrites ibkr_heartbeat.json with
    # a fresh ts + its live pid. If that heartbeat is fresh AND its pid is alive, a pull is
    # already working — NO-OP. (Once the singleton launcher is the one running, its own
    # supervised child keeps the heartbeat fresh, so this stays a correct no-op across
    # overlapping triggers too.)
    hb = _load_json(HEARTBEAT_JSON, {})
    age = _heartbeat_age_secs()
    hb_pid = hb.get("pid")
    if age is not None and age < STALE_HEARTBEAT_SECS and _pid_is_alive(hb_pid):
        _log(f"LAUNCHER: a live pull is already running (heartbeat {age:.0f}s old, pid "
             f"{hb_pid} alive) — no-op; NOT starting a second pull.")
        return

    try:
        cm = _supervisor_lock(wait_secs=0.0)
    except Exception as e:
        _log(f"LAUNCHER: could not build singleton lock ({e}); aborting to be safe (no-op).")
        return

    try:
        with cm:
            _log(f"LAUNCHER: acquired singleton (pid {os.getpid()}) — supervising the pull.")
            watchdog()
    except GatewayBusySkip as e:
        _log(f"LAUNCHER: a live pull-supervisor already holds the singleton ({e}); "
             f"no-op — NOT starting a second pull.")
        return


# ==========================================================================================
# watchdog — relaunch the pull on crash or stale heartbeat
# ==========================================================================================

def _heartbeat_age_secs() -> float | None:
    hb = _load_json(HEARTBEAT_JSON, {})
    try:
        ts = datetime.fromisoformat(hb["ts"])
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


def watchdog() -> None:
    """Supervise a pull subprocess: relaunch on crash (non-zero exit) and on a stale
    heartbeat (killed + relaunched). Exits when the pull reports the survivor set complete."""
    py = sys.executable
    script = str(Path(__file__).resolve())
    _log("WATCHDOG start")
    while True:
        # complete? (nothing resolved-AND-PULLABLE left: on-disk + terminal-skip both count
        # as "done", so the permanently-unpullable err-162 tail can no longer wedge the loop)
        cands = _candidates()
        resolved = _load_json(RESOLVED_JSON, {})
        remaining = _remaining_symbols(cands)
        # if we've done a resolve pass and there's nothing pullable left, STAND DOWN
        if resolved and not remaining:
            _log("WATCHDOG: survivor set complete (all resolved survivors on disk or "
                 "terminal-skipped) — standing down.")
            return

        _log(f"WATCHDOG: launching pull ({len(remaining):,} survivors remaining "
             f"or resolve pass pending)")
        proc = subprocess.Popen([py, script, "pull"], cwd=str(_REPO / "canslim"))
        # monitor liveness
        while True:
            try:
                rc = proc.wait(timeout=WATCHDOG_POLL_SECS)
            except subprocess.TimeoutExpired:
                age = _heartbeat_age_secs()
                if age is not None and age > STALE_HEARTBEAT_SECS:
                    _log(f"WATCHDOG: heartbeat stale ({age:.0f}s > {STALE_HEARTBEAT_SECS}s) "
                         f"— killing + relaunching pull")
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break
                continue
            # process exited
            if rc == 0:
                _log("WATCHDOG: pull exited 0 — re-checking remaining; will stop if complete.")
            else:
                _log(f"WATCHDOG: pull exited {rc} — relaunching after backoff.")
                time.sleep(30)
            break
        time.sleep(5)


# ==========================================================================================
# main
# ==========================================================================================

def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "pull"
    if cmd == "smoke":
        n = int(args[1]) if len(args) > 1 else 10
        sys.exit(smoke(n))
    elif cmd == "resolve":
        resolve()
    elif cmd == "status":
        status()
    elif cmd == "watchdog":
        watchdog()
    elif cmd == "launcher":
        launcher()
    elif cmd == "pull":
        run_limit = None
        if "--limit" in args:
            run_limit = int(args[args.index("--limit") + 1])
        pull(run_limit=run_limit)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
