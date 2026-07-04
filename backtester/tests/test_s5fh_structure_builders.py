r"""
test_s5fh_structure_builders.py -- the NEW S5 financing STRUCTURE builders on
s5_financing_harness (Phase-2b structure families):

  * put_write            : ONE naked short put, no long wing;
  * iron_condor_call_param : put spread + a PARAMETERIZABLE call side (neutral vs income arm);
  * put_calendar         : short front-month put + long back-month put (per-leg DTE override);
  * sell_against_owned_tail : short nearer put + the already-owned deep tail as the long leg,
                             recording the incremental defined risk (short_strike - tail_strike).

Each test pins the LEGS the builder produces (rights/actions/selection modes/DTE overrides)
and runs ONE cell on a synthetic in-memory chain to prove it resolves + fills honestly. No
warehouse read -- the chain schema matches the loader's output.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s5_financing_harness as h  # noqa: E402


def _synthetic_chain(trade_date=dt.date(2019, 6, 3), und=2900.0):
    """A THREE-expiration synthetic SPXW chain (a short front, a ~45d, and a ~90d back month)
    so calendars/diagonals (per-leg DTE overrides) have distinct expiries to pick. Monotone
    deltas + monotone prices so credit structures clear the commission floor honestly. Grid
    widened DOWN to 2000 so a deep ~20%-OTM tail put (~2320) stays on-grid."""
    rows = []
    # front ~2d, ~45d (short/front tenor), ~63d (owned-tail tenor), ~90d (calendar back month)
    exps = {dt.date(2019, 6, 5): 2, dt.date(2019, 7, 18): 45,
            dt.date(2019, 8, 5): 63, dt.date(2019, 9, 1): 90}
    # Realistic delta ramp: |delta|=0.5 at spot and falls to LOW deltas only a few % OTM (a
    # steep, near-money ramp), so a 0.15-delta short put sits NEAR spot (~2700s) -- well ABOVE
    # a deep ~20%-OTM tail (~2320), mirroring the real chain. width ~ 6% of spot per 0.5 delta.
    half_width = und * 0.06     # strike distance from spot to reach |delta| ~ 0
    for exp in exps:
        for k in range(2000, 3201, 25):
            put_delta = -min(max((k - (und - half_width)) / (half_width / 0.5), 0.01), 0.99)
            call_delta = min(max(((und + half_width) - k) / (half_width / 0.5), 0.01), 0.99)
            for right, d in (("PUT", put_delta), ("CALL", call_delta)):
                mid = max(1.0 + abs(d) * 20.0, 0.5)
                rows.append({
                    "date": trade_date, "symbol": "SPXW", "expiration": exp,
                    "strike": float(k), "right": right,
                    "close": mid, "bid": mid - 0.25, "ask": mid + 0.25,
                    "bid_size": 10, "ask_size": 10, "volume": 100, "open_interest": 1000,
                    "implied_vol": 0.15, "delta": d, "gamma": 0.001, "theta": -0.1,
                    "vega": 1.0, "underlying_price": und,
                })
    df = pd.DataFrame(rows)
    df["dte"] = df["expiration"].apply(lambda e: (e - trade_date).days)
    df["two_sided"] = (df["bid"] > 0) & (df["ask"] > 0)
    return df


ENTRY = dt.date(2019, 6, 3)


# --------------------------------------------------------------------------- #
# 1. put_write -- a single naked short put.
# --------------------------------------------------------------------------- #
def test_put_write_single_short_put_no_wing():
    struct = h.put_write(dte=45, short_delta=0.15)
    assert len(struct.legs) == 1
    leg = struct.legs[0]
    assert leg.right == "PUT" and leg.action == "sell"
    assert leg.target_delta == pytest.approx(0.15)
    # resolves + fills: naked short put, net credit = short bid * mult, one entry commission.
    ch = _synthetic_chain()
    pos = h.open_position(struct, ENTRY, ch)
    assert len(pos.legs) == 1
    short = pos.legs[0]
    srow = h.contract_row(ch, short.expiration, short.strike, "PUT")
    assert short.entry_fill == pytest.approx(srow.bid)         # sold at the bid
    assert pos.entry_credit == pytest.approx(srow.bid * h.CONTRACT_MULTIPLIER)
    assert pos.entry_commission == pytest.approx(0.65)         # one leg
    assert pos.entry_credit > pos.entry_commission             # clears the min-credit floor


# --------------------------------------------------------------------------- #
# 2. iron_condor_call_param -- put spread + parameterizable call side.
# --------------------------------------------------------------------------- #
def test_iron_condor_neutral_arm_uses_far_low_delta_call():
    # default call_delta = short_delta * 0.5 -> a FARTHER (lower-delta) short call.
    struct = h.iron_condor_call_param(dte=45, short_delta=0.20, wing=50.0)
    assert len(struct.legs) == 4
    sp, pw, sc, cw = struct.legs
    assert (sp.right, sp.action) == ("PUT", "sell")
    assert (pw.right, pw.action) == ("PUT", "buy") and pw.strike_offset == pytest.approx(-50.0)
    assert (sc.right, sc.action) == ("CALL", "sell")
    assert sc.target_delta == pytest.approx(0.10)   # 0.20 * 0.5 (neutral, far call)
    assert (cw.right, cw.action) == ("CALL", "buy") and cw.strike_offset == pytest.approx(+50.0)

    ch = _synthetic_chain()
    pos = h.open_position(struct, ENTRY, ch)
    short_call = [l for l in pos.legs if l.right == "CALL" and l.action == "sell"][0]
    call_wing = [l for l in pos.legs if l.right == "CALL" and l.action == "buy"][0]
    assert call_wing.strike == pytest.approx(short_call.strike + 50.0)


def test_iron_condor_income_arm_uses_nearer_higher_delta_call():
    # explicit call_delta == put short_delta -> a NEARER, higher-premium short call.
    neutral = h.iron_condor_call_param(dte=45, short_delta=0.20, wing=50.0)
    income = h.iron_condor_call_param(dte=45, short_delta=0.20, wing=50.0, call_delta=0.20)
    ch = _synthetic_chain()
    p_neu = h.open_position(neutral, ENTRY, ch)
    p_inc = h.open_position(income, ENTRY, ch)
    sc_neu = [l for l in p_neu.legs if l.right == "CALL" and l.action == "sell"][0]
    sc_inc = [l for l in p_inc.legs if l.right == "CALL" and l.action == "sell"][0]
    # income arm's short call is NEARER the money (lower strike) than the neutral arm's.
    assert sc_inc.strike < sc_neu.strike
    # both are genuine 4-leg condors
    assert len(p_inc.legs) == 4


# --------------------------------------------------------------------------- #
# 3. put_calendar -- short front + long back-month put (per-leg DTE override).
# --------------------------------------------------------------------------- #
def test_put_calendar_short_front_long_back_distinct_expiries():
    struct = h.put_calendar(dte=45, short_delta=0.15, back_dte_mult=2.0)
    assert len(struct.legs) == 2
    front, back = struct.legs
    assert (front.right, front.action) == ("PUT", "sell")
    assert front.dte is None                       # uses the structure DTE (front tenor)
    assert (back.right, back.action) == ("PUT", "buy")
    assert back.dte == 90                          # round(45 * 2.0) -- per-leg DTE override
    assert back.strike_offset == pytest.approx(0.0) and back.ref_leg == 0

    ch = _synthetic_chain()
    pos = h.open_position(struct, ENTRY, ch)
    short = [l for l in pos.legs if l.action == "sell"][0]
    long_ = [l for l in pos.legs if l.action == "buy"][0]
    # the long back-month is a LATER expiration than the short front-month.
    assert long_.expiration > short.expiration
    # net DEBIT structure (long back-month costs more than the front short brings in) ->
    # entry_credit is NEGATIVE and legitimately bypasses the min-credit floor.
    assert pos.entry_credit < 0


def test_put_calendar_diagonal_strike_offset_shifts_long_strike():
    struct = h.put_calendar(dte=45, short_delta=0.15, back_dte_mult=2.0, strike_offset=-25.0)
    ch = _synthetic_chain()
    pos = h.open_position(struct, ENTRY, ch)
    short = [l for l in pos.legs if l.action == "sell"][0]
    long_ = [l for l in pos.legs if l.action == "buy"][0]
    assert long_.strike == pytest.approx(short.strike - 25.0)   # diagonal offset


# --------------------------------------------------------------------------- #
# 4. sell_against_owned_tail -- short nearer put + owned deep tail as long leg.
# --------------------------------------------------------------------------- #
def test_sell_against_owned_tail_legs_and_defined_risk():
    struct = h.sell_against_owned_tail(dte=45, short_delta=0.15,
                                       tail_moneyness=-0.20, tail_dte=63)
    assert len(struct.legs) == 2
    short, tail = struct.legs
    assert (short.right, short.action) == ("PUT", "sell")
    assert short.dte is None                       # short uses the structure DTE
    assert (tail.right, tail.action) == ("PUT", "buy")
    assert tail.target_moneyness == pytest.approx(-0.20)   # ~20% OTM owned tail
    assert tail.dte == 63                                   # tail's own DTE

    ch = _synthetic_chain()
    pos = h.open_position(struct, ENTRY, ch)
    sp = [l for l in pos.legs if l.action == "sell"][0]
    tl = [l for l in pos.legs if l.action == "buy"][0]
    # tail is deep OTM (well below the short strike) and at a later expiry than the short.
    assert tl.strike < sp.strike
    assert tl.expiration > sp.expiration
    # income modeling: short premium dominates -> net CREDIT (clears the floor).
    assert pos.entry_credit > pos.entry_commission
    # incremental defined risk = short_strike - tail_strike (positive, bounded loss).
    defined_risk = sp.strike - tl.strike
    assert defined_risk > 0
