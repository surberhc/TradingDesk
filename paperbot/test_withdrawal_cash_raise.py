"""Tests for withdrawal_cash_raise — the narrowly-scoped raise-cash trigger.

Pure/monkeypatched only: no broker, no CRM, no gateway. accounts_needing_cash() and
build_restricted_plans() delegate to withdrawal_reserve_check / group_execute, which are
tested on their own terms elsewhere (dailyreport/test_withdrawal_reserve_check.py,
paperbot/test_group_execute.py's build_plans_for_accounts tests). These tests pin the
DELEGATION itself: the right accounts go in, nothing is duplicated or widened.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _p in (str(_HERE), str(_REPO / "dailyreport"), str(_REPO / "connections")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cashflows  # noqa: E402
import withdrawal_cash_raise as wcr  # noqa: E402
import withdrawal_reserve_check as wrc  # noqa: E402


# --------------------------------------------------------------------------- #
# accounts_needing_cash()
# --------------------------------------------------------------------------- #
def test_returns_only_the_short_accounts_with_their_shortfall(monkeypatch):
    fake_schedule = {
        "USHORT": [cashflows.Flow("distribution", amount=8500.0, pct_nav=0.0, day=15)],
        "UOK": [cashflows.Flow("distribution", amount=1000.0, pct_nav=0.0, day=15)],
    }
    monkeypatch.setattr(cashflows, "SCHEDULE", fake_schedule)

    def fake_read_cash(account):
        return {"USHORT": {"net_liq": 500_000, "total_cash": 10_000},
                "UOK": {"net_liq": 100_000, "total_cash": 5_000}}[account]

    monkeypatch.setattr(wrc, "read_cash", fake_read_cash)

    rows = wcr.accounts_needing_cash()
    assert [r["account"] for r in rows] == ["USHORT"]
    # reserve = 2 * 8500 = 17000; shortfall = 17000 - 10000
    assert rows[0]["reserve"] == 17_000.0
    assert round(rows[0]["shortfall"], 2) == 7_000.0
    assert rows[0]["net_liq"] == 500_000
    assert rows[0]["total_cash"] == 10_000


def test_no_accounts_short_returns_an_empty_list(monkeypatch):
    fake_schedule = {
        "UOK": [cashflows.Flow("distribution", amount=1000.0, pct_nav=0.0, day=15)],
    }
    monkeypatch.setattr(cashflows, "SCHEDULE", fake_schedule)
    monkeypatch.setattr(wrc, "read_cash",
                        lambda account: {"net_liq": 100_000, "total_cash": 50_000})
    assert wcr.accounts_needing_cash() == []


def test_a_live_read_failure_skips_that_account_without_raising(monkeypatch):
    fake_schedule = {
        "UGOOD": [cashflows.Flow("distribution", amount=8500.0, pct_nav=0.0, day=15)],
        "UBAD": [cashflows.Flow("distribution", amount=8500.0, pct_nav=0.0, day=15)],
    }
    monkeypatch.setattr(cashflows, "SCHEDULE", fake_schedule)

    def fake_read_cash(account):
        if account == "UBAD":
            raise RuntimeError("account UBAD not found under the live-trading login")
        return {"net_liq": 500_000, "total_cash": 10_000}

    monkeypatch.setattr(wrc, "read_cash", fake_read_cash)
    rows = wcr.accounts_needing_cash()
    assert [r["account"] for r in rows] == ["UGOOD"]


def test_sorted_by_account(monkeypatch):
    fake_schedule = {
        "UB": [cashflows.Flow("distribution", amount=8500.0, pct_nav=0.0, day=15)],
        "UA": [cashflows.Flow("distribution", amount=8500.0, pct_nav=0.0, day=15)],
    }
    monkeypatch.setattr(cashflows, "SCHEDULE", fake_schedule)
    monkeypatch.setattr(wrc, "read_cash",
                        lambda account: {"net_liq": 500_000, "total_cash": 1_000})
    assert [r["account"] for r in wcr.accounts_needing_cash()] == ["UA", "UB"]


# --------------------------------------------------------------------------- #
# build_restricted_plans() — pure delegation to group_execute.build_plans_for_accounts.
# The account-scoping guarantee itself (never a 3rd sibling account) is proven against the
# real function in paperbot/test_group_execute.py; here we only pin that this module hands
# the account list through UNCHANGED and does not re-derive or widen it.
# --------------------------------------------------------------------------- #
def test_build_restricted_plans_passes_the_exact_account_list_through(monkeypatch):
    import group_execute as ge

    seen = []

    def fake_build(ib, accounts, *, band_pct=None):
        seen.append((list(accounts), band_pct))
        return {"plans": [f"plan-for-{a}" for a in accounts], "prices": {}, "versions": {},
                "targets": {}, "metas": {}, "skipped": [], "account_inputs": [],
                "summaries": {}}

    monkeypatch.setattr(ge, "build_plans_for_accounts", fake_build)

    result = wcr.build_restricted_plans(["U1", "U3"], object())
    assert seen == [(["U1", "U3"], None)]
    assert result["plans"] == ["plan-for-U1", "plan-for-U3"]


def test_build_restricted_plans_never_widens_a_two_account_two_model_input(monkeypatch):
    """The scenario named in the build spec: 2 accounts, 2 different models. A 3rd account
    that happens to share one of those models must never appear."""
    import group_execute as ge

    universe = {"U1": {"model": "Growth (Custom)", "sibling": "U2"},
                "U3": {"model": "Balanced (Custom)"}}

    def fake_build(ib, accounts, *, band_pct=None):
        # A real implementation would only ever see the accounts it was handed - this fake
        # asserts that invariant directly, standing in for group_execute's own account-list
        # scoping (proven independently in test_group_execute.py).
        assert set(accounts) <= set(universe), "must never be asked about an unknown account"
        return {"plans": [{"account": a, "model": universe[a]["model"]} for a in accounts],
                "prices": {}, "versions": {a: universe[a]["model"] for a in accounts},
                "targets": {}, "metas": {}, "skipped": [], "account_inputs": [],
                "summaries": {}}

    monkeypatch.setattr(ge, "build_plans_for_accounts", fake_build)

    result = wcr.build_restricted_plans(["U1", "U3"], object())
    accounts_in_result = {p["account"] for p in result["plans"]}
    assert accounts_in_result == {"U1", "U3"}
    assert "U2" not in accounts_in_result
    assert "U2" not in result["versions"]


# --------------------------------------------------------------------------- #
# prepare_run() — restricted plans -> ticker groups -> routes, walled to the exact scope.
# --------------------------------------------------------------------------- #
def test_prepare_run_walls_to_exactly_the_given_accounts(monkeypatch):
    import group_execute as ge
    import recon_report

    def fake_build_restricted(accounts, ib, *, band_pct=None):
        plans = [recon_report.AccountPlan(
            account=a, version="V", net_liq=100_000.0, reserve=0.0, investable=100_000.0,
            lines=[], needs_rebalance=True, orders={"XLE": 10}) for a in accounts]
        return {"plans": plans, "prices": {"XLE": 65.0}, "versions": {a: "V" for a in accounts},
                "targets": {}, "metas": {}, "skipped": [], "account_inputs": [], "summaries": {}}

    monkeypatch.setattr(wcr, "build_restricted_plans", fake_build_restricted)

    run = wcr.prepare_run(["U1", "U3"], object(), run_stamp="20260905-0900")
    assert run["accounts_scope"] == ["U1", "U3"]
    assert run["n_accounts"] == 2
    assert run["accounts"] == ["U1", "U3"]
    # allowed_accounts wall is EXACTLY the requested scope: nothing is outside it.
    assert run["outside"] == ge.accounts_outside_the_wall(run["group_plans"], ["U1", "U3"])
    assert run["outside"] == []


def test_prepare_run_flags_an_account_the_plan_should_never_have_touched(monkeypatch):
    """A defensive check: if build_restricted_plans ever leaked in an account outside the
    requested scope, the wall in prepare_run must catch it (accounts_outside_the_wall is
    computed against the ORIGINAL request, not whatever the plan happened to contain)."""
    import recon_report

    def fake_build_restricted(accounts, ib, *, band_pct=None):
        # Simulate a bug: an extra account sneaks into the plan.
        plans = [recon_report.AccountPlan(
            account=a, version="V", net_liq=100_000.0, reserve=0.0, investable=100_000.0,
            lines=[], needs_rebalance=True, orders={"XLE": 10})
            for a in list(accounts) + ["U_LEAK"]]
        return {"plans": plans, "prices": {"XLE": 65.0},
                "versions": {a: "V" for a in list(accounts) + ["U_LEAK"]},
                "targets": {}, "metas": {}, "skipped": [], "account_inputs": [], "summaries": {}}

    monkeypatch.setattr(wcr, "build_restricted_plans", fake_build_restricted)

    run = wcr.prepare_run(["U1"], object(), run_stamp="20260905-0900")
    assert run["outside"] == ["U_LEAK"]
