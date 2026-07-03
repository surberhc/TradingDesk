r"""Tests for s4_fwd_vol_experiment -- causality (no look-ahead) and arm correctness.

The load-bearing tests are the no-look-ahead ones: the implied de-bias, the Arm B blend,
and the Arm C re-risk logic must all be computable from data ON/BEFORE date T only. We
prove this by corrupting all data STRICTLY AFTER a cutoff and asserting the signal up to
the cutoff is byte-identical. ASCII only.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import s4_fwd_vol_experiment as fx
from strategies.spx_vol_control import realized_vol_simple, exposure_from_vol


# --------------------------------------------------------------------------- #
# Synthetic fixtures
# --------------------------------------------------------------------------- #
def _synthetic_rets(n=400, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2019-01-01", periods=n)
    r_spx = pd.Series(rng.normal(0.0004, 0.011, n), index=idx)
    r_cash = pd.Series(0.00008, index=idx)
    return pd.DataFrame({"r_spx": r_spx, "r_cash": r_cash})


def _synthetic_implied(rets, level=0.16, seed=1):
    rng = np.random.default_rng(seed)
    # implied roughly tracks realized but with noise + a premium
    rv = realized_vol_simple(rets["r_spx"], 20, 60).bfill().fillna(level)
    imp = rv * 1.3 + pd.Series(rng.normal(0, 0.01, len(rets)), index=rets.index)
    return imp.clip(lower=0.03)


# --------------------------------------------------------------------------- #
# Causality: de-bias uses only trailing data
# --------------------------------------------------------------------------- #
def test_debias_no_lookahead():
    rets = _synthetic_rets()
    realized = realized_vol_simple(rets["r_spx"], 20, 60)
    implied = _synthetic_implied(rets)
    cutoff = rets.index[300]

    full = fx._debias_causal(implied, realized)

    # Corrupt EVERYTHING strictly after the cutoff, recompute, compare up to cutoff.
    imp2 = implied.copy()
    real2 = realized.copy()
    imp2.loc[imp2.index > cutoff] = 99.0
    real2.loc[real2.index > cutoff] = 99.0
    partial = fx._debias_causal(imp2, real2)

    a = full.loc[:cutoff].dropna()
    b = partial.loc[:cutoff].dropna()
    common = a.index.intersection(b.index)
    assert len(common) > 100
    pd.testing.assert_series_equal(a.loc[common], b.loc[common], check_names=False)


def test_armB_blend_no_lookahead():
    rets = _synthetic_rets()
    implied = _synthetic_implied(rets)
    imp_db = fx._debias_causal(implied, realized_vol_simple(rets["r_spx"], 20, 60))
    cutoff = rets.index[300]

    full = fx.est_armB(rets, imp_db)

    rets2 = rets.copy()
    rets2.loc[rets2.index > cutoff, "r_spx"] = 5.0
    imp_db2 = imp_db.copy()
    imp_db2.loc[imp_db2.index > cutoff] = 5.0
    partial = fx.est_armB(rets2, imp_db2)

    a = full.loc[:cutoff].dropna()
    b = partial.loc[:cutoff].dropna()
    common = a.index.intersection(b.index)
    assert len(common) > 100
    pd.testing.assert_series_equal(a.loc[common], b.loc[common], check_names=False)


def test_armC_rerisk_no_lookahead():
    rets = _synthetic_rets()
    implied = _synthetic_implied(rets)
    imp_db = fx._debias_causal(implied, realized_vol_simple(rets["r_spx"], 20, 60))
    cutoff = rets.index[300]

    full = fx._exposure_armC(rets, imp_db)

    rets2 = rets.copy()
    rets2.loc[rets2.index > cutoff, "r_spx"] = -3.0
    imp_db2 = imp_db.copy()
    imp_db2.loc[imp_db2.index > cutoff] = -3.0
    partial = fx._exposure_armC(rets2, imp_db2)

    a = full.loc[:cutoff].dropna()
    b = partial.loc[:cutoff].dropna()
    common = a.index.intersection(b.index)
    assert len(common) > 100
    pd.testing.assert_series_equal(a.loc[common], b.loc[common], check_names=False)


# --------------------------------------------------------------------------- #
# Arm C logic: de-risk uses realized, re-risk uses implied
# --------------------------------------------------------------------------- #
def test_armC_derisk_trusts_realized():
    """When the realized-based exposure is FALLING, Arm C must equal the realized
    candidate (implied cannot talk it into more risk while vol is rising)."""
    rets = _synthetic_rets(seed=7)
    # implied that is absurdly LOW (would demand high exposure) -- must be IGNORED on
    # de-risk days.
    imp_db = pd.Series(0.02, index=rets.index)  # -> exposure would want to be capped high
    realized = realized_vol_simple(rets["r_spx"], fx.FAST, fx.SLOW)
    e_real = exposure_from_vol(realized, fx.TARGET_VOL, fx.LEVERAGE_CAP).reindex(rets.index)
    eC = fx._exposure_armC(rets, imp_db)

    # On every day the running exposure falls vs the prior day, eC must match e_real.
    prev = np.nan
    checked = 0
    for t in rets.index:
        er = e_real.get(t, np.nan)
        c = eC.get(t, np.nan)
        if not np.isnan(prev) and not np.isnan(er) and er < prev:
            assert abs(c - er) < 1e-9, f"de-risk day {t} used implied not realized"
            checked += 1
        prev = c if not np.isnan(c) else prev
    assert checked > 5


def test_armC_rerisk_uses_implied():
    """When exposure is RISING and implied is warm, Arm C uses the implied candidate."""
    rets = _synthetic_rets(seed=11)
    imp_db = pd.Series(0.05, index=rets.index)  # low implied -> high implied exposure
    realized = realized_vol_simple(rets["r_spx"], fx.FAST, fx.SLOW)
    e_imp = exposure_from_vol(imp_db, fx.TARGET_VOL, fx.LEVERAGE_CAP).reindex(rets.index)
    e_real = exposure_from_vol(realized, fx.TARGET_VOL, fx.LEVERAGE_CAP).reindex(rets.index)
    eC = fx._exposure_armC(rets, imp_db)

    prev = np.nan
    checked = 0
    for t in rets.index:
        er = e_real.get(t, np.nan)
        ei = e_imp.get(t, np.nan)
        c = eC.get(t, np.nan)
        if not np.isnan(prev) and not np.isnan(er) and er >= prev and not np.isnan(ei):
            expected = min(ei, fx.LEVERAGE_CAP)
            assert abs(c - expected) < 1e-9, f"re-risk day {t} did not use implied"
            checked += 1
        prev = c if not np.isnan(c) else prev
    assert checked > 5


# --------------------------------------------------------------------------- #
# Implied annualization convention: VIX/100 == annualized vol fraction
# --------------------------------------------------------------------------- #
def test_implied_annualized_is_fraction():
    idx = pd.bdate_range("2020-01-01", periods=50)
    vix = pd.Series(20.0, index=idx)   # 20 vol points
    imp = fx.implied_annualized(vix, idx)
    # 20 VIX points -> 0.20 annualized vol fraction, apples-to-apples with realized.
    assert np.allclose(imp.dropna().values, 0.20)


# --------------------------------------------------------------------------- #
# Simulation parity: our _simulate_from_vol must match S4's simulate() exactly
# for the stock-S4 estimator (proves we reuse the accounting faithfully).
# --------------------------------------------------------------------------- #
def test_simulate_parity_with_s4():
    import s4_vol_control as s4
    rets, spx_price = s4.build_returns("SPY", "BIL")
    vol = realized_vol_simple(rets["r_spx"], fx.FAST, fx.SLOW)

    ours = fx._simulate_from_vol(rets, spx_price, vol, "2018-01-02", None,
                                 cost_bps=1.0, borrow_bps=50.0)
    theirs = s4.simulate(rets, spx_price, fx.TARGET_VOL, fx.LEVERAGE_CAP,
                         fx.FAST, fx.SLOW, "simple", 0, "2018-01-02", None,
                         cost_bps=1.0, borrow_spread_bps=50.0)
    pd.testing.assert_series_equal(ours["r_tr"], theirs["r_tr"], check_names=False)
    pd.testing.assert_series_equal(ours["exposure"], theirs["exposure"],
                                   check_names=False)


# --------------------------------------------------------------------------- #
# re-risk-lag metric sanity
# --------------------------------------------------------------------------- #
def test_rerisk_lag_basic():
    idx = pd.bdate_range("2020-03-01", periods=120)
    # exposure sits at 0.3 through the trough, then ramps to 1.0 over 40 days.
    exp = pd.Series(0.3, index=idx)
    trough = idx[20]
    ramp = np.linspace(0.3, 1.0, 40)
    exp.iloc[20:60] = ramp
    exp.iloc[60:] = 1.0
    # pre-trough plateau ~0.3 -> target = 0.95 * max(0.3,0.30) = 0.285 -> reached fast.
    lg = fx.rerisk_lag(exp, str(trough.date()), str(idx[-1].date()))
    assert lg["reached"]
    assert lg["days_to_full"] >= 0
    assert lg["exposure_days"] > 0
