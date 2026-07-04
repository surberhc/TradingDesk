r"""
universe_download.py — STANDALONE, resumable bulk downloader for the EXPANDED
options universe (universe_config.UNIVERSE, ~140 roots), two layers:

  LAYER 1  EOD greeks + open_interest  (same schema/tree as the existing warehouse
           raw/options/{SYMBOL}/{YYYYMMDD}.parquet). Reuses download.pull_day and
           storage.write_day/have_day verbatim, so byte-for-byte the same product;
           the 50 roots already on disk only grow by missing days, the 90 new roots
           fill from scratch.

  LAYER 2  Fixed-time consistent NBBO SNAPSHOTS (NEW tree). For each (symbol, day)
           we pull the quote endpoint's 15m grid per live expiration inside the
           0-60 DTE window, KEEP only the near-money band (+/-15% of the day's spot)
           and only the target minutes (10:00/12:00/14:00/15:45 ET), and write
               raw/options_snap/{SYMBOL}/{HHMM}/{YYYYMMDD}.parquet
           All legs at ONE instant — fixing the greeks/eod timing problem (that
           endpoint stamps each contract's quote at ITS own last-activity time;
           verified 13:57 vs 15:58 on the same day).

RESUMABLE / IDEMPOTENT (the whole point):
  * Unit of work = (symbol, day, layer). A layer's day is DONE when its file(s)
    are present. Re-launch skips done work and continues exactly where it stopped.
  * EOD done      = raw/options/{SYM}/{DAY}.parquet exists (storage.have_day).
  * SNAP done     = ALL four raw/options_snap/{SYM}/{HHMM}/{DAY}.parquet exist,
                    OR a 0-row marker (a legit no-data / pre-listing day).
  * Atomic writes (temp + os.replace) so a kill mid-write never leaves a torn file.

PARALLELISM: K=4 shards over the ROOT list (memory: one terminal sustains ~4
shards / ~2.85x, no gain past 4). Roots are independent, so shards never collide.
Each shard is a detached child running THIS script with --shard i/K over a
disjoint root slice; the parent monitors, restarts a dead shard, writes a combined
heartbeat, and is a good citizen (backs off on 429/error bursts).

RUN ORDER (priority): --layer eod first (fast, unlocks analysis), then --layer snap,
or --layer both. --only-new to skip the 50 already-warehoused roots on the EOD pass.

READ-ONLY to the running terminal / collector / scheduled tasks — pure HTTP GETs.
NEW files only; config.py + the frozen warehouse scope untouched.

USAGE
  # calibration (what this session runs): 3 names, one month, foreground, no shards
  python universe_download.py --roots SPXW AAPL TSLA \
      --start 20230601 --end 20230630 --layer both --shards 1 --calibrate

  # full pull (AWAITS APPROVAL — do not run yet):
  python universe_download.py --layer eod            # priority 1
  python universe_download.py --layer snap           # priority 2

ASCII-only console output (Windows cp1252 console).
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import requests

import config
import storage
import universe_config as uni
import download as eod_dl          # reuse pull_day (greeks join OI) verbatim

# --------------------------------------------------------------------------- #
# Paths — snapshot tree is NEW; EOD tree is the existing warehouse.
# --------------------------------------------------------------------------- #
SNAP_ROOT = config.DATA_ROOT / "raw" / "options_snap"
STATE_DIR = config.DATA_ROOT / "universe_dl_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

# Durable, machine-side reliability artifacts (read by the independent staleness alarm
# and the scheduled-task launcher). Kept LOCAL (never on Drive).
HEARTBEAT = STATE_DIR / "universe_dl_heartbeat.txt"      # mtime advances per sym-day; "COMPLETE" on finish
COMBINED = STATE_DIR / "universe_dl_progress.json"       # {done_units,total_units,pct,...}
SINGLETON_LOCK = STATE_DIR / "universe_dl_supervisor.lock"
SUPERVISOR_LOG = STATE_DIR / "universe_dl_supervisor.log"

HERE = config.CODE_ROOT
THIS = HERE / "universe_download.py"

QUOTE_TIMEOUT = 180
LIST_TIMEOUT = 120
HTTP_RETRIES = 5

PY = getattr(sys, "_base_executable", None) or sys.executable
if os.name == "nt":
    DETACH_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
else:
    DETACH_FLAGS = 0


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def _log(logpath: Path, msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        logpath.parent.mkdir(parents=True, exist_ok=True)
        with open(logpath, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _write_heartbeat(text: str) -> None:
    """Rewrite the top-level heartbeat file (mtime = proof of forward progress).

    The independent staleness alarm reads this file's mtime + text. The supervisor
    rewrites it every monitor tick with the latest progress; on full completion it
    writes a line containing 'COMPLETE' so the alarm suppresses (legit finish, not a
    stall). Atomic write so a kill can never leave a torn heartbeat. Never raises.
    """
    try:
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tmp = HEARTBEAT.with_name(HEARTBEAT.name + ".tmp")
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(f"{stamp}  {text}\n")
        os.replace(tmp, HEARTBEAT)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #
def _daystr(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def _dashed(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")


def _business_days(start: str, end: str) -> list[dt.date]:
    rng = pd.bdate_range(pd.to_datetime(start), pd.to_datetime(end))
    return [d.date() for d in rng]


def _default_end() -> str:
    # Up to YESTERDAY — the terminal rejects the current day's expiration=* (400).
    return _daystr(dt.date.today() - dt.timedelta(days=1))


# --------------------------------------------------------------------------- #
# Low-level HTTP -> DataFrame (own retry). Mirrors the other collectors.
# --------------------------------------------------------------------------- #
def _get_csv(path: str, params: dict, timeout: int) -> pd.DataFrame:
    url = f"{config.THETA_BASE_URL}{path}"
    params = {**params, "format": "csv"}
    last = None
    for attempt in range(HTTP_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                if not r.text.strip():
                    return pd.DataFrame()
                # "No data found for your request" is a plaintext 200 body.
                if r.text.lstrip().lower().startswith("no data"):
                    return pd.DataFrame()
                return pd.read_csv(io.StringIO(r.text))
            if r.status_code in (472, 404):
                return pd.DataFrame()
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            last = repr(e)
        time.sleep(min(4 * (attempt + 1), 20))
    raise RuntimeError(f"GET {url} failed after {HTTP_RETRIES} tries: {last}")


def connected() -> bool:
    """Any HTTP reply from the terminal counts as up (matches the other clients)."""
    try:
        requests.get(f"{config.THETA_BASE_URL}/option/history/quote",
                     params={"symbol": "X"}, timeout=5)
        return True
    except requests.RequestException:
        return False


# --------------------------------------------------------------------------- #
# Atomic parquet write
# --------------------------------------------------------------------------- #
def _write_atomic(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, engine="pyarrow", compression="zstd", index=False)
    os.replace(tmp, path)


# =========================================================================== #
# LAYER 1 — EOD (reuse the existing warehouse code path verbatim)
# =========================================================================== #
def eod_have(symbol: str, d: dt.date) -> bool:
    return storage.have_day(symbol, _daystr(d))


def eod_pull_and_write(symbol: str, d: dt.date) -> int:
    """Reuse download.pull_day (greeks join open_interest); write the parquet directly.

    Byte-identical PARQUET to the existing warehouse product (same schema, same atomic
    temp+os.replace, same per-(symbol,day) path via storage.partition_path). A 0-row
    holiday file is a valid 'done' marker exactly as in download.main.

    We do NOT call storage.write_day here: that also updates the SINGLE shared
    raw/options/_manifest.json, and with K=4 shards racing to os.replace one global file
    Windows raises PermissionError (verified in the first launch). The manifest is
    non-authoritative — have_day()/eod_have() key off FILE PRESENCE, not the manifest,
    and the DuckDB catalog is rebuilt from the parquet tree — so writing the parquet and
    skipping the manifest is fully correct and collision-free. The manifest is rebuilt
    once at the end of the whole pull (see storage.rebuild_catalog / a manifest rebuild).
    """
    ds = _daystr(d)
    df = eod_dl.pull_day(symbol, ds)              # same JOIN_KEYS / DROP_COLS / schema
    _write_atomic(storage.partition_path(symbol, ds), df)   # per-symbol-day; no shared file
    return len(df)


# =========================================================================== #
# LAYER 2 — fixed-time consistent NBBO snapshots (NEW)
# =========================================================================== #
SNAP_KEEP_COLS = ["symbol", "expiration", "strike", "right", "timestamp",
                  "bid", "bid_size", "ask", "ask_size"]


def _snap_path(symbol: str, hhmm: str, d: dt.date) -> Path:
    return SNAP_ROOT / symbol / hhmm.replace(":", "") / f"{_daystr(d)}.parquet"


def snap_have(symbol: str, d: dt.date) -> bool:
    """DONE = all four target-time files present (0-row markers count as done)."""
    return all(_snap_path(symbol, t, d).exists() for t in uni.SNAP_TIMES)


def _day_spot(symbol: str, d: dt.date) -> tuple[float | None, list[str]]:
    """Return (underlying_price, live_expirations dashed) from ONE greeks/eod call.

    The greeks/eod expiration=* call gives us BOTH the day's consistent settlement
    spot (underlying_price) and the full expiration list in a single request — no
    extra round-trip. Returns (None, []) on a legit empty day (holiday/pre-listing).
    """
    df = _get_csv("/option/history/greeks/eod", {
        "symbol": symbol, "expiration": "*",
        "start_date": _dashed(d), "end_date": _dashed(d),
        "strike": "*", "right": "both",
        "rate_type": config.THETA_RATE_TYPE,
    }, timeout=LIST_TIMEOUT)
    if df.empty or "underlying_price" not in df.columns:
        return None, []
    spot = float(pd.to_numeric(df["underlying_price"], errors="coerce").dropna().median())
    exps = sorted({str(x) for x in df["expiration"].dropna().unique()})
    return (spot if spot > 0 else None), exps


def _dte_ok(exp_dashed: str, d: dt.date) -> bool:
    try:
        ed = dt.date.fromisoformat(exp_dashed)
    except ValueError:
        return False
    dte = (ed - d).days
    return uni.SNAP_DTE_MIN <= dte <= uni.SNAP_DTE_MAX


def snap_pull_day(symbol: str, d: dt.date) -> dict[str, pd.DataFrame]:
    """Return {HHMM: dataframe} of the near-money band at each target minute.

    The quote endpoint ACCEPTS expiration=* with a coarse interval (verified: SPXW's
    whole 15m chain — 37 expirations — comes back in ONE ~17s call, vs ~256s looping
    per-expiration). So we make ONE greeks/eod call (for the day's consistent spot)
    and ONE expiration=* quote call for the 15m grid, then filter IN MEMORY to:
      * the target minutes (10:00/12:00/14:00/15:45) — exact 15m grid boundaries,
      * the 0-60 DTE expirations, and
      * the near-money band (+/-15% of spot).
    Filtering after a single call trades a larger transfer (the full chain) for far
    fewer round-trips — a big net win on the heavy indices where the per-expiration
    loop dominated. Missing target-minute rows for a contract are simply absent (the
    grid guarantees the boundary row exists whenever the contract quoted).
    """
    out: dict[str, pd.DataFrame] = {t: pd.DataFrame() for t in uni.SNAP_TIMES}
    # Spot for the band (consistent settlement underlying_price). One greeks/eod call.
    spot, _ = _day_spot(symbol, d)
    if spot is None:
        return out                       # legit empty day -> caller writes 0-row markers
    lo, hi = spot * (1 - uni.SNAP_BAND_PCT), spot * (1 + uni.SNAP_BAND_PCT)
    ds_iso = _dashed(d)
    target_ts = {t: f"{ds_iso}T{t}:00.000" for t in uni.SNAP_TIMES}

    # ONE expiration=* call for the whole 15m grid.
    allrows = _get_csv("/option/history/quote", {
        "symbol": symbol, "expiration": "*",
        "start_date": ds_iso, "end_date": ds_iso,
        "strike": "*", "right": "both", "interval": uni.SNAP_INTERVAL,
    }, timeout=QUOTE_TIMEOUT)
    if allrows.empty or "strike" not in allrows.columns:
        return out

    # Filter 1: only the target-minute rows (collapses ~26 grid points to 4).
    allrows = allrows[allrows["timestamp"].astype(str).isin(target_ts.values())]
    if allrows.empty:
        return out
    # Filter 2: near-money band.
    strike = pd.to_numeric(allrows["strike"], errors="coerce")
    allrows = allrows[(strike >= lo) & (strike <= hi)]
    # Filter 3: 0-60 DTE window (vectorized over the exp column).
    if not allrows.empty:
        exp_dte_ok = allrows["expiration"].astype(str).map(lambda e: _dte_ok(e, d))
        allrows = allrows[exp_dte_ok]
    if allrows.empty:
        return out

    keep = [c for c in SNAP_KEEP_COLS if c in allrows.columns]
    for t, ts in target_ts.items():
        sub = allrows[allrows["timestamp"].astype(str) == ts]
        out[t] = sub[keep].reset_index(drop=True) if not sub.empty else pd.DataFrame()
    return out


def snap_pull_and_write(symbol: str, d: dt.date) -> int:
    """Pull one day's snapshots and write all four target-time files atomically.

    Writes a 0-row file for a target time that had no near-money rows (still a valid
    'done' marker so the day is never re-pulled). Returns total rows written.
    """
    frames = snap_pull_day(symbol, d)
    total = 0
    for t in uni.SNAP_TIMES:
        df = frames.get(t, pd.DataFrame())
        _write_atomic(_snap_path(symbol, t, d), df)
        total += len(df)
    return total


# =========================================================================== #
# WORKER — one shard: iterate its root slice x days x layer(s)
# =========================================================================== #
def _shard_roots(all_roots: list[str], shard: int, k: int) -> list[str]:
    """Round-robin slice so each shard gets a mix of heavy + light roots."""
    return [r for i, r in enumerate(all_roots) if i % k == shard]


def run_worker(roots: list[str], days: list[dt.date], layer: str,
               logpath: Path, progresspath: Path, calibrate: bool = False) -> dict:
    """Serial worker over (root, day, layer). Returns a stats dict (for calibration).

    Idempotent: skips any (root, day, layer) already done. Per-day try/except so one
    bad day never aborts. Writes a heartbeat JSON after each day.
    """
    if not connected():
        _log(logpath, f"TERMINAL NOT REACHABLE at {config.THETA_BASE_URL} -> abort")
        return {"error": "terminal_unreachable"}

    do_eod = layer in ("eod", "both")
    do_snap = layer in ("snap", "both")

    stats = {
        "eod_days": 0, "eod_secs": 0.0, "eod_bytes": 0,
        "snap_days": 0, "snap_secs": 0.0, "snap_bytes": 0,
        "errors": 0, "per_symbol_day": [],
    }
    total_units = len(roots) * len(days) * (int(do_eod) + int(do_snap))
    done_units = 0
    t_start = time.time()

    for symbol in roots:
        for d in days:
            # ---- EOD ----
            if do_eod:
                if eod_have(symbol, d):
                    done_units += 1
                else:
                    t0 = time.time()
                    try:
                        eod_pull_and_write(symbol, d)
                        secs = time.time() - t0
                        p = storage.partition_path(symbol, _daystr(d))
                        b = p.stat().st_size if p.exists() else 0
                        stats["eod_days"] += 1
                        stats["eod_secs"] += secs
                        stats["eod_bytes"] += b
                        if calibrate:
                            stats["per_symbol_day"].append(
                                {"sym": symbol, "day": _daystr(d), "layer": "eod",
                                 "secs": round(secs, 2), "bytes": b})
                    except Exception as e:      # noqa: BLE001
                        stats["errors"] += 1
                        _log(logpath, f"EOD FAIL {symbol} {_daystr(d)}: {e!r}")
                    done_units += 1

            # ---- SNAP ----
            if do_snap:
                if snap_have(symbol, d):
                    done_units += 1
                else:
                    t0 = time.time()
                    try:
                        snap_pull_and_write(symbol, d)
                        secs = time.time() - t0
                        b = sum((_snap_path(symbol, t, d).stat().st_size
                                 for t in uni.SNAP_TIMES
                                 if _snap_path(symbol, t, d).exists()), 0)
                        stats["snap_days"] += 1
                        stats["snap_secs"] += secs
                        stats["snap_bytes"] += b
                        if calibrate:
                            stats["per_symbol_day"].append(
                                {"sym": symbol, "day": _daystr(d), "layer": "snap",
                                 "secs": round(secs, 2), "bytes": b})
                    except Exception as e:      # noqa: BLE001
                        stats["errors"] += 1
                        _log(logpath, f"SNAP FAIL {symbol} {_daystr(d)}: {e!r}")
                    done_units += 1

            _write_progress(progresspath, done_units, total_units, symbol,
                            _daystr(d), stats, t_start)
        _log(logpath, f"root {symbol} done "
                      f"(eod+{stats['eod_days']} snap+{stats['snap_days']} "
                      f"err {stats['errors']})")
    return stats


def _write_progress(path: Path, done: int, total: int, sym: str, day: str,
                    stats: dict, t_start: float) -> None:
    elapsed = max(time.time() - t_start, 1e-9)
    rate = done / elapsed
    remaining = max(total - done, 0)
    eta_s = remaining / rate if rate > 0 else 0
    payload = {
        "updated": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        "done_units": done, "total_units": total,
        "pct": round(100.0 * done / total, 2) if total else 0.0,
        "current": f"{sym} {day}",
        "eod_days": stats["eod_days"], "snap_days": stats["snap_days"],
        "errors": stats["errors"],
        "eta": (f"{eta_s/3600:.1f}h (~{(dt.datetime.now()+dt.timedelta(seconds=eta_s)):%Y-%m-%d %H:%M})"
                if eta_s > 0 else ""),
    }
    try:
        tmp = path.with_name(path.name + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, path)
    except Exception:
        pass


# =========================================================================== #
# SUPERVISOR — launch K detached shard-children, monitor + restart + heartbeat
# =========================================================================== #
def run_supervisor(roots: list[str], start: str, end: str, layer: str,
                   k: int) -> int:
    log = SUPERVISOR_LOG
    combined = COMBINED
    _log(log, f"=== supervisor start === {len(roots)} roots | {start}..{end} | "
              f"layer={layer} | K={k}")
    _write_heartbeat(f"supervisor start: {len(roots)} roots {start}..{end} layer={layer} K={k}")

    def child_cmd(shard: int) -> list[str]:
        return [PY, "-u", str(THIS),
                "--roots", *roots,
                "--start", start, "--end", end,
                "--layer", layer,
                "--shard", str(shard), "--of", str(k),
                "--child"]

    def child_env() -> dict:
        env = os.environ.copy()
        site = next((p for p in sys.path
                     if p.endswith("site-packages") and "venv" in p.lower()), "")
        if site:
            env["PYTHONPATH"] = site + (os.pathsep + env.get("PYTHONPATH", ""))
        env["PYTHONIOENCODING"] = "ascii"
        return env

    procs: dict[int, subprocess.Popen] = {}
    for i in range(k):
        p = subprocess.Popen(child_cmd(i), cwd=str(HERE), env=child_env(),
                             creationflags=DETACH_FLAGS,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs[i] = p
        _log(log, f"shard{i} LAUNCHED pid={p.pid} ({len(_shard_roots(roots, i, k))} roots)")

    while True:
        time.sleep(30)
        alive = 0
        for i, p in list(procs.items()):
            if p.poll() is None:
                alive += 1
        # aggregate shard heartbeats
        agg = {"done": 0, "total": 0}
        for i in range(k):
            sp = STATE_DIR / f"universe_dl_shard{i}.json"
            try:
                j = json.loads(sp.read_text())
                agg["done"] += j.get("done_units", 0)
                agg["total"] += j.get("total_units", 0)
            except Exception:
                pass
        pct = round(100.0 * agg["done"] / agg["total"], 2) if agg["total"] else 0.0
        all_done = agg["total"] > 0 and agg["done"] >= agg["total"]
        try:
            combined.write_text(json.dumps(
                {"updated": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
                 "alive_shards": alive, **agg, "pct": pct,
                 "complete": all_done}, indent=2))
        except Exception:
            pass
        # Heartbeat: mtime advances every tick while alive (proof of forward progress).
        # The alarm reads mtime + text; "COMPLETE" suppresses on a legit finish.
        _write_heartbeat(f"progress {agg['done']}/{agg['total']} ({pct}%) | {alive} shard(s) alive"
                         + ("  COMPLETE" if all_done else ""))
        _log(log, f"progress {agg['done']}/{agg['total']} ({pct}%) | {alive} shard(s) alive")
        # restart a dead-but-unfinished shard (child skips done -> resumes). Do NOT
        # restart once the whole scope is complete (avoid a relaunch race at the finish).
        if not all_done:
            for i, p in list(procs.items()):
                if p.poll() is not None:
                    sp = STATE_DIR / f"universe_dl_shard{i}.json"
                    unfinished = True
                    try:
                        j = json.loads(sp.read_text())
                        unfinished = j.get("done_units", 0) < j.get("total_units", 1)
                    except Exception:
                        pass
                    if unfinished:
                        _log(log, f"shard{i} DEAD + unfinished -> restart")
                        np = subprocess.Popen(child_cmd(i), cwd=str(HERE), env=child_env(),
                                             creationflags=DETACH_FLAGS,
                                             stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL)
                        procs[i] = np
        if all_done or alive == 0:
            _log(log, f"=== supervisor done === done={agg['done']}/{agg['total']} "
                      f"complete={all_done} alive={alive}")
            _write_heartbeat(f"progress {agg['done']}/{agg['total']} ({pct}%)  COMPLETE")
            return 0


# =========================================================================== #
# LAUNCHER — the scheduled-task entry point (singleton-guarded, idempotent)
# =========================================================================== #
def _load_gateway_lock():
    """Import paperbot.gateway_lock cleanly despite the `config` module-name collision.

    Both datacollector and paperbot ship a top-level `config` module; ours is already
    bound as sys.modules['config'] here, but gateway_lock needs PAPERBOT's config (for
    config.STATE_DIR). So we load paperbot's config from its file under a private name,
    temporarily bind it as `config` for the duration of the gateway_lock import, then
    restore ours. importlib is used so nothing depends on sys.path ordering. Returns the
    loaded gateway_lock module."""
    import importlib.util
    pb = config.CODE_ROOT.parent / "paperbot"
    # Load paperbot's config under a private module name.
    spec_c = importlib.util.spec_from_file_location("_pb_config", pb / "config.py")
    pb_config = importlib.util.module_from_spec(spec_c)
    spec_c.loader.exec_module(pb_config)
    saved = sys.modules.get("config")
    sys.modules["config"] = pb_config           # gateway_lock's `import config` -> paperbot's
    try:
        spec_g = importlib.util.spec_from_file_location("_pb_gateway_lock", pb / "gateway_lock.py")
        gl = importlib.util.module_from_spec(spec_g)
        spec_g.loader.exec_module(gl)
    finally:
        if saved is not None:
            sys.modules["config"] = saved       # restore OUR config
        else:
            sys.modules.pop("config", None)
    return gl


_GL = None                                       # cached paperbot.gateway_lock module


def _gl():
    """Return the (cached) paperbot.gateway_lock module — one load, so GatewayBusySkip
    raised by _singleton_lock() is the SAME class the launcher's except-clause catches."""
    global _GL
    if _GL is None:
        _GL = _load_gateway_lock()
    return _GL


def _singleton_lock():
    """Cross-process SINGLETON lock for the supervisor role, on the PROVEN
    paperbot.gateway_lock reclaim machinery (atomic O_EXCL + dead-PID / stale-heartbeat
    reclaim) pointed at our own dedicated lock path. NON-blocking: if a live supervisor
    already holds it, the caller gets GatewayBusySkip and no-ops (no duplicate instance).
    A crashed supervisor's lock (dead pid, or its lock-heartbeat silent > ~300s) is
    auto-reclaimed, so the next scheduled trigger cleanly takes over.

    We pass a dummy client_id — this download uses NO IBKR connection; client_id is only
    metadata in the lock JSON here (kept distinct from any live clientId so it can't be
    confused with a real gateway holder)."""
    return _gl().gateway_lock(
        purpose="universe_download_supervisor",
        client_id=999,                 # metadata only; NOT an IBKR clientId
        on_busy="skip",
        wait_secs=0.0,
        lock_path=str(SINGLETON_LOCK),
    )


def _scope_complete(roots: list[str], days: list[dt.date], layer: str) -> bool:
    """True iff every (root, day, layer) unit in scope is already on disk.

    Cheap disk checks only (have_day / snap_have) — no network. A leftover scheduled
    trigger then no-ops instead of spinning up shards for nothing."""
    do_eod = layer in ("eod", "both")
    do_snap = layer in ("snap", "both")
    for r in roots:
        for d in days:
            if do_eod and not eod_have(r, d):
                return False
            if do_snap and not snap_have(r, d):
                return False
    return True


def launcher(roots: list[str], start: str, end: str, layer: str, k: int) -> int:
    """SCHEDULED-TASK entry point. Idempotent + singleton-safe:

      1. If the whole scope is already on disk -> clean no-op (leftover trigger).
      2. Verify the terminal is reachable; if not, log + no-op (do NOT spin) so the
         next trigger retries — the independent alarm catches a persistent outage.
      3. Acquire the singleton with a NON-BLOCKING attempt. If a live supervisor holds
         it, no-op (no duplicate). Otherwise hold it and run the K=4 supervisor, which
         resumes from the on-disk checkpoint (skip-done). The lock releases on exit
         (even on crash), so the next trigger takes over.
    """
    GatewayBusySkip = _gl().GatewayBusySkip

    days = _business_days(start, end)
    if _scope_complete(roots, days, layer):
        _log(SUPERVISOR_LOG, "LAUNCHER: scope already complete on disk — no-op.")
        _write_heartbeat("scope already complete on disk  COMPLETE")
        return 0
    if not connected():
        _log(SUPERVISOR_LOG, "LAUNCHER: terminal not reachable — no-op; next trigger retries.")
        return 0
    try:
        cm = _singleton_lock()
    except Exception as e:                       # noqa: BLE001
        _log(SUPERVISOR_LOG, f"LAUNCHER: could not build singleton lock ({e!r}); no-op to be safe.")
        return 0
    try:
        with cm:
            _log(SUPERVISOR_LOG, f"LAUNCHER: acquired singleton (pid {os.getpid()}) — supervising.")
            return run_supervisor(roots, start, end, layer, k)
    except GatewayBusySkip as e:
        _log(SUPERVISOR_LOG, f"LAUNCHER: a live supervisor already holds the singleton ({e}); "
                             "no-op — NOT starting a duplicate.")
        return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Standalone expanded-universe options downloader")
    ap.add_argument("--roots", nargs="*", default=None,
                    help="explicit roots; default = universe_config.all_roots()")
    ap.add_argument("--only-new", action="store_true",
                    help="EOD: restrict to roots NOT already in the frozen warehouse")
    ap.add_argument("--start", default=uni.GRAB_START)
    ap.add_argument("--end", default=None, help="YYYYMMDD; default = yesterday")
    ap.add_argument("--layer", choices=["eod", "snap", "both"], default="both")
    ap.add_argument("--shards", type=int, default=uni.SHARDS,
                    help="parallel shards (supervisor mode); 1 = single foreground worker")
    ap.add_argument("--calibrate", action="store_true",
                    help="single foreground worker + emit detailed per-symbol-day timing")
    ap.add_argument("--launcher", action="store_true",
                    help="SCHEDULED-TASK entry: singleton-guarded, idempotent, resumable "
                         "supervisor. Overlapping triggers no-op; a crashed one is reclaimed.")
    # internal (child) flags
    ap.add_argument("--shard", type=int, default=None)
    ap.add_argument("--of", type=int, default=None)
    ap.add_argument("--child", action="store_true")
    args = ap.parse_args()

    roots = [r.upper() for r in args.roots] if args.roots else uni.all_roots()
    if args.only_new:
        roots = [r for r in roots if r not in uni.EXISTING_ROOTS]
    end = args.end or _default_end()
    days = _business_days(args.start, end)

    # ---- scheduled-task launcher (singleton-guarded) ----
    if args.launcher:
        return launcher(roots, args.start, end, args.layer, max(1, args.shards))

    # ---- child (one shard) ----
    if args.child and args.shard is not None and args.of:
        my = _shard_roots(roots, args.shard, args.of)
        logp = STATE_DIR / f"universe_dl_shard{args.shard}.log"
        prog = STATE_DIR / f"universe_dl_shard{args.shard}.json"
        _log(logp, f"child shard {args.shard}/{args.of}: {len(my)} roots, "
                   f"{len(days)} days, layer={args.layer}")
        run_worker(my, days, args.layer, logp, prog)
        return 0

    # ---- calibration / single foreground worker ----
    if args.calibrate or args.shards <= 1:
        logp = STATE_DIR / "universe_dl_foreground.log"
        prog = STATE_DIR / "universe_dl_foreground.json"
        _log(logp, f"foreground worker: {len(roots)} roots {roots}, {len(days)} days "
                   f"({args.start}..{end}), layer={args.layer}, calibrate={args.calibrate}")
        stats = run_worker(roots, days, args.layer, logp, prog, calibrate=args.calibrate)
        if args.calibrate:
            _print_calibration(stats, roots, days)
        return 0

    # ---- supervisor (parallel shards) ----
    return run_supervisor(roots, args.start, end, args.layer, args.shards)


def _print_calibration(stats: dict, roots: list[str], days: list[dt.date]) -> None:
    def per(secs, n): return (secs / n) if n else 0.0
    def mb(b, n): return (b / n / 1e6) if n else 0.0
    print("\n" + "=" * 68)
    print("CALIBRATION RESULT")
    print("=" * 68)
    print(f"roots: {roots}   days: {len(days)} ({_daystr(days[0])}..{_daystr(days[-1])})")
    print(f"EOD : {stats['eod_days']} sym-days | "
          f"{per(stats['eod_secs'], stats['eod_days']):.2f} s/sym-day | "
          f"{mb(stats['eod_bytes'], stats['eod_days']):.2f} MB/sym-day")
    print(f"SNAP: {stats['snap_days']} sym-days | "
          f"{per(stats['snap_secs'], stats['snap_days']):.2f} s/sym-day | "
          f"{mb(stats['snap_bytes'], stats['snap_days']):.2f} MB/sym-day")
    print(f"errors: {stats['errors']}")
    # machine-readable dump for the report step
    out = STATE_DIR / "calibration_result.json"
    out.write_text(json.dumps({
        "roots": roots, "days": [_daystr(d) for d in days],
        "eod_days": stats["eod_days"], "eod_secs": stats["eod_secs"],
        "eod_bytes": stats["eod_bytes"],
        "snap_days": stats["snap_days"], "snap_secs": stats["snap_secs"],
        "snap_bytes": stats["snap_bytes"], "errors": stats["errors"],
        "eod_s_per_symday": per(stats["eod_secs"], stats["eod_days"]),
        "eod_mb_per_symday": mb(stats["eod_bytes"], stats["eod_days"]),
        "snap_s_per_symday": per(stats["snap_secs"], stats["snap_days"]),
        "snap_mb_per_symday": mb(stats["snap_bytes"], stats["snap_days"]),
        "per_symbol_day": stats.get("per_symbol_day", []),
    }, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    sys.exit(main())
