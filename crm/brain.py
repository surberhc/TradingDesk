"""brain.py — the CRM composition capstone (conductor #42/#43).

The thin, PURE service that stitches the four already-built CRM modules into the desk's
end-to-end flows (docs/CRM_DESIGN_groups_brain.md §5 → §6 → §7 → §12). It OWNS no logic of
its own worth the name — every decision lives in one of the four modules it composes:

  * crm/domain.py     — Template / SLEEVE_REGISTRY / AssignmentBook / derive_group_membership
  * crm/capability.py — the §5 capability gate (evaluate_template / assignable_templates)
  * crm/ledger.py     — the §7 sleeve ledger + reconcile checksum (attribute_block_fill /
                        reconcile_account / ReconResult)
  * crm/latch.py      — the §12 triage + fault-latch lifecycle (triage_reconcile /
                        fault_for_latch / LatchBook / below_floor)

What this layer ADDS is WIRING, and exactly one cross-module rule the spec demands but no
single module can enforce alone:

  * §5 THE HARD GATE IS ENFORCED AT ASSIGNMENT. capability.evaluate_template only *computes*
    a GateResult; it does not stop anyone assigning a blocked template. The brain makes the
    gate load-bearing: assign() REFUSES (ValueError) a template the account cannot legally /
    financially run, so it is "impossible to assign a sleeve it cannot run." Soft warnings do
    NOT block — they are surfaced (see assign / assignment_warnings), never enforced.
  * §7 → §12 THE RECONCILE → TRIAGE → LATCH TIE. reconcile() runs the ledger checksum, feeds
    its verdict to triage, and — this is the load-bearing tie — when triage says LATCH it
    actually latches the account out via the LatchBook. A DRIFT verdict flows all the way to
    an account being excluded at pre-flight.

HARD BOUNDARIES honored here (load-bearing — do not cross):
  * PURE / OFFLINE. stdlib only (dataclasses, datetime, typing) + crm.domain / crm.ledger /
    crm.latch / crm.capability (all pure). NO broker, NO ib_async, NO paperbot/config/order
    path, NO gateway. Broker snapshots (positions / cash / capabilities) and the broker
    activity ledger are passed IN as data — the brain fetches NOTHING.
  * TRANSPORT IS OPEN (§8 / §10.2). to_dict()/from_dict() on the aggregate are the FUTURE
    serialization boundary — but NO persistence: no JSON/DB/file I/O anywhere here. The brain
    HOLDS in-memory state; a future transport wraps persistence around it.
  * NO FROZEN NUMBER INVENTED (rule #1). The capability soft-cushion floor and the contract
    floor are passed THROUGH / passed IN, never decided here.
  * THIN COMPOSITION ONLY. Nothing the four modules already do is re-implemented — it is
    called. This layer wires them and enforces the one cross-module rule above.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping, Optional

import domain
import ledger
import latch
import capability

# Alias the ledger module's SleeveLedger — the constructor's `ledger` PARAMETER (named per the
# slice contract) shadows the `ledger` module inside __init__, so we reach the class this way.
from ledger import SleeveLedger as _SleeveLedger, POS_TOL as _POS_TOL, CASH_TOL as _CASH_TOL


# =============================================================================
# reconcile() report — the composed §7 → §12 outcome for one account/day
# =============================================================================
@dataclass(frozen=True)
class ReconcileReport:
    """The end-to-end result of reconcile(): the ledger checksum (§7.4), the triage verdict
    (§12.1), and whether that verdict actually latched the account out (§12.3). Frozen — an
    operational audit fact once produced.

      * `recon`   — the raw ledger.ReconResult (per-instrument + cash + verdict).
      * `outcome` — the latch.TriageOutcome (CLEAN / EXPLAINED / UNEXPLAINED_PENDING / LATCH).
      * `reason`  — the human triage reason string.
      * `latched` — True iff this reconcile drove a fault-latch (outcome == LATCH and the
                    LatchBook raised/held a latch for the account).
      * `alerted` — True iff THIS call raised a NEW alert (LatchBook.latch's alerted flag);
                    False when an already-active latch absorbed it (alert-once, §12.3)."""
    account_id: str
    day: date
    recon: "ledger.ReconResult"
    outcome: "latch.TriageOutcome"
    reason: str
    latched: bool
    alerted: bool


# =============================================================================
# CRMBrain — the composition capstone
# =============================================================================
class CRMBrain:
    """Composes the four CRM modules into the end-to-end flows and HOLDS the in-memory brain
    state (templates + the three books). PURE / offline — see the module header for the hard
    boundaries. All state is INJECTABLE (constructor) and time is INJECTABLE on every mutating
    call (`now` / `day` / `effective_at`) so the whole thing unit-tests with zero infra and no
    hidden clock."""

    def __init__(self, templates: Mapping[str, "domain.Template"], *,
                 registry: Mapping[str, "domain.Sleeve"] = domain.SLEEVE_REGISTRY,
                 assignments: Optional["domain.AssignmentBook"] = None,
                 ledger: Optional["ledger.SleeveLedger"] = None,
                 latches: Optional["latch.LatchBook"] = None) -> None:
        self.templates: dict = dict(templates)
        self.registry = registry
        self._assignments = (assignments if assignments is not None
                             else domain.AssignmentBook())
        self._ledger = ledger if ledger is not None else _SleeveLedger()
        self._latches = latches if latches is not None else latch.LatchBook()
        # Soft (advisory) warnings from the most recent successful assign(), per account. The
        # capability gate's soft warnings ALLOW but flag (§5.2) — the brain surfaces them here
        # rather than enforcing them, mirroring capability.GateResult.soft_warnings.
        self._assign_warnings: dict[str, list] = {}

    # --- book accessors (read-only handles for callers/tests) ------------------
    @property
    def assignments(self) -> "domain.AssignmentBook":
        return self._assignments

    @property
    def ledger(self) -> "ledger.SleeveLedger":
        return self._ledger

    @property
    def latches(self) -> "latch.LatchBook":
        return self._latches

    # =========================================================================
    # Assignment — GATED by the §5 capability gate
    # =========================================================================
    def assignable(self, caps: "capability.AccountCapabilities", *,
                   warn_cushion_floor: float = capability.DEFAULT_WARN_CUSHION_FLOOR
                   ) -> dict:
        """The §5.2 assignment-UI view over THIS brain's templates: template_id -> GateResult
        (enabled vs grayed-out + any advisory soft warnings). Pure delegation to
        capability.assignable_templates — the soft-cushion floor is passed THROUGH, never
        decided here (rule #1)."""
        return capability.assignable_templates(
            caps, self.templates, self.registry,
            warn_cushion_floor=warn_cushion_floor)

    def assign(self, account_id: str, template_id: str,
               caps: "capability.AccountCapabilities", set_by: str, *,
               effective_at: Optional[datetime] = None,
               now: Optional[datetime] = None,
               warn_cushion_floor: float = capability.DEFAULT_WARN_CUSHION_FLOOR
               ) -> "domain.AccountAssignment":
        """Bind `account_id` to `template_id`, ENFORCING the §5 HARD capability gate.

        The gate is what makes it "impossible to assign a sleeve the account cannot run":
          * Unknown `template_id` → ValueError (nothing to gate against).
          * capability.evaluate_template — if NOT allowed (a missing permission: options L3 /
            margin account / index-option perm), RAISE ValueError naming the blocking reasons.
            The account is never bound to a template it cannot legally/financially run.
          * If allowed, delegate to AssignmentBook.assign and return the audit row.

        SOFT warnings do NOT block (§5.2 — buying-power/margin fluctuates; Andrew overrides
        knowingly). On a successful assign they are SURFACED, not enforced: stored per account
        and reachable via `assignment_warnings(account_id)`. `warn_cushion_floor` is passed
        THROUGH to the gate, never decided here (rule #1)."""
        template = self.templates.get(template_id)
        if template is None:
            raise ValueError(
                f"cannot assign unknown template {template_id!r} to {account_id!r} "
                f"(known templates: {', '.join(sorted(self.templates))})")
        gate = capability.evaluate_template(
            caps, template, self.registry, warn_cushion_floor=warn_cushion_floor)
        if not gate.allowed:
            reasons = "; ".join(gate.hard_reasons) or "unmet capability requirement(s)"
            raise ValueError(
                f"account {account_id!r} cannot be assigned template {template_id!r}: "
                f"{reasons}. Fix the permission at IBKR first — the CRM cannot override a "
                f"missing permission (§5.2).")
        # Allowed. Surface (do NOT enforce) any advisory soft warnings for the caller.
        self._assign_warnings[account_id] = list(gate.soft_warnings)
        return self._assignments.assign(
            account_id, template_id, set_by, effective_at=effective_at, now=now)

    def assignment_warnings(self, account_id: str) -> list:
        """The advisory soft (§5.2) warnings from the account's most recent successful assign()
        — surfaced, never enforced. Empty list if none / never assigned. Companion accessor to
        assign() (a frozen AccountAssignment can't carry them)."""
        return list(self._assign_warnings.get(account_id, []))

    def current_assignment(self, account_id: str) -> Optional["domain.AccountAssignment"]:
        """The account's current assignment (or None) — delegates to AssignmentBook.current."""
        return self._assignments.current(account_id)

    def assignment_history(self, account_id: str) -> list:
        """The account's full append-only assignment audit trail — AssignmentBook.history."""
        return self._assignments.history(account_id)

    def group_membership(self) -> dict:
        """The derived FA-group membership (§2 / §3.4): fa_group_name -> set of account_ids,
        from CURRENT assignments. Pure delegation to domain.derive_group_membership."""
        return domain.derive_group_membership(
            self._assignments, self.templates, self.registry)

    # =========================================================================
    # Ledger ops — §7 attribution (delegate to crm.ledger)
    # =========================================================================
    def attribute_block(self, *, fa_group: str,
                        per_account_split: Mapping[str, float],
                        instrument: "ledger.Instrument", price: float,
                        commission_total: float = 0.0, side: str = "BUY",
                        now: Optional[datetime] = None) -> dict:
        """Attribute an FA-group block fill across accounts (§7.2 / §13.3). Delegates to
        ledger.attribute_block_fill over this brain's ledger; the sleeve is recovered from the
        group via the registry. Returns the per-account applied report."""
        return ledger.attribute_block_fill(
            self._ledger, fa_group=fa_group, per_account_split=per_account_split,
            instrument=instrument, price=price, commission_total=commission_total,
            side=side, group_sleeve_map=ledger.build_group_sleeve_map(self.registry),
            now=now)

    def attribute_leg(self, account: str, sleeve: str,
                     instrument: "ledger.Instrument", qty_delta: float, price: float, *,
                     commission: float = 0.0, now: Optional[datetime] = None) -> float:
        """Attribute one filled leg to one sleeve (§7.2 primitive) — SleeveLedger.attribute_fill.
        Returns the cash_delta applied (BUY drains, SELL adds; commission always a cost)."""
        return self._ledger.attribute_fill(
            account, sleeve, instrument, qty_delta, price,
            commission=commission, now=now)

    def blended_positions(self, account: str) -> dict:
        """The ledger's blended (Σ across sleeves) position view for an account (§7.4)."""
        return self._ledger.blended_positions(account)

    def blended_cash(self, account: str) -> float:
        """The ledger's blended (Σ across sleeves) cash for an account (§7.4)."""
        return self._ledger.blended_cash(account)

    # =========================================================================
    # The end-to-end reconcile → triage → latch flow (§7.4 → §12.1 → §12.3)
    # =========================================================================
    def reconcile(self, account_id: str,
                  broker_positions: Mapping["ledger.Instrument", float],
                  broker_cash: float, transactions: list, *,
                  day: date, is_eod: bool, now: Optional[datetime] = None,
                  pos_tol: float = _POS_TOL,
                  cash_tol: float = _CASH_TOL) -> ReconcileReport:
        """The load-bearing composition: run the §7.4 ledger checksum, triage it (§12.1), and —
        when triage says LATCH — actually pull the account out via the LatchBook (§12.3).

        Flow:
          1. ledger.reconcile_account  → ReconResult (verdict OK / REVIEW / DRIFT).
          2. latch.triage_reconcile    → (TriageOutcome, reason), using the broker's own labeled
             `transactions` and the intraday-vs-EOD `is_eod` timing.
          3. If outcome == LATCH: self._latches.latch(account_id, day,
             latch.fault_for_latch(recon), reason, now=now) — the FaultType is chosen by the
             latch module, and `alerted` is captured from the LatchBook's return (alert-once).
             Otherwise no latch is raised (latched=False, alerted=False).

        A DRIFT verdict therefore flows all the way to the account being latched out and
        excluded at pre-flight. The broker snapshot + activity ledger are passed IN (pure).
        `pos_tol`/`cash_tol` are mechanical float-slop params passed through (not strategy)."""
        recon = ledger.reconcile_account(
            self._ledger, account_id, broker_positions, broker_cash,
            pos_tol=pos_tol, cash_tol=cash_tol)
        outcome, reason = latch.triage_reconcile(
            recon, transactions, is_eod=is_eod)

        latched = False
        alerted = False
        if outcome is latch.TriageOutcome.LATCH:
            _latch, alerted = self._latches.latch(
                account_id, day, latch.fault_for_latch(recon), reason, now=now)
            latched = True
        return ReconcileReport(
            account_id=account_id, day=day, recon=recon, outcome=outcome,
            reason=reason, latched=latched, alerted=alerted)

    # =========================================================================
    # Pre-flight gates (§12.2 / §12.3 / §12.4)
    # =========================================================================
    def preflight_excluded(self, account_id: str, day: date) -> bool:
        """§12.2/§12.3 pre-flight gate: is the account already latched out for `day` (so it is
        excluded WITHOUT re-alerting)? Read-only delegation to LatchBook.excluded_at_preflight —
        never raises a fault, never alerts."""
        return self._latches.excluded_at_preflight(account_id, day)

    def preflight_floor_check(self, account_id: str, investable: float,
                              floor_threshold: float, *, day: date,
                              now: Optional[datetime] = None) -> bool:
        """§12.4 whole-contract floor pre-flight: if `investable` is BELOW the given
        `floor_threshold` (latch.below_floor), raise a BELOW_FLOOR fault-latch and return True
        (the account is excluded — TEMPLATE_NO_LONGER_QUALIFIES: its situation changed, reassign
        not retry). Otherwise return False.

        The floor NUMBER is FROZEN/BLESSED and passed IN (rule #1) — computed elsewhere from
        peak-concurrent contracts × margin (§12.4/§12.5), NEVER here. This only APPLIES it."""
        if latch.below_floor(investable, floor_threshold):
            self._latches.latch(
                account_id, day, latch.FaultType.BELOW_FLOOR,
                f"investable ${investable:,.0f} below whole-contract floor "
                f"${floor_threshold:,.0f} — account sits out (§12.4)",
                now=now)
            return True
        return False

    # =========================================================================
    # Serialization boundary (§8 / §10.2) — NO persistence, no file/DB I/O
    # =========================================================================
    def to_dict(self) -> dict:
        """The FUTURE-transport serialization boundary (§8): the whole brain state as plain
        dicts — templates + the three books' own to_dict(). NO persistence here; a future
        transport wraps file/DB I/O around this. The structural registry is NOT serialized (it
        is a constant reconstructed on load), matching the modules' own to_dict scope."""
        return {
            "templates": {tid: t.to_dict() for tid, t in self.templates.items()},
            "assignments": self._assignments.to_dict(),
            "ledger": self._ledger.to_dict(),
            "latches": self._latches.to_dict(),
            "assign_warnings": {a: list(w) for a, w in self._assign_warnings.items()},
        }

    @classmethod
    def from_dict(cls, d: Mapping, *,
                  registry: Mapping[str, "domain.Sleeve"] = domain.SLEEVE_REGISTRY
                  ) -> "CRMBrain":
        """Reconstruct a brain from to_dict() output. NO file/DB I/O — pure in-memory rebuild.
        The registry defaults to the structural SLEEVE_REGISTRY (not serialized)."""
        templates = {
            tid: domain.Template.from_dict(td)
            for tid, td in d.get("templates", {}).items()
        }
        assignments = domain.AssignmentBook.from_dict(d.get("assignments", {}))
        led = ledger.SleeveLedger.from_dict(d.get("ledger", {}))
        latches = latch.LatchBook.from_dict(d.get("latches", {}))
        brain = cls(templates, registry=registry, assignments=assignments,
                    ledger=led, latches=latches)
        brain._assign_warnings = {
            a: list(w) for a, w in d.get("assign_warnings", {}).items()
        }
        return brain
