"""
Unit tests for the Volatility Multiplier (SPEC.md §8).

Covers: the three vol buckets; the hard band-floor (never de-risk below the
regime floor); never exceeds the band high; monotonic (higher vol -> lower-or-
equal target); and no look-ahead in the realized-vol series.
"""

import numpy as np
import pandas as pd
import pytest

from strategies import config
from strategies.parts import volatility as vol


VERSION = "Balanced"
LO, HI = config.TARGET_VOL_BY_VERSION[VERSION]
CALM, MOD, ELEV = config.VOL_BUCKET_MULTIPLIERS


def test_bucket_thresholds():
    assert vol.volatility_multiplier(LO - 0.01, VERSION) == CALM
    assert vol.volatility_multiplier((LO + HI) / 2, VERSION) == MOD
    assert vol.volatility_multiplier(HI + 0.05, VERSION) == ELEV


def test_nan_vol_treated_as_calm():
    assert vol.volatility_multiplier(float("nan"), VERSION) == CALM


def test_calm_vol_reaches_band_high():
    band = (0.80, 1.00)
    assert vol.equity_target(band, LO - 0.01, VERSION) == pytest.approx(1.00)


def test_elevated_vol_floored_at_band_low():
    # RiskOn band: 0.70 * 1.00 = 0.70 < 0.80 floor -> clamped to 0.80.
    band = (0.80, 1.00)
    assert vol.equity_target(band, HI + 0.10, VERSION) == pytest.approx(0.80)


def test_never_exceeds_band_high():
    band = (0.35, 0.60)
    for v in (0.05, 0.11, 0.20):
        assert vol.equity_target(band, v, VERSION) <= 0.60 + 1e-9


def test_monotonic_non_increasing_in_vol():
    band = (0.10, 0.60)
    targets = [vol.equity_target(band, v, VERSION) for v in (0.05, 0.11, 0.20)]
    assert targets[0] >= targets[1] >= targets[2]


def test_realized_vol_no_lookahead():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2015-01-01", periods=300)
    spy = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)), index=idx)
    full = vol.realized_vol(spy)
    for t in (200, 250, 290):
        cutoff = idx[t]
        trunc = vol.realized_vol(spy.loc[:cutoff])
        assert trunc.loc[cutoff] == pytest.approx(full.loc[cutoff], nan_ok=True)
