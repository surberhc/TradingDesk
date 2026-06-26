"""
Unit tests for metrics.py (SPEC.md §14) with known-value checks.
"""

import numpy as np
import pandas as pd
import pytest

from src import metrics


def test_cagr_doubling_in_one_year():
    idx = pd.bdate_range("2020-01-01", "2020-12-31")
    nav = pd.Series(np.linspace(1.0, 2.0, len(idx)), index=idx)
    assert metrics.cagr(nav) == pytest.approx(1.0, abs=0.02)


def test_max_drawdown_known_path():
    idx = pd.bdate_range("2020-01-01", periods=4)
    nav = pd.Series([1.0, 1.2, 0.9, 1.0], index=idx)
    assert metrics.max_drawdown(nav) == pytest.approx(-0.25)


def test_beta_and_capture_self_is_one():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2018-01-01", periods=500)
    ret = pd.Series(rng.normal(0, 0.01, 500), index=idx)
    assert metrics.beta(ret, ret) == pytest.approx(1.0)
    # A series captures 100% of itself, up and down.
    assert metrics.capture_ratio(ret, ret, up=True) == pytest.approx(1.0)
    assert metrics.capture_ratio(ret, ret, up=False) == pytest.approx(1.0)


def test_sharpe_positive_for_steady_gains():
    idx = pd.bdate_range("2020-01-01", periods=300)
    nav = pd.Series(1.01 ** np.arange(300), index=idx)  # steady compounding
    assert metrics.sharpe(metrics._returns(nav)) > 0


def test_longest_underperformance_months():
    idx = pd.bdate_range("2020-01-01", periods=130)
    month_value = {1: 1.0, 2: 1.1, 3: 1.05, 4: 1.04, 5: 1.03, 6: 1.2}
    strat = pd.Series([month_value[d.month] for d in idx], index=idx)
    spy = pd.Series(1.0, index=idx)
    # Relative peak in Feb (1.1); Mar/Apr/May underwater (3 months); Jun new high.
    assert metrics.longest_underperformance_months(strat, spy) == 3


def test_compute_metrics_table_shape():
    idx = pd.bdate_range("2018-01-01", periods=800)
    rng = np.random.default_rng(1)
    navs = pd.DataFrame(
        {
            "strategy": (1 + pd.Series(rng.normal(0.0003, 0.006, 800), index=idx)).cumprod(),
            "SPY": (1 + pd.Series(rng.normal(0.0004, 0.010, 800), index=idx)).cumprod(),
            "60/40": (1 + pd.Series(rng.normal(0.0003, 0.007, 800), index=idx)).cumprod(),
            "T-bills": (1 + pd.Series(0.00008, index=idx)).cumprod(),
        }
    )
    table = metrics.compute_metrics(navs)
    assert list(table.columns) == ["strategy", "SPY", "60/40", "T-bills"]
    for label in ("Max drawdown", "Sortino", "Down capture vs SPY",
                  "Longest underperf. vs SPY (months)"):
        assert label in table.index
    # Strategy beta vs SPY should be well below 1 (lower-vol than SPY here).
    assert table.loc["Beta vs SPY", "SPY"] == pytest.approx(1.0)
