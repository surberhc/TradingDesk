"""crm_rebalance.py — the CRM -> desk per-SLEEVE delta-wiring bridge (conductor #42/#43).

docs/CRM_DESIGN_groups_brain.md §6 / §11 gap 1. The desk's pure rebalance engine
(paperbot/rebalance_engine.py) sizes each account against its WHOLE-account NetLiq and
diffs against the account's BLENDED broker positions. Option A needs it to size each
SLEEVE against NetLiq x template_weight (§6 step 3) and diff against the CRM SLEEVE
LEDGER's attributed positions for that (account, sleeve). The engine MATH is reusable
UNCHANGED — only the INPUTS change to be per-sleeve. This module produces those per-sleeve
inputs from CRM state and runs them through rebalance_engine.build_plan.

HARD BOUNDARIES (load-bearing — do not cross):
  * WHAT-IF ONLY. This builds NO ib_async order, transmits NOTHING, touches NO gateway, and
    does NOT call order_router's transmit path, rebalance_execute, or the arm gate. It stops
    at rebalance_engine's reviewable RoutePlan output, exactly as rebalance_engine itself
    does. It edits NO existing module (rebalance_engine / reconcile / cashflows / the crm/
    package are all reused unchanged).
  * DATA IN, NOT FETCHED. Per-account blended NetLiq, prices, and per-sleeve strategy Targets
    are passed IN as data. The bridge contacts no broker and computes no strategy target of
    its own (targets come from strategy_target.current_target upstream).
  * EQUITY (S0) SLEEVES ONLY, this slice. An OPTION sleeve — one whose
    crm domain.sleeve_requirements(sleeve) is NON-EMPTY (e.g. S8 'S8-Overlay',
    strategy_key 's8_british_ic') — takes a different (combo / options-ladder) execution
    path and is a SEPARATE follow-on. Such sleeves are SKIPPED here and returned in a clearly
    labeled `skipped_option_sleeves` list, never silently dropped or run through the equity
    block machinery.
  * NO FROZEN NUMBER INVENTED (rule #1). band_pct defaults to config.REBALANCE_BAND_PCT via
    rebalance_engine's own default (None flows through). No version.py bump: this is what-if
    only, not yet wired to placement (the same posture as the S5 seam); the VERSION bump
    comes when this is connected to rebalance_execute later.

THE `version` KEY CHOICE (documented, per the slice contract)
------------------------------------------------------------
rebalance_engine aggregates deltas across accounts by each account_input's `version`
(aggregate_blocks keys on (version, symbol, side)) and routes each block to
tier_groups[version]. For S0, sleeves are distinguished by TIER (Conservative/Balanced/
Growth), so we set `version = sleeve.tier`. Then:
  * all accounts running the SAME S0 sleeve aggregate into ONE block (same tier key), and
  * group_map[tier] = sleeve.fa_group_name routes that block to the sleeve's FA group.
For the S0 registry, group_map[tier] EQUALS rebalance_engine.TIER_GROUPS[tier]
(Conservative->tier_conservative, Balanced->tier_balanced, Growth->tier_growth), verified
by test. Passing our own group_map keeps the bridge correct even if a future sleeve's group
name diverges from that table.

THE reserve_for NUANCE (checked — see cashflows.reserve_for)
-----------------------------------------------------------
cashflows.reserve_for(account, nav) = RESERVE_MONTHS x (sum of the account's scheduled
monthly DISTRIBUTIONS). A distribution Flow is expressed either as a FIXED dollar `amount`
or as `pct_nav`. So reserve_for is a PER-ACCOUNT DISTRIBUTION reserve, and in the dominant
fixed-`amount` case it is a FIXED PER-ACCOUNT DOLLAR amount, independent of the nav argument.

Consequence for per-sleeve sizing: plan_account computes reserve = reserve_for(account,
net_liq) and investable = (net_liq - reserve) x (1 - cash_reserve_pct). We pass the SLEEVE
capital (net_liq_sleeve = blended_net_liq[account] x template_weight) as `net_liq`, matching
§6 step 3 ("investable = sleeve_$ x (1 - reserve)").
  * If the account's distribution is `pct_nav`-based, the reserve scales with sleeve capital
    -> correct.
  * If it is a FIXED dollar amount, reserve_for ignores the nav argument, so the FULL fixed
    reserve would be carved out of EVERY equity sleeve of a MULTI-sleeve template — i.e. a
    per-account distribution would be double-counted across sleeves. That is WRONG and is a
    KNOWN open concern (flagged, not silently mis-sliced). It does NOT affect this slice's
    live correctness because (a) cashflows.SCHEDULE is currently EMPTY, so reserve_for
    returns 0.0 for every account, and (b) a single-sleeve template (weight 1.0 — the parity
    case) sizes the sleeve at the full NetLiq, so the reserve is identical to the whole-
    account computation regardless. The correct fix, when a fixed-dollar distribution meets a
    multi-equity-sleeve template, is to apportion the per-account reserve across sleeves ONCE
    (e.g. pro-rata to sleeve weight) — deferred to when live schedules and multi-S0-sleeve
    templates actually exist.

CROSS-PACKAGE IMPORT (the crm/ flat-import bridge)
--------------------------------------------------
crm/ modules use flat imports (`import domain`, `import sleeve_ledger`, ...) that resolve only
with crm/ on sys.path — the same precedent as account_monitor_run.py adding the sibling
dailyreport dir. This USED to require a sys.modules save/restore dance: crm's ledger module
collided by bare name with paperbot/ledger.py (the audit trail, imported as `import ledger` by
execution_engine and ~6 others), so leaving crm's `ledger` registered in sys.modules would
have made a later `import ledger` in the paperbot suite return the WRONG module. Conductor #58
removed that collision by renaming crm/ledger.py -> crm/sleeve_ledger.py, so NO crm module now
shares a bare name with any paperbot module (verified: domain / capability / sleeve_ledger /
latch / brain / store have no paperbot namesake). The loader is therefore a plain sys.path
insert + normal import — it registers the crm modules under their now-distinct bare names, and
a later `import ledger` in the paperbot suite still resolves to paperbot's own audit-trail
ledger. sys.path is restored after the load (the loaded modules persist in sys.modules).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from typing import Mapping, Optional

import rebalance_engine


# =============================================================================
# CRM cross-package loader — plain sys.path insert + normal import (#58)
# =============================================================================
_CRM_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm")

# Dependency order: each module's flat sibling imports must already be registered when it
# executes (domain has none; capability/sleeve_ledger need domain; latch needs sleeve_ledger;
# brain needs domain/sleeve_ledger/latch/capability; store needs brain/domain).
_CRM_MODULE_NAMES = ("domain", "capability", "sleeve_ledger", "latch", "brain", "store")


def _load_crm_modules() -> dict:
    """Load crm/'s flat-import modules under their bare names.

    crm/ modules import their siblings flatly (`import domain`, `import sleeve_ledger`, ...),
    which only resolves with crm/ on sys.path. Conductor #58 renamed crm/ledger.py ->
    crm/sleeve_ledger.py, eliminating the sole bare-name collision with a paperbot module
    (paperbot/ledger.py, the audit trail). With no collision left, this is a plain load: put
    crm/ on sys.path, import the modules (registering each in sys.modules under its now-distinct
    bare name BEFORE exec so its sibling flat imports resolve to the crm version), then drop
    crm/ back off sys.path. A subsequent `import ledger` anywhere in the paperbot suite still
    resolves to paperbot's own audit-trail ledger, since no crm module is named `ledger`.

    Fails LOUDLY (ImportError) if crm/ or any expected module is missing."""
    if not os.path.isdir(_CRM_DIR):
        raise ImportError(
            f"CRM package dir not found at {_CRM_DIR!r} — the crm/ sibling package is "
            f"required for the CRM->desk rebalance bridge.")

    added_to_path = _CRM_DIR not in sys.path
    if added_to_path:
        sys.path.insert(0, _CRM_DIR)
    try:
        loaded: dict = {}
        for name in _CRM_MODULE_NAMES:
            path = os.path.join(_CRM_DIR, name + ".py")
            if not os.path.isfile(path):
                raise ImportError(
                    f"CRM module {name!r} not found at {path!r} — cannot build the "
                    f"CRM->desk rebalance bridge.")
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module          # register before exec (siblings resolve to it)
            spec.loader.exec_module(module)
            loaded[name] = module
        return loaded
    finally:
        # Leave sys.path pristine; the loaded modules persist in sys.modules under their
        # (collision-free) bare names, which is all the bridge needs.
        if added_to_path:
            try:
                sys.path.remove(_CRM_DIR)
            except ValueError:
                pass


_crm = _load_crm_modules()
crm_domain = _crm["domain"]
crm_ledger = _crm["sleeve_ledger"]
crm_brain = _crm["brain"]
crm_store = _crm["store"]


# =============================================================================
# 1) Per-sleeve rebalance inputs, extracted from CRM state
# =============================================================================
def _sleeve_attributed_positions(brain, account: str, sleeve_id: str) -> dict:
    """The (account, sleeve) attributed positions from the sleeve ledger, WITHOUT creating a
    row (a read must not mutate the ledger). Returns the raw Instrument -> qty dict, or {} if
    the sleeve has no attributed line yet."""
    for entry in brain.ledger.entries_for_account(account):
        if entry.sleeve_id == sleeve_id:
            return entry.attributed_positions
    return {}


def _equity_positions(attributed: Mapping, account: str, sleeve_id: str,
                      flags: list) -> dict:
    """Collapse a sleeve's attributed Instrument->qty map into a plain symbol->qty dict for
    the equity block machinery. An equity sleeve should hold only STK lines; a non-STK
    instrument appearing here is a data anomaly (an option leg mis-attributed to an S0
    sleeve) — it is FLAGGED and skipped, never fed to the equity engine."""
    out: dict = {}
    for inst, qty in attributed.items():
        if getattr(inst, "sec_type", "STK") != "STK":
            flags.append({
                "account": account, "sleeve_id": sleeve_id,
                "symbol": getattr(inst, "symbol", "?"),
                "sec_type": getattr(inst, "sec_type", "?"),
                "reason": "non-STK instrument on an equity sleeve — skipped from equity "
                          "sizing (review)",
            })
            continue
        out[inst.symbol] = out.get(inst.symbol, 0.0) + qty
    return out


def sleeve_inputs_from_crm(brain, blended_net_liq: Mapping[str, float],
                           prices: Mapping[str, float], *,
                           registry: Mapping = None) -> dict:
    """Extract per-SLEEVE rebalance inputs from CRM brain state (§6 step 3 / §11 gap 1).

    For each account present in `blended_net_liq` that has a CURRENT template assignment
    (brain.current_assignment), for each sleeve in that template:
      * an OPTION sleeve (crm domain.sleeve_requirements non-empty) is SKIPPED and recorded
        in `skipped_option_sleeves` (separate execution path — not this slice);
      * an EQUITY sleeve (no requirements) yields one rebalance_engine account_input:
          net_liq  = blended_net_liq[account] x template_weight   (the §6 step 3 sleeve $)
          positions= that (account, sleeve) sleeve-ledger attributed positions, as symbol->qty
          version  = sleeve.tier   (so per-tier aggregation + FA-group routing line up)
          group    = sleeve.fa_group_name
          prices   = the shared prices dict (per-account price override the engine consumes)

    An account with NO assignment contributes nothing. Returns:
        {"account_inputs": [...], "group_map": {tier -> fa_group_name},
         "skipped_option_sleeves": [...], "flags": [...]}
    `group_map` is the tier->group routing table for exactly the tiers present, suitable to
    pass straight to rebalance_engine.build_plan(tier_groups=...). PURE — reads CRM state and
    the passed-in data only; contacts no broker."""
    reg = crm_domain.SLEEVE_REGISTRY if registry is None else registry

    account_inputs: list[dict] = []
    group_map: dict[str, str] = {}
    skipped_option_sleeves: list[dict] = []
    flags: list[dict] = []

    for account in sorted(blended_net_liq):
        assignment = brain.current_assignment(account)
        if assignment is None:
            continue                     # no current template -> nothing to size
        template = brain.templates.get(assignment.template_id)
        if template is None:
            # A current assignment pointing at an unknown template is a data error; mirror
            # the fail-loud choice crm.domain.derive_group_membership makes for the same case.
            raise ValueError(
                f"account {account!r} is assigned template {assignment.template_id!r}, "
                f"which is not in the brain's templates "
                f"({', '.join(sorted(brain.templates))}) — dangling assignment.")

        net_liq_account = float(blended_net_liq[account])
        for sleeve_id, weight in template.weights.items():
            sleeve = reg.get(sleeve_id)
            if sleeve is None:
                raise ValueError(
                    f"template {template.template_id!r} references unknown sleeve_id "
                    f"{sleeve_id!r} — not in the sleeve registry.")

            if crm_domain.sleeve_requirements(sleeve):
                # OPTION sleeve (S8): non-empty requirements -> separate options path.
                skipped_option_sleeves.append({
                    "account": account, "sleeve_id": sleeve_id,
                    "tier": sleeve.tier, "group": sleeve.fa_group_name,
                    "weight": float(weight), "strategy_key": sleeve.strategy_key,
                    "reason": "option sleeve (non-empty sleeve_requirements) — routed to the "
                              "separate combo/options-ladder path, not the equity block "
                              "machinery",
                })
                continue

            # EQUITY sleeve -> a per-sleeve account_input for the pure engine.
            net_liq_sleeve = net_liq_account * float(weight)
            attributed = _sleeve_attributed_positions(brain, account, sleeve_id)
            positions = _equity_positions(attributed, account, sleeve_id, flags)
            version = sleeve.tier
            group_map[version] = sleeve.fa_group_name
            account_inputs.append({
                "account": account,
                "version": version,
                "net_liq": net_liq_sleeve,
                "positions": positions,
                "prices": dict(prices),
                "sleeve_id": sleeve_id,
                "group": sleeve.fa_group_name,
            })

    return {
        "account_inputs": account_inputs,
        "group_map": group_map,
        "skipped_option_sleeves": skipped_option_sleeves,
        "flags": flags,
    }


# =============================================================================
# 2) Run the per-sleeve inputs through the UNCHANGED pure engine
# =============================================================================
def plan_sleeve_rebalance(account_inputs: list[dict], targets: Mapping, *,
                          band_pct: Optional[float] = None,
                          group_map: Optional[Mapping] = None) -> dict:
    """Run per-sleeve inputs through rebalance_engine.build_plan and return its
    {plans, blocks, routes}. A thin REUSE of the pure engine, unchanged:

        build_plan(account_inputs, targets, band_pct=band_pct, tier_groups=group_map)

    `targets` maps version(tier) -> strategy_target.Target for that sleeve's strategy+tier.
    `band_pct=None` flows through to the engine's config.REBALANCE_BAND_PCT default (no frozen
    number invented here). `group_map` (tier -> fa_group_name) is the routing table from
    sleeve_inputs_from_crm; None lets the engine fall back to rebalance_engine.TIER_GROUPS.
    Builds no order and transmits nothing (build_plan is pure)."""
    return rebalance_engine.build_plan(
        account_inputs, targets, band_pct=band_pct, tier_groups=group_map)


# =============================================================================
# 3) Convenience: CRM state -> reviewable per-sleeve what-if in one call
# =============================================================================
def plan_from_crm(brain, blended_net_liq: Mapping[str, float],
                  prices: Mapping[str, float], targets: Mapping, *,
                  band_pct: Optional[float] = None) -> dict:
    """Tie (1) + (2): extract per-sleeve inputs from CRM state and run them through the pure
    engine. Returns {plans, blocks, routes, skipped_option_sleeves, flags} — a complete,
    reviewable per-sleeve what-if. Builds no order, transmits nothing, touches no gateway."""
    extracted = sleeve_inputs_from_crm(brain, blended_net_liq, prices)
    result = plan_sleeve_rebalance(
        extracted["account_inputs"], targets,
        band_pct=band_pct, group_map=extracted["group_map"])
    return {
        **result,
        "skipped_option_sleeves": extracted["skipped_option_sleeves"],
        "flags": extracted["flags"],
    }


# =============================================================================
# 4) Optional: pull CRM state from the SQLite store
# =============================================================================
def load_brain(db_path=None):
    """Load a CRMBrain from the CRM SQLite store (crm.store.CRMStore). With db_path=None this
    uses the store's DEFAULT off-Drive DB — a live-data convenience for a real caller.

    Tests must NEVER call this against the real DB; they build a synthetic in-memory brain
    instead (crm.brain.CRMBrain(...))."""
    store = crm_store.CRMStore() if db_path is None else crm_store.CRMStore(db_path)
    try:
        return store.load_brain()
    finally:
        store.close()
