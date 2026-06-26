"""
sweep.py — Parameter-sensitivity sweeps (SPEC.md §16).

Run the backtest across a grid of values for one config parameter and collect the
metrics that matter, so the user can test sensitivity (e.g. 50/200-day vs 10-month
trend, 3/6/12-month lookbacks, confirmation days). Works for any numeric config
knob the engines read live at call time (the lookbacks, thresholds, and counts).

Example:
    from src import sweep
    sweep.run_sweep("REGIME_CONFIRMATION_DAYS", [2, 3, 4])
    sweep.run_sweep("MA_LONG_DAYS", [150, 200, 250])
"""

from __future__ import annotations

import pandas as pd

from strategies import config
from src import backtest, metrics

DEFAULT_METRICS = ("CAGR", "Max drawdown", "Worst rolling 12m", "Sortino", "Calmar")


def run_sweep(
    param_name: str,
    values: list,
    metric_names: tuple[str, ...] = DEFAULT_METRICS,
    version: str = config.ACTIVE_VERSION,
    **backtest_kwargs,
) -> pd.DataFrame:
    """
    Backtest once per value of `param_name`, returning a table of strategy metrics
    indexed by the swept value. The original config value is always restored, even
    if a run raises.
    """
    if not hasattr(config, param_name):
        raise AttributeError(f"config has no parameter '{param_name}'")
    original = getattr(config, param_name)
    out: dict = {}
    try:
        for value in values:
            setattr(config, param_name, value)
            result = backtest.run_backtest(version=version, **backtest_kwargs)
            table = metrics.compute_metrics(result["benchmark_navs"])
            out[value] = {m: table.loc[m, "strategy"] for m in metric_names}
    finally:
        setattr(config, param_name, original)  # always restore

    frame = pd.DataFrame(out).T
    frame.index.name = param_name
    return frame
