"""
ma_experiment5.py — final guardrails for the REGIME-ONLY early-exit margin.

Candidate: REGIME_TREND_MARGIN = 0.03 (everything else production).
  (1) margin-size robustness scoped to regime (is 3% on a plateau?)
  (2) 3 client versions (2015-26)
  (3) proper walk-forward (NAV split)
  (4) 2008 GFC tail
"""
from __future__ import annotations

import pandas as pd
from strategies import config
from src import backtest, metrics
from ma_experiment import load_inputs, run_once, MA_GRID

inputs = load_inputs()
prices, yld, hyg, vix, oas = inputs
CAND = {"REGIME_TREND_MARGIN": 0.03}


def full_navs(version="Balanced", **cfg):
    saved = {k: getattr(config, k) for k in cfg}
    try:
        for k, v in cfg.items():
            setattr(config, k, v)
        return backtest.run_backtest(prices, yld, hyg, vix, oas, version=version,
                                     start="2015-01-01")["benchmark_navs"]
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


def seg(navs, lo, hi=None):
    s = navs.loc[lo:hi].copy(); s = s / s.iloc[0]
    m = metrics.compute_metrics(s)
    return float(m.loc["Max drawdown", "strategy"]), float(m.loc["Calmar", "strategy"])


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("### (1) margin-size robustness, REGIME scope (lookback swept 150..250) ###")
    print("margin | base@200 Cal/maxDD | lookback Calmar | spread(rel)")
    for m in [0.0, 0.01, 0.02, 0.03, 0.04, 0.05]:
        fixed = {"REGIME_TREND_MARGIN": m, "STRESS_MA_DAYS": 200}
        cs = [run_once(inputs, TREND_MA_DAYS=n, **fixed)["Calmar"] for n in MA_GRID]
        s = pd.Series(cs, index=MA_GRID)
        spread = float(s.max() - s.min()); rel = spread / (abs(s.median()) + 1e-9)
        base = run_once(inputs, TREND_MA_DAYS=200, **fixed)
        print(f"{m:>5.3f}  | {base['Calmar']:.3f}/{base['Max drawdown']:+.3f} | "
              f"{' '.join(f'{v:.2f}' for v in s.values)} | {spread:.3f} ({rel:.0%})")

    print("\n### (2) base case, 3 versions (2015-26): Calmar / maxDD / CAGR ###")
    for ver in ("Conservative", "Balanced", "Growth"):
        p = run_once(inputs, version=ver); c = run_once(inputs, version=ver, **CAND)
        print(f"  {ver:13} PROD {p['Calmar']:.3f}/{p['Max drawdown']:+.3f}/{p['CAGR']:.3f}"
              f"   ->   CAND {c['Calmar']:.3f}/{c['Max drawdown']:+.3f}/{c['CAGR']:.3f}")

    print("\n### (3) walk-forward (Balanced, NAV split) ###")
    for tag, cfg in [("PROD", {}), ("CAND", CAND)]:
        nv = full_navs(**cfg)
        i = seg(nv, "2015-01-01", "2019-12-31"); o = seg(nv, "2020-01-01")
        print(f"  {tag:5} in<=2019 maxDD {i[0]:+.3f} Calmar {i[1]:.3f}   |   OOS2020+ maxDD {o[0]:+.3f} Calmar {o[1]:.3f}")

    print("\n### (4) 2008 GFC tail (start 2007) ###")
    p = run_once(inputs, start="2007-01-01"); c = run_once(inputs, start="2007-01-01", **CAND)
    print(f"  PROD {p['Calmar']:.3f}/{p['Max drawdown']:+.3f}/{p['CAGR']:.3f}"
          f"   ->   CAND {c['Calmar']:.3f}/{c['Max drawdown']:+.3f}/{c['CAGR']:.3f}")
