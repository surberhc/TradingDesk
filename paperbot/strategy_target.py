"""
strategy_target.py — what the shared strategy wants to hold RIGHT NOW.

The paperbot must place the EXACT portfolio the backtester validated. The strategy's
target depends on the chain of prior monthly targets (the re-entry ladder and the
whipsaw "current holding wins ties" rule both read prev_weights), so the only faithful
way to get today's target is to run the validated month-by-month engine through today
and take its most recent rebalance. We reuse the backtester's own run_backtest for
that — no re-derivation, so paper == backtest by construction.

READ-ONLY: this loads price data and computes. It touches no broker and places no order.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# The backtester is a local `src` package (run via `python -m src.run`), not an
# installed dependency. Add its folder to the path so we can reuse its exact data
# assembly + simulation. Path is derived relative to this file (…/TradingDesk/paperbot
# -> …/TradingDesk/backtester), so it is independent of the current directory.
_BACKTESTER = Path(__file__).resolve().parent.parent / "backtester"
if str(_BACKTESTER) not in sys.path:
    sys.path.insert(0, str(_BACKTESTER))

from src import backtest, data_loader  # noqa: E402  (after sys.path setup)

import config  # paperbot config (STRATEGY_VERSION, etc.)


@dataclass
class Target:
    """The book the strategy wants to hold, plus the data context behind it."""
    weights: pd.Series        # ticker -> fraction of NAV (sums to ~1.0)
    prices: pd.Series         # ticker -> latest adjusted close (= live share price, Tiingo)
    as_of: pd.Timestamp       # the rebalance date these weights are for
    price_date: pd.Timestamp  # last date we have prices for (data-freshness check)
    version: str              # client version the weights were built for


def current_target(version: str = config.STRATEGY_VERSION) -> Target:
    """Run the validated engine through today and return its latest target book.

    `end=None` lets the backtester run to the most recent data date; the final row
    of its target-weights frame is the portfolio it wants to hold now.
    """
    result = backtest.run_backtest(version=version, end=None)
    weights_df = result["weights"]
    weights = weights_df.iloc[-1]
    weights = weights[weights > 1e-9]

    prices = data_loader.load_prices()
    price_date = prices.index[-1]
    # Latest available close per ticker (ffill covers a ticker that didn't print on
    # the very last date). Tiingo adjClose is anchored to the latest real price, so
    # the most recent value is the actual current share price for order sizing.
    latest = prices.ffill().loc[price_date]

    return Target(
        weights=weights,
        prices=latest,
        as_of=weights_df.index[-1],
        price_date=price_date,
        version=result["version"],
    )
