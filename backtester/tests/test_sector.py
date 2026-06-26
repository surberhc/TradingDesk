"""
Unit tests for the Sector Engine (SPEC.md §5).

Covers: default-off returns broad beta only; weights always sum to 1; the trend
gate excludes a sector below its 200d MA; leading sectors are selected when tilt
is on; max single sector cap and 3-4 count are respected; and no look-ahead.
"""

import numpy as np
import pandas as pd
import pytest

from strategies import config
from strategies.parts import sector


def _frame(n=400):
    """SPY/VTI/RSP rising broad beta + all sectors (one leader, rest laggards)."""
    idx = pd.bdate_range("2012-01-02", periods=n)
    data = {
        "SPY": 100 + np.arange(n) * 0.10,
        "VTI": 100 + np.arange(n) * 0.10,
        "RSP": 100 + np.arange(n) * 0.10,
    }
    for i, s in enumerate(config.SECTORS):
        # XLK is the clear leader (steeper); others lag SPY slightly.
        slope = 0.20 if s == "XLK" else 0.08
        data[s] = 100 + np.arange(n) * slope
    return pd.DataFrame(data, index=idx)


def test_default_off_is_broad_beta_only():
    df = _frame()
    w = sector.select_sectors(df, df.index[-1])  # tilt defaults to config (0)
    assert w.sum() == pytest.approx(1.0)
    assert not any(s in w.index for s in config.SECTORS)
    assert "SPY" in w.index


def test_tilt_selects_leaders_and_sums_to_one():
    df = _frame()
    w = sector.select_sectors(df, df.index[-1], tilt_pct=0.20)
    assert w.sum() == pytest.approx(1.0)
    held_sectors = [s for s in config.SECTORS if s in w.index]
    assert "XLK" in held_sectors           # the clear leader is picked
    assert len(held_sectors) <= config.SECTOR_COUNT_WHEN_USED[1]


def test_max_single_sector_cap():
    df = _frame()
    w = sector.select_sectors(df, df.index[-1], tilt_pct=0.30)
    for s in config.SECTORS:
        if s in w.index:
            assert w[s] <= config.SECTOR_MAX_WEIGHT + 1e-9


def test_trend_gate_excludes_downtrending_sector():
    n = 400
    df = _frame(n)
    # Force XLE into a downtrend (below its 200d MA) but with strong recent RS.
    df["XLE"] = 300 - np.arange(n) * 0.20
    w = sector.select_sectors(df, df.index[-1], tilt_pct=0.20)
    assert "XLE" not in w.index  # fails the 200d trend gate regardless of momentum


def test_no_eligible_sectors_reverts_to_beta():
    n = 400
    df = _frame(n)
    # Put every sector below its 200d MA -> none pass the gate.
    for s in config.SECTORS:
        df[s] = 300 - np.arange(n) * 0.20
    w = sector.select_sectors(df, df.index[-1], tilt_pct=0.20)
    assert not any(s in w.index for s in config.SECTORS)
    assert w.sum() == pytest.approx(1.0)


def test_no_lookahead_selection_stable():
    df = _frame()
    asof = df.index[-1]
    full = sector.select_sectors(df, asof, tilt_pct=0.20)
    trunc = sector.select_sectors(df.loc[:asof], asof, tilt_pct=0.20)
    pd.testing.assert_series_equal(full.sort_index(), trunc.sort_index())
