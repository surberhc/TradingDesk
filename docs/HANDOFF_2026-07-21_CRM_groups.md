# Handoff — IBKR multi-strategy account architecture (2026-07-21)

**Start here** to resume. Then read `docs/CRM_DESIGN_groups_brain.md` (the current spec) and `conductor/STATUS.md`.

## Where we are
Designing + building how to run multiple strategies across client accounts at IBKR: S0 (adaptive all-weather ETFs) and S8 (SPX 0DTE credit-spread overlay), e.g. a client at 75% S0 / 25% S8.

## The big decision (LOCKED)
- **IBKR Model Portfolios are DEAD for automation** — can't create/edit/rebalance or FUND a model via the gateway API (funding needs a UI-only Independent→Model cash transfer); `requestFA` exposes only GROUPS/PROFILES/ALIASES (no models); multi-leg likely barred in a model. Empirical: the "Core" model on DU8922144 = +$1.09M positions / −$1.09M cash / ~$0 NetLiq (a position overlay, not a capital slice).
- **Architecture = FA Account Groups + CRM-as-brain.** **Option A** chosen: ONE blended IBKR account per client; the CRM is the FULL per-sleeve ledger (IBKR keeps no per-sleeve books). Groups give same-price/same-time block fills across accounts; the blended account shares one margin pool (S0 ETFs collateralize the S8 options overlay); netting risk low (ETFs vs SPX options) but handled explicitly.

## THE current spec
`docs/CRM_DESIGN_groups_brain.md` — read first. Grounded in the existing "Option B" code (`paperbot/rebalance_engine.py`, `order_router.py`, `rebalance_execute.py`, `accounts.py`, `model_portfolio.py`). Section 11 lists the honest gaps.

## Built this session
- `paperbot/model_portfolio.py` + tests (foundation; the `modelCode` EXECUTION path is now dead, but its policy/sizing/validation concepts are reused). paperbot suite 362 passing.
- Docs: `MODEL_PORTFOLIO_SPEC.md`, `MODEL_PORTFOLIO_RESEARCH.md` (tools+policy, cited), `CRM_HANDOFF_model_allocation.md` (SUPERSEDED for execution by the groups design), `MODEL_PORTFOLIO_GATEWAY_TEST_PLAN.md`, `CRM_DESIGN_groups_brain.md` (current).
- Fixes: gateway launcher path moved to `C:\IBC\StartGateway.bat`; read-wrapper hang fix; morning-execute stale tests de-flaked.

## Open decisions (need Andrew)
1. **Overlay-tier weights** — does "Balanced+Overlay" use S0-Balanced, or a de-risked S0 to make room for option risk? FROZEN config under rule #1; needs out-of-sample/per-regime validation + sign-off before any number is set. CRM only stores blessed numbers.
2. **Transport** — CRM↔desk: versioned JSON file vs shared DB (the mutating sleeve ledger leans DB).

## Load-bearing UNRUN test
Can an FA-group **block** fill every member account at one price with the ContractsOrShares split, AND can an **S8 multi-leg combo ride an FA block**? (`config.LADDER_FA_BLOCKS = False`; FA-block × combo unconfirmed.) Needs a deliberate ARMED gateway step (an FA-block whatIf hangs; the gateway's Read-Only API wall blocks it otherwise). This is Option A's execution "Test 0" — everything else can be built with confidence; this is the genuine unknown.

## Biggest to-build (from spec §9/§11)
1. Sleeve ledger + reconciliation (per-(account,sleeve) positions/cash/P&L; fill attribution by orderRef/acctNumber; netting watch; drift checksum that halts on an unprovable book). No code today — the heart of Option A.
2. Capability gate — options-level reads (FA Account Management Web API `GET /gw/api/v1/accounts/{id}/details`, onboarding-gated OAuth; fallback = manual CRM field + nightly Flex coarse cross-check) + blended-account margin pre-check.
3. Template layer (Conservative/Balanced/Aggressive × ETF-only/+overlay) + group-membership sync (extend `set_group_contracts_or_shares` from amounts to membership).

## IBKR facts to remember
- FA master **DF8922141**; paper sub-accounts **DU8922142–DU8922146**. Test account used: DU8922144 (reallocated 75/25 into empty models S0_ALLWEATHER/S8_ZERODTE — which did nothing, proving empty models don't populate).
- Options LEVEL is NOT on the gateway/TWS socket API or Flex — only the FA Account Management Web API `details` endpoint (separate OAuth integration). Margin/BP/cash = `reqAccountSummary` (socket). Account type/permissions (coarse) = Flex.
- CP Web API added `/fa` model management 2026-03-18 (create/edit/invest/rebalance) — REJECTED for now (unproven, IBKR-controlled rebalance) vs groups.
- Can't be in TWS desktop and the gateway at once (one login per paper username) — close TWS before API reads.

## Conductor
Items #42 (CRM handoff), #43 (gateway test session). Log entries #66–#70 track this arc.

## Next step
Andrew to (a) decide the two open items, then either build the sleeve ledger against the spec OR run the group-block gateway test (needs arming). 

---
(END OF HANDOFF CONTENT)
