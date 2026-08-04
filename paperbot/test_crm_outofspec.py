"""test_crm_outofspec.py — the PURE whole-book out-of-spec assembly (no DB, no backtest).

Covers the CRM->engine input assembly, the funded/unfunded split, the leg extraction, and
the verdict mapping — all with synthetic data so nothing here touches the CRM or runs the
validated engine.
"""
from types import SimpleNamespace

import crm_outofspec


def _row(num, aid, model="Growth", total=None, advisor="Andrew P Surber", entity="TFR"):
    return {"account_number": num, "account_id": aid, "model": model,
            "total_value": total, "advisor_name": advisor, "entity": entity,
            "master_name": "APS Ventures, LLC"}


def _hold(sym, qty, px, mv=None):
    return {"symbol": sym, "quantity": qty, "mark_price": px,
            "market_value": mv if mv is not None else qty * px}


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
