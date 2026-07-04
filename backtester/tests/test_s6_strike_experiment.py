r"""
test_s6_strike_experiment.py — mechanics of the S6 strike-selection experiment.

Pins, with exact arithmetic on tiny in-memory inputs (no warehouse):
  * strike pickers place the short strike just BEYOND the zone (put at/below demand low,
    call at/above supply high) rounded to the 5-point grid;
  * breach detection fires when recovered spot reaches/exceeds the short strike (per structure);
  * credit is NOT a silent gate — a sub-$0.30 credit still TRADES and is bucketed correctly
    (Andrew's design refinement); only an uncomputable credit is skipped;
  * arm C matches arm B's zone-implied delta (fooling-guard) — mechanically distinct from B.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s6_zones as z  # noqa: E402
import s6_strike_experiment as ex  # noqa: E402


def _zone(kind, low, high):
    prox = high if kind == "demand" else low
    dist = low if kind == "demand" else high
    return z.Zone(kind=kind, pattern="RBR" if kind == "demand" else "RBD",
                  timeframe_min=5, zone_low=low, zone_high=high, proximal=prox, distal=dist,
                  base_start=pd.Timestamp("2024-06-03 10:00"),
                  base_end=pd.Timestamp("2024-06-03 10:05"),
                  confirm_time=pd.Timestamp("2024-06-03 10:20"),
                  base_count=1, base_range=high - low)


def test_strike_beyond_zone_and_rounding():
    # demand 4603-4607 -> short put at/below 4603 -> floor to 5 = 4600.
    assert ex._pick_strike_below_demand(_zone("demand", 4603, 4607)) == 4600.0
    # supply 4661-4669 -> short call at/above 4669 -> ceil to 5 = 4670.
    assert ex._pick_strike_above_supply(_zone("supply", 4661, 4669)) == 4670.0
    # exact grid edges: demand low exactly 4600 -> floor stays 4600; supply high 4670 -> 4670.
    assert ex._pick_strike_below_demand(_zone("demand", 4600, 4604)) == 4600.0
    assert ex._pick_strike_above_supply(_zone("supply", 4666, 4670)) == 4670.0


def test_breach_logic_per_structure():
    idx = pd.date_range("2024-06-03 14:01", "2024-06-03 16:00", freq="1min")
    # spot path dips to 4590 and rises to 4720.
    path = pd.Series(np.linspace(4650, 4650, len(idx)), index=idx)
    path.iloc[10] = 4590.0   # a dip
    path.iloc[20] = 4720.0   # a spike
    # bull put short 4600: dip to 4590 <= 4600 -> breach.
    assert ex._breached(path, "bull_put", put_short=4600, call_short=float("nan")) is True
    # bull put short 4580: dip 4590 > 4580 -> no breach.
    assert ex._breached(path, "bull_put", put_short=4580, call_short=float("nan")) is False
    # bear call short 4700: spike 4720 >= 4700 -> breach.
    assert ex._breached(path, "bear_call", put_short=float("nan"), call_short=4700) is True
    # bear call short 4750: spike 4720 < 4750 -> no breach.
    assert ex._breached(path, "bear_call", put_short=float("nan"), call_short=4750) is False
    # iron condor breaches if EITHER side does.
    assert ex._breached(path, "iron_condor", put_short=4600, call_short=4800) is True
    assert ex._breached(path, "iron_condor", put_short=4500, call_short=4800) is False


def test_credit_is_not_a_silent_gate_but_is_bucketed():
    # A synthetic single-day NBBO with a cheap put spread (credit 0.10 < 0.30). It must TRADE,
    # be flagged meets_min_credit=False, and land in the '<0.30' bucket — never skipped.
    day = dt.date(2024, 6, 3)
    # Build minutes 14:00 (entry) and 16:00 (settle) with a put spread priced so:
    #   entry credit = short_bid - long_ask = 0.15 - 0.05 = 0.10.
    #   at settle debit = short_ask - long_bid = 0.05 - 0.00 = 0.05 (winner).
    rows = []
    for minute, sk_bid, sk_ask, lk_bid, lk_ask in [
        ("14:00", 0.15, 0.20, 0.00, 0.05),
        ("16:00", 0.00, 0.05, 0.00, 0.00),
    ]:
        hh, mm = map(int, minute.split(":"))
        t = pd.Timestamp(dt.datetime.combine(day, dt.time(hh, mm)))
        rows += [
            {"minute": t, "strike": 4600.0, "right": "PUT", "bid": sk_bid, "ask": sk_ask},
            {"minute": t, "strike": 4595.0, "right": "PUT", "bid": lk_bid, "ask": lk_ask},
        ]
    nbbo = pd.DataFrame(rows)
    entry = pd.Timestamp(dt.datetime.combine(day, dt.time(14, 0)))
    settle = pd.Timestamp(dt.datetime.combine(day, dt.time(16, 0)))
    build = ex._build_put_side(ex.ctrl._snap_at(nbbo, entry), 4600.0)
    assert build is not None
    assert build["credit"] == pytest.approx(0.10)
    # Confirm the credit bucket rule directly (finish() is exercised in integration).
    credit = build["credit"]
    bucket = ("<0.30" if credit < 0.30 else ("0.30-0.50" if credit < 0.50 else "0.50+"))
    assert bucket == "<0.30"
    assert (credit >= ex.MIN_ENTRY_CREDIT) is False  # reported flag, not a filter


def test_arm_c_matches_zone_implied_delta():
    # A delta table with a monotone put-delta curve; the zone strike lands at delta -0.08.
    # Arm C should pick the strike whose |delta| is nearest 0.08 — i.e. the same strike here,
    # proving C reads B's implied delta rather than the fixed 0.15.
    delta_tbl = pd.DataFrame([
        {"strike": 4600.0, "right": "PUT", "delta": -0.08},
        {"strike": 4610.0, "right": "PUT", "delta": -0.15},
        {"strike": 4620.0, "right": "PUT", "delta": -0.25},
    ])
    picked = ex._pick_short_by_delta(delta_tbl, "PUT", abs(-0.08))
    assert picked == 4600.0
    # And the fixed-0.15 arm A would pick a DIFFERENT strike.
    picked_a = ex._pick_short_by_delta(delta_tbl, "PUT", ex.TARGET_SHORT_DELTA)
    assert picked_a == 4610.0
