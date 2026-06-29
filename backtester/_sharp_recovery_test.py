"""
_sharp_recovery_test.py — ANTI-CURVE-FIT research test of the regime `sharp_recovery`
refinement (RESEARCH ONLY; adopts nothing; mutates no canonical config/data/logic on disk).

What it does
------------
Injects a refined `sharp_recovery` trigger into the re-entry MAX-LAG override IN-PROCESS
(monkeypatch of strategies.all_weather._reentry_conditions, restored after each run), then
runs the canonical month-by-month backtest and scores:
  * headline metrics (CAGR / maxDD / Calmar)
  * a PER-EPISODE safety gate (episode-window maxDD AND episode-end NAV, before vs after),
    the same hard gate that killed MAX_LAG 6->3 last session.

The refinement (principled, economic — see report): a "sharp recovery" is a CLEAN V, which
has (i) POSITIVE rebound momentum right now and (ii) price that has climbed and STAYED well
above the trough it bounced from (no recent re-violation of the low). A sideways GRIND is
flat and keeps chopping back near its low. We add a causal V-shape gate on top of the
existing level test (score>stage4 & above-trend), parameterized by two knobs we SWEEP for a
plateau:
    V_MOM_MIN     : min trailing 6m SPY return to count the rebound as "rising"
    V_NOLOW_MIN   : min "recent-off-low" (price's last-3mo low must sit this far above the
                    trailing trough) to count the low as "held, not re-violated"

Everything is strictly causal (trailing windows sampled at/<= the signal date).
"""
from __future__ import annotations
import warnings, sys, copy
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

import numpy as np
import pandas as pd

from strategies import config
from src import backtest, data_loader, metrics
import strategies.all_weather as aw

START = "2007-01-01"
VERSION = "Balanced"

# Episode windows (peak->trough->recovery brackets), aligned with s4_reentry_analysis
# KNOWN_EPISODES plus the two SIDEWAYS-GRIND stretches that are the whole point of the
# refinement. Each is (label, lo, hi); episode maxDD is the worst drawdown inside [lo,hi]
# and episode-end NAV-vs-baseline is measured at hi.
EPISODES = [
    ("GFC 2008-09",   "2007-10-01", "2009-12-31"),
    ("2011 euro",     "2011-04-01", "2012-03-31"),
    ("2015-16 grind", "2015-05-01", "2016-12-31"),
    ("2018-Q4",       "2018-08-01", "2019-06-30"),
    ("COVID 2020",    "2020-01-01", "2020-12-31"),
    ("2022 bear",     "2021-12-01", "2023-06-30"),
]


def load_inputs():
    prices = data_loader.load_prices()
    bond_t = config.BENCHMARK_6040[1]
    if bond_t not in prices.columns:
        try:
            prices = prices.join(data_loader.load_prices([bond_t]))
        except Exception:
            pass
    try:
        hyg = data_loader.load_prices([config.CREDIT_PROXY[0]])[config.CREDIT_PROXY[0]]
    except Exception:
        hyg = None
    yld, _ = data_loader.load_treasury_10y()
    vix, _ = data_loader.load_vix()
    oas, _ = data_loader.load_hy_oas()
    return prices, yld, hyg, vix, oas


PRICES, YLD, HYG, VIX, OAS = load_inputs()
_ORIG_CONDS = aw._reentry_conditions   # the canonical builder we wrap


# ---------------------------------------------------------------------------
# Causal V-shape features (sampled at the signal dates) — for the refined gate.
# ---------------------------------------------------------------------------
def v_shape_features(prices: pd.DataFrame, signal_dates) -> pd.DataFrame:
    """For each signal date, trailing/causal V-shape descriptors of SPY:
        ret_6m       : trailing 6-month total return (rebound momentum)
        recent_off_low: (last-3mo low) / (trailing-18mo low) - 1  (re-violation tell)
    Both use only data on/before the date. Vectorized over the daily index, then
    sampled at the signal dates."""
    spy = prices["SPY"]
    idx = spy.index
    ret_6m = spy.pct_change(6 * 21)
    low18 = spy.rolling(18 * 21, min_periods=60).min()
    low3 = spy.rolling(3 * 21, min_periods=20).min()
    recent_off_low = low3 / low18 - 1.0
    feats = pd.DataFrame({"ret_6m": ret_6m, "recent_off_low": recent_off_low}, index=idx)
    return feats.reindex(pd.DatetimeIndex(signal_dates))


def make_patched_conditions(v_mom_min: float, v_nolow_min: float):
    """Return a drop-in replacement for aw._reentry_conditions that ANDs the existing
    sharp_recovery level test with a causal V-shape gate. Everything else byte-identical."""
    def patched(prices, score_df, confirmed_regime, realized, signal_dates, version):
        conds = _ORIG_CONDS(prices, score_df, confirmed_regime, realized, signal_dates, version)
        feats = v_shape_features(prices, signal_dates).reindex(conds.index)
        is_v = (feats["ret_6m"] >= v_mom_min) & (feats["recent_off_low"] >= v_nolow_min)
        conds["sharp_recovery"] = (conds["sharp_recovery"] & is_v.fillna(False))
        return conds
    return patched


# ---------------------------------------------------------------------------
# Run one backtest under (optional) refined trigger + (optional) max_lag override.
# ---------------------------------------------------------------------------
def run_backtest_variant(max_lag: int | None = None,
                         v_mom_min: float | None = None,
                         v_nolow_min: float | None = None) -> dict:
    """Run the canonical backtest with config/patches applied IN-PROCESS and restored.
    max_lag None -> use config default (6). v_* None -> no V-filter (canonical trigger)."""
    orig_lag = config.REENTRY_MAX_LAG_MONTHS
    orig_conds = aw._reentry_conditions
    try:
        if max_lag is not None:
            config.REENTRY_MAX_LAG_MONTHS = max_lag
        if v_mom_min is not None or v_nolow_min is not None:
            aw._reentry_conditions = make_patched_conditions(
                v_mom_min if v_mom_min is not None else -1e9,
                v_nolow_min if v_nolow_min is not None else -1e9,
            )
        r = backtest.run_backtest(PRICES, YLD, HYG, VIX, OAS, start=START, version=VERSION)
    finally:
        config.REENTRY_MAX_LAG_MONTHS = orig_lag
        aw._reentry_conditions = orig_conds
    return r


def headline(r: dict) -> dict:
    m = metrics.compute_metrics(r["benchmark_navs"])
    return {
        "CAGR": float(m.loc["CAGR", "strategy"]),
        "maxDD": float(m.loc["Max drawdown", "strategy"]),
        "Calmar": float(m.loc["Calmar", "strategy"]),
    }


def episode_table(r: dict) -> dict:
    """Per-episode window maxDD and episode-end NAV (normalized to episode start)."""
    nav = r["benchmark_navs"]["strategy"]
    out = {}
    for name, lo, hi in EPISODES:
        sub = nav.loc[lo:hi]
        if len(sub) < 2:
            out[name] = {"mdd": np.nan, "endnav": np.nan}
            continue
        mdd = float((sub / sub.cummax() - 1.0).min())
        endnav = float(sub.iloc[-1] / sub.iloc[0] - 1.0)
        out[name] = {"mdd": mdd, "endnav": endnav}
    return out


def bp(a, b):
    """Change b-a in basis points (for DD: more-negative = worse = negative bp)."""
    return (b - a) * 10000.0


def gate_table(base_ep: dict, var_ep: dict) -> str:
    lines = []
    lines.append(f"{'episode':<16} {'base_mdd':>9} {'var_mdd':>9} {'dMDD_bp':>8} | "
                 f"{'base_end':>9} {'var_end':>9} {'dEND_bp':>8}")
    lines.append("-"*78)
    worst_dd = 0.0
    for name, _, _ in EPISODES:
        b, v = base_ep[name], var_ep[name]
        ddbp = bp(b["mdd"], v["mdd"])      # negative = DD got worse (deeper)
        endbp = bp(b["endnav"], v["endnav"])
        worst_dd = min(worst_dd, ddbp)
        lines.append(f"{name:<16} {b['mdd']*100:>8.2f}% {v['mdd']*100:>8.2f}% {ddbp:>8.0f} | "
                     f"{b['endnav']*100:>8.2f}% {v['endnav']*100:>8.2f}% {endbp:>8.0f}")
    lines.append("-"*78)
    lines.append(f"WORST episode dMDD: {worst_dd:>+.0f} bp   (gate: must be >= -50 bp to pass)")
    return "\n".join(lines)


if __name__ == "__main__":
    import json
    print("Loaded. Running baseline + variants...\n")

    base = run_backtest_variant()  # canonical (max_lag=6, no V-filter)
    base_h, base_ep = headline(base), episode_table(base)
    print("BASELINE (max_lag=6, canonical):", {k: round(v,4) for k,v in base_h.items()})
    for n, d in base_ep.items():
        print(f"   {n:<16} mdd={d['mdd']*100:>7.2f}%  endNAV={d['endnav']*100:>7.2f}%")

    # --- Reproduce the prior failure: MAX_LAG=3 alone (no V-filter) ---
    print("\n" + "="*78)
    print("VARIANT A — MAX_LAG=3 ALONE (reproduce prior HELD failure):")
    a = run_backtest_variant(max_lag=3)
    a_h, a_ep = headline(a), episode_table(a)
    print("  headline:", {k: round(v,4) for k,v in a_h.items()})
    print(gate_table(base_ep, a_ep))

    # --- The refinement: MAX_LAG=3 + principled V-filter, swept for a plateau ---
    # Economic anchors (NOT reverse-engineered to 2015-16): a clean V is rising (ret_6m>0)
    # and has held above its trough (recent low well above the 18mo trough). Sweep both.
    MOM_GRID = [0.00, 0.02, 0.04, 0.06]            # min trailing-6m return
    NOLOW_GRID = [0.05, 0.10, 0.15, 0.20]          # min recent-off-low
    print("\n" + "="*78)
    print("VARIANT B — MAX_LAG=3 + V-FILTER, PLATEAU SWEEP (CAGR / worst-episode dMDD bp):")
    print(f"{'':>10}" + "".join(f"nolow={x:>5.2f}" for x in NOLOW_GRID))
    grid_cagr, grid_worst = {}, {}
    for mom in MOM_GRID:
        row_c, row_w = [], []
        for nolow in NOLOW_GRID:
            r = run_backtest_variant(max_lag=3, v_mom_min=mom, v_nolow_min=nolow)
            h, ep = headline(r), episode_table(r)
            worst = min(bp(base_ep[n]["mdd"], ep[n]["mdd"]) for n,_,_ in EPISODES)
            grid_cagr[(mom,nolow)] = h["CAGR"]; grid_worst[(mom,nolow)] = worst
            row_c.append(f"{h['CAGR']*100:>5.2f}/{worst:>+5.0f}")
        print(f"mom={mom:>4.2f} | " + "  ".join(row_c))

    # --- Detailed per-episode gate for a representative interior cell ---
    print("\n" + "="*78)
    pick = (0.02, 0.10)
    print(f"VARIANT B detail — representative interior cell mom={pick[0]}, nolow={pick[1]}:")
    rb = run_backtest_variant(max_lag=3, v_mom_min=pick[0], v_nolow_min=pick[1])
    rb_h, rb_ep = headline(rb), episode_table(rb)
    print("  headline:", {k: round(v,4) for k,v in rb_h.items()})
    print(gate_table(base_ep, rb_ep))

    # Persist machine-readable results for the report
    out = {
        "baseline": {"headline": base_h, "episodes": base_ep},
        "maxlag3_alone": {"headline": a_h, "episodes": a_ep},
        "vfilter_grid": {f"{m}_{n}": {"cagr": grid_cagr[(m,n)], "worst_dd_bp": grid_worst[(m,n)]}
                          for m in MOM_GRID for n in NOLOW_GRID},
        "vfilter_pick": {"cell": pick, "headline": rb_h, "episodes": rb_ep},
    }
    with open("output/_sharp_recovery_results.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print("\nWROTE output/_sharp_recovery_results.json\nDONE")
