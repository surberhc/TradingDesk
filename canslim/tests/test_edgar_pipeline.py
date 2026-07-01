"""
Tests for the EDGAR point-in-time fundamentals pipeline.

Focus: the load-bearing, curve-fit-adjacent logic — canonical concept resolution across
tag-era switches, YTD->discrete-quarter differencing, and (the whole point) the as-of /
no-lookahead guarantee, incl. the restatement case. These use small synthetic fixtures so
they run with no network and no warehouse data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import edgar_pipeline as ep  # noqa: E402


def _fact(**kw):
    """Build a Fact dict with sensible defaults for a duration flow fact."""
    base = dict(
        ticker="TST", cik=1, concept="revenue", tag="Revenues",
        period_end="2020-03-31", period_start="2020-01-01", fy=2020, fp="Q1",
        form="10-Q", filed="2020-04-30", accn="a", value=100.0, qtrs=1, priority=0,
    )
    base.update(kw)
    return base


# --------------------------------------------------------------------------------------
# Canonical mapping / multi-era tag resolution
# --------------------------------------------------------------------------------------

def test_priority_resolves_multi_era_tags_per_period():
    """A period disclosed under both an old and a new tag keeps the higher-priority tag,
    and eras that use different tags are BOTH retained (stitched)."""
    facts = pd.DataFrame([
        # 2018 period disclosed under BOTH SalesRevenueNet (rank 3) and the ASC606 tag (rank 0)
        _fact(period_end="2018-03-31", tag="SalesRevenueNet", priority=3, value=90.0,
              filed="2018-04-30"),
        _fact(period_end="2018-03-31",
              tag="RevenueFromContractWithCustomerExcludingAssessedTax", priority=0,
              value=91.0, filed="2018-04-30"),
        # 2016 period only under the old tag
        _fact(period_end="2016-03-31", tag="SalesRevenueNet", priority=3, value=80.0,
              filed="2016-04-30"),
    ])
    out = ep._as_first_filed(facts)
    # 2018: higher-priority ASC606 value wins
    v2018 = out[out.period_end == "2018-03-31"].value.iloc[0]
    assert v2018 == 91.0
    # 2016: old-tag era retained (not dropped)
    assert (out.period_end == "2016-03-31").any()
    assert out[out.period_end == "2016-03-31"].value.iloc[0] == 80.0


# --------------------------------------------------------------------------------------
# YTD -> discrete quarter differencing
# --------------------------------------------------------------------------------------

def test_ytd_differencing_recovers_discrete_quarters():
    """Q1=100 (3mo), Q2 YTD=250 (6mo), Q3 YTD=430 (9mo), FY=600 (12mo) ->
    discrete quarters 100, 150, 180, 170."""
    facts = pd.DataFrame([
        _fact(fy=2021, qtrs=1, period_start="2021-01-01", period_end="2021-03-31",
              value=100.0, filed="2021-04-30"),
        _fact(fy=2021, qtrs=2, period_start="2021-01-01", period_end="2021-06-30",
              value=250.0, filed="2021-07-30"),
        _fact(fy=2021, qtrs=3, period_start="2021-01-01", period_end="2021-09-30",
              value=430.0, filed="2021-10-30"),
        _fact(fy=2021, qtrs=4, period_start="2021-01-01", period_end="2021-12-31",
              value=600.0, filed="2022-02-28", form="10-K"),
    ])
    disc = ep._difference_ytd_to_quarters(facts).sort_values("fq")
    assert list(disc.fq) == [1, 2, 3, 4]
    assert list(round(disc.value, 2)) == [100.0, 150.0, 180.0, 170.0]


def test_ytd_differencing_derived_quarter_inherits_later_filing_date():
    """The discrete Q2 was not 'known' until the 6-month YTD (Q2 filing) was filed, so its
    filing date must be the Q2 YTD filing date — never the Q1 date. (No lookahead in the
    other direction either: Q1's own date is used for Q1.)"""
    facts = pd.DataFrame([
        _fact(fy=2021, qtrs=1, period_end="2021-03-31", value=100.0, filed="2021-04-30"),
        _fact(fy=2021, qtrs=2, period_start="2021-01-01", period_end="2021-06-30",
              value=250.0, filed="2021-07-30"),
    ])
    disc = ep._difference_ytd_to_quarters(facts).set_index("fq")
    assert disc.loc[1, "filed"] == "2021-04-30"
    assert disc.loc[2, "filed"] == "2021-07-30"


def test_ytd_gap_does_not_subtract_across_a_hole():
    """If an intermediate YTD is missing (Q2 absent), we must NOT compute Q3 = YTD3 - YTD1
    (that would be a 6-month figure mislabeled as a quarter). Q3 is skipped instead."""
    facts = pd.DataFrame([
        _fact(fy=2021, qtrs=1, period_end="2021-03-31", value=100.0, filed="2021-04-30"),
        _fact(fy=2021, qtrs=3, period_start="2021-01-01", period_end="2021-09-30",
              value=430.0, filed="2021-10-30"),
    ])
    disc = ep._difference_ytd_to_quarters(facts)
    assert set(disc.fq) == {1}  # Q3 correctly withheld


# --------------------------------------------------------------------------------------
# THE AS-OF / NO-LOOKAHEAD GUARANTEE  (desk causality discipline)
# --------------------------------------------------------------------------------------

def _restatement_store():
    """Raw PIT store: one period filed twice — original then a later restatement."""
    return pd.DataFrame([
        _fact(concept="revenue", period_end="2021-12-31", qtrs=4,
              period_start="2021-01-01", value=1000.0, filed="2022-02-25"),
        _fact(concept="revenue", period_end="2021-12-31", qtrs=4,
              period_start="2021-01-01", value=1010.0, filed="2024-11-08"),  # restatement
    ])


def test_asof_returns_original_not_restatement():
    """As-of a date AFTER the original filing but BEFORE the restatement returns the ORIGINAL
    value — the later restatement must not leak backward."""
    store = _restatement_store()
    got = ep.asof_quarterly(store, "TST", "revenue", "2022-03-01")
    row = got[got.period_end == "2021-12-31"]
    assert len(row) == 1
    assert row.value.iloc[0] == 1000.0  # original, not 1010 restatement


def test_asof_hides_periods_not_yet_filed():
    """As-of a date BEFORE the period was ever filed, the period is invisible."""
    store = _restatement_store()
    got = ep.asof_quarterly(store, "TST", "revenue", "2022-02-24")
    assert got.empty or "2021-12-31" not in set(got.period_end)


def test_asof_never_shows_a_future_filing():
    """No fact with filed > as_of may ever appear in an as-of query (the core invariant)."""
    store = pd.DataFrame([
        _fact(period_end="2020-03-31", qtrs=1, value=10.0, filed="2020-04-30"),
        _fact(period_end="2020-06-30", qtrs=1, period_start="2020-04-01",
              value=20.0, filed="2020-07-30"),
        _fact(period_end="2020-09-30", qtrs=1, period_start="2020-07-01",
              value=30.0, filed="2020-10-30"),
    ])
    as_of = "2020-08-01"
    got = ep.asof_quarterly(store, "TST", "revenue", as_of)
    assert not got.empty
    assert (pd.to_datetime(got.filed) <= pd.Timestamp(as_of)).all()
    # the Sep-30 quarter (filed Oct) must be absent
    assert "2020-09-30" not in set(got.period_end)


def test_asof_prefers_priority_within_visible_set():
    """When two tag variants of the same visible period exist, the higher-priority tag wins
    in the as-of result too (consistency with _as_first_filed)."""
    store = pd.DataFrame([
        _fact(period_end="2019-03-31", tag="SalesRevenueNet", priority=3, value=50.0,
              filed="2019-04-30"),
        _fact(period_end="2019-03-31",
              tag="RevenueFromContractWithCustomerExcludingAssessedTax", priority=0,
              value=51.0, filed="2019-04-30"),
    ])
    got = ep.asof_quarterly(store, "TST", "revenue", "2019-05-01")
    assert got.value.iloc[0] == 51.0
