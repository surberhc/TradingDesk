"""
ma_experiment4.py — localize the early-exit margin by ENGINE.

Apply the 3% one-sided trend margin to one engine at a time (others off) and ask:
  (a) does THAT engine's margin alone flatten the whole-strategy lookback curve?
  (b) what does it cost/help on the base case and the 2008 GFC tail?

Then 'ALL' and 'ALL-but-duration' to decide whether the proven duration ban rules
should carry the margin at all.
"""
from __future__ import annotations

import pandas as pd
from ma_experiment import load_inputs, run_once, MA_GRID

inputs = load_inputs()

CONFIGS = {
    "PROD (no margin)":     {},
    "regime@3%":            {"REGIME_TREND_MARGIN": 0.03},
    "duration@3%":          {"DURATION_TREND_MARGIN": 0.03},
    "realasset@3%":         {"REALASSET_TREND_MARGIN": 0.03},
    "sector@3%":            {"SECTOR_TREND_MARGIN": 0.03},
    "ALL@3%":               {"MA_GATE_BUFFER_PCT": 0.03},
    "ALL-but-duration@3%":  {"MA_GATE_BUFFER_PCT": 0.03, "DURATION_TREND_MARGIN": 0.0},
}


def lookback_curve(cfg):
    fixed = {"STRESS_MA_DAYS": 200, **cfg}
    cs = [run_once(inputs, TREND_MA_DAYS=n, **fixed)["Calmar"] for n in MA_GRID]
    s = pd.Series(cs, index=MA_GRID)
    return s, float(s.max() - s.min()), float((s.max() - s.min()) / (abs(s.median()) + 1e-9))


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print(f"{'config':<22} | base@200 Cal/maxDD | 2008 Cal/maxDD | lookback Calmar 150..250 | spread(rel)")
    print("-" * 110)
    for name, cfg in CONFIGS.items():
        s, spread, rel = lookback_curve(cfg)
        base = run_once(inputs, TREND_MA_DAYS=200, STRESS_MA_DAYS=200, **cfg)
        gfc = run_once(inputs, start="2007-01-01", TREND_MA_DAYS=200, STRESS_MA_DAYS=200, **cfg)
        curve = " ".join(f"{v:.2f}" for v in s.values)
        flag = "ROBUST" if rel < 0.20 else ("mod" if rel < 0.35 else "FRAGILE")
        print(f"{name:<22} | {base['Calmar']:.3f}/{base['Max drawdown']:+.3f}   "
              f"| {gfc['Calmar']:.3f}/{gfc['Max drawdown']:+.3f} "
              f"| {curve} | {spread:.3f} ({rel:.0%}) {flag}")
