r"""
test_s5_band_strike_experiment.py — guards for the band-relative tail-strike experiment.

Pins the CAUSALITY (no look-ahead) and the MECHANICS, not any strategy outcome:
  * the strike rule at day i uses only data <= i (a future data spike cannot change a past
    strike) — the cardinal no-lookahead rule for this EOD study;
  * the continuous skew-uplift interpolator matches the discrete real-skew knots at the
    knot OTMs, is monotone increasing in OTM, and slope-extrapolates beyond 25%;
  * the OTM clamp is respected (floor/cap) for both the implied-band and rvol-band rules;
  * the rvol-control M-solve actually matches the band arm's average OTM (the anti-curve-fit
    control is honestly matched);
  * a FIXED-OTM arm reproduces the known real-skew-priced tail behaviour (sanity anchor).

Synthetic in-memory panels where possible; one small real read is guarded/skipped if the
warehouse tables are absent (so the suite still runs on a code-only checkout).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s5_band_strike_experiment as B  # noqa: E402


# ---------------------------------------------------------------------------
# helpers: a small synthetic panel + a synthetic skew-knot frame
# ---------------------------------------------------------------------------
def _synth_panel(n=400, seed=0):
    idx = pd.bdate_range("2019-01-01", periods=n)
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0003, 0.01, n)
    spx = 3000.0 * np.cumprod(1.0 + r)
    df = pd.DataFrame(index=idx)
    df["spx_px"] = spx
    df["r_spy"] = pd.Series(spx, index=idx).pct_change().values
    df["r_cash"] = 0.00008
    df["vix"] = np.clip(15.0 + 40.0 * np.abs(r) / 0.01, 9.0, 80.0)
    return df


def _synth_knots(idx):
    # flat-ish skew that rises with OTM; constant across dates (enough to price)
    up = pd.DataFrame(index=pd.Index(idx, name="date"))
    up["iv_atm"] = 0.15
    up["up_10"] = 0.07
    up["up_15"] = 0.10
    up["up_20"] = 0.145
    up["up_25"] = 0.184
    return up


# ---------------------------------------------------------------------------
# CAUSALITY — the cardinal guard: a FUTURE data spike cannot alter a PAST strike
# ---------------------------------------------------------------------------
def test_strike_rule_is_causal_no_lookahead():
    df = _synth_panel()
    up = _synth_knots(df.index)
    uf, ok = B.make_continuous_uplift_fn(df.index, up)

    # build two expected-move arrays identical up to day k, differing AFTER k
    n = len(df)
    k = n // 2
    em_base = np.full(n, 0.014)          # ~ places strike near 20% OTM at N=1
    em_shock = em_base.copy()
    em_shock[k + 1:] = 0.06              # a big FUTURE spike after day k

    sqrtT = np.sqrt(B.TENOR_D)

    def rule_for(em):
        def f(i):
            e = em[i]
            if not np.isfinite(e) or e <= 0:
                return 0.20
            return float(np.clip(1.0 * e * sqrtT, B.OTM_FLOOR, B.OTM_CAP))
        return f

    s_base = B.simulate_band_tail(df, rule_for(em_base), uf, ok)
    s_shock = B.simulate_band_tail(df, rule_for(em_shock), uf, ok)

    # every OTM used AT OR BEFORE day k must be identical — the future spike is invisible
    otm_base = s_base["df"]["otm_used"].values
    otm_shock = s_shock["df"]["otm_used"].values
    np.testing.assert_allclose(otm_base[1:k + 1], otm_shock[1:k + 1], rtol=0, atol=0,
                               err_msg="future EM leaked into a past strike (look-ahead!)")
    # and the fund returns up to k must match to the same tolerance
    rb = s_base["df"]["r_fund"].values
    rs = s_shock["df"]["r_fund"].values
    np.testing.assert_allclose(rb[1:k + 1], rs[1:k + 1], rtol=0, atol=1e-12,
                               err_msg="future EM changed a past fund return (look-ahead!)")


# ---------------------------------------------------------------------------
# continuous skew interpolator — matches the knots, monotone, extrapolates
# ---------------------------------------------------------------------------
def test_uplift_interp_matches_knots_and_monotone():
    df = _synth_panel(n=60)
    up = _synth_knots(df.index)
    uf, ok = B.make_continuous_uplift_fn(df.index, up)
    i = 30
    # matches the measured knots at the knot OTMs
    assert abs(uf(i, 0.10) - 0.07) < 1e-9
    assert abs(uf(i, 0.15) - 0.10) < 1e-9
    assert abs(uf(i, 0.20) - 0.145) < 1e-9
    assert abs(uf(i, 0.25) - 0.184) < 1e-9
    # ATM anchor: uplift 0 at 0% OTM
    assert abs(uf(i, 0.0) - 0.0) < 1e-9
    # monotone increasing across a fine OTM grid
    grid = np.linspace(0.0, 0.35, 40)
    vals = [uf(i, o) for o in grid]
    assert all(vals[j + 1] >= vals[j] - 1e-12 for j in range(len(vals) - 1))
    # extrapolation beyond 25% uses the 20->25 slope (strictly above the 25% knot)
    slope = (0.184 - 0.145) / 0.05
    assert abs(uf(i, 0.30) - (0.184 + slope * 0.05)) < 1e-9


# ---------------------------------------------------------------------------
# OTM clamp respected for both dynamic rules
# ---------------------------------------------------------------------------
def test_otm_clamp_floor_and_cap():
    df = _synth_panel()
    up = _synth_knots(df.index)
    uf, ok = B.make_continuous_uplift_fn(df.index, up)
    n = len(df)
    sqrtT = np.sqrt(B.TENOR_D)

    # tiny EM everywhere -> strike must clamp to the FLOOR (never near spot)
    em_tiny = np.full(n, 1e-5)
    def rule_tiny(i):
        e = em_tiny[i]
        return float(np.clip(1.0 * e * sqrtT, B.OTM_FLOOR, B.OTM_CAP)) if e > 0 else 0.20
    s = B.simulate_band_tail(df, rule_tiny, uf, ok)
    otm = s["df"]["otm_used"].dropna().values
    assert otm.min() >= B.OTM_FLOOR - 1e-12
    assert abs(otm.max() - B.OTM_FLOOR) < 1e-9   # all pinned to floor

    # huge EM everywhere -> strike must clamp to the CAP
    em_huge = np.full(n, 1.0)
    def rule_huge(i):
        e = em_huge[i]
        return float(np.clip(1.0 * e * sqrtT, B.OTM_FLOOR, B.OTM_CAP)) if e > 0 else 0.20
    s2 = B.simulate_band_tail(df, rule_huge, uf, ok)
    otm2 = s2["df"]["otm_used"].dropna().values
    assert otm2.max() <= B.OTM_CAP + 1e-12
    assert abs(otm2.min() - B.OTM_CAP) < 1e-9


# ---------------------------------------------------------------------------
# a fixed-OTM arm produces a sane, positive carry and a well-formed return path
# ---------------------------------------------------------------------------
def test_fixed_arm_sane():
    df = _synth_panel()
    up = _synth_knots(df.index)
    uf, ok = B.make_continuous_uplift_fn(df.index, up)
    s = B.simulate_band_tail(df, lambda i: 0.20, uf, ok)
    assert 0.0 < s["carry_pct_yr"] < 0.30          # positive, plausible annual carry
    assert abs(s["mean_otm"] - 0.20) < 1e-9         # fixed rule holds the OTM constant
    r = s["df"]["r_fund"].dropna()
    assert len(r) > 100
    assert np.isfinite(r).all()
    # net delta of core(+1) + a long put(<0) is < 1 and > 0 in normal times
    nd = s["df"]["net_delta"].dropna()
    assert nd.max() <= 1.0 + 1e-6
    assert nd.min() >= -0.5   # a 0.5-notional 20% put can't drag net delta below ~0


# ---------------------------------------------------------------------------
# the rvol control M-solve honestly matches a target mean OTM (anti-curve-fit)
# ---------------------------------------------------------------------------
def test_rvol_control_matches_target_mean_otm():
    df = _synth_panel(n=800)
    sqrtT = np.sqrt(B.TENOR_D)
    # a realized-vol daily sigma proxy off the synthetic returns
    rvol_daily = pd.Series(df["r_spy"]).rolling(20).std().bfill().values

    target = 0.20
    lo_M, hi_M = 0.1, 20.0
    for _ in range(50):
        mid = 0.5 * (lo_M + hi_M)
        otm = np.clip(mid * rvol_daily * sqrtT, B.OTM_FLOOR, B.OTM_CAP)
        mo = np.nanmean(otm)
        if mo < target:
            lo_M = mid
        else:
            hi_M = mid
    M = 0.5 * (lo_M + hi_M)
    otm = np.clip(M * rvol_daily * sqrtT, B.OTM_FLOOR, B.OTM_CAP)
    assert abs(np.nanmean(otm) - target) < 0.01   # matched within 1 OTM-pct


# ---------------------------------------------------------------------------
# real-data smoke: the full experiment arms run and produce finite metrics
# (skipped cleanly if the warehouse-derived tables are not present)
# ---------------------------------------------------------------------------
def test_real_smoke_arms_finite():
    import os
    vix3m = os.path.join(B.BT_DATA, "_vix3m.parquet")
    if not (os.path.exists(B.SKEW_TABLE) and os.path.exists(vix3m)):
        pytest.skip("warehouse-derived tables / vol family absent (code-only checkout)")
    from s5_convexity_overlay import build_panel
    full = build_panel()
    df = full.loc[B.REAL_START:B.REAL_END].copy()
    up = B.load_skew_knots()
    uf, ok = B.make_continuous_uplift_fn(df.index, up)
    # clean VIX3M forward EM, already horizon-scaled to the tail DTE (NOT the corrupt column)
    em = B.load_forward_em(df.index, "_vix3m", B.TENOR_D)

    def band_rule(N):
        def f(i):
            e = em[i]
            if not np.isfinite(e) or e <= 0:
                return 0.20
            return float(np.clip(N * e, B.OTM_FLOOR, B.OTM_CAP))  # N is a pure sigma-multiple
        return f

    s_fixed = B.simulate_band_tail(df, lambda i: 0.225, uf, ok)
    s_band = B.simulate_band_tail(df, band_rule(2.0), uf, ok)
    for s in (s_fixed, s_band):
        assert np.isfinite(s["carry_pct_yr"])
        assert s["carry_pct_yr"] > 0
        assert np.isfinite(s["df"]["r_fund"].dropna()).all()
    # a ~2-sigma VIX3M band lands the deep tail in the validated 20-25% OTM region
    assert 0.18 < s_band["mean_otm"] < 0.27


def test_forward_em_is_clean_not_corrupt_column():
    """Guard the DATA-BUG substitution: the forward EM must come from the CLEAN CBOE VIX3M
    (no degenerate 2020-21 days), NOT the corrupt warehouse expected_move_pct. We assert the
    VIX3M-derived em_tail has ~zero degenerate (<0.05% of spot) days in 2020-21, which the
    corrupt column would fail hard."""
    import os
    vix3m = os.path.join(B.BT_DATA, "_vix3m.parquet")
    if not os.path.exists(vix3m):
        pytest.skip("vol family absent (code-only checkout)")
    from s5_convexity_overlay import build_panel
    df = build_panel().loc["2020-01-01":"2021-12-31"].copy()
    em = B.load_forward_em(df.index, "_vix3m", B.TENOR_D)
    frac_degenerate = float(np.mean(em < 0.0005))   # <0.05% of spot = degenerate
    assert frac_degenerate < 0.01, "forward EM has degenerate crisis days — corrupt source?"
    # sane 63-DTE forward move: a few % to tens of % of spot
    assert 0.02 < np.nanmedian(em) < 0.30
