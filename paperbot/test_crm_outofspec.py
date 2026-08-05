"""test_crm_outofspec.py — the PURE whole-book out-of-spec assembly (no DB, no backtest).

Covers the CRM->engine input assembly, the funded/unfunded split, the leg extraction, and
the verdict mapping — all with synthetic data so nothing here touches the CRM or runs the
validated engine.
"""
from dataclasses import asdict
from types import SimpleNamespace

import pandas as pd

import crm_outofspec
import rebalance_engine
import strategy_target


def _row(num, aid, model="Growth", total=None, advisor="Andrew P Surber", entity="TFR"):
    return {"account_number": num, "account_id": aid, "model": model,
            "total_value": total, "advisor_name": advisor, "entity": entity,
            "master_name": "APS Ventures, LLC"}


def _hold(sym, qty, px, mv=None, cat="STK"):
    return {"symbol": sym, "quantity": qty, "mark_price": px, "asset_category": cat,
            "market_value": mv if mv is not None else qty * px}


def _bond(sym, face_qty, mark_per_100, mv=None):
    """An IBKR-style individual bond holdings row: FACE-value quantity, per-100 price."""
    return {"symbol": sym, "quantity": face_qty, "mark_price": mark_per_100,
            "asset_category": "BOND",
            "market_value": mv if mv is not None else face_qty * mark_per_100 / 100.0}


def _target(weights: dict, prices: dict, version="Growth"):
    """A synthetic strategy_target.Target so tests can run the REAL rebalance_engine with
    no backtest / broker."""
    return strategy_target.Target(
        weights=pd.Series(weights, dtype="float64"),
        prices=pd.Series(prices, dtype="float64"),
        as_of=pd.Timestamp("2026-08-03"),
        price_date=pd.Timestamp("2026-08-03"),
        version=version)


def test_account_inputs_map_number_positions_prices_and_netliq():
    rows = [_row("U111", "a1", total=100000)]
    holdings = {"a1": [_hold("SPY", 10, 500.0), _hold("BIL", 5, 91.0)]}
    inputs, skipped = crm_outofspec.account_inputs_from_roster(rows, holdings)
    assert skipped == []
    assert len(inputs) == 1
    ai = inputs[0]
    assert ai["account"] == "U111"          # CRM account_number == desk id
    assert ai["version"] == "Growth"
    assert ai["net_liq"] == 100000.0
    assert ai["positions"] == {"SPY": 10.0, "BIL": 5.0}
    assert ai["prices"] == {"SPY": 500.0, "BIL": 91.0}
    assert ai["n_positions"] == 2


def test_netliq_falls_back_to_holdings_value_when_total_missing():
    rows = [_row("U222", "a2", total=None)]
    holdings = {"a2": [_hold("SPY", 2, 100.0, mv=200.0)]}
    inputs, skipped = crm_outofspec.account_inputs_from_roster(rows, holdings)
    assert skipped == []
    assert inputs[0]["net_liq"] == 200.0


def test_unfunded_account_is_skipped_not_planned():
    rows = [_row("U333", "a3", total=None)]        # no total, no holdings
    inputs, skipped = crm_outofspec.account_inputs_from_roster(rows, {})
    assert inputs == []
    assert len(skipped) == 1
    assert skipped[0]["account"] == "U333"
    assert "unfunded" in skipped[0]["reason"]


def test_bad_price_dropped_but_position_kept():
    rows = [_row("U444", "a4", total=1000)]
    holdings = {"a4": [_hold("XYZ", 3, 0.0)]}       # non-positive mark -> price dropped
    inputs, _ = crm_outofspec.account_inputs_from_roster(rows, holdings)
    assert inputs[0]["positions"] == {"XYZ": 3.0}
    assert inputs[0]["prices"] == {}


def test_legs_from_orders_sells_first_then_buys():
    legs = crm_outofspec._legs_from_orders({"AAA": 5, "BBB": -3, "CCC": 0})
    assert legs == [
        {"symbol": "BBB", "side": "SELL", "shares": 3},
        {"symbol": "AAA", "side": "BUY", "shares": 5},
    ]


def test_verdicts_from_plans_reads_breach_and_legs():
    inputs = [
        {"account": "U111", "version": "Growth", "advisor_name": "Andrew P Surber",
         "entity": "TFR", "master_name": "M", "net_liq": 100000, "n_positions": 2},
        {"account": "U222", "version": "Growth", "advisor_name": "Andrew P Surber",
         "entity": "TFR", "master_name": "M", "net_liq": 50000, "n_positions": 1},
    ]
    plans = [
        SimpleNamespace(account="U111", net_liq=100000.0, needs_rebalance=True,
                        orders={"SPY": 4, "BIL": -2}, alien_lines=[]),
        SimpleNamespace(account="U222", net_liq=50000.0, needs_rebalance=False,
                        orders={}, alien_lines=[object()]),
    ]
    verdicts = crm_outofspec.verdicts_from_plans(plans, inputs)
    by = {v["account"]: v for v in verdicts}
    assert by["U111"]["out_of_spec"] is True
    assert by["U111"]["n_legs"] == 2
    assert by["U222"]["out_of_spec"] is False
    assert by["U222"]["n_legs"] == 0
    assert by["U222"]["n_alien"] == 1
    # out-of-spec sorts first
    assert verdicts[0]["account"] == "U111"


def test_scan_runs_pure_engine_via_injected_targets(monkeypatch):
    """scan_out_of_spec must call the UNCHANGED rebalance_engine.build_plan and map its plans;
    we stub build_plan so the test needs no backtest, and assert the wiring."""
    captured = {}

    def fake_build_plan(account_inputs, targets, band_pct=None, universe=None):
        captured["account_inputs"] = account_inputs
        captured["targets"] = targets
        plans = [SimpleNamespace(account=ai["account"], net_liq=ai["net_liq"],
                                 needs_rebalance=(ai["account"] == "U111"),
                                 orders={"SPY": 1} if ai["account"] == "U111" else {},
                                 alien_lines=[]) for ai in account_inputs]
        return {"plans": plans, "blocks": [], "routes": []}

    monkeypatch.setattr(crm_outofspec.rebalance_engine, "build_plan", fake_build_plan)
    rows = [_row("U111", "a1", total=100000), _row("U222", "a2", total=50000)]
    holdings = {"a1": [_hold("SPY", 10, 500.0)], "a2": [_hold("SPY", 5, 500.0)]}
    out = crm_outofspec.scan_out_of_spec(rows, holdings, {"Growth": object()})
    assert out["n_accounts"] == 2
    assert out["n_out_of_spec"] == 1
    assert out["n_in_spec"] == 1
    assert captured["targets"] == {"Growth": object} or "Growth" in captured["targets"]


# --------------------------------------------------------------------------- #
# BOND handling (order-affecting correctness, 2026-08-05)                       #
# IBKR carries a bond with a FACE-value quantity and a per-100 price, so the    #
# engine's qty*mark valuation overstates it ~100x and can't route it. Bonds     #
# are valued qty*mark/100 into NAV, kept OUT of the engine, and flagged for     #
# manual liquidation. Non-bond accounts must be byte-identical to before.       #
# --------------------------------------------------------------------------- #
def test_bond_valuation_uses_per_100_not_qty_times_mark():
    # A single bond: 10,000 face @ 100.146 per-100 => true value 10,014.63 (NOT 1,001,462.88).
    rows = [_row("U1", "a1", total=None)]                # force NAV = holdings value
    holdings = {"a1": [_bond("797843BE8 4.6 08/01/34", 10000, 100.14628819)]}
    inputs, skipped = crm_outofspec.account_inputs_from_roster(rows, holdings)
    assert skipped == []
    ai = inputs[0]
    # NAV is the corrected bond value, not the ~100x-inflated qty*mark.
    assert round(ai["net_liq"], 2) == 10014.63
    assert ai["net_liq"] != 10000 * 100.14628819         # would be the phantom value
    # The bond is held OUT of the engine's positions/prices (it can't be routed as equity).
    assert ai["positions"] == {}
    assert ai["prices"] == {}
    # ...and surfaced on `bonds`, valued correctly.
    assert len(ai["bonds"]) == 1
    assert ai["bonds"][0]["symbol"] == "797843BE8 4.6 08/01/34"
    assert round(ai["bonds"][0]["value"], 2) == 10014.63


def test_bond_legs_excluded_and_flagged_manual_liquidation():
    rows = [_row("U1", "a1", total=25035.55)]
    holdings = {"a1": [_bond("806721GU4 5 12/01/31", 15000, 100.13943782),
                       _bond("797843BE8 4.6 08/01/34", 10000, 100.14628819)]}
    inputs, _ = crm_outofspec.account_inputs_from_roster(rows, holdings)
    # Model wants only ETFs; the account holds only bonds -> no equity legs, bonds flagged.
    target = _target({"SPY": 1.0}, {"SPY": 500.0})
    result = rebalance_engine.build_plan(inputs, {"Growth": target})
    verdicts = crm_outofspec.verdicts_from_plans(result["plans"], inputs)
    v = verdicts[0]
    assert v["out_of_spec"] is True          # bonds require manual action
    assert v["n_bonds"] == 2
    assert all(b["action"] == "manual liquidation required (bond)" for b in v["bonds"])
    # No auto-generated leg references a bond symbol.
    leg_syms = {l["symbol"] for l in v["legs"]}
    assert leg_syms.isdisjoint({"806721GU4 5 12/01/31", "797843BE8 4.6 08/01/34"})


def test_mixed_bond_and_etf_sizes_etf_off_corrected_nav_bond_excluded():
    # total_value is the corrected NAV (includes the bond's true value). The ETF leg must
    # size off that NAV, and must be IDENTICAL whether or not the (excluded) bond is present.
    target = _target({"SPY": 1.0}, {"SPY": 500.0})
    etf_only = [{"account": "U1", "version": "Growth", "net_liq": 300000.0,
                 "positions": {"SPY": 1.0}, "prices": {"SPY": 500.0}, "bonds": []}]
    rows = [_row("U1", "a1", total=300000.0)]
    holdings = {"a1": [_hold("SPY", 1, 500.0),
                       _bond("235308RA3 6.45 02/15/35", 20000, 101.49008974)]}
    mixed, _ = crm_outofspec.account_inputs_from_roster(rows, holdings)
    # The ETF sizing (target_shares/orders) is unchanged by the excluded bond.
    p_etf = rebalance_engine.build_plan(etf_only, {"Growth": target})["plans"][0]
    p_mixed = rebalance_engine.build_plan(mixed, {"Growth": target})["plans"][0]
    spy_etf = next(l for l in p_etf.lines if l.symbol == "SPY")
    spy_mixed = next(l for l in p_mixed.lines if l.symbol == "SPY")
    assert spy_mixed.target_shares == spy_etf.target_shares
    assert p_mixed.orders.get("SPY") == p_etf.orders.get("SPY")
    # The bond never becomes an equity position/leg.
    assert "235308RA3 6.45 02/15/35" not in p_mixed.orders
    assert len(mixed[0]["bonds"]) == 1


def test_etf_only_account_plan_is_byte_identical_to_pre_bond_input():
    """Characterization: an ETF/stock-only account produces a plan byte-identical to the
    same input WITHOUT the new `bonds` key — proving the bond code path is inert for
    non-bond accounts (the engine's plan is unchanged)."""
    target = _target({"SPY": 0.6, "BIL": 0.4}, {"SPY": 500.0, "BIL": 91.0})
    rows = [_row("U1", "a1", total=100000.0)]
    holdings = {"a1": [_hold("SPY", 10, 500.0), _hold("BIL", 5, 91.0)]}
    produced, _ = crm_outofspec.account_inputs_from_roster(rows, holdings)
    # The engine-relevant inputs match the legacy shape exactly.
    ai = produced[0]
    assert ai["positions"] == {"SPY": 10.0, "BIL": 5.0}
    assert ai["prices"] == {"SPY": 500.0, "BIL": 91.0}
    assert ai["net_liq"] == 100000.0
    assert ai["bonds"] == []
    # Legacy input = the produced one minus the additive `bonds` key.
    legacy = {k: v for k, v in ai.items() if k != "bonds"}
    plan_new = rebalance_engine.build_plan([ai], {"Growth": target})
    plan_legacy = rebalance_engine.build_plan([legacy], {"Growth": target})
    assert [asdict(p) for p in plan_new["plans"]] == [asdict(p) for p in plan_legacy["plans"]]
    assert [asdict(b) for b in plan_new["blocks"]] == [asdict(b) for b in plan_legacy["blocks"]]
