"""test_crm_outofspec.py — the PURE whole-book out-of-spec assembly (no DB, no backtest).

Covers the CRM->engine input assembly, the funded/unfunded split, the leg extraction, and
the verdict mapping — all with synthetic data so nothing here touches the CRM or runs the
validated engine.
"""
from dataclasses import asdict
from types import SimpleNamespace

import pandas as pd
import pytest

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
    inputs, skipped, excluded = crm_outofspec.account_inputs_from_roster(rows, holdings)
    assert skipped == []
    assert excluded == []
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
    inputs, skipped, excluded = crm_outofspec.account_inputs_from_roster(rows, holdings)
    assert skipped == []
    assert excluded == []
    assert inputs[0]["net_liq"] == 200.0


def test_unfunded_account_is_skipped_not_planned():
    rows = [_row("U333", "a3", total=None)]        # no total, no holdings
    inputs, skipped, excluded = crm_outofspec.account_inputs_from_roster(rows, {})
    assert inputs == []
    assert excluded == []
    assert len(skipped) == 1
    assert skipped[0]["account"] == "U333"
    assert "unfunded" in skipped[0]["reason"]


def test_bad_price_dropped_but_position_kept():
    rows = [_row("U444", "a4", total=1000)]
    holdings = {"a4": [_hold("XYZ", 3, 0.0)]}       # non-positive mark -> price dropped
    inputs, _, _ = crm_outofspec.account_inputs_from_roster(rows, holdings)
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
# HELD-ASIDE handling (owner decision, 2026-08-19)                              #
# Individual bonds are on a NO-TRADE list: priced, counted, reported, never      #
# traded — and they sit OUTSIDE the target allocation, so the model applies to   #
# the remaining sleeve as its own 100%. A bond-holding account is NO LONGER      #
# benched; it rebalances its non-bond sleeve normally. (This replaces the older  #
# "bond => manual liquidation required" treatment.) IBKR carries a bond with a   #
# FACE-value quantity and a per-100 price, so it is valued qty*mark/100.         #
# Non-bond accounts must stay byte-identical.                                    #
# --------------------------------------------------------------------------- #
def test_bond_valuation_uses_per_100_not_qty_times_mark():
    # A single bond: 10,000 face @ 100.146 per-100 => true value 10,014.63 (NOT 1,001,462.88).
    rows = [_row("U1", "a1", total=None)]                # force NAV = holdings value
    holdings = {"a1": [_bond("797843BE8 4.6 08/01/34", 10000, 100.14628819)]}
    inputs, skipped, excluded = crm_outofspec.account_inputs_from_roster(rows, holdings)
    assert skipped == []
    assert excluded == []
    ai = inputs[0]
    # NAV is the corrected bond value, not the ~100x-inflated qty*mark.
    assert round(ai["net_liq"], 2) == 10014.63
    assert ai["net_liq"] != 10000 * 100.14628819         # would be the phantom value
    # The bond IS handed to the engine, but tagged BOND and priced explicitly — it is the
    # engine that carves it out, so no caller can forget to.
    assert ai["positions"] == {"797843BE8 4.6 08/01/34": 10000.0}
    assert ai["sec_types"] == {"797843BE8 4.6 08/01/34": "BOND"}
    assert round(ai["values"]["797843BE8 4.6 08/01/34"], 2) == 10014.63
    assert ai["prices"] == {}                            # never sized -> never priced for sizing
    assert ai["n_positions"] == 0                        # no MANAGED positions
    assert ai["n_held_aside"] == 1


def test_bond_is_held_aside_reported_and_never_a_leg():
    # NetLiq is exactly the two bonds' true (per-100) value: an all-bond account.
    total = 15000 * 100.13943782 / 100.0 + 10000 * 100.14628819 / 100.0
    rows = [_row("U1", "a1", total=total)]
    holdings = {"a1": [_bond("806721GU4 5 12/01/31", 15000, 100.13943782),
                       _bond("797843BE8 4.6 08/01/34", 10000, 100.14628819)]}
    inputs, _, _ = crm_outofspec.account_inputs_from_roster(rows, holdings)
    # Model wants only ETFs; the account holds ONLY bonds -> nothing to trade at all.
    target = _target({"SPY": 1.0}, {"SPY": 500.0})
    result = rebalance_engine.build_plan(inputs, {"Growth": target})
    verdicts = crm_outofspec.verdicts_from_plans(result["plans"], inputs)
    v = verdicts[0]
    # Both bonds are reported in full — symbol, quantity, market value, reason.
    assert v["n_held_aside"] == 2
    assert {h["symbol"] for h in v["held_aside"]} == {"806721GU4 5 12/01/31",
                                                      "797843BE8 4.6 08/01/34"}
    assert all(h["sec_type"] == "BOND" for h in v["held_aside"])
    assert all(h["market_value"] > 0 for h in v["held_aside"])
    assert all("never traded" in h["reason"] for h in v["held_aside"])
    assert v["n_unclassified"] == 0
    # ...and the whole account is accounted for: managed sleeve + held aside == NetLiq.
    assert v["held_aside_value"] == pytest.approx(total)
    assert v["managed_net_liq"] == pytest.approx(0.0)
    assert v["managed_net_liq"] + v["held_aside_value"] == pytest.approx(v["net_liq"])
    # No leg anywhere references a bond, and holding one is NOT itself out-of-spec.
    assert v["n_legs"] == 0
    assert v["out_of_spec"] is False
    assert v["blocked"] is False


def test_bond_account_rebalances_its_managed_sleeve_instead_of_being_benched():
    """THE BEHAVIOR CHANGE: an account holding an individual bond used to be surfaced as
    'manual liquidation required' and held out of the batch. It must now show a REAL,
    actionable out-of-spec figure for its managed sleeve."""
    target = _target({"SPY": 1.0}, {"SPY": 500.0})
    rows = [_row("U7552750", "a1", total=110014.63)]
    holdings = {"a1": [_hold("SPY", 20, 500.0),                       # $10,000 of a $100k sleeve
                       _bond("797843BE8 4.6 08/01/34", 10000, 100.14628819)]}
    inputs, _, _ = crm_outofspec.account_inputs_from_roster(rows, holdings)
    result = rebalance_engine.build_plan(inputs, {"Growth": target})
    v = crm_outofspec.verdicts_from_plans(result["plans"], inputs)[0]

    assert v["out_of_spec"] is True
    assert v["n_legs"] == 1                       # a real, routable managed-sleeve leg
    assert v["legs"][0]["symbol"] == "SPY"
    assert v["legs"][0]["side"] == "BUY"
    # Managed sleeve ~100,000 -> investable 98,500 -> 197 sh; holds 20 -> BUY 177.
    assert v["legs"][0]["shares"] == 177
    assert round(v["managed_net_liq"], 2) == 100000.0
    assert v["n_held_aside"] == 1
    assert v["blocked"] is False


def test_managed_sleeve_sizes_as_its_own_100pct_ignoring_the_bond():
    """Bonds sit OUTSIDE the allocation: a $100k managed sleeve sizes identically whether or
    not the account also holds a bond. The bond is NOT the client's fixed-income sleeve."""
    target = _target({"SPY": 1.0}, {"SPY": 500.0})
    etf_only = [{"account": "U1", "version": "Growth", "net_liq": 100000.0,
                 "positions": {"SPY": 1.0}, "prices": {"SPY": 500.0}}]
    rows = [_row("U1", "a1", total=100000.0 + 20000 * 101.49008974 / 100.0)]
    holdings = {"a1": [_hold("SPY", 1, 500.0),
                       _bond("235308RA3 6.45 02/15/35", 20000, 101.49008974)]}
    mixed, _, _ = crm_outofspec.account_inputs_from_roster(rows, holdings)
    p_etf = rebalance_engine.build_plan(etf_only, {"Growth": target})["plans"][0]
    p_mixed = rebalance_engine.build_plan(mixed, {"Growth": target})["plans"][0]
    spy_etf = next(l for l in p_etf.lines if l.symbol == "SPY")
    spy_mixed = next(l for l in p_mixed.lines if l.symbol == "SPY")
    assert spy_mixed.target_shares == spy_etf.target_shares
    assert p_mixed.orders.get("SPY") == p_etf.orders.get("SPY")
    # The bond never becomes an equity position/leg, and never a reconcile line.
    assert "235308RA3 6.45 02/15/35" not in p_mixed.orders
    assert "235308RA3 6.45 02/15/35" not in {l.symbol for l in p_mixed.lines}
    assert [h.symbol for h in p_mixed.held_aside] == ["235308RA3 6.45 02/15/35"]


def test_unknown_asset_category_is_held_aside_and_flagged_for_classification():
    """FAIL CLOSED on an asset_category the desk does not recognise: held aside, reported as
    needing classification, and never traded."""
    target = _target({"SPY": 1.0}, {"SPY": 500.0})
    rows = [_row("U1", "a1", total=110000.0)]
    holdings = {"a1": [_hold("SPY", 20, 500.0),
                       _hold("WHATSIT", 4, 2500.0, cat="")]}     # blank category
    inputs, _, _ = crm_outofspec.account_inputs_from_roster(rows, holdings)
    assert inputs[0]["sec_types"]["WHATSIT"] == "UNKNOWN"
    result = rebalance_engine.build_plan(inputs, {"Growth": target})
    v = crm_outofspec.verdicts_from_plans(result["plans"], inputs)[0]
    assert v["n_unclassified"] == 1
    assert [h["symbol"] for h in v["held_aside"]] == ["WHATSIT"]
    assert all(l["symbol"] != "WHATSIT" for l in v["legs"])


def test_etf_only_account_plan_is_byte_identical_to_pre_carve_out_input():
    """Characterization: an ETF/stock-only account produces a plan byte-identical to the
    same input WITHOUT the additive held-aside keys — proving the carve-out code path is
    inert for accounts with nothing held aside (the engine's plan is unchanged)."""
    target = _target({"SPY": 0.6, "BIL": 0.4}, {"SPY": 500.0, "BIL": 91.0})
    rows = [_row("U1", "a1", total=100000.0)]
    holdings = {"a1": [_hold("SPY", 10, 500.0), _hold("BIL", 5, 91.0)]}
    produced, _, _ = crm_outofspec.account_inputs_from_roster(rows, holdings)
    # The engine-relevant inputs match the legacy shape exactly.
    ai = produced[0]
    assert ai["positions"] == {"SPY": 10.0, "BIL": 5.0}
    assert ai["prices"] == {"SPY": 500.0, "BIL": 91.0}
    assert ai["net_liq"] == 100000.0
    assert ai["sec_types"] == {"SPY": "STK", "BIL": "STK"}
    assert ai["values"] == {}
    assert ai["n_held_aside"] == 0
    # Legacy input = the produced one minus the additive classification keys.
    legacy = {k: v for k, v in ai.items() if k not in ("sec_types", "values")}
    plan_new = rebalance_engine.build_plan([ai], {"Growth": target})
    plan_legacy = rebalance_engine.build_plan([legacy], {"Growth": target})
    assert [asdict(p) for p in plan_new["plans"]] == [asdict(p) for p in plan_legacy["plans"]]
    assert [asdict(b) for b in plan_new["blocks"]] == [asdict(b) for b in plan_legacy["blocks"]]


def test_unpriceable_bond_blocks_orders_with_a_named_reason():
    """FAIL CLOSED on valuation: a bond with no mark and no reported value cannot be carved
    out, so the account emits nothing — but is surfaced, with the reason, not dropped."""
    target = _target({"SPY": 1.0}, {"SPY": 500.0})
    rows = [_row("U1", "a1", total=110000.0)]
    holdings = {"a1": [_hold("SPY", 20, 500.0),
                       _bond("BADBOND", 10000, 0.0, mv=0.0)]}      # no mark, no value
    inputs, _, _ = crm_outofspec.account_inputs_from_roster(rows, holdings)
    assert "BADBOND" not in inputs[0]["values"]
    result = rebalance_engine.build_plan(inputs, {"Growth": target})
    v = crm_outofspec.verdicts_from_plans(result["plans"], inputs)[0]
    assert v["blocked"] is True
    assert v["n_legs"] == 0
    assert v["out_of_spec"] is True                       # surfaced for a human, not hidden
    assert any("could not be priced" in r for r in v["blocked_reasons"])


# --------------------------------------------------------------------------- #
# NAV / holdings-value MISMATCH fail-safe (pre-arm exclusion, 2026-08-05).      #
# An account whose recorded NAV (total_value) is wildly BELOW the holdings+cash  #
# it should reflect is EXCLUDED from the engine run so it can never be sized or  #
# armed off a bad NAV (a departed/closed account with a now-stale holdings       #
# snapshot, a feed dropout, or any data desync). It is ONE-SIDED: total_value >  #
# holdings is the NORMAL cash cushion and must NOT be flagged. Mirrors the two   #
# real departed APS Ventures accounts (total_value ~0.1% of ~$836k / ~$195k).    #
# --------------------------------------------------------------------------- #
def test_nav_mismatch_departed_account_is_flagged_and_excluded():
    # U7349572 shape: recorded NAV $1,073 vs ~$836,622 of (stale) holdings.
    rows = [_row("U7349572", "dep1", total=1072.82)]
    holdings = {"dep1": [_hold("VTI", 3000, 278.874)]}   # ~$836,622 market value
    inputs, skipped, excluded = crm_outofspec.account_inputs_from_roster(rows, holdings)
    # Held OUT of the engine entirely — never sized, never armed.
    assert inputs == []
    assert skipped == []
    assert len(excluded) == 1
    ex = excluded[0]
    assert ex["account"] == "U7349572"
    assert ex["total_value"] == 1072.82
    assert round(ex["holdings_value"], 0) == 836622
    assert "manual review" in ex["reason"]
    assert "NAV/holdings mismatch" in ex["reason"]


def test_nav_mismatch_cash_heavy_account_is_NOT_flagged():
    # U20073052 shape: total_value $100,153 > $29,993 holdings — legit cash cushion, NOT a
    # mismatch. (cash_balance absent in the roster row, so the reference is holdings alone;
    # total_value > holdings is never flagged.)
    rows = [_row("U20073052", "cash1", total=100152.59)]
    holdings = {"cash1": [_hold("SPY", 60, 499.88)]}     # ~$29,993 market value
    inputs, skipped, excluded = crm_outofspec.account_inputs_from_roster(rows, holdings)
    assert excluded == []
    assert skipped == []
    assert len(inputs) == 1
    assert inputs[0]["net_liq"] == 100152.59             # sizes off the full (correct) NAV


def test_nav_mismatch_normal_account_is_unaffected():
    # total_value == holdings sum exactly (no cash): reference == holdings, ratio 1.0, fine.
    rows = [_row("U111", "n1", total=100000.0)]
    holdings = {"n1": [_hold("SPY", 200, 500.0)]}        # exactly $100,000
    inputs, _, excluded = crm_outofspec.account_inputs_from_roster(rows, holdings)
    assert excluded == []
    assert len(inputs) == 1
    assert inputs[0]["net_liq"] == 100000.0


def test_nav_mismatch_uses_cash_when_present_in_row():
    # If cash_balance IS supplied on the row, the reference is holdings+cash. An account that
    # is mostly cash (small holdings) with a healthy total_value is NOT flagged.
    rows = [{**_row("U555", "c5", total=100000.0), "cash_balance": 90000.0}]
    holdings = {"c5": [_hold("SPY", 20, 500.0)]}         # $10,000 holdings + $90k cash = $100k
    inputs, _, excluded = crm_outofspec.account_inputs_from_roster(rows, holdings)
    assert excluded == []
    assert len(inputs) == 1


def test_nav_mismatch_zero_total_value_with_large_stale_holdings_is_excluded():
    # The dangerous fallback case: total_value 0 would previously be sized off the (stale)
    # holdings sum. The guard catches it AHEAD of that fallback and excludes it.
    rows = [_row("U999", "z9", total=0.0)]
    holdings = {"z9": [_hold("VTI", 1000, 278.0)]}       # $278,000 stale holdings
    inputs, skipped, excluded = crm_outofspec.account_inputs_from_roster(rows, holdings)
    assert inputs == []
    assert skipped == []                                  # NOT the unfunded path
    assert len(excluded) == 1
    assert excluded[0]["account"] == "U999"


def test_nav_mismatch_tiny_account_below_reference_floor_not_flagged():
    # A sub-$1k reference is immaterial (snapshot noise) — not worth a manual-review exclusion.
    rows = [_row("U777", "t7", total=1.0)]
    holdings = {"t7": [_hold("SPY", 1, 500.0)]}          # $500 reference < $1,000 floor
    inputs, skipped, excluded = crm_outofspec.account_inputs_from_roster(rows, holdings)
    assert excluded == []
    # falls through to normal sizing off the small total_value/holdings.
    assert len(inputs) == 1


def test_scan_excludes_mismatch_account_from_verdicts_and_surfaces_it(monkeypatch):
    """scan_out_of_spec must NEVER size/arm a mismatched account: it is absent from verdicts
    and surfaced on `excluded` with n_excluded. A healthy account still runs."""
    def fake_build_plan(account_inputs, targets, band_pct=None, universe=None):
        plans = [SimpleNamespace(account=ai["account"], net_liq=ai["net_liq"],
                                 needs_rebalance=False, orders={}, alien_lines=[])
                 for ai in account_inputs]
        return {"plans": plans, "blocks": [], "routes": []}

    monkeypatch.setattr(crm_outofspec.rebalance_engine, "build_plan", fake_build_plan)
    rows = [_row("U7349572", "dep1", total=1072.82),     # departed -> excluded
            _row("U111", "ok1", total=100000.0)]         # healthy -> scanned
    holdings = {"dep1": [_hold("VTI", 3000, 278.874)],
                "ok1": [_hold("SPY", 200, 500.0)]}
    out = crm_outofspec.scan_out_of_spec(rows, holdings, {"Growth": object()})
    assert out["n_accounts"] == 1                         # only the healthy account sized
    assert out["n_excluded"] == 1
    assert [e["account"] for e in out["excluded"]] == ["U7349572"]
    assert all(v["account"] != "U7349572" for v in out["verdicts"])
