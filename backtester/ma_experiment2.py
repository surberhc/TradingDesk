"""
ma_experiment2.py — drill into the winner (SMA + buffer deadband) + guardrails.

(1) Is the BUFFER SIZE itself a fitted knob, or do a range of buffers all flatten
    the lookback sensitivity while keeping the level healthy?
(2) Guardrails on the candidate vs production baseline:
      - base case (all three versions, 2015-26)
      - 2008 GFC tail (start 2007)
      - walk-forward (build <=2019, test >2019)
    A real fix flattens the curve WITHOUT wrecking any of these.
"""
from __future__ import annotations

import pandas as pd
from strategies import config
from ma_experiment import load_inputs, run_once, MA_GRID

CAND = {"MA_GATE_MODE": "sma", "MA_GATE_BUFFER_PCT": 0.02, "STRESS_MA_DAYS": 200}


def lookback_spread(inputs, fixed):
    cs = []
    for n in MA_GRID:
        cs.append(run_once(inputs, TREND_MA_DAYS=n, **fixed)["Calmar"])
    s = pd.Series(cs, index=MA_GRID)
    spread = float(s.max() - s.min())
    return s, spread, spread / (abs(s.median()) + 1e-9)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    inputs = load_inputs()

    print("### (1) Buffer-size robustness: does a RANGE of buffers flatten the curve? ###")
    print("buffer | base@200 Calmar/maxDD | lookback-sweep Calmar(150..250) | spread(rel)")
    for buf in [0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.04]:
        fixed = {"MA_GATE_MODE": "sma", "MA_GATE_BUFFER_PCT": buf, "STRESS_MA_DAYS": 200}
        base = run_once(inputs, TREND_MA_DAYS=200, **fixed)
        s, spread, rel = lookback_spread(inputs, fixed)
        curve = " ".join(f"{v:.2f}" for v in s.values)
        print(f"{buf:>5.3f}  | {base['Calmar']:.3f}/{base['Max drawdown']:+.3f} "
              f"| {curve} | {spread:.3f} ({rel:.0%})")

    print("\n### (2) Guardrails: PRODUCTION (sma, no buffer) vs CANDIDATE (sma+2% buffer) ###")

    def line(tag, **ov):
        r = run_once(inputs, **ov)
        print(f"  {tag:<34} CAGR {r['CAGR']:.3f}  maxDD {r['Max drawdown']:+.3f}  "
              f"Sortino {r['Sortino']:.3f}  Calmar {r['Calmar']:.3f}  turn {r['turnover']:.3f}")

    print("-- base case, 3 versions (2015-26) --")
    for ver in ("Conservative", "Balanced", "Growth"):
        line(f"PROD   {ver}", version=ver)
        line(f"CAND   {ver}", version=ver, **CAND)

    print("-- 2008 GFC tail (start 2007) --")
    line("PROD   2007-start", start="2007-01-01")
    line("CAND   2007-start", start="2007-01-01", **CAND)

    print("-- walk-forward (Balanced) --")
    line("PROD   in-sample <=2019", start="2015-01-01", end="2019-12-31")
    line("CAND   in-sample <=2019", start="2015-01-01", end="2019-12-31", **CAND)
    line("PROD   OOS 2020+",        start="2020-01-01")
    line("CAND   OOS 2020+",        start="2020-01-01", **CAND)

    print("-- belt&suspenders: ensemble(150/200/250) + 2% buffer (Balanced 2015-26) --")
    line("CAND+ENS  ", MA_GATE_MODE="ensemble", MA_ENSEMBLE_LOOKBACKS=(150, 200, 250),
         MA_GATE_BUFFER_PCT=0.02, STRESS_MA_DAYS=200)
