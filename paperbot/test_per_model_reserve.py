"""test_per_model_reserve.py — the CASH RESERVE is PER MODEL: 1% for an Andrew-authored
custom allocation, 1.5% (unchanged, validated) for S0 and everything else.

WHY THE RESERVE EXISTS AT ALL, because it decides what these tests must prove. IBKR deducts
its advisory fee from account CASH, and client distributions are paid from cash. An account
that is 100% invested is overdrawn the moment a fee posts — that is the 2026-07-28
negative-balance incident. So the reserve is REAL uninvested cash, not a display line, and
"the account reserves 1%" means the sizing actually leaves 1% of NAV in cash.

THE CORRECTNESS RISK THIS FILE EXISTS FOR. The reserve is read at TWO kinds of site:
  * SIZING      — investable = (NAV - distribution_reserve) * (1 - reserve)
  * MEASUREMENT — the synthetic CASH bucket's TARGET weight, i.e. the drift readout
If a custom account is SIZED against 1% but MEASURED against 1.5%, it holds exactly what it
was told to hold and still reads 0.5% adrift on cash forever. It never reconciles, the
readouts call it out-of-spec every day, and any surface that acts on drift churns it. So
every test below that checks sizing ALSO checks the drift line, and vice versa.

SYNTHETIC ONLY. No broker, no gateway, no real CRM, no order. The one end-to-end test drives
the batch executor's PREVIEW path against a fake read-only broker.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_per_model_reserve.py -q
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pandas as pd
import pytest

import batch_rebalance_execute as bre
import config
import crm_roster
import custom_target
import execution_engine
import investable
import rebalance_engine as eng
import recon_report
import reconcile
import risk_manager
import strategy_target

# Reuse the batch executor's existing synthetic fixtures rather than forking a second set
# that could drift from them (the fake broker, the CRM stub, the allocation-row shape).
from test_batch_rebalance_execute import (
    CUSTOM_ACCT, CUSTOM_LABEL, _FakeIB, _FakePosition, _custom_target_and_meta, _fake_crm,
    _row,
)

GLOBAL_RESERVE = 0.015      # config.RISK_LIMITS["cash_reserve_pct"] — S0's, unchanged
CUSTOM_RESERVE = 0.01       # config.RISK_LIMITS["custom_allocation_cash_reserve_pct"]

NAV = 1_000_000.0
PRICE = 100.0
S0_LABEL = "Balanced"


def _one_asset_target(version: str) -> strategy_target.Target:
    """A 100%-SPY book at $100 — deliberately trivial so every number below is exact and a
    reader can check the arithmetic by hand."""
    return strategy_target.Target(
        weights=pd.Series({"SPY": 1.0}, dtype="float64"),
        prices=pd.Series({"SPY": PRICE}, dtype="float64"),
        as_of=pd.Timestamp("2026-08-25"), price_date=pd.Timestamp("2026-08-25"),
        version=version)


def _cash_line(plan_or_lines):
    lines = getattr(plan_or_lines, "lines", plan_or_lines)
    cash = [ln for ln in lines if ln.symbol == investable.CASH_SYMBOL]
    assert len(cash) == 1, "exactly one synthetic CASH bucket per reconcile"
    return cash[0]


# =====================================================================================
# 1. The knobs themselves: two distinct values, both read from config, neither hardcoded.
# =====================================================================================
def test_the_two_reserves_are_distinct_and_come_from_config():
    assert config.RISK_LIMITS["cash_reserve_pct"] == GLOBAL_RESERVE
    assert config.RISK_LIMITS[investable.CUSTOM_RESERVE_KEY] == CUSTOM_RESERVE
    assert investable.buffer_pct() == GLOBAL_RESERVE
    assert investable.custom_buffer_pct() == CUSTOM_RESERVE
    # The whole point: they are NOT the same number, so any test that passes with one and
    # fails with the other is genuinely discriminating.
    assert investable.buffer_pct_for(is_custom=True) != investable.buffer_pct_for()


def test_buffer_pct_for_defaults_to_the_global(monkeypatch):
    """False (and the default) is the S0 value, and it still tracks the config knob."""
    assert investable.buffer_pct_for() == investable.buffer_pct()
    assert investable.buffer_pct_for(is_custom=False) == investable.buffer_pct()
    monkeypatch.setitem(config.RISK_LIMITS, "cash_reserve_pct", 0.025)
    assert investable.buffer_pct_for(is_custom=False) == 0.025
    # ...and the custom override is independent of it.
    assert investable.buffer_pct_for(is_custom=True) == CUSTOM_RESERVE


def test_a_missing_custom_knob_falls_back_to_the_global_never_to_zero(monkeypatch):
    """Fail toward today's behavior, not toward a fully-invested (overdrawable) account."""
    limits = {k: v for k, v in config.RISK_LIMITS.items()
              if k != investable.CUSTOM_RESERVE_KEY}
    monkeypatch.setattr(config, "RISK_LIMITS", limits)
    assert investable.buffer_pct_for(is_custom=True) == GLOBAL_RESERVE
    assert investable.buffer_pct_for(is_custom=True) != 0.0


# =====================================================================================
# 2. S0 KEEPS 1.5% — sizing AND drift. S0 is validated; this change must not touch it.
# =====================================================================================
def test_an_s0_account_still_reserves_one_and_a_half_percent():
    target = _one_asset_target(S0_LABEL)
    # Holding exactly what a 1.5%-reserved account should hold: 985,000 / 100 = 9,850 sh.
    plan = eng.plan_account("DU1", S0_LABEL, NAV, {"SPY": 9850}, target)

    assert plan.investable == pytest.approx(NAV * (1 - GLOBAL_RESERVE))   # 985,000
    assert plan.cash_reserve_pct == pytest.approx(GLOBAL_RESERVE)
    spy = [ln for ln in plan.lines if ln.symbol == "SPY"][0]
    assert spy.target_shares == 9850
    # The measurement side agrees: 1.5% intended, 1.5% actually uninvested, zero drift.
    cash = _cash_line(plan)
    assert cash.target_weight == pytest.approx(GLOBAL_RESERVE)
    assert cash.actual_weight == pytest.approx(GLOBAL_RESERVE)
    assert cash.drift_weight == pytest.approx(0.0)
    assert plan.orders == {}          # correctly invested -> nothing to trade


def test_every_pre_existing_caller_is_byte_identical_when_nothing_is_passed():
    """Omitting the new argument anywhere in the chain reproduces the old numbers exactly."""
    target = _one_asset_target(S0_LABEL)
    positions = {"SPY": 9000}
    before = eng.plan_account("DU1", S0_LABEL, NAV, positions, target)
    after = eng.plan_account("DU1", S0_LABEL, NAV, positions, target, cash_reserve_pct=None)
    assert before.investable == after.investable
    assert before.orders == after.orders
    assert [(l.symbol, l.target_shares, l.drift_weight) for l in before.lines] == \
           [(l.symbol, l.target_shares, l.drift_weight) for l in after.lines]
    # And it equals the raw shared formula with no override.
    assert before.investable == pytest.approx(investable.compute_investable(NAV, 0.0))


# =====================================================================================
# 3. A CUSTOM ALLOCATION RESERVES 1% — sizing AND drift.
# =====================================================================================
def test_a_custom_allocation_account_reserves_one_percent():
    target = _one_asset_target(CUSTOM_LABEL)
    # 990,000 / 100 = 9,900 sh: what a 1%-reserved account should hold.
    plan = eng.plan_account("DU2", CUSTOM_LABEL, NAV, {"SPY": 9900}, target,
                            cash_reserve_pct=CUSTOM_RESERVE)

    assert plan.investable == pytest.approx(NAV * (1 - CUSTOM_RESERVE))   # 990,000
    assert plan.cash_reserve_pct == pytest.approx(CUSTOM_RESERVE)
    spy = [ln for ln in plan.lines if ln.symbol == "SPY"][0]
    assert spy.target_shares == 9900
    assert plan.orders == {}
    # REAL uninvested cash, which is the operational point: 1% of NAV = $10,000 left to pay
    # the advisory fee and fund distributions from.
    assert NAV - plan.investable == pytest.approx(10_000.0)


def test_the_custom_reserve_deploys_more_than_the_global_one():
    """1% vs 1.5% is a real order difference, not a cosmetic label change."""
    target = _one_asset_target(CUSTOM_LABEL)
    at_global = eng.plan_account("DU2", CUSTOM_LABEL, NAV, {}, target)
    at_custom = eng.plan_account("DU2", CUSTOM_LABEL, NAV, {}, target,
                                 cash_reserve_pct=CUSTOM_RESERVE)
    assert at_custom.orders["SPY"] == 9900
    assert at_global.orders["SPY"] == 9850
    assert at_custom.orders["SPY"] - at_global.orders["SPY"] == 50    # $5,000 more deployed


# =====================================================================================
# 4. THE MAIN CORRECTNESS RISK: the per-model value must reach the DRIFT/RECONCILE path,
#    not only the sizing site.
# =====================================================================================
def test_the_per_model_reserve_reaches_the_drift_path_not_only_sizing():
    """A custom account holding EXACTLY its 1%-sized book must read ZERO cash drift.

    This is the churn guard. Size at 1%, measure at 1.5%, and the account is permanently
    0.5% adrift on CASH by construction: it can never be brought in line, because holding
    MORE cash would breach the model's risk weights and holding less is what it already did.
    """
    target = _one_asset_target(CUSTOM_LABEL)
    plan = eng.plan_account("DU2", CUSTOM_LABEL, NAV, {"SPY": 9900}, target,
                            cash_reserve_pct=CUSTOM_RESERVE)
    cash = _cash_line(plan)
    assert cash.target_weight == pytest.approx(CUSTOM_RESERVE)     # NOT the global 1.5%
    assert cash.actual_weight == pytest.approx(CUSTOM_RESERVE)
    assert cash.drift_weight == pytest.approx(0.0)
    assert cash.status == "MATCHED"


def test_measuring_the_same_book_against_the_global_reserve_would_show_phantom_drift():
    """The bug this guards against, exhibited: SAME 1%-sized book, measured at 1.5%.

    Pinning the failure mode explicitly means a future refactor that stops threading the
    reserve into reconcile fails HERE with an obvious message, rather than quietly shipping
    an account that reads out-of-spec forever."""
    target = _one_asset_target(CUSTOM_LABEL)
    sized_at_1pct = investable.compute_investable(NAV, 0.0, CUSTOM_RESERVE)
    lines_wrong = reconcile.reconcile(target, NAV, {"SPY": 9900},
                                      investable=sized_at_1pct)          # no reserve passed
    assert _cash_line(lines_wrong).drift_weight == pytest.approx(-0.005)  # phantom 0.5%
    # Threading the model's own reserve is what removes it.
    lines_right = reconcile.reconcile(target, NAV, {"SPY": 9900},
                                      investable=sized_at_1pct,
                                      cash_reserve_pct=CUSTOM_RESERVE)
    assert _cash_line(lines_right).drift_weight == pytest.approx(0.0)


def test_reconcile_default_investable_also_honours_the_per_model_reserve():
    """When no explicit `investable` is given, the default must use the model's reserve too —
    otherwise a caller that passes only cash_reserve_pct sizes at 1.5% and measures at 1%."""
    target = _one_asset_target(CUSTOM_LABEL)
    lines = reconcile.reconcile(target, NAV, {}, cash_reserve_pct=CUSTOM_RESERVE)
    spy = [ln for ln in lines if ln.symbol == "SPY"][0]
    assert spy.target_shares == 9900
    assert _cash_line(lines).target_weight == pytest.approx(CUSTOM_RESERVE)


def test_the_read_only_recon_report_uses_the_same_reserve_as_the_engine():
    """recon_report.plan_account is a separate implementation of the same reconciliation. If
    it claimed a different reserve, the report would show drift the engine never acts on."""
    target = _one_asset_target(CUSTOM_LABEL)
    rpt = recon_report.plan_account("DU2", CUSTOM_LABEL, NAV, {"SPY": 9900}, target,
                                    cash_reserve_pct=CUSTOM_RESERVE)
    engine = eng.plan_account("DU2", CUSTOM_LABEL, NAV, {"SPY": 9900}, target,
                              cash_reserve_pct=CUSTOM_RESERVE)
    assert rpt.investable == pytest.approx(engine.investable)
    assert rpt.cash_reserve_pct == pytest.approx(CUSTOM_RESERVE)
    assert _cash_line(rpt).target_weight == pytest.approx(CUSTOM_RESERVE)
    assert _cash_line(rpt).drift_weight == pytest.approx(0.0)
    # ...and the default is still the global, unchanged, for S0.
    s0 = recon_report.plan_account("DU1", S0_LABEL, NAV, {"SPY": 9850},
                                   _one_asset_target(S0_LABEL))
    assert s0.investable == pytest.approx(NAV * (1 - GLOBAL_RESERVE))
    assert s0.cash_reserve_pct == pytest.approx(GLOBAL_RESERVE)


def test_the_single_account_execution_engine_sizes_against_the_per_model_reserve():
    target = _one_asset_target(CUSTOM_LABEL)
    orders = execution_engine.compute_intended_orders(NAV, {}, target,
                                                      cash_reserve_pct=CUSTOM_RESERVE)
    assert [(o.symbol, o.side, o.quantity) for o in orders] == [("SPY", "BUY", 9900)]
    # Default (S0) path unchanged.
    s0 = execution_engine.compute_intended_orders(NAV, {}, _one_asset_target(S0_LABEL))
    assert [(o.symbol, o.side, o.quantity) for o in s0] == [("SPY", "BUY", 9850)]


def test_the_risk_manager_reserve_guard_uses_the_per_model_reserve(monkeypatch):
    """A book SIZED to 1% must be CHECKED against 1%. Checked against 1.5% the no-leverage
    guard vetoes a correctly-sized custom account for being 0.5% over-invested."""
    monkeypatch.setattr(risk_manager, "check_kill_switch", lambda *a, **k: (False, None))
    target = _one_asset_target(CUSTOM_LABEL)
    resulting = {"SPY": 9900}          # 99% invested — exactly the 1% reserve

    vetoed = risk_manager.evaluate(NAV, 0.0, resulting, [], target)
    assert vetoed.batch_reasons, "1.5% threshold should reject a 99%-invested book"

    ok = risk_manager.evaluate(NAV, 0.0, resulting, [], target,
                               cash_reserve_pct=CUSTOM_RESERVE)
    assert ok.batch_reasons == []


# =====================================================================================
# 5. BOTH IN ONE BATCH, with no cross-contamination.
# =====================================================================================
def _mixed_batch_inputs():
    return [
        {"account": "DU1", "version": S0_LABEL, "net_liq": NAV, "positions": {}},
        {"account": "DU2", "version": CUSTOM_LABEL, "net_liq": NAV, "positions": {}},
    ]


def test_one_batch_holds_an_s0_and_a_custom_account_at_different_reserves():
    targets = {S0_LABEL: _one_asset_target(S0_LABEL),
               CUSTOM_LABEL: _one_asset_target(CUSTOM_LABEL)}
    plans = eng.plan_accounts(_mixed_batch_inputs(), targets,
                              cash_reserve_pct_by_version={CUSTOM_LABEL: CUSTOM_RESERVE})
    by_account = {p.account: p for p in plans}

    assert by_account["DU1"].investable == pytest.approx(NAV * (1 - GLOBAL_RESERVE))
    assert by_account["DU2"].investable == pytest.approx(NAV * (1 - CUSTOM_RESERVE))
    assert by_account["DU1"].orders["SPY"] == 9850
    assert by_account["DU2"].orders["SPY"] == 9900
    # Drift side too, in the same run: each account's CASH target is its OWN reserve.
    assert _cash_line(by_account["DU1"]).target_weight == pytest.approx(GLOBAL_RESERVE)
    assert _cash_line(by_account["DU2"]).target_weight == pytest.approx(CUSTOM_RESERVE)


def test_the_custom_account_in_the_batch_cannot_move_the_s0_account():
    """No cross-contamination, proved by comparison rather than by assertion of a constant:
    the S0 plan from the MIXED batch is identical to the S0 plan from an S0-only batch."""
    targets = {S0_LABEL: _one_asset_target(S0_LABEL),
               CUSTOM_LABEL: _one_asset_target(CUSTOM_LABEL)}
    mixed = {p.account: p for p in eng.plan_accounts(
        _mixed_batch_inputs(), targets,
        cash_reserve_pct_by_version={CUSTOM_LABEL: CUSTOM_RESERVE})}
    alone = {p.account: p for p in eng.plan_accounts(
        [_mixed_batch_inputs()[0]], targets)}

    assert mixed["DU1"].investable == alone["DU1"].investable
    assert mixed["DU1"].orders == alone["DU1"].orders
    assert mixed["DU1"].cash_reserve_pct == alone["DU1"].cash_reserve_pct
    assert [(l.symbol, l.target_shares, l.target_weight, l.drift_weight)
            for l in mixed["DU1"].lines] == \
           [(l.symbol, l.target_shares, l.target_weight, l.drift_weight)
            for l in alone["DU1"].lines]


def test_a_version_absent_from_the_reserve_map_gets_the_global_default():
    """Fail toward today's behavior: an unnamed model is 1.5%, never 0% and never 1%."""
    targets = {S0_LABEL: _one_asset_target(S0_LABEL),
               CUSTOM_LABEL: _one_asset_target(CUSTOM_LABEL)}
    plans = {p.account: p for p in eng.plan_accounts(
        _mixed_batch_inputs(), targets, cash_reserve_pct_by_version={})}
    assert plans["DU1"].investable == pytest.approx(NAV * (1 - GLOBAL_RESERVE))
    assert plans["DU2"].investable == pytest.approx(NAV * (1 - GLOBAL_RESERVE))

    none_map = {p.account: p for p in eng.plan_accounts(
        _mixed_batch_inputs(), targets, cash_reserve_pct_by_version=None)}
    assert none_map["DU2"].investable == pytest.approx(NAV * (1 - GLOBAL_RESERVE))


def test_build_plan_threads_the_reserve_map_through_to_the_plans():
    targets = {S0_LABEL: _one_asset_target(S0_LABEL),
               CUSTOM_LABEL: _one_asset_target(CUSTOM_LABEL)}
    out = eng.build_plan(_mixed_batch_inputs(), targets,
                         cash_reserve_pct_by_version={CUSTOM_LABEL: CUSTOM_RESERVE})
    by_account = {p.account: p for p in out["plans"]}
    assert by_account["DU2"].investable == pytest.approx(NAV * (1 - CUSTOM_RESERVE))
    assert by_account["DU1"].investable == pytest.approx(NAV * (1 - GLOBAL_RESERVE))


# =====================================================================================
# 6. SOURCE-BASED resolution — never the label's spelling.
# =====================================================================================
def test_the_executor_resolves_the_reserve_from_the_allocation_source():
    _t, meta = _custom_target_and_meta()
    assert bre.account_reserve_pct(meta) == pytest.approx(CUSTOM_RESERVE)
    assert bre.account_reserve_pct(None) == pytest.approx(GLOBAL_RESERVE)


def test_a_custom_sounding_label_with_nothing_published_keeps_the_global_reserve():
    """The spelling test the whole feature refuses to make. A label that LOOKS custom but has
    no rows in the CRM view has meta=None, and gets 1.5% — a CRM rename can never move an
    account's reserve, in either direction."""
    assert bre.account_reserve_pct(None) == pytest.approx(GLOBAL_RESERVE)
    # Same label string, both answers, decided only by whether a meta exists.
    _t, meta = _custom_target_and_meta(label="Growth (Custom)")
    assert bre.account_reserve_pct(meta) != bre.account_reserve_pct(None)


def test_reserve_pct_for_labels_maps_only_the_published_custom_labels(monkeypatch):
    _fake_crm(monkeypatch, published_labels={CUSTOM_LABEL})
    out = custom_target.reserve_pct_for_labels([CUSTOM_LABEL, S0_LABEL, "Growth"])
    assert out == {CUSTOM_LABEL: CUSTOM_RESERVE}      # S0 labels absent -> caller defaults
    assert custom_target.reserve_pct_for(CUSTOM_LABEL) == pytest.approx(CUSTOM_RESERVE)
    assert custom_target.reserve_pct_for(S0_LABEL) == pytest.approx(GLOBAL_RESERVE)


def test_reserve_pct_for_labels_is_empty_when_nothing_is_published(monkeypatch):
    """Degrade to today's behavior (everything at 1.5%), never to a 0% reserve."""
    _fake_crm(monkeypatch, published_labels=set())
    assert custom_target.reserve_pct_for_labels([CUSTOM_LABEL, S0_LABEL]) == {}


# =====================================================================================
# 7. END TO END through the batch executor's PREVIEW path (fake read-only broker).
# =====================================================================================
def test_run_batch_session_sizes_a_custom_account_against_one_percent(monkeypatch,
                                                                     tmp_path):
    """The executor must hand the engine the 1% reserve for a custom account. Capture the
    kwarg at the real call site, then let the real engine run — so this pins the WIRING, not
    a re-implementation of it."""
    import ledger

    monkeypatch.setattr(ledger, "RUNS_JSONL", os.path.join(str(tmp_path), "runs.jsonl"))
    monkeypatch.setattr(ledger, "LOG_TXT", os.path.join(str(tmp_path), "paperbot.log"))
    monkeypatch.setattr(
        bre.live_quotes, "fetch",
        lambda ib, syms: {s: bre.live_quotes.Quote(s, 100.0, 100.0, 100.0, 100.0, 1)
                          for s in syms})
    monkeypatch.setattr(bre.sp, "_strategy_universe", lambda: {"SCHB", "USFR"})

    seen: dict = {}
    real_plan_account = eng.plan_account

    def spy_plan_account(account, version, *a, **kw):
        seen[account] = kw.get("cash_reserve_pct")
        return real_plan_account(account, version, *a, **kw)

    monkeypatch.setattr(bre.rebalance_engine, "plan_account", spy_plan_account)

    target, meta = _custom_target_and_meta()
    summary = [_row(CUSTOM_ACCT, "NetLiquidation", "100000"),
               _row(CUSTOM_ACCT, "BuyingPower", "100000"),
               _row(CUSTOM_ACCT, "TotalCashValue", "100000")]
    ib = _FakeIB(summary, [_FakePosition(CUSTOM_ACCT, "SCHB", 100)])

    rc = bre.run_batch_session(ib, [CUSTOM_ACCT], {CUSTOM_ACCT: CUSTOM_LABEL},
                               {CUSTOM_LABEL: target}, armed=False, armed_conn=False,
                               kill=False, metas={CUSTOM_LABEL: meta})
    assert rc == 0
    assert seen[CUSTOM_ACCT] == pytest.approx(CUSTOM_RESERVE)


def test_run_batch_session_leaves_an_s0_account_at_the_global_reserve(monkeypatch,
                                                                     tmp_path):
    """The same executor, the same run shape, an S0 label (metas empty) -> 1.5%."""
    import ledger

    monkeypatch.setattr(ledger, "RUNS_JSONL", os.path.join(str(tmp_path), "runs.jsonl"))
    monkeypatch.setattr(ledger, "LOG_TXT", os.path.join(str(tmp_path), "paperbot.log"))
    monkeypatch.setattr(
        bre.live_quotes, "fetch",
        lambda ib, syms: {s: bre.live_quotes.Quote(s, 100.0, 100.0, 100.0, 100.0, 1)
                          for s in syms})
    monkeypatch.setattr(bre.sp, "_strategy_universe", lambda: {"SPY"})

    seen: dict = {}
    real_plan_account = eng.plan_account

    def spy_plan_account(account, version, *a, **kw):
        seen[account] = kw.get("cash_reserve_pct")
        return real_plan_account(account, version, *a, **kw)

    monkeypatch.setattr(bre.rebalance_engine, "plan_account", spy_plan_account)

    summary = [_row("DU8922142", "NetLiquidation", "100000"),
               _row("DU8922142", "BuyingPower", "100000"),
               _row("DU8922142", "TotalCashValue", "100000")]
    ib = _FakeIB(summary, [_FakePosition("DU8922142", "SPY", 100)])

    rc = bre.run_batch_session(ib, ["DU8922142"], {"DU8922142": S0_LABEL},
                               {S0_LABEL: _one_asset_target(S0_LABEL)},
                               armed=False, armed_conn=False, kill=False, metas={})
    assert rc == 0
    assert seen["DU8922142"] == pytest.approx(GLOBAL_RESERVE)
