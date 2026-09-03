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
  * held-aside carve-out     -> instruments on the no-trade list are priced, counted and
                                reported, never traded, and sit OUTSIDE the allocation
"""
from __future__ import annotations

from dataclasses import asdict

import pandas as pd
import pytest

import config
import holding_class
import recon_report
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


CASH_RESERVE = config.RISK_LIMITS["cash_reserve_pct"]   # 0.015 (Slice 2 re-base)


# --- 1. reserve carve-out ------------------------------------------------------
def test_investable_no_reserve():
    # (1,000,000 - 0) * (1 - 0.015) = 985,000
    assert eng.compute_investable(1_000_000, 0.0, 0.015) == pytest.approx(985_000.0)


def test_investable_with_reserve_carved_first():
    # reserve removed BEFORE the cash buffer: (1,000,000 - 100,000) * 0.985 = 886,500
    assert eng.compute_investable(1_000_000, 100_000, 0.015) == pytest.approx(886_500.0)


def test_investable_never_negative():
    # reserve larger than NetLiq must clamp to 0, not manufacture phantom sells.
    assert eng.compute_investable(50_000, 200_000, 0.015) == 0.0


# --- 2. share math -------------------------------------------------------------
def test_share_math_floor_division():
    # one all-cash account, single 100% holding @ $250, NetLiq 1,000,000.
    # investable = 985,000 ; target = floor(985,000 / 250) = 3940 shares.
    target = make_target({"SPY": 1.0}, {"SPY": 250.0})
    plan = eng.plan_account("DU0001", "Balanced", 1_000_000, {}, target, band_pct=0.03)
    assert plan.investable == pytest.approx(985_000.0)
    assert plan.orders == {"SPY": 3940}     # all BUY from flat
    line = next(l for l in plan.lines if l.symbol == "SPY")
    assert line.target_shares == 3940


def test_share_math_two_holdings():
    # 60/40 split. investable 985,000.
    #   SPY 60% -> 591,000 / 200 = 2955 ; BND 40% -> 394,000 / 100 = 3940
    target = make_target({"SPY": 0.6, "BND": 0.4}, {"SPY": 200.0, "BND": 100.0})
    plan = eng.plan_account("DU0002", "Balanced", 1_000_000, {}, target, band_pct=0.03)
    assert plan.orders == {"SPY": 2955, "BND": 3940}


# --- 3. band suppression -------------------------------------------------------
def test_band_suppresses_small_drift():
    # Hold a position whose required trade is well inside the 3% band -> NO delta.
    # target SPY 100% @ $100, NetLiq 1,000,000, investable 985,000 -> target 9850 sh.
    # Hold 9800 -> trade = |9850-9800|*100/1,000,000 = 0.5% of NetLiq -> INSIDE band, no delta.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    plan = eng.plan_account("DU0003", "Balanced", 1_000_000, {"SPY": 9800}, target,
                            band_pct=0.03)
    assert plan.orders == {}                 # inside band -> suppressed
    assert plan.needs_rebalance is False


def test_band_lets_large_drift_through():
    # Same model, but hold only 5000 sh -> trade ~48% of NetLiq (way past band).
    # Breaches -> emit the delta back to the integer target (9850 sh on 985,000 investable).
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    plan = eng.plan_account("DU0004", "Balanced", 1_000_000, {"SPY": 5000}, target,
                            band_pct=0.03)
    assert plan.needs_rebalance is True
    assert plan.orders == {"SPY": 9850 - 5000}   # target 9850, actual 5000 -> +4850 BUY


def test_untracked_holding_is_sold():
    # A holding not in the model (weight 0) must be flagged and fully sold, band or not.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0, "GOOG": 150.0})
    # hold the full SPY target (9850 on 985,000 investable) so SPY is on target, plus a
    # stray 10 GOOG. The untracked GOOG breaches, the account rebalances, but SPY (delta 0)
    # stays out of the orders.
    plan = eng.plan_account("DU0005", "Balanced", 1_000_000, {"SPY": 9850, "GOOG": 10},
                            target, band_pct=0.03)
    assert plan.orders.get("GOOG") == -10        # SELL the untracked position
    assert "SPY" not in plan.orders              # SPY is on target / in band


def test_account_level_band_rebalances_in_band_siblings():
    # ACCOUNT-LEVEL all-or-nothing (Andrew 2026-06-27): if ONE holding breaches, the WHOLE
    # account is rebalanced -- including holdings that are individually inside the band.
    # 50/50 SPY/BND, investable 985,000 -> target 4925 each @ $100.
    #   SPY held 4800 -> trade |4925-4800|*100/1M = 1.25% of NetLiq -> INSIDE band per-holding
    #   BND held 1000 -> trade |4925-1000|*100/1M = 39.25% of NetLiq -> BREACHES the band
    # Because BND breaches, the account rebalances and SPY ALSO moves (+125) -- this is the
    # behavior that distinguishes account-level from per-holding.
    target = make_target({"SPY": 0.5, "BND": 0.5}, {"SPY": 100.0, "BND": 100.0})
    plan = eng.plan_account("DU0099", "Balanced", 1_000_000, {"SPY": 4800, "BND": 1000},
                            target, band_pct=0.03)
    assert plan.needs_rebalance is True
    assert plan.orders["BND"] == 4925 - 1000     # +3925 BUY (the breach)
    assert plan.orders["SPY"] == 4925 - 4800     # +125 BUY (in-band sibling moves too)


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

    # spot-check SPY math: acct6 invest 985,000*0.5=492,500/200=2462 ;
    #                      acct7 invest 1,970,000*0.5=985,000/200=4925 ; block 7387.
    spy = next(b for b in blocks if b.symbol == "SPY")
    assert spy.per_account == {"DU0006": 2462, "DU0007": 4925}
    assert spy.total_qty == 7387


def test_buys_and_sells_are_separate_blocks():
    # acct A must BUY SPY, acct B must SELL SPY (same tier) -> two blocks, never netted.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    inputs = [
        {"account": "DU0008", "version": "Balanced", "net_liq": 1_000_000, "positions": {}},        # BUY 9850
        {"account": "DU0009", "version": "Balanced", "net_liq": 1_000_000, "positions": {"SPY": 20000}},  # SELL down to 9850
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
    # target 9850 sh on 985,000 investable; hold exactly that so the account is on model.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    inputs = [
        {"account": "DU0013", "version": "Balanced", "net_liq": 1_000_000, "positions": {"SPY": 9850}},
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


# ============================================================================== #
# 6. HELD-ASIDE CARVE-OUT (the bond no-trade list, owner decision 2026-08-19).    #
#                                                                                 #
# Held aside means: we PRICE it, we COUNT it, we REPORT it, and we NEVER emit an  #
# order for it. Its value sits OUTSIDE the target allocation, so the model applies #
# to the REMAINING sleeve as its own 100%. An account holding individual bonds is  #
# no longer benched — its non-bond sleeve rebalances normally.                     #
# ============================================================================== #
# A live IBKR bond shape: FACE-amount quantity, percent-of-par-per-100 mark.
BOND_SYM = "797843BE8 4.6 08/01/34"
BOND_FACE = 10_000
BOND_MARK = 100.14628819
BOND_VALUE = BOND_FACE * BOND_MARK / 100.0        # 10,014.628819 — NOT 1,001,462.88


def test_characterization_no_held_aside_is_byte_identical_to_today():
    """CHARACTERIZATION (the load-bearing one): an account with NO held-aside positions
    must produce exactly today's numbers. Both the legacy call (no sec_types at all) and an
    all-MANAGED classification must land on the same plan, field for field."""
    target = make_target({"SPY": 1.0}, {"SPY": 250.0})
    legacy = eng.plan_account("DU0001", "Balanced", 1_000_000, {}, target, band_pct=0.03)
    classified = eng.plan_account("DU0001", "Balanced", 1_000_000, {}, target,
                                  band_pct=0.03, sec_types={"SPY": "STK"})

    # The exact pre-existing numbers (same literals as test_share_math_floor_division).
    assert legacy.investable == pytest.approx(985_000.0)
    assert legacy.orders == {"SPY": 3940}
    assert legacy.net_liq == 1_000_000
    assert legacy.reserve == 0.0

    # Every field, including the new ones, is identical between the two paths.
    assert asdict(legacy) == asdict(classified)

    # ...and the new fields are inert: the managed sleeve IS the whole account.
    for p in (legacy, classified):
        assert p.managed_net_liq == p.net_liq
        assert p.held_aside_value == 0.0
        assert p.held_aside == []
        assert p.blocked_reasons == []
        assert p.blocked is False
        assert p.unclassified == []


def test_characterization_holding_positions_unchanged_when_all_managed():
    """Same characterization with real holdings and a drift breach: classifying every
    position as STK changes nothing about the plan."""
    target = make_target({"SPY": 0.5, "BND": 0.5}, {"SPY": 100.0, "BND": 100.0})
    pos = {"SPY": 4800, "BND": 1000}
    legacy = eng.plan_account("DU0099", "Balanced", 1_000_000, pos, target, band_pct=0.03)
    classified = eng.plan_account("DU0099", "Balanced", 1_000_000, pos, target,
                                  band_pct=0.03,
                                  sec_types={"SPY": "STK", "BND": "STK"})
    assert asdict(legacy) == asdict(classified)
    assert legacy.orders == {"BND": 3925, "SPY": 125}      # unchanged from today


def test_bond_is_excluded_from_legs_but_present_in_the_reporting_detail():
    """A bond never becomes a leg — not a buy, not a sell, not an ALIEN liquidation — and is
    fully accounted for on the plan: symbol, quantity, market value, reason."""
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    net_liq = 100_000.0 + BOND_VALUE                    # managed sleeve is exactly 100,000
    plan = eng.plan_account(
        "DU0500", "Balanced", net_liq,
        {"SPY": 0, BOND_SYM: BOND_FACE}, target, band_pct=0.03,
        prices={"SPY": 100.0, BOND_SYM: BOND_MARK},
        sec_types={"SPY": "STK", BOND_SYM: "BOND"})

    # --- never a leg, in any direction ---
    assert BOND_SYM not in plan.orders
    assert all(not sym.startswith("797843") for sym in plan.orders)
    # --- and never a reconcile line either, so it can NEVER be read as drift or as an
    #     UNTRACKED / ALIEN holding awaiting liquidation ---
    assert BOND_SYM not in {ln.symbol for ln in plan.lines}
    assert plan.alien_lines == []

    # --- but fully reported ---
    assert len(plan.held_aside) == 1
    h = plan.held_aside[0]
    assert h.symbol == BOND_SYM
    assert h.sec_type == "BOND"
    assert h.quantity == BOND_FACE
    assert h.market_value == pytest.approx(BOND_VALUE)   # per-100, not qty*mark
    assert "never traded" in h.reason
    assert h.needs_classification is False

    # --- account totals: total == managed sleeve + held aside, both priced ---
    assert plan.net_liq == pytest.approx(net_liq)
    assert plan.held_aside_value == pytest.approx(BOND_VALUE)
    assert plan.managed_net_liq == pytest.approx(100_000.0)
    assert plan.managed_net_liq + plan.held_aside_value == pytest.approx(plan.net_liq)


def test_model_weights_apply_to_the_remaining_sleeve_as_its_own_100pct():
    """Bonds sit OUTSIDE the target allocation: an account with a $10,014.63 bond and a
    $100,000 managed sleeve must size EXACTLY like a $100,000 all-equity account. The bond
    is NOT counted as the client's fixed-income allocation."""
    target = make_target({"SPY": 0.6, "BND": 0.4}, {"SPY": 200.0, "BND": 100.0})
    plain = eng.plan_account("DU0601", "Balanced", 100_000.0, {}, target, band_pct=0.03)
    with_bond = eng.plan_account(
        "DU0602", "Balanced", 100_000.0 + BOND_VALUE,
        {BOND_SYM: BOND_FACE}, target, band_pct=0.03,
        prices={BOND_SYM: BOND_MARK},
        sec_types={BOND_SYM: "BOND", "SPY": "STK", "BND": "STK"})

    assert with_bond.investable == pytest.approx(plain.investable)
    assert with_bond.orders == plain.orders              # identical share targets
    # investable = 100,000 * (1 - 0.015) = 98,500 ; SPY 60% -> 295 sh @200, BND 40% -> 394 @100
    assert plain.orders == {"SPY": 295, "BND": 394}
    # ...and the model's weights sum to 100% OF THE MANAGED SLEEVE, not of the whole account.
    spy = next(ln for ln in with_bond.lines if ln.symbol == "SPY")
    assert spy.target_weight == pytest.approx(0.6)


def test_unknown_instrument_type_is_held_aside_and_flagged_not_traded():
    """FAIL CLOSED: an instrument whose type cannot be determined is held aside and reported
    as needing classification. It is never silently assumed tradeable."""
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    plan = eng.plan_account(
        "DU0700", "Balanced", 100_000.0, {"SPY": 0, "MYSTERY": 7}, target, band_pct=0.03,
        prices={"SPY": 100.0},
        sec_types={"SPY": "STK"},                # MYSTERY deliberately absent
        values={"MYSTERY": 5_000.0})

    assert "MYSTERY" not in plan.orders
    assert "MYSTERY" not in {ln.symbol for ln in plan.lines}
    assert [h.symbol for h in plan.held_aside] == ["MYSTERY"]
    assert plan.held_aside[0].sec_type == holding_class.UNKNOWN
    assert plan.held_aside[0].needs_classification is True
    assert [h.symbol for h in plan.unclassified] == ["MYSTERY"]
    # It is still PRICED and carved out, so the rest of the account plans normally.
    assert plan.managed_net_liq == pytest.approx(95_000.0)
    assert plan.orders["SPY"] == int(95_000.0 * (1 - CASH_RESERVE) / 100.0)


def test_untyped_position_would_have_been_sold_without_classification():
    """CONTRAST — the behavior the classifier replaces. With NO sec_types, an unrecognised
    holding is UNTRACKED and always breaches: the engine would SELL it. Classification is
    what turns that into 'hold it aside and ask a human'."""
    target = make_target({"SPY": 1.0}, {"SPY": 100.0, "MYSTERY": 700.0})
    legacy = eng.plan_account("DU0701", "Balanced", 100_000.0, {"SPY": 0, "MYSTERY": 7},
                              target, band_pct=0.03)
    assert legacy.orders["MYSTERY"] == -7            # liquidated today
    classified = eng.plan_account(
        "DU0701", "Balanced", 100_000.0, {"SPY": 0, "MYSTERY": 7}, target, band_pct=0.03,
        sec_types={"SPY": "STK"}, values={"MYSTERY": 4_900.0})
    assert "MYSTERY" not in classified.orders        # never traded once classified


def test_carve_out_composes_with_the_cash_reserve_and_the_distribution_reserve(monkeypatch):
    """The bond carve-out COMPOSES with the existing reserve; it does not replace it.
        managed_net_liq = net_liq - held_aside_value
        investable      = (managed_net_liq - distribution_reserve) * (1 - cash_reserve)
    and the distribution reserve is still computed on the WHOLE account (a client's
    scheduled distribution does not shrink because part of their money sits in bonds)."""
    seen = {}

    def fake_reserve_for(account, nav):
        seen["nav"] = nav
        return 10_000.0

    monkeypatch.setattr(eng.cashflows, "reserve_for", fake_reserve_for)
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    net_liq = 100_000.0 + BOND_VALUE
    plan = eng.plan_account(
        "DU0800", "Balanced", net_liq, {BOND_SYM: BOND_FACE}, target, band_pct=0.03,
        prices={BOND_SYM: BOND_MARK}, sec_types={BOND_SYM: "BOND", "SPY": "STK"})

    assert seen["nav"] == pytest.approx(net_liq)       # reserve keyed off the WHOLE account
    assert plan.reserve == 10_000.0
    assert plan.managed_net_liq == pytest.approx(100_000.0)
    # (100,000 - 10,000) * (1 - 0.015) = 88,650  -> both carve-outs applied, in order.
    assert plan.investable == pytest.approx(90_000.0 * (1 - CASH_RESERVE))
    assert plan.investable == pytest.approx(88_650.0)
    assert plan.orders == {"SPY": 886}


def test_band_is_measured_against_the_managed_sleeve_not_the_whole_account():
    """A 4% drift in the tradeable half of a half-bond account is a real 4% breach — it must
    not be diluted by the held-aside half into an in-band 2%."""
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    # Managed sleeve 100,000 -> target 985 sh. Hold 940 -> trade 45 sh = $4,500 = 4.5% of the
    # managed sleeve (breaches a 3% band) but only 2.25% of the 200,000 whole account.
    plan = eng.plan_account(
        "DU0900", "Balanced", 200_000.0, {"SPY": 940, "BONDX": 100_000}, target,
        band_pct=0.03, prices={"SPY": 100.0},
        sec_types={"SPY": "STK", "BONDX": "BOND"}, values={"BONDX": 100_000.0})
    assert plan.managed_net_liq == pytest.approx(100_000.0)
    assert plan.needs_rebalance is True
    assert plan.orders == {"SPY": 45}


def test_formerly_benched_bond_account_now_yields_a_real_plan_end_to_end():
    """THE POINT OF THE WHOLE CHANGE: an account holding individual bonds used to be held
    out of the batch entirely. It must now produce a real, routable plan for its non-bond
    sleeve — with no bond anywhere in the blocks or routes."""
    target = make_target({"SPY": 0.5, "BND": 0.5}, {"SPY": 100.0, "BND": 100.0})
    inputs = [
        # A bond-holding account, badly drifted on its managed sleeve.
        {"account": "U7552750", "version": "Growth", "net_liq": 100_000.0 + BOND_VALUE,
         "positions": {"SPY": 100, "BND": 0, BOND_SYM: BOND_FACE},
         "prices": {"SPY": 100.0, "BND": 100.0, BOND_SYM: BOND_MARK},
         "sec_types": {"SPY": "STK", "BND": "STK", BOND_SYM: "BOND"}},
        # A plain account in the same tier, so the block machinery is exercised too.
        {"account": "U7552751", "version": "Growth", "net_liq": 100_000.0,
         "positions": {}, "prices": {"SPY": 100.0, "BND": 100.0},
         "sec_types": {"SPY": "STK", "BND": "STK"}},
    ]
    out = eng.build_plan(inputs, {"Growth": target}, band_pct=0.03)
    bond_plan = next(p for p in out["plans"] if p.account == "U7552750")

    # A REAL plan for the managed sleeve — not a bench.
    assert bond_plan.needs_rebalance is True
    assert bond_plan.blocked is False
    # managed sleeve 100,000 -> investable 98,500 -> 492 sh each @ $100
    assert bond_plan.orders == {"SPY": 392, "BND": 492}
    # The bond is reported on the plan and absent from every block and route.
    assert [h.symbol for h in bond_plan.held_aside] == [BOND_SYM]
    assert all(b.symbol != BOND_SYM for b in out["blocks"])
    assert all(r.symbol != BOND_SYM for r in out["routes"])
    # ...and the two accounts' managed sleeves aggregate into shared blocks as normal.
    bnd = next(b for b in out["blocks"] if b.symbol == "BND")
    assert set(bnd.per_account) == {"U7552750", "U7552751"}
    assert sum(bnd.per_account.values()) == bnd.total_qty


def test_unpriceable_held_aside_holding_withholds_orders_and_says_why():
    """FAIL CLOSED on valuation: if a held-aside holding cannot be priced, the managed
    sleeve's size is a guess. The engine reports everything and emits nothing, with a named
    reason — it does not silently treat the holding as worth zero and over-invest."""
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    plan = eng.plan_account(
        "DU1000", "Balanced", 100_000.0, {"SPY": 0, "BONDX": 10_000}, target,
        band_pct=0.03, prices={"SPY": 100.0},
        sec_types={"SPY": "STK", "BONDX": "BOND"})      # no price, no value for BONDX
    assert plan.orders == {}
    assert plan.needs_rebalance is False
    assert plan.blocked is True
    assert any("could not be priced" in r for r in plan.blocked_reasons)
    # Still fully reported, not vanished.
    assert [h.symbol for h in plan.held_aside] == ["BONDX"]
    assert plan.held_aside[0].market_value is None


def test_held_aside_position_never_reaches_a_block_or_route():
    """Belt and braces on the safety property: a held-aside symbol cannot appear in ANY
    block or route, because it never becomes a reconcile line and so has no delta at all."""
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    inputs = [{"account": "DU1100", "version": "Growth",
               "net_liq": 100_000.0 + BOND_VALUE,
               "positions": {BOND_SYM: BOND_FACE},
               "prices": {"SPY": 100.0, BOND_SYM: BOND_MARK},
               "sec_types": {BOND_SYM: "BOND", "SPY": "STK"}}]
    out = eng.build_plan(inputs, {"Growth": target}, band_pct=0.03)
    every_symbol = ({b.symbol for b in out["blocks"]}
                    | {r.symbol for r in out["routes"]}
                    | set(out["plans"][0].orders))
    assert BOND_SYM not in every_symbol
    assert every_symbol == {"SPY"}


# ========================================================================================
# CROSS-MODEL PER-TICKER BLOCKS (owner decision 2026-09-03).
# The model decides how many shares each account needs; it has no business reaching the order
# layer. An API order is one contract and IBKR has no rebalance verb, so a rebalance is N
# block orders either way - and slicing per TICKER rather than per MODEL means ONE XLE order
# at ONE average price for every account in the book, instead of six.
# ========================================================================================
def _plan(account, version, orders):
    return recon_report.AccountPlan(
        account=account, version=version, net_liq=100000.0, reserve=1500.0,
        investable=98500.0, lines=[], needs_rebalance=bool(orders), orders=dict(orders))


def test_one_ticker_from_many_models_becomes_a_single_block():
    plans = [
        _plan("U1", "Growth (Custom)", {"XLE": 10}),
        _plan("U2", "Balanced (Custom)", {"XLE": 4}),
        _plan("U3", "Growth (Small, Custom)", {"XLE": 1}),
    ]
    blocks = eng.aggregate_blocks_by_ticker(plans)
    assert len(blocks) == 1
    b = blocks[0]
    assert (b.symbol, b.side, b.total_qty) == ("XLE", "BUY", 15)
    assert b.per_account == {"U1": 10, "U2": 4, "U3": 1}
    assert sum(b.per_account.values()) == b.total_qty
    assert b.version == eng.CROSS_MODEL_VERSION


def test_the_same_book_produces_far_fewer_blocks_than_per_model():
    """The whole point: per-model gives one order per model per ticker."""
    plans = [
        _plan("U1", "Growth (Custom)", {"XLE": 10, "XLF": 5}),
        _plan("U2", "Balanced (Custom)", {"XLE": 4, "XLF": 2}),
        _plan("U3", "Conservative (Custom)", {"XLE": 1, "XLF": 1}),
    ]
    per_model = eng.aggregate_blocks(plans)
    per_ticker = eng.aggregate_blocks_by_ticker(plans)
    assert len(per_model) == 6      # 3 models x 2 tickers
    assert len(per_ticker) == 2     # 2 tickers, everyone at one price
    assert {b.symbol for b in per_ticker} == {"XLE", "XLF"}


def test_buys_and_sells_of_one_symbol_stay_separate_blocks():
    plans = [
        _plan("U1", "Growth (Custom)", {"JAAA": -100}),
        _plan("U2", "Balanced (Custom)", {"JAAA": -50}),
        _plan("U3", "Growth (Custom)", {"FLOT": 30}),
    ]
    blocks = eng.aggregate_blocks_by_ticker(plans)
    by = {(b.symbol, b.side): b for b in blocks}
    assert by[("JAAA", "SELL")].total_qty == 150
    assert by[("JAAA", "SELL")].per_account == {"U1": 100, "U2": 50}
    assert by[("FLOT", "BUY")].total_qty == 30


def test_zero_deltas_never_create_a_block():
    blocks = eng.aggregate_blocks_by_ticker(
        [_plan("U1", "Growth (Custom)", {"XLE": 0, "XLF": 3})])
    assert [b.symbol for b in blocks] == ["XLF"]


def test_an_account_on_both_sides_of_one_symbol_is_refused():
    """Impossible from plan_account - one net delta per symbol - so it means plans from two
    runs were mixed, which would double-trade the account."""
    plans = [
        _plan("U1", "Growth (Custom)", {"XLE": 10}),
        _plan("U1", "Growth (Custom)", {"XLE": -3}),
    ]
    with pytest.raises(ValueError) as e:
        eng.aggregate_blocks_by_ticker(plans)
    assert "BOTH sides" in str(e.value)


def test_output_is_deterministic_and_sorted_by_symbol_then_side():
    plans = [_plan("U1", "G", {"XLV": 1, "XLB": 2}), _plan("U2", "B", {"XLB": -1})]
    blocks = eng.aggregate_blocks_by_ticker(plans)
    assert [(b.symbol, b.side) for b in blocks] == [
        ("XLB", "BUY"), ("XLB", "SELL"), ("XLV", "BUY")]


def test_the_existing_per_model_aggregator_is_unchanged():
    """aggregate_blocks must keep keying on version - nothing else may shift under it."""
    plans = [_plan("U1", "Growth (Custom)", {"XLE": 10}),
             _plan("U2", "Balanced (Custom)", {"XLE": 4})]
    blocks = eng.aggregate_blocks(plans)
    assert len(blocks) == 2
    assert {b.version for b in blocks} == {"Growth (Custom)", "Balanced (Custom)"}
