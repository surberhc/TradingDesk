"""
test_s8_vol.py — OFFLINE unit tests for the S8 realized-volatility helper (Phase 1,
entry-capture gap-close #2). NO broker, NO gateway, NO network.

Covers the PURE piece of s8_vol:
  * realized_vol_from_closes — annualized close-to-close realized vol from a synthetic
    bar series (hand-checked), plus graceful None on too-few-bars / NaN / non-positive /
    None / non-numeric inputs.

The LIVE piece (realized_vol_live) needs a real gateway and is exercised by the live smoke.

Run:
  cd livebot
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s8_vol.py -q
"""
from __future__ import annotations

import math
import statistics

import s8_vol


def _independent_annualized_rv(closes, annualization=252):
    """A from-scratch reimplementation used ONLY as an independent cross-check in the test
    (deliberately NOT calling s8_vol) — sample-stdev of daily log returns * sqrt(ann.)."""
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    return statistics.stdev(rets) * math.sqrt(annualization)


def test_realized_vol_matches_hand_checked_value():
    closes = [100.0, 102.0, 101.0, 103.0, 105.0]   # 4 daily log returns
    got = s8_vol.realized_vol_from_closes(closes)

    # Independent cross-check (separate code path from the implementation).
    expected = _independent_annualized_rv(closes)
    assert got == expected

    # Hand-checked literal: log returns ≈ [0.019803, -0.009852, 0.019609, 0.019231];
    # sample stdev ≈ 0.014702; * sqrt(252) ≈ 0.2334.
    assert abs(got - 0.2334) < 1e-3


def test_realized_vol_annualization_factor_applied():
    closes = [100.0, 102.0, 101.0, 103.0, 105.0]
    # With annualization=1 the result is just the raw sample stdev of the log returns.
    raw = s8_vol.realized_vol_from_closes(closes, annualization=1)
    assert abs(raw - statistics.stdev(
        [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))])) < 1e-12
    # sqrt(252) scaling relates the two.
    ann = s8_vol.realized_vol_from_closes(closes, annualization=252)
    assert abs(ann - raw * math.sqrt(252)) < 1e-12


def test_realized_vol_too_few_bars_returns_none():
    assert s8_vol.realized_vol_from_closes([]) is None
    assert s8_vol.realized_vol_from_closes([100.0]) is None
    # 2 closes -> only 1 return -> sample stdev undefined -> None.
    assert s8_vol.realized_vol_from_closes([100.0, 101.0]) is None
    # 3 closes -> 2 returns -> defined.
    assert s8_vol.realized_vol_from_closes([100.0, 101.0, 102.0]) is not None


def test_realized_vol_nan_close_returns_none():
    closes = [100.0, float("nan"), 102.0, 103.0]
    assert s8_vol.realized_vol_from_closes(closes) is None


def test_realized_vol_none_or_nonpositive_or_nonnumeric_returns_none():
    assert s8_vol.realized_vol_from_closes([100.0, None, 102.0, 103.0]) is None
    assert s8_vol.realized_vol_from_closes([100.0, 0.0, 102.0, 103.0]) is None
    assert s8_vol.realized_vol_from_closes([100.0, -5.0, 102.0, 103.0]) is None
    assert s8_vol.realized_vol_from_closes([100.0, "x", 102.0, 103.0]) is None
