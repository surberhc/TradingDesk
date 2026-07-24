"""capability.py — the CRM capability gate: foolproof strategy selection (#42/#43).

The §5 layer of docs/CRM_DESIGN_groups_brain.md — the gate that makes it IMPOSSIBLE to
assign an account a sleeve it cannot legally or financially run. It sits on top of the pure
domain model (crm/domain.py: Requirement, Template, sleeve_requirements,
template_requirements, SLEEVE_REGISTRY) and turns a per-account capability SNAPSHOT into an
enabled / grayed-out assignment view:

  * §5.1  Requirements are DERIVED, never hand-tagged. A template's requirements are the
          union of its sleeves' strategy-derived requirements (domain.template_requirements).
          This module never invents a requirement; it only CHECKS the derived ones against a
          snapshot.
  * §5.2  Two gates, deliberately different in kind:
            - HARD gray-out: a MISSING PERMISSION (options L3 / margin account / index-option
              perm). Categorical — the template is BLOCKED with a human reason, no override in
              the CRM (fix the permission at IBKR first). Permissions do not fluctuate.
            - SOFT warn: insufficient buying-power / margin AT TARGET SIZE. Computed as
              sleeve_need = NetLiq × weight (the §5.2 method), compared to BuyingPower /
              ExcessLiquidity, projecting the resulting cushion. It ALLOWS but flags — Andrew
              can proceed knowingly. Buying power fluctuates; hence soft, not hard.
  * §5.3  Requirement inputs: options level / index-option perm ← Web API details or the
          manual field + Flex cross-check; margin-vs-cash ← Flex Account Capabilities;
          buying-power / margin headroom ← reqAccountSummary socket tags (§4). ALL of these
          arrive here as data on the AccountCapabilities snapshot — a FUTURE live driver
          fetches them; this module fetches nothing.

HARD BOUNDARIES honored here (load-bearing — do not cross):
  * PURE / OFFLINE. stdlib only (dataclasses, typing) + crm.domain (itself pure). NO broker,
    NO ib_async, NO paperbot/config/order path, NO gateway, NO Flex / Web-API fetch. The
    account capability snapshot is passed IN as data; this module never fetches anything.
  * TRANSPORT IS OPEN (§8 / §10.2). to_dict()/from_dict() on the snapshot are the FUTURE
    serialization boundary — but NO persistence: no JSON/DB/file I/O anywhere here.
  * NO FROZEN NUMBER INVENTED (rule #1). The HARD gate is categorical (permission present or
    not) — no number at all. The SOFT gate implements EXACTLY §5.2's stated method
    (sleeve_need = NetLiq × weight), a MECHANICAL sizing calc (like model_portfolio.
    sleeve_capital), never a strategy number. `warn_cushion_floor` is a MECHANICAL / advisory
    param with a documented default (0.10) — it only decides "is the margin cushion getting
    thin enough to mention," never anything about allocation or strategy. Rule #1 untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional

import domain  # crm.domain — pure; Requirement / Template / *_requirements / SLEEVE_REGISTRY


# =============================================================================
# Mechanical / advisory soft-gate default — NOT a frozen strategy number
# =============================================================================
# The soft gate whispers a warning when the account's projected maintenance-margin cushion
# (ExcessLiquidity / NetLiquidation) would fall below this after opening the margin-consuming
# (options) sleeve at target size. 0.10 is a sensible ADVISORY default, clearly MECHANICAL:
# it only decides whether the margin picture is thin enough to flag for a knowing override
# (§5.2) — it NEVER blocks, and it never touches allocation or any strategy knob (rule #1).
# Overridable per call.
DEFAULT_WARN_CUSHION_FLOOR = 0.10


# =============================================================================
# 1) The account capability snapshot  (§4 tags / §5.3 sources)
# =============================================================================
@dataclass(frozen=True)
class AccountCapabilities:
    """A per-account snapshot of the blended IBKR account, as a FUTURE live driver would
    fetch it (§4 tags). The gate consumes this; it NEVER fetches it. Frozen — a snapshot is a
    point-in-time fact, not something the gate edits.

    Field sources (§5.3 — stated explicitly so the driver knows where each comes from):
      * options_level, index_option_perm ← Web API `GET /gw/api/v1/accounts/{id}/details`
        (onboarding-gated OAuth), OR the near-term fallback: a MANUAL CRM field set by Andrew
        at onboarding + a nightly Flex Trading-Permissions coarse cross-check (§4 source #3).
      * is_margin, account_type ← Flex **Account Capabilities** (§4 source #2).
      * net_liq, buying_power, excess_liquidity ← socket `reqAccountSummary` tags
        NetLiquidation / BuyingPower / ExcessLiquidity (§4 source #1).

    `options_level` is None when the account's approved level is unknown (e.g. the manual
    field has not been set) — treated as "no options approval" by the requirement checks."""
    account_id: str
    options_level: Optional[int]
    index_option_perm: bool
    is_margin: bool
    account_type: str
    net_liq: float
    buying_power: float
    excess_liquidity: float

    @property
    def cushion(self) -> float:
        """`ExcessLiquidity / NetLiquidation` — the §4 single at-a-glance margin-health number.
        Guarded: 0.0 when net_liq <= 0 (a zero/negative-equity account has no cushion, and we
        never divide by zero)."""
        if self.net_liq <= 0.0:
            return 0.0
        return self.excess_liquidity / self.net_liq

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "options_level": self.options_level,
            "index_option_perm": self.index_option_perm,
            "is_margin": self.is_margin,
            "account_type": self.account_type,
            "net_liq": self.net_liq,
            "buying_power": self.buying_power,
            "excess_liquidity": self.excess_liquidity,
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "AccountCapabilities":
        return cls(
            account_id=d["account_id"],
            options_level=d.get("options_level"),
            index_option_perm=d.get("index_option_perm", False),
            is_margin=d.get("is_margin", False),
            account_type=d.get("account_type", ""),
            net_liq=d.get("net_liq", 0.0),
            buying_power=d.get("buying_power", 0.0),
            excess_liquidity=d.get("excess_liquidity", 0.0),
        )


# =============================================================================
# 2) Requirement checks  (§5.1 / §5.3 — map each Requirement to a snapshot predicate)
# =============================================================================
# The ONLY place a Requirement is turned into an account predicate. Each entry answers
# "does this snapshot SATISFY the requirement?" — the union of these is the derived gate.
_REQUIREMENT_CHECKS: dict[domain.Requirement, Callable[["AccountCapabilities"], bool]] = {
    # Options Level 3+ approval — None (unknown) never satisfies.
    domain.Requirement.OPTIONS_L3:
        lambda caps: caps.options_level is not None and caps.options_level >= 3,
    # A margin account (cash accounts cannot carry the S8 options overlay).
    domain.Requirement.MARGIN_ACCOUNT:
        lambda caps: caps.is_margin,
    # Index-option (SPX) trading permission.
    domain.Requirement.INDEX_OPTION_PERM:
        lambda caps: caps.index_option_perm,
}


def satisfies(caps: AccountCapabilities, requirement: domain.Requirement) -> bool:
    """Does `caps` satisfy a single `requirement`? PURE. Raises on an unknown requirement (a
    Requirement with no predicate is a coding gap, not something to silently pass)."""
    check = _REQUIREMENT_CHECKS.get(requirement)
    if check is None:
        raise ValueError(
            f"no capability check for requirement {requirement!r} "
            f"(known: {[r.name for r in _REQUIREMENT_CHECKS]})")
    return check(caps)


def unmet_requirements(
        caps: AccountCapabilities,
        required: frozenset[domain.Requirement]) -> frozenset[domain.Requirement]:
    """The subset of `required` the account does NOT satisfy (§5.1). These are the HARD
    blocks — a missing permission the CRM cannot override. Empty frozenset == fully qualified.
    PURE."""
    return frozenset(r for r in required if not satisfies(caps, r))


# =============================================================================
# 3) Hard-gate reasons  (§5.2 HARD gray-out — one human line per missing permission)
# =============================================================================
def _reason_options_l3(caps: AccountCapabilities) -> str:
    have = "no options approval" if caps.options_level is None else f"L{caps.options_level}"
    return f"requires options Level 3 (account has {have})"


def _reason_margin(caps: AccountCapabilities) -> str:
    acct = caps.account_type or "cash"
    return f"requires a margin account (account is {acct})"


def _reason_index_perm(caps: AccountCapabilities) -> str:
    return "requires index-option (SPX) permission (account lacks it)"


# Requirement -> human reason builder. Keyed the same as _REQUIREMENT_CHECKS.
_REQUIREMENT_REASONS: dict[
        domain.Requirement, Callable[["AccountCapabilities"], str]] = {
    domain.Requirement.OPTIONS_L3: _reason_options_l3,
    domain.Requirement.MARGIN_ACCOUNT: _reason_margin,
    domain.Requirement.INDEX_OPTION_PERM: _reason_index_perm,
}


def hard_reasons(caps: AccountCapabilities,
                 unmet: frozenset[domain.Requirement]) -> list[str]:
    """A stable-ordered human reason per unmet requirement (§5.2). Ordered by the Requirement
    enum's declaration order so the UI reads the same every time. PURE."""
    reasons: list[str] = []
    for requirement in domain.Requirement:  # declaration order = deterministic
        if requirement in unmet:
            builder = _REQUIREMENT_REASONS.get(requirement)
            reasons.append(builder(caps) if builder is not None
                           else f"missing capability: {requirement.value}")
    return reasons


# =============================================================================
# 4) Soft-gate margin projection  (§5.2 SOFT warn — allow, but flag)
# =============================================================================
def soft_warnings(caps: AccountCapabilities,
                  template: domain.Template,
                  registry: Mapping[str, domain.Sleeve] = None,
                  *, warn_cushion_floor: float = DEFAULT_WARN_CUSHION_FLOOR) -> list[str]:
    """The §5.2 SOFT gate: does the account have the buying-power / margin to open this
    template's MARGIN-CONSUMING (options) sleeves AT TARGET SIZE? ALLOW-but-flag — this NEVER
    blocks; it returns warning strings for a knowing override.

    Method (EXACTLY §5.2 — a mechanical sizing calc, no strategy number):
      * For each sleeve, sleeve_need = net_liq × weight (like model_portfolio.sleeve_capital).
      * The margin-consuming sleeves are those that carry ANY requirement
        (domain.sleeve_requirements non-empty) — i.e. the options sleeves; ETF sleeves carry
        none. `margin_need` = Σ their needs; `aggregate_need` = Σ ALL sleeve needs (context).
      * Compare margin_need to BuyingPower and ExcessLiquidity, and project the resulting
        cushion = (ExcessLiquidity − margin_need) / NetLiquidation.
      * WARN (a non-empty list) when margin_need would exceed BuyingPower OR exceed
        ExcessLiquidity OR drop the projected cushion below `warn_cushion_floor`. The message
        mirrors §5.2's example ("S8 overlay at 25% would consume $X of $Y available; cushion
        drops to Z%").

    Guards: with net_liq <= 0, or no margin-consuming sleeve (an ETF-only template), or a
    non-positive margin_need, there is nothing to size against margin → NO warning (an
    ETF-only template never soft-warns, matching §5.1's "none special"). `warn_cushion_floor`
    is MECHANICAL/advisory (module header) — not a frozen strategy number. PURE."""
    reg = domain.SLEEVE_REGISTRY if registry is None else registry
    net_liq = caps.net_liq
    if net_liq <= 0.0:
        return []

    margin_need = 0.0
    margin_parts: list[tuple[str, float, float]] = []  # (sleeve_id, weight, need)
    aggregate_need = 0.0
    for sleeve_id, weight in template.weights.items():
        need = net_liq * float(weight)
        aggregate_need += need
        sleeve = reg.get(sleeve_id)
        if sleeve is None:
            raise ValueError(
                f"{template.template_id}/{sleeve_id}: unknown sleeve_id — not in registry "
                f"({', '.join(sorted(reg))}).")
        if domain.sleeve_requirements(sleeve):   # carries a requirement == margin-consuming
            margin_need += need
            margin_parts.append((sleeve_id, float(weight), need))

    if margin_need <= 0.0:
        return []

    projected_excess = caps.excess_liquidity - margin_need
    projected_cushion = projected_excess / net_liq  # net_liq > 0 guaranteed above

    breach_bp = margin_need > caps.buying_power
    breach_xl = margin_need > caps.excess_liquidity
    breach_cushion = projected_cushion < warn_cushion_floor
    if not (breach_bp or breach_xl or breach_cushion):
        return []

    # Per §5.2's example phrasing. `available` is the maintenance-margin headroom the overlay
    # eats into (ExcessLiquidity); buying_power is reported alongside for context.
    sleeve_desc = ", ".join(
        f"{sid} at {w:.0%} (${need:,.0f})" for sid, w, need in margin_parts)
    warnings: list[str] = [
        f"{sleeve_desc} would consume ${margin_need:,.0f} of ${caps.excess_liquidity:,.0f} "
        f"ExcessLiquidity available (BuyingPower ${caps.buying_power:,.0f}); projected cushion "
        f"drops to {projected_cushion:.1%} (advisory floor {warn_cushion_floor:.0%})"
    ]
    if breach_bp:
        warnings.append(
            f"margin need ${margin_need:,.0f} exceeds BuyingPower ${caps.buying_power:,.0f}")
    if breach_xl:
        warnings.append(
            f"margin need ${margin_need:,.0f} exceeds ExcessLiquidity "
            f"${caps.excess_liquidity:,.0f}")
    return warnings


# =============================================================================
# 5) Combined evaluation + the assignment-UI dropdown view  (§5.2)
# =============================================================================
@dataclass(frozen=True)
class GateResult:
    """The §5.2 gate verdict for one (account, template) pair — what the assignment dropdown
    renders: enabled vs grayed-out, plus any advisory margin warnings.

      * `allowed` == the HARD gate only (soft NEVER blocks): True iff no requirement is unmet.
      * `blocked_requirements` — the unmet requirements that caused a block (empty when allowed).
      * `hard_reasons` — one human line per blocked requirement (the gray-out tooltip).
      * `soft_warnings` — advisory margin flags (may be non-empty even when allowed=True;
        those are the allow-but-flag case). An allowed=True template with non-empty
        soft_warnings is assignable, with a warning Andrew overrides knowingly."""
    template_id: str
    allowed: bool
    blocked_requirements: frozenset
    hard_reasons: list
    soft_warnings: list


def evaluate_template(caps: AccountCapabilities,
                      template: domain.Template,
                      registry: Mapping[str, domain.Sleeve] = None,
                      *, warn_cushion_floor: float = DEFAULT_WARN_CUSHION_FLOOR
                      ) -> GateResult:
    """Run the full §5.2 gate for one template against one account snapshot. Returns a
    GateResult. `allowed` reflects the HARD gate ALONE (a missing permission blocks; soft
    never blocks).

    DESIGN CHOICE (documented): soft warnings are computed REGARDLESS of the hard verdict, so
    the UI can show BOTH the permission block AND the margin picture at once — a fixed-
    permission preview ("if this account got L3, would the money also fit?") still shows the
    margin story rather than going blank behind the hard wall. PURE."""
    reg = domain.SLEEVE_REGISTRY if registry is None else registry
    required = domain.template_requirements(template, reg)
    unmet = unmet_requirements(caps, required)
    reasons = hard_reasons(caps, unmet)
    warnings = soft_warnings(caps, template, reg, warn_cushion_floor=warn_cushion_floor)
    return GateResult(
        template_id=template.template_id,
        allowed=(len(unmet) == 0),
        blocked_requirements=unmet,
        hard_reasons=reasons,
        soft_warnings=warnings,
    )


def assignable_templates(caps: AccountCapabilities,
                         templates: Mapping[str, domain.Template],
                         registry: Mapping[str, domain.Sleeve] = None,
                         *, warn_cushion_floor: float = DEFAULT_WARN_CUSHION_FLOOR
                         ) -> dict[str, GateResult]:
    """The §5.2 assignment-UI view: for each candidate template, its GateResult (enabled vs
    grayed-out + any soft warnings). This is the whole point of the gate — it makes it
    "impossible to assign an account a sleeve it cannot legally or financially run": a
    hard-blocked template comes back allowed=False (grayed out) with the reason, a thin-margin
    template comes back allowed=True with a soft warning, a clean template comes back
    allowed=True with no warnings. Keyed by template_id. PURE."""
    reg = domain.SLEEVE_REGISTRY if registry is None else registry
    return {
        template_id: evaluate_template(caps, template, reg,
                                       warn_cushion_floor=warn_cushion_floor)
        for template_id, template in templates.items()
    }
