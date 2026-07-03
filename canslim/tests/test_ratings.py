"""
Tests for the Phase-2 CAN SLIM ratings replica (canslim/ratings.py).

Load-bearing guarantees under test:
  1. NO-LOOKAHEAD (the anti-curve-fit contract): at decision date D, ratings depend ONLY on
     price bars dated <= D and fundamentals FILED <= D. Appending FUTURE bars or a later-filed
     RESTATEMENT must NOT change any rating computed as-of D.
  2. RS raw formula matches the frozen [PLAN] spec: 2*(C/C63)+(C/C126)+(C/C189)+(C/C252).
  3. Cross-sectional ratings are integers in 1..99, ties handled, NaNs stay unrated.
  4. HONEST unavailability: I (institutional) and S-float are None, never fabricated.
  5. Deterministic: same inputs -> identical output.

All fixtures are tiny synthetic parquet warehouses (monkeypatched paths), offline, no network,
no dependence on the real pull state — mirroring test_full_market_join.py's approach.
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import full_market_join as fj  # noqa: E402
import ratings as R            # noqa: E402


# ------------------------------------------------------------------------------------------
# Fixtures: a synthetic warehouse with a leak-bait future bar + future restatement.
# ------------------------------------------------------------------------------------------

@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    prices = tmp_path / "prices"
    fund = tmp_path / "quarterly_fundamentals_full"
    prices.mkdir(); fund.mkdir()

    # BULL: a long, smooth uptrend so RS legs are all well-defined and > 1.
    # 400 business days ending 2020-06-30, close rising 100 -> ~ (deterministic ramp).
    dates = pd.bdate_range(end="2020-06-30", periods=400)
    close = np.linspace(100.0, 300.0, len(dates))
    vol = np.full(len(dates), 1_000_000.0)
    vol[-1] = 2_000_000.0  # a demand surge on the last known bar
    px_bull = pd.DataFrame({
        "date": dates, "open": close, "high": close, "low": close,
        "close": close, "volume": vol, "adj_close": close, "source": "synthetic",
    })
    px_bull.to_parquet(prices / "BULL.parquet", index=False)

    # a leak-bait FUTURE spike AFTER the as-of date, to prove it can't change as-of ratings
    future = pd.bdate_range(start="2020-07-01", periods=60)
    px_bull_future = pd.concat([px_bull, pd.DataFrame({
        "date": future, "open": 9999.0, "high": 9999.0, "low": 9999.0,
        "close": 9999.0, "volume": 50_000_000.0, "adj_close": 9999.0, "source": "synthetic",
    })], ignore_index=True)

    # BEAR: a downtrend over the same window (for a meaningful cross-section of >=2 names).
    close_b = np.linspace(300.0, 100.0, len(dates))
    px_bear = pd.DataFrame({
        "date": dates, "open": close_b, "high": close_b, "low": close_b,
        "close": close_b, "volume": vol, "adj_close": close_b, "source": "synthetic",
    })
    px_bear.to_parquet(prices / "BEAR.parquet", index=False)

    # Fundamentals for BULL (cik 200 -> shard 0) — a clean quarterly EPS ladder + a leak-bait
    # RESTATEMENT of the latest known quarter filed AFTER the as-of date.
    rows = []
    # 16 quarters of rising EPS, filed ~45d after each period end, through 2020-03-31.
    pe = pd.period_range("2016-06", "2020-03", freq="Q").to_timestamp(how="end").normalize()
    eps = np.linspace(0.50, 2.00, len(pe))
    for i, (p, e) in enumerate(zip(pe, eps)):
        filed = p + pd.Timedelta(days=45)
        yoy = None if i < 4 else float(eps[i] / eps[i - 4] - 1.0)
        rows.append(dict(cik=200, ticker="BULL", fy=p.year, fq=((p.month - 1) // 3) + 1,
                         period_end=p, filed=filed, eps_diluted=float(e),
                         eps_growth_yoy=yoy, sales_growth_yoy=0.30, sales_growth_qoq=0.05,
                         roe_ttm_annualized=0.25, net_margin=0.15, operating_margin=0.18,
                         gross_margin=0.5, revenue=1000.0 + i, net_income=100.0 + i))
    # leak-bait: RESTATE the 2020-03-31 EPS to an absurd 99.0, filed AFTER our as-of date
    rows.append(dict(cik=200, ticker="BULL", fy=2020, fq=1, period_end=pd.Timestamp("2020-03-31"),
                     filed=pd.Timestamp("2020-12-01"), eps_diluted=99.0, eps_growth_yoy=99.0,
                     sales_growth_yoy=99.0, sales_growth_qoq=99.0, roe_ttm_annualized=99.0,
                     net_margin=99.0, operating_margin=99.0, gross_margin=99.0,
                     revenue=99999.0, net_income=99999.0))
    pd.DataFrame(rows).to_parquet(fund / "shard=0.parquet", index=False)

    # BEAR fundamentals (cik 220 -> shard 0 too): flat EPS, no restatement.
    rows_b = []
    for i, p in enumerate(pe):
        rows_b.append(dict(cik=220, ticker="BEAR", fy=p.year, fq=((p.month - 1) // 3) + 1,
                           period_end=p, filed=p + pd.Timedelta(days=45), eps_diluted=1.0,
                           eps_growth_yoy=(0.0 if i >= 4 else None), sales_growth_yoy=0.0,
                           sales_growth_qoq=0.0, roe_ttm_annualized=0.05, net_margin=0.02,
                           operating_margin=0.03, gross_margin=0.2, revenue=500.0,
                           net_income=10.0))
    # append BEAR rows to the same shard=0 file
    all0 = pd.concat([pd.read_parquet(fund / "shard=0.parquet"), pd.DataFrame(rows_b)],
                     ignore_index=True)
    all0.to_parquet(fund / "shard=0.parquet", index=False)

    monkeypatch.setattr(fj, "PRICES", prices)
    monkeypatch.setattr(fj, "QUARTERLY_FULL_DIR", fund)
    fj._fund_shard.cache_clear()
    yield {"tmp": tmp_path, "prices": prices, "px_bull_future": px_bull_future}
    fj._fund_shard.cache_clear()


ASOF = pd.Timestamp("2020-06-30")


# ------------------------------------------------------------------------------------------
# 1. RS raw formula exactness  [PLAN]
# ------------------------------------------------------------------------------------------

def test_rs_raw_matches_plan_formula(warehouse):
    rr = R.compute_raw(200, "BULL", ASOF)
    px = fj.prices_asof("BULL", ASOF)
    close = px["close"].to_numpy(dtype="float64")
    c0 = close[-1]
    expected = (2.0 * c0 / close[-1 - 63]
                + 1.0 * c0 / close[-1 - 126]
                + 1.0 * c0 / close[-1 - 189]
                + 1.0 * c0 / close[-1 - 252])
    assert rr.rs_raw == pytest.approx(expected, rel=1e-12)


# ------------------------------------------------------------------------------------------
# 2. NO-LOOKAHEAD: future price bars cannot change as-of ratings
# ------------------------------------------------------------------------------------------

def test_future_price_bars_do_not_change_asof_ratings(warehouse):
    before = R.compute_raw(200, "BULL", ASOF)
    # write the leak-bait future-spike price file over BULL, recompute as-of the SAME date
    warehouse["px_bull_future"].to_parquet(warehouse["prices"] / "BULL.parquet", index=False)
    after = R.compute_raw(200, "BULL", ASOF)
    assert after.rs_raw == pytest.approx(before.rs_raw, rel=1e-12)
    assert after.pct_off_52w_high == pytest.approx(before.pct_off_52w_high, rel=1e-12)
    assert after.vol_surge == pytest.approx(before.vol_surge, rel=1e-12)


# ------------------------------------------------------------------------------------------
# 3. NO-LOOKAHEAD: a later-filed restatement is invisible as-of
# ------------------------------------------------------------------------------------------

def test_future_restatement_invisible_asof(warehouse):
    rr = R.compute_raw(200, "BULL", ASOF)
    # the 99.0 restatement is filed 2020-12-01, AFTER 2020-06-30 -> must not leak in
    assert rr.c_eps_yoy is not None
    assert rr.c_eps_yoy < 1.0                 # real YoY ~0..0.5, never the 99.0 leak-bait
    assert rr.a_roe == pytest.approx(0.25)    # the true as-of ROE, not 99.0


def test_restatement_visible_after_its_filed_date(warehouse):
    # as-of AFTER the restatement's filing date, the latest known EPS updates (correct PIT order)
    rr = R.compute_raw(200, "BULL", "2021-01-01")
    assert rr.a_roe == pytest.approx(99.0)    # now the restated row is the latest known


# ------------------------------------------------------------------------------------------
# 4. Cross-sectional ratings: 1..99 integers, RS ordering sane
# ------------------------------------------------------------------------------------------

def test_ratings_are_1_to_99_and_rank_bull_over_bear(warehouse):
    members = pd.DataFrame({"cik": [200, 220], "ticker": ["BULL", "BEAR"]})
    rated = R.rate_universe_asof(ASOF, members)
    assert len(rated) == 2
    for col in ("rs_rating", "eps_rating", "composite_rating"):
        vals = rated[col].dropna()
        assert vals.between(1, 99).all()
        assert (vals == vals.round()).all()   # integer-valued
    bull = rated[rated.ticker == "BULL"].iloc[0]
    bear = rated[rated.ticker == "BEAR"].iloc[0]
    assert bull["rs_rating"] > bear["rs_rating"]          # uptrend outranks downtrend
    assert bull["composite_rating"] > bear["composite_rating"]


# ------------------------------------------------------------------------------------------
# 5. Honest unavailability — I and S-float are None, never fabricated
# ------------------------------------------------------------------------------------------

def test_institutional_and_float_are_explicitly_unavailable(warehouse):
    rr = R.compute_raw(200, "BULL", ASOF)
    assert rr.i_institutional is None
    assert rr.s_float is None
    members = pd.DataFrame({"cik": [200, 220], "ticker": ["BULL", "BEAR"]})
    rated = R.rate_universe_asof(ASOF, members)
    assert rated["i_institutional"].isna().all()          # column present, all None/NaN
    assert "rs_rating" in rated.columns                   # sanity: real components ARE present


# ------------------------------------------------------------------------------------------
# 6. Screen gates are sourced booleans (C/A/N/L present; M absent by design)
# ------------------------------------------------------------------------------------------

def test_screen_flags_present_and_M_absent(warehouse):
    members = pd.DataFrame({"cik": [200, 220], "ticker": ["BULL", "BEAR"]})
    rated = R.rate_universe_asof(ASOF, members)
    for gate in ("C_pass", "A_pass", "N_pass", "L_pass"):
        assert gate in rated.columns
    # M is emergent — there must be no market-direction/timing gate column
    assert not any("M_pass" == c or "market" in c.lower() for c in rated.columns)


# ------------------------------------------------------------------------------------------
# 7. Determinism
# ------------------------------------------------------------------------------------------

def test_deterministic(warehouse):
    members = pd.DataFrame({"cik": [200, 220], "ticker": ["BULL", "BEAR"]})
    a = R.rate_universe_asof(ASOF, members)
    b = R.rate_universe_asof(ASOF, members)
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))


# ------------------------------------------------------------------------------------------
# 8. GUARD: IBD composite grades are DISPLAY-ONLY — perturbing their proprietary-weight
#    approximations must NOT change the raw spec-pinned screen (C/A/N/L). This locks the
#    contract that Phase 3 selection gates only on raw components, never on the composites.
# ------------------------------------------------------------------------------------------

def _screen_pass_set(rated: pd.DataFrame):
    """The set of names passing ALL raw component gates (C_pass & A_pass & N_pass & L_pass)."""
    passed = rated[
        (rated["C_pass"] == True) & (rated["A_pass"] == True)      # noqa: E712
        & (rated["N_pass"] == True) & (rated["L_pass"] == True)    # noqa: E712
    ]
    return set(passed["ticker"])


def test_composite_weights_do_not_affect_screen(warehouse, monkeypatch):
    members = pd.DataFrame({"cik": [200, 220], "ticker": ["BULL", "BEAR"]})

    baseline = R.rate_universe_asof(ASOF, members)
    baseline_pass = _screen_pass_set(baseline)

    # Perturb the proprietary-weight approximations that feed ONLY the display grades
    # (eps_rating / composite_rating): the EPS-blend recent-quarter weight AND every
    # composite-blend weight. If any decision path secretly gated on a composite grade, the
    # raw screen-pass set would shift; it must not.
    monkeypatch.setattr(R, "EPS_W_Q0", R.EPS_W_Q0 * 7.0 + 3.0)
    monkeypatch.setattr(R, "COMPOSITE_W_EPS", R.COMPOSITE_W_EPS * 11.0 + 5.0)
    monkeypatch.setattr(R, "COMPOSITE_W_RS", R.COMPOSITE_W_RS * 0.3 + 0.1)
    monkeypatch.setattr(R, "COMPOSITE_W_SMR", R.COMPOSITE_W_SMR * 13.0)
    monkeypatch.setattr(R, "COMPOSITE_W_NEARHIGH", R.COMPOSITE_W_NEARHIGH * 0.0 + 0.5)

    perturbed = R.rate_universe_asof(ASOF, members)
    perturbed_pass = _screen_pass_set(perturbed)

    # 1. The raw screen-pass SET is byte-identical before/after the weight perturbation.
    assert perturbed_pass == baseline_pass

    # 2. And every raw gate column itself is unchanged row-for-row (stronger than just the set).
    for gate in ("C_pass", "A_pass", "N_pass", "L_pass"):
        pd.testing.assert_series_equal(
            baseline.set_index("ticker")[gate].sort_index(),
            perturbed.set_index("ticker")[gate].sort_index(),
            check_names=False,
        )

    # 3. Sanity: the perturbed EPS weight DOES move the underlying (pre-percentile) composite
    #    blend — proving the test actually exercised the weights (otherwise it would pass
    #    vacuously). The 1-99 percentile mapping is degenerate on a 2-name universe (BULL always
    #    ranks 99, BEAR 50), which is exactly WHY it must never back a decision; we therefore probe
    #    the raw blend directly on a small frame whose legs diverge enough to register the change.
    probe = pd.DataFrame({
        "c_eps_yoy": [0.10, 5.00],          # recent-quarter YoY (the leg EPS_W_Q0 weights)
        "c_eps_yoy_prior": [0.10, 0.10],
        "a_eps_growth_3y": [0.10, 0.10],
        "eps_stability": [0.80, 0.80],
    })
    R.EPS_W_Q0 = 2.0                         # restore baseline weight for the reference blend
    base_blend = R._eps_rating_raw(probe).to_numpy()
    R.EPS_W_Q0 = 2.0 * 7.0 + 3.0             # re-apply the perturbation
    pert_blend = R._eps_rating_raw(probe).to_numpy()
    assert not np.allclose(base_blend, pert_blend)
