"""
ma_experiment.py — 200-day MA fragility study (research scratch, not production).

Measures whether the strategy's dependence on the 200d MA is a lucky spike or a
broad plateau, and tests fixes. The TREND role was localized as the fragile one
(STRESS baselines are robust), so the fixes target the trend gates only:

  * Opt 3 (diagnostic): split MA_LONG_DAYS into TREND vs STRESS roles, sweep each.
  * Opt 1 (ensemble):  MA_GATE_MODE="ensemble" — vote across several lookbacks.
  * Opt 2 (EMA/buffer): MA_GATE_MODE="ema", and SMA + MA_GATE_BUFFER_PCT deadband.

Measuring stick: Calmar across the swept range (flat = robust). Guardrails on the
chosen winner: base-case (200) intact, 2008 GFC tail preserved, walk-forward holds.

Run:  python -m ma_experiment      (from backtester/, with the venv)
"""
from __future__ import annotations

import pandas as pd

from strategies import config
from src import backtest, data_loader, metrics

METRICS = ("CAGR", "Max drawdown", "Sortino", "Calmar")
MA_GRID = [150, 175, 200, 225, 250]


def load_inputs():
    prices = data_loader.load_prices()
    try:
        hyg = data_loader.load_prices(["HYG"])["HYG"]
    except (KeyError, FileNotFoundError):
        hyg = None
    yld, _ = data_loader.load_treasury_10y()
    vix, _ = data_loader.load_vix()
    oas, _ = data_loader.load_hy_oas()
    return prices, yld, hyg, vix, oas


def run_once(inputs, version="Balanced", start=None, end=None, **cfg):
    """Run one backtest with temporary config overrides (**cfg); always restore.

    `start`/`end` are run_backtest window args, not config attrs.
    """
    prices, yld, hyg, vix, oas = inputs
    saved = {k: getattr(config, k) for k in cfg}
    bt_kw = {}
    if start is not None:
        bt_kw["start"] = start
    if end is not None:
        bt_kw["end"] = end
    try:
        for k, v in cfg.items():
            setattr(config, k, v)
        r = backtest.run_backtest(prices, yld, hyg, vix, oas, version=version, **bt_kw)
        t = metrics.compute_metrics(r["benchmark_navs"])
        row = {m: float(t.loc[m, "strategy"]) for m in METRICS}
        row["turnover"] = float(r["turnover"].mean())
        return row
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


def sweep(inputs, axis_attr, values, fixed=None, keylabels=None, version="Balanced"):
    """Sweep one config attr over `values`, with `fixed` attrs held for every run."""
    fixed = fixed or {}
    rows = {}
    for i, v in enumerate(values):
        key = keylabels[i] if keylabels else v
        rows[key] = run_once(inputs, version=version, **{axis_attr: v}, **fixed)
    table = pd.DataFrame(rows).T
    table.index.name = axis_attr
    return table


def show(table, label):
    print(f"\n=== {label} ===")
    for v in table.index:
        print(f"  {str(v):<16} Calmar {table.loc[v,'Calmar']:.3f}  "
              f"maxDD {table.loc[v,'Max drawdown']:.3f}  "
              f"CAGR {table.loc[v,'CAGR']:.3f}  Sortino {table.loc[v,'Sortino']:.3f}")
    c = table["Calmar"]
    spread = float(c.max() - c.min())
    rel = spread / (abs(c.median()) + 1e-9)
    verdict = ("ROBUST plateau" if rel < 0.20 else
               "moderate sensitivity" if rel < 0.35 else "FRAGILE")
    print(f"   -> Calmar spread {spread:.3f} (rel {rel:.0%})  => {verdict}")
    return spread, rel


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    inputs = load_inputs()
    print("data span:", inputs[0].index.min().date(), "->", inputs[0].index.max().date())
    print("Baseline (config as-is):", {k: round(v, 4) for k, v in run_once(inputs).items()})

    # 1) Measuring stick: combined knob, and the trend-only baseline (apples-to-apples).
    show(sweep(inputs, "MA_LONG_DAYS", MA_GRID), "BASELINE: combined 200d knob")
    show(sweep(inputs, "TREND_MA_DAYS", MA_GRID, fixed={"STRESS_MA_DAYS": 200}),
         "BASELINE-TREND: SMA trend gates swept (stress @200)  <-- the curve to flatten")

    # 2) Opt 1 — ensemble: shift the 3-lookback window's CENTER; half-width 50.
    centers = MA_GRID
    ens_windows = [(c - 50, c, c + 50) for c in centers]
    show(sweep(inputs, "MA_ENSEMBLE_LOOKBACKS", ens_windows,
               fixed={"MA_GATE_MODE": "ensemble", "STRESS_MA_DAYS": 200},
               keylabels=[f"ctr{c}" for c in centers]),
         "OPT1 ENSEMBLE: 3-lookback vote, center swept (150/200/250 at ctr200)")

    # tighter ensemble for reference
    show(sweep(inputs, "MA_ENSEMBLE_LOOKBACKS", [(c - 25, c, c + 25) for c in centers],
               fixed={"MA_GATE_MODE": "ensemble", "STRESS_MA_DAYS": 200},
               keylabels=[f"ctr{c}" for c in centers]),
         "OPT1 ENSEMBLE (tight +/-25): center swept")

    # 3) Opt 2a — EMA: single EMA(span) trend gate, span swept.
    show(sweep(inputs, "TREND_MA_DAYS", MA_GRID,
               fixed={"MA_GATE_MODE": "ema", "STRESS_MA_DAYS": 200}),
         "OPT2a EMA: EMA(span) trend gates swept (stress @200)")

    # 4) Opt 2b — SMA + buffer deadband (2%), lookback swept.
    show(sweep(inputs, "TREND_MA_DAYS", MA_GRID,
               fixed={"MA_GATE_MODE": "sma", "MA_GATE_BUFFER_PCT": 0.02, "STRESS_MA_DAYS": 200}),
         "OPT2b SMA+2% buffer: trend gates swept (stress @200)")
