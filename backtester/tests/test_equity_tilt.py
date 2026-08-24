"""
Tests for the STUDY-ONLY equity-sleeve broadening tilt (PREREG 2026-07-20 §6).

Two pre-registered guarantees:
  (a) CAUSALITY — the tilt selection at date T uses only data on/before T (no
      look-ahead); truncating the frame at T must not change the selection.
  (b) SKILL-vs-BETA ATTRIBUTION — a higher-beta-but-NO-skill series is correctly
      reported as "just beta": alpha ~ 0 from beta_attrib.capm_attribution.

Plus a guard that the study flag defaults OFF and is byte-neutral to production
(the equity sleeve equals sector.select_sectors), and that the tilt, when on,
actually broadens into size funds / sectors and still sums to 1.
"""

import numpy as np
import pandas as pd
import pytest

from strategies import config

import pytest as _pytest_for_pin
from strategies import config as _cfg_pin
from strategies.parts import sector as _sector_pin


@_pytest_for_pin.fixture(autouse=True)
def _legacy_broad_beta_path():
    """This module covers the ORIGINAL broad-beta + equity-tilt path.

    That path is still live and still correct, but it only runs when the 11-sector neutral is
    OFF - and the neutral was ARMED in production on 2026-08-24. Pin the flag off so this
    module keeps testing what it was written to test.
    """
    prev = _cfg_pin.SECTOR_NEUTRAL_ENABLED
    _cfg_pin.SECTOR_NEUTRAL_ENABLED = False
    _sector_pin._NEUTRAL_CACHE.clear()
    yield
    _cfg_pin.SECTOR_NEUTRAL_ENABLED = prev
    _sector_pin._NEUTRAL_CACHE.clear()

from strategies.parts import equity_tilt, sector

from src import beta_attrib


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _frame(n=400):
    """Broad beta (SPY/VTI/RSP) + all sectors + the size funds.

    IJH is the clear leader (steeper than SPY); the rest lag SPY slightly so only
    the genuine leaders clear the positive-RS gate.
    """
    idx = pd.bdate_range("2012-01-02", periods=n)
    data = {t: 100 + np.arange(n) * 0.10 for t in ("SPY", "VTI", "RSP")}
    for s in config.SECTORS:
        data[s] = 100 + np.arange(n) * (0.20 if s == "XLK" else 0.08)
    # Size funds: IJH a strong leader, IJR a mild laggard.
    data["IJH"] = 100 + np.arange(n) * 0.22
    data["IJR"] = 100 + np.arange(n) * 0.085
    data["VO"] = 100 + np.arange(n) * 0.21
    data["VB"] = 100 + np.arange(n) * 0.085
    return pd.DataFrame(data, index=idx)


@pytest.fixture
def tilt_on(monkeypatch):
    """Turn the study tilt ON for a test, then auto-restore (frozen config safe)."""
    monkeypatch.setattr(config, "EQUITY_TILT_ENABLED", True)
    monkeypatch.setattr(config, "EQUITY_TILT_PCT", 0.20)
    monkeypatch.setattr(config, "EQUITY_TILT_COUNT", 4)
    monkeypatch.setattr(config, "EQUITY_TILT_USE_ALT_SIZE", False)
    yield


# --------------------------------------------------------------------------- #
# Default-OFF byte-neutrality
# --------------------------------------------------------------------------- #
def test_default_off_matches_production_sector_engine():
    """With the study flag OFF (default), the sleeve is identical to production."""
    df = _frame()
    asof = df.index[-1]
    assert config.EQUITY_TILT_ENABLED is False  # ships OFF
    got = equity_tilt.select_equity_sleeve(df, asof)
    want = sector.select_sectors(df, asof, config.SECTOR_TILT_PCT)
    pd.testing.assert_series_equal(got.sort_index(), want.sort_index())
    # And that production default is broad-beta only (no size funds, no sectors).
    assert not any(t in got.index for t in config.SECTORS)
    assert not any(t in got.index for t in ("IJH", "IJR", "VO", "VB"))


# --------------------------------------------------------------------------- #
# (a) Causality — no look-ahead in the tilt selection
# --------------------------------------------------------------------------- #
def test_tilt_selection_is_causal(tilt_on):
    """Selection on the full frame == selection on the frame truncated at asof.

    If any future bar leaked into the score/gate, truncation would change the
    result. It must not."""
    df = _frame()
    asof = df.index[-1]
    full = equity_tilt.select_equity_sleeve(df, asof)
    trunc = equity_tilt.select_equity_sleeve(df.loc[:asof], asof)
    pd.testing.assert_series_equal(full.sort_index(), trunc.sort_index())

    # A stronger causal probe: appending FUTURE bars after asof cannot move the
    # date-asof selection (the extra rows are all strictly after asof).
    future = df.copy()
    extra_idx = pd.bdate_range(df.index[-1] + pd.Timedelta(days=1), periods=30)
    future = pd.concat([future, pd.DataFrame(
        {c: np.linspace(future[c].iloc[-1], future[c].iloc[-1] * 2, 30) for c in future.columns},
        index=extra_idx)])
    with_future = equity_tilt.select_equity_sleeve(future, asof)
    pd.testing.assert_series_equal(full.sort_index(), with_future.sort_index())


def test_tilt_broadens_and_sums_to_one(tilt_on):
    """When on, the tilt actually carves into leaders and the sleeve still sums to 1."""
    df = _frame()
    w = equity_tilt.select_equity_sleeve(df, df.index[-1])
    assert w.sum() == pytest.approx(1.0)
    held = [t for t in equity_tilt.tilt_candidates() if t in w.index]
    assert "IJH" in held               # the clear size leader is picked up
    assert len(held) <= config.EQUITY_TILT_COUNT
    for t in held:                     # per-candidate cap respected
        assert w[t] <= config.SECTOR_MAX_WEIGHT + 1e-9
    # Broad beta still holds the majority (1 - filled) of the sleeve.
    assert w.get("SPY", 0.0) > 0.0


def test_no_eligible_reverts_to_broad_beta(tilt_on):
    """If nothing beats SPY / clears the gate, the sleeve is pure broad beta."""
    df = _frame()
    # Make every candidate a laggard below its own 200d trend.
    for t in list(config.SECTORS) + ["IJH", "IJR", "VO", "VB"]:
        df[t] = 300 - np.arange(len(df)) * 0.20
    w = equity_tilt.select_equity_sleeve(df, df.index[-1])
    assert not any(t in w.index for t in equity_tilt.tilt_candidates())
    assert w.sum() == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# (b) Skill-vs-beta attribution — "just beta" reads as alpha ~ 0
# --------------------------------------------------------------------------- #
def test_higher_beta_no_skill_reports_zero_alpha():
    """A series that is SPY scaled to higher beta with NO skill must attribute to
    beta, not alpha: alpha_annual ~ 0, beta ~ the scaling factor.

    This is the property the study's killer control relies on — it is what stops a
    'more equity beta' result from masquerading as selection skill."""
    rng = np.random.default_rng(20260720)
    n = 2000
    idx = pd.bdate_range("2015-01-02", periods=n)
    spy_ret = pd.Series(rng.normal(0.0004, 0.01, n), index=idx)

    # No-skill, higher-beta series: 1.5x SPY plus zero-mean idiosyncratic noise
    # (noise has NO drift -> no alpha). This is 'just more beta', by construction.
    scale = 1.5
    noise = pd.Series(rng.normal(0.0, 0.003, n), index=idx)
    strat_ret = scale * spy_ret + noise

    attr = beta_attrib.capm_attribution(strat_ret, spy_ret)
    assert attr.beta == pytest.approx(scale, abs=0.05)   # recovers the beta loading
    assert attr.alpha_annual == pytest.approx(0.0, abs=0.02)  # no skill -> ~0 alpha
    assert attr.r2 > 0.9                                  # SPY explains almost all of it


def test_genuine_alpha_is_detected():
    """Sanity counterpart: a series with a real positive drift beyond beta shows
    positive alpha — the attribution isn't rigged to always say 'just beta'."""
    rng = np.random.default_rng(20260720)
    n = 2000
    idx = pd.bdate_range("2015-01-02", periods=n)
    spy_ret = pd.Series(rng.normal(0.0004, 0.01, n), index=idx)
    daily_alpha = 0.0003                                  # ~7.8%/yr of true skill
    strat_ret = 1.0 * spy_ret + daily_alpha + pd.Series(rng.normal(0.0, 0.002, n), index=idx)

    attr = beta_attrib.capm_attribution(strat_ret, spy_ret)
    assert attr.alpha_annual == pytest.approx(daily_alpha * 252, abs=0.02)
    assert attr.alpha_annual > 0.03                       # clearly non-zero skill
