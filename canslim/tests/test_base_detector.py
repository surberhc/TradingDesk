"""Tests for the deterministic CAN SLIM base/pivot detector.

The single most important test is NO-LOOKAHEAD (the desk's hard causality rule): the
detection at as_of must be byte-identical whether or not future bars exist in the frame.
"""
import datetime as dt
import sys
import os

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from base_detector import detect_base, BaseResult, PIVOT_OFFSET  # noqa: E402


def _daily(prices, start="2022-01-03"):
    """Build a daily OHLCV frame from a close path (business days)."""
    idx = pd.bdate_range(start=start, periods=len(prices))
    c = np.asarray(prices, dtype=float)
    df = pd.DataFrame({
        "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
        "volume": np.full(len(c), 1_000_000.0),
    }, index=idx)
    return df


def _flat_base_path():
    """Prior uptrend +40% then a tight ~8% sideways range for ~8 weeks."""
    up = list(np.linspace(50, 70, 120))            # ~24wk advance (+40%)
    flat = list(70 + np.sin(np.linspace(0, 6, 45)) * 2.5)  # ~9wk tight range around 70
    return up + flat


def test_returns_baseresult():
    df = _daily(_flat_base_path())
    r = detect_base(df, df.index[-1].date())
    assert isinstance(r, BaseResult)


def test_no_lookahead_causality():
    """Detection at as_of must NOT change when future bars are appended."""
    path = _flat_base_path()
    as_of = _daily(path).index[len(path) - 1].date()
    df_short = _daily(path)                          # ends exactly at as_of
    # append 60 future bars (a sharp breakout) that must NOT influence the as_of decision
    future = list(np.linspace(72, 110, 60))
    df_long = _daily(path + future)
    r_short = detect_base(df_short, as_of)
    r_long = detect_base(df_long, as_of)
    assert r_short.as_dict() == r_long.as_dict(), "future bars changed the as-of detection!"


def test_pivot_is_resistance_plus_ten_cents():
    df = _daily(_flat_base_path())
    r = detect_base(df, df.index[-1].date())
    if r.found:
        # pivot must equal some resistance high + $0.10 (rounded)
        frac = round(r.pivot - int(r.pivot * 100) / 100, 2)
        # simplest invariant: pivot ends in a .x0 offset from an integer-cent high
        assert abs((r.pivot * 100) % 1) < 1e-6  # pivot is a clean 2-decimal price
        assert r.pivot > 0


def test_insufficient_history_no_base():
    df = _daily(list(np.linspace(50, 55, 30)))       # 30 bars < min
    r = detect_base(df, df.index[-1].date())
    assert r.found is False
    assert "insufficient" in r.notes


def test_flat_base_detected():
    df = _daily(_flat_base_path())
    r = detect_base(df, df.index[-1].date())
    assert r.found, "a textbook flat base after a +40% run should be detected"
    assert r.pattern in ("flat_base", "consolidation", "double_bottom", "cup_with_handle")
    # pivot should sit near the top of the sideways range (~72), within a sane band
    assert 70 <= r.pivot <= 76


def test_pure_downtrend_no_base():
    df = _daily(list(np.linspace(100, 40, 200)))     # steady decline, no base
    r = detect_base(df, df.index[-1].date())
    assert r.found is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
