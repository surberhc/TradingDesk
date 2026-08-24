"""
Unit tests for the Phase 2 sector MOMENTUM OVERLAY (prereg section 5).

Covers: default OFF; the core is PERMANENT (no sector can ever be zeroed, which is the whole
safety argument after Phase 0 measured the anti-oracle at -46.91%); weights sum to 1; the
skip month is real; percentile scoring; eligibility needs BOTH top-N and the 10-month trend;
per-sector tactical caps hold; unplaced budget returns to the core and never to cash; and the
score is causal.
"""

import numpy as np
import pandas as pd
import pytest

from strategies import config
from strategies.parts import sector


@pytest.fixture(autouse=True)
def _clean():
    n_prev, m_prev = config.SECTOR_NEUTRAL_ENABLED, config.SECTOR_MOMENTUM_ENABLED
    config.SECTOR_NEUTRAL_ENABLED = False
    config.SECTOR_MOMENTUM_ENABLED = False
    sector._NEUTRAL_CACHE.clear()
    yield
    config.SECTOR_NEUTRAL_ENABLED, config.SECTOR_MOMENTUM_ENABLED = n_prev, m_prev
    sector._NEUTRAL_CACHE.clear()


def _frame(n=900, end="2026-08-21"):
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    data = {"SPY": 100 + np.arange(n) * 0.10, "RSP": 100 + np.arange(n) * 0.08}
    for i, s in enumerate(config.SECTORS):
        data[s] = 100 * (1.0 + 0.0002 * (i + 1)) ** np.arange(n)
    return pd.DataFrame(data, index=idx)


def test_flags_default_off():
    assert config.SECTOR_MOMENTUM_ENABLED is False


def test_score_weights_sum_to_one():
    assert sum(config.SECTOR_SCORE_WEIGHTS.values()) == pytest.approx(1.0)


def test_core_is_permanent_no_sector_is_ever_zeroed():
    """The safety property. Phase 0: the anti-oracle drew -46.91% vs -10.08% baseline."""
    px = _frame()
    config.SECTOR_NEUTRAL_ENABLED = True
    config.SECTOR_MOMENTUM_ENABLED = True
    w = sector.momentum_overlay(px, px.index[-1])
    assert len(w) == 11
    assert (w > 0).all(), f"zeroed: {list(w[w <= 0].index)}"
    assert w.sum() == pytest.approx(1.0)


def test_every_sector_keeps_at_least_its_core_share():
    px = _frame()
    config.SECTOR_NEUTRAL_ENABLED = True
    neutral = sector.neutral_weights(px, px.index[-1])
    w = sector.momentum_overlay(px, px.index[-1])
    for t in neutral.index:
        assert w[t] >= neutral[t] * config.SECTOR_CORE_PCT - 1e-9


def test_skip_month_actually_skips():
    """A spike inside the skipped month must NOT move the 12-1 score."""
    px = _frame()
    asof = px.index[-1]
    base = sector._skip_month_return(px["XLK"], asof, 12)
    spiked = px["XLK"].copy()
    spiked.iloc[-10:] *= 3.0                      # inside the skipped final month
    assert sector._skip_month_return(spiked, asof, 12) == pytest.approx(base)


def test_percentile_scores_rank_best_at_100():
    out = sector._percentile_scores({"a": 0.1, "b": 0.5, "c": 0.3})
    assert out["b"] == pytest.approx(100.0)
    assert out["a"] == pytest.approx(0.0)
    assert 0 < out["c"] < 100


def test_unmeasurable_sector_scores_zero_not_midpack():
    out = sector._percentile_scores({"a": float("nan"), "b": 0.5})
    assert out["a"] == 0.0
    assert out["b"] == pytest.approx(100.0)


def test_eligibility_requires_the_absolute_trend_too():
    """Top-N alone is not enough - that is the 'best-performing loser' guard."""
    px = _frame()
    asof = px.index[-1]
    falling = px.copy()
    best = sector.composite_scores(px, asof).index[0]
    falling[best] = falling[best].iloc[0] * np.linspace(1.0, 0.4, len(falling))
    assert sector._above_10m(px[best], asof) is True
    assert sector._above_10m(falling[best], asof) is False


def test_tactical_add_respects_the_per_sector_caps():
    px = _frame()
    asof = px.index[-1]
    config.SECTOR_NEUTRAL_ENABLED = True
    neutral = sector.neutral_weights(px, asof)
    w = sector.momentum_overlay(px, asof)
    leftover_max = max(0.0, 1.0 - config.SECTOR_CORE_PCT)
    for t in neutral.index:
        cap = min(config.SECTOR_TACTICAL_MAX_ADD_PTS,
                  config.SECTOR_TACTICAL_MAX_ADD_MULT * float(neutral[t]))
        add = w[t] - float(neutral[t]) * config.SECTOR_CORE_PCT
        # tactical add, plus at most this sector's pro-rata share of unplaced budget
        assert add <= cap + float(neutral[t]) * leftover_max + 1e-9


def test_full_core_means_no_tilt():
    px = _frame()
    asof = px.index[-1]
    config.SECTOR_NEUTRAL_ENABLED = True
    prev = config.SECTOR_CORE_PCT
    try:
        config.SECTOR_CORE_PCT = 1.0
        w = sector.momentum_overlay(px, asof)
        neutral = sector.neutral_weights(px, asof)
        assert (w - neutral).abs().max() == pytest.approx(0.0, abs=1e-9)
    finally:
        config.SECTOR_CORE_PCT = prev


def test_unplaced_budget_returns_to_equities_never_cash():
    """Weights must still sum to 1 across the sectors only - no cash leaks in."""
    px = _frame()
    config.SECTOR_NEUTRAL_ENABLED = True
    for core in (0.60, 0.70, 0.80):
        prev = config.SECTOR_CORE_PCT
        try:
            config.SECTOR_CORE_PCT = core
            w = sector.momentum_overlay(px, px.index[-1])
            assert set(w.index) <= set(config.SECTORS)
            assert w.sum() == pytest.approx(1.0)
        finally:
            config.SECTOR_CORE_PCT = prev


def test_composite_score_is_causal():
    """Appending future bars must not change a past score."""
    px = _frame(end="2027-06-30")
    asof = pd.Timestamp("2026-08-21")
    full = sector.composite_scores(px, asof)
    trunc = sector.composite_scores(px.loc[:asof], asof)
    assert (full - trunc).abs().max() == pytest.approx(0.0, abs=1e-9)


def test_cascade_keeps_tactical_money_inside_eligible_sectors():
    """The Amendment-2 fix: unplaced budget is RE-OFFERED to eligible sectors with room
    before any of it falls back to the strategic core.

    Without the cascade, section 15 sent stranded budget pro rata across all eleven — funding
    the six sectors that had just FAILED the eligibility screen. Measured 2026-08-21: 2.03 of
    30 tactical points went to non-eligible sectors.
    """
    px = _frame()
    asof = px.index[-1]
    config.SECTOR_NEUTRAL_ENABLED = True
    config.SECTOR_MOMENTUM_ENABLED = True
    neutral = sector.neutral_weights(px, asof)
    prev = config.SECTOR_TACTICAL_MAX_ADD_MULT
    try:
        # A generous multiplier lets the caps absorb the whole budget, so with the cascade
        # NOTHING should reach a non-eligible sector.
        config.SECTOR_TACTICAL_MAX_ADD_MULT = 1.0
        w = sector.momentum_overlay(px, asof)
        scores = sector.composite_scores(px, asof)
        top = set(scores.index[: config.SECTOR_ELIGIBLE_TOP_N])
        eligible = {t for t in scores.index if t in top and sector._above_10m(px[t], asof)}
        for t in neutral.index:
            if t not in eligible:
                core_share = float(neutral[t]) * config.SECTOR_CORE_PCT
                assert w[t] == pytest.approx(core_share, abs=1e-9), (
                    f"{t} failed eligibility but received tactical money")
    finally:
        config.SECTOR_TACTICAL_MAX_ADD_MULT = prev


def test_cascade_never_breaches_a_per_sector_cap():
    px = _frame()
    asof = px.index[-1]
    config.SECTOR_NEUTRAL_ENABLED = True
    config.SECTOR_MOMENTUM_ENABLED = True
    neutral = sector.neutral_weights(px, asof)
    for mult in (0.50, 0.75, 1.00):
        prev = config.SECTOR_TACTICAL_MAX_ADD_MULT
        try:
            config.SECTOR_TACTICAL_MAX_ADD_MULT = mult
            w = sector.momentum_overlay(px, asof)
            leftover_max = max(0.0, 1.0 - config.SECTOR_CORE_PCT)
            for t in neutral.index:
                cap = min(config.SECTOR_TACTICAL_MAX_ADD_PTS, mult * float(neutral[t]))
                add = w[t] - float(neutral[t]) * config.SECTOR_CORE_PCT
                assert add <= cap + float(neutral[t]) * leftover_max + 1e-9
            assert w.sum() == pytest.approx(1.0)
        finally:
            config.SECTOR_TACTICAL_MAX_ADD_MULT = prev
