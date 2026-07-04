r"""
test_s5fh_fill_model.py -- the HONEST fill model of s5_financing_harness.

Pins the money-math that the whole S5 financing sweep rides on:
  * SELL fills at the BID, BUY fills at the ASK -- never the mid;
  * a leg with no two-sided quote (bid<=0 or ask<=0) is REJECTED (NotFillableError);
  * $0.65/leg commission on entry legs, and on ITM cash-settled legs at expiry only;
  * cash-settled index-option expiry cashflows (long ITM receives, short ITM pays; OTM = 0).

All synthetic / in-memory -- no warehouse read, exact arithmetic, deterministic.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s5_financing_harness as h  # noqa: E402


def _row(bid, ask):
    return pd.Series({"bid": bid, "ask": ask})


def test_sell_fills_at_bid_buy_fills_at_ask():
    r = _row(bid=2.00, ask=2.40)
    assert h.fill_price(r, "sell") == 2.00     # receive the bid
    assert h.fill_price(r, "buy") == 2.40      # pay the ask
    # never the mid
    assert h.fill_price(r, "sell") != pytest.approx(2.20)


def test_non_two_sided_leg_is_rejected():
    for bid, ask in [(0.0, 2.4), (2.0, 0.0), (0.0, 0.0), (np.nan, 2.4), (2.0, np.nan)]:
        with pytest.raises(h.NotFillableError):
            h.fill_price(_row(bid, ask), "sell")


def test_entry_cashflow_credit_and_commission():
    # sell 1 contract at bid 3.00 -> +$300 credit, $0.65 commission
    cash, comm = h.leg_entry_cashflow(_row(3.00, 3.20), "sell", n_contracts=1)
    assert cash == pytest.approx(300.0)
    assert comm == pytest.approx(0.65)
    # buy 1 at ask 1.10 -> -$110 debit, $0.65 commission
    cash, comm = h.leg_entry_cashflow(_row(1.00, 1.10), "buy", n_contracts=1)
    assert cash == pytest.approx(-110.0)
    assert comm == pytest.approx(0.65)


def test_entry_cashflow_scales_with_contracts():
    cash, comm = h.leg_entry_cashflow(_row(3.00, 3.20), "sell", n_contracts=4)
    assert cash == pytest.approx(1200.0)
    assert comm == pytest.approx(4 * 0.65)


def test_expiry_intrinsic_call_and_put():
    assert h.leg_expiry_intrinsic(4000, "CALL", 4050) == pytest.approx(50.0)
    assert h.leg_expiry_intrinsic(4000, "CALL", 3950) == 0.0
    assert h.leg_expiry_intrinsic(4000, "PUT", 3950) == pytest.approx(50.0)
    assert h.leg_expiry_intrinsic(4000, "PUT", 4050) == 0.0


def test_expiry_cashflow_long_itm_receives_short_itm_pays_otm_free():
    # long call 50 ITM -> receive +$5000, $0.65 settle commission
    cash, comm = h.leg_expiry_cashflow(4000, "CALL", "buy", settle_underlying=4050)
    assert cash == pytest.approx(5000.0)
    assert comm == pytest.approx(0.65)
    # short call 50 ITM -> pay -$5000, $0.65 settle commission
    cash, comm = h.leg_expiry_cashflow(4000, "CALL", "sell", settle_underlying=4050)
    assert cash == pytest.approx(-5000.0)
    assert comm == pytest.approx(0.65)
    # OTM -> worthless, no cash, NO commission (no trade)
    cash, comm = h.leg_expiry_cashflow(4000, "CALL", "sell", settle_underlying=3950)
    assert cash == 0.0
    assert comm == 0.0


def test_close_fill_price_is_the_opposite_side():
    r = _row(bid=1.00, ask=1.30)
    # opened by selling -> close by BUYING back at the ask
    assert h.close_fill_price(r, "sell") == 1.30
    # opened by buying -> close by SELLING at the bid
    assert h.close_fill_price(r, "buy") == 1.00
