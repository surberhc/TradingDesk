"""
robustness.py — Parameter-robustness (anti-curve-fit) analysis. SPEC.md §16.

For each key parameter, re-run the whole strategy across a WIDE range of values
(one knob at a time, others at default) and report how the risk metrics move. The
question is not "what's the best value" but "is our chosen value a lucky spike or
a point on a broad plateau?" A flat response = robust; a sharp peak at our default
= a red flag for over-fitting.

Data is loaded once and reused across every run (engines read config live, so
changing a config attribute takes effect without reloading prices).

Usage:
    from src import robustness
    res = robustness.run_robustness()        # default grid, Balanced
    robustness.print_report(res)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies import config
from src import backtest, data_loader, metrics

# Each knob -> a wide, sensible range centered on (and including) its default.
DEFAULT_GRID: dict[str, list] = {
    "REGIME_IMMEDIATE_DROP_POINTS": [8, 10, 12, 15, 20, 25, 30],  # the one we tuned
    # MA_LONG_DAYS used to be FRAGILE (sharp Calmar peak at 200). The regime-engine
    # early-exit margin (REGIME_TREND_MARGIN, adopted 2026-06-26) flattened it to a
    # plateau (~0.69-0.73 across 150-250). The margin itself is also a plateau (3-5%).
    "MA_LONG_DAYS": [150, 175, 200, 225, 250],
    "REGIME_TREND_MARGIN": [0.0, 0.01, 0.02, 0.03, 0.04, 0.05],
    "TREND_RETURN_MONTHS": [3, 6, 9, 12],
    "VOL_LOOKBACK_DAYS": [42, 63, 84, 126],
    "SLOPE_LOOKBACK_DAYS": [100, 150, 200, 250],
    "REGIME_CONFIRMATION_DAYS": [2, 3, 4],
    "REGIME_MIN_THRESHOLD_CROSS": [2, 3, 4, 6],
    "LONG_TSY_PERMISSION_MIN_PASSES": [3, 4, 5],
}

METRICS = ("CAGR", "Max drawdown", "Sortino", "Calmar")


def _load_inputs():
    prices = data_loader.load_prices()
    try:
        hyg = data_loader.load_prices(["HYG"])["HYG"]
    except (KeyError, FileNotFoundError):
        hyg = None
    yld, _ = data_loader.load_treasury_10y()
    vix, _ = data_loader.load_vix()
    oas, _ = data_loader.load_hy_oas()
    return prices, yld, hyg, vix, oas


def run_robustness(
    version: str = config.ACTIVE_VERSION,
    grid: dict[str, list] | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Sweep each parameter in `grid` one-at-a-time; return {param: metrics table}.

    Each table is indexed by the swept value with columns from METRICS plus the
    strategy's average per-rebalance turnover. The config default is always
    restored after each parameter.
    """
    grid = grid or DEFAULT_GRID
    prices, yld, hyg, vix, oas = _load_inputs()
    out: dict[str, pd.DataFrame] = {}

    for param, values in grid.items():
        original = getattr(config, param)
        rows = {}
        try:
            for v in values:
                setattr(config, param, v)
                r = backtest.run_backtest(prices, yld, hyg, vix, oas, version=version)
                t = metrics.compute_metrics(r["benchmark_navs"])
                rows[v] = {m: t.loc[m, "strategy"] for m in METRICS}
                rows[v]["turnover"] = r["turnover"].mean()
        finally:
            setattr(config, param, original)
        table = pd.DataFrame(rows).T
        table.index.name = param
        out[param] = table
    return out


def assess(table: pd.DataFrame, default_value, metric: str = "Calmar") -> dict:
    """
    Plateau-vs-peak verdict for one parameter on the chosen metric (higher=better
    for CAGR/Sortino/Calmar; Max drawdown is handled as 'less negative = better').
    """
    series = table[metric]
    better_high = metric != "Max drawdown"
    best_val = series.idxmax() if better_high else series.idxmax()  # all our metrics: higher better
    spread = float(series.max() - series.min())
    rel_spread = spread / (abs(series.median()) + 1e-9)
    default_metric = float(series.loc[default_value]) if default_value in series.index else np.nan
    is_peak = (best_val == default_value) and (
        series.drop(default_value).max() < series.loc[default_value] - 0.05 * abs(series.loc[default_value])
    )
    verdict = "FRAGILE (sharp peak at default)" if is_peak and rel_spread > 0.25 else (
        "robust (broad plateau)" if rel_spread < 0.20 else "moderate sensitivity"
    )
    return {
        "default": default_value, "default_metric": default_metric,
        "best_value": best_val, "spread": spread, "rel_spread": rel_spread,
        "verdict": verdict,
    }


def print_report(results: dict[str, pd.DataFrame], metric: str = "Calmar") -> None:
    """Print a per-parameter table and a plateau/peak verdict line."""
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"Parameter robustness (metric = {metric}; current config defaults marked *)\n")
    for param, table in results.items():
        default_value = getattr(config, param)
        a = assess(table, default_value, metric)
        cells = []
        for v in table.index:
            mark = "*" if v == default_value else " "
            cells.append(f"{mark}{v}={table.loc[v, metric]:.2f}")
        print(f"{param}")
        print("   " + "  ".join(cells))
        print(f"   -> {a['verdict']}  (range {a['spread']:.2f}, "
              f"rel {a['rel_spread']:.0%}; best at {a['best_value']})\n")
