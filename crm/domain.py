"""domain.py — the CRM 'brain' pure domain model (conductor #42/#43).

Implements the layered model of docs/CRM_DESIGN_groups_brain.md Option A, TOP-of-stack
only (the in-memory brain state and its derivations):

    STRATEGY  ->  SLEEVE (strategy @ tier)  ->  TEMPLATE (named sleeve-weight bundle)
              ->  ACCOUNT ASSIGNMENT (audited binding)  ->  GROUP MEMBERSHIP (derived)

HARD BOUNDARIES honored here (load-bearing — do not cross):
  * PURE / OFFLINE. stdlib only (dataclasses, enum, datetime, math, typing). NO broker,
    NO ib_async, NO order path, NO replaceFA, NO gateway. This module imports NOTHING from
    config / order_router / ib_async — crm/ is deliberately dependency-free so it can never
    drag the desk's broker plumbing into the brain, and so it unit-tests with zero infra.
  * TRANSPORT IS OPEN (spec §8 / §10.2 — "do not build until chosen"). We provide clean
    to_dict()/from_dict() serialization as the FUTURE transport boundary, but write NO
    persistence: no JSON reader/writer, no DB, no file I/O anywhere in this file.
  * WEIGHTS ARE FROZEN (CLAUDE.md rule #1). The CRM STORES blessed numbers; it never
    invents or tunes them. The one concrete-weight object here (EXAMPLE_TEMPLATES) is
    ILLUSTRATIVE only and clearly marked "NOT blessed — example," mirroring
    model_portfolio.EXAMPLE_POLICY.

The numeric validation rule is intentionally the SAME one as
paperbot/model_portfolio.validate_policy (POLICY_WEIGHT_TOL = 1e-6, finite / [0,1] /
sum-to-1.0), re-expressed here without importing model_portfolio — because model_portfolio
imports config + ib_async + order_router, which this dependency-free package must not touch.
The modelCode coupling of model_portfolio is DEAD (spec §0) and is deliberately NOT reused.
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional


# =============================================================================
# Constants
# =============================================================================
# Same rule as paperbot/model_portfolio.py POLICY_WEIGHT_TOL — template sleeve weights must
# sum to 1.0 within this absolute tolerance (float slop), each weight finite in [0, 1].
WEIGHT_TOL = 1e-6

# The desk's risk tiers. STRUCTURAL identifiers, not frozen strategy numbers.
VALID_TIERS = frozenset({"Conservative", "Balanced", "Growth", "Overlay"})

# The strategy family keys. Match config.STRATEGY / model_portfolio's strategy strings;
# duplicated deliberately (NOT imported) to keep crm/ dependency-free.
VALID_STRATEGY_KEYS = frozenset({"adaptive_all_weather", "s8_british_ic"})


# =============================================================================
# Requirement — the capability gate's atoms (spec §5.1)
# =============================================================================
class Requirement(enum.Enum):
    """A capability an account must hold to run a sleeve. DERIVED from a sleeve's strategy
    (§5.1), never hand-tagged. These are the atoms the CRM's assignment UI would gate on."""
    OPTIONS_L3 = "options_l3"
    MARGIN_ACCOUNT = "margin_account"
    INDEX_OPTION_PERM = "index_option_perm"


# =============================================================================
# Sleeve — one (strategy, tier) pair  (spec §2, §3.2)
# =============================================================================
@dataclass(frozen=True)
class Sleeve:
    """One strategy at one risk tier == one FA group's worth of behavior (spec §2).
    Frozen so a validated registry entry can't be mutated out from under a derivation.

    `fa_group_name` is the FA group that IMPLEMENTS this sleeve on the gateway (e.g.
    "tier_balanced", "s8_overlay"). These identities/group-names are STRUCTURAL (mechanical
    wiring), NOT frozen strategy numbers — naming them here does not set any weight."""
    sleeve_id: str
    strategy_key: str
    tier: str
    fa_group_name: str
    description: str = ""

    def validate(self) -> "Sleeve":
        """Validate and return self (fluent). strategy_key/tier must be in the valid sets."""
        if self.strategy_key not in VALID_STRATEGY_KEYS:
            raise ValueError(
                f"sleeve {self.sleeve_id!r}: strategy_key {self.strategy_key!r} not in "
                f"{sorted(VALID_STRATEGY_KEYS)}")
        if self.tier not in VALID_TIERS:
            raise ValueError(
                f"sleeve {self.sleeve_id!r}: tier {self.tier!r} not in {sorted(VALID_TIERS)}")
        return self

    def to_dict(self) -> dict:
        return {
            "sleeve_id": self.sleeve_id,
            "strategy_key": self.strategy_key,
            "tier": self.tier,
            "fa_group_name": self.fa_group_name,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "Sleeve":
        return cls(
            sleeve_id=d["sleeve_id"],
            strategy_key=d["strategy_key"],
            tier=d["tier"],
            fa_group_name=d["fa_group_name"],
            description=d.get("description", ""),
        )


# --- Requirements are DERIVED from the strategy, never hand-tagged (spec §5.1) -----------
# The mapping from strategy family -> its capability requirements. This is the ONLY place
# requirements are decided; a sleeve never carries a hand-typed requirement list.
_STRATEGY_REQUIREMENTS: dict[str, frozenset[Requirement]] = {
    # S8 SPX 0DTE credit spreads: options Level 3 + a margin account + index-option perm.
    "s8_british_ic": frozenset({
        Requirement.OPTIONS_L3,
        Requirement.MARGIN_ACCOUNT,
        Requirement.INDEX_OPTION_PERM,
    }),
    # S0 adaptive all-weather ETFs: nothing special (cash-or-margin, no options).
    "adaptive_all_weather": frozenset(),
}


def sleeve_requirements(sleeve: Sleeve) -> frozenset[Requirement]:
    """A sleeve's capability requirements, DERIVED from its strategy_key (§5.1). Never
    hand-tagged: it is a pure function of the strategy, so adding an S8 sleeve anywhere
    automatically carries the options-L3 requirement with it."""
    if sleeve.strategy_key not in _STRATEGY_REQUIREMENTS:
        raise ValueError(
            f"sleeve {sleeve.sleeve_id!r}: no requirement mapping for strategy_key "
            f"{sleeve.strategy_key!r} (known: {sorted(_STRATEGY_REQUIREMENTS)})")
    return _STRATEGY_REQUIREMENTS[sleeve.strategy_key]


# =============================================================================
# The desk's sleeve registry  (spec §3.2)
# =============================================================================
# sleeve_id -> Sleeve. STRUCTURAL (mechanical) wiring — sleeve identities, tiers, and the
# FA group each maps to. NOT frozen strategy weights (those live in blessed templates, §10.1).
SLEEVE_REGISTRY: dict[str, Sleeve] = {
    "S0-Conservative": Sleeve(
        sleeve_id="S0-Conservative", strategy_key="adaptive_all_weather",
        tier="Conservative", fa_group_name="tier_conservative",
        description="S0 Adaptive All-Weather ETF sleeve, Conservative tier"),
    "S0-Balanced": Sleeve(
        sleeve_id="S0-Balanced", strategy_key="adaptive_all_weather",
        tier="Balanced", fa_group_name="tier_balanced",
        description="S0 Adaptive All-Weather ETF sleeve, Balanced tier"),
    "S0-Growth": Sleeve(
        sleeve_id="S0-Growth", strategy_key="adaptive_all_weather",
        tier="Growth", fa_group_name="tier_growth",
        description="S0 Adaptive All-Weather ETF sleeve, Growth tier"),
    "S8-Overlay": Sleeve(
        sleeve_id="S8-Overlay", strategy_key="s8_british_ic",
        tier="Overlay", fa_group_name="s8_overlay",
        description="S8 SPX 0DTE scheduled credit-spread overlay sleeve"),
}


# =============================================================================
# Template — a named bundle of sleeve weights  (spec §2, §3.2)
# =============================================================================
@dataclass(frozen=True)
class Template:
    """A reusable product: a NAMED bundle of sleeve weights (spec §2). e.g.
    "Balanced+Overlay" = {S0-Balanced: 0.75, S8-Overlay: 0.25}. The weights are BLESSED
    numbers the CRM STORES — never numbers it invents or tunes (rule #1 / §10.1).

    `weights` maps sleeve_id -> fraction; the fractions must sum to ~1.0 (validate_template).
    Frozen so a validated template can't be mutated out from under a sizing/membership run."""
    template_id: str
    name: str
    weights: Mapping[str, float]
    active: bool = True

    def validate(self, registry: Mapping[str, Sleeve] = None) -> "Template":
        """Validate and return self (fluent). See validate_template for the rules."""
        validate_template(self, SLEEVE_REGISTRY if registry is None else registry)
        return self

    def to_dict(self) -> dict:
        return {
            "template_id": self.template_id,
            "name": self.name,
            "weights": dict(self.weights),
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "Template":
        return cls(
            template_id=d["template_id"],
            name=d["name"],
            weights=dict(d["weights"]),
            active=d.get("active", True),
        )


def validate_template(template: Template,
                      registry: Mapping[str, Sleeve] = SLEEVE_REGISTRY,
                      *, tol: float = WEIGHT_TOL) -> None:
    """Raise ValueError unless `template` is well-formed. Mirrors
    model_portfolio.validate_policy's rules and message style exactly:
      * a non-empty weights map,
      * every sleeve_id is a REGISTERED sleeve (typo guard),
      * every weight is a finite number in [0.0, 1.0] (no NaN/inf/negative/>1),
      * the weights sum to 1.0 within `tol`.
    Pure — no broker, no I/O."""
    if not template.weights:
        raise ValueError(f"template {template.template_id!r} has no sleeve weights")
    total = 0.0
    for sleeve_id, weight in template.weights.items():
        if sleeve_id not in registry:
            raise ValueError(
                f"{template.template_id}/{sleeve_id}: unknown sleeve_id — not in registry "
                f"({', '.join(sorted(registry))}). Register the sleeve first.")
        try:
            w = float(weight)
        except (TypeError, ValueError):
            raise ValueError(
                f"{template.template_id}/{sleeve_id}: weight {weight!r} is not a number")
        if not math.isfinite(w):
            raise ValueError(
                f"{template.template_id}/{sleeve_id}: weight {w} is not finite")
        if w < 0.0 or w > 1.0:
            raise ValueError(
                f"{template.template_id}/{sleeve_id}: weight {w} out of range [0, 1]")
        total += w
    if abs(total - 1.0) > tol:
        raise ValueError(
            f"{template.template_id}: sleeve weights sum to {total:.6f}, not 1.0 "
            f"(tol {tol}). Weights: {dict(template.weights)}")


def template_requirements(template: Template,
                          registry: Mapping[str, Sleeve] = SLEEVE_REGISTRY
                          ) -> frozenset[Requirement]:
    """A template's requirements = the UNION of its member sleeves' requirements (§5.1). So
    an overlay template (containing an S8 sleeve) AUTO-requires OPTIONS_L3 etc. — nobody tags
    the template by hand; it is computed from composition. PURE."""
    reqs: set[Requirement] = set()
    for sleeve_id in template.weights:
        if sleeve_id not in registry:
            raise ValueError(
                f"{template.template_id}/{sleeve_id}: unknown sleeve_id — not in registry "
                f"({', '.join(sorted(registry))})")
        reqs |= sleeve_requirements(registry[sleeve_id])
    return frozenset(reqs)


# =============================================================================
# AccountAssignment + AssignmentBook — the audited binding  (spec §3.3)
# =============================================================================
@dataclass(frozen=True)
class AccountAssignment:
    """One immutable binding of a client account to a template. "One active row per account;
    supersede, never overwrite; every change is an immutable audit row" (§3.3). Frozen — an
    assignment, once recorded, is an audit fact and is never edited in place."""
    account_id: str
    template_id: str
    effective_at: datetime
    set_by: str
    set_at: datetime
    prior_template_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "template_id": self.template_id,
            "effective_at": self.effective_at.isoformat(),
            "set_by": self.set_by,
            "set_at": self.set_at.isoformat(),
            "prior_template_id": self.prior_template_id,
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "AccountAssignment":
        return cls(
            account_id=d["account_id"],
            template_id=d["template_id"],
            effective_at=datetime.fromisoformat(d["effective_at"]),
            set_by=d["set_by"],
            set_at=datetime.fromisoformat(d["set_at"]),
            prior_template_id=d.get("prior_template_id"),
        )


class AssignmentBook:
    """In-memory account->template assignments with an APPEND-ONLY audit history (§3.3).

    Invariant: exactly ONE current row per account; a re-assign SUPERSEDES (records the
    prior template_id, appends a new immutable row, replaces the current) — it never
    overwrites or drops the old row. `history` is the full chronological audit trail.

    PURE / in-memory only — no persistence (transport is the open question §8). `now` /
    `effective_at` are INJECTABLE so tests fully control time (no hidden datetime.now)."""

    def __init__(self) -> None:
        self._current: dict[str, AccountAssignment] = {}
        self._history: list[AccountAssignment] = []   # append-only, chronological

    def assign(self, account_id: str, template_id: str, set_by: str, *,
               effective_at: Optional[datetime] = None,
               now: Optional[datetime] = None) -> AccountAssignment:
        """Bind `account_id` to `template_id`, superseding any current binding. Records the
        current row's template as `prior_template_id`, appends the new row to history, and
        replaces the current row. `set_at` = `now` (or datetime.now() if not injected);
        `effective_at` defaults to `set_at`. Returns the new assignment."""
        set_at = now if now is not None else datetime.now()
        eff = effective_at if effective_at is not None else set_at
        prior = self._current.get(account_id)
        prior_template_id = prior.template_id if prior is not None else None
        row = AccountAssignment(
            account_id=account_id,
            template_id=template_id,
            effective_at=eff,
            set_by=set_by,
            set_at=set_at,
            prior_template_id=prior_template_id,
        )
        self._history.append(row)
        self._current[account_id] = row
        return row

    def current(self, account_id: str) -> Optional[AccountAssignment]:
        """The account's current (latest) assignment, or None if never assigned."""
        return self._current.get(account_id)

    def history(self, account_id: str) -> list[AccountAssignment]:
        """The account's full assignment history, chronological. Returns an immutable COPY
        (a new list) so callers can't mutate the append-only audit trail."""
        return [row for row in self._history if row.account_id == account_id]

    def all_current(self) -> dict[str, AccountAssignment]:
        """A COPY of the current-assignment map, account_id -> current AccountAssignment."""
        return dict(self._current)


# =============================================================================
# Group-membership derivation  (spec §2 / §3.4)
# =============================================================================
def derive_group_membership(book: AssignmentBook,
                            templates: Mapping[str, Template],
                            registry: Mapping[str, Sleeve] = SLEEVE_REGISTRY
                            ) -> dict[str, set[str]]:
    """PURE derivation (§2 / §3.4): an account is a MEMBER of the FA group of EVERY sleeve
    its CURRENTLY-assigned template runs. Returns fa_group_name -> set of account_ids.

    Missing-data choice: a current assignment whose template_id is NOT in `templates` is a
    DATA ERROR (a dangling reference), so this RAISES ValueError rather than silently
    dropping the account — a silent skip could quietly strand an account outside every group
    and let it miss trades unnoticed. (An unknown sleeve_id inside a template likewise
    raises.) This is the deliberate fail-loud choice for the pure brain layer."""
    membership: dict[str, set[str]] = {}
    for account_id, assignment in book.all_current().items():
        template = templates.get(assignment.template_id)
        if template is None:
            raise ValueError(
                f"account {account_id!r} is assigned template "
                f"{assignment.template_id!r}, which is not in the provided templates map "
                f"({', '.join(sorted(templates))}) — dangling assignment.")
        for sleeve_id in template.weights:
            sleeve = registry.get(sleeve_id)
            if sleeve is None:
                raise ValueError(
                    f"template {template.template_id!r} references unknown sleeve_id "
                    f"{sleeve_id!r} — not in registry.")
            membership.setdefault(sleeve.fa_group_name, set()).add(account_id)
    return membership


# =============================================================================
# ILLUSTRATIVE templates — NOT blessed weights (rule #1 / §10.1)
# =============================================================================
# WARNING — NOT BLESSED. EXAMPLE ONLY. Mirrors model_portfolio.EXAMPLE_POLICY_* convention.
# These concrete weights are ILLUSTRATIVE fixtures so the model/tests have something to chew
# on; they are NOT the desk's frozen allocation and the CRM must never treat them as such.
# The CRM STORES blessed numbers; it does not invent or tune them (rule #1).
#
# OPEN FROZEN QUESTION (§10.1, do NOT presume here): for "balanced_overlay", does the S0
# portion reuse the SAME S0-Balanced tier, or a DE-RISKED S0 to make room for the option
# risk? That is a strategy/weights decision under rule #1 — it needs out-of-sample /
# per-regime validation + Andrew's sign-off and is FROZEN until then. Both the 75/25 split
# AND the choice of S0 tier inside the overlay below are placeholders pending that blessing;
# they are the example's, never the CRM's, to decide.
EXAMPLE_TEMPLATES: dict[str, Template] = {
    "balanced": Template(
        template_id="balanced", name="Balanced (ETF-only) — EXAMPLE, not blessed",
        weights={"S0-Balanced": 1.0}),
    "balanced_overlay": Template(
        template_id="balanced_overlay",
        name="Balanced + S8 Overlay — EXAMPLE, not blessed",
        # 75/25 and the S0-Balanced-within-overlay choice are FROZEN/illustrative (§10.1).
        weights={"S0-Balanced": 0.75, "S8-Overlay": 0.25}),
}
