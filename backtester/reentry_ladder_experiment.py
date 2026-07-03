"""
reentry_ladder_experiment.py — evaluate the PRE-REGISTERED 3-rung re-entry scale-in
ladder against CONTROL and a beta-matched PLACEBO (research; adopts nothing).

Gate (control-first, placebo, per-episode):
  * CONTROL = frozen single-step re-entry (overlay OFF == production).
  * LADDER  = the ONE pre-registered ladder (1/3 -> 2/3 -> 1 over 3 rebalances after a
              re-entry; exits immediate & override).
  * PLACEBO = a flat/uniform equity haircut sized to the LADDER's OWN average
              under-exposure, spread across ALL rebalances (NOT concentrated at
              re-entry). If LADDER does not beat PLACEBO, the benefit is a beta artifact.

Reuses the repo's established episode windows (_sharp_recovery_test.EPISODES) and the
walk-forward OOS split (config.WALK_FORWARD_TRAIN_END). No grid, no tuning — one ladder.
"""
from __future__ import annotations
import warnings, sys, json
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

import numpy as np
import pandas as pd

from strategies import config
from src import data_loader, metrics
from src import reentry_ladder as rl

START = "2007-01-01"
VERSION = "Balanced"

# Established episode windows (identical to _sharp_recovery_test.EPISODES).
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


def headline(r: dict) -> dict:
    m = metrics.compute_metrics(r["benchmark_navs"])
    return {
        "CAGR": float(m.loc["CAGR", "strategy"]),
        "maxDD": float(m.loc["Max drawdown", "strategy"]),
        "Calmar": float(m.loc["Calmar", "strategy"]),
        "Sortino": float(m.loc["Sortino", "strategy"]),
        "AnnVol": float(m.loc["Annual volatility", "strategy"]),
    }


def episode_table(r: dict) -> dict:
    """Per-episode window maxDD and episode-end return (normalized to episode start)."""
    nav = r["benchmark_navs"]["strategy"]
    out = {}
    for name, lo, hi in EPISODES:
        sub = nav.loc[lo:hi]
        if len(sub) < 2:
            out[name] = {"mdd": np.nan, "ret": np.nan}
            continue
        mdd = float((sub / sub.cummax() - 1.0).min())
        ret = float(sub.iloc[-1] / sub.iloc[0] - 1.0)
        out[name] = {"mdd": mdd, "ret": ret}
    return out


def oos_split(r: dict) -> dict:
    train, test = metrics.split_walk_forward(r["benchmark_navs"], config.WALK_FORWARD_TRAIN_END)
    def grab(tbl):
        return {
            "CAGR": float(tbl.loc["CAGR", "strategy"]),
            "maxDD": float(tbl.loc["Max drawdown", "strategy"]),
            "Calmar": float(tbl.loc["Calmar", "strategy"]),
        }
    return {"train": grab(train), "test": grab(test)}


def pp(x):  # to percentage points
    return x * 100.0


def bp(a, b):
    """Change b-a in basis points (DD: more-negative = worse = negative bp)."""
    return (b - a) * 10000.0


def main():
    print("Loading inputs...")
    prices, yld, hyg, vix, oas = load_inputs()

    print("Running CONTROL (overlay OFF == production)...")
    ctrl = rl.run_laddered_backtest(prices, yld, hyg, vix, oas, start=START, version=VERSION, mode="control")
    print("Running LADDER (pre-registered 3-rung)...")
    lad = rl.run_laddered_backtest(prices, yld, hyg, vix, oas, start=START, version=VERSION, mode="ladder")

    # Placebo sized to the LADDER's OWN average under-exposure, spread flat.
    haircut = rl.average_under_exposure(prices, yld, hyg, vix, oas, start=START, version=VERSION)
    print(f"Ladder avg realized/engine equity ratio = {haircut:.4f}  -> placebo flat multiplier")
    print("Running PLACEBO (flat beta-matched haircut)...")
    plac = rl.run_laddered_backtest(prices, yld, hyg, vix, oas, start=START, version=VERSION,
                                    mode="placebo", placebo_haircut=haircut)

    ch, lh, ph = headline(ctrl), headline(lad), headline(plac)
    ce, le, pe = episode_table(ctrl), episode_table(lad), episode_table(plac)
    co, lo_, po = oos_split(ctrl), oos_split(lad), oos_split(plac)

    # How many rebalances did the ladder actually cap?
    lm = lad["monthly"]
    n_capped = int((lm["ladder_multiplier"] < 1.0 - 1e-9).sum())
    n_reentries = int(((lm["ladder_multiplier"] - lm["ladder_multiplier"].shift(1)).abs() > 1e-9).sum())

    out = {
        "haircut": haircut, "n_capped": n_capped,
        "headline": {"control": ch, "ladder": lh, "placebo": ph},
        "episodes": {"control": ce, "ladder": le, "placebo": pe},
        "oos": {"control": co, "ladder": lo_, "placebo": po},
    }

    # ---- console tables ----
    print("\n===== HEADLINE (full sample 2007->present) =====")
    print(f"{'metric':<10} {'CONTROL':>10} {'LADDER':>10} {'PLACEBO':>10}")
    for k in ("CAGR", "maxDD", "Calmar", "Sortino", "AnnVol"):
        print(f"{k:<10} {ch[k]:>10.4f} {lh[k]:>10.4f} {ph[k]:>10.4f}")

    print("\n===== PER-EPISODE (return / maxDD; dLADDER vs CONTROL) =====")
    print(f"{'episode':<15} {'C_ret':>7} {'L_ret':>7} {'dRet_bp':>8} | "
          f"{'C_mdd':>7} {'L_mdd':>7} {'dMDD_bp':>8}")
    for name, _, _ in EPISODES:
        c, l = ce[name], le[name]
        print(f"{name:<15} {pp(c['ret']):>6.1f}% {pp(l['ret']):>6.1f}% {bp(c['ret'],l['ret']):>8.0f} | "
              f"{pp(c['mdd']):>6.1f}% {pp(l['mdd']):>6.1f}% {bp(c['mdd'],l['mdd']):>8.0f}")

    print("\n===== PLACEBO CHECK (per-episode return: LADDER vs PLACEBO) =====")
    print(f"{'episode':<15} {'L_ret':>7} {'P_ret':>7} {'L-P_bp':>8}")
    for name, _, _ in EPISODES:
        l, p = le[name], pe[name]
        print(f"{name:<15} {pp(l['ret']):>6.1f}% {pp(p['ret']):>6.1f}% {bp(p['ret'],l['ret']):>8.0f}")

    print("\n===== OOS (walk-forward split @", config.WALK_FORWARD_TRAIN_END, ") =====")
    for half in ("train", "test"):
        print(f"  {half.upper():5} CAGR  C={co[half]['CAGR']:.4f} L={lo_[half]['CAGR']:.4f} P={po[half]['CAGR']:.4f}"
              f"   maxDD C={co[half]['maxDD']:.4f} L={lo_[half]['maxDD']:.4f} P={po[half]['maxDD']:.4f}")

    with open("output/_reentry_ladder_results.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print("\nWROTE output/_reentry_ladder_results.json")
    return out


if __name__ == "__main__":
    main()
