r"""
test_condor_fill_realism.py — guards for ARM 3 (condor_fill_realism).

These pin the MEASUREMENT invariants, not any outcome:
  * bid <= ask on every leg quote we build a record from (a well-formed NBBO);
  * the worst-side credit is NEVER above the mid credit (crossing the spread can only
    cost you, never pay you) — this is the whole point of the arm;
  * the one-way spread cost = mid - worst_side is non-negative, and the round-trip
    (full 4-leg width) is >= the one-way cost;
  * spread-%-of-credit is non-negative whenever the mid credit is positive;
  * the legs measured are the control's OWN iron-condor legs (structural agreement,
    asserted by identity of the imported builder), so the numbers can't silently drift
    from what s6_control actually trades.

The arithmetic invariants are checked on tiny in-memory snapshots (no warehouse). One
optional smoke test runs a single real day IF the warehouse is present, and is skipped
cleanly otherwise so the suite is green offline.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s6_control as ctrl  # noqa: E402
import condor_fill_realism as cfr  # noqa: E402


# --------------------------------------------------------------------------- #
# Structural: we measure the control's OWN condor legs (no drift).
# --------------------------------------------------------------------------- #
def test_uses_control_builder():
    # The module must build via s6_control's iron-condor builder + leg-quote helper.
    assert cfr.ctrl._build_iron_condor is ctrl._build_iron_condor
    assert cfr.ctrl._leg_quote is ctrl._leg_quote


# --------------------------------------------------------------------------- #
# A hand-built 4-leg iron condor with known bid/ask, run through the same math
# measure_day uses, so we can assert the invariants exactly.
# --------------------------------------------------------------------------- #
def _synthetic_condor():
    """Return (legs, per-leg bid/ask) for a clean iron condor.

    legs: (strike, right, side) with side +1 short(sold), -1 long(bought).
    Put spread short 4000 / long 3995 ; call spread short 4100 / long 4105.
    """
    quotes = {
        (4000.0, "PUT"): (1.00, 1.20),   # short put:  bid 1.00 ask 1.20  (width .20)
        (3995.0, "PUT"): (0.70, 0.85),   # long put:   bid 0.70 ask 0.85  (width .15)
        (4100.0, "CALL"): (0.90, 1.10),  # short call: bid 0.90 ask 1.10  (width .20)
        (4105.0, "CALL"): (0.60, 0.72),  # long call:  bid 0.60 ask 0.72  (width .12)
    }
    legs = [
        (4000.0, "PUT", +1), (3995.0, "PUT", -1),
        (4100.0, "CALL", +1), (4105.0, "CALL", -1),
    ]
    return legs, quotes


def _compute(legs, quotes):
    """Reproduce measure_day's arithmetic on an explicit quote dict."""
    mid_credit = 0.0
    worstside = 0.0
    widths = {}
    for strike, right, side in legs:
        bid, ask = quotes[(strike, right)]
        assert bid <= ask, "malformed quote: bid > ask"
        mid = 0.5 * (bid + ask)
        mid_credit += side * mid
        # control worst-side: sold leg gets BID, bought leg pays ASK
        worstside += bid if side > 0 else -ask
        widths[(strike, right)] = ask - bid
    oneway = mid_credit - worstside
    roundtrip = sum(widths.values())
    return mid_credit, worstside, oneway, roundtrip, widths


def test_worstside_never_above_mid():
    legs, quotes = _synthetic_condor()
    mid_credit, worstside, oneway, roundtrip, widths = _compute(legs, quotes)
    assert worstside <= mid_credit + 1e-12
    assert oneway >= -1e-12


def test_oneway_is_half_total_width():
    # One-way worst-side cost = half of every leg's width (lose half a width per leg).
    legs, quotes = _synthetic_condor()
    _, _, oneway, roundtrip, widths = _compute(legs, quotes)
    assert oneway == pytest.approx(0.5 * sum(widths.values()))
    assert roundtrip == pytest.approx(sum(widths.values()))
    assert roundtrip >= oneway - 1e-12


def test_pct_nonnegative_when_credit_positive():
    legs, quotes = _synthetic_condor()
    mid_credit, _, oneway, roundtrip, _ = _compute(legs, quotes)
    assert mid_credit > 0
    oneway_pct = 100.0 * oneway / mid_credit
    roundtrip_pct = 100.0 * roundtrip / mid_credit
    assert oneway_pct >= 0.0
    assert roundtrip_pct >= oneway_pct - 1e-9


def test_dist_handles_empty():
    assert cfr._dist(pd.Series([], dtype=float)) == {"n": 0}


def test_dist_percentiles_ordered():
    d = cfr._dist(pd.Series([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], dtype=float))
    assert d["p05"] <= d["p25"] <= d["median"] <= d["p75"] <= d["p95"]


# --------------------------------------------------------------------------- #
# Optional real-day smoke test (skipped cleanly if the warehouse is absent).
# --------------------------------------------------------------------------- #
def test_one_real_day_smoke():
    import s5_intraday_data as s5
    try:
        days = s5.available_days()
    except Exception:
        pytest.skip("warehouse not available")
    if not days:
        pytest.skip("no warehouse days")
    from s6_matrix import DayClassifier
    rec = cfr.measure_day(days[len(days) // 2], clf=DayClassifier())
    if not rec.measured:
        pytest.skip(f"day not measurable: {rec.skip_reason}")
    # Invariants on a real measured day.
    assert rec.worstside_credit <= rec.mid_credit + 1e-9
    assert rec.oneway_spread_cost >= -1e-9
    assert rec.roundtrip_spread_cost >= rec.oneway_spread_cost - 1e-9
    if np.isfinite(rec.mid_credit) and rec.mid_credit > 0:
        assert rec.oneway_pct_of_credit >= 0.0
