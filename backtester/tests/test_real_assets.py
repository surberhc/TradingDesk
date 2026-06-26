"""
Unit tests for the diversified real-asset basket (SPEC.md §1 revised, §6, §12).

Covers: a confirmed gold uptrend produces a gold leg; gold + commodities both
trending yields a two-leg inverse-vol basket; nothing trending -> None; the trend
gate rejects a downtrend; inception-aware exclusion; and no look-ahead.
"""

import numpy as np
import pandas as pd
import pytest

from strategies import config
from strategies.parts import real_assets as ra


def _frame(n=400, **series):
    idx = pd.bdate_range("2012-01-02", periods=n)
    # All basket candidates default to flat (fail momentum) unless overridden.
    base = {t: 100 + np.zeros(n) for tickers in config.REAL_ASSET_BASKET.values() for t in tickers}
    base.update(series)
    return pd.DataFrame(base, index=idx)


def test_gold_uptrend_makes_a_gold_leg():
    n = 400
    df = _frame(n, IAU=100 + np.arange(n) * 0.10)  # gold trending; commodities flat
    basket = ra.select_real_basket(df, df.index[-1])
    assert basket is not None
    cats = {leg["category"] for leg in basket["legs"]}
    assert "gold" in cats
    assert basket["legs"][0]["weight"] == pytest.approx(1.0)  # only leg -> full weight


def test_two_legs_inverse_vol_weighted():
    n = 400
    rng = np.random.default_rng(0)
    # Gold: smooth uptrend (low vol). Commodities: uptrend + noise (higher vol).
    gold = 100 + np.arange(n) * 0.08
    com = 100 + np.arange(n) * 0.10 + np.cumsum(rng.normal(0, 0.6, n))
    df = _frame(n, IAU=gold, PDBC=com)
    basket = ra.select_real_basket(df, df.index[-1])
    legs = {leg["category"]: leg for leg in basket["legs"]}
    assert set(legs) == {"gold", "commodities"}
    assert sum(leg["weight"] for leg in basket["legs"]) == pytest.approx(1.0)
    # Lower-vol gold should carry the larger inverse-vol weight.
    assert legs["gold"]["weight"] > legs["commodities"]["weight"]


def test_empty_when_nothing_trends():
    assert ra.select_real_basket(_frame(400), pd.Timestamp("2013-06-03")) is None


def test_trend_gate_rejects_downtrend():
    n = 400
    # Recent pop but below the 200d MA -> fails the gate, no leg.
    path = np.concatenate([np.linspace(300, 100, n - 20), np.linspace(100, 130, 20)])
    df = _frame(n, IAU=path)
    assert ra.select_real_basket(df, df.index[-1]) is None


def test_inception_aware_excludes_absent():
    n = 400
    late = np.full(n, np.nan)
    late[395:] = 100 + np.arange(n - 395) * 0.10  # too little history for a 200d MA
    df = _frame(n, GLDM=late)
    assert ra.select_real_basket(df, df.index[-1]) is None


def test_no_lookahead_stable_under_truncation():
    n = 400
    df = _frame(n, IAU=100 + np.arange(n) * 0.08, PDBC=100 + np.arange(n) * 0.12)
    asof = df.index[-1]
    full = ra.select_real_basket(df, asof)
    trunc = ra.select_real_basket(df.loc[:asof], asof)
    assert [l["ticker"] for l in full["legs"]] == [l["ticker"] for l in trunc["legs"]]
    assert full["legs"][0]["weight"] == pytest.approx(trunc["legs"][0]["weight"])
