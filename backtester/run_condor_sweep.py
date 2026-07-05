r"""
run_condor_sweep.py -- THIN, RESUMABLE runner for the IRON CONDOR structure sweep (BOTH arms).

PAPER / research only. OFFLINE. Windows. READ-ONLY consumer of the COMMITTED harness +
driver (s5_financing_harness.py, s5_financing_sweep.py). This script does NOT edit either
module and does NOT re-implement any fill / selection / metric -- it only orchestrates the
committed driver's own per-cell primitive (`_run_cell_window`) with PER-CELL CHECKPOINTING so
a long sweep is resumable from disk, exactly like run_put_write_sweep.py / run_s5_sweep_pcs.py.

TWO ARMS (run arm-by-arm: finish neutral, then income), each to its OWN checkpoint dir:
  ARM 1  upside-NEUTRAL : SPEC = sw.iron_condor_spec(call_delta=None)
                          -> output/s5_financing/iron_condor_neutral/cells/
  ARM 2  call-side INCOME: SPEC = sw.iron_condor_spec(call_delta=0.15)
                          -> output/s5_financing/iron_condor_income/cells/

Grid (FULL pre-registered space, unchanged from the driver defaults):
  tenor_dte {7,14,30,45} x short_delta {0.10,0.15,0.20,0.30}
  x management {hold_to_expiry, profit_50, dte_21, profit_50_or_dte_21, stop_2x}
  x regime {ungated, calm_only}  ... over BOTH clean windows A and B.
  => 4*4*5*2 = 160 cells x 2 windows = 320 cell-windows PER ARM, 640 across both arms.

Checkpoints: one JSON per (cell x window) under each arm's cells/ dir (output/* is gitignored).
On relaunch, any cell whose checkpoint already exists is SKIPPED (resume, don't restart). The
memoized available_days scan is reused across BOTH arms. Progress flushes to stdout + disk.

NOTE: an iron_condor_neutral/cells dir may already hold a few cells from an earlier partial
run; the cell-key format below matches run_put_write_sweep.py's schema so any valid existing
checkpoint is reused as-is, otherwise it is simply recomputed (harmless).
"""
from __future__ import annotations

import datetime as _dt
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# import the committed driver + harness (read-only consumers)
import s5_financing_sweep as sw
import s5_financing_harness as h

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# --------------------------------------------------------------------------- #
# RUNNER-SIDE MEMOIZATION of h.available_days (NOT an edit to the committed files).
# --------------------------------------------------------------------------- #
# `available_days(clean_only=True)` scans ~2200 parquet footers on disk (~44s per call). It is
# a PURE, DETERMINISTIC read of a static warehouse -- the same disk yields the same day list
# every call within a run. The committed driver/harness call it once per universe backtest AND
# once per gated cell, so the SAME 44s scan runs hundreds of times: pure overhead, ~3 hours of
# nothing. We memoize it in-process by key (symbol, clean_only). This changes NO fill, NO
# selection, NO metric, NO trade -- it returns the identical list, only faster. The committed
# source files are untouched; this is a legitimate runner-side cache, exactly like the driver's
# own _UNIVERSE_CACHE / _CHAIN_CACHE. It is applied ONLY inside this runner and is shared across
# BOTH arms (one warehouse footer scan for the whole run).
_AVAIL_CACHE: dict = {}
_ORIG_AVAILABLE_DAYS = h.available_days


def _memo_available_days(symbol: str = h.ROOT, clean_only: bool = True):
    key = (symbol, clean_only)
    if key not in _AVAIL_CACHE:
        _AVAIL_CACHE[key] = _ORIG_AVAILABLE_DAYS(symbol=symbol, clean_only=clean_only)
    # return a fresh list copy so no caller can mutate the cached day list in place.
    return list(_AVAIL_CACHE[key])


# patch the harness module attribute -> both backtest_structure (module-global lookup) and the
# sweep's gated branch (h.available_days) resolve to the memoized version.
h.available_days = _memo_available_days

# ---- run configuration (matches the pre-registered grid; nothing tuned) -------------------
PLACEBO_DRAWS = 500      # driver default
SEED = 12345             # driver default (fixes the matched-placebo resample)

TENOR_GRID = sw.TENOR_DTE_GRID          # (7, 14, 30, 45)
DELTA_GRID = sw.SHORT_DELTA_GRID        # (0.10, 0.15, 0.20, 0.30)
MGMT_GRID = sw.MANAGEMENT_GRID          # 5 management rules
REGIME_GRID = sw.REGIME_GRID            # ('ungated', 'calm_only')
WINDOWS = sw.WINDOWS                    # {'A': ..., 'B': ...}

# The two pre-registered arms: (checkpoint-dir-name, spec-builder-kwarg).
ARMS = (
    ("iron_condor_neutral", dict(call_delta=None)),   # ARM 1: upside-NEUTRAL
    ("iron_condor_income", dict(call_delta=0.15)),     # ARM 2: call-side INCOME
)

_OUT_ROOT = Path(__file__).resolve().parent / "output" / "s5_financing"

# The columns of the assembled per-cell table -- IDENTICAL to sweep_structure's `store`.
OBJECT_COLS = ("crash_exit_cost", "regime_buckets", "entry_rejects", "ret_series")


def _cell_key(tenor, mgmt, delta, regime, window) -> str:
    """Filesystem-safe checkpoint key for one (cell x window) -- EXACT schema of
    run_put_write_sweep.py so any valid existing checkpoint is reused as-is."""
    return f"t{tenor}_m{mgmt}_d{delta}_{regime}_w{window}"


def _row_from_cell(spec, tenor, mgmt, delta, regime, window) -> dict:
    """Run ONE (cell x window) via the driver's OWN primitive and flatten to the driver's exact
    row schema. Never re-implements a fill/metric -- delegates to sw._run_cell_window."""
    r = sw._run_cell_window(spec, tenor, delta, mgmt, regime, window,
                            PLACEBO_DRAWS, SEED, use_cache=True)
    placebo = r["_placebo"]
    return {
        "structure": spec.name,
        "tenor_dte": tenor,
        "management": mgmt,
        "short_delta": delta,
        "regime": regime,
        "window": r["window"],
        "net_pct_yr_of_core": r["net_pct_yr_of_core"],
        "sharpe_ann": r["sharpe_ann"],
        "sortino_ann": r["sortino_ann"],
        "win_rate": r["win_rate"],
        "loss_win_ratio": r["loss_win_ratio"],
        "n_trades": r["n_trades"],
        "n_universe": r["n_universe"],
        "fill_rate": r["fill_rate"],
        "mean_entry_credit": r["mean_entry_credit"],
        "mean_net_pnl": r["mean_net_pnl"],
        "truncated_dropped": r["truncated_dropped"],
        "placebo_percentile": placebo["percentile"],
        "placebo_beats": placebo["beats_placebo"],
        "placebo_real": placebo["real_metric"],
        "placebo_mean": placebo["placebo_mean"],
        "placebo_n_selected": placebo["n_selected"],
        "placebo_n_universe": placebo["n_universe"],
        "crash_exit_cost": r["_crash_exit_cost"],
        "regime_buckets": r["_regime_buckets"],
        "entry_rejects": r["entry_rejects"],
        "ret_series": r["_ret_series"],
    }


def _write_checkpoint(path: Path, row: dict) -> None:
    """Atomically persist one cell row (tmp then rename -- atomic on Windows same-volume)."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(row))
    tmp.replace(path)


def run_arm(arm_name: str, spec_kwargs: dict) -> pd.DataFrame:
    """Walk the full pre-registered combo grid for ONE arm with per-cell checkpointing; resume
    on relaunch. Combo order matches sweep_structure: product(tenor, management, short_delta,
    regime), then windows A,B inside each combo (so a gated+ungated sibling pair reuses the same
    cached universe frame). We DO NOT clear the driver's universe cache between cells within an
    arm; we DO clear it once at the START of each arm (fresh cache per structure)."""
    spec = sw.iron_condor_spec(**spec_kwargs)
    out_dir = _OUT_ROOT / arm_name
    cell_dir = out_dir / "cells"
    cell_dir.mkdir(parents=True, exist_ok=True)

    combos = list(itertools.product(TENOR_GRID, MGMT_GRID, DELTA_GRID, REGIME_GRID))
    n_cells = len(combos) * len(WINDOWS)
    print(f"\n[arm start] {arm_name}: spec={spec.name} kwargs={spec_kwargs}", flush=True)
    print(f"[arm start] {len(combos)} combos x {len(WINDOWS)} windows = {n_cells} cells; "
          f"checkpoint dir = {cell_dir}", flush=True)

    # fresh universe cache for THIS arm (mirrors sweep_structure(use_cache=True)).
    sw.clear_backtest_cache()

    done = 0
    resumed = 0
    for (tenor, mgmt, delta, regime) in combos:
        for window in WINDOWS:
            done += 1
            key = _cell_key(tenor, mgmt, delta, regime, window)
            path = cell_dir / f"{key}.json"
            if path.is_file():
                resumed += 1
                if done % 20 == 0 or done == n_cells:
                    print(f"[{arm_name} {done}/{n_cells}] SKIP (checkpoint exists) {key}",
                          flush=True)
                continue
            row = _row_from_cell(spec, tenor, mgmt, delta, regime, window)
            _write_checkpoint(path, row)
            print(f"[{arm_name} {done}/{n_cells}] {key} "
                  f"n={row['n_trades']} netpctyr={row['net_pct_yr_of_core']:+.4f} "
                  f"plc={row['placebo_percentile']}", flush=True)

    print(f"[arm complete] {arm_name}: {n_cells} cell-windows; {resumed} resumed from "
          f"checkpoint, {n_cells - resumed} freshly computed.", flush=True)
    return _assemble(spec, out_dir, cell_dir)


def _assemble(spec, out_dir: Path, cell_dir: Path) -> pd.DataFrame:
    """Load every checkpoint into the full per-cell table and persist parquet + CSV + the
    sign-consistency sidecar, in the EXACT format sweep_structure(write=True) produces."""
    rows = []
    for tenor, mgmt, delta, regime in itertools.product(
            TENOR_GRID, MGMT_GRID, DELTA_GRID, REGIME_GRID):
        for window in WINDOWS:
            key = _cell_key(tenor, mgmt, delta, regime, window)
            path = cell_dir / f"{key}.json"
            if not path.is_file():
                raise RuntimeError(f"missing checkpoint {path}; grid incomplete, cannot assemble")
            rows.append(json.loads(path.read_text()))
    df = pd.DataFrame(rows)

    # sign-consistency across windows A/B per (tenor,mgmt,delta,regime) cell -- same logic as
    # the committed sweep_structure.
    sign_consistency: dict = {}
    if not df.empty:
        for key, g in df.groupby(["tenor_dte", "management", "short_delta", "regime"]):
            by_win = g.set_index("window")["net_pct_yr_of_core"]
            a = by_win.get("A", float("nan"))
            b = by_win.get("B", float("nan"))
            same = (np.isfinite(a) and np.isfinite(b) and (np.sign(a) == np.sign(b))
                    and a != 0 and b != 0)
            sign_consistency["|".join(str(x) for x in key)] = {
                "A": float(a) if np.isfinite(a) else None,
                "B": float(b) if np.isfinite(b) else None,
                "sign_consistent": bool(same),
            }

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.date.today().strftime("%Y%m%d")
    base = out_dir / f"s5_sweep_{spec.name}_{stamp}"
    store = df.copy()
    for col in OBJECT_COLS:
        store[col] = store[col].apply(json.dumps)
    try:
        store.to_parquet(base.with_suffix(".parquet"), index=False)
    except Exception as e:  # pragma: no cover - parquet engine optional
        print(f"[warn] parquet write failed ({type(e).__name__}: {e}); CSV only", flush=True)
    store.to_csv(base.with_suffix(".csv"), index=False)
    (base.with_name(base.name + "_sign_consistency.json")
     .write_text(json.dumps(sign_consistency, indent=2)))
    print(f"[written] {base}.parquet / .csv  ({len(df)} cells)", flush=True)
    print(f"[written] {base.name}_sign_consistency.json", flush=True)
    return df


def main() -> None:
    print(f"[start] iron_condor sweep: BOTH arms {[a for a, _ in ARMS]}; "
          f"grid={len(TENOR_GRID)}x{len(MGMT_GRID)}x{len(DELTA_GRID)}x{len(REGIME_GRID)} "
          f"x {len(WINDOWS)} windows per arm", flush=True)
    for arm_name, spec_kwargs in ARMS:
        run_arm(arm_name, spec_kwargs)
    print("\n[all arms complete]", flush=True)


if __name__ == "__main__":
    main()
