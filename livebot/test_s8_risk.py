"""
test_s8_risk.py — S8 margin/account preflight (paperbot/s8_risk.py), Stage 4 of the
5-stage S8 build.

Proves the defined-risk margin gate gets the same fail-closed behavior s4_risk.py's
margin_preflight() already proves for S4, adapted to an option spread's notional (width/
credit/qty) instead of NAV*exposure. All offline (dict-based accountSummary fixtures, no
broker connection, nothing transmitted) — same convention as test_s4_sizing_risk.py.

Run:
  cd paperbot
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s8_risk.py -q
"""
from __future__ import annotations

import pytest

import s8_risk


def _summary(account_type, bp, xl):
    return {"AccountType": account_type, "BuyingPower": bp, "ExcessLiquidity": xl}


# A width-80, $4-credit spread: required_notional = 80*100*1 - 4*100*1 = 7,600 per contract
# (matches this session's earlier finding that per-trade margin at 1 contract runs
# $2,795-$8,315 depending on template).
WIDTH_80_CREDIT_4 = dict(width_points=80.0, realized_credit=4.0, qty=1)


# --- required_notional formula ------------------------------------------------------
def test_required_notional_matches_verified_formula():
    # (width*100*qty) - (credit*100*qty); independently verified this session against
    # real IBKR BuyingPower (ratio 1.000000 across 4,687 TAT rows).
    assert s8_risk.required_notional(80.0, 4.0, 1) == pytest.approx(7_600.0)
    assert s8_risk.required_notional(50.0, 2.0, 1) == pytest.approx(4_800.0)
    # qty scales linearly.
    assert s8_risk.required_notional(80.0, 4.0, 3) == pytest.approx(22_800.0)


# --- MARGIN PREFLIGHT ----------------------------------------------------------------
def test_preflight_allows_margin_account_with_sufficient_buying_power():
    r = s8_risk.margin_preflight(
        _summary("MARGIN", bp=100_000, xl=50_000), **WIDTH_80_CREDIT_4)
    assert r.ok, r.reasons
    assert r.is_margin
    assert r.required_notional == pytest.approx(7_600.0)


def test_preflight_refuses_cash_account():
    r = s8_risk.margin_preflight(
        _summary("CASH", bp=1_000_000, xl=500_000), **WIDTH_80_CREDIT_4)
    assert not r.ok
    assert not r.is_margin
    assert any("require a MARGIN account" in x for x in r.reasons)


def test_preflight_refuses_insufficient_buying_power():
    # Margin account, but BuyingPower below the $7,600 required notional.
    r = s8_risk.margin_preflight(
        _summary("MARGIN", bp=5_000, xl=50_000), **WIDTH_80_CREDIT_4)
    assert not r.ok
    assert r.is_margin
    assert any("insufficient BuyingPower" in x for x in r.reasons)


def test_preflight_refuses_non_positive_excess_liquidity():
    r = s8_risk.margin_preflight(
        _summary("MARGIN", bp=100_000, xl=0), **WIDTH_80_CREDIT_4)
    assert not r.ok
    assert any("non-positive ExcessLiquidity" in x for x in r.reasons)

    r2 = s8_risk.margin_preflight(
        _summary("MARGIN", bp=100_000, xl=-500), **WIDTH_80_CREDIT_4)
    assert not r2.ok
    assert any("non-positive ExcessLiquidity" in x for x in r2.reasons)


def test_preflight_scales_required_notional_with_qty():
    # 3 contracts of the width-80/$4 template -> $22,800 required; BP of $10k is enough
    # for 1 contract but not 3.
    r = s8_risk.margin_preflight(
        _summary("MARGIN", bp=10_000, xl=50_000),
        width_points=80.0, realized_credit=4.0, qty=3)
    assert not r.ok
    assert r.required_notional == pytest.approx(22_800.0)
    assert any("insufficient BuyingPower" in x for x in r.reasons)


def test_preflight_accepts_ib_async_style_rows():
    # Same dual-shape support as s4_risk.py: a list of tag/value row objects, not a dict.
    class Row:
        def __init__(self, tag, value):
            self.tag = tag
            self.value = value

    rows = [
        Row("AccountType", "REG T MARGIN"),
        Row("BuyingPower", "100000"),
        Row("ExcessLiquidity", "50000"),
    ]
    r = s8_risk.margin_preflight(rows, **WIDTH_80_CREDIT_4)
    assert r.ok, r.reasons


def test_account_is_margin_reused_from_s4_risk():
    # Classification logic is imported (not duplicated) from s4_risk.py so it can never
    # drift between S4 and S8.
    assert s8_risk.account_is_margin("MARGIN")
    assert not s8_risk.account_is_margin("CASH")


def test_preflight_accepts_trust_account_when_buying_power_exceeds_net_liq():
    summary = {"AccountType": "TRUST", "BuyingPower": 378_279,
               "ExcessLiquidity": 94_569, "NetLiquidation": 116_852}
    r = s8_risk.margin_preflight(summary, **WIDTH_80_CREDIT_4)
    assert r.ok, r.reasons
    assert r.is_margin


def test_preflight_refuses_entity_labeled_account_when_bp_not_over_net_liq():
    summary = {"AccountType": "TRUST", "BuyingPower": 50_000,
               "ExcessLiquidity": 50_000, "NetLiquidation": 116_852}
    r = s8_risk.margin_preflight(summary, **WIDTH_80_CREDIT_4)
    assert not r.ok
    assert not r.is_margin
    assert any("require a MARGIN account" in x for x in r.reasons)


def test_account_is_margin_buying_power_signal():
    assert s8_risk.account_is_margin("TRUST", buying_power=378_000, net_liq=116_000)
    assert s8_risk.account_is_margin("INDIVIDUAL", buying_power=200_000, net_liq=100_000)
    assert not s8_risk.account_is_margin("TRUST", buying_power=100_000, net_liq=116_000)
    assert not s8_risk.account_is_margin("INDIVIDUAL", buying_power=957, net_liq=957)
    assert s8_risk.account_is_margin("REG T MARGIN")
    assert not s8_risk.account_is_margin("TRUST", buying_power=100, net_liq=0)
