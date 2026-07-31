# CRM ↔ TradingDesk Convergence — Handoff & Design Brief

**Date:** 2026-07-31 · **Owner:** Andrew Surber · **Author:** Claude (TradingDesk session)

> START-HERE for a future session opened with access to BOTH folders (the CRM project AND
> `C:\TradingDesk`). Read this first, then read the CRM codebase. Do NOT build or modify
> either system until the convergence architecture below is produced and Andrew agrees it.

## 1. Why this exists
Andrew runs two systems that must converge:
- A **CRM** — a SEPARATE codebase / folder (mostly built out, fully loaded with client
  accounts, with its own live data feed). It is the system of record for clients, their
  accounts, which model each account is assigned to, and advisor ownership.
- The **TradingDesk** (`C:\TradingDesk`) — the research + validated strategy models + the
  safe, gated order-execution engine that actually trades through IBKR.

They are separate today and about to collide: the TradingDesk was starting to keep its own
tiny account→model list, but the real accounts/models/clients already live in the CRM.
Duplicating that would be wrong. This brief sets up the session that designs the integration.

NOTE ON A NAMING COLLISION: there is a folder literally named `crm/` INSIDE the TradingDesk
repo (`C:\TradingDesk\crm` — brain/domain/store/ledger). That is a trading-side rebalancing
helper module, NOT Andrew's real CRM application. Its state DB (`C:\TradingDesk-Local\crm\crm.db`)
does not even exist / is empty. Do not confuse the two.

## 2. The north-star capability (what we are building toward)
On any given day, Andrew or Ted can ask: "Which accounts are OUT OF SPEC relative to the
model they are currently assigned to?" — and then rebalance ALL out-of-spec accounts for a
chosen model/group, in ONE batch, behind the TradingDesk's review → arm → transmit gate.

Requirements Andrew stated:
- **Multi-advisor.** Ted has his own clients in their own models; Andrew has his clients in
  his models. Scope and ownership by advisor AND model.
- **Flexible model assignment.** Accounts move between models over time; "rebalance group X
  to model Y" must keep working as assignments change.
- **One-action batch.** Pick a group/model, see every out-of-spec account, trade them all at
  once — still fully gated.

## 3. Proposed division of responsibility (confirm in the dual-folder session)
- **CRM = system of record.** Owns client identities, accounts, advisor ownership (Ted vs
  Andrew), the model assigned to each account, and a live feed of account data. All
  account/model/client truth originates here.
- **TradingDesk = models + execution.** Owns the validated strategy targets
  (Growth/Balanced/Conservative), the pure rebalance/drift ("out of spec") engine, the safe
  execution engine (`paperbot/safe_execute.py::execute_plan` — DEPLOY-vs-REBALANCE purpose,
  per-account margin pre-flight, whole-share/price/notional caps, two-phase cash-gated
  transmit — all already built as of v0.29.0), the account-wall roster, and the SINGLE
  transmit chokepoint behind the review → arm → transmit gate.
- **Data flows ONE WAY: CRM → TradingDesk.** The CRM feeds the trading system the account
  list + model assignment + advisor owner (and possibly live positions/NAV). The TradingDesk
  NEVER writes client data back into the CRM, and NEVER reaches into the CRM to trade. The
  CRM NEVER places or transmits an order.

## 4. The target workflow (out-of-spec batch rebalance)
1. CRM provides: accounts + assigned model + advisor owner (+ live positions/NAV, or the
   trading system reads positions from IBKR directly — to be decided).
2. TradingDesk computes per-account drift vs the assigned model's target → who is OUT OF SPEC
   (beyond the no-trade band).
3. UI: pick advisor and/or model → see every out-of-spec account in scope, per-account plan +
   aggregate verdict.
4. Arm + Send: rebalance all selected out-of-spec accounts to their model in one batch via the
   REBALANCE lane (built, v0.29.0) behind review → arm → transmit, with the per-account margin
   pre-flight.

## 5. Hard guardrails (non-negotiable in any design)
- The TradingDesk safe execution engine is the SINGLE transmit chokepoint. The CRM never
  places or transmits an order. The human is always the transmit trigger (typed confirm +
  physically armed gateway).
- One-way data: CRM (record) → TradingDesk (execution). No trading-system writes into CRM
  client data (unless a separate, deliberate, gated path is later designed and approved).
- Real-money safety: roster allow-list (only blessed accounts), per-account margin pre-flight,
  whole-share/price/notional caps, two-phase cash-gated fills, durable audit.
- Models are FROZEN (rule #1, no curve-fitting); convergence never edits strategy/regime/band
  config.
- Multi-advisor isolation: Ted's and Andrew's books must be correctly owned/scoped; a rebalance
  must never cross an advisor's book unintentionally.
- IBKR currency: verify against IBKR's current API; real client accounts likely sit under an
  IBKR FA (advisor) master — confirm the gateway/account model (ties to the parked #57
  official-ibapi FA-block margin test).

## 6. Open questions to resolve WITH access to the CRM (investigate directly)
1. What is the CRM concretely — language, structure, DB schema, any API/export? READ its code.
2. How does the CRM represent accounts, model assignments, and advisor ownership (Ted vs Andrew)?
3. What is the CRM "live feed" — positions/NAV, or only account metadata? What is its source
   (IBKR? a custodian?)?
4. What is the cleanest, safest ONE-WAY, READ-ONLY integration seam? Weigh: a read-only export
   the trading system ingests; a small read-only API; a shared read-only view. Prefer the
   simplest read-only contract.
5. Where do the real client accounts live at IBKR — under an FA master? Which gateway/login?
   (Determines how the trading system reads positions and transmits per sub-account.)
6. What happens to the TradingDesk-side `crm/` module and `config.ENROLLMENT`? Retire,
   repurpose as the ingestion target, or keep as a thin read-only cache of the CRM's truth?
7. Out-of-spec computation: does the trading system pull positions from IBKR itself (execution
   truth) or consume them from the CRM feed? (Likely IBKR-direct for positions; CRM supplies
   the account→model→advisor list.)
8. Freshness + safety: guarantee the trading system only ever acts on accounts the CRM
   CURRENTLY blesses (roster synced one-way from CRM), never a stale/removed account.

## 7. Deliverable of the dual-folder session
A convergence architecture document + phased plan:
- The confirmed division of responsibility and the exact one-way data contract (fields/format
  the CRM hands the trading system).
- The integration seam (how the account→model→advisor list — and positions, if applicable —
  cross from CRM to TradingDesk, read-only).
- A phased build plan to the north-star, REUSING the already-built REBALANCE lane + per-account
  margin gate + Control Plane UI.
- What to build first (likely: read-only ingestion of the CRM's account→model→advisor list into
  the trading roster → the out-of-spec view → the gated batch send).

## 8. How to start that session
Open a session with access to BOTH folders (the CRM project AND `C:\TradingDesk`). Point it at
THIS doc first, then at the CRM root. Its first job is to READ the CRM and understand its data +
feed, THEN produce the convergence architecture in §7. It must not build or modify either system
until the architecture is agreed with Andrew.

## 9. Already in place on the TradingDesk side (so the design reuses, not rebuilds)
- REBALANCE execution lane + per-account margin pre-flight — `paperbot/safe_execute.py`
  (v0.29.0, purpose=DEPLOY|REBALANCE; margin gate reuses `s4_risk.margin_preflight`).
- CRM→engine PREVIEW adapter (pure, read-only) — `paperbot/crm_execute.py`
  (`requests_from_crm_plan`, `preview_crm`); planner `paperbot/crm_rebalance.py`.
- The account-wall roster accessor — `paperbot/roster.py` (`enrolled_roster()` off
  `config.ENROLLMENT`).
- The pure rebalance/drift engine — `paperbot/rebalance_engine.py` + `strategy_target.py`.
- The in-app Control Plane (single-account S0 today) with the review → arm → transmit gate,
  freshness/expiry guard, and audit — `dashboard/desk/page_control_plane.py`.
- Spec context — `docs/PRODUCTION_REBALANCE_CONTROL_PLANE.md`,
  `docs/CRM_DESIGN_groups_brain.md`, `docs/SAFE_EXECUTION_CONTRACT.md`.
