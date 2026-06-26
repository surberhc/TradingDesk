"""
Tests for the §11/§16 refinements: taxable turnover bands, walk-forward split,
and the parameter-sweep harness. These run against the downloaded data and skip
cleanly if it is absent.
"""

import pandas as pd
import pytest

from strategies import config
from src import backtest, data_loader, metrics, sweep

_data_ready = data_loader.DATA_PATH.exists() and any(data_loader.DATA_PATH.glob("*.parquet"))
pytestmark = pytest.mark.skipif(not _data_ready, reason="run the downloader first")


@pytest.fixture(scope="module")
def inputs():
    prices = data_loader.load_prices()
    try:
        hyg = data_loader.load_prices(["HYG"])["HYG"]
    except (KeyError, FileNotFoundError):
        hyg = None
    yld, _ = data_loader.load_treasury_10y()
    return prices, hyg, yld


def test_taxable_band_reduces_turnover(inputs):
    prices, hyg, yld = inputs
    base = backtest.run_backtest(prices, yld, hyg, start="2018-01-01", taxable_mode=False)
    taxed = backtest.run_backtest(
        prices, yld, hyg, start="2018-01-01", taxable_mode=True, turnover_band=0.02
    )
    # The no-trade band must lower average turnover and still produce valid weights.
    assert taxed["turnover"].mean() < base["turnover"].mean()
    assert taxed["weights"].sum(axis=1).round(6).eq(1.0).all()


def test_walk_forward_split_shapes(inputs):
    prices, hyg, yld = inputs
    result = backtest.run_backtest(prices, yld, hyg, start="2016-01-01")
    train, test = metrics.split_walk_forward(result["benchmark_navs"], "2019-12-31")
    assert "CAGR" in train.index and "CAGR" in test.index
    # Train ends in 2019; test starts in 2020 — disjoint, non-empty windows.
    assert train.loc["Max drawdown", "strategy"] <= 0
    assert test.loc["Max drawdown", "strategy"] <= 0


def test_sweep_runs_and_restores_config(inputs):
    original = config.REGIME_CONFIRMATION_DAYS
    table = sweep.run_sweep(
        "REGIME_CONFIRMATION_DAYS", [2, 4], start="2020-01-01"
    )
    assert list(table.index) == [2, 4]
    assert "Max drawdown" in table.columns
    assert config.REGIME_CONFIRMATION_DAYS == original  # restored after the sweep


def test_sweep_restores_config_on_error():
    original = config.MA_LONG_DAYS
    with pytest.raises(Exception):
        # A bogus (non-numeric) value makes a run raise; config must still restore.
        sweep.run_sweep("MA_LONG_DAYS", ["not-a-number"], start="2022-01-01")
    assert config.MA_LONG_DAYS == original


def test_rebalance_frequency_changes_cadence(inputs):
    prices, hyg, yld = inputs
    monthly = backtest.run_backtest(prices, yld, hyg, start="2018-01-01", rebalance_frequency="monthly")
    weekly = backtest.run_backtest(prices, yld, hyg, start="2018-01-01", rebalance_frequency="weekly")
    # Weekly rebalances ~4x as often; both still produce valid full-weight books.
    assert len(weekly["weights"]) > 3 * len(monthly["weights"])
    assert weekly["weights"].sum(axis=1).round(6).eq(1.0).all()
    with pytest.raises(ValueError):
        backtest.run_backtest(prices, yld, hyg, start="2022-01-01", rebalance_frequency="hourly")


def test_backtest_uses_real_vix_when_downloaded(inputs):
    prices, hyg, yld = inputs
    vix, _ = data_loader.load_vix()
    if vix is None:
        pytest.skip("VIX not downloaded")
    result = backtest.run_backtest(prices, yld, hyg, start="2018-01-01")
    assert result["vix_is_real"] is True  # real VIX is loaded from data/ and used
