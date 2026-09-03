"""Tests for group_execute — the LIVE advisor-master target for the per-ticker group rail.

The master F6795549 was read live on 2026-09-03: 354 client accounts, 8 pre-existing groups.
The old login (apsv1816) carried 18 accounts and no master at all, so the account wall is now
the ONLY thing scoping a run away from Ted's and Doug's books.
"""
from __future__ import annotations

import pytest

import config
import fa_membership
import group_execute as ge
from connections import clientids


@pytest.fixture
def armed_config(monkeypatch):
    """Flip the CODE GATE open in-process only; the on-disk defaults stay READONLY."""
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)


BOOK = {"U23415099": "Growth (Custom)", "U23414989": "Growth (Custom)",
        "U27305011": "Balanced (Small, Custom)"}


def test_points_at_the_live_master_on_4003_with_the_reserved_client_id():
    t = ge.live_gateway(BOOK)
    assert t.name == "LIVE"
    assert t.master_account == "F6795549"
    assert t.port == clientids.LIVE_TRADE_PORT == 4003
    assert t.clientid_consumer == "live_fa_block_exec"
    assert clientids.get("live_fa_block_exec") == 63


def test_the_pin_is_a_client_sub_and_is_deterministic():
    """Two runs of the same scope must pin identically or a run is not reproducible."""
    a = ge.live_gateway(BOOK)
    b = ge.live_gateway(dict(reversed(list(BOOK.items()))))
    assert a.pin_account == b.pin_account == "U23414989"
    assert a.pin_account != ge.LIVE_MASTER_ACCOUNT


def test_refuses_to_pin_to_the_master():
    """The master's own account-update stream hangs the session."""
    with pytest.raises(ValueError) as e:
        ge.live_gateway({**BOOK, "F6795549": "Growth (Custom)"},
                        pin_account="F6795549")
    assert "hangs the session" in str(e.value)


def test_refuses_a_pin_outside_the_scoped_book():
    with pytest.raises(ValueError) as e:
        ge.live_gateway(BOOK, pin_account="U99999999")
    assert "not in the enrollment" in str(e.value)


def test_refuses_an_empty_enrollment():
    """An empty roster read must never silently produce a live gateway."""
    for empty in ({}, None, {"   ": "Growth (Custom)"}):
        with pytest.raises(ValueError):
            ge.live_gateway(empty)


def test_carries_no_static_group_map():
    """TIER_GROUPS is meaningless here: one group per TICKER per RUN, name on the route."""
    assert ge.live_gateway(BOOK).group_names is None


def test_enrollment_is_copied_not_aliased():
    src = dict(BOOK)
    t = ge.live_gateway(src)
    src["U00000001"] = "Growth (Custom)"
    assert "U00000001" not in t.enrollment, "a later mutation must not widen a built gateway"


def test_an_explicit_pin_is_honoured_when_it_is_in_the_book():
    assert ge.live_gateway(BOOK, pin_account="U27305011").pin_account == "U27305011"


# ========================================================================================
# plan_group_run - the pure planning core, and the second account wall.
# ========================================================================================
import recon_report


def _plan(account, version, orders):
    return recon_report.AccountPlan(
        account=account, version=version, net_liq=100000.0, reserve=1500.0,
        investable=98500.0, lines=[], needs_rebalance=bool(orders), orders=dict(orders))


STAMP = "20260904-0931"


def test_plan_group_run_returns_the_whole_run_as_reviewable_data():
    plans = [_plan("U1", "Growth (Custom)", {"XLE": 10, "JAAA": -20}),
             _plan("U2", "Balanced (Custom)", {"XLE": 4})]
    run = ge.plan_group_run(plans, run_stamp=STAMP, prices={"XLE": 65.0, "JAAA": 50.5})
    assert run["n_groups"] == 2
    assert run["n_buy"] == 1 and run["n_sell"] == 1
    assert run["n_accounts"] == 2
    assert run["accounts"] == ["U1", "U2"]
    assert run["symbols"] == ["JAAA", "XLE"]
    assert len(run["routes"]) == 2
    assert all(r.route == "fa_block" for r in run["routes"])
    assert "ONE average price" in run["summary_text"]


def test_the_single_account_model_still_produces_group_routes():
    """Conservative (Custom) - 1 account - is the FIRST staged run."""
    run = ge.plan_group_run([_plan("U101", "Conservative (Custom)", {"USFR": 40})],
                            run_stamp=STAMP)
    assert run["n_groups"] == 1
    assert run["routes"][0].route == "fa_block"
    assert run["routes"][0].fa_group == "USFR BUY 20260904-0931"


def test_pure_layer_refuses_before_anything_reaches_the_broker():
    """An account on both sides means plans from two runs were mixed. It must raise HERE."""
    with pytest.raises(ValueError):
        ge.plan_group_run([_plan("U1", "G", {"XLE": 10}), _plan("U1", "G", {"XLE": -3})],
                          run_stamp=STAMP)
    with pytest.raises(ValueError):
        ge.plan_group_run([_plan("U1", "G", {"XLE": 10})], run_stamp="")


def test_nothing_to_do_is_an_empty_run_not_an_error():
    run = ge.plan_group_run([_plan("U1", "G", {"XLE": 0})], run_stamp=STAMP)
    assert run["n_groups"] == 0
    assert run["routes"] == []
    assert "nothing in this scope needs to trade" in run["summary_text"]


def test_the_second_account_wall_catches_an_account_off_the_roster():
    """The executor walls each ROUTE, but a group is CREATED before its route executes - so an
    off-roster account could be written into a live group at the master and only refused
    later, leaving a group naming a client this run had no business touching."""
    plans = [_plan("U1", "G", {"XLE": 10}), _plan("U_TEDS_CLIENT", "G", {"XLE": 5})]
    run = ge.plan_group_run(plans, run_stamp=STAMP)
    assert ge.accounts_outside_the_wall(run["group_plans"], ["U1"]) == ["U_TEDS_CLIENT"]


def test_the_second_account_wall_passes_when_everything_is_in_scope():
    run = ge.plan_group_run([_plan("U1", "G", {"XLE": 10})], run_stamp=STAMP)
    assert ge.accounts_outside_the_wall(run["group_plans"], ["U1", "U2"]) == []


def test_an_empty_allowed_list_fails_closed_rather_than_open():
    run = ge.plan_group_run([_plan("U1", "G", {"XLE": 10})], run_stamp=STAMP)
    assert ge.accounts_outside_the_wall(run["group_plans"], []) == ["U1"]
    assert ge.accounts_outside_the_wall(run["group_plans"], None) == ["U1"]


# ========================================================================================
# create_run_groups - every group up front, behind a run-level account wall.
# ========================================================================================
class _FakeIB:
    """Records replaceFA. Echoes writes so successive creations compose."""

    def __init__(self, xml):
        self.xml = xml
        self.replaced = []

    def requestFA(self, kind):
        return self.xml

    def replaceFA(self, kind, xml):
        self.replaced.append((kind, xml))
        self.xml = xml


_BASE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<ListOfGroups>"
    "<Group><name>Main</name><defaultMethod>NetLiq</defaultMethod>"
    '<ListOfAccts varName="list"><Account><acct>U9999999</acct></Account></ListOfAccts>'
    "</Group></ListOfGroups>")


def _run(plans):
    return ge.plan_group_run(plans, run_stamp=STAMP)


def test_the_whole_run_is_refused_when_one_account_is_off_the_roster(tmp_path):
    """Nothing created, no order - because a group is created BEFORE its route executes."""
    run = _run([_plan("U1", "G", {"XLE": 10}), _plan("U_TED", "G", {"XLE": 5})])
    ib = _FakeIB(_BASE_XML)
    with pytest.raises(ge.GroupRunRefused) as e:
        ge.create_run_groups(ib, run["group_plans"], allowed_accounts=["U1"],
                             armed=True, backup_path=str(tmp_path))
    assert "U_TED" in str(e.value)
    assert "NOTHING was created" in str(e.value)
    assert ib.replaced == []


def test_unarmed_creates_nothing_but_plans_every_group(tmp_path):
    run = _run([_plan("U1", "G", {"XLE": 10, "JAAA": -5})])
    ib = _FakeIB(_BASE_XML)
    res = ge.create_run_groups(ib, run["group_plans"], allowed_accounts=["U1"],
                               armed=False, backup_path=str(tmp_path))
    assert res["created"] == 0
    assert res["previewed"] == 2
    assert ib.replaced == []
    assert all(r["diff"].strip() for r in res["results"])


def test_armed_creates_every_group_and_they_compose(tmp_path, armed_config):
    """Each creation reads the document the previous one wrote - so all N survive."""
    run = _run([_plan("U1", "G", {"XLE": 10, "XLF": 3, "JAAA": -5})])
    ib = _FakeIB(_BASE_XML)
    res = ge.create_run_groups(ib, run["group_plans"], allowed_accounts=["U1"],
                               armed=True, backup_path=str(tmp_path))
    assert res["created"] == 3
    assert len(ib.replaced) == 3
    final = fa_membership.parse_group_membership(ib.xml)
    assert "Main" in final, "the groups we do not own must survive every creation"
    for g in run["group_plans"]:
        assert final[g.group_name] == set(g.accounts)
    assert len(final) == 4


def test_each_result_carries_what_the_group_is_for(tmp_path):
    run = _run([_plan("U1", "G", {"XLE": 10})])
    res = ge.create_run_groups(_FakeIB(_BASE_XML), run["group_plans"],
                               allowed_accounts=["U1"], armed=False,
                               backup_path=str(tmp_path))
    r = res["results"][0]
    assert (r["symbol"], r["side"], r["total_qty"]) == ("XLE", "BUY", 10)


def test_nothing_to_do_is_not_an_error(tmp_path):
    res = ge.create_run_groups(_FakeIB(_BASE_XML), [], allowed_accounts=["U1"],
                               armed=True, backup_path=str(tmp_path))
    assert res["created"] == 0 and res["results"] == []
