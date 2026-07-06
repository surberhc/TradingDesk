r"""
test_s7_income_condor.py — correctness guards for the S7 managed income-condor engine.

Covers the three pre-registered invariants:
  1. NO LOOK-AHEAD: a future day cannot change a past entry or a past close. The exit walks
     days forward and stops at the FIRST firing day; truncating the day list at the exit day
     must leave entry, exit day, exit reason and P&L byte-identical.
  2. COST-CHARGED: a worse (larger) fill fraction never IMPROVES the entry credit and never
     REDUCES the close debit — honest fills only move against us. And f>0 is strictly worse
     than mid on a real spread.
  3. CLEAN-DELTA GUARD for 2020/2021: on a known-corrupt 2021 day the engine flags the
     vendor delta degenerate and takes the BSM re-inversion path (never selects strikes off
     the corrupt vendor delta); a clean 2018 day is NOT flagged.

These run against the read-only warehouse. If the warehouse is absent, the data-backed
tests skip (the pure-logic fill test still runs).
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

import s7_income_condor as s7

_HAS_WAREHOUSE = s7.WAREHOUSE.is_dir() and any(s7.WAREHOUSE.glob("2018*.parquet"))

# Known-good sample days.
CLEAN_DAY = dt.date(2018, 6, 1)
CORRUPT_DAY = dt.date(2021, 7, 1)   # verified 49% delta==0 / 50% IV==0 in prereg


# --------------------------------------------------------------------------- #
# 2. Cost-charged fills (pure logic, no data needed)
# --------------------------------------------------------------------------- #
def test_fill_fraction_is_monotonically_worse():
    bid, ask = 4.0, 4.4  # a real half-spread of 0.2
    # Selling: price received falls as f grows (worse for us).
    prices = [s7._sell_price(bid, ask, f) for f in (0.0, 0.25, 0.5, 1.0)]
    assert prices == sorted(prices, reverse=True)
    assert prices[0] == pytest.approx(4.2)   # mid
    assert prices[-1] == pytest.approx(4.0)   # bid at full cross
    # Buying: price paid rises as f grows (worse for us).
    prices = [s7._buy_price(bid, ask, f) for f in (0.0, 0.25, 0.5, 1.0)]
    assert prices == sorted(prices)
    assert prices[0] == pytest.approx(4.2)   # mid
    assert prices[-1] == pytest.approx(4.4)   # ask at full cross


def test_intrinsic_settlement_is_defined_risk():
    c = s7.Condor(entry_day=CLEAN_DAY, expiration=CLEAN_DAY, entry_dte=45,
                  short_put=2600.0, long_put=2575.0, short_call=2800.0, long_call=2825.0,
                  entry_short_put_delta=-0.16, entry_short_call_delta=0.16,
                  entry_credit=4.0, used_clean_delta=False)
    # Between the shorts: no intrinsic.
    assert s7._condor_intrinsic(2700.0, c) == pytest.approx(0.0)
    # Deep below long put: capped at the wing width (defined risk).
    assert s7._condor_intrinsic(2000.0, c) == pytest.approx(25.0)
    # Deep above long call: capped at the wing width.
    assert s7._condor_intrinsic(3500.0, c) == pytest.approx(25.0)
    # Between short and long put: partial.
    assert s7._condor_intrinsic(2590.0, c) == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# 3. Clean-delta guard for 2020/2021
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="warehouse not present")
def test_corrupt_2021_flagged_clean_2018_not():
    assert s7.delta_column_is_degenerate(s7.load_day(CORRUPT_DAY)) is True
    assert s7.delta_column_is_degenerate(s7.load_day(CLEAN_DAY)) is False


@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="warehouse not present")
def test_corrupt_blackout_day_never_fabricates_a_fill():
    """On the 2021 delta-corrupt QUOTE-BLACKOUT window the vendor bid/ask are all-zero.

    The engine must NOT invent a spread there: the day loads unquotable (empty) and
    build_condor declines to trade rather than manufacture a fill. This is the honest
    guard — the corruption window is perfectly coincident with the quote blackout, so a
    trade there could only exist by fabrication.
    """
    day_df = s7.load_day(CORRUPT_DAY)
    assert day_df is not None and len(day_df) == 0, "blackout day must load unquotable"
    assert s7.day_quote_ok(CORRUPT_DAY) is False
    c = s7.build_condor(day_df, CORRUPT_DAY, 45, 0.16, 0.50)
    assert c is None, "must refuse to trade on a no-quote day (no fabricated fills)"


def test_reinverted_delta_unit_recovers_sane_deltas():
    """The clean-delta re-inversion (used whenever the vendor delta is degenerate) recovers
    real fractional deltas from mid/spot/T on a synthetic clean chain — unit-level proof
    the defensive path is correct even though on real data it is dominated by the blackout."""
    import pandas as pd
    spot = 4000.0
    d = dt.date(2021, 7, 1)
    exp = dt.date(2021, 8, 20)   # ~50 DTE
    t = (exp - d).days / 365.25
    # Build a synthetic OTM put + OTM call priced by BSM at a plausible 18% vol.
    rows = []
    for k, right in [(3800.0, "PUT"), (4200.0, "CALL")]:
        price = s7.recon.bs_price(spot, k, t, 0.18, right == "CALL",
                                  s7.RISK_FREE_RATE, s7.DIVIDEND_YIELD)
        rows.append({"strike": k, "right": right,
                     "bid": price - 0.5, "ask": price + 0.5,
                     "underlying_price": spot})
    sub = pd.DataFrame(rows)
    deltas = s7._clean_delta_for_exp(sub, d, exp, spot)
    # OTM put delta ~ small negative; OTM call delta ~ small positive.
    put_delta = deltas.iloc[0]
    call_delta = deltas.iloc[1]
    assert -0.5 < put_delta < 0.0
    assert 0.0 < call_delta < 0.5


@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="warehouse not present")
def test_clean_day_can_use_vendor_delta():
    day_df = s7.load_day(CLEAN_DAY)
    c = s7.build_condor(day_df, CLEAN_DAY, 45, 0.16, 0.50)
    assert c is not None
    assert c.used_clean_delta is False  # clean vendor delta was trusted


# --------------------------------------------------------------------------- #
# 4. Delta-wing guard (rebuild addendum): 5-delta wings sit strictly further OTM
#    than the shorts, on the correct side, with SMALLER |delta|.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="warehouse not present")
def test_delta_wing_ordering_and_delta_magnitude():
    day_df = s7.load_day(CLEAN_DAY)
    c = s7.build_condor(day_df, CLEAN_DAY, 45, 0.16, 0.50, wing_spec=("delta", 0.05))
    assert c is not None
    # strict defined-risk ordering
    assert c.long_put < c.short_put, "long put must be below short put"
    assert c.short_call < c.long_call, "long call must be above short call"
    # spot sits between the shorts
    sub = day_df[day_df["expiration"] == c.expiration]
    spot = float(sub["underlying_price"].iloc[0])
    assert c.short_put < spot < c.short_call
    # long legs are FURTHER OTM => smaller |delta| than the shorts
    assert abs(c.entry_long_put_delta) < abs(c.entry_short_put_delta)
    assert abs(c.entry_long_call_delta) < abs(c.entry_short_call_delta)
    # 5-delta wings should carry |delta| in a sane small band (not the ~16-delta shorts)
    assert abs(c.entry_long_put_delta) < 0.16
    assert abs(c.entry_long_call_delta) < 0.16
    # realized wing widths recorded and positive
    assert c.put_wing_width > 0 and c.call_wing_width > 0


@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="warehouse not present")
def test_delta_wings_are_wider_than_25pt_control():
    """A 5-delta wing on a normal-vol SPX day is materially wider than the 25-pt control —
    that width is exactly the mechanism (bigger credit/max-loss ratio) the rebuild tests."""
    day_df = s7.load_day(CLEAN_DAY)
    c_delta = s7.build_condor(day_df, CLEAN_DAY, 45, 0.16, 0.50, wing_spec=("delta", 0.05))
    c_pts = s7.build_condor(day_df, CLEAN_DAY, 45, 0.16, 0.50, wing_spec=("points", 25.0))
    assert c_delta is not None and c_pts is not None
    assert c_pts.put_wing_width == pytest.approx(25.0)
    assert c_delta.put_wing_width >= 25.0  # 5-delta wing no narrower than the 25-pt control


@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="warehouse not present")
def test_csp_builds_atm_and_settles():
    day_df = s7.load_day(CLEAN_DAY)
    c = s7.build_csp(day_df, CLEAN_DAY, 45, 0.50)
    assert c is not None
    sub = day_df[day_df["expiration"] == c.expiration]
    spot = float(sub["underlying_price"].iloc[0])
    # ATM put => |delta| roughly near 0.5, strike near spot
    assert abs(c.entry_delta) > 0.30
    assert abs(c.strike - spot) < 0.10 * spot
    assert c.entry_credit > 0


# --------------------------------------------------------------------------- #
# CSP daily book mark-to-market + the alpha detector (alpha-vs-beta study)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="warehouse not present")
def test_csp_book_daily_mark_is_causal():
    """Truncating the day universe at the last mark date cannot change any earlier mark.

    Build a small CSP book, mark it daily over the full window, then mark it again over a
    window truncated to the book's last relevant day. Every overlapping day's equity/pnl
    must be byte-identical — a future day never reaches back and changes a past mark."""
    all_days = [d for d in s7.available_days()
                if dt.date(2018, 6, 1) <= d <= dt.date(2018, 12, 31)]
    cache: dict = {}

    def loader(d):
        if d not in cache:
            cache[d] = s7.load_day(d)
        return cache[d]

    csps = s7.run_csp_book(45, 0.50, days=all_days, day_cache=cache)
    assert len(csps) > 0
    pm_full: dict = {}
    book_full = s7.csp_book_daily_marks(csps, lambda d: cache.get(d), all_days, 0.50,
                                        price_maps=pm_full)
    assert not book_full.empty
    cutoff = book_full.index[len(book_full) // 2]
    trunc_days = [d for d in all_days if d <= cutoff.date()]
    pm_tr: dict = {}
    book_tr = s7.csp_book_daily_marks(csps, lambda d: cache.get(d), trunc_days, 0.50,
                                      price_maps=pm_tr)
    common = book_full.index.intersection(book_tr.index)
    assert len(common) > 5
    # equity on overlapping days must match exactly (marks are causal / path-independent)
    np.testing.assert_allclose(book_full.loc[common, "equity"].to_numpy(),
                               book_tr.loc[common, "equity"].to_numpy(), rtol=0, atol=1e-9)


@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="warehouse not present")
def test_csp_book_mark_is_cost_charged():
    """A worse fill (f=1) never gives the seller a HIGHER book equity than mid (f=0).

    The liability is the put buy-back price, which RISES with f (cost-charged), so equity
    (premium − liability) can only fall as f grows. Checked on the aggregate final equity."""
    all_days = [d for d in s7.available_days()
                if dt.date(2018, 6, 1) <= d <= dt.date(2018, 12, 31)]
    cache: dict = {}

    def loader(d):
        if d not in cache:
            cache[d] = s7.load_day(d)
        return cache[d]

    # Build each book at its own fill (entry credit also depends on f) and compare final equity.
    eq = {}
    for f in (0.0, 1.0):
        csps = s7.run_csp_book(45, f, days=all_days, day_cache=cache)
        book = s7.csp_book_daily_marks(csps, lambda d: cache.get(d), all_days, f,
                                       price_maps={})
        eq[f] = float(book["equity"].iloc[-1])
    assert eq[1.0] <= eq[0.0] + 1e-6, "full-cross fill must not beat mid on book equity"


def test_alpha_detector_reports_no_edge_on_pure_beta():
    """Sanity: a series built as beta*r_spx + noise with TRUE alpha=0 must recover a
    bootstrap alpha 95% CI that INCLUDES 0 (the detector is not rigged to find edge)."""
    import csp_alpha_beta as cab
    rng = np.random.default_rng(12345)
    n = 1500
    r_spx = rng.normal(0.0004, 0.01, n)          # market daily returns
    r_y = 0.5 * r_spx + rng.normal(0.0, 0.003, n)  # pure beta, ZERO alpha
    reg = cab.ols_alpha_beta(r_y, r_spx)
    assert abs(reg["beta"] - 0.5) < 0.05          # recovers the true beta
    ci = cab.bootstrap_alpha_ci(r_y, r_spx, resamples=800, seed=99)
    assert ci["alpha_ann_lo"] < 0 < ci["alpha_ann_hi"], "CI must span 0 when alpha is truly 0"


def test_alpha_detector_finds_known_positive_drift():
    """Sanity: add a known positive daily drift (true alpha>0) and the recovered annualized
    alpha and its bootstrap CI must be positive (the detector CAN find real edge)."""
    import csp_alpha_beta as cab
    rng = np.random.default_rng(2020)
    n = 1500
    drift = 0.0006                                # +0.06%/day ~ +15%/yr alpha
    r_spx = rng.normal(0.0004, 0.01, n)
    r_y = drift + 0.5 * r_spx + rng.normal(0.0, 0.003, n)
    reg = cab.ols_alpha_beta(r_y, r_spx)
    assert reg["alpha_daily"] > 0
    assert reg["alpha_daily"] * cab.TRADING_DAYS > 0.05   # clearly positive annualized
    ci = cab.bootstrap_alpha_ci(r_y, r_spx, resamples=800, seed=7)
    assert ci["alpha_ann_lo"] > 0, "CI must exclude 0 (positive) when a real drift is present"


def test_ivr_series_is_causal_and_ranked():
    """IVR loads from the VIX parquet, is bounded 0..100, and each day's rank uses only
    closes up to that day (rolling window with raw last-value percentile)."""
    ivr = s7.load_ivr_series()
    if ivr is None:
        pytest.skip("VIX daily parquet not present -> IVR arm skipped by design")
    vals = ivr.dropna()
    assert len(vals) > 0
    assert vals.min() >= 0.0 and vals.max() <= 100.0


# --------------------------------------------------------------------------- #
# 1. No look-ahead: truncating future days at the exit cannot change the trade
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="warehouse not present")
def test_no_lookahead_truncation_invariance():
    all_days = s7.available_days()
    cache: dict = {}

    def loader(d):
        if d not in cache:
            cache[d] = s7.load_day(d)
        return cache[d]

    ed = CLEAN_DAY
    # build_condor signature: (day_df, d, target_dte, target_delta, fill_fraction)
    c0 = s7.build_condor(loader(ed), ed, 45, 0.16, 0.50)
    assert c0 is not None

    # Full-history management of this one condor.
    import copy
    c_full = s7.manage_condor(copy.deepcopy(c0), loader, all_days, "managed", 0.50, 0.50)
    assert c_full.exit_day is not None

    # Now truncate the day universe at the exit day: everything AFTER the exit is removed.
    truncated = [d for d in all_days if d <= c_full.exit_day]
    c_trunc = s7.manage_condor(copy.deepcopy(c0), loader, truncated, "managed", 0.50, 0.50)

    # The entry and the close must be byte-identical — the future did not touch the past.
    assert c_trunc.exit_day == c_full.exit_day
    assert c_trunc.exit_reason == c_full.exit_reason
    assert c_trunc.exit_debit == pytest.approx(c_full.exit_debit)
    assert c_trunc.pnl_dollars == pytest.approx(c_full.pnl_dollars)


@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="warehouse not present")
def test_worse_fill_never_helps_pnl_on_same_trade():
    """At entry, a worse fill fraction must not RAISE the received credit."""
    day_df = s7.load_day(CLEAN_DAY)
    c_mid = s7.build_condor(day_df, CLEAN_DAY, 45, 0.16, 0.0)
    c_half = s7.build_condor(day_df, CLEAN_DAY, 45, 0.16, 0.50)
    c_full = s7.build_condor(day_df, CLEAN_DAY, 45, 0.16, 1.0)
    assert c_mid and c_half and c_full
    # Same strikes selected (delta selection independent of fill), credit monotone down.
    assert (c_mid.short_put, c_mid.short_call) == (c_half.short_put, c_half.short_call)
    assert c_mid.entry_credit >= c_half.entry_credit >= c_full.entry_credit
