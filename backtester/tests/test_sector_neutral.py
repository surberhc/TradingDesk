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

# Captured at IMPORT, before the autouse fixture below forces the flag off for the
# behavioural tests. This is the value production actually ships with.
_SHIPPED_NEUTRAL = config.SECTOR_NEUTRAL_ENABLED
_SHIPPED_MOMENTUM = config.SECTOR_MOMENTUM_ENABLED


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


def test_flag_is_armed():
    """ARMED 2026-08-24 by Andrew (Phase 1 adopted; Phase 2 momentum refuted and left off).

    This assertion is deliberately strict: the sleeve's composition is a live production
    decision, so an accidental flip in either direction should break the build rather than
    silently change what every full-model account holds.
    """
    assert _SHIPPED_NEUTRAL is True
    assert _SHIPPED_MOMENTUM is False, (
        "the momentum overlay was REFUTED in Phase 2 (worse than random) - it must stay off")


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
    first = min(config.SECTOR_NEUTRAL_OBSERVED)
    w = sector.neutral_weights(px, pd.Timestamp(first))
    for t, expected in config.SECTOR_NEUTRAL_OBSERVED[first].items():
        assert w[t] == pytest.approx(expected, abs=5e-3)


def test_weights_snap_to_an_observation_on_its_date():
    """On an observation date the neutral SNAPS to the observed vector, discarding drift.

    This is the memo section 3 re-anchor. It is what lets the LIVE book track Andrew's actual
    table instead of eight years of accumulated drift error.
    """
    px = _frame()
    config.SECTOR_NEUTRAL_ENABLED = True
    obs_date = max(config.SECTOR_NEUTRAL_OBSERVED)
    observed = config.SECTOR_NEUTRAL_OBSERVED[obs_date]
    w = sector.neutral_weights(px, pd.Timestamp(obs_date))
    for t, expected in observed.items():
        assert w[t] == pytest.approx(expected, abs=1e-4), f"{t} did not snap to the observation"


def test_between_observations_an_outperformer_gains_weight():
    """Between anchors the weights DRIFT on realized returns, so a winner grows.

    Checked strictly between the two observation dates, because an observation deliberately
    overrides drift and would mask it.
    """
    px = _frame()
    config.SECTOR_NEUTRAL_ENABLED = True
    dates = sorted(pd.Timestamp(d) for d in config.SECTOR_NEUTRAL_OBSERVED)
    lo, hi = dates[0], dates[-1]
    inner = [d for d in px.index if lo < d < hi]
    if len(inner) < 40:
        pytest.skip("synthetic frame does not span the gap between observations")
    early, late = inner[10], inner[-10]
    window = px.loc[early:late]
    best = max(config.SECTORS, key=lambda t: window[t].iloc[-1] / window[t].iloc[0])
    w_early = sector.neutral_weights(px, early)
    w_late = sector.neutral_weights(px, late)
    assert w_late[best] > w_early[best]


def test_a_later_observation_cannot_change_an_earlier_weight():
    """The anti-look-ahead property of the observed-anchor design.

    Adding a NEW observation dated after T must leave every weight at or before T untouched.
    An earlier design de-drifted BACKWARD from a single 2026 anchor and failed
    tests/test_no_lookahead.py for exactly this reason.
    """
    px = _frame()
    config.SECTOR_NEUTRAL_ENABLED = True
    dates = sorted(pd.Timestamp(d) for d in config.SECTOR_NEUTRAL_OBSERVED)
    asof = [d for d in px.index if d < dates[-1]][-5]
    before = sector.neutral_weights(px, asof)

    prev = dict(config.SECTOR_NEUTRAL_OBSERVED)
    try:
        future = {t: 1.0 / len(config.SECTORS) for t in config.SECTORS}
        config.SECTOR_NEUTRAL_OBSERVED = {**prev, "2026-12-31": future}
        sector._NEUTRAL_CACHE.clear()
        after = sector.neutral_weights(px, asof)
        assert (before - after).abs().max() == pytest.approx(0.0, abs=1e-12)
    finally:
        config.SECTOR_NEUTRAL_OBSERVED = prev
        sector._NEUTRAL_CACHE.clear()


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
