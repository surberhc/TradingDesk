r"""
run_s5_sweep_pcs.py -- THIN, RESUMABLE runner for the PUT CREDIT SPREAD structure sweep.

PAPER / research only. OFFLINE. Windows. READ-ONLY consumer of the committed harness +
driver (s5_financing_harness.py, s5_financing_sweep.py). This script does NOT edit either
module and does NOT re-implement any fill / selection / metric -- it only orchestrates the
committed driver's own per-cell primitive (`_run_cell_window`) with PER-CELL CHECKPOINTING so
a long sweep is resumable from disk.

WHY A THIN RUNNER (and not sweep_structure directly):
  `sweep_structure(...)` runs the WHOLE grid in one process and only writes at the very end.
  If it dies mid-grid you lose everything. This runner walks the SAME pre-registered combo
  order the driver uses, calls the driver's OWN `_run_cell_window` for each (cell x window),
  and writes each cell's standardized result to disk as it completes. On relaunch it SKIPS
  any cell whose checkpoint already exists (resume, don't restart). When every cell is done it
  assembles the exact same per-cell table the driver's `store` writes (parquet + CSV) plus the
  sign-consistency sidecar, then prints the honest read.

Structure: put_credit_spread_spec(wing=10.0) -- the 2a validation family.
Grids: the FULL pre-registered grid (driver defaults):
  tenor_dte {7,14,30,45} x short_delta {0.10,0.15,0.20,0.30}
  x management {hold_to_expiry, profit_50, dte_21, profit_50_or_dte_21, stop_2x}
  x regime {ungated, calm_only}  ... over BOTH clean windows A and B.
=> 4*4*5*2 = 160 cells x 2 windows = 320 cell-windows.

Output (gitignored -- output/* is ignored): backtester/output/s5_financing/put_credit_spread/
  cells/<key>.json          one checkpoint per (cell x window)
  s5_sweep_put_credit_spread_10w_<stamp>.parquet / .csv   assembled full table
  ..._sign_consistency.json                               cross-window sign read
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

# ---- run configuration (matches the pre-registered grid; nothing tuned) -------------------
WING = 10.0
PLACEBO_DRAWS = 500      # driver default
SEED = 12345             # driver default (fixes the matched-placebo resample)

SPEC = sw.put_credit_spread_spec(wing=WING)

TENOR_GRID = sw.TENOR_DTE_GRID          # (7, 14, 30, 45)
DELTA_GRID = sw.SHORT_DELTA_GRID        # (0.10, 0.15, 0.20, 0.30)
MGMT_GRID = sw.MANAGEMENT_GRID          # 5 management rules
REGIME_GRID = sw.REGIME_GRID            # ('ungated', 'calm_only')
WINDOWS = sw.WINDOWS                    # {'A': ..., 'B': ...}

OUT_DIR = (Path(__file__).resolve().parent / "output" / "s5_financing"
           / "put_credit_spread")
CELL_DIR = OUT_DIR / "cells"

# The columns of the assembled per-cell table -- IDENTICAL to sweep_structure's `store`.
OBJECT_COLS = ("crash_exit_cost", "regime_buckets", "entry_rejects", "ret_series")


def _cell_key(tenor: int, mgmt: str, delta: float, regime: str, window: str) -> str:
    """Filesystem-safe checkpoint key for one (cell x window)."""
    return f"dte{tenor}__{mgmt}__d{delta}__{regime}__win{window}"


def _row_from_cell(tenor: int, mgmt: str, delta: float, regime: str,
                   window: str) -> dict:
    """Run ONE (cell x window) via the driver's OWN primitive and flatten to the driver's
    exact row schema. Never re-implements a fill/metric -- delegates to sw._run_cell_window,
    which uses the harness for the whole walk + the shared battery."""
    r = sw._run_cell_window(SPEC, tenor, delta, mgmt, regime, window,
                            PLACEBO_DRAWS, SEED, use_cache=True)
    placebo = r["_placebo"]
    row = {
        "structure": SPEC.name,
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
    return row


def _write_checkpoint(path: Path, row: dict) -> None:
    """Atomically persist one cell row (object fields JSON-encoded already by json.dumps)."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(row))
    tmp.replace(path)   # atomic on Windows for same-volume replace


def run_grid() -> pd.DataFrame:
    """Walk the full pre-registered combo grid with per-cell checkpointing; resume on relaunch.

    Combo order matches sweep_structure: product(tenor, management, short_delta, regime),
    then windows A,B inside each combo (so a gated+ungated sibling pair reuses the same cached
    universe frame). We DO NOT clear the driver's universe cache between cells within this run,
    so the ungated backtest for each (tenor,mgmt,delta,window) is computed once and the gated
    sibling reuses it -- exactly the driver's own efficiency."""
    CELL_DIR.mkdir(parents=True, exist_ok=True)
    combos = list(itertools.product(TENOR_GRID, MGMT_GRID, DELTA_GRID, REGIME_GRID))
    n_cells = len(combos) * len(WINDOWS)

    # fresh universe cache for THIS run (mirrors sweep_structure(use_cache=True)).
    sw.clear_backtest_cache()

    done = 0
    resumed = 0
    for (tenor, mgmt, delta, regime) in combos:
        for window in WINDOWS:
            done += 1
            key = _cell_key(tenor, mgmt, delta, regime, window)
            path = CELL_DIR / f"{key}.json"
            if path.is_file():
                resumed += 1
                print(f"[{done}/{n_cells}] SKIP (checkpoint exists) {key}", flush=True)
                continue
            row = _row_from_cell(tenor, mgmt, delta, regime, window)
            _write_checkpoint(path, row)
            print(f"[{done}/{n_cells}] {key} "
                  f"n={row['n_trades']} netpctyr={row['net_pct_yr_of_core']:+.4f} "
                  f"plc={row['placebo_percentile']}", flush=True)

    print(f"\n[grid complete] {n_cells} cell-windows; {resumed} resumed from checkpoint, "
          f"{n_cells - resumed} freshly computed.", flush=True)
    return _assemble()


def _assemble() -> pd.DataFrame:
    """Load every checkpoint into the full per-cell table and persist parquet + CSV + the
    sign-consistency sidecar, in the EXACT format sweep_structure(write=True) produces."""
    rows = []
    for tenor, mgmt, delta, regime in itertools.product(
            TENOR_GRID, MGMT_GRID, DELTA_GRID, REGIME_GRID):
        for window in WINDOWS:
            key = _cell_key(tenor, mgmt, delta, regime, window)
            path = CELL_DIR / f"{key}.json"
            if not path.is_file():
                raise RuntimeError(f"missing checkpoint {path}; grid incomplete, cannot assemble")
            rows.append(json.loads(path.read_text()))
    df = pd.DataFrame(rows)

    # sign-consistency across windows A/B per (tenor,mgmt,delta,regime) cell -- same logic as
    # the driver.
    sign_consistency: dict = {}
    if not df.empty:
        for keytup, g in df.groupby(["tenor_dte", "management", "short_delta", "regime"]):
            by_win = g.set_index("window")["net_pct_yr_of_core"]
            a = by_win.get("A", float("nan"))
            b = by_win.get("B", float("nan"))
            same = (np.isfinite(a) and np.isfinite(b) and (np.sign(a) == np.sign(b))
                    and a != 0 and b != 0)
            sign_consistency[keytup] = {
                "A": float(a) if np.isfinite(a) else None,
                "B": float(b) if np.isfinite(b) else None,
                "sign_consistent": bool(same),
            }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.date.today().strftime("%Y%m%d")
    base = OUT_DIR / f"s5_sweep_{SPEC.name}_{stamp}"
    store = df.copy()
    # object columns are already Python objects in the row; JSON-encode for parquet/CSV like
    # the driver's store does (they came in from checkpoints already decoded).
    for col in OBJECT_COLS:
        store[col] = store[col].apply(json.dumps)
    try:
        store.to_parquet(base.with_suffix(".parquet"), index=False)
    except Exception as e:  # pragma: no cover - parquet engine optional
        print(f"[warn] parquet write failed ({type(e).__name__}: {e}); CSV only", flush=True)
    store.to_csv(base.with_suffix(".csv"), index=False)
    sc = {"|".join(str(x) for x in k): v for k, v in sign_consistency.items()}
    base.with_name(base.name + "_sign_consistency.json").write_text(json.dumps(sc, indent=2))
    print(f"[written] {base}.parquet / .csv  ({len(df)} cells)", flush=True)

    df.attrs["sign_consistency"] = sign_consistency
    return df


# --------------------------------------------------------------------------- #
# Honest read / report (consumes the assembled table; applies NO DSR here).
# --------------------------------------------------------------------------- #
BAR_NET = 0.0156   # >= 1.56%/yr net-of-core: the tail-carry the program must fund


def report(df: pd.DataFrame) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sc = df.attrs.get("sign_consistency", {})

    def sc_lookup(tenor, mgmt, delta, regime):
        return sc.get((tenor, mgmt, delta, regime), {})

    print("\n" + "=" * 78)
    print("PUT CREDIT SPREAD (10w) -- FULL GRID HONEST READ")
    print("=" * 78)

    # (4) coverage first (frame-wide).
    n_rows = len(df)
    total_universe = int(df["n_universe"].sum())
    total_trades = int(df["n_trades"].sum())
    trunc = int(df["truncated_dropped"].sum())
    # aggregate reject tallies across cells
    rej = {"min_credit_floor": 0, "unfillable_or_unselectable": 0}
    for er in df["entry_rejects"]:
        d = er if isinstance(er, dict) else json.loads(er)
        for k in rej:
            rej[k] += int(d.get(k, 0))
    ungated = df[df["regime"] == "ungated"]
    fr = ungated["fill_rate"].dropna()
    print("\n(4) COVERAGE")
    print(f"  rows (cell x window)        : {n_rows}")
    print(f"  sum n_universe / n_trades   : {total_universe} / {total_trades}")
    print(f"  ungated fill_rate mean/min/max: "
          f"{fr.mean():.4f} / {fr.min():.4f} / {fr.max():.4f}")
    print(f"  truncated_dropped (summed)  : {trunc}")
    print(f"  entry_rejects (summed)      : min_credit_floor={rej['min_credit_floor']}, "
          f"unfillable_or_unselectable={rej['unfillable_or_unselectable']}")

    # (2) best cells by net_pct_yr_of_core (per cell-window row), with OOS/placebo/regime read.
    print("\n(2) TOP CELL-WINDOWS BY net_pct_yr_of_core")
    top = df.sort_values("net_pct_yr_of_core", ascending=False).head(12)
    for _, r in top.iterrows():
        scv = sc_lookup(r["tenor_dte"], r["management"], r["short_delta"], r["regime"])
        rb = r["regime_buckets"] if isinstance(r["regime_buckets"], dict) else json.loads(r["regime_buckets"])
        vix = rb.get("vix_tercile", {})
        vix_means = {b: round(vix[b]["mean_ret_pct"], 4) for b in vix}
        yrs = rb.get("year", {})
        yr_means = {int(y): round(v["mean_ret_pct"], 4) for y, v in yrs.items()}
        print(f"  dte={r['tenor_dte']:>2} mgmt={r['management']:<20} d={r['short_delta']} "
              f"{r['regime']:<9} win={r['window']}  "
              f"net%/yr={r['net_pct_yr_of_core']*100:+.3f}  "
              f"plc={r['placebo_percentile']}  beats={r['placebo_beats']}  "
              f"signAB=A:{scv.get('A')} B:{scv.get('B')} consistent={scv.get('sign_consistent')}")
        print(f"        VIX-tercile mean_ret_pct={vix_means}  year mean_ret_pct={yr_means}")

    # (3) how many cells clear the FULL bar (excluding DSR): a "cell" = (tenor,mgmt,delta,
    #     regime); it must, on BOTH windows, be net>=BAR, beat its matched placebo, be OOS
    #     sign-consistent, AND be regime-flat (no VIX tercile or year with a mean-ret sign
    #     opposite the cell's headline sign -> not living in one regime).
    print("\n(3) CELLS CLEARING THE BAR (net>=1.56%/yr on BOTH windows AND beats placebo on"
          " BOTH AND OOS sign-consistent AND regime-flat) -- DSR NOT applied (synthesis step)")
    passers = []
    grouped = df.groupby(["tenor_dte", "management", "short_delta", "regime"])
    for keytup, g in grouped:
        by_win = {r["window"]: r for _, r in g.iterrows()}
        if set(by_win) != {"A", "B"}:
            continue
        a, b = by_win["A"], by_win["B"]
        net_ok = (a["net_pct_yr_of_core"] >= BAR_NET) and (b["net_pct_yr_of_core"] >= BAR_NET)
        plc_ok = bool(a["placebo_beats"]) and bool(b["placebo_beats"])
        scv = sc_lookup(*keytup)
        sign_ok = bool(scv.get("sign_consistent"))
        regime_flat = _regime_flat(a) and _regime_flat(b)
        if net_ok and plc_ok and sign_ok and regime_flat:
            passers.append((keytup, a["net_pct_yr_of_core"], b["net_pct_yr_of_core"]))
    # Also report the softer counts so the failure mode is legible.
    n_net_both = _count_net_both(df, grouped)
    print(f"  cells with net>=1.56%/yr on BOTH windows           : {n_net_both}")
    print(f"  cells clearing the FULL bar (ex-DSR)               : {len(passers)}")
    for keytup, na, nb in passers:
        print(f"    PASS dte={keytup[0]} mgmt={keytup[1]} d={keytup[2]} {keytup[3]} "
              f"A={na*100:+.3f}%/yr B={nb*100:+.3f}%/yr")
    if not passers:
        print("    (none -- the defined-risk PCS fails the bar across the full grid, as the"
              " 2a validation predicted)")

    # honest overall sign read
    med_net = df["net_pct_yr_of_core"].median()
    frac_neg = float((df["net_pct_yr_of_core"] < 0).mean())
    print("\nHONEST READ")
    print(f"  median net%/yr across all cell-windows: {med_net*100:+.3f}")
    print(f"  fraction of cell-windows net-NEGATIVE : {frac_neg:.3f}")


def _regime_flat(row: pd.Series) -> bool:
    """True iff the cell's result does NOT live in one regime: no VIX tercile and no calendar
    year has a mean per-trade return whose SIGN is opposite the cell's headline net sign (and
    the headline is non-trivial). A conservative 'not concentrated in one regime' read used
    only for the pass/fail count -- the full per-bucket numbers are in the table."""
    headline = row["net_pct_yr_of_core"]
    if headline == 0 or not np.isfinite(headline):
        return False
    hs = np.sign(headline)
    rb = row["regime_buckets"] if isinstance(row["regime_buckets"], dict) else json.loads(row["regime_buckets"])
    for b, v in rb.get("vix_tercile", {}).items():
        if np.sign(v["mean_ret_pct"]) == -hs and abs(v["mean_ret_pct"]) > 1e-9:
            return False
    for y, v in rb.get("year", {}).items():
        if np.sign(v["mean_ret_pct"]) == -hs and abs(v["mean_ret_pct"]) > 1e-9:
            return False
    return True


def _count_net_both(df: pd.DataFrame, grouped) -> int:
    n = 0
    for keytup, g in grouped:
        by_win = {r["window"]: r["net_pct_yr_of_core"] for _, r in g.iterrows()}
        if set(by_win) == {"A", "B"} and by_win["A"] >= BAR_NET and by_win["B"] >= BAR_NET:
            n += 1
    return n


if __name__ == "__main__":
    df = run_grid()
    report(df)
