"""
Unit tests for the STUDY-ONLY sector NEUTRAL core.

Prereg: docs/PREREG_S0_sector_momentum_core_2026-08-24.md (Phase 1, "neutral-only" arm).

Covers: the flag is OFF by default and off-parity is exact; the neutral holds ALL 11
sectors with no zero weights (the whole point of the arm); weights sum to 1; the anchor
is reproduced at the anchor date; reconstruction de-drifts in the correct direction; and
post-anchor drift is strictly causal (future bars cannot change a past decision).
"""

import numpy as np
import pandas as pd
import pytest

from strategies import config
from strategies.parts import sector


@pytest.fixture(autouse=True)
def _clean():
    """Every test starts from production defaults and a cold cache."""
    prev = config.SECTOR_NEUTRAL_ENABLED
    config.SECTOR_NEUTRAL_ENABLED = False
    sector._NEUTRAL_CACHE.clear()
    yield
    config.SECTOR_NEUTRAL_ENABLED = prev
    sector._NEUTRAL_CACHE.clear()


def _frame(n=900, end="2026-08-21"):
    """Broad beta plus all 11 sectors, each on its own distinct upward drift."""
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    data = {"SPY": 100 + np.arange(n) * 0.10,
            "RSP": 100 + np.arange(n) * 0.08,
            "VTI": 100 + np.arange(n) * 0.10}
    for i, s in enumerate(config.SECTORS):
        data[s] = 100 * (1.0 + 0.0002 * (i + 1)) ** np.arange(n)
    return pd.DataFrame(data, index=idx)


def test_flag_defaults_off():
    assert config.SECTOR_NEUTRAL_ENABLED is False


def test_off_parity_is_exact():
    """With the flag off the sleeve must be byte-identical to broad beta."""
    px = _frame()
    asof = px.index[-1]
    off = sector.select_sectors(px, asof)
    assert set(off.index) <= set(config.EQUITY_CORE)
    assert not set(off.index) & set(config.SECTORS)


def test_neutral_holds_all_eleven_with_no_zero_weights():
    """The arm exists to hold every sector — a zero weight would defeat its purpose."""
    px = _frame()
    config.SECTOR_NEUTRAL_ENABLED = True
    w = sector.neutral_weights(px, px.index[-1])
    assert len(w) == len(config.SECTORS) == 11
    assert set(w.index) == set(config.SECTORS)
    assert (w > 0).all(), f"zero-weighted sector(s): {list(w[w <= 0].index)}"
    assert w.sum() == pytest.approx(1.0)


def test_select_sectors_returns_the_neutral_when_enabled():
    px = _frame()
    config.SECTOR_NEUTRAL_ENABLED = True
    w = sector.select_sectors(px, px.index[-1])
    assert set(w.index) == set(config.SECTORS)
    assert w.sum() == pytest.approx(1.0)


def test_anchor_is_reproduced_at_the_anchor_date():
    px = _frame()
    config.SECTOR_NEUTRAL_ENABLED = True
    w = sector.neutral_weights(px, pd.Timestamp(config.SECTOR_NEUTRAL_ANCHOR_DATE))
    for t, expected in config.SECTOR_NEUTRAL_ANCHOR.items():
        assert w[t] == pytest.approx(expected, abs=5e-3)


def test_reconstruction_underweights_the_outperformer_in_the_past():
    """De-drifting must assign a LOWER past weight to a sector that then outperformed.

    This is the direction that makes the backtest CONSERVATIVE rather than flattering,
    and it is the property the prereg leans on when accepting the reconstruction.
    """
    px = _frame()
    config.SECTOR_NEUTRAL_ENABLED = True
    best = max(config.SECTORS, key=lambda s: px[s].iloc[-1] / px[s].iloc[0])
    early = sector.neutral_weights(px, px.index[60])
    late = sector.neutral_weights(px, px.index[-1])
    assert early[best] < late[best]


def test_post_anchor_drift_is_causal():
    """For dates AFTER the anchor, appending future bars must not change the answer."""
    px = _frame(end="2027-06-30")          # extends beyond the anchor date
    asof = pd.Timestamp("2026-12-31")
    config.SECTOR_NEUTRAL_ENABLED = True
    sector._NEUTRAL_CACHE.clear()
    full = sector.neutral_weights(px, asof)
    sector._NEUTRAL_CACHE.clear()
    trunc = sector.neutral_weights(px.loc[:asof], asof)
    sector._NEUTRAL_CACHE.clear()
    assert (full - trunc).abs().max() == pytest.approx(0.0, abs=1e-9)
