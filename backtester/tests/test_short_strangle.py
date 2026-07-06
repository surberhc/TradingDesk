r"""
test_short_strangle.py — correctness guards for the delta-neutral short-strangle VRP study.

Covers the pre-registered invariants for the strangle engine (short_strangle.py):
  1. UNCAPPED settlement: a deep crash produces a LARGE uncapped put-side loss (NOT capped at
     any wing width — a strangle has no wings). This is the realism the whole study rests on.
  2. COST-CHARGED daily mark: a worse fill (f=1) never gives the seller a HIGHER book equity
     than mid (f=0) — the two-leg buy-back liability rises with f.
  3. NO LOOK-AHEAD: truncating the day universe at the last mark cannot change any earlier mark.
  4. Alpha-detector sanity (reused): a pure-beta series -> bootstrap alpha CI spans 0.

Data-backed tests skip if the warehouse is absent; the pure-logic tests always run.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

import short_strangle as ss
import s7_income_condor as s7

_HAS_WAREHOUSE = s7.WAREHOUSE.is_dir() and any(s7.WAREHOUSE.glob("2018*.parquet"))
CLEAN_DAY = dt.date(2018, 6, 1)


# --------------------------------------------------------------------------- #
# 1. UNCAPPED intrinsic settlement (pure logic, no data)
# --------------------------------------------------------------------------- #
def test_strangle_intrinsic_is_uncapped():
    s = ss.Strangle(entry_day=CLEAN_DAY, expiration=CLEAN_DAY, entry_dte=45,
                    short_put=2600.0, short_call=2800.0,
                    entry_short_put_delta=-0.16, entry_short_call_delta=0.16,
                    entry_credit=20.0, used_clean_delta=False)
    # Between the shorts: no intrinsic.
    assert ss._strangle_intrinsic(2700.0, s) == pytest.approx(0.0)
    # A deep crash: put-side loss is UNCAPPED = short_put - S (NOT capped at a wing width).
    assert ss._strangle_intrinsic(2000.0, s) == pytest.approx(600.0)
    assert ss._strangle_intrinsic(1000.0, s) == pytest.approx(1600.0)   # keeps growing
    # A deep melt-up: call-side loss uncapped = S - short_call.
    assert ss._strangle_intrinsic(3500.0, s) == pytest.approx(700.0)
    # Just below the short put: small partial loss.
    assert ss._strangle_intrinsic(2590.0, s) == pytest.approx(10.0)


def test_strangle_loss_dwarfs_a_condor_wing_on_a_crash():
    """The strangle put-side loss on a crash MUST exceed what a 25-pt-winged condor would cap
    at — proving the uncapped realism is actually engaged (not accidentally condor-like)."""
    s = ss.Strangle(entry_day=CLEAN_DAY, expiration=CLEAN_DAY, entry_dte=45,
                    short_put=2600.0, short_call=2800.0,
                    entry_short_put_delta=-0.16, entry_short_call_delta=0.16,
                    entry_credit=20.0, used_clean_delta=False)
    crash = 2000.0
    strangle_loss = ss._strangle_intrinsic(crash, s)
    condor_like_cap = 25.0  # a 25-pt winged condor caps the loss here
    assert strangle_loss > 20 * condor_like_cap   # 600 >> 25 — genuinely uncapped


# --------------------------------------------------------------------------- #
# 2. Cost-charged open credit (pure logic)
# --------------------------------------------------------------------------- #
def test_open_credit_falls_with_worse_fill():
    import pandas as pd
    exp = dt.date(2018, 7, 20)
    sub = pd.DataFrame([
        {"expiration": exp, "strike": 2600.0, "right": "PUT", "bid": 9.0, "ask": 11.0},
        {"expiration": exp, "strike": 2800.0, "right": "CALL", "bid": 8.0, "ask": 10.0},
    ])
    credits = [ss._strangle_open_credit(sub, exp, 2600.0, 2800.0, f)
               for f in (0.0, 0.25, 0.5, 1.0)]
    assert credits == sorted(credits, reverse=True)     # worse fill => less credit
    assert credits[0] == pytest.approx(10.0 + 9.0)       # both mids at f=0 (put mid 10, call mid 9)
    assert credits[-1] == pytest.approx(9.0 + 8.0)       # both bids at full cross


# --------------------------------------------------------------------------- #
# 3. Data-backed: build, daily-mark causality + cost-charged, delta-neutral
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="warehouse not present")
def test_strangle_builds_and_is_roughly_delta_neutral():
    day_df = s7.load_day(CLEAN_DAY)
    s = ss.build_strangle(day_df, CLEAN_DAY, 45, 0.16, 0.50)
    assert s is not None
    sub = day_df[day_df["expiration"] == s.expiration]
    spot = float(sub["underlying_price"].iloc[0])
    assert s.short_put < spot < s.short_call          # OTM both sides
    assert s.entry_credit > 0
    # net delta (put_delta + call_delta) should be near zero (put ~ -0.16, call ~ +0.16)
    assert abs(s.net_entry_delta) < 0.10, f"net delta {s.net_entry_delta} not ~neutral"


@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="warehouse not present")
def test_strangle_book_daily_mark_is_causal():
    all_days = [d for d in s7.available_days()
                if dt.date(2018, 6, 1) <= d <= dt.date(2018, 12, 31)]
    cache: dict = {}
    pm: dict = {}
    strangles = ss.run_strangle_book(45, 0.16, "managed", 0.50, all_days, cache,
                                     price_maps=pm)
    assert len(strangles) > 0
    book_full = ss.strangle_book_daily_marks(strangles, lambda d: cache.get(d), all_days,
                                             0.50, price_maps={})
    assert not book_full.empty
    cutoff = book_full.index[len(book_full) // 2]
    trunc_days = [d for d in all_days if d <= cutoff.date()]
    book_tr = ss.strangle_book_daily_marks(strangles, lambda d: cache.get(d), trunc_days,
                                           0.50, price_maps={})
    common = book_full.index.intersection(book_tr.index)
    assert len(common) > 5
    np.testing.assert_allclose(book_full.loc[common, "equity"].to_numpy(),
                               book_tr.loc[common, "equity"].to_numpy(), rtol=0, atol=1e-9)


@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="warehouse not present")
def test_strangle_book_mark_is_cost_charged():
    all_days = [d for d in s7.available_days()
                if dt.date(2018, 6, 1) <= d <= dt.date(2018, 12, 31)]
    cache: dict = {}
    eq = {}
    for f in (0.0, 1.0):
        strangles = ss.run_strangle_book(45, 0.16, "managed", f, all_days, cache,
                                         price_maps={})
        book = ss.strangle_book_daily_marks(strangles, lambda d: cache.get(d), all_days, f,
                                            price_maps={})
        eq[f] = float(book["equity"].iloc[-1])
    assert eq[1.0] <= eq[0.0] + 1e-6, "full-cross fill must not beat mid on book equity"


@pytest.mark.skipif(not _HAS_WAREHOUSE, reason="warehouse not present")
def test_no_lookahead_truncation_invariance_on_management():
    """Truncating the day universe at a strangle's exit day cannot change its close."""
    import copy
    all_days = s7.available_days()
    cache: dict = {}

    def loader(d):
        if d not in cache:
            cache[d] = s7.load_day(d)
        return cache[d]

    s0 = ss.build_strangle(loader(CLEAN_DAY), CLEAN_DAY, 45, 0.16, 0.50)
    assert s0 is not None
    s_full = ss.manage_strangle(copy.deepcopy(s0), loader, all_days, "managed", 0.50, 0.50)
    assert s_full.exit_day is not None
    truncated = [d for d in all_days if d <= s_full.exit_day]
    s_tr = ss.manage_strangle(copy.deepcopy(s0), loader, truncated, "managed", 0.50, 0.50)
    assert s_tr.exit_day == s_full.exit_day
    assert s_tr.exit_reason == s_full.exit_reason
    assert s_tr.exit_debit == pytest.approx(s_full.exit_debit)
    assert s_tr.pnl_dollars == pytest.approx(s_full.pnl_dollars)


# --------------------------------------------------------------------------- #
# 4. Alpha-detector sanity (reused from the CSP study) — pure-beta => alpha CI spans 0
# --------------------------------------------------------------------------- #
def test_alpha_detector_reports_no_edge_on_pure_beta():
    import csp_alpha_beta as cab
    rng = np.random.default_rng(4242)
    n = 1500
    r_spx = rng.normal(0.0004, 0.01, n)
    r_y = 0.05 * r_spx + rng.normal(0.0, 0.003, n)   # ~delta-neutral, ZERO alpha
    ci = cab.bootstrap_alpha_ci(r_y, r_spx, resamples=800, seed=99)
    assert ci["alpha_ann_lo"] < 0 < ci["alpha_ann_hi"], "CI must span 0 when alpha is truly 0"
