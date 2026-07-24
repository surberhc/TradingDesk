"""test_store.py — offline tests for the CRM SQLite TRANSPORT layer (#42/#43).

All tests use a TMP db path (tmp_path fixture) — NEVER the real off-Drive default DB. Runs
with zero infra:
    cd crm
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest -q

The store's contract is fidelity to the brain's already-tested serialization: it decomposes
CRMBrain.to_dict() into normalized rows and reassembles the identical dict on load. The
load-bearing assertion throughout is therefore
    store.load_brain().to_dict() == brain.to_dict()
plus the incremental-writer / change-detection behavior SQLite was chosen to give us.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

import domain
from domain import Template, AccountAssignment, AssignmentBook
import ledger
from ledger import Instrument, SleeveLedger, SleeveLedgerEntry
import latch
from latch import Latch, LatchBook, FaultType, AlertType
import brain
from brain import CRMBrain

from store import CRMStore, DEFAULT_DB_PATH


# ===========================================================================
# Fixtures — a fully POPULATED brain built through the entities' own APIs
# ===========================================================================
DAY = date(2026, 7, 24)
DAY2 = date(2026, 7, 23)

T1 = datetime(2026, 7, 20, 9, 0, 0)
T2 = datetime(2026, 7, 22, 10, 30, 0)
T3 = datetime(2026, 7, 23, 14, 0, 0)
T4 = datetime(2026, 7, 24, 9, 5, 0)


TEMPLATES = {
    "balanced": Template(template_id="balanced", name="Balanced ETF-only",
                         weights={"S0-Balanced": 1.0}, active=True),
    # active=False exercises the boolean round-trip through the INTEGER column.
    "balanced_overlay": Template(
        template_id="balanced_overlay", name="Balanced + S8 Overlay",
        weights={"S0-Balanced": 0.75, "S8-Overlay": 0.25}, active=False),
}


def _populated_brain() -> CRMBrain:
    """A brain with something in every book: templates (incl an inactive one), an assignment
    history containing a SUPERSEDE, ledger entries with equity + option positions, a current
    latch, a cleared+re-latched pair (producing a superseded audit row), and assign_warnings."""
    # --- assignments: A assigned then reassigned (supersede); B assigned once ------------
    ab = AssignmentBook()
    ab.assign("DU-A", "balanced", "andrew", now=T1)
    ab.assign("DU-A", "balanced_overlay", "andrew", now=T2)   # supersede -> prior recorded
    ab.assign("DU-B", "balanced", "andrew", now=T3)

    # --- ledger: an equity leg + two option legs; plus a line opened and fully closed ----
    led = SleeveLedger()
    led.attribute_fill("DU-A", "S0-Balanced", Instrument.stock("SPY"), 100, 500.0, now=T2)
    led.attribute_fill("DU-B", "S0-Balanced", Instrument.stock("AGG"), 40, 100.0, now=T3)
    led.attribute_fill(
        "DU-A", "S8-Overlay",
        Instrument.option("SPX", "20260724", 5000.0, "P", con_id=111), -1, 3.20, now=T4)
    led.attribute_fill(
        "DU-A", "S8-Overlay",
        Instrument.option("SPX", "20260724", 4900.0, "P", con_id=222), 1, 1.10, now=T4)
    # Open then fully close a line — it must leave NO position row (fully-closed line).
    led.attribute_fill("DU-B", "S0-Balanced", Instrument.stock("GLD"), 10, 200.0, now=T3)
    led.attribute_fill("DU-B", "S0-Balanced", Instrument.stock("GLD"), -10, 205.0, now=T3)

    # --- latches: A currently latched; B cleared then re-latched (supersede audit row) ---
    lb = LatchBook()
    lb.latch("DU-A", DAY, FaultType.LEDGER_DRIFT, "ledger drift on SPY", now=T4)
    lb.latch("DU-B", DAY2, FaultType.UNEXPLAINED_TRANSACTION, "cash drift $50", now=T1)
    lb.clear("DU-B", DAY2, "andrew", now=T2)
    lb.latch("DU-B", DAY2, FaultType.BELOW_FLOOR, "fell under floor", now=T3)  # supersedes

    brn = CRMBrain(TEMPLATES, assignments=ab, ledger=led, latches=lb)
    # assign_warnings are surfaced advisory soft-warnings (set as assign() would).
    brn._assign_warnings = {"DU-A": ["buying-power cushion below advisory floor"]}
    return brn


def _store(tmp_path) -> CRMStore:
    return CRMStore(db_path=tmp_path / "crm.db")


# ===========================================================================
# 0) Tests must NEVER touch the real off-Drive DB
# ===========================================================================
def test_uses_tmp_path_not_real_db(tmp_path):
    st = _store(tmp_path)
    try:
        assert st.db_path != DEFAULT_DB_PATH
        assert str(tmp_path) in str(st.db_path)
        assert st.db_path.exists()
    finally:
        st.close()


# ===========================================================================
# 1) Full round-trip of a populated brain — the load-bearing invariant
# ===========================================================================
def test_full_roundtrip_populated_brain(tmp_path):
    brn = _populated_brain()
    st = _store(tmp_path)
    try:
        st.save_brain(brn, now=T4)
        reloaded = st.load_brain()
        assert reloaded.to_dict() == brn.to_dict()
    finally:
        st.close()


def test_roundtrip_survives_reopen(tmp_path):
    """A second CRMStore over the SAME file sees the persisted payload (real durability)."""
    brn = _populated_brain()
    st = _store(tmp_path)
    st.save_brain(brn, now=T4)
    st.close()

    st2 = CRMStore(db_path=tmp_path / "crm.db")
    try:
        assert st2.load_brain().to_dict() == brn.to_dict()
    finally:
        st2.close()


def test_roundtrip_preserves_supersede_and_current(tmp_path):
    brn = _populated_brain()
    st = _store(tmp_path)
    try:
        st.save_brain(brn, now=T4)
        r = st.load_brain()
        # Supersede: A's history has two rows, the second recording the prior template.
        hist_a = r.assignment_history("DU-A")
        assert [h.template_id for h in hist_a] == ["balanced", "balanced_overlay"]
        assert hist_a[1].prior_template_id == "balanced"
        # Derived current-per-account matches the last row.
        assert r.current_assignment("DU-A").template_id == "balanced_overlay"
        assert r.current_assignment("DU-B").template_id == "balanced"
        # Latch supersede audit row survived (cleared+re-latched B).
        assert len(r.latches.to_dict()["superseded"]) == 1
    finally:
        st.close()


def test_fully_closed_line_has_no_position_row(tmp_path):
    brn = _populated_brain()
    st = _store(tmp_path)
    try:
        st.save_brain(brn, now=T4)
        # GLD was bought then fully sold on DU-B/S0-Balanced -> no position row persisted.
        rows = st._conn.execute(
            "SELECT instrument_key FROM ledger_positions WHERE account_id='DU-B'"
        ).fetchall()
        keys = [row["instrument_key"] for row in rows]
        assert not any(k.startswith("GLD|") for k in keys)
        # AGG (still held) IS present.
        assert any(k.startswith("AGG|") for k in keys)
    finally:
        st.close()


def test_normalized_not_a_blob(tmp_path):
    """Sanity: the schema really is normalized (row-per-thing), the whole point of SQLite."""
    brn = _populated_brain()
    st = _store(tmp_path)
    try:
        st.save_brain(brn, now=T4)
        assert st._conn.execute("SELECT COUNT(*) c FROM templates").fetchone()["c"] == 2
        assert st._conn.execute("SELECT COUNT(*) c FROM assignments").fetchone()["c"] == 3
        assert st._conn.execute(
            "SELECT COUNT(*) c FROM ledger_positions").fetchone()["c"] >= 3
        assert st._conn.execute("SELECT COUNT(*) c FROM latches").fetchone()["c"] == 2
        assert st._conn.execute(
            "SELECT COUNT(*) c FROM latches_superseded").fetchone()["c"] == 1
    finally:
        st.close()


# ===========================================================================
# 2) Cold start — a fresh empty DB loads an empty-but-valid brain
# ===========================================================================
def test_cold_start_empty_brain(tmp_path):
    st = _store(tmp_path)
    try:
        empty = st.load_brain()
        assert isinstance(empty, CRMBrain)
        assert empty.to_dict() == CRMBrain({}).to_dict()
        assert st.version() == 0
        assert st.updated_at() is None
    finally:
        st.close()


# ===========================================================================
# 3) Change detection (§8) — version increments, updated_at changes, per write
# ===========================================================================
def test_version_and_updated_at_advance_per_write(tmp_path):
    brn = _populated_brain()
    st = _store(tmp_path)
    try:
        assert st.version() == 0
        st.save_brain(brn, now=T1)
        assert st.version() == 1
        assert st.updated_at() == T1.isoformat()

        st.save_brain(brn, now=T2)
        assert st.version() == 2
        assert st.updated_at() == T2.isoformat()

        # An incremental write ALSO bumps version + updated_at.
        entry = brn.ledger.entry("DU-A", "S0-Balanced")
        st.upsert_ledger_entry(entry, now=T3)
        assert st.version() == 3
        assert st.updated_at() == T3.isoformat()
    finally:
        st.close()


def test_explicit_version_override(tmp_path):
    brn = _populated_brain()
    st = _store(tmp_path)
    try:
        st.save_brain(brn, now=T1, version=42)
        assert st.version() == 42
        st.save_brain(brn, now=T2)   # monotonic bump from the override
        assert st.version() == 43
    finally:
        st.close()


# ===========================================================================
# 4) Incremental upsert_ledger_entry — reload reflects the mutation
# ===========================================================================
def test_incremental_upsert_ledger_entry(tmp_path):
    brn = _populated_brain()
    st = _store(tmp_path)
    try:
        st.save_brain(brn, now=T1)
        # Mutate one entry in memory: add 50 more SPY to DU-A/S0-Balanced.
        brn.ledger.attribute_fill(
            "DU-A", "S0-Balanced", Instrument.stock("SPY"), 50, 510.0, now=T2)
        entry = brn.ledger.entry("DU-A", "S0-Balanced")
        st.upsert_ledger_entry(entry, now=T2)

        reloaded = st.load_brain()
        rentry = reloaded.ledger.entry("DU-A", "S0-Balanced")
        spy = Instrument.stock("SPY")
        assert rentry.attributed_positions[spy] == 150
        assert rentry.attributed_cash == entry.attributed_cash
        assert rentry.ledger_version == entry.ledger_version
    finally:
        st.close()


def test_incremental_upsert_ledger_entry_drops_closed_line(tmp_path):
    brn = _populated_brain()
    st = _store(tmp_path)
    try:
        st.save_brain(brn, now=T1)
        # Sell the entire SPY line -> the position row must disappear on upsert.
        brn.ledger.attribute_fill(
            "DU-A", "S0-Balanced", Instrument.stock("SPY"), -100, 505.0, now=T2)
        st.upsert_ledger_entry(brn.ledger.entry("DU-A", "S0-Balanced"), now=T2)
        rows = st._conn.execute(
            "SELECT instrument_key FROM ledger_positions "
            "WHERE account_id='DU-A' AND sleeve_id='S0-Balanced'").fetchall()
        assert not any(r["instrument_key"].startswith("SPY|") for r in rows)
    finally:
        st.close()


# ===========================================================================
# 5) Incremental append_assignment — history order + derived current
# ===========================================================================
def test_incremental_append_assignment(tmp_path):
    brn = _populated_brain()
    st = _store(tmp_path)
    try:
        st.save_brain(brn, now=T1)
        # A fresh assignment for a NEW account, appended incrementally.
        a = AccountAssignment(
            account_id="DU-C", template_id="balanced",
            effective_at=T4, set_by="andrew", set_at=T4, prior_template_id=None)
        st.append_assignment(a, now=T4)

        reloaded = st.load_brain()
        assert reloaded.current_assignment("DU-C").template_id == "balanced"
        # Existing history untouched and still ordered.
        hist_a = reloaded.assignment_history("DU-A")
        assert [h.template_id for h in hist_a] == ["balanced", "balanced_overlay"]

        # Re-point DU-A via an appended supersede row; derived current updates, history grows.
        a2 = AccountAssignment(
            account_id="DU-A", template_id="balanced",
            effective_at=T4, set_by="andrew", set_at=T4,
            prior_template_id="balanced_overlay")
        st.append_assignment(a2, now=T4)
        reloaded2 = st.load_brain()
        assert reloaded2.current_assignment("DU-A").template_id == "balanced"
        assert [h.template_id for h in reloaded2.assignment_history("DU-A")] == [
            "balanced", "balanced_overlay", "balanced"]
    finally:
        st.close()


# ===========================================================================
# 6) Incremental upsert_latch and set_template
# ===========================================================================
def test_incremental_upsert_latch(tmp_path):
    brn = _populated_brain()
    st = _store(tmp_path)
    try:
        st.save_brain(brn, now=T1)
        new = Latch(
            account_id="DU-C", day=DAY, fault=FaultType.LEDGER_DRIFT,
            alert=AlertType.TRADE_SKIPPED_LATCHED, reason="drift on QQQ", latched_at=T4)
        st.upsert_latch(new, now=T4)
        reloaded = st.load_brain()
        assert reloaded.latches.is_latched("DU-C", DAY)
    finally:
        st.close()


def test_incremental_set_template(tmp_path):
    brn = _populated_brain()
    st = _store(tmp_path)
    try:
        st.save_brain(brn, now=T1)
        t = Template(template_id="growth", name="Growth ETF-only",
                     weights={"S0-Growth": 1.0}, active=True)
        st.set_template(t, now=T4)
        reloaded = st.load_brain()
        assert "growth" in reloaded.templates
        assert reloaded.templates["growth"].weights == {"S0-Growth": 1.0}
    finally:
        st.close()


# ===========================================================================
# 7) Re-save after a removal deletes the stale rows
# ===========================================================================
def test_resave_reflects_removals(tmp_path):
    brn = _populated_brain()
    st = _store(tmp_path)
    try:
        st.save_brain(brn, now=T1)
        assert st._conn.execute("SELECT COUNT(*) c FROM templates").fetchone()["c"] == 2

        # Remove a template and an assign_warning, then re-save the snapshot.
        del brn.templates["balanced_overlay"]
        brn._assign_warnings = {}
        st.save_brain(brn, now=T2)

        assert st._conn.execute("SELECT COUNT(*) c FROM templates").fetchone()["c"] == 1
        assert st._conn.execute(
            "SELECT COUNT(*) c FROM assign_warnings").fetchone()["c"] == 0
        # And the reloaded brain matches the trimmed state exactly.
        assert st.load_brain().to_dict() == brn.to_dict()
    finally:
        st.close()


def test_resave_removes_stale_ledger_entry(tmp_path):
    brn = _populated_brain()
    st = _store(tmp_path)
    try:
        st.save_brain(brn, now=T1)
        # Drop DU-A's S8 overlay entry entirely, re-save; its positions must vanish too.
        brn.ledger._entries.pop(("DU-A", "S8-Overlay"))
        st.save_brain(brn, now=T2)
        n_entries = st._conn.execute(
            "SELECT COUNT(*) c FROM ledger_entries WHERE sleeve_id='S8-Overlay'"
        ).fetchone()["c"]
        n_pos = st._conn.execute(
            "SELECT COUNT(*) c FROM ledger_positions WHERE sleeve_id='S8-Overlay'"
        ).fetchone()["c"]
        assert n_entries == 0
        assert n_pos == 0
        assert st.load_brain().to_dict() == brn.to_dict()
    finally:
        st.close()
