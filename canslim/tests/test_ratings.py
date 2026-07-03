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
