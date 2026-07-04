r"""
test_s5fh_selection.py -- contract SELECTION helpers of s5_financing_harness.

Pins:
  * nearest_expiration picks the DTE-nearest available expiry (tie -> longer);
  * select_by_delta picks the nearest STORED |delta| strike on the right side;
  * select_by_moneyness picks the strike nearest underlying*(1+m);
  * strike_offset legs key off a reference leg's chosen strike (defined-risk wings);
  * open_position resolves a declared Structure into concrete honest-filled legs and the
    entry credit / commission are computed from the honest fills.

Synthetic in-memory chain -- no warehouse read. Chain schema matches the loader's output.
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
    """A two-expiration synthetic SPXW chain with monotone deltas and simple quotes.

    The grid is WIDE ENOUGH BELOW spot that a 0.15-delta short put lands well above the
    lowest strike, so its defined-risk wing (short - wing) stays ON the grid and the entry
    is not dropped. The delta ramp is centered so |delta|=0.15 sits comfortably mid-grid.
    """
    rows = []
    exps = {dt.date(2019, 6, 5): 2, dt.date(2019, 7, 18): 45}  # ~2 DTE and ~45 DTE
    for exp, _dte in exps.items():
        # widened DOWN to 2300 so a low-delta put short + wing both stay on-grid.
        for k in range(2300, 3101, 25):
            # monotone: put |delta| RISES with strike (deep-OTM low strikes ~0, ATM ~0.5).
            # Anchored so |delta|=0 near k=2300 and =0.5 at spot -> |delta|=0.15 lands at
            # k ~ 2480, well above the 2300 floor with room for a 50-wide wing below it.
            put_delta = -min(max((k - 2300) / ((und - 2300) / 0.5), 0.01), 0.99)
            call_delta = min(max((3100 - k) / ((3100 - und) / 0.5), 0.01), 0.99)
            for right, d in (("PUT", put_delta), ("CALL", call_delta)):
                # Price monotone in |delta| so the nearer-the-money leg (higher |delta|) is
                # worth MORE -- a real credit-spread relationship. A put credit spread (sell
                # higher strike, buy lower wing) is then a genuine net CREDIT that clears the
                # commission floor, and a call spread the mirror.
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


def test_nearest_expiration_picks_closest_dte():
    ch = _synthetic_chain()
    assert h.nearest_expiration(ch, 45) == dt.date(2019, 7, 18)
    assert h.nearest_expiration(ch, 2) == dt.date(2019, 6, 5)
    assert h.nearest_expiration(ch, 0) == dt.date(2019, 6, 5)  # nearest to 0 is the 2-DTE


def test_select_by_delta_nearest_stored_greek():
    ch = _synthetic_chain()
    exp = dt.date(2019, 7, 18)
    k = h.select_by_delta(ch, exp, "PUT", 0.15)
    row = h.contract_row(ch, exp, k, "PUT")
    # nearest |delta| to 0.15 among the discrete strikes
    all_puts = ch[(ch.expiration == exp) & (ch.right == "PUT")]
    best = all_puts.iloc[(all_puts.delta.abs() - 0.15).abs().argmin()]
    assert k == pytest.approx(best.strike)
    assert abs(abs(row.delta) - 0.15) <= abs(abs(all_puts.delta) - 0.15).min() + 1e-9


def test_select_by_moneyness():
    ch = _synthetic_chain(und=2900.0)
    exp = dt.date(2019, 7, 18)
    k = h.select_by_moneyness(ch, exp, "PUT", -0.05)   # ~5% OTM put => ~2755
    assert k == pytest.approx(2750.0, abs=25.0)


def test_strike_offset_wing_keys_off_reference_leg():
    ch = _synthetic_chain()
    struct = h.put_credit_spread(dte=45, short_delta=0.15, wing=50.0)
    pos = h.open_position(struct, dt.date(2019, 6, 3), ch)
    short = [l for l in pos.legs if l.action == "sell"][0]
    wing = [l for l in pos.legs if l.action == "buy"][0]
    assert wing.strike == pytest.approx(short.strike - 50.0)
    assert wing.expiration == short.expiration


def test_open_position_entry_credit_from_honest_fills():
    ch = _synthetic_chain()
    struct = h.put_credit_spread(dte=45, short_delta=0.15, wing=50.0)
    pos = h.open_position(struct, dt.date(2019, 6, 3), ch)
    short = [l for l in pos.legs if l.action == "sell"][0]
    wing = [l for l in pos.legs if l.action == "buy"][0]
    # credit = short bid - wing ask, times multiplier
    expect = (short.entry_fill - wing.entry_fill) * h.CONTRACT_MULTIPLIER
    assert pos.entry_credit == pytest.approx(expect)
    assert pos.entry_commission == pytest.approx(2 * 0.65)
    # short leg sold at bid, wing bought at ask
    srow = h.contract_row(ch, short.expiration, short.strike, "PUT")
    wrow = h.contract_row(ch, wing.expiration, wing.strike, "PUT")
    assert short.entry_fill == pytest.approx(srow.bid)
    assert wing.entry_fill == pytest.approx(wrow.ask)
