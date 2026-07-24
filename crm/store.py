"""store.py — the CRM SQLite TRANSPORT layer (conductor #42/#43).

The persistence boundary the pure brain (crm/brain.py) deliberately left OPEN (spec §8 /
§10.2 — "transport is the open question; do not build until chosen"). Andrew chose SQLite
over versioned JSON because the sleeve ledger MUTATES on every fill and needs an audit
trail + safe concurrent read/write + native queryability — exactly what conductor.db already
gives the desk (conductor/db.py). This module is the "CRM writes / desk reads a versioned
payload" contract of docs/CRM_DESIGN_groups_brain.md §8.

WHAT IS DIFFERENT ABOUT THIS LAYER (and what is NOT):
  * IT IS THE TRANSPORT, so — unlike domain/ledger/latch/brain — it IS allowed to do I/O:
    sqlite3 (stdlib) + the filesystem for the DB file. That is the whole point of the slice.
  * BUT the dependency wall still stands. It imports ONLY sqlite3 / json / pathlib (stdlib)
    and the pure crm.* modules (brain / domain — which themselves pull ledger / latch /
    capability). NO broker, NO ib_async, NO paperbot / config / order path, NO gateway. It
    PERSISTS the brain; it never touches an account or an order.

FIDELITY STRATEGY (least-risk): the brain and its entities already have TESTED
to_dict()/from_dict() serialization. This store does NOT invent a second encoding — it
DECOMPOSES CRMBrain.to_dict() into normalized rows on write and REASSEMBLES the identical
dict shape on read, then hands it to CRMBrain.from_dict(). The round-trip invariant is
therefore `CRMStore(...).load_brain().to_dict() == brain.to_dict()`.

WHY NORMALIZED, NOT ONE JSON BLOB: the entire reason to pick SQLite over versioned JSON is
row-level mutation + query + audit. A single blob would throw that away. So the schema is
normalized (one row per template / assignment / ledger entry / position / latch), the
append-only tables (assignments, latches_superseded) ARE the audit trail, and the
incremental writers below mutate ONE row per event instead of rewriting the whole payload.

CHANGE DETECTION (§8): meta carries a monotonic `brain_version` + `updated_at`, bumped in
the SAME transaction as every write, so the desk can poll `version()` / `updated_at()` to
learn a new payload exists without diffing it.

TIME IS INJECTABLE (no hidden clock): every writer accepts an optional `now` (and
`brain_version` an optional `version`) so tests fully control the audit timestamps — the
store never reaches for an uncontrollable wall clock a test can't pin.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional

import brain as _brain          # crm.brain — pure; CRMBrain.to_dict()/from_dict()
import domain                   # crm.domain — pure; SLEEVE_REGISTRY default for load

# --- DB location: OFF Drive, mirroring conductor/db.py (Drive-sync mid-write corrupts) ----
DB_DIR = Path(r"C:\TradingDesk-Local\crm")
DEFAULT_DB_PATH = DB_DIR / "crm.db"

# Bumped only if the physical schema shape changes (carried in meta for future migrations).
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS templates (
    template_id  TEXT PRIMARY KEY,
    name         TEXT,
    active       INTEGER,
    weights_json TEXT
);

-- APPEND-ONLY assignment history: one row per assign(). ORDER BY seq preserves chronology;
-- current-per-account is DERIVED on load (last row per account), matching AssignmentBook.
CREATE TABLE IF NOT EXISTS assignments (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        TEXT,
    template_id       TEXT,
    effective_at      TEXT,
    set_by            TEXT,
    set_at            TEXT,
    prior_template_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_assignments_account ON assignments(account_id);

-- The mutating book: one row per (account, sleeve).
CREATE TABLE IF NOT EXISTS ledger_entries (
    account_id         TEXT,
    sleeve_id          TEXT,
    target_weight      REAL,
    attributed_cash    REAL,
    last_reconciled_at TEXT,
    ledger_version     INTEGER,
    PRIMARY KEY (account_id, sleeve_id)
);

-- attributed_positions, one row per instrument (key via Instrument.key()); a fully-closed
-- line simply has no row.
CREATE TABLE IF NOT EXISTS ledger_positions (
    account_id     TEXT,
    sleeve_id      TEXT,
    instrument_key TEXT,
    qty            REAL,
    PRIMARY KEY (account_id, sleeve_id, instrument_key)
);

-- Current latch per (account, day).
CREATE TABLE IF NOT EXISTS latches (
    account_id TEXT,
    day        TEXT,
    fault      TEXT,
    alert      TEXT,
    reason     TEXT,
    latched_at TEXT,
    cleared    INTEGER,
    cleared_by TEXT,
    cleared_at TEXT,
    PRIMARY KEY (account_id, day)
);

-- Audit trail of cleared-then-superseded latches (append-only; ORDER BY seq).
CREATE TABLE IF NOT EXISTS latches_superseded (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT,
    day        TEXT,
    fault      TEXT,
    alert      TEXT,
    reason     TEXT,
    latched_at TEXT,
    cleared    INTEGER,
    cleared_by TEXT,
    cleared_at TEXT
);

-- Surfaced soft-warnings per account (advisory; §5.2).
CREATE TABLE IF NOT EXISTS assign_warnings (
    account_id    TEXT PRIMARY KEY,
    warnings_json TEXT
);
"""


class CRMStore:
    """Normalized-SQLite transport for the CRM brain (§8). Decomposes CRMBrain.to_dict() into
    rows on write and reassembles the identical dict on read for CRMBrain.from_dict().

    Concurrency/durability come from SQLite itself (WAL mode, one transaction per write). The
    DB path is constructor-configurable so tests point at a tmp file and NEVER touch the real
    off-Drive default DB."""

    def __init__(self, db_path=DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # -- schema init (idempotent, mirrors conductor/db.py) ----------------------
    def _init_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        # Seed meta once; never clobber an existing brain_version/updated_at.
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),))
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('brain_version', '0')")
        self._conn.commit()

    # =========================================================================
    # Change-detection meta (§8) — the desk polls these
    # =========================================================================
    def version(self) -> int:
        """The monotonic brain_version — bumped in the same transaction as every write."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='brain_version'").fetchone()
        return int(row["value"]) if row is not None else 0

    def updated_at(self) -> Optional[str]:
        """ISO timestamp of the last write (the `now` passed in), or None before any write."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='updated_at'").fetchone()
        return row["value"] if row is not None else None

    @staticmethod
    def _norm_now(now) -> str:
        """Normalize an injected `now` to an ISO string. datetime -> isoformat(); str passes
        through; None -> datetime.now() (the ONLY wall-clock fallback, used solely when a
        caller does not pin the time — every test pins it)."""
        if now is None:
            return datetime.now().isoformat()
        if isinstance(now, datetime):
            return now.isoformat()
        return str(now)

    def _bump(self, *, now=None, version: Optional[int] = None) -> None:
        """Bump brain_version (monotonic, or an explicit `version`) and set updated_at. MUST be
        called inside an open transaction so it commits atomically with the write it stamps."""
        new_v = version if version is not None else self.version() + 1
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('brain_version', ?)",
            (str(int(new_v)),))
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('updated_at', ?)",
            (self._norm_now(now),))

    # =========================================================================
    # Full snapshot write (§8 payload) — one transaction
    # =========================================================================
    def save_brain(self, brain, *, now=None, version: Optional[int] = None) -> None:
        """Full transactional write of the whole brain. Decomposes brain.to_dict() into every
        table, then bumps brain_version + updated_at — all in ONE transaction.

        REMOVAL SEMANTICS: this is a full snapshot, so it CLEARS the row tables and re-inserts
        from the current payload. A template / assignment / ledger entry / position / latch /
        warning that is no longer in the brain therefore leaves NO row behind — a re-save
        faithfully reflects removals (the simplest correct guarantee; brains are small)."""
        d = brain.to_dict()
        with self._conn:                       # commit on success, rollback on exception
            self._clear_row_tables()
            self._insert_payload(d)
            self._bump(now=now, version=version)

    def _clear_row_tables(self) -> None:
        for t in ("templates", "assignments", "ledger_entries", "ledger_positions",
                  "latches", "latches_superseded", "assign_warnings"):
            self._conn.execute(f"DELETE FROM {t}")

    def _insert_payload(self, d: Mapping) -> None:
        # templates
        for td in d.get("templates", {}).values():
            self._insert_template(td)
        # assignments — history in chronological order (append-only)
        for ad in d.get("assignments", {}).get("history", []):
            self._insert_assignment(ad)
        # ledger — entry + its position rows
        for ed in d.get("ledger", {}).get("entries", []):
            self._insert_ledger_entry(ed)
        # latches — current + superseded audit trail
        for ld in d.get("latches", {}).get("current", []):
            self._insert_latch_current(ld)
        for ld in d.get("latches", {}).get("superseded", []):
            self._insert_latch_superseded(ld)
        # assign_warnings
        for account_id, warnings in d.get("assign_warnings", {}).items():
            self._conn.execute(
                "INSERT OR REPLACE INTO assign_warnings(account_id, warnings_json) "
                "VALUES(?, ?)",
                (account_id, json.dumps(list(warnings))))

    # --- low-level row inserters (shared by full-write and incremental writers) --
    def _insert_template(self, td: Mapping) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO templates(template_id, name, active, weights_json) "
            "VALUES(?, ?, ?, ?)",
            (td["template_id"], td["name"], int(bool(td.get("active", True))),
             json.dumps(dict(td["weights"]))))

    def _insert_assignment(self, ad: Mapping) -> None:
        self._conn.execute(
            "INSERT INTO assignments(account_id, template_id, effective_at, set_by, "
            "set_at, prior_template_id) VALUES(?, ?, ?, ?, ?, ?)",
            (ad["account_id"], ad["template_id"], ad["effective_at"], ad["set_by"],
             ad["set_at"], ad.get("prior_template_id")))

    def _insert_ledger_entry(self, ed: Mapping) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO ledger_entries(account_id, sleeve_id, target_weight, "
            "attributed_cash, last_reconciled_at, ledger_version) VALUES(?, ?, ?, ?, ?, ?)",
            (ed["account_id"], ed["sleeve_id"], ed.get("target_weight", 0.0),
             ed.get("attributed_cash", 0.0), ed.get("last_reconciled_at"),
             ed.get("ledger_version", 0)))
        self._conn.execute(
            "DELETE FROM ledger_positions WHERE account_id=? AND sleeve_id=?",
            (ed["account_id"], ed["sleeve_id"]))
        for inst_key, qty in ed.get("attributed_positions", {}).items():
            self._conn.execute(
                "INSERT OR REPLACE INTO ledger_positions(account_id, sleeve_id, "
                "instrument_key, qty) VALUES(?, ?, ?, ?)",
                (ed["account_id"], ed["sleeve_id"], inst_key, qty))

    def _insert_latch_current(self, ld: Mapping) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO latches(account_id, day, fault, alert, reason, "
            "latched_at, cleared, cleared_by, cleared_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ld["account_id"], ld["day"], ld["fault"], ld["alert"], ld["reason"],
             ld["latched_at"], int(bool(ld.get("cleared", False))),
             ld.get("cleared_by"), ld.get("cleared_at")))

    def _insert_latch_superseded(self, ld: Mapping) -> None:
        self._conn.execute(
            "INSERT INTO latches_superseded(account_id, day, fault, alert, reason, "
            "latched_at, cleared, cleared_by, cleared_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ld["account_id"], ld["day"], ld["fault"], ld["alert"], ld["reason"],
             ld["latched_at"], int(bool(ld.get("cleared", False))),
             ld.get("cleared_by"), ld.get("cleared_at")))

    # =========================================================================
    # Full read — reassemble brain.to_dict() shape, hand to CRMBrain.from_dict()
    # =========================================================================
    def load_brain(self, *, registry: Mapping = domain.SLEEVE_REGISTRY):
        """Read every table, rebuild the exact brain.to_dict() dict shape, and return
        CRMBrain.from_dict(...). A fresh (empty) DB yields an empty-but-valid brain. Rows are
        read in insertion order (ORDER BY rowid / seq) so the reassembled LISTS match the
        original ordering, making the round-trip a true equality."""
        d = {
            "templates": self._load_templates(),
            "assignments": self._load_assignments(),
            "ledger": self._load_ledger(),
            "latches": self._load_latches(),
            "assign_warnings": self._load_assign_warnings(),
        }
        return _brain.CRMBrain.from_dict(d, registry=registry)

    def _load_templates(self) -> dict:
        out: dict = {}
        for r in self._conn.execute(
                "SELECT template_id, name, active, weights_json FROM templates "
                "ORDER BY rowid"):
            out[r["template_id"]] = {
                "template_id": r["template_id"],
                "name": r["name"],
                "weights": json.loads(r["weights_json"]),
                "active": bool(r["active"]),
            }
        return out

    def _load_assignments(self) -> dict:
        history = [{
            "account_id": r["account_id"],
            "template_id": r["template_id"],
            "effective_at": r["effective_at"],
            "set_by": r["set_by"],
            "set_at": r["set_at"],
            "prior_template_id": r["prior_template_id"],
        } for r in self._conn.execute(
            "SELECT account_id, template_id, effective_at, set_by, set_at, "
            "prior_template_id FROM assignments ORDER BY seq")]
        # current-per-account is DERIVED by AssignmentBook.from_dict from history order
        # (last row per account wins) — matching how assign() built it — so we supply only
        # the append-only history and let the entity rebuild current.
        return {"history": history}

    def _load_ledger(self) -> dict:
        # Gather positions per (account, sleeve) first.
        pos: dict = {}
        for r in self._conn.execute(
                "SELECT account_id, sleeve_id, instrument_key, qty FROM ledger_positions "
                "ORDER BY rowid"):
            pos.setdefault((r["account_id"], r["sleeve_id"]), {})[
                r["instrument_key"]] = r["qty"]
        entries = []
        for r in self._conn.execute(
                "SELECT account_id, sleeve_id, target_weight, attributed_cash, "
                "last_reconciled_at, ledger_version FROM ledger_entries ORDER BY rowid"):
            entries.append({
                "account_id": r["account_id"],
                "sleeve_id": r["sleeve_id"],
                "target_weight": r["target_weight"],
                "attributed_positions": pos.get(
                    (r["account_id"], r["sleeve_id"]), {}),
                "attributed_cash": r["attributed_cash"],
                "last_reconciled_at": r["last_reconciled_at"],
                "ledger_version": r["ledger_version"],
            })
        return {"entries": entries}

    def _load_latches(self) -> dict:
        current = [self._latch_row_to_dict(r) for r in self._conn.execute(
            "SELECT account_id, day, fault, alert, reason, latched_at, cleared, "
            "cleared_by, cleared_at FROM latches ORDER BY rowid")]
        superseded = [self._latch_row_to_dict(r) for r in self._conn.execute(
            "SELECT account_id, day, fault, alert, reason, latched_at, cleared, "
            "cleared_by, cleared_at FROM latches_superseded ORDER BY seq")]
        return {"current": current, "superseded": superseded}

    @staticmethod
    def _latch_row_to_dict(r: sqlite3.Row) -> dict:
        return {
            "account_id": r["account_id"],
            "day": r["day"],
            "fault": r["fault"],
            "alert": r["alert"],
            "reason": r["reason"],
            "latched_at": r["latched_at"],
            "cleared": bool(r["cleared"]),
            "cleared_by": r["cleared_by"],
            "cleared_at": r["cleared_at"],
        }

    def _load_assign_warnings(self) -> dict:
        return {r["account_id"]: json.loads(r["warnings_json"])
                for r in self._conn.execute(
                    "SELECT account_id, warnings_json FROM assign_warnings ORDER BY rowid")}

    # =========================================================================
    # Incremental writers — the SQLite payoff: one row per event, not a rewrite
    # =========================================================================
    def upsert_ledger_entry(self, entry, *, now=None) -> None:
        """Persist ONE mutated sleeve-ledger entry (the on-every-fill hot path): replace its
        entry row and its position rows (a closed line drops out), then bump version — one
        transaction. `entry` is a ledger.SleeveLedgerEntry."""
        with self._conn:
            self._insert_ledger_entry(entry.to_dict())
            self._bump(now=now)

    def append_assignment(self, a, *, now=None) -> None:
        """Append ONE assignment audit row (append-only history), then bump version — one
        transaction. `a` is a domain.AccountAssignment."""
        with self._conn:
            self._insert_assignment(a.to_dict())
            self._bump(now=now)

    def upsert_latch(self, latch, *, now=None) -> None:
        """Upsert the CURRENT latch for one (account, day), then bump version — one
        transaction. `latch` is a latch.Latch. (Superseded audit rows are written only by the
        full snapshot; an incremental supersede is a domain concern the brain resolves before
        it hands its state here.)"""
        with self._conn:
            self._insert_latch_current(latch.to_dict())
            self._bump(now=now)

    def set_template(self, t, *, now=None) -> None:
        """Upsert ONE template, then bump version — one transaction. `t` is a
        domain.Template."""
        with self._conn:
            self._insert_template(t.to_dict())
            self._bump(now=now)

    # =========================================================================
    def close(self) -> None:
        self._conn.close()
