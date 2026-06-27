"""
test_rebalance_engine.py — pure offline unit tests for the multi-account block engine.

SYNTHETIC data only (fake accounts, prices, weights). No broker, no gateway, no orders.
Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\paperbot\\test_rebalance_engine.py" -v

These prove the five things the engine must get right:
  * share math               -> int(weight * investable / price)
  * reserve carve-out        -> (NetLiq - reserve) * (1 - cash_reserve)
  * band (account-level)     -> a fully in-band account emits nothing; if ANY holding
                                breaches, the whole account rebalances (in-band siblings too)
  * block aggregation        -> per-account split sums to the block quantity
  * single-account fallback  -> a lone-account block routes DIRECT, not as a group
"""
from __future__ import annotations

import pandas as pd
import pytest

import config
import rebalance_engine as eng
import strategy_target


# --- synthetic target helper ---------------------------------------------------
def make_target(weights: dict, prices: dict, version: str = "Balanced") -> strategy_target.Target:
    """Build a strategy_target.Target from plain dicts — no backtester, no data load.
    reconcile.reconcile only reads .weights (index + get) and .prices (get)."""
    return strategy_target.Target(
        weights=pd.Series(weights, dtype="float64"),
        prices=pd.Series(prices, dtype="float64"),
        as_of=pd.Timestamp("2026-06-26"),
        price_date=pd.Timestamp("2026-06-26"),
        version=version,
    )


CASH_RESERVE = config.RISK_LIMITS["cash_reserve_pct"]   # 0.05 by default


# --- 1. reserve carve-out ------------------------------------------------------
def test_investable_no_reserve():
    # (1,000,000 - 0) * (1 - 0.05) = 950,000
    assert eng.compute_investable(1_000_000, 0.0, 0.05) == pytest.approx(950_000.0)


def test_investable_with_reserve_carved_first():
    # reserve removed BEFORE the cash buffer: (1,000,000 - 100,000) * 0.95 = 855,000
    assert eng.compute_investable(1_000_000, 100_000, 0.05) == pytest.approx(855_000.0)


def test_investable_never_negative():
    # reserve larger than NetLiq must clamp to 0, not manufacture phantom sells.
    assert eng.compute_investable(50_000, 200_000, 0.05) == 0.0


# --- 2. share math -------------------------------------------------------------
def test_share_math_floor_division():
    # one all-cash account, single 100% holding @ $250, NetLiq 1,000,000.
    # investable = 950,000 ; target = floor(950,000 / 250) = 3800 shares.
    target = make_target({"SPY": 1.0}, {"SPY": 250.0})
    plan = eng.plan_account("DU0001", "Balanced", 1_000_000, {}, target, band_pct=0.03)
    assert plan.investable == pytest.approx(950_000.0)
    assert plan.orders == {"SPY": 3800}     # all BUY from flat
    line = next(l for l in plan.lines if l.symbol == "SPY")
    assert line.target_shares == 3800


def test_share_math_two_holdings():
    # 60/40 split. investable 950,000.
    #   SPY 60% -> 570,000 / 200 = 2850 ; BND 40% -> 380,000 / 100 = 3800
    target = make_target({"SPY": 0.6, "BND": 0.4}, {"SPY": 200.0, "BND": 100.0})
    plan = eng.plan_account("DU0002", "Balanced", 1_000_000, {}, target, band_pct=0.03)
    assert plan.orders == {"SPY": 2850, "BND": 3800}


# --- 3. band suppression -------------------------------------------------------
def test_band_suppresses_small_drift():
    # Hold a position whose weight is ~1% off target (inside the 3% band) -> NO delta.
    # target SPY 100% @ $100, NetLiq 1,000,000 -> target_w 100%, investable 950,000.
    # Hold 9600 sh -> actual_w = 9600*100/1,000,000 = 96% ; drift -4%? -> recompute:
    # Use a drift well inside band: hold 9700 -> actual_w 97%, drift -3% (== band edge).
    # To be unambiguously INSIDE the band, hold 9800 -> 98%, drift -2% -> MATCHED, no delta.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    plan = eng.plan_account("DU0003", "Balanced", 1_000_000, {"SPY": 9800}, target,
                            band_pct=0.03)
    assert plan.orders == {}                 # inside band -> suppressed
    assert plan.needs_rebalance is False


def test_band_lets_large_drift_through():
    # Same model, but hold only 5000 sh -> actual_w 50%, drift -50% (way past band).
    # Breaches -> emit the delta back to the integer target (9500 sh on 950,000 investable).
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    plan = eng.plan_account("DU0004", "Balanced", 1_000_000, {"SPY": 5000}, target,
                            band_pct=0.03)
    assert plan.needs_rebalance is True
    assert plan.orders == {"SPY": 9500 - 5000}   # target 9500, actual 5000 -> +4500 BUY


def test_untracked_holding_is_sold():
    # A holding not in the model (weight 0) must be flagged and fully sold, band or not.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0, "GOOG": 150.0})
    # hold the full SPY target so SPY is in-band, plus a stray 10 GOOG.
    plan = eng.plan_account("DU0005", "Balanced", 1_000_000, {"SPY": 9500, "GOOG": 10},
                            target, band_pct=0.03)
    assert plan.orders.get("GOOG") == -10        # SELL the untracked position
    assert "SPY" not in plan.orders              # SPY is on target / in band


def test_account_level_band_rebalances_in_band_siblings():
    # ACCOUNT-LEVEL all-or-nothing (Andrew 2026-06-27): if ONE holding breaches, the WHOLE
    # account is rebalanced -- including holdings that are individually inside the band.
    # 50/50 SPY/BND, investable 950,000 -> target 4750 each @ $100.
    #   SPY held 4800 -> 48% vs 50% = -2% drift -> INSIDE band (would be left alone per-holding)
    #   BND held 1000 -> 10% vs 50% = -40% drift -> BREACHES the band
    # Because BND breaches, the account rebalances and SPY ALSO moves (-50) -- this is the
    # behavior that distinguishes account-level from per-holding.
    target = make_target({"SPY": 0.5, "BND": 0.5}, {"SPY": 100.0, "BND": 100.0})
    plan = eng.plan_account("DU0099", "Balanced", 1_000_000, {"SPY": 4800, "BND": 1000},
                            target, band_pct=0.03)
    assert plan.needs_rebalance is True
    assert plan.orders["BND"] == 4750 - 1000     # +3750 BUY (the breach)
    assert plan.orders["SPY"] == 4750 - 4800     # -50 SELL (in-band sibling moves too)


# --- 4. block aggregation: split sums to block qty -----------------------------
def test_block_aggregation_sums():
    # Two Balanced accounts, both flat, same model -> same-symbol same-side BUY block.
    target = make_target({"SPY": 0.5, "BND": 0.5}, {"SPY": 200.0, "BND": 100.0})
    inputs = [
        {"account": "DU0006", "version": "Balanced", "net_liq": 1_000_000, "positions": {}},
        {"account": "DU0007", "version": "Balanced", "net_liq": 2_000_000, "positions": {}},
    ]
    plans = eng.plan_accounts(inputs, {"Balanced": target}, band_pct=0.03)
    blocks = eng.aggregate_blocks(plans)

    for b in blocks:
        # the core invariant: the per-account split sums to the block quantity.
        assert sum(b.per_account.values()) == b.total_qty
        assert set(b.per_account) == {"DU0006", "DU0007"}
        assert b.side == "BUY"

    # spot-check SPY math: acct6 invest 950,000*0.5=475,000/200=2375 ;
    #                      acct7 invest 1,900,000*0.5=950,000/200=4750 ; block 7125.
    spy = next(b for b in blocks if b.symbol == "SPY")
    assert spy.per_account == {"DU0006": 2375, "DU0007": 4750}
    assert spy.total_qty == 7125


def test_buys_and_sells_are_separate_blocks():
    # acct A must BUY SPY, acct B must SELL SPY (same tier) -> two blocks, never netted.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    inputs = [
        {"account": "DU0008", "version": "Balanced", "net_liq": 1_000_000, "positions": {}},        # BUY 9500
        {"account": "DU0009", "version": "Balanced", "net_liq": 1_000_000, "positions": {"SPY": 20000}},  # SELL down to 9500
    ]
    plans = eng.plan_accounts(inputs, {"Balanced": target}, band_pct=0.03)
    blocks = eng.aggregate_blocks(plans)
    sides = {b.side for b in blocks if b.symbol == "SPY"}
    assert sides == {"BUY", "SELL"}              # not netted into one
    for b in blocks:
        assert sum(b.per_account.values()) == b.total_qty


# --- 5. routing: group block vs single-account direct fallback -----------------
def test_route_multi_account_is_fa_block():
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    inputs = [
        {"account": "DU0010", "version": "Growth", "net_liq": 1_000_000, "positions": {}},
        {"account": "DU0011", "version": "Growth", "net_liq": 1_000_000, "positions": {}},
    ]
    out = eng.build_plan(inputs, {"Growth": target}, band_pct=0.03)
    routes = out["routes"]
    assert len(routes) == 1
    r = routes[0]
    assert r.route == "fa_block"
    assert r.fa_group == eng.TIER_GROUPS["Growth"]
    assert r.account is None
    # CRITICAL: fa_method must be empty, NOT "NetLiq" (Error 10226). The group's
    # ContractsOrShares (== per_account_split) governs the allocation.
    assert r.fa_method == ""
    assert sum(r.per_account_split.values()) == r.total_qty
    assert set(r.per_account_split) == {"DU0010", "DU0011"}


def test_route_single_account_falls_back_to_direct():
    # Only ONE Conservative account enrolled -> its block must route DIRECT, not as a group.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    inputs = [
        {"account": "DU0012", "version": "Conservative", "net_liq": 1_000_000, "positions": {}},
    ]
    out = eng.build_plan(inputs, {"Conservative": target}, band_pct=0.03)
    routes = out["routes"]
    assert len(routes) == 1
    r = routes[0]
    assert r.route == "direct"
    assert r.account == "DU0012"
    assert r.fa_group is None
    assert r.per_account_split == {"DU0012": r.total_qty}


def test_in_band_account_produces_no_routes():
    # Every account already on target -> no plans needing rebalance -> no blocks, no routes.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    inputs = [
        {"account": "DU0013", "version": "Balanced", "net_liq": 1_000_000, "positions": {"SPY": 9500}},
    ]
    out = eng.build_plan(inputs, {"Balanced": target}, band_pct=0.03)
    assert out["blocks"] == []
    assert out["routes"] == []
    assert all(not p.needs_rebalance for p in out["plans"])


# --- integration: heterogeneous tiers, mixed actions ---------------------------
def test_mixed_tiers_end_to_end():
    bal = make_target({"SPY": 0.5, "BND": 0.5}, {"SPY": 200.0, "BND": 100.0}, "Balanced")
    gro = make_target({"SPY": 1.0}, {"SPY": 200.0}, "Growth")
    targets = {"Balanced": bal, "Growth": gro}
    inputs = [
        {"account": "DU0142", "version": "Balanced", "net_liq": 1_000_000, "positions": {}},
        {"account": "DU0143", "version": "Balanced", "net_liq": 1_000_000, "positions": {}},
        {"account": "DU0145", "version": "Growth",   "net_liq": 1_000_000, "positions": {}},
        {"account": "DU0146", "version": "Growth",   "net_liq": 1_000_000, "positions": {}},
    ]
    out = eng.build_plan(inputs, targets, band_pct=0.03)
    # Two Balanced -> SPY & BND blocks (fa_block); two Growth -> SPY block (fa_block).
    routes = out["routes"]
    assert all(r.route == "fa_block" for r in routes)
    # every split sums to its block quantity and stays within its tier's accounts.
    bal_accts, gro_accts = {"DU0142", "DU0143"}, {"DU0145", "DU0146"}
    for r in routes:
        assert sum(r.per_account_split.values()) == r.total_qty
        expected = bal_accts if r.version == "Balanced" else gro_accts
        assert set(r.per_account_split) == expected
