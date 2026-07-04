r"""test_s5fh_per_leg_expiry.py -- PER-LEG-EXPIRY settlement in the s5_financing_harness walk
loop.

The harness must settle EACH leg on ITS OWN expiration date. For a multi-expiry structure
(put calendar/diagonal, sell-against-owned-tail) the earlier-expiring FRONT leg cash-settles
at intrinsic against ITS expiry-day underlying, and the position keeps being managed with the
remaining leg(s) until the LAST expiry. Before this fix the walk settled ALL legs on the
single `last_expiration` date, so a front leg was wrongly valued at the LATER leg's expiry.

These tests pin, on a fully synthetic in-memory multi-expiry chain (NO warehouse read):
  * the front short leg settles on ITS OWN date (its cashflow books at the front expiry,
    against the front-expiry-day underlying, NOT the back-expiry underlying);
  * the back long leg settles on ITS OWN (later) date;
  * the terminal position net_pnl == entry_net + front_leg_settle + back_leg_settle - comms,
    i.e. the sum of the two legs each settled at their own expiry;
  * a SINGLE-expiry structure (both legs same expiry) is unaffected: it settles exactly once
    on the shared expiry (regression guard, same code path).
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s5_financing_harness as h  # noqa: E402


# --------------------------------------------------------------------------- #
# A hand-built multi-expiry chain with KNOWN per-day underlyings, so we can compute the
# EXACT intrinsic each leg settles at on its own expiry and check the booking date.
# --------------------------------------------------------------------------- #
ENTRY = dt.date(2022, 1, 3)
FRONT_EXP = dt.date(2022, 1, 21)   # front short put expiry
BACK_EXP = dt.date(2022, 2, 18)    # back long put expiry (later)

# underlying by trading day; chosen so BOTH puts finish ITM by controlled amounts:
#   at FRONT_EXP underlying = 4400 -> a 4500 put is 100 ITM
#   at BACK_EXP  underlying = 4300 -> a 4500 put is 200 ITM
UND = {
    ENTRY: 4700.0,
    dt.date(2022, 1, 10): 4600.0,
    dt.date(2022, 1, 14): 4550.0,
    FRONT_EXP: 4400.0,
    dt.date(2022, 1, 28): 4380.0,
    dt.date(2022, 2, 4): 4350.0,
    BACK_EXP: 4300.0,
}
DAYS = sorted(UND)


def _chain_for(d: dt.date) -> pd.DataFrame:
    """Both expirations present every day; monotone deltas + monotone two-sided prices so a
    0.15-delta short put resolves and every leg is fillable / markable on every day."""
    und = UND[d]
    half = 4700.0 * 0.06
    rows = []
    for exp in (FRONT_EXP, BACK_EXP):
        for k in range(3800, 5001, 25):
            put_delta = -min(max((k - (und - half)) / (half / 0.5), 0.01), 0.99)
            call_delta = min(max(((und + half) - k) / (half / 0.5), 0.01), 0.99)
            for right, dd in (("PUT", put_delta), ("CALL", call_delta)):
                mid = max(1.0 + abs(dd) * 20.0, 0.5)
                rows.append({
                    "date": d, "symbol": "SPXW", "expiration": exp,
                    "strike": float(k), "right": right,
                    "close": mid, "bid": mid - 0.25, "ask": mid + 0.25,
                    "bid_size": 10, "ask_size": 10, "volume": 100, "open_interest": 1000,
                    "implied_vol": 0.15, "delta": dd, "gamma": 0.001, "theta": -0.1,
                    "vega": 1.0, "underlying_price": und,
                })
    df = pd.DataFrame(rows)
    df["dte"] = df["expiration"].apply(lambda e: (e - d).days)
    df["two_sided"] = (df["bid"] > 0) & (df["ask"] > 0)
    return df


def _two_expiry_structure() -> h.Structure:
    """SHORT front put + LONG back put, both selected at the same 0.15-delta strike, but at
    DIFFERENT expiries (per-leg DTE override on the back leg) -> a genuine 2-expiry calendar.
    Held to expiry (no early management) so both legs settle at intrinsic on their own dates."""
    front_dte = (FRONT_EXP - ENTRY).days
    back_dte = (BACK_EXP - ENTRY).days
    legs = [
        h.Leg(right="PUT", action="sell", target_delta=0.15),                 # front short
        h.Leg(right="PUT", action="buy", strike_offset=0.0, ref_leg=0, dte=back_dte),  # back long
    ]
    return h.Structure(name="test_2exp_calendar", legs=legs, dte=front_dte,
                       management=h.Management(mode="hold"))


def test_front_leg_settles_on_its_own_expiry_not_the_back_expiry():
    """The position resolves at the BACK expiry, but the front short leg must have cash-settled
    at the FRONT expiry's underlying (4400 -> 100 ITM), NOT the back expiry's (4300 -> 200)."""
    struct = _two_expiry_structure()
    pos = h.open_position(struct, ENTRY, _chain_for(ENTRY))
    short = [l for l in pos.legs if l.action == "sell"][0]
    long_ = [l for l in pos.legs if l.action == "buy"][0]
    assert short.expiration == FRONT_EXP
    assert long_.expiration == BACK_EXP
    assert short.strike == pytest.approx(long_.strike)   # pure calendar (same strike)

    res = h.run_trade(struct, ENTRY, DAYS, chain_loader=_chain_for)
    assert res is not None
    assert res.exit_reason == "settle"
    assert res.exit_date == BACK_EXP                      # terminal day = LAST expiry

    # Analytic expectation: each leg settles at intrinsic on ITS OWN expiry-day underlying.
    K = short.strike
    front_intrinsic = max(K - UND[FRONT_EXP], 0.0)        # short put ITM at front expiry
    back_intrinsic = max(K - UND[BACK_EXP], 0.0)          # long put ITM at back expiry
    # short ITM PAYS intrinsic; long ITM RECEIVES intrinsic.
    front_settle_cash = -front_intrinsic * h.CONTRACT_MULTIPLIER * short.n_contracts
    back_settle_cash = +back_intrinsic * h.CONTRACT_MULTIPLIER * long_.n_contracts
    # settle commissions: one per ITM leg (both ITM here)
    settle_comm = h.COMMISSION_PER_LEG * 2
    expect_net = (pos.entry_credit - pos.entry_commission) \
        + front_settle_cash + back_settle_cash - settle_comm

    assert res.net_pnl == pytest.approx(expect_net)
    assert res.total_commission == pytest.approx(pos.entry_commission + settle_comm)

    # SANITY: if the front had been (wrongly) settled at the BACK underlying (4300 -> 200 ITM
    # short), the short-leg cash would have been -200*100 instead of -100*100, a $10,000
    # difference. Prove the new number is NOT that broken value.
    wrong_front_cash = -max(K - UND[BACK_EXP], 0.0) * h.CONTRACT_MULTIPLIER
    wrong_net = (pos.entry_credit - pos.entry_commission) \
        + wrong_front_cash + back_settle_cash - settle_comm
    assert res.net_pnl != pytest.approx(wrong_net)


def test_each_leg_cashflow_books_on_the_correct_date_and_pnl_is_their_sum():
    """The final mark books on the terminal (back) expiry, and net_pnl equals the SUM of the
    two legs each settled at their own expiry -- verified by settling each leg independently
    with the harness's own primitive at its own expiry-day underlying."""
    struct = _two_expiry_structure()
    pos = h.open_position(struct, ENTRY, _chain_for(ENTRY))
    short = [l for l in pos.legs if l.action == "sell"][0]
    long_ = [l for l in pos.legs if l.action == "buy"][0]

    res = h.run_trade(struct, ENTRY, DAYS, chain_loader=_chain_for)

    # settle each leg with the PUBLIC primitive at its own expiry-day underlying
    front_cash, front_comm = h.settle_legs([short], UND[FRONT_EXP])
    back_cash, back_comm = h.settle_legs([long_], UND[BACK_EXP])
    expect_net = (pos.entry_credit - pos.entry_commission) \
        + front_cash + back_cash - front_comm - back_comm

    assert res.net_pnl == pytest.approx(expect_net)
    # the terminal mark is booked on the LAST expiry date
    assert res.marks[-1][0] == BACK_EXP
    assert res.marks[-1][1] == pytest.approx(res.net_pnl)


def test_single_expiry_two_leg_settles_once_on_shared_expiry_regression():
    """Regression guard: a co-expiry 2-leg put spread (both legs same expiry) settles exactly
    once on the shared expiry -- per-leg settlement must reduce to the single-settlement path.
    net_pnl == entry_net + both-legs-settled-together at the shared expiry underlying."""
    front_dte = (FRONT_EXP - ENTRY).days
    legs = [
        h.Leg(right="PUT", action="sell", target_delta=0.15),
        h.Leg(right="PUT", action="buy", strike_offset=-50.0, ref_leg=0),  # same expiry wing
    ]
    struct = h.Structure(name="test_coexpiry_spread", legs=legs, dte=front_dte,
                         management=h.Management(mode="hold"))
    pos = h.open_position(struct, ENTRY, _chain_for(ENTRY))
    assert len({l.expiration for l in pos.legs}) == 1   # co-expiry

    res = h.run_trade(struct, ENTRY, DAYS, chain_loader=_chain_for)
    assert res is not None and res.exit_reason == "settle"
    assert res.exit_date == FRONT_EXP                    # the single shared expiry

    settle_cash, settle_comm = h.settle_position(pos, UND[FRONT_EXP])
    expect_net = (pos.entry_credit - pos.entry_commission) + settle_cash - settle_comm
    assert res.net_pnl == pytest.approx(expect_net)
