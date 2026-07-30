"""crm_execute.py — the PURE CRM->engine adapter (Control Plane multi-account, conductor
#64/#66, spec docs/PRODUCTION_REBALANCE_CONTROL_PLANE.md §6/§7, Phase 3 SAFE slice).

WHAT THIS IS
------------
A thin, PURE adapter that turns a reviewable CRM what-if (crm_rebalance.plan_from_crm's
`{plans, blocks, routes, ...}`) into per-account `ExecutionRequest`s and drives the shared
`safe_execute.execute_plan` in PREVIEW mode per account. It sits ALONGSIDE crm_rebalance
(the planner) and safe_execute (the executor) and glues them — it re-implements neither.

HARD BOUNDARY — PREVIEW ONLY, this slice:
  * Builds and transmits NOTHING. `requests_from_crm_plan` is a pure field-mapping loop;
    `preview_crm` runs execute_plan in MODE_PREVIEW with no `ib`, which sizes + builds the
    ordered leg list and runs the gate but sends nothing.
  * Changes NO gate logic. Requests are built with conform=False (an ONGOING rebalance is not
    a full-account DEPLOY, so it must NOT carry the deploy executor's liquidate-and-conform
    intent) and, by default, armed=False. With conform=False the PREVIEW's blocked-reasons
    WILL include "conform intent absent" and "not armed" — that is EXPECTED and CORRECT for a
    preview: the leg list of what WOULD trade is still produced and returned on each
    ExecutionResult. Suppressing those reasons is the DEFERRED order-affecting step (spec §7
    Step 3), OUT OF SCOPE here.
  * The account wall is the human-blessed roster (roster.enrolled_roster()), passed in as
    `roster` — never derived from the planner output.

No version.py bump: these are pure additions with no transmit-path behavior change.
"""
from __future__ import annotations

from safe_execute import (
    ExecutionCaps,
    ExecutionRequest,
    MODE_PREVIEW,
    execute_plan,
)


def _has_nonzero_orders(plan) -> bool:
    """True iff the plan has at least one non-zero share delta to trade. A plan whose
    `.orders` is empty or all-zero represents an account already in-band (nothing to do) and
    is skipped — no request, no preview, no leg list."""
    orders = getattr(plan, "orders", None) or {}
    return any(int(v) != 0 for v in orders.values())


def requests_from_crm_plan(crm_result, *, targets, quotes, prices, roster,
                           summaries=None, armed=False, kill=False) -> list:
    """PURE: map a CRM what-if (`crm_result["plans"]`) to one ExecutionRequest per account
    that actually needs to trade. Builds and transmits nothing.

    Loops `crm_result["plans"]` (per-(account, sleeve) AccountPlans), SKIPS any plan with no
    non-zero `.orders`, and for each remaining plan builds an ExecutionRequest with:
        account          = plan.account
        strategy_version = plan.version
        plan             = plan
        target           = targets[plan.version]
        quotes / prices  = the shared given dicts
        allowed_accounts = roster            (the human-blessed execution allow-list)
        caps             = ExecutionCaps()   (defaults)
        conform          = False             (ongoing rebalance — NOT a full-account deploy)
        run_id           = None
        net_liq          = plan.net_liq
        summary          = (summaries or {}).get(plan.account, [])
        armed / kill     = as passed (default armed=False -> preview)

    `summaries` maps account -> filtered accountSummary rows (for the buying-power gate on an
    armed run); absent -> an empty list per account (fine for a preview). Pure: reads the
    passed-in data only; contacts no broker."""
    summaries = summaries or {}
    requests: list = []
    for plan in crm_result["plans"]:
        if not _has_nonzero_orders(plan):
            continue
        requests.append(ExecutionRequest(
            account=plan.account,
            strategy_version=plan.version,
            plan=plan,
            target=targets[plan.version],
            quotes=quotes,
            prices=prices,
            allowed_accounts=roster,
            caps=ExecutionCaps(),
            conform=False,
            run_id=None,
            net_liq=plan.net_liq,
            summary=summaries.get(plan.account, []),
            armed=armed,
            kill=kill,
        ))
    return requests


def preview_crm(crm_result, *, targets, quotes, prices, roster) -> list:
    """Drive `safe_execute.execute_plan` in PREVIEW mode for every account in the CRM what-if
    that needs to trade, and return the per-account ExecutionResults.

    Each result carries the ordered candidate leg list (`.legs`, sells before buys) and the
    collected blocked reasons (`.reasons`). PREVIEW transmits NOTHING (no `ib` is passed).
    NOTE: with conform=False the reasons WILL include "conform intent absent" and "not
    armed" — expected and correct for a preview; the would-trade leg list is still produced.
    Pure: build requests, run the pure PREVIEW gate, return results."""
    requests = requests_from_crm_plan(
        crm_result, targets=targets, quotes=quotes, prices=prices, roster=roster)
    return [execute_plan(req, mode=MODE_PREVIEW) for req in requests]
