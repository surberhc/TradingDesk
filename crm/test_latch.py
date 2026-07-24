"""test_latch.py — offline unit tests for the CRM fault-latch + triage lifecycle (#42/#43).

Pure/offline: no broker, no gateway, no I/O. Runs with zero infra:
    cd crm
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

import ledger
from ledger import Instrument, SleeveLedger, reconcile_account
from latch import (
    DRIFT_TOL,
    FaultType,
    AlertType,
    alert_for_fault,
    Latch,
    LatchBook,
    LabeledTransaction,
    explain_drift,
    TriageOutcome,
    triage_reconcile,
    fault_for_latch,
    below_floor,
)


DAY = date(2026, 7, 24)
DAY2 = date(2026, 7, 25)
NOW = datetime(2026, 7, 24, 15, 30, 0)
LATER = datetime(2026, 7, 24, 16, 0, 0)


# ===========================================================================
# Helpers — build ReconResult snapshots via the real ledger (no re-implementation)
# ===========================================================================
def _ledger_with(account, positions, cash):
    """A one-sleeve ledger holding `positions` (Instrument->qty) and `cash` for `account`."""
    led = SleeveLedger()
    e = led.entry(account, "S0-Balanced")
    for inst, qty in positions.items():
        e.attributed_positions[inst] = qty
    e.attributed_cash = cash
    return led


def _recon(account, ledger_positions, ledger_cash, broker_positions, broker_cash):
    led = _ledger_with(account, ledger_positions, ledger_cash)
    return reconcile_account(led, account, broker_positions, broker_cash)


SPY = Instrument.stock("SPY")
QQQ = Instrument.stock("QQQ")


# ===========================================================================
# 1) Fault → alert mapping  (§12.3)
# ===========================================================================
def test_alert_for_fault_mapping():
    assert alert_for_fault(FaultType.LEDGER_DRIFT) is AlertType.TRADE_SKIPPED_LATCHED
    assert alert_for_fault(FaultType.UNEXPLAINED_TRANSACTION) is AlertType.TRADE_SKIPPED_LATCHED
    assert alert_for_fault(FaultType.BELOW_FLOOR) is AlertType.TEMPLATE_NO_LONGER_QUALIFIES


def test_alert_covers_every_fault_type():
    for f in FaultType:
        assert isinstance(alert_for_fault(f), AlertType)


# ===========================================================================
# 2) Latch alert-once semantics  (§12.3)
# ===========================================================================
def test_first_latch_alerts_and_derives_alert_type():
    book = LatchBook()
    latch, alerted = book.latch("DU1", DAY, FaultType.LEDGER_DRIFT, "drift", now=NOW)
    assert alerted is True
    assert latch.account_id == "DU1"
    assert latch.day == DAY
    assert latch.fault is FaultType.LEDGER_DRIFT
    assert latch.alert is AlertType.TRADE_SKIPPED_LATCHED  # derived from fault
    assert latch.latched_at == NOW
    assert latch.cleared is False


def test_second_same_day_latch_does_not_realert_or_duplicate():
    book = LatchBook()
    first, a1 = book.latch("DU1", DAY, FaultType.LEDGER_DRIFT, "drift", now=NOW)
    second, a2 = book.latch("DU1", DAY, FaultType.LEDGER_DRIFT, "drift again", now=LATER)
    assert a1 is True and a2 is False          # alert once
    assert second is first                     # unchanged existing latch returned
    assert second.reason == "drift"            # NOT overwritten
    assert len(book.all_latches()) == 1        # no duplicate


def test_different_fault_while_latched_still_no_realert():
    # Design choice: the account is already out; a DIFFERENT fault does not stack a 2nd alert.
    book = LatchBook()
    first, a1 = book.latch("DU1", DAY, FaultType.LEDGER_DRIFT, "pos drift", now=NOW)
    second, a2 = book.latch("DU1", DAY, FaultType.UNEXPLAINED_TRANSACTION, "cash", now=LATER)
    assert a1 is True and a2 is False
    assert second is first
    assert second.fault is FaultType.LEDGER_DRIFT  # first fault kept
    assert len(book.all_latches()) == 1


# ===========================================================================
# 3) is_latched / excluded_at_preflight  (§12.2/§12.3)
# ===========================================================================
def test_is_latched_and_preflight_alias():
    book = LatchBook()
    assert book.is_latched("DU1", DAY) is False
    assert book.excluded_at_preflight("DU1", DAY) is False
    book.latch("DU1", DAY, FaultType.LEDGER_DRIFT, "drift", now=NOW)
    assert book.is_latched("DU1", DAY) is True
    assert book.excluded_at_preflight("DU1", DAY) is True
    # A different day is unaffected.
    assert book.is_latched("DU1", DAY2) is False


def test_preflight_check_never_alerts():
    # Calling the pre-flight gate must not create or re-fire anything.
    book = LatchBook()
    book.latch("DU1", DAY, FaultType.LEDGER_DRIFT, "drift", now=NOW)
    before = len(book.all_latches())
    for _ in range(5):
        assert book.excluded_at_preflight("DU1", DAY) is True
    assert len(book.all_latches()) == before   # untouched


# ===========================================================================
# 4) Human clear re-enables; a new fault after clear alerts again  (§12.3)
# ===========================================================================
def test_clear_marks_fields_and_reenables():
    book = LatchBook()
    book.latch("DU1", DAY, FaultType.LEDGER_DRIFT, "drift", now=NOW)
    assert book.clear("DU1", DAY, by="andrew", now=LATER) is True
    assert book.is_latched("DU1", DAY) is False       # re-enabled
    cleared = book._current[("DU1", DAY)]
    assert cleared.cleared is True
    assert cleared.cleared_by == "andrew"
    assert cleared.cleared_at == LATER


def test_clear_nothing_to_clear_returns_false():
    book = LatchBook()
    assert book.clear("DU1", DAY, by="andrew", now=LATER) is False   # never latched
    book.latch("DU1", DAY, FaultType.LEDGER_DRIFT, "drift", now=NOW)
    assert book.clear("DU1", DAY, by="andrew", now=LATER) is True
    assert book.clear("DU1", DAY, by="andrew", now=LATER) is False   # already cleared


def test_new_fault_after_clear_alerts_again():
    book = LatchBook()
    book.latch("DU1", DAY, FaultType.LEDGER_DRIFT, "first", now=NOW)
    book.clear("DU1", DAY, by="andrew", now=LATER)
    latch2, alerted = book.latch("DU1", DAY, FaultType.UNEXPLAINED_TRANSACTION, "second",
                                 now=LATER)
    assert alerted is True                       # fresh fault → fresh alert
    assert book.is_latched("DU1", DAY) is True
    assert latch2.fault is FaultType.UNEXPLAINED_TRANSACTION
    # The cleared original is preserved in the audit trail.
    all_l = book.all_latches()
    assert len(all_l) == 2
    assert any(l.cleared and l.reason == "first" for l in all_l)


# ===========================================================================
# 5) active_latches / all_latches
# ===========================================================================
def test_active_and_all_latches():
    book = LatchBook()
    book.latch("DU1", DAY, FaultType.LEDGER_DRIFT, "d1", now=NOW)
    book.latch("DU2", DAY, FaultType.UNEXPLAINED_TRANSACTION, "d2", now=NOW)
    book.latch("DU3", DAY2, FaultType.BELOW_FLOOR, "d3", now=NOW)
    active_day1 = book.active_latches(DAY)
    assert [l.account_id for l in active_day1] == ["DU1", "DU2"]   # sorted, DAY only
    # Clear one → drops out of active but stays in all.
    book.clear("DU1", DAY, by="andrew", now=LATER)
    assert [l.account_id for l in book.active_latches(DAY)] == ["DU2"]
    assert len(book.all_latches()) == 3


# ===========================================================================
# 6) explain_drift  (§12.1)
# ===========================================================================
def _txn(amount, kind="DIVIDEND", symbol="SPY", d=DAY):
    return LabeledTransaction(account_id="DU1", symbol=symbol, amount=amount, kind=kind, date=d)


def test_explain_drift_exact_match():
    txns = [_txn(50.0, "DIVIDEND"), _txn(-2.5, "FEE")]
    m = explain_drift(50.0, txns)
    assert m is not None and m.kind == "DIVIDEND" and m.amount == 50.0


def test_explain_drift_within_tolerance():
    m = explain_drift(50.4, [_txn(50.0, "DIVIDEND")], tol=DRIFT_TOL)   # |50 - 50.4| = 0.4 <= 1.0
    assert m is not None and m.kind == "DIVIDEND"


def test_explain_drift_near_miss_outside_tol_returns_none():
    assert explain_drift(52.0, [_txn(50.0, "DIVIDEND")], tol=DRIFT_TOL) is None  # gap 2.0 > 1.0


def test_explain_drift_no_transactions_returns_none():
    assert explain_drift(50.0, []) is None


def test_explain_drift_prefers_closest():
    txns = [_txn(50.9, "INTEREST"), _txn(50.1, "DIVIDEND")]
    m = explain_drift(50.0, txns, tol=DRIFT_TOL)
    assert m is not None and m.kind == "DIVIDEND"    # 50.1 is closer than 50.9


# ===========================================================================
# 7) triage_reconcile  (§12.1) — CLEAN / EXPLAINED / PENDING / LATCH
# ===========================================================================
def test_triage_clean():
    recon = _recon("DU1", {SPY: 100.0}, 1000.0, {SPY: 100.0}, 1000.0)
    assert recon.verdict == "OK"
    outcome, reason = triage_reconcile(recon, [], is_eod=False)
    assert outcome is TriageOutcome.CLEAN


def test_triage_explained_dividend():
    # Broker credited a $50 dividend the ledger hasn't booked: broker_cash = ledger + 50.
    recon = _recon("DU1", {SPY: 100.0}, 1000.0, {SPY: 100.0}, 1050.0)
    assert recon.cash_status == "CASH_DRIFT" and not recon.drift_instruments
    txns = [_txn(50.0, "DIVIDEND")]      # amount frame = broker_cash - ledger_cash = +50
    outcome, reason = triage_reconcile(recon, txns, is_eod=False)
    assert outcome is TriageOutcome.EXPLAINED
    assert "DIVIDEND" in reason


def test_triage_cash_drift_unexplained_intraday_is_pending_not_latched():
    recon = _recon("DU1", {SPY: 100.0}, 1000.0, {SPY: 100.0}, 1050.0)
    outcome, reason = triage_reconcile(recon, [], is_eod=False)   # nothing explains it
    assert outcome is TriageOutcome.UNEXPLAINED_PENDING
    assert fault_for_latch(recon) is FaultType.UNEXPLAINED_TRANSACTION


def test_triage_cash_drift_unexplained_eod_latches():
    recon = _recon("DU1", {SPY: 100.0}, 1000.0, {SPY: 100.0}, 1050.0)
    outcome, reason = triage_reconcile(recon, [], is_eod=True)    # residual survived the sweep
    assert outcome is TriageOutcome.LATCH
    assert fault_for_latch(recon) is FaultType.UNEXPLAINED_TRANSACTION


def test_triage_hard_ledger_drift_latches_regardless_of_timing():
    # Ledger says 100 SPY, broker says 90 → LEDGER_DRIFT (position mismatch).
    recon = _recon("DU1", {SPY: 100.0}, 1000.0, {SPY: 90.0}, 1000.0)
    assert recon.drift_instruments
    for is_eod in (False, True):
        outcome, reason = triage_reconcile(recon, [], is_eod=is_eod)
        assert outcome is TriageOutcome.LATCH
    assert fault_for_latch(recon) is FaultType.LEDGER_DRIFT


def test_triage_position_drift_dominates_even_with_explainable_cash():
    # Both a position drift AND a cash drift that a transaction could explain: position wins.
    recon = _recon("DU1", {SPY: 100.0}, 1000.0, {SPY: 90.0}, 1050.0)
    txns = [_txn(50.0, "DIVIDEND")]
    outcome, reason = triage_reconcile(recon, txns, is_eod=False)
    assert outcome is TriageOutcome.LATCH
    assert "LEDGER_DRIFT" in reason
    assert fault_for_latch(recon) is FaultType.LEDGER_DRIFT


def test_triage_review_alien_intraday_pending_eod_latch():
    # Broker holds QQQ the ledger attributes zero of → ALIEN → verdict REVIEW, no hard drift.
    recon = _recon("DU1", {SPY: 100.0}, 1000.0, {SPY: 100.0, QQQ: 10.0}, 1000.0)
    assert recon.verdict == "REVIEW" and recon.alien_instruments and not recon.drift_instruments
    out_intraday, _ = triage_reconcile(recon, [], is_eod=False)
    assert out_intraday is TriageOutcome.UNEXPLAINED_PENDING
    out_eod, _ = triage_reconcile(recon, [], is_eod=True)
    assert out_eod is TriageOutcome.LATCH
    assert fault_for_latch(recon) is FaultType.LEDGER_DRIFT   # unattributable holding


def test_triage_decision_then_caller_latches():
    # The documented split: triage decides, caller applies LatchBook.latch.
    recon = _recon("DU1", {SPY: 100.0}, 1000.0, {SPY: 90.0}, 1000.0)
    outcome, reason = triage_reconcile(recon, [], is_eod=True)
    book = LatchBook()
    if outcome is TriageOutcome.LATCH:
        latch, alerted = book.latch("DU1", DAY, fault_for_latch(recon), reason, now=NOW)
    assert alerted is True
    assert book.is_latched("DU1", DAY) is True
    assert book._current[("DU1", DAY)].alert is AlertType.TRADE_SKIPPED_LATCHED


# ===========================================================================
# 8) below_floor  (§12.4) — apply the given floor, boundary
# ===========================================================================
def test_below_floor_boundary():
    assert below_floor(999.0, 1000.0) is True     # under → fault
    assert below_floor(1000.0, 1000.0) is False   # exactly AT the floor is NOT below
    assert below_floor(1001.0, 1000.0) is False   # above is fine


def test_below_floor_maps_to_reassignment_alert():
    # Below-floor is the situation-changed fault → TEMPLATE_NO_LONGER_QUALIFIES.
    assert alert_for_fault(FaultType.BELOW_FLOOR) is AlertType.TEMPLATE_NO_LONGER_QUALIFIES


# ===========================================================================
# 9) to_dict / from_dict round-trips
# ===========================================================================
def test_latch_roundtrip_active_and_cleared():
    active = Latch(account_id="DU1", day=DAY, fault=FaultType.LEDGER_DRIFT,
                   alert=AlertType.TRADE_SKIPPED_LATCHED, reason="drift", latched_at=NOW)
    assert Latch.from_dict(active.to_dict()) == active
    cleared = Latch(account_id="DU1", day=DAY, fault=FaultType.BELOW_FLOOR,
                    alert=AlertType.TEMPLATE_NO_LONGER_QUALIFIES, reason="withdrawal",
                    latched_at=NOW, cleared=True, cleared_by="andrew", cleared_at=LATER)
    assert Latch.from_dict(cleared.to_dict()) == cleared


def test_labeled_transaction_roundtrip():
    txn = LabeledTransaction(account_id="DU1", symbol="SPY", amount=50.0, kind="DIVIDEND",
                             date=DAY)
    assert LabeledTransaction.from_dict(txn.to_dict()) == txn
    # symbol may be None (an account-level fee/interest with no symbol).
    txn2 = LabeledTransaction(account_id="DU1", symbol=None, amount=-3.0, kind="FEE", date=DAY)
    assert LabeledTransaction.from_dict(txn2.to_dict()) == txn2


def test_latchbook_roundtrip_with_active_cleared_and_superseded():
    book = LatchBook()
    book.latch("DU1", DAY, FaultType.LEDGER_DRIFT, "first", now=NOW)
    book.clear("DU1", DAY, by="andrew", now=LATER)
    book.latch("DU1", DAY, FaultType.UNEXPLAINED_TRANSACTION, "second", now=LATER)  # supersedes
    book.latch("DU2", DAY2, FaultType.BELOW_FLOOR, "floor", now=NOW)

    round_tripped = LatchBook.from_dict(book.to_dict())
    assert {l.reason for l in round_tripped.all_latches()} == {"first", "second", "floor"}
    assert round_tripped.is_latched("DU1", DAY) is True
    assert round_tripped.is_latched("DU2", DAY2) is True
    # The superseded cleared "first" survives the round-trip as an audit row.
    assert any(l.cleared and l.reason == "first" for l in round_tripped.all_latches())
