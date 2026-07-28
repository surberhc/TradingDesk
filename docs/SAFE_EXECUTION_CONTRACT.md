# Safe Execution Contract — every order the desk sends through the gateway

**Date:** 2026-07-28
**Status:** STANDARD — the checklist every armed order path must satisfy. Motivated by the first
live S0 deploy on trust account U14438624 (conductor #145), which executed but left the account
~$40k negative. This contract generalizes the fixes tracked in #63 to the WHOLE desk.

## Principle: one shared safe-execute layer, not per-strategy click-tools
Order safety lives in the **shared execution layer** every path runs through — `order_router`
(transmit/price/dedup/ladder), `rebalance_engine` (sizing, no-trade band, reconcile, ALIEN),
`rebalance_execute` (arm, gateway lock), and the CRM plan→execute flow — NOT in bespoke tools a
human clicks. Any strategy/account deploy or rebalance is a thin trigger that hands a plan to that
layer; the layer enforces this contract. `s0_live_deploy` is the prototype of the two-phase /
re-price logic; those checks get promoted into the shared layer so every future order — any account,
any sleeve, fired on a schedule through the gateway with no human clicking — inherits them.

## The contract

### A. Pre-flight — before a single order is sent
1. **Arm gate.** READONLY=False AND DRY_RUN=False AND armed token AND gateway physically
   write-enabled (read-only probe) AND kill-switch absent. Fail closed.
2. **Account authorization.** Order account is an allowed/enrolled account; refuse any other
   (single-account or roster wall).
3. **Model / entitlement clear.** Account not allocated to a conflicting IBKR Model Portfolio;
   trading permissions (margin type, options level) adequate for the sleeve. NOTE: our sub-account
   connection cannot see a model overlay — only the FA master can — so until master-level detection
   exists this is an explicit operator affirmation. Andrew is removing all models book-wide, which
   makes the desk the source of truth and reduces this to a backstop.
4. **Fresh, sane market data.** Quotes are live and sane; reject stale/None/zero prices before
   building a limit. (A stale quote is what mispriced BUCK's sell so it cancelled.)
5. **Fresh target.** The strategy target was computed from current data (stale-data guard).
6. **Real cash / buying-power.** Read actual TotalCashValue / BuyingPower from the account; the plan
   is sized to it — never to assumed or expected funds.
7. **Sizing constraints.** Whole shares (whole contracts for options); minimum-viable size; per-order
   notional <= cap (e.g. % of NAV) and total <= investable / available cash.
8. **Price guard.** Every limit within a sane band of the quote (fat-finger guard).
9. **Idempotency by position, not history.** Correctness comes from the engine's delta vs. CURRENT
   positions (a re-run just completes the remaining gap). Dedup only blocks a currently WORKING/open
   duplicate order — never a leg merely because an identical order filled earlier. (The SPY re-buy
   was false-blocked because the order tag was keyed on the monthly date and saw a prior fill.)

### B. Execution — while orders are live
10. **Two-phase, cash-gated.** Raise cash (sells) first, CONFIRM the cash actually landed, then commit
    buys only up to that confirmed cash. Never buy against proceeds that haven't settled. (Root cause
    of the negative: ~$115k of buys placed against ~$76k realized because a ~$40k sell cancelled.)
11. **Fill confirmation + straggler re-pricing.** Wait for each leg to reach a terminal state; re-peg /
    chase an unfilled leg within bounded attempts; never silently drop a straggler — report it loudly.
12. **Fail closed on the unexpected.** A cancelled/rejected/ambiguous leg halts the batch (and the next
    phase) rather than plowing ahead. (Today the run plowed into the buys after BUCK cancelled.)
13. **Serialized gateway access.** One coherent armed session; no racing another lane on shared
    gateway state.

### C. Post — after the batch
14. **Reconcile to target.** Verify resulting positions match the intended plan; report the residual
    and any unfilled leg.
15. **Durable audit log.** Every order, fill, re-price, and rejection written to a persistent log file.
    (Today the deploy logged only to a transient console window — a real gap.)
16. **Disarm + exposure sanity.** Return the gateway to read-only; confirm the account isn't negative
    or over-levered post-trade.

## Mapping to what exists (build on, don't reinvent)
- **Have:** `order_router` (transmit_guard, price guard `_check_limit_price`, dedup/LegState, laddered
  place), `rebalance_engine` (plan_account sizing, no-trade band, reconcile, ALIEN protection),
  `rebalance_execute` (arm token, gateway lock, FA XML backup), `s0_live_deploy` (two-phase +
  re-price + per-run ref + model gate — the prototype).
- **To fold into the shared layer:** two-phase cash-gating (currently only in s0_live_deploy),
  straggler re-pricing as a standard place() mode, position-based idempotency (retire history-keyed
  dedup for rebalances), the durable audit log, and fail-closed batch semantics — so a single
  "execute this plan safely on this account" primitive enforces A–C for every caller.

## To-build
1. Land the two-phase / re-price / per-run-ref / model-gate fixes in `s0_live_deploy` (conductor #63,
   in progress) — the proving ground.
2. Extract the safe-execute primitive into the shared layer (`order_router` / `rebalance_execute`) so
   deploy, ongoing rebalance, and CRM execution all call it.
3. Durable execution audit log for every armed run.
4. Master-level model detection (or keep the operator affirmation) — lower priority once models are
   removed book-wide.
