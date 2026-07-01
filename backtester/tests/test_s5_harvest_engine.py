r"""
test_s5_harvest_engine.py -- unit tests for the S5 harvest engine mechanics.

Pins the MECHANICS, not any strategy outcome:
  * honest condor fills: put credit = short_bid - long_ask; call credit likewise;
  * settlement intrinsic is capped at the 5-wide wing (defined risk), per side;
  * commission: 4 entry legs always; +2 per ITM side that cash-settles;
  * hand-checked net P&L in dollars;
  * settlement spot recovery falls back to the last quoted minute before 16:00.

Synthetic in-memory grids -- no warehouse needed.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s5_harvest_engine as h  # noqa: E402


def test_settlement_intrinsic_capped_both_sides():
    b = {"p_short_k": 4000.0, "c_short_k": 4100.0}
    # spot between shorts -> both OTM -> 0 intrinsic
    assert h._settlement_intrinsic(4050.0, b) == 0.0
    # deep below put short by more than the wing -> capped at width
    assert h._settlement_intrinsic(3000.0, b) == h.SPREAD_WIDTH
    # partial put breach: short 4000, spot 3998 -> 2 pts (< width 5)
    assert abs(h._settlement_intrinsic(3998.0, b) - 2.0) < 1e-9
    # deep above call short -> capped at width
    assert h._settlement_intrinsic(5000.0, b) == h.SPREAD_WIDTH
    # partial call breach: short 4100, spot 4103 -> 3 pts
    assert abs(h._settlement_intrinsic(4103.0, b) - 3.0) < 1e-9


def test_honest_condor_fills():
    day = dt.date(2024, 1, 2)
    minute = pd.Timestamp(dt.datetime.combine(day, h.ENTRY_TIME))
    # build a snap with put short/long and call short/long
    rows = [
        {"strike": 4000, "right": "PUT", "bid": 1.00, "ask": 1.10},   # short put
        {"strike": 3995, "right": "PUT", "bid": 0.40, "ask": 0.50},   # long put wing
        {"strike": 4100, "right": "CALL", "bid": 0.90, "ask": 1.00},  # short call
        {"strike": 4105, "right": "CALL", "bid": 0.30, "ask": 0.40},  # long call wing
    ]
    snap = pd.DataFrame([{**r, "strike": float(r["strike"])} for r in rows])
    # delta table: put 4000 at -0.15, call 4100 at +0.15 (nearest to 0.15 target)
    dtbl = pd.DataFrame({
        "strike": [4000.0, 3995.0, 4100.0, 4105.0],
        "right": ["PUT", "PUT", "CALL", "CALL"],
        "delta": [-0.15, -0.10, 0.15, 0.10],
    })
    b = h._build_condor(snap, dtbl, h.SHORT_DELTA)
    assert b is not None
    # put credit = short_bid(1.00) - long_ask(0.50) = 0.50
    assert abs(b["p_credit"] - 0.50) < 1e-9
    # call credit = 0.90 - 0.40 = 0.50
    assert abs(b["c_credit"] - 0.50) < 1e-9
    assert abs(b["entry_credit"] - 1.00) < 1e-9
    assert b["p_short_k"] == 4000.0 and b["c_short_k"] == 4100.0
    assert b["p_long_k"] == 3995.0 and b["c_long_k"] == 4105.0


def test_commission_logic_and_pnl_no_breach():
    """No breach: 4 entry legs only, full credit kept, hand-checked P&L."""
    b = {"p_short_k": 4000.0, "c_short_k": 4100.0, "p_long_k": 3995.0, "c_long_k": 4105.0,
         "entry_credit": 1.00}
    spot = 4050.0
    intr = h._settlement_intrinsic(spot, b)
    assert intr == 0.0
    breach_p = spot < b["p_short_k"]; breach_c = spot > b["c_short_k"]
    assert not breach_p and not breach_c
    settle_legs = (2 if breach_p else 0) + (2 if breach_c else 0)
    commission = (4 + settle_legs) * h.COMMISSION_PER_LEG * h.N_CONTRACTS
    assert commission == 4 * h.COMMISSION_PER_LEG   # 4 legs only
    pnl = (b["entry_credit"] - intr) * h.CONTRACT_MULTIPLIER * h.N_CONTRACTS - commission
    # 1.00 pt credit * 100 - 4*0.65 = 100 - 2.6 = 97.4
    assert abs(pnl - 97.4) < 1e-9


def test_commission_and_pnl_with_put_breach():
    """Put breach beyond wing: max loss, 2 extra settle legs charged."""
    b = {"p_short_k": 4000.0, "c_short_k": 4100.0, "p_long_k": 3995.0, "c_long_k": 4105.0,
         "entry_credit": 1.00}
    spot = 3900.0  # deep put breach
    intr = h._settlement_intrinsic(spot, b)
    assert intr == h.SPREAD_WIDTH  # capped at 5
    breach_p = spot < b["p_short_k"]; breach_c = spot > b["c_short_k"]
    assert breach_p and not breach_c
    commission = (4 + 2) * h.COMMISSION_PER_LEG * h.N_CONTRACTS
    pnl = (b["entry_credit"] - intr) * h.CONTRACT_MULTIPLIER * h.N_CONTRACTS - commission
    # (1.00 - 5.00)*100 - 6*0.65 = -400 - 3.9 = -403.9
    assert abs(pnl - (-403.9)) < 1e-9


def test_settle_spot_fallback_to_last_quoted_minute():
    day = dt.date(2024, 1, 2)
    # Build a chain missing the 16:00 minute; last quoted at 15:59.
    rows = []
    for mm, tag in [("15:59", "a")]:
        hh, m = 15, 59
        t = pd.Timestamp(dt.datetime.combine(day, dt.time(hh, m)))
        # a near-ATM strip so recon can solve the forward (C-P parity)
        for k in range(3990, 4011, 5):
            # crude synthetic: call - put ~ (F-K); set mids so forward ~ 4000
            cp = 4000.0 - k
            call_mid = max(cp, 0) + 2.0
            put_mid = max(-cp, 0) + 2.0
            rows.append({"minute": t, "strike": float(k), "right": "CALL",
                         "bid": call_mid - 0.05, "ask": call_mid + 0.05})
            rows.append({"minute": t, "strike": float(k), "right": "PUT",
                         "bid": put_mid - 0.05, "ask": put_mid + 0.05})
    nbbo = pd.DataFrame(rows)
    spot, m = h._recover_settle_spot(nbbo, day)
    assert spot is not None
    assert m.time() == dt.time(15, 59)   # fell back to the last quoted minute
    assert 3980 < spot < 4020            # recovered near 4000
