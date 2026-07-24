"""test_brain.py — END-TO-END flow tests for the CRM composition capstone (#42/#43).

Pure/offline: no broker, no gateway, no I/O. Runs with zero infra:
    cd crm
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest -q

These are COMPOSITION tests — they exercise the brain wiring the four modules into flows,
not the modules' internals (those are covered by test_domain/ledger/latch/capability):
  * the §5 HARD capability gate ENFORCED at assign() (refuse a template the account can't run),
    and soft warnings SURFACED (not enforced) on an allowed assign;
  * group membership derived from assignments, updating on reassignment;
  * the §7.4 → §12.1 → §12.3 reconcile → triage → latch tie (clean vs drift vs intraday-pending);
  * the §12.4 whole-contract floor pre-flight; and
  * the whole-brain to_dict/from_dict transport round-trip.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

import domain
from domain import Template
import ledger
from ledger import Instrument
import latch
import capability
from capability import AccountCapabilities

import brain
from brain import CRMBrain, ReconcileReport


# ===========================================================================
# Fixtures — templates from the real registry + a caps builder
# ===========================================================================
ETF_ONLY = Template(template_id="balanced", name="Balanced ETF-only",
                    weights={"S0-Balanced": 1.0})
OVERLAY = Template(template_id="balanced_overlay", name="Balanced + S8 Overlay",
                   weights={"S0-Balanced": 0.75, "S8-Overlay": 0.25})

TEMPLATES = {"balanced": ETF_ONLY, "balanced_overlay": OVERLAY}

DAY = date(2026, 7, 24)
NOW = datetime(2026, 7, 24, 9, 5, 0)


def _caps(**over) -> AccountCapabilities:
    """A fully-qualified, ample margin account. Override to break a specific axis."""
    base = dict(
        account_id="DU8922143",
        options_level=3,
        index_option_perm=True,
        is_margin=True,
        account_type="margin",
        net_liq=1_000_000.0,
        buying_power=2_000_000.0,
        excess_liquidity=800_000.0,
    )
    base.update(over)
    return AccountCapabilities(**base)


def _brain() -> CRMBrain:
    return CRMBrain(TEMPLATES)


# ===========================================================================
# 1) assign REFUSED — the hard capability gate is enforced at assignment
# ===========================================================================
def test_assign_refused_overlay_for_l2_cash_account():
    """§5: it is impossible to assign a sleeve the account cannot run. An overlay (S8) template
    for an L2 cash account with no index perm hard-blocks → assign RAISES, reasons surfaced."""
    b = _brain()
    caps = _caps(account_id="DUcash", options_level=2, is_margin=False,
                 account_type="cash", index_option_perm=False)
    with pytest.raises(ValueError) as ei:
        b.assign("DUcash", "balanced_overlay", caps, set_by="andrew", now=NOW)
    msg = str(ei.value)
    # The blocking reasons are surfaced in the error.
    assert "options Level 3" in msg
    assert "margin account" in msg
    assert "index-option" in msg
    # And nothing was bound — the account has no assignment.
    assert b.current_assignment("DUcash") is None


def test_assign_refused_unknown_template():
    b = _brain()
    with pytest.raises(ValueError) as ei:
        b.assign("DUx", "does_not_exist", _caps(), set_by="andrew", now=NOW)
    assert "unknown template" in str(ei.value)


# ===========================================================================
# 2) assign SUCCEEDS — allowed template, and soft-warned-but-allowed template
# ===========================================================================
def test_assign_succeeds_etf_only_for_any_account():
    """An ETF-only template carries no requirement → assignable to a bare cash account."""
    b = _brain()
    caps = _caps(account_id="DUcash", options_level=None, is_margin=False,
                 account_type="cash", index_option_perm=False)
    row = b.assign("DUcash", "balanced", caps, set_by="andrew", now=NOW)
    assert isinstance(row, domain.AccountAssignment)
    assert b.current_assignment("DUcash").template_id == "balanced"
    assert b.assignment_warnings("DUcash") == []   # no soft warnings on the ETF sleeve


def test_assign_succeeds_but_soft_warns_surfaced_not_enforced():
    """§5.2 allow-but-flag: a fully-qualified account with THIN margin gets the overlay
    assigned (soft never blocks) AND the advisory warning surfaced via assignment_warnings."""
    b = _brain()
    thin = _caps(account_id="DUthin", buying_power=100_000.0, excess_liquidity=300_000.0)
    row = b.assign("DUthin", "balanced_overlay", thin, set_by="andrew", now=NOW)
    assert row.template_id == "balanced_overlay"           # assigned despite the warning
    warns = b.assignment_warnings("DUthin")
    assert warns                                            # surfaced
    assert any("cushion drops to" in w for w in warns)     # the §5.2 advisory
    # Sanity: the gate itself allowed it (soft warning, not a hard block).
    assert b.assignable(thin)["balanced_overlay"].allowed is True


# ===========================================================================
# 3) group_membership — reflects the assignment and updates on reassignment
# ===========================================================================
def test_group_membership_reflects_and_updates_on_reassignment():
    b = _brain()
    caps = _caps(account_id="DUrich")
    # Assign the ETF-only balanced → member of the tier_balanced group only.
    b.assign("DUrich", "balanced", caps, set_by="andrew", now=NOW)
    m1 = b.group_membership()
    assert m1 == {"tier_balanced": {"DUrich"}}

    # Reassign to the overlay (both sleeves) → now in tier_balanced AND s8_overlay.
    b.assign("DUrich", "balanced_overlay", caps, set_by="andrew",
             now=datetime(2026, 7, 24, 10, 0, 0))
    m2 = b.group_membership()
    assert m2["tier_balanced"] == {"DUrich"}
    assert m2["s8_overlay"] == {"DUrich"}
    # The reassignment superseded — history has both rows, current is the overlay.
    assert b.current_assignment("DUrich").template_id == "balanced_overlay"
    assert len(b.assignment_history("DUrich")) == 2


# ===========================================================================
# 4) attribute_block then a CLEAN reconcile → CLEAN, not latched
# ===========================================================================
def test_attribute_block_then_clean_reconcile():
    b = _brain()
    spy = Instrument.stock("SPY")
    # Block buy 10 SPY @ 100 for one account in the tier_balanced group (→ S0-Balanced sleeve).
    b.attribute_block(fa_group="tier_balanced", per_account_split={"DU1": 10},
                      instrument=spy, price=100.0, side="BUY", now=NOW)
    # Broker truth agrees exactly: 10 SPY, cash -1000.
    report = b.reconcile("DU1", {spy: 10.0}, -1000.0, [],
                         day=DAY, is_eod=False, now=NOW)
    assert isinstance(report, ReconcileReport)
    assert report.recon.verdict == "OK"
    assert report.outcome is latch.TriageOutcome.CLEAN
    assert report.latched is False
    assert report.alerted is False
    assert b.preflight_excluded("DU1", DAY) is False


# ===========================================================================
# 5) reconcile with LEDGER_DRIFT → LATCH, excluded, then clear re-enables
# ===========================================================================
def test_reconcile_ledger_drift_latches_then_clear_reenables():
    b = _brain()
    spy = Instrument.stock("SPY")
    b.attribute_block(fa_group="tier_balanced", per_account_split={"DU1": 10},
                      instrument=spy, price=100.0, side="BUY", now=NOW)
    # Broker holds only 5 SPY — a hard position drift the ledger can't reconcile.
    report = b.reconcile("DU1", {spy: 5.0}, -1000.0, [],
                         day=DAY, is_eod=False, now=NOW)
    assert report.recon.verdict == "DRIFT"
    assert report.outcome is latch.TriageOutcome.LATCH
    assert report.latched is True
    assert report.alerted is True                       # first fault of the day → alerts once
    # The account is now excluded at pre-flight WITHOUT re-alerting.
    assert b.preflight_excluded("DU1", DAY) is True
    # The latch used the LEDGER_DRIFT fault (chosen by fault_for_latch).
    active = b.latches.active_latches(DAY)
    assert len(active) == 1
    assert active[0].fault is latch.FaultType.LEDGER_DRIFT

    # A HUMAN clears it → the account is re-enabled at pre-flight.
    assert b.latches.clear("DU1", DAY, by="andrew", now=NOW) is True
    assert b.preflight_excluded("DU1", DAY) is False


def test_reconcile_latch_alert_once():
    """A SECOND latching reconcile the same day does NOT re-alert (§12.3 alert-once)."""
    b = _brain()
    spy = Instrument.stock("SPY")
    b.attribute_block(fa_group="tier_balanced", per_account_split={"DU1": 10},
                      instrument=spy, price=100.0, side="BUY", now=NOW)
    r1 = b.reconcile("DU1", {spy: 5.0}, -1000.0, [], day=DAY, is_eod=False, now=NOW)
    r2 = b.reconcile("DU1", {spy: 5.0}, -1000.0, [], day=DAY, is_eod=False, now=NOW)
    assert r1.alerted is True
    assert r2.latched is True and r2.alerted is False    # absorbed by the active latch


# ===========================================================================
# 6) intraday unexplained cash drift → UNEXPLAINED_PENDING, NOT latched
# ===========================================================================
def test_intraday_unexplained_cash_drift_not_latched():
    b = _brain()
    spy = Instrument.stock("SPY")
    b.attribute_block(fa_group="tier_balanced", per_account_split={"DU1": 10},
                      instrument=spy, price=100.0, side="BUY", now=NOW)
    # Positions match; broker cash is off by $50 with no labeled transaction to explain it.
    report = b.reconcile("DU1", {spy: 10.0}, -950.0, [],
                         day=DAY, is_eod=False, now=NOW)
    assert report.recon.cash_status == "CASH_DRIFT"
    assert report.outcome is latch.TriageOutcome.UNEXPLAINED_PENDING
    assert report.latched is False                       # held, not latched (intraday)
    assert b.preflight_excluded("DU1", DAY) is False


def test_intraday_cash_drift_explained_books_no_latch():
    """An intraday cash drift a labeled transaction explains → EXPLAINED, not latched."""
    b = _brain()
    spy = Instrument.stock("SPY")
    b.attribute_block(fa_group="tier_balanced", per_account_split={"DU1": 10},
                      instrument=spy, price=100.0, side="BUY", now=NOW)
    # Broker cash +50 vs ledger; a labeled dividend of +50 explains it.
    div = latch.LabeledTransaction(account_id="DU1", symbol="SPY", amount=50.0,
                                   kind="DIVIDEND", date=DAY)
    report = b.reconcile("DU1", {spy: 10.0}, -950.0, [div],
                         day=DAY, is_eod=False, now=NOW)
    assert report.outcome is latch.TriageOutcome.EXPLAINED
    assert report.latched is False


# ===========================================================================
# 7) preflight_floor_check — latches BELOW_FLOOR under the floor, returns True
# ===========================================================================
def test_preflight_floor_check_latches_below_floor():
    b = _brain()
    # Investable $5,000 under a blessed floor of $20,000 → sits out.
    excluded = b.preflight_floor_check("DU1", 5_000.0, 20_000.0, day=DAY, now=NOW)
    assert excluded is True
    assert b.preflight_excluded("DU1", DAY) is True
    active = b.latches.active_latches(DAY)
    assert active[0].fault is latch.FaultType.BELOW_FLOOR
    # The below-floor alert points at reassignment, not retry (§12.3).
    assert active[0].alert is latch.AlertType.TEMPLATE_NO_LONGER_QUALIFIES


def test_preflight_floor_check_at_or_above_floor_ok():
    b = _brain()
    # Exactly at the floor is NOT below (>= floor is fine).
    assert b.preflight_floor_check("DU1", 20_000.0, 20_000.0, day=DAY, now=NOW) is False
    assert b.preflight_floor_check("DU2", 50_000.0, 20_000.0, day=DAY, now=NOW) is False
    assert b.preflight_excluded("DU1", DAY) is False


# ===========================================================================
# 8) whole-brain to_dict / from_dict round-trip
# ===========================================================================
def test_brain_roundtrip_preserves_assignments_ledger_latches():
    b = _brain()
    caps = _caps(account_id="DUrich")
    # Assignment (with a supersede so history is non-trivial).
    b.assign("DUrich", "balanced", caps, set_by="andrew", now=NOW)
    b.assign("DUrich", "balanced_overlay", caps, set_by="andrew",
             now=datetime(2026, 7, 24, 10, 0, 0))
    # Ledger state.
    spy = Instrument.stock("SPY")
    b.attribute_block(fa_group="tier_balanced", per_account_split={"DUrich": 10},
                      instrument=spy, price=100.0, side="BUY", now=NOW)
    # A latch (below-floor pre-flight on another account).
    b.preflight_floor_check("DUsmall", 1_000.0, 20_000.0, day=DAY, now=NOW)

    back = CRMBrain.from_dict(b.to_dict())

    # Assignments preserved (current + full history).
    assert back.current_assignment("DUrich").template_id == "balanced_overlay"
    assert len(back.assignment_history("DUrich")) == 2
    # Group membership re-derives identically.
    assert back.group_membership() == b.group_membership()
    # Ledger preserved.
    assert back.blended_positions("DUrich") == b.blended_positions("DUrich")
    assert back.blended_cash("DUrich") == b.blended_cash("DUrich")
    # Latch preserved — DUsmall still excluded at pre-flight after the round-trip.
    assert back.preflight_excluded("DUsmall", DAY) is True


def test_brain_roundtrip_preserves_soft_warnings():
    b = _brain()
    thin = _caps(account_id="DUthin", buying_power=100_000.0, excess_liquidity=300_000.0)
    b.assign("DUthin", "balanced_overlay", thin, set_by="andrew", now=NOW)
    back = CRMBrain.from_dict(b.to_dict())
    assert back.assignment_warnings("DUthin") == b.assignment_warnings("DUthin")
    assert back.assignment_warnings("DUthin")   # non-empty, survived the round-trip


def test_brain_roundtrip_empty():
    b = _brain()
    back = CRMBrain.from_dict(b.to_dict())
    assert set(back.templates) == {"balanced", "balanced_overlay"}
    assert back.group_membership() == {}
