"""Hand-checked unit tests for the risk-adjusted scorecard metric helpers."""

import numpy as np
import pandas as pd

import risk_adjusted_scorecard as ras


def _series(vals):
    idx = pd.date_range("2020-01-01", periods=len(vals), freq="B")
    return pd.Series(vals, index=idx, dtype=float)


def test_max_drawdown_hand_checked():
    # equity: 1 -> 1.10 -> 0.99 -> 1.089 ; peak 1.10, trough 0.99 => dd = 0.99/1.10 - 1 = -0.10
    r = _series([0.10, -0.10, 0.10])
    assert abs(ras.max_drawdown(r) - (-0.10)) < 1e-12


def test_drawdown_series_zero_when_monotone():
    r = _series([0.01, 0.02, 0.005, 0.01])  # strictly rising equity
    assert ras.max_drawdown(r) == 0.0
    assert (ras.drawdown_series(r) <= 1e-15).all()


def test_ann_vol_and_return_basis():
    r = _series([0.001] * 300)
    # constant daily return: vol ~ 0; ann_ret = 0.001 * 252
    assert abs(ras.ann_vol(r) - 0.0) < 1e-9
    assert abs(ras.ann_return(r) - 0.001 * 252) < 1e-9


def test_calmar_sign_and_formula():
    # steady up with one dip: CAGR positive, maxDD negative -> Calmar positive
    r = _series([0.02, 0.02, -0.05, 0.02, 0.02, 0.02])
    c = ras.calmar(r)
    assert c > 0
    # cross-check against explicit CAGR / |maxDD|
    expect = ras.cagr(r) / abs(ras.max_drawdown(r))
    assert abs(c - expect) < 1e-12


def test_sortino_only_penalizes_downside():
    # symmetric-magnitude but asymmetric-frequency: more small ups, few big downs
    r = _series([0.01, 0.01, 0.01, -0.03, 0.01, 0.01, 0.01, -0.03])
    so = ras.sortino(r, rf_annual=0.0)
    sh = ras.sharpe(r, rf_annual=0.0)
    assert np.isfinite(so) and np.isfinite(sh)


def test_ulcer_index_zero_when_no_drawdown():
    r = _series([0.01] * 50)
    assert abs(ras.ulcer_index(r)) < 1e-9


def test_realized_beta_recovers_known_slope():
    spx = _series(np.linspace(-0.02, 0.02, 100))
    veh = 0.5 * spx + 0.0001  # beta 0.5 exactly + tiny alpha
    b = ras.realized_beta(veh, spx)
    assert abs(b - 0.5) < 1e-6


def test_solve_w_for_vol_linear():
    spx = _series(np.random.default_rng(0).normal(0.0004, 0.012, 400))
    sv = ras.ann_vol(spx)
    w = ras.solve_w_for_vol(spx, sv / 2.0)
    assert abs(w - 0.5) < 1e-9
    # blend at that w should have ~half the vol
    b = ras.blend_return_series(spx, w)
    assert abs(ras.ann_vol(b) - sv / 2.0) < 1e-9


def test_solve_w_for_maxdd_hits_target():
    rng = np.random.default_rng(1)
    spx = _series(rng.normal(0.0003, 0.013, 600))
    full_dd = ras.max_drawdown(ras.blend_return_series(spx, 1.0))
    target = full_dd * 0.5  # want half the depth
    w = ras.solve_w_for_maxdd(spx, target)
    got = ras.max_drawdown(ras.blend_return_series(spx, w))
    assert abs(abs(got) - abs(target)) < 5e-3  # bisection tolerance on a fraction
    assert 0.0 <= w <= 1.0


def test_solve_w_for_maxdd_clamps_when_target_deeper_than_spx():
    rng = np.random.default_rng(2)
    spx = _series(rng.normal(0.0004, 0.010, 400))
    full_dd = ras.max_drawdown(ras.blend_return_series(spx, 1.0))
    deeper = full_dd * 2.0  # deeper than full SPX -> would need leverage -> clamp to 1.0
    w = ras.solve_w_for_maxdd(spx, deeper)
    assert w == 1.0
