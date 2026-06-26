"""
No-look-ahead tests (SPEC.md §16 — the single most important correctness rule).

Two integrated checks against the real backtest:
  1. Shifting execution to T+1 changes results vs same-day (T) execution — proof
     the execution lag is actually wired through the simulation.
  2. Signals never use future data — target weights for rebalances before a cutoff
     are byte-for-byte identical whether or not later price data exists.

Both run against the downloaded data and skip cleanly if it is absent.
"""

import pandas as pd
import pytest

from src import backtest, data_loader

_data_ready = data_loader.DATA_PATH.exists() and any(data_loader.DATA_PATH.glob("*.parquet"))
pytestmark = pytest.mark.skipif(_data_ready is False, reason="run the downloader first")


@pytest.fixture(scope="module")
def inputs():
    prices = data_loader.load_prices()
    try:
        hyg = data_loader.load_prices(["HYG"])["HYG"]
    except (KeyError, FileNotFoundError):
        hyg = None
    yld, _ = data_loader.load_treasury_10y()
    return prices, hyg, yld


def test_t_plus_one_changes_results(inputs):
    prices, hyg, yld = inputs
    lag1 = backtest.run_backtest(prices, yld, hyg, start="2018-01-01", execution_lag_days=1)
    lag0 = backtest.run_backtest(prices, yld, hyg, start="2018-01-01", execution_lag_days=0)
    # Same signals, different execution timing -> the NAV paths must differ.
    f1, f0 = lag1["nav"].iloc[-1], lag0["nav"].iloc[-1]
    assert f1 != pytest.approx(f0), "T+1 vs same-day execution produced identical NAVs"


def test_signals_never_use_future_data(inputs):
    prices, hyg, yld = inputs
    cutoff = pd.Timestamp("2021-06-30")

    full = backtest.run_backtest(prices, yld, hyg, start="2018-01-01")
    trunc = backtest.run_backtest(
        prices.loc[:cutoff],
        yld.loc[:cutoff] if yld is not None else None,
        hyg.loc[:cutoff] if hyg is not None else None,
        start="2018-01-01",
    )

    # Every rebalance the truncated run produced must match the full run exactly:
    # removing future data cannot change a past signal.
    common = trunc["weights"].index.intersection(full["weights"].index)
    assert len(common) > 12  # several years of monthly rebalances
    a = full["weights"].loc[common].fillna(0.0)
    b = trunc["weights"].loc[common].reindex(columns=a.columns).fillna(0.0)
    pd.testing.assert_frame_equal(a, b, atol=1e-12, check_like=True)
