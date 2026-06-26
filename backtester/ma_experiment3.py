"""
ma_experiment3.py — validate the SYMMETRIC-hysteresis deadband (the clean form).

(1) Buffer-size robustness: for each band half-width, sweep the trend lookback
    150..250. We want a RANGE of buffers that all flatten the curve at a healthy
    level (=> neither the lookback nor the buffer is a fitted spike).
(2) Guardrails at the chosen buffer vs production: 3 versions (2015-26), 2008 GFC
    tail (start 2007), and a PROPER walk-forward (split the full-run NAV, not the
    buggy end= param).
"""
from __future__ import annotations

import pandas as pd
from strategies import config
from src import backtest, metrics
from ma_experiment import load_inputs, run_once, MA_GRID

inputs = load_inputs()
prices, yld, hyg, vix, oas = inputs


def lookback_curve(**fixed):
    cs = [run_once(inputs, TREND_MA_DAYS=n, **fixed)["Calmar"] for n in MA_GRID]
    s = pd.Series(cs, index=MA_GRID)
    return s, float(s.max() - s.min()), float((s.max() - s.min()) / (abs(s.median()) + 1e-9))


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
    s = navs.loc[lo:hi].copy()
    s = s / s.iloc[0]
    m = metrics.compute_metrics(s)
    return float(m.loc["Max drawdown", "strategy"]), float(m.loc["Calmar", "strategy"]), float(m.loc["CAGR", "strategy"])


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("### (1) SYMMETRIC buffer-size robustness (mode=sma, stress@200) ###")
    print("buffer | base@200 Calmar/maxDD | lookback-sweep Calmar(150..250) | spread(rel)")
    for buf in [0.0, 0.01, 0.02, 0.03, 0.04, 0.05]:
        fixed = {"MA_GATE_MODE": "sma", "MA_GATE_BUFFER_PCT": buf, "STRESS_MA_DAYS": 200}
        base = run_once(inputs, TREND_MA_DAYS=200, **fixed)
        s, spread, rel = lookback_curve(**fixed)
        curve = " ".join(f"{v:.2f}" for v in s.values)
        print(f"{buf:>5.3f}  | {base['Calmar']:.3f}/{base['Max drawdown']:+.3f} | {curve} | {spread:.3f} ({rel:.0%})")

    CAND = {"MA_GATE_MODE": "sma", "MA_GATE_BUFFER_PCT": 0.03, "STRESS_MA_DAYS": 200}
    print("\n### (2) Guardrails: PRODUCTION vs CANDIDATE (symmetric 3% deadband) ###")

    print("-- base case, 3 versions (2015-26): Calmar / maxDD / CAGR --")
    for ver in ("Conservative", "Balanced", "Growth"):
        p = run_once(inputs, version=ver)
        c = run_once(inputs, version=ver, **CAND)
        print(f"  {ver:13} PROD {p['Calmar']:.3f}/{p['Max drawdown']:+.3f}/{p['CAGR']:.3f}"
              f"   ->   CAND {c['Calmar']:.3f}/{c['Max drawdown']:+.3f}/{c['CAGR']:.3f}")

    print("-- 2008 GFC tail (start 2007) --")
    p = run_once(inputs, start="2007-01-01")
    c = run_once(inputs, start="2007-01-01", **CAND)
    print(f"  PROD {p['Calmar']:.3f}/{p['Max drawdown']:+.3f}/{p['CAGR']:.3f}"
          f"   ->   CAND {c['Calmar']:.3f}/{c['Max drawdown']:+.3f}/{c['CAGR']:.3f}")

    print("-- walk-forward (Balanced, NAV split) --")
    for tag, cfg in [("PROD", {}), ("CAND", CAND)]:
        nv = full_navs(**cfg)
        i = seg(nv, "2015-01-01", "2019-12-31")
        o = seg(nv, "2020-01-01")
        print(f"  {tag:5} in<=2019 maxDD {i[0]:+.3f} Calmar {i[1]:.3f}   |   OOS2020+ maxDD {o[0]:+.3f} Calmar {o[1]:.3f}")
