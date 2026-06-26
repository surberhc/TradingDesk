"""
Unit tests for the Defensive Engine (SPEC.md §7).

Covers: weights sum to 100; a clearly superior asset outranks a weak one;
T-bills are always eligible; scores stay in [0,100]; inception-aware exclusion;
and the no-look-ahead property.
"""

import numpy as np
import pandas as pd
import pytest

from strategies import config
from strategies.parts import defensive


def _idx(n):
    return pd.bdate_range("2012-01-02", periods=n)


def _frame(n, **series):
    idx = _idx(n)
    return pd.DataFrame({k: v for k, v in series.items()}, index=idx)


def test_weights_sum_to_100():
    assert sum(config.DEFENSIVE_SCORE_WEIGHTS.values()) == 100


def test_superior_asset_outranks_weak_one():
    n = 400
    rng = np.random.default_rng(0)
    strong = 100 + np.arange(n) * 0.05                       # steady, low-vol uptrend
    weak = 100 - np.arange(n) * 0.05 + rng.normal(0, 2, n)   # falling, high-vol
    bil = 100 + np.arange(n) * 0.002                          # flat cash
    df = _frame(n, STRONG=strong, WEAK=weak, BIL=bil)
    ranked = defensive.rank_defensives(df, df.index[-1], candidates=["STRONG", "WEAK"], tbill="BIL")
    assert ranked.index[0] == "STRONG"
    assert ranked["STRONG"] > ranked["WEAK"]


def test_tbill_always_eligible():
    n = 400
    df = _frame(n, SHY=100 + np.arange(n) * 0.01, BIL=100 + np.arange(n) * 0.002)
    ranked = defensive.rank_defensives(df, df.index[-1], candidates=["SHY"], tbill="BIL")
    assert "BIL" in ranked.index  # added even though not in the candidate list


def test_scores_within_bounds():
    n = 400
    rng = np.random.default_rng(1)
    df = _frame(
        n,
        A=100 + np.cumsum(rng.normal(0, 1, n)),
        B=100 + np.cumsum(rng.normal(0, 1, n)),
        BIL=100 + np.arange(n) * 0.002,
    )
    scores = defensive.defensive_scores(df, candidates=["A", "B"], tbill="BIL")
    valid = scores.dropna(how="all")
    assert valid.min().min() >= 0.0
    assert valid.max().max() <= 100.0


def test_inception_aware_excludes_absent():
    n = 400
    late = np.full(n, np.nan)
    late[300:] = 100 + np.arange(n - 300) * 0.02  # only trades from day 300
    df = _frame(n, EARLY=100 + np.arange(n) * 0.01, LATE=late, BIL=100 + np.arange(n) * 0.002)
    scores = defensive.defensive_scores(df, candidates=["EARLY", "LATE"], tbill="BIL")
    # Before inception LATE has no score; after, it does.
    assert np.isnan(scores["LATE"].iloc[250])
    assert not np.isnan(scores["LATE"].iloc[-1])
    # A ranking mid-history simply omits the not-yet-trading candidate.
    ranked = defensive.rank_defensives(df, df.index[250], candidates=["EARLY", "LATE"], tbill="BIL")
    assert "LATE" not in ranked.index
    assert "EARLY" in ranked.index


def test_no_lookahead_truncation_matches():
    n = 400
    rng = np.random.default_rng(2)
    df = _frame(
        n,
        A=100 + np.cumsum(rng.normal(0, 1, n)),
        B=100 + np.cumsum(rng.normal(0, 1, n)),
        BIL=100 + np.arange(n) * 0.002,
    )
    full = defensive.defensive_scores(df, candidates=["A", "B"], tbill="BIL")
    for t in (300, 350, 390):
        cutoff = df.index[t]
        trunc = defensive.defensive_scores(df.loc[:cutoff], candidates=["A", "B"], tbill="BIL")
        for col in ("A", "B", "BIL"):
            assert trunc.loc[cutoff, col] == pytest.approx(full.loc[cutoff, col], nan_ok=True)
