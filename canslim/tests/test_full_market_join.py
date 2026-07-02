"""
No-lookahead / leak-free guards for the full-market selection JOIN leg (full_market_join.py).

Load-bearing guarantees (the causality contract of Phase 1):
  1. PRICE leak-free: prices_asof(sym, D) never returns a bar dated after D, and appending
     FUTURE bars cannot change what was returned as-of D.
  2. FUNDAMENTALS leak-free: fundamentals_asof(cik, D) never returns a filing FILED after D;
     a later RESTATEMENT of an old period (filed after D) is invisible as-of D.
  3. joined_asof stitches the two without leaking either leg.

These use tiny synthetic parquet fixtures written to a temp warehouse (monkeypatched paths),
so they run offline with no network and no dependence on the real pull state.
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import full_market_join as fj  # noqa: E402


@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    """A tiny isolated price + fundamentals warehouse with known leak-bait rows."""
    prices = tmp_path / "prices"
    fund = tmp_path / "quarterly_fundamentals_full"
    prices.mkdir(); fund.mkdir()

    # --- price file for symbol FOO: daily bars across a known window ---
    dates = pd.bdate_range("2015-01-01", "2015-03-31")
    px = pd.DataFrame({
        "date": dates,
        "open": range(len(dates)), "high": range(len(dates)),
        "low": range(len(dates)), "close": [100 + i for i in range(len(dates))],
        "volume": [1_000_000] * len(dates), "adj_close": [100 + i for i in range(len(dates))],
        "source": ["tiingo"] * len(dates),
    })
    px.to_parquet(prices / "FOO.parquet", index=False)

    # --- fundamentals for CIK 123 (123 % 20 == 3 -> shard=3) with a leak-bait restatement ---
    # period_end 2014-12-31 was FIRST filed 2015-02-15 (value 1.00),
    # then RESTATED, filed 2015-06-01 (value 9.99). As-of 2015-03-01 only the 1.00 is known.
    rows = [
        # a Q filed before the as-of date (known)
        dict(cik=123, ticker="FOO", fy=2014, fq=3, period_end="2014-09-30",
             filed="2014-11-01", eps_diluted=0.50, eps_growth_yoy=0.10, sales_growth_yoy=0.20,
             sales_growth_qoq=0.05, roe_ttm_annualized=0.15, net_margin=0.1,
             operating_margin=0.12, gross_margin=0.4, revenue=1000, net_income=100),
        # original filing of the annual period
        dict(cik=123, ticker="FOO", fy=2014, fq=4, period_end="2014-12-31",
             filed="2015-02-15", eps_diluted=1.00, eps_growth_yoy=0.25, sales_growth_yoy=0.30,
             sales_growth_qoq=0.05, roe_ttm_annualized=0.18, net_margin=0.1,
             operating_margin=0.12, gross_margin=0.4, revenue=1200, net_income=120),
        # RESTATEMENT of the SAME period, filed later (leak bait)
        dict(cik=123, ticker="FOO", fy=2014, fq=4, period_end="2014-12-31",
             filed="2015-06-01", eps_diluted=9.99, eps_growth_yoy=9.99, sales_growth_yoy=9.99,
             sales_growth_qoq=9.99, roe_ttm_annualized=9.99, net_margin=9.99,
             operating_margin=9.99, gross_margin=9.99, revenue=9999, net_income=9999),
    ]
    pd.DataFrame(rows).to_parquet(fund / "shard=3.parquet", index=False)

    monkeypatch.setattr(fj, "PRICES", prices)
    monkeypatch.setattr(fj, "QUARTERLY_FULL_DIR", fund)
    fj._fund_shard.cache_clear()
    yield tmp_path
    fj._fund_shard.cache_clear()


def test_prices_asof_never_returns_future_bars(warehouse):
    d = pd.Timestamp("2015-02-01")
    out = fj.prices_asof("FOO", d)
    assert out is not None
    assert out["date"].max() <= d


def test_prices_asof_stable_when_future_bars_exist(warehouse):
    """As-of a mid-window date, the returned tail must equal the full-history slice to that date
    (i.e. later bars that DO exist on disk never alter the as-of view)."""
    d = pd.Timestamp("2015-02-01")
    asof = fj.prices_asof("FOO", d)
    full = fj.load_prices("FOO")
    expected = full[full["date"] <= d]
    assert len(asof) == len(expected)
    assert asof["date"].max() == expected["date"].max()


def test_fundamentals_asof_excludes_later_filings(warehouse):
    """As-of 2015-03-01 the ORIGINAL 1.00 annual is known; the 9.99 restatement (filed later)
    must NOT appear."""
    row = fj.latest_fundamentals_asof(123, "2015-03-01")
    assert row is not None
    assert row["period_end"] == pd.Timestamp("2014-12-31")
    assert row["eps_diluted"] == 1.00           # original, not the 9.99 restatement
    hist = fj.fundamentals_asof(123, "2015-03-01")
    assert 9.99 not in set(hist["eps_diluted"])


def test_fundamentals_asof_before_any_filing_is_empty(warehouse):
    assert fj.fundamentals_asof(123, "2014-01-01").empty
    assert fj.latest_fundamentals_asof(123, "2014-01-01") is None


def test_restatement_becomes_visible_only_after_its_filed_date(warehouse):
    """After the restatement's filed date, the latest known value updates (no permanent hiding —
    just correct point-in-time ordering)."""
    row = fj.latest_fundamentals_asof(123, "2015-07-01")
    assert row["eps_diluted"] == 9.99


def test_joined_asof_is_leakfree_on_both_legs(warehouse):
    d = pd.Timestamp("2015-03-01")
    snap = fj.joined_asof(123, "FOO", d)
    assert snap is not None
    assert snap["prices"]["date"].max() <= d
    assert snap["fund_row"]["eps_diluted"] == 1.00
    assert 9.99 not in set(snap["fund_hist"]["eps_diluted"])
