"""Tests for group_rebalance — the PURE per-ticker group planner.

Owner decision 2026-09-03: one FA group per TICKER per run, never per model. An API order is
always one contract and IBKR has no rebalance verb, so a rebalance is N block orders either
way; per-ticker means every account in the run fills that ticker at ONE average price.
"""
from __future__ import annotations

import pytest

import group_rebalance as gr
import rebalance_engine as eng
import recon_report


def _plan(account, version, orders):
    return recon_report.AccountPlan(
        account=account, version=version, net_liq=100000.0, reserve=1500.0,
        investable=98500.0, lines=[], needs_rebalance=bool(orders), orders=dict(orders))


STAMP = "20260904-0931"


def test_accounts_from_different_models_land_in_one_group_per_ticker():
    plans = [
        _plan("U1", "Growth (Custom)", {"XLE": 10}),
        _plan("U2", "Balanced (Custom)", {"XLE": 4}),
        _plan("U3", "Growth (Small, Custom)", {"XLE": 1}),
    ]
    groups = gr.plan_ticker_groups(plans, run_stamp=STAMP)
    assert len(groups) == 1
    g = groups[0]
    assert (g.symbol, g.side, g.total_qty) == ("XLE", "BUY", 15)
    assert g.per_account == {"U1": 10, "U2": 4, "U3": 1}
    assert g.n_accounts == 3
    assert g.accounts == ("U1", "U2", "U3")
    assert g.group_name == "XLE BUY 20260904-0931"


def test_buy_and_sell_of_one_symbol_are_separate_groups_with_distinct_names():
    plans = [_plan("U1", "G", {"JAAA": -100}), _plan("U2", "B", {"JAAA": -50}),
             _plan("U3", "G", {"FLOT": 30})]
    groups = {(g.symbol, g.side): g for g in gr.plan_ticker_groups(plans, run_stamp=STAMP)}
    assert groups[("JAAA", "SELL")].group_name == "JAAA SELL 20260904-0931"
    assert groups[("FLOT", "BUY")].group_name == "FLOT BUY 20260904-0931"
    assert groups[("JAAA", "SELL")].total_qty == 150


def test_a_single_account_model_produces_a_one_account_group():
    """The staged rollout starts here: Conservative (Custom) has exactly 1 account."""
    groups = gr.plan_ticker_groups(
        [_plan("U101", "Conservative (Custom)", {"USFR": 40, "FLOT": 25, "JAAA": -30})],
        run_stamp=STAMP)
    assert len(groups) == 3
    assert all(g.n_accounts == 1 for g in groups)
    assert {g.side for g in groups} == {"BUY", "SELL"}


def test_widening_the_model_scope_widens_groups_it_does_not_multiply_orders():
    one = gr.plan_ticker_groups([_plan("U1", "Growth (Custom)", {"XLE": 10})],
                                run_stamp=STAMP)
    two = gr.plan_ticker_groups(
        [_plan("U1", "Growth (Custom)", {"XLE": 10}),
         _plan("U2", "Balanced (Custom)", {"XLE": 4})], run_stamp=STAMP)
    assert len(one) == len(two) == 1
    assert one[0].n_accounts == 1 and two[0].n_accounts == 2
    assert two[0].total_qty == 14


def test_est_notional_is_decoration_only_and_absent_without_prices():
    plans = [_plan("U1", "G", {"XLE": 10})]
    assert gr.plan_ticker_groups(plans, run_stamp=STAMP)[0].est_notional is None
    priced = gr.plan_ticker_groups(plans, run_stamp=STAMP, prices={"XLE": 65.0})[0]
    assert priced.est_notional == 650.0
    assert priced.per_account == {"U1": 10}


def test_zero_deltas_never_create_a_group():
    groups = gr.plan_ticker_groups([_plan("U1", "G", {"XLE": 0, "XLF": 3})],
                                   run_stamp=STAMP)
    assert [g.symbol for g in groups] == ["XLF"]


def test_a_run_stamp_is_mandatory():
    with pytest.raises(ValueError):
        gr.plan_ticker_groups([_plan("U1", "G", {"XLE": 1})], run_stamp="")


def test_two_runs_the_same_day_get_different_group_names():
    a = gr.plan_ticker_groups([_plan("U1", "G", {"XLE": 1})], run_stamp="20260904-0931")
    b = gr.plan_ticker_groups([_plan("U1", "G", {"XLE": 1})], run_stamp="20260904-1416")
    assert a[0].group_name != b[0].group_name


def test_the_split_always_sums_to_the_block_total():
    plans = [_plan(f"U{i}", "G", {"XLE": i}) for i in range(1, 8)]
    for g in gr.plan_ticker_groups(plans, run_stamp=STAMP):
        assert sum(g.per_account.values()) == g.total_qty


def test_an_account_on_both_sides_of_a_symbol_is_refused():
    with pytest.raises(ValueError):
        gr.plan_ticker_groups(
            [_plan("U1", "G", {"XLE": 10}), _plan("U1", "G", {"XLE": -3})],
            run_stamp=STAMP)


def test_summarize_reads_in_plain_english_and_never_raises_on_empty():
    assert "nothing in this scope needs to trade" in gr.summarize([])
    text = gr.summarize(gr.plan_ticker_groups(
        [_plan("U1", "G", {"XLE": 10}), _plan("U2", "B", {"XLE": 4, "JAAA": -20})],
        run_stamp=STAMP, prices={"XLE": 65.0, "JAAA": 50.5}))
    assert "2 block order(s) across 2 account(s)" in text
    assert "ONE average price" in text
    assert "XLE BUY 20260904-0931" in text


# ========================================================================================
# routes_from_group_plans - EVERY group is an fa_block route, including a one-account group.
# rebalance_engine.route_blocks sends a lone account down a "direct" route; under the
# 2026-09-03 owner decision the group IS the audit record, and the staged rollout STARTS with
# the single-account model, so that run must be recorded like every other.
# ========================================================================================
def test_every_route_is_an_fa_block_even_with_one_account():
    """Conservative (Custom) has exactly 1 account and is deliberately run FIRST."""
    groups = gr.plan_ticker_groups(
        [_plan("U101", "Conservative (Custom)", {"USFR": 40, "JAAA": -30})],
        run_stamp=STAMP)
    routes = gr.routes_from_group_plans(groups)
    assert len(routes) == 2
    assert all(r.route == "fa_block" for r in routes),         "a one-account group must NOT fall back to a direct order - it would be the one run "         "with no audit record, and it is the first run we make"
    assert all(r.account is None for r in routes), "a block order sets no single account"
    assert all(len(r.per_account_split) == 1 for r in routes)


def test_the_engine_would_have_routed_that_same_block_direct():
    """Pins the deliberate difference so nobody 'fixes' it back."""
    plans = [_plan("U101", "Conservative (Custom)", {"USFR": 40})]
    engine_routes = eng.route_blocks(eng.aggregate_blocks_by_ticker(plans))
    assert engine_routes[0].route == "direct"
    ours = gr.routes_from_group_plans(gr.plan_ticker_groups(plans, run_stamp=STAMP))
    assert ours[0].route == "fa_block"


def test_route_carries_the_run_group_name_and_empty_fa_method():
    routes = gr.routes_from_group_plans(
        gr.plan_ticker_groups([_plan("U1", "G", {"XLE": 10}), _plan("U2", "B", {"XLE": 4})],
                              run_stamp=STAMP))
    r = routes[0]
    assert r.fa_group == "XLE BUY 20260904-0931"
    assert r.fa_method == "", "an order-level faMethod is rejected by IBKR (Error 10226)"
    assert r.per_account_split == {"U1": 10, "U2": 4}
    assert r.total_qty == 14
    assert r.version == eng.CROSS_MODEL_VERSION


def test_split_still_sums_to_total_on_every_route():
    plans = [_plan(f"U{i}", "G", {"XLB": i, "XLE": -i}) for i in range(1, 6)]
    for r in gr.routes_from_group_plans(gr.plan_ticker_groups(plans, run_stamp=STAMP)):
        assert sum(r.per_account_split.values()) == r.total_qty


def test_sides_are_preserved_so_the_executor_can_phase_them():
    """The executor runs ALL sells, re-reads realized cash, then buys. It phases on side."""
    plans = [_plan("U1", "G", {"JAAA": -100, "FLOT": 99})]
    sides = {(r.symbol, r.side) for r in
             gr.routes_from_group_plans(gr.plan_ticker_groups(plans, run_stamp=STAMP))}
    assert sides == {("JAAA", "SELL"), ("FLOT", "BUY")}
