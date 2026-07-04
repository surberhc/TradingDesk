"""
test_s4_sizing_risk.py — S4 leverage sizing + risk guard + margin preflight.

Proves the three things the S4 leverage path must get right, all offline (SYNTHETIC
Targets, no broker, nothing transmitted):

  * SIZING: the SPY (risk) leg is sized to NAV*exposure (notional MAY exceed NAV when
    exposure>1.0 — real margin), and the BIL borrow leg (negative weight) is CARRIED
    THROUGH as a borrow, never silently dropped. A positive BIL cash leg sizes as shares.

  * RISK GUARD: evaluate_s4 PERMITS exposure up to leverage_cap and VETOES beyond it.

  * MARGIN PREFLIGHT: refuses the leveraged (>1.0) path on a simulated CASH account and on
    insufficient buying power; allows the un-levered conservative path on any account type.

Run:
  cd paperbot
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s4_sizing_risk.py -q
"""
from __future__ import annotations

import pandas as pd
import pytest

import s4_risk
import s4_sizing
from strategy_target import Target


def _target(spy_w: float, prices=(600.0, 91.5)):
    """A synthetic S4 Target with SPY weight = spy_w and BIL = 1-spy_w."""
    return Target(
        weights=pd.Series({"SPY": spy_w, "BIL": 1.0 - spy_w}, dtype="float64"),
        prices=pd.Series({"SPY": prices[0], "BIL": prices[1]}, dtype="float64"),
        as_of=pd.Timestamp("2026-07-02"),
        price_date=pd.Timestamp("2026-07-02"),
        version="S4/test",
    )


# --- SIZING ------------------------------------------------------------------------
def test_unlevered_sizes_both_legs_as_shares():
    # exposure 0.60 -> SPY 60% of NAV, BIL 40% of NAV, both positive holdings.
    nav = 1_000_000.0
    t = _target(0.60)
    intents = s4_sizing.size_orders(nav, {}, t)
    by = {(i.symbol, i.side): i for i in intents}
    spy = by[("SPY", "BUY")]
    bil = by[("BIL", "BUY")]
    assert spy.quantity == int(0.60 * nav / 600.0)      # 1000 shares
    assert bil.quantity == int(0.40 * nav / 91.5)
    assert all(not i.is_borrow_leg for i in intents)


def test_levered_spy_notional_exceeds_nav_and_borrow_leg_carried():
    # exposure 1.30 -> SPY notional = 1.30 * NAV (ABOVE NAV, margin), BIL weight = -0.30
    # (a real borrow) must appear as a borrow leg, NOT be dropped.
    nav = 1_000_000.0
    t = _target(1.30)
    intents = s4_sizing.size_orders(nav, {}, t)
    spy = next(i for i in intents if i.symbol == "SPY")
    assert spy.side == "BUY"
    assert spy.quantity == int(1.30 * nav / 600.0)      # 2166 shares
    assert spy.quantity * 600.0 > nav                   # notional exceeds NAV (leverage)
    assert spy.target_dollars == pytest.approx(1.30 * nav)

    borrow = [i for i in intents if i.is_borrow_leg]
    assert len(borrow) == 1, "borrow leg was dropped — the exact frozen-path bug S4 avoids"
    assert borrow[0].symbol == "BIL"
    assert borrow[0].side == "BORROW"
    assert borrow[0].target_dollars == pytest.approx(-0.30 * nav)   # negative = borrow


def test_levered_book_closes_any_held_bil():
    # If we currently hold BIL while the model says borrow, the sizing emits a SELL to flat
    # the BIL shares (plus the borrow leg).
    nav = 1_000_000.0
    t = _target(1.20)
    intents = s4_sizing.size_orders(nav, {"BIL": 500.0}, t)
    sells = [i for i in intents if i.symbol == "BIL" and i.side == "SELL"]
    assert len(sells) == 1 and sells[0].quantity == 500


# --- RISK GUARD --------------------------------------------------------------------
def test_guard_permits_up_to_cap():
    nav = 1_000_000.0
    t = _target(1.50)               # exactly at a 1.5 cap
    intents = s4_sizing.size_orders(nav, {}, t)
    v = s4_risk.evaluate_s4(nav, t, intents, leverage_cap=1.5)
    assert v.ok, v.reasons
    assert v.exposure == pytest.approx(1.50)


def test_guard_vetoes_beyond_cap():
    nav = 1_000_000.0
    t = _target(1.75)               # above a 1.5 cap
    intents = s4_sizing.size_orders(nav, {}, t)
    v = s4_risk.evaluate_s4(nav, t, intents, leverage_cap=1.5)
    assert not v.ok
    assert any("exceeds leverage_cap" in r for r in v.reasons)


def test_guard_permits_unlevered_conservative():
    nav = 1_000_000.0
    t = _target(0.30)               # a conservative-style light book
    intents = s4_sizing.size_orders(nav, {}, t)
    v = s4_risk.evaluate_s4(nav, t, intents, leverage_cap=1.5)
    assert v.ok, v.reasons


# --- MARGIN PREFLIGHT --------------------------------------------------------------
def _summary(account_type, bp, xl):
    return {"AccountType": account_type, "BuyingPower": bp, "ExcessLiquidity": xl}


def test_preflight_refuses_leverage_on_cash_account():
    nav = 1_000_000.0
    r = s4_risk.margin_preflight(_summary("CASH", bp=5_000_000, xl=1_000_000),
                                 nav, exposure=1.30, leverage_cap=1.5)
    assert not r.ok
    assert not r.is_margin
    assert any("requires a MARGIN account" in x for x in r.reasons)


def test_preflight_refuses_leverage_on_insufficient_bp():
    nav = 1_000_000.0
    # Margin account, but buying power below the 1.30x SPY notional (1.30M needed).
    r = s4_risk.margin_preflight(_summary("MARGIN", bp=1_000_000, xl=500_000),
                                 nav, exposure=1.30, leverage_cap=1.5)
    assert not r.ok
    assert r.is_margin
    assert any("insufficient BuyingPower" in x for x in r.reasons)


def test_preflight_allows_leverage_on_healthy_margin_account():
    nav = 1_000_000.0
    r = s4_risk.margin_preflight(_summary("REG T MARGIN", bp=3_000_000, xl=800_000),
                                 nav, exposure=1.30, leverage_cap=1.5)
    assert r.ok, r.reasons


def test_preflight_allows_unlevered_on_cash_account():
    # exposure <= 1.0 needs no borrow -> allowed even on a CASH account.
    nav = 1_000_000.0
    r = s4_risk.margin_preflight(_summary("CASH", bp=0, xl=0),
                                 nav, exposure=0.35, leverage_cap=1.5)
    assert r.ok, r.reasons


def test_preflight_refuses_exposure_over_cap_regardless():
    nav = 1_000_000.0
    r = s4_risk.margin_preflight(_summary("PORTFOLIO MARGIN", bp=9_000_000, xl=5_000_000),
                                 nav, exposure=1.80, leverage_cap=1.5)
    assert not r.ok
    assert any("exceeds leverage_cap" in x for x in r.reasons)


def test_account_is_margin_classification():
    assert s4_risk.account_is_margin("MARGIN")
    assert s4_risk.account_is_margin("REG T MARGIN")
    assert s4_risk.account_is_margin("PORTFOLIO MARGIN")
    assert not s4_risk.account_is_margin("CASH")
    assert not s4_risk.account_is_margin("REG T CASH")
    assert not s4_risk.account_is_margin("")     # unknown -> fail closed (non-margin)
