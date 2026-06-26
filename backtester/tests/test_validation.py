"""
Smoke tests for the validation harnesses (robustness sweep + Monte Carlo).
Kept tiny so the suite stays fast; skip cleanly if data is absent.
"""

import pytest

from src import data_loader, montecarlo, robustness

_data_ready = data_loader.DATA_PATH.exists() and any(data_loader.DATA_PATH.glob("*.parquet"))
pytestmark = pytest.mark.skipif(not _data_ready, reason="run the downloader first")


def test_robustness_runs_and_restores_config():
    from strategies import config
    orig = config.REGIME_CONFIRMATION_DAYS
    res = robustness.run_robustness(grid={"REGIME_CONFIRMATION_DAYS": [2, 3]})
    table = res["REGIME_CONFIRMATION_DAYS"]
    assert list(table.index) == [2, 3]
    assert "Calmar" in table.columns
    assert config.REGIME_CONFIRMATION_DAYS == orig  # restored


def test_montecarlo_runs():
    df = montecarlo.run_mc(n_paths=2, block=63, seed=1)
    assert len(df) == 2
    # Synthetic drawdowns are real negative numbers, and SPY is computed alongside.
    assert (df["Max drawdown"] < 0).all()
    assert "SPY_MaxDD" in df.columns
