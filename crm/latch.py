"""latch.py — the CRM fault-latch + reconciliation-triage lifecycle (conductor #42/#43).

The OPERATIONAL layer on top of the §7.4 sleeve-ledger checksum (crm/sleeve_ledger.py). Where
sleeve_ledger.reconcile_account produces a per-account verdict ("OK"/"REVIEW"/"DRIFT") and its
per-instrument classifications (MATCH / LEDGER_DRIFT / ALIEN / CASH_DRIFT), THIS module
decides what that means operationally and holds the resulting state:

  * §12.1  Reconciliation is TRIAGED, not binary-freeze. A cash drift is first checked
           against the broker's OWN labeled activity ledger (dividend / interest / fee /
           corp-action / …). A drift a labeled transaction explains is booked with NO alert.
           The activity ledger is EOD, so an unexplained INTRADAY drift is HELD
           (UNEXPLAINED_PENDING), not latched — only the residual that survives the nightly
           sweep (unexplained at EOD) escalates. A hard POSITION drift is not a cash event
           and latches regardless of timing.
  * §12.3  Faults LATCH. Ledger drift, unexplained transaction, and below-floor size are the
           SAME rule in three hats: a fault pulls the account out, alerts ONCE, and the
           account sits idle the REST OF THE DAY until a HUMAN clears it. It never silently
           re-fires; an already-latched account is excluded at pre-flight WITHOUT re-alerting.
           Two distinct alert types (operational skip vs. situation-changed reassignment).
  * §12.4/§12.5  The whole-contract floor NUMBER is CONFIG — frozen/blessed and computed
           elsewhere (peak-concurrent-contracts × margin, cadence-driven). This module only
           APPLIES a given floor ("below the blessed floor → fault"); it NEVER computes or
           blesses the number (rule #1).

HARD BOUNDARIES honored here (load-bearing — do not cross):
  * PURE / OFFLINE. stdlib only (dataclasses, enum, datetime, math, typing) + crm.sleeve_ledger /
    crm.domain (both pure). NO broker, NO ib_async, NO paperbot/config/order path, NO
    gateway. The broker activity ledger and the recon snapshot are passed IN as data — this
    module never fetches them.
  * TRANSPORT IS OPEN (§8 / §10.2). to_dict()/from_dict() on the stateful entities are the
    FUTURE serialization boundary — but NO persistence: no JSON/DB/file I/O anywhere here.
  * NO FROZEN NUMBER INVENTED (rule #1). The drift-match tolerance is a MECHANICAL float-slop
    param with a sensible default (see below), NOT a strategy number. The contract FLOOR is
    passed IN, never derived here (§12.4/§12.5).
  * STATE + PURE DECISIONS ONLY. This slice consumes sleeve_ledger.ReconResult; it does NOT
    re-implement reconciliation. triage_* DECIDES (pure); LatchBook HOLDS state.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Mapping, Optional

import sleeve_ledger  # crm.sleeve_ledger — pure; we consume its ReconResult / verdict, never re-run recon


# =============================================================================
# Mechanical tolerance — float/rounding slop only (NOT a frozen strategy number)
# =============================================================================
# A labeled transaction "explains" a cash drift when their amounts agree within this. $1.00
# absorbs sub-dollar rounding / commission slop, mirroring sleeve_ledger.CASH_TOL. It decides
# "same dollar figure within noise," never anything about allocation — rule #1 does not touch it.
DRIFT_TOL = 1.0


# =============================================================================
# 1) Fault + alert taxonomy  (§12.3 — "the same rule in three hats")
# =============================================================================
class FaultType(enum.Enum):
    """The three faults that latch an account out — one rule wearing three hats (§12.3)."""
    LEDGER_DRIFT = "ledger_drift"                  # position split doesn't reconcile to broker
    UNEXPLAINED_TRANSACTION = "unexplained_transaction"  # cash residual survived the EOD sweep
    BELOW_FLOOR = "below_floor"                    # account fell under the blessed contract floor


class AlertType(enum.Enum):
    """The two distinct operator alerts — different problems, different resolutions (§12.3)."""
    # Operational: the account is out for the day; resolve when convenient (clear-and-retry).
    TRADE_SKIPPED_LATCHED = "trade_skipped_latched"
    # The account's SITUATION changed so its template no longer fits — the fix is
    # REASSIGNMENT to a fitting strategy, NOT clearing a flag so it retries and fails again.
    TEMPLATE_NO_LONGER_QUALIFIES = "template_no_longer_qualifies"


def alert_for_fault(fault: FaultType) -> AlertType:
    """Map a fault to its operator alert (§12.3). Ledger drift and an unexplained transaction
    are operational hiccups → TRADE_SKIPPED_LATCHED (out for the day, resolve when convenient).
    Below-floor means the account's situation changed → TEMPLATE_NO_LONGER_QUALIFIES (reassign,
    not retry)."""
    if fault is FaultType.BELOW_FLOOR:
        return AlertType.TEMPLATE_NO_LONGER_QUALIFIES
    return AlertType.TRADE_SKIPPED_LATCHED


# =============================================================================
# 2) The latch state  (§12.3 — alert once, idle the rest of the day, human clears)
# =============================================================================
@dataclass(frozen=True)
class Latch:
    """One account's fault-latch for one trading day (§12.3). Frozen — a latch, once raised,
    is an operational audit fact; "clearing" produces a NEW cleared copy rather than editing
    it in place. `alert` is DERIVED from `fault` (alert_for_fault) at raise time so the two
    can never disagree."""
    account_id: str
    day: date
    fault: FaultType
    alert: AlertType
    reason: str
    latched_at: datetime
    cleared: bool = False
    cleared_by: Optional[str] = None
    cleared_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "day": self.day.isoformat(),
            "fault": self.fault.name,
            "alert": self.alert.name,
            "reason": self.reason,
            "latched_at": self.latched_at.isoformat(),
            "cleared": self.cleared,
            "cleared_by": self.cleared_by,
            "cleared_at": None if self.cleared_at is None else self.cleared_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "Latch":
        return cls(
            account_id=d["account_id"],
            day=date.fromisoformat(d["day"]),
            fault=FaultType[d["fault"]],
            alert=AlertType[d["alert"]],
            reason=d["reason"],
            latched_at=datetime.fromisoformat(d["latched_at"]),
            cleared=d.get("cleared", False),
            cleared_by=d.get("cleared_by"),
            cleared_at=(None if d.get("cleared_at") is None
                        else datetime.fromisoformat(d["cleared_at"])),
        )


class LatchBook:
    """In-memory fault-latch book, keyed by `(account_id, day)` (§12.3).

    Alert-once semantics (§12.3 — "alerts ONCE ... never silently re-fires"):
      * The FIRST fault of the day for an account raises a latch and returns alerted=True.
      * While an ACTIVE (uncleared) latch exists for that (account, day), any further latch
        attempt returns the EXISTING latch with alerted=False — the fault does not re-fire and
        no duplicate is created.

    DESIGN CHOICE — a DIFFERENT fault type arriving while already latched (documented per the
    slice contract): we KEEP the first active latch and still return alerted=False. The account
    is already out for the day; stacking a second alert for a second fault would defeat the
    "alert once" rule and spam the operator. The first fault is what pulled the account; the
    later one is moot until a human looks. (A future refinement could ATTACH the extra fault to
    the existing latch for the operator's context, but it must NOT raise a second alert.)

    A latch only stops firing when a HUMAN clears it (`clear`); a cleared latch re-enables the
    account, and a NEW fault AFTER a clear (same day) alerts again — it is a fresh latch.

    PURE / in-memory only — no persistence (§8). `now` is INJECTABLE and `day` is passed in on
    every call; this book NEVER calls date.today()/datetime.now() itself, so tests fully
    control time."""

    def __init__(self) -> None:
        # The current latch for each (account, day) — active OR most-recently cleared.
        self._current: dict[tuple[str, date], Latch] = {}
        # Latches that were superseded by a later fault after being cleared (audit trail).
        self._superseded: list[Latch] = []

    # --- raise -----------------------------------------------------------------
    def latch(self, account_id: str, day: date, fault: FaultType, reason: str, *,
              now: Optional[datetime] = None) -> tuple[Latch, bool]:
        """Raise a fault-latch for `(account_id, day)`. Returns `(latch, alerted)`.

        If an ACTIVE (uncleared) latch already exists for the day, this is a no-op: returns
        `(existing_latch, False)` — the fault does NOT re-fire and no duplicate is made (this
        holds even when `fault` differs from the active latch's — see the class docstring).
        Otherwise (no latch, or the day's latch was human-cleared) a NEW active latch is raised
        and `(new_latch, True)` is returned; a cleared latch it replaces is moved to the audit
        trail. `latched_at` = `now`. The alert is derived from the fault (alert_for_fault)."""
        key = (account_id, day)
        existing = self._current.get(key)
        if existing is not None and not existing.cleared:
            return existing, False
        if existing is not None:  # existing.cleared → keep it in the audit trail, then re-latch
            self._superseded.append(existing)
        new = Latch(
            account_id=account_id,
            day=day,
            fault=fault,
            alert=alert_for_fault(fault),
            reason=reason,
            latched_at=now,
        )
        self._current[key] = new
        return new, True

    # --- query -----------------------------------------------------------------
    def is_latched(self, account_id: str, day: date) -> bool:
        """True iff an ACTIVE (uncleared) latch exists for `(account_id, day)`. A human-cleared
        latch is NOT active. Read-only — never alerts."""
        e = self._current.get((account_id, day))
        return e is not None and not e.cleared

    def excluded_at_preflight(self, account_id: str, day: date) -> bool:
        """§12.2/§12.3 pre-flight gate: is this account excluded because it is already latched
        out? Alias of `is_latched`, named for the call site. Calling this NEVER alerts — that is
        the whole point of "excluded at pre-flight WITHOUT re-alerting": pre-flight only READS
        the latch state, it never re-raises the fault."""
        return self.is_latched(account_id, day)

    # --- clear (human) ---------------------------------------------------------
    def clear(self, account_id: str, day: date, by: str, *,
              now: Optional[datetime] = None) -> bool:
        """A HUMAN clears the day's active latch for an account (§12.3 — only a human clear
        lets the account trade again). Marks cleared / cleared_by / cleared_at (a new frozen
        copy). Returns True if an active latch was cleared, False if there was nothing active to
        clear (no latch, or already cleared)."""
        key = (account_id, day)
        e = self._current.get(key)
        if e is None or e.cleared:
            return False
        self._current[key] = replace(e, cleared=True, cleared_by=by, cleared_at=now)
        return True

    # --- listing ---------------------------------------------------------------
    def active_latches(self, day: date) -> list:
        """All ACTIVE (uncleared) latches for `day`, sorted by account_id."""
        out = [e for (acct, d), e in self._current.items()
               if d == day and not e.cleared]
        out.sort(key=lambda e: e.account_id)
        return out

    def all_latches(self) -> list:
        """Every latch this book holds — current (active or cleared) plus superseded audit
        rows — sorted by (day, account_id, latched_at)."""
        out = list(self._superseded) + list(self._current.values())
        out.sort(key=lambda e: (e.day, e.account_id, e.latched_at))
        return out

    # --- serialization ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "current": [e.to_dict() for e in self._current.values()],
            "superseded": [e.to_dict() for e in self._superseded],
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "LatchBook":
        book = cls()
        for ed in d.get("current", []):
            e = Latch.from_dict(ed)
            book._current[(e.account_id, e.day)] = e
        book._superseded = [Latch.from_dict(ed) for ed in d.get("superseded", [])]
        return book


# =============================================================================
# 3) Triage  (§12.1 — explain drift from the broker's own LABELED activity ledger)
# =============================================================================
@dataclass(frozen=True)
class LabeledTransaction:
    """One row of the broker's OWN activity ledger (§12.1) — already LABELED by the broker
    (dividend / interest / payment-in-lieu / corp action / fee / trade / …). We explain a cash
    drift by looking one of these up, NOT by rebuilding it from holdings × rate × a calendar.

    `amount` sign convention (reconciliation frame): a POSITIVE amount is cash the broker added
    to the account that the ledger has not yet booked (dividend, interest — a broker surplus);
    a NEGATIVE amount is cash the broker removed that the ledger has not booked (a fee). This is
    the frame `explain_drift`/`triage_reconcile` match against (broker_cash − ledger_cash); a
    future Flex loader normalizes IBKR's raw activity amounts into it."""
    account_id: str
    symbol: Optional[str]
    amount: float
    kind: str
    date: date

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "symbol": self.symbol,
            "amount": self.amount,
            "kind": self.kind,
            "date": self.date.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "LabeledTransaction":
        return cls(
            account_id=d["account_id"],
            symbol=d.get("symbol"),
            amount=d["amount"],
            kind=d["kind"],
            date=date.fromisoformat(d["date"]),
        )


def explain_drift(cash_drift: float, transactions: list, *,
                  tol: float = DRIFT_TOL) -> Optional[LabeledTransaction]:
    """PURE matcher (§12.1): return the labeled transaction whose `amount` matches `cash_drift`
    within `tol`, preferring the CLOSEST match; None if nothing explains it (unexplained).

    Kept deliberately simple: a single-transaction match on `|amount - cash_drift| <= tol`.
    `cash_drift` is the reconciliation-frame gap the caller wants explained (broker_cash −
    ledger_cash; see LabeledTransaction), so a $50 dividend the broker already credited
    (drift +50) is explained by a labeled DIVIDEND of +50.

    FUTURE refinement (noted, not built): multi-transaction / summed matching (several small
    events explaining one drift) and a symbol filter. This slice does the single-row lookup."""
    best: Optional[LabeledTransaction] = None
    best_gap = tol
    for txn in transactions:
        gap = abs(txn.amount - cash_drift)
        if gap <= best_gap:
            best = txn
            best_gap = gap
    return best


class TriageOutcome(enum.Enum):
    """The §12.1 triage verdict for one account's reconcile."""
    CLEAN = "clean"                          # nothing to do (recon OK)
    EXPLAINED = "explained"                  # a labeled transaction accounts for the drift — book it
    UNEXPLAINED_PENDING = "unexplained_pending"  # held, NOT latched (intraday; EOD sweep may clear it)
    LATCH = "latch"                          # caller must latch the account out


def triage_reconcile(recon: "sleeve_ledger.ReconResult", transactions: list, *,
                     is_eod: bool, tol: float = DRIFT_TOL) -> tuple:
    """Triage one account's §7.4 reconcile into a §12.1 operational decision. Returns
    `(TriageOutcome, reason)`. This function DECIDES (pure); the CALLER applies
    `LatchBook.latch(...)` when the outcome is LATCH — triage never mutates latch state.
    Use `fault_for_latch(recon)` to get the FaultType the caller should latch with.

    Decision (precedence top-to-bottom):
      1. verdict == "OK"                         → (CLEAN, …).
      2. Any HARD LEDGER_DRIFT instrument (a position mismatch) → (LATCH, …) REGARDLESS of
         timing. A position mismatch is not a labeled cash event; the activity ledger can't
         explain it, so there is nothing to wait for. Caller latches FaultType.LEDGER_DRIFT.
         (This DOMINATES — a hard position drift wins even if cash also drifted or an ALIEN
         holding is present.)
      3. A CASH_DRIFT (and no hard position drift): try explain_drift on the cash gap.
           * explained            → (EXPLAINED, "<kind> $<amount>") — book it, NO alert.
           * unexplained & !is_eod → (UNEXPLAINED_PENDING, …) — HELD, not latched (the activity
                                      ledger is EOD; don't panic on a drift we haven't had a
                                      chance to explain yet).
           * unexplained & is_eod → (LATCH, …) — the residual survived the nightly sweep. Caller
                                      latches FaultType.UNEXPLAINED_TRANSACTION.
      4. verdict == "REVIEW" (ALIEN holding(s), no hard drift): an ALIEN is a broker holding the
         ledger can't attribute — unattributable, so it gets a HUMAN look, but per §7.4 "REVIEW"
         is NOT itself a hard latch. DESIGN CHOICE (documented): treat it on the SAME EOD timing
         as an unexplained cash drift — (UNEXPLAINED_PENDING, …) intraday (a corp action may post
         a matching transaction by EOD), escalating to (LATCH, …) only if the ALIEN PERSISTS at
         EOD. The EOD-persistent ALIEN latch uses FaultType.LEDGER_DRIFT (a books-vs-reality
         position discrepancy → the operational TRADE_SKIPPED_LATCHED alert)."""
    if recon.verdict == "OK":
        return TriageOutcome.CLEAN, "reconciles clean (all MATCH)"

    # (2) Hard position drift dominates all timing — not a labeled cash event.
    if recon.drift_instruments:
        syms = ", ".join(s.instrument.symbol for s in recon.drift_instruments)
        return (TriageOutcome.LATCH,
                f"ledger drift on {syms}: position split does not reconcile to broker — "
                f"latch (LEDGER_DRIFT), timing-independent")

    # (3) Cash drift with no hard position drift — try the activity ledger.
    if recon.cash_status == "CASH_DRIFT":
        cash_drift = recon.broker_cash - recon.ledger_cash
        match = explain_drift(cash_drift, transactions, tol=tol)
        if match is not None:
            return (TriageOutcome.EXPLAINED,
                    f"{match.kind} ${match.amount:.2f} explains cash drift "
                    f"${cash_drift:.2f} — book it, no alert")
        if not is_eod:
            return (TriageOutcome.UNEXPLAINED_PENDING,
                    f"cash drift ${cash_drift:.2f} unexplained intraday — held pending the "
                    f"EOD activity-ledger sweep, not latched")
        return (TriageOutcome.LATCH,
                f"cash drift ${cash_drift:.2f} unexplained at EOD — residual survived the "
                f"nightly sweep, latch (UNEXPLAINED_TRANSACTION)")

    # (4) REVIEW: ALIEN holding(s), no hard drift.
    if recon.verdict == "REVIEW":
        syms = ", ".join(s.instrument.symbol for s in recon.alien_instruments)
        if not is_eod:
            return (TriageOutcome.UNEXPLAINED_PENDING,
                    f"alien holding(s) {syms} unattributable intraday — held for review, "
                    f"a corp action may post a matching transaction by EOD, not latched")
        return (TriageOutcome.LATCH,
                f"alien holding(s) {syms} persisted at EOD — unattributable, latch for "
                f"review (LEDGER_DRIFT)")

    # Defensive fallthrough (a DRIFT verdict always has drift_instruments or CASH_DRIFT above;
    # this only fires on an unexpected recon shape). Hold rather than silently pass.
    return (TriageOutcome.UNEXPLAINED_PENDING,
            f"reconcile verdict {recon.verdict!r} with no classified drift — held for review")


def fault_for_latch(recon: "sleeve_ledger.ReconResult") -> FaultType:
    """The FaultType a caller should latch with for a LATCH-outcome triage of `recon` (§12.3).
    Mirrors triage_reconcile's precedence: a hard position LEDGER_DRIFT (or an unattributable
    ALIEN, treated as a books-vs-reality position discrepancy) → FaultType.LEDGER_DRIFT; an
    unexplained cash residual → FaultType.UNEXPLAINED_TRANSACTION. (BELOW_FLOOR is NOT a
    reconcile fault — it comes from the pre-flight floor check, below_floor, not from recon.)"""
    if recon.drift_instruments:
        return FaultType.LEDGER_DRIFT
    if recon.cash_status == "CASH_DRIFT":
        return FaultType.UNEXPLAINED_TRANSACTION
    # ALIEN-only REVIEW that reached a latch: a holding the books can't attribute.
    return FaultType.LEDGER_DRIFT


# =============================================================================
# 4) Floor check  (§12.4/§12.5 — APPLY the given floor; do NOT compute it)
# =============================================================================
def below_floor(account_investable: float, floor_threshold: float) -> bool:
    """True iff `account_investable < floor_threshold` — the account has fallen under the
    whole-contract sizing floor and must sit out (§12.4).

    The floor NUMBER is FROZEN/BLESSED and passed IN. Its formula (1 contract × peak-concurrent
    firings × margin per spread) and its cadence-driven recompute are §12.4/§12.5 config — they
    are decided and blessed ELSEWHERE, never here (rule #1). This function only applies the
    "below the given floor → fault" rule. Below-floor maps to FaultType.BELOW_FLOOR →
    AlertType.TEMPLATE_NO_LONGER_QUALIFIES: the account's SITUATION changed (e.g. a large
    withdrawal), so the fix is REASSIGNMENT to a fitting strategy, not clear-and-retry. Boundary:
    exactly AT the floor is NOT below (>= floor is fine)."""
    return account_investable < floor_threshold
