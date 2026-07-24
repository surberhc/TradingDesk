"""test_crm_rebalance.py — the CRM->desk per-SLEEVE bridge, PARITY-FIRST (#42/#43).

Per CLAUDE.md, this is ORDER-PATH-SENSITIVE architecture and the contract REQUIRES a parity
test written FIRST. The headline test proves the new per-sleeve target-sourcing path does not
drift from the existing whole-account engine for the degenerate single-sleeve case: an
account assigned a template that is 100% ONE S0 sleeve, whose sleeve-ledger attributed
positions equal the account's full holdings, must yield the SAME AccountPlan.orders, the SAME
aggregated blocks, and the SAME RoutePlans as calling rebalance_engine.build_plan the WHOLE-
account way with the full NetLiq and full positions.

Everything here is pure/offline — no broker, no gateway, no DB, no order object. Run:
    cd paperbot
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest -q test_crm_rebalance.py
"""
from __future__ import annotations

import pandas as pd
import pytest

import rebalance_engine
import strategy_target

import crm_rebalance
from crm_rebalance import crm_domain, crm_ledger, crm_brain

Template = crm_domain.Template
CRMBrain = crm_brain.CRMBrain
Instrument = crm_ledger.Instrument

# capability.AccountCapabilities — loaded through the same transient loader crm_rebalance uses.
_caps_mod = crm_rebalance._crm["capability"]
AccountCapabilities = _caps_mod.AccountCapabilities


# ===========================================================================
# Helpers — synthetic Targets, permissive caps, brains built in-memory
# ===========================================================================
def make_target(version: str, weights: dict, prices: dict) -> strategy_target.Target:
    """A synthetic strategy_target.Target (the sleeve's intra-sleeve asset allocation).
    Independent of the template sleeve WEIGHT, which scales capital, not asset mix."""
    return strategy_target.Target(
        weights=pd.Series(weights, dtype=float),
        prices=pd.Series(prices, dtype=float),
        as_of=pd.Timestamp("2026-07-24"),
        price_date=pd.Timestamp("2026-07-24"),
        version=version,
    )


def full_caps(account: str, net_liq: float = 5_000_000.0) -> "AccountCapabilities":
    """Capabilities that satisfy EVERY requirement, so even an overlay (S8) template can be
    assigned — lets us test that the bridge SKIPS the option sleeve rather than the gate
    blocking assignment. Buying power / excess liquidity set huge so no soft warning matters."""
    return AccountCapabilities(
        account_id=account, options_level=3, index_option_perm=True,
        is_margin=True, account_type="margin", net_liq=net_liq,
        buying_power=net_liq * 4, excess_liquidity=net_liq)


def brain_with(templates: dict) -> CRMBrain:
    return CRMBrain(templates)


def set_sleeve_positions(brain: CRMBrain, account: str, sleeve_id: str,
                         positions: dict) -> None:
    """Seed the sleeve ledger's attributed positions for (account, sleeve) directly (a raw
    position set, cash irrelevant to equity sizing)."""
    for sym, qty in positions.items():
        brain.ledger.apply_delta(account, sleeve_id, Instrument.stock(sym),
                                 qty_delta=qty, cash_delta=0.0)


# Shared registry sleeves (from the real crm domain registry).
BALANCED_100 = Template(template_id="balanced", name="Balanced ETF-only (100% S0-Balanced)",
                        weights={"S0-Balanced": 1.0})
SPLIT_75_25 = Template(template_id="split", name="S0 Balanced/Growth split",
                       weights={"S0-Balanced": 0.75, "S0-Growth": 0.25})
OVERLAY = Template(template_id="balanced_overlay", name="Balanced + S8 Overlay",
                   weights={"S0-Balanced": 0.75, "S8-Overlay": 0.25})


# ===========================================================================
# THE PARITY TEST — written and passing FIRST (the load-bearing safety proof)
# ===========================================================================
def test_parity_single_sleeve_equals_whole_account():
    """100%-one-S0-sleeve account: the per-sleeve bridge produces byte-identical orders,
    blocks, and routes to the whole-account engine call. This is the drift proof."""
    account = "DU8922142"
    net_liq = 1_000_000.0
    prices = {"SPY": 100.0, "AGG": 50.0}
    # Full account holdings == the sleeve's attributed positions (single-sleeve identity).
    full_positions = {"SPY": 5000.0, "AGG": 3000.0}
    target = make_target("Balanced", {"SPY": 0.6, "AGG": 0.4}, prices)
    targets = {"Balanced": target}

    # --- per-sleeve path (the bridge) ---
    brain = brain_with({"balanced": BALANCED_100})
    brain.assign(account, "balanced", full_caps(account), set_by="test")
    set_sleeve_positions(brain, account, "S0-Balanced", full_positions)
    sleeve_res = crm_rebalance.plan_from_crm(
        brain, {account: net_liq}, prices, targets)

    # --- whole-account path (the existing engine, sized against full NetLiq/positions) ---
    whole_res = rebalance_engine.build_plan(
        [{"account": account, "version": "Balanced", "net_liq": net_liq,
          "positions": full_positions, "prices": prices}],
        targets)

    # The account must actually breach the band, so this is a real (non-empty) parity proof.
    assert len(sleeve_res["plans"]) == 1
    assert sleeve_res["plans"][0].orders, "expected a breaching account (non-empty orders)"

    # Orders identical.
    assert sleeve_res["plans"][0].orders == whole_res["plans"][0].orders
    # Whole AccountPlan identical (net_liq, reserve, investable, lines, breach, orders).
    assert sleeve_res["plans"] == whole_res["plans"]
    # Aggregated blocks identical.
    assert sleeve_res["blocks"] == whole_res["blocks"]
    # RoutePlans identical (per-account share deltas + routing byte-identical).
    assert sleeve_res["routes"] == whole_res["routes"]

    # No option sleeve here.
    assert sleeve_res["skipped_option_sleeves"] == []
    assert sleeve_res["flags"] == []


# ===========================================================================
# Multi-ACCOUNT aggregation — two accounts, one S0 sleeve, one FA block
# ===========================================================================
def test_multi_account_aggregates_into_one_fa_block():
    """Two accounts each 100% S0-Balanced with breaching BUYs of the same symbol aggregate
    into ONE fa_block routed to tier_balanced, with the correct per_account_split."""
    prices = {"SPY": 100.0}
    target = make_target("Balanced", {"SPY": 1.0}, prices)
    targets = {"Balanced": target}

    a, b = "DU8922142", "DU8922143"
    net_liq = {a: 1_000_000.0, b: 500_000.0}

    brain = brain_with({"balanced": BALANCED_100})
    for acct in (a, b):
        brain.assign(acct, "balanced", full_caps(acct), set_by="test")
    # Both start flat -> full BUY -> both breach.

    res = crm_rebalance.plan_from_crm(brain, net_liq, prices, targets)

    # target_shares = int(1.0 * net_liq*(1-cash_reserve) / 100)
    import config
    buf = config.RISK_LIMITS["cash_reserve_pct"]
    qa = int(net_liq[a] * (1 - buf) / 100.0)
    qb = int(net_liq[b] * (1 - buf) / 100.0)

    assert len(res["routes"]) == 1
    route = res["routes"][0]
    assert route.route == "fa_block"
    assert route.version == "Balanced"
    assert route.symbol == "SPY"
    assert route.side == "BUY"
    assert route.fa_group == "tier_balanced"
    assert route.fa_method == ""            # group ContractsOrShares governs (Error 10226 guard)
    assert route.total_qty == qa + qb
    assert route.per_account_split == {a: qa, b: qb}


# ===========================================================================
# Option (S8) sleeve is SKIPPED — no route, lands in skipped_option_sleeves
# ===========================================================================
def test_option_sleeve_is_skipped_not_routed():
    """An overlay template mixes an equity sleeve (S0-Balanced) with an option sleeve
    (S8-Overlay). The equity sleeve routes; the option sleeve is SKIPPED into
    skipped_option_sleeves and produces no route/block."""
    account = "DU8922144"
    net_liq = 2_000_000.0
    prices = {"SPY": 100.0}
    target = make_target("Balanced", {"SPY": 1.0}, prices)
    targets = {"Balanced": target}

    brain = brain_with({"balanced_overlay": OVERLAY})
    brain.assign(account, "balanced_overlay", full_caps(account), set_by="test")

    extracted = crm_rebalance.sleeve_inputs_from_crm(brain, {account: net_liq}, prices)

    # Exactly one EQUITY input (S0-Balanced), sized at 75% of NetLiq.
    assert len(extracted["account_inputs"]) == 1
    eq = extracted["account_inputs"][0]
    assert eq["sleeve_id"] == "S0-Balanced"
    assert eq["net_liq"] == pytest.approx(net_liq * 0.75)

    # The S8 option sleeve is skipped, clearly labeled — never in the equity inputs.
    assert len(extracted["skipped_option_sleeves"]) == 1
    skipped = extracted["skipped_option_sleeves"][0]
    assert skipped["sleeve_id"] == "S8-Overlay"
    assert skipped["strategy_key"] == "s8_british_ic"
    assert skipped["group"] == "s8_overlay"

    # End-to-end: no route touches the option sleeve's group.
    res = crm_rebalance.plan_from_crm(brain, {account: net_liq}, prices, targets)
    assert all(r.fa_group != "s8_overlay" for r in res["routes"])
    assert len(res["skipped_option_sleeves"]) == 1


# ===========================================================================
# Sleeve capital sizing — net_liq x weight scales the sleeve target
# ===========================================================================
def test_fractional_sleeve_capital_scales_targets():
    """A 75/25 two-equity-sleeve template sizes each sleeve at net_liq x weight (§6 step 3),
    and the resulting per-sleeve target_shares scale with that capital."""
    account = "DU8922145"
    net_liq = 1_000_000.0
    prices = {"SPY": 100.0}
    bal_target = make_target("Balanced", {"SPY": 1.0}, prices)
    grw_target = make_target("Growth", {"SPY": 1.0}, prices)
    targets = {"Balanced": bal_target, "Growth": grw_target}

    brain = brain_with({"split": SPLIT_75_25})
    brain.assign(account, "split", full_caps(account), set_by="test")

    extracted = crm_rebalance.sleeve_inputs_from_crm(brain, {account: net_liq}, prices)
    by_sleeve = {i["sleeve_id"]: i for i in extracted["account_inputs"]}

    # Sleeve capital = net_liq x template_weight.
    assert by_sleeve["S0-Balanced"]["net_liq"] == pytest.approx(net_liq * 0.75)
    assert by_sleeve["S0-Growth"]["net_liq"] == pytest.approx(net_liq * 0.25)
    # Two distinct tiers -> two distinct versions -> group_map covers both.
    assert extracted["group_map"] == {"Balanced": "tier_balanced",
                                      "Growth": "tier_growth"}

    # Targets scale with capital: Balanced sleeve target_shares are 3x the Growth sleeve's
    # (0.75 vs 0.25), same price, so the per-sleeve deltas scale accordingly.
    res = crm_rebalance.plan_sleeve_rebalance(
        extracted["account_inputs"], targets, group_map=extracted["group_map"])
    buf = __import__("config").RISK_LIMITS["cash_reserve_pct"]
    exp_bal = int(net_liq * 0.75 * (1 - buf) / 100.0)
    exp_grw = int(net_liq * 0.25 * (1 - buf) / 100.0)
    orders = {p.version: p.orders for p in res["plans"]}
    assert orders["Balanced"]["SPY"] == exp_bal
    assert orders["Growth"]["SPY"] == exp_grw
    assert exp_bal == pytest.approx(3 * exp_grw, rel=1e-3)


# ===========================================================================
# group_map for S0 sleeves matches rebalance_engine.TIER_GROUPS
# ===========================================================================
def test_group_map_matches_tier_groups_for_s0():
    """The bridge's tier->group routing table equals rebalance_engine.TIER_GROUPS for the S0
    sleeves it emits — so passing our own group_map routes identically to the engine default."""
    prices = {"SPY": 100.0}
    target = make_target("Balanced", {"SPY": 1.0}, prices)
    grw = make_target("Growth", {"SPY": 1.0}, prices)

    brain = brain_with({"split": SPLIT_75_25})
    brain.assign("DU8922142", "split", full_caps("DU8922142"), set_by="test")
    extracted = crm_rebalance.sleeve_inputs_from_crm(
        brain, {"DU8922142": 1_000_000.0}, prices)

    for tier, group in extracted["group_map"].items():
        assert group == rebalance_engine.TIER_GROUPS[tier]


# ===========================================================================
# An account with no assignment produces nothing
# ===========================================================================
def test_unassigned_account_produces_nothing():
    """An account present in the NetLiq map but with NO current template assignment yields no
    inputs, no plans, no blocks, no routes."""
    prices = {"SPY": 100.0}
    targets = {"Balanced": make_target("Balanced", {"SPY": 1.0}, prices)}

    brain = brain_with({"balanced": BALANCED_100})   # no assign() call

    extracted = crm_rebalance.sleeve_inputs_from_crm(
        brain, {"DU8922142": 1_000_000.0}, prices)
    assert extracted["account_inputs"] == []
    assert extracted["skipped_option_sleeves"] == []

    res = crm_rebalance.plan_from_crm(brain, {"DU8922142": 1_000_000.0}, prices, targets)
    assert res["plans"] == []
    assert res["blocks"] == []
    assert res["routes"] == []


# ===========================================================================
# The loader did NOT clobber paperbot's own `ledger` module in sys.modules
# ===========================================================================
def test_loader_does_not_pollute_sys_modules_ledger():
    """Importing crm_rebalance must not leave crm's ledger registered as sys.modules['ledger']
    — paperbot's audit-trail `ledger` (imported by execution_engine et al.) must still be the
    one a bare `import ledger` resolves to."""
    import ledger as paperbot_ledger
    # crm's ledger has SleeveLedger/Instrument; paperbot's audit ledger does not.
    assert not hasattr(paperbot_ledger, "SleeveLedger")
    assert hasattr(paperbot_ledger, "log") or hasattr(paperbot_ledger, "record") \
        or "audit" in (paperbot_ledger.__doc__ or "").lower()
    # And the bridge's captured crm ledger IS the sleeve ledger.
    assert hasattr(crm_ledger, "SleeveLedger")
