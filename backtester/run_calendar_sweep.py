r"""
run_calendar_sweep.py -- THIN, RESUMABLE runner for the PUT-CALENDAR (tail-internal) structure.

PAPER / research only. OFFLINE. Windows. READ-ONLY consumer of the COMMITTED harness
(s5_financing_harness) and the COMMITTED driver (s5_financing_sweep). This runner does NOT
edit either -- it only calls the driver's own per-cell function `_run_cell_window`, adds
per-cell checkpointing so a restart resumes where it left off, then assembles the SAME
per-cell-per-window table + sign-consistency + parquet/CSV that `sweep_structure` would.

STRUCTURE: sw.put_calendar_spec() with defaults -- a NET-DEBIT put calendar whose swept
`tenor_dte` sets the SHORT FRONT-month tenor; the LONG back-month tenor follows structurally
from back_dte_mult=2.0 (back = 2x front). back_dte_mult / back_dte_offset / strike_offset are
STATED structural choices of the family, NOT swept knobs. This is a MULTI-EXPIRY structure;
with per-leg-expiry settlement (harness commit 06faee2) it settles each leg on its own expiry
honestly.

Grid (full pre-registered space, unchanged from the driver):
    tenor_dte  : {7, 14, 30, 45}     (the SHORT front-month DTE; back leg = 2x, structural)
    management : {hold_to_expiry, profit_50, dte_21, profit_50_or_dte_21, stop_2x}
    short_delta: {0.10, 0.15, 0.20, 0.30}
    regime     : {ungated, calm_only}
  x BOTH clean windows A (2018-01-02..2020-08-12) and B (2022-01-03..2026-07-02),
  evaluated SEPARATELY. => 4 x 5 x 4 x 2 x 2 = 320 cells.

Checkpoints: one JSON per cell under output/s5_financing/put_calendar/cells/ (gitignored).
On restart, completed cells are loaded from disk and SKIPPED (resumable). Progress is
flushed to stdout AND to disk per cell.
"""
from __future__ import annotations

import datetime as _dt
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

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
# every call within a run. The committed driver/harness call it once per universe backtest
# (160x) AND once per gated cell (80x), so the SAME 44s scan runs ~240 times: pure overhead,
# ~3 hours of nothing. We memoize it in-process by key (symbol, clean_only). This changes NO
# fill, NO selection, NO metric, NO trade -- it returns the identical list, only faster. The
# committed source files are untouched; this is a legitimate runner-side cache, exactly like
# the driver's own _UNIVERSE_CACHE / _CHAIN_CACHE. It is applied ONLY inside this runner.
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


# --- configuration: the FULL pre-registered grids, taken from the committed driver ---------
# STRUCTURE = put calendar with DEFAULTS (back = 2x front tenor). The back-leg multiple/offset
# and strike offset are STRUCTURAL defaults of the family, NOT swept knobs.
SPEC = sw.put_calendar_spec()
TENOR_DTE_GRID = sw.TENOR_DTE_GRID
SHORT_DELTA_GRID = sw.SHORT_DELTA_GRID
MANAGEMENT_GRID = sw.MANAGEMENT_GRID
REGIME_GRID = sw.REGIME_GRID
WINDOWS = sw.WINDOWS  # {"A": ..., "B": ...}
PLACEBO_DRAWS = 500
SEED = 12345

OUT_DIR = Path(__file__).resolve().parent / "output" / "s5_financing" / "put_calendar"
CELL_DIR = OUT_DIR / "cells"


def _cell_key(tenor, mgmt, delta, regime, window) -> str:
    return f"t{tenor}_m{mgmt}_d{delta}_{regime}_w{window}"


def _cell_path(tenor, mgmt, delta, regime, window) -> Path:
    return CELL_DIR / (_cell_key(tenor, mgmt, delta, regime, window) + ".json")


def _row_from_cell(spec_name, tenor, mgmt, delta, regime, window, r: dict) -> dict:
    """Assemble the per-cell row EXACTLY as sweep_structure does (same columns)."""
    placebo = r["_placebo"]
    return {
        "structure": spec_name,
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


def main() -> None:
    CELL_DIR.mkdir(parents=True, exist_ok=True)
    combos = list(itertools.product(TENOR_DTE_GRID, MANAGEMENT_GRID, SHORT_DELTA_GRID,
                                    REGIME_GRID))
    n_cells = len(combos) * len(WINDOWS)
    print(f"[start] {SPEC.name} sweep: {len(combos)} combos x {len(WINDOWS)} windows "
          f"= {n_cells} cells; checkpoint dir = {CELL_DIR}", flush=True)

    # Note: the driver's own _run_cell_window uses a process-level universe cache keyed by
    # (spec, tenor, delta, mgmt, window). We DO NOT clear it per cell so an ungated cell and
    # its calm_only sibling reuse the same underlying backtest (identical to the committed
    # sweep_structure, which clears once at the top). We clear once here at start.
    sw.clear_backtest_cache()

    done = 0
    skipped = 0
    for (tenor, mgmt, delta, regime) in combos:
        for window in WINDOWS:
            done += 1
            cp = _cell_path(tenor, mgmt, delta, regime, window)
            if cp.is_file():
                skipped += 1
                if done % 20 == 0 or done == n_cells:
                    print(f"[{done}/{n_cells}] SKIP (cached) {cp.name}", flush=True)
                continue
            r = sw._run_cell_window(SPEC, tenor, delta, mgmt, regime, window,
                                    PLACEBO_DRAWS, SEED, use_cache=True)
            row = _row_from_cell(SPEC.name, tenor, mgmt, delta, regime, window, r)
            # atomic-ish checkpoint write (tmp then rename)
            tmp = cp.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(row))
            tmp.replace(cp)
            plc = row["placebo_percentile"]
            print(f"[{done}/{n_cells}] {SPEC.name} dte={tenor} mgmt={mgmt} d={delta} "
                  f"{regime} win={window} n={row['n_trades']} "
                  f"netpctyr={row['net_pct_yr_of_core']:+.4f} plc={plc}", flush=True)

    print(f"[cells complete] {n_cells} total, {skipped} were already cached", flush=True)

    # --- assemble the full per-cell table from checkpoints (resume-safe) ---------------------
    rows = []
    for (tenor, mgmt, delta, regime) in combos:
        for window in WINDOWS:
            cp = _cell_path(tenor, mgmt, delta, regime, window)
            rows.append(json.loads(cp.read_text()))
    df = pd.DataFrame(rows)

    # sign-consistency across windows A/B per (tenor,mgmt,delta,regime) cell -- SAME logic as
    # the committed sweep_structure.
    sign_consistency: dict = {}
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

    # --- persist the aggregate table (parquet + CSV) the SAME way sweep_structure does -------
    stamp = _dt.date.today().strftime("%Y%m%d")
    base = OUT_DIR / f"s5_sweep_{SPEC.name}_{stamp}"
    store = df.copy()
    for col in ("crash_exit_cost", "regime_buckets", "entry_rejects", "ret_series"):
        store[col] = store[col].apply(json.dumps)
    try:
        store.to_parquet(base.with_suffix(".parquet"), index=False)
    except Exception as e:
        print(f"[warn] parquet write failed ({type(e).__name__}: {e}); CSV only", flush=True)
    store.to_csv(base.with_suffix(".csv"), index=False)
    (base.with_name(base.name + "_sign_consistency.json")
     .write_text(json.dumps(sign_consistency, indent=2)))
    print(f"[written] {base}.parquet / .csv  ({len(df)} cells)", flush=True)
    print(f"[written] {base.name}_sign_consistency.json", flush=True)


if __name__ == "__main__":
    main()
