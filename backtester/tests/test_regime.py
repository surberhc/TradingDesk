"""
Unit tests for the Regime Engine (SPEC.md §4).

Covers: score is bounded [0,100]; the trend/breadth components respond correctly
to synthetic up/down markets; regime classification at the band boundaries; the
four hysteresis behaviors (dead-zone, confirmation buffer, immediate de-risk, no
instant re-risk); and the single most important correctness property (SPEC §16)
— the score on date T uses only data on/before T (no look-ahead).
"""

import numpy as np
import pandas as pd
import pytest

from strategies import config
from strategies.parts import regime


# ---------------------------------------------------------------------------
# Synthetic price frame helpers
# ---------------------------------------------------------------------------
def _frame(spy: np.ndarray, n_days: int) -> pd.DataFrame:
    """Build a minimal prices frame (SPY, RSP, all sectors, IEF) from one path."""
    idx = pd.bdate_range("2012-01-02", periods=n_days)
    data = {"SPY": spy, "RSP": spy.copy(), "IEF": np.linspace(90, 100, n_days)}
    for s in config.SECTORS:
        data[s] = spy.copy()
    return pd.DataFrame(data, index=idx)


def _rising(n=600, start=100.0, step=0.2):
    return start + np.arange(n) * step


def _falling(n=600, start=300.0, step=0.2):
    return start - np.arange(n) * step


# ---------------------------------------------------------------------------
# Score bounds and component direction
# ---------------------------------------------------------------------------
def test_score_within_bounds():
    df = _frame(_rising(), 600)
    out = regime.market_health_score(df)
    score = out["score"].dropna()
    assert len(score) > 0
    assert score.min() >= 0.0 and score.max() <= 100.0


def test_strong_uptrend_scores_high_trend_component():
    df = _frame(_rising(), 600)
    out = regime.market_health_score(df).dropna(subset=["trend"])
    # A clean monotonic rise: above both MAs, positive 6m return, positive slope.
    assert out["trend"].iloc[-1] == pytest.approx(1.0)


def test_downtrend_scores_low_trend_component():
    df = _frame(_falling(), 600)
    out = regime.market_health_score(df).dropna(subset=["trend"])
    assert out["trend"].iloc[-1] == pytest.approx(0.0)


def test_breadth_inception_aware_excludes_absent_sectors():
    # One sector is NaN for the whole window — it must not count as "below".
    df = _frame(_rising(), 600)
    df["XLC"] = np.nan
    out = regime.market_health_score(df).dropna(subset=["breadth_pct"])
    # All *trading* sectors are rising -> full breadth despite the absent one.
    assert out["breadth_pct"].iloc[-1] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "score,expected",
    [
        (100, "RiskOn"), (75, "RiskOn"),
        (74, "RiskOnNarrowing"), (55, "RiskOnNarrowing"),
        (54, "Caution"), (40, "Caution"),
        (39, "Defensive"), (25, "Defensive"),
        (24, "CapitalPreservation"), (0, "CapitalPreservation"),
    ],
)
def test_classify_regime_boundaries(score, expected):
    assert regime.classify_regime(score) == expected


def test_classify_regime_handles_nan():
    assert regime.classify_regime(float("nan")) == "Undefined"


# ---------------------------------------------------------------------------
# Hysteresis (SPEC §4)
# ---------------------------------------------------------------------------
def test_hysteresis_deadzone_ignores_small_wiggle():
    # Oscillate by < REGIME_MIN_THRESHOLD_CROSS points across the 75 boundary.
    scores = pd.Series([80, 80, 74, 80, 73, 80, 74], dtype=float)
    out = regime.apply_hysteresis(scores)
    assert (out == "RiskOn").all(), "sub-3-point wiggle must not flip the regime"


def test_hysteresis_confirms_after_buffer():
    # Decisive drop (>=3 below 75) held long enough to confirm Narrowing.
    n = config.REGIME_CONFIRMATION_DAYS
    scores = pd.Series([80] + [60] * (n + 2), dtype=float)
    out = regime.apply_hysteresis(scores)
    assert out.iloc[0] == "RiskOn"
    # Not confirmed until the buffer elapses, then it switches.
    assert out.iloc[n] == "RiskOnNarrowing"
    assert out.iloc[-1] == "RiskOnNarrowing"


def test_hysteresis_immediate_derisk_on_big_drop():
    # A >10-point collapse de-risks immediately, no waiting for the buffer.
    scores = pd.Series([85, 85, 30], dtype=float)
    out = regime.apply_hysteresis(scores)
    assert out.iloc[-1] == "Defensive"


def test_hysteresis_no_instant_rerisk():
    # An upgrade must serve the confirmation buffer (no instant jump up).
    scores = pd.Series([30, 90], dtype=float)
    out = regime.apply_hysteresis(scores)
    assert out.iloc[-1] == "Defensive", "single-day upgrade must not jump immediately"


# ---------------------------------------------------------------------------
# No look-ahead — the single most important correctness rule (SPEC §3, §16)
# ---------------------------------------------------------------------------
def test_no_lookahead_truncation_matches():
    rng = np.random.default_rng(0)
    # Random-walk price so signals genuinely vary over time.
    path = 100 + np.cumsum(rng.normal(0.05, 1.0, 600))
    df = _frame(path, 600)
    full = regime.market_health_score(df)

    # Recompute on history truncated at several dates; the score at T must match.
    for t in (400, 480, 560):
        cutoff = df.index[t]
        truncated = regime.market_health_score(df.loc[:cutoff])
        assert truncated.loc[cutoff, "score"] == pytest.approx(
            full.loc[cutoff, "score"], nan_ok=True
        ), f"score at {cutoff.date()} changed when future data was removed"


# ---------------------------------------------------------------------------
# Real VIX upgrade path (stress component, §4 / DATA.md)
# ---------------------------------------------------------------------------
def test_stress_uses_real_vix_when_provided():
    df = _frame(_rising(), 600)
    falling_vix = pd.Series(np.linspace(40, 15, 600), index=df.index)  # calming
    rising_vix = pd.Series(np.linspace(15, 40, 600), index=df.index)   # stressing
    calm = regime.market_health_score(df, vix=falling_vix).dropna(subset=["stress_vol_calm"])
    stress = regime.market_health_score(df, vix=rising_vix).dropna(subset=["stress_vol_calm"])
    assert calm["stress_vol_calm"].iloc[-1] == pytest.approx(1.0)    # below its trend
    assert stress["stress_vol_calm"].iloc[-1] == pytest.approx(0.0)  # above its trend


# ---------------------------------------------------------------------------
# 2026-07-09 NaN-as-bearish regression: a missing today's print must read as
# unknown (NaN), never as a false "broke trend" / "not calm" bearish 0.0. Bare
# pandas `>` reads NaN>NaN as False, not NaN — this was the root-cause bug.
# ---------------------------------------------------------------------------
def test_missing_last_price_reads_as_unknown_not_bearish():
    df = _frame(_rising(), 600)
    df.loc[df.index[-1], "SPY"] = np.nan  # today's SPY print hasn't landed yet
    out = regime.market_health_score(df)
    last = out.iloc[-1]
    # Sub-signals that previously bare-`>`-compared against a NaN price/MA/return
    # must be NaN (unknown), not 0.0 (a false bearish read).
    assert pd.isna(last["trend_above_10m"])
    assert pd.isna(last["trend_ret_6m_pos"])
    assert pd.isna(last["trend_slope_pos"])  # _rolling_slope_positive cast, same bug class
    # The rolled-up trend component (100% dependent on SPY) must also be NaN, not
    # a deflated score from silently treating the missing print as bearish.
    assert pd.isna(last["trend"])
    # NOTE: the overall `score` is NOT asserted NaN here on purpose. score =
    # mean(trend, breadth, stress) with skipna=True (by design — see
    # _breadth_component's own inception-aware skipna averaging), and in this
    # synthetic frame breadth_pct/stress are independent of SPY's last-day value,
    # so they legitimately stay defined. That's a pre-existing, deliberate
    # partial-information averaging choice, not part of this NaN-as-bearish bug.


def test_missing_last_price_makes_rsp_leads_unknown():
    # breadth_rsp_leads = membership(RSP/SPY, ...) needs SPY specifically — a
    # missing SPY print must NaN this sub-signal out, not silently read as
    # "RSP not leading" (bearish-for-breadth false read). breadth_pct (sector-vs-
    # own-MA) is independent of SPY and legitimately stays defined.
    df = _frame(_rising(), 600)
    df.loc[df.index[-1], "SPY"] = np.nan
    sector_cols = [s for s in config.SECTORS if s in df.columns]
    b = regime._breadth_component(df["RSP"], df["SPY"], df[sector_cols])
    last = b.iloc[-1]
    assert pd.isna(last["breadth_rsp_leads"])
    assert not pd.isna(last["breadth_pct"])  # unaffected by SPY, stays defined
