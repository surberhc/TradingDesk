# CRM Design — "CRM as the Brain" over FA Account Groups (Option A, blended accounts)

**Date:** 2026-07-21
**Conductor:** items **#42** (Model Portfolios foundation), **#43** (gateway verification); entries **#67–#70**.
**Status:** design LOCKED on **Architecture Option A** (one blended account per client; FA Account
Groups + our own engine; **no** IBKR Model Portfolios for automation). Buildable spec — a
developer can implement against it.
**Execution architecture VALIDATED live on paper 2026-07-23** — the load-bearing §10.4 test PASSED
for both single-leg and multi-leg-combo FA blocks. Evidence + the two limitations it exposed
(no per-subaccount executions; no what-if pre-trade gate) are in **§13**, which corrects §6, §7.1,
§7.2 and §11 where they assumed otherwise.

> **SUPERSEDES (execution architecture only):** this document supersedes
> `docs/CRM_HANDOFF_model_allocation.md` **for the execution architecture**. That handoff
> assumed the **Model Portfolios / `modelCode`** path (Option-B-of-research "pure two-model" or
> the hybrid). That `modelCode` execution path is now **DEAD for automation** (see §0). The old
> file is retained for its still-valid *policy/audit/validation* contract and its research trail;
> where the two disagree on **how orders are executed and how per-sleeve state is tracked**, **this
> document wins**. The old file is **not** deleted.

---

## 0. Why the model path died, in one place

The research (`docs/MODEL_PORTFOLIO_RESEARCH.md`, `docs/CRM_HANDOFF_model_allocation.md`) settled
these against the live paper gateway and IBKR docs:

- **You cannot create, edit, rebalance, or fund a model via the gateway API.** Model creation,
  target-allocation, IBKR's own rebalance, **and the Independent→Model cash transfer that funds a
  model are all UI-only.** There is no API to make a `modelCode` sleeve "real."
- **`requestFA` on our gateway exposes only GROUPS / PROFILES / ALIASES** — not models. So the desk
  cannot even enumerate or reconcile model membership programmatically.
- **Multi-leg is likely barred in a model** (IBKR: a model "can invest in stocks and single leg
  positions"), which would exclude S8's SPX 0DTE two-leg credit spreads outright.

Net: models are a dead end for a hands-off desk. **Option A** keeps everything the desk *can*
drive from the API — **FA Account Groups + block orders + our own ledger** — and is LOCKED.

---

## 1. Purpose & the architecture in one paragraph

The **CRM is the brain**: it owns the policy (which client account runs which strategy sleeves at
what weights), holds the **full per-sleeve ledger** that IBKR will not keep for us, and drives the
desk's existing FA-group block-order machinery to execute that policy — while the desk keeps the
sacred review→arm→transmit gate and remains the only thing that ever touches the broker. Under
**Option A**, each client has **one blended IBKR account** under our FA master **`DF8922141`**, and
that single account can run **multiple strategy sleeves at once** (e.g. 75% S0 all-weather ETFs +
25% S8 SPX 0DTE credit-spread overlay). We use **FA Account Groups**, not Model Portfolios, because
(a) models are dead for automation (§0); (b) a **group block order fills every account in the group
at the same price at the same time** — the best-execution/fairness property we need across clients;
(c) a **blended account shares one margin pool**, so the S0 ETF holdings collateralize the S8
options overlay instead of stranding cash in a separate sub-account; and (d) the netting risk that
normally damns a single-blended-account design is **low here** because the two sleeves trade
disjoint instrument classes (S0 = broad ETFs, S8 = SPX index options) — real but rare, and handled
explicitly (§7). Because IBKR sees **one blended account per client**, the CRM must be the **full
sleeve ledger**: it tracks which positions and cash belong to which sleeve via `orderRef`-tagged
fills, since IBKR keeps no per-sleeve books for us.

---

## 2. The layered model

Five layers, top (pure strategy) to bottom (broker plumbing). Each layer only references the one
above it.

```
  STRATEGY        S0 (adaptive_all_weather) / S8 (s8_british_ic) code — computes the target basket.
     |            Frozen config (rule #1). Lives in strategies\, imported by backtester + paperbot.
     v
  SLEEVE          A strategy at a RISK TIER. e.g. "S0-Conservative", "S0-Balanced", "S8-Overlay".
     |            One sleeve == one (strategy, tier) pair == one FA group's worth of behavior.
     v
  TEMPLATE        A NAMED bundle of sleeve weights. e.g. "Balanced+Overlay" = {S0-Balanced: 0.75,
     |            S8-Overlay: 0.25}. Conservative/Balanced/Aggressive  x  ETF-only / +S8 overlay.
     v
  ACCOUNT         One blended client account  ->  exactly one Template (its blessed target mix).
  ASSIGNMENT      Audited: who assigned it, when, and the prior template.
     |
     v
  GROUP           The account is a MEMBER of one FA group PER SLEEVE its template runs. A block
  MEMBERSHIP      order per group fills all member accounts at one price. CRM keeps IBKR group
                  membership in sync with template assignments (via replaceFA, §8).
```

- A **Template** is the reusable product ("Balanced+Overlay"); an **Account assignment** binds one
  client account to one template; **Group membership** is the mechanical consequence — the account
  joins the FA group of every sleeve in its template.
- **Open sub-decision (frozen-config STRATEGY call, not a CRM call):** whether an *overlay*
  template's S0 portion reuses the *same* S0 tier as the ETF-only template of the same name (does
  "Balanced+Overlay" use S0-Balanced, or a de-risked S0 to make room for the option risk?). This is
  a **strategy/weights decision under rule #1** — it requires validation + Andrew's sign-off and is
  **frozen** until then. **The CRM does not decide weights; it only stores the blessed numbers.**
  Every template's sleeve weights are data the CRM holds, never data it invents or tunes.

---

## 3. CRM data model / entities

Relational sketch (a store-agnostic schema; §8 leaves file-vs-DB open). `PK`/`FK` noted; history
tables are append-only.

### 3.1 Client hierarchy

| Entity | Key fields | Notes |
|---|---|---|
| **Household** | `household_id` (PK), `name` | Billing / reporting rollup. Optional grouping. |
| **Client** | `client_id` (PK), `household_id` (FK), `name`, `contact` | The person/entity. |
| **Account** | `account_id` (PK = IBKR number, a `DU…` sub under `DF8922141`), `client_id` (FK), `status` | **Exactly one blended IBKR account per client** in Option A. Mirrors `accounts.AccountInfo` fields (`kind`, `is_master`, `funded`, …). |

### 3.2 Product / policy layer

| Entity | Key fields | Notes |
|---|---|---|
| **Sleeve** (registry) | `sleeve_id` (PK), `strategy_key`, `tier`, `fa_group_name`, `description`, `requirements` | Mirrors the desk. `strategy_key` ∈ {`adaptive_all_weather`, `s8_british_ic`}; `tier` ∈ {Conservative, Balanced, Growth, Overlay}. `fa_group_name` is the FA group implementing this sleeve (e.g. `tier_balanced`, `s8_overlay`). `requirements` = capability requirements (§5), **derived** from the strategy, not hand-typed. |
| **Template** | `template_id` (PK), `name`, `active` | e.g. "Balanced", "Balanced+Overlay". |
| **SleeveWeight** | (`template_id` FK, `sleeve_id` FK) (composite PK), `weight` | Weights within a template **must sum to 1.0 ± 1e-6** — reuse `model_portfolio.validate_policy`'s exact rule (`POLICY_WEIGHT_TOL = 1e-6`), each weight finite in `[0,1]`. |

### 3.3 Assignment + audit

| Entity | Key fields | Notes |
|---|---|---|
| **AccountTemplateAssignment** | `account_id` (FK), `template_id` (FK), `effective_at`, `set_by`, `set_at`, `prior_template_id` | The current binding. **One active row per account**; supersede, never overwrite. |
| **AssignmentHistory** | append-only rows of the above | Every change is an immutable audit row: who/when/effective/from→to. Same shape as the CRM_HANDOFF §6 compliance record. |

### 3.4 Group definitions + membership

| Entity | Key fields | Notes |
|---|---|---|
| **GroupDef** | `fa_group_name` (PK), `sleeve_id` (FK), `fa_method` | The FA group as it must exist on the gateway. `fa_method` is stored as **`""`** — the group's `ContractsOrShares` allocation governs; an order-level `faMethod="NetLiq"` is **rejected (Error 10226)** (see `order_router._base_fields` and `rebalance_engine` module docstring). **`faMethod=""` is the DOCUMENTED path, not a workaround** — IBKR: *"If specifying actual group name and the faMethod is blank/omitted the default method of that group will be used."* ([financial_advisor_methods_and_orders](https://interactivebrokers.github.io/tws-api/financial_advisor_methods_and_orders.html)). Note `ContractsOrShares` is a **profile-style** method, not one of the documented *group* methods (EqualQuantity / NetLiq / AvailableEquity / PctChange); under TWS build 983+ *"Use Account Groups with Allocation Methods"* groups and profiles are **unified** — `requestFA`/`replaceFA` accept **Group only** (Profile errors, which matches what `fa_probe.py` observed) and `placeOrder` accepts a profile name in `faGroup` ([financial_advisor](https://interactivebrokers.github.io/tws-api/financial_advisor.html)). Also documented: *"Unlike in TWS, there is not a default account allocation for the API — it must be specified with every order placed."* |
| **GroupMembership** | (`fa_group_name` FK, `account_id` FK) (composite PK), `synced_at` | Which accounts are in which group. **Derived** from template assignments (an account is in the group of every sleeve its template runs). CRM keeps IBKR in sync via `replaceFA` (§8). |

### 3.5 THE SLEEVE LEDGER (the heart of Option A)

The book IBKR does **not** keep for us. One row per `(account, sleeve)`:

| Field | Type | Meaning |
|---|---|---|
| `account_id` | string (FK) | The blended client account. |
| `sleeve_id` | string (FK) | Which sleeve this slice belongs to. |
| `target_weight` | number | This sleeve's weight in the account's template (denormalized for fast sizing). |
| `attributed_positions` | map `symbol → qty` (+ per-option `conId`/right/strike/expiry for S8 legs) | The positions the CRM attributes to this sleeve. **Sum across an account's sleeves must reconcile to the broker's blended holdings** (§7). |
| `attributed_cash` | number | Cash the CRM attributes to this sleeve. Sum across sleeves ≈ account TotalCash. |
| `last_reconciled_at` | timestamp | When this row was last checksummed against broker truth. |
| `ledger_version` | int (monotonic) | Bumped on every attributed fill; the transport/versioning key. |

**Source of truth boundary:** the broker is authoritative for the **blended totals** (positions,
cash, margin). The **CRM ledger is authoritative for the per-sleeve split** of those totals.
Reconciliation (§7) is the process that keeps the split honest against the totals.

---

## 4. Per-account IBKR state the CRM must track

To size sleeves and gate templates safely, the CRM needs a per-account snapshot of the blended
account. These are the tags to pull (names are the IBKR account-summary/`AccountValue` tags):

| Field (IBKR tag) | Why the CRM needs it |
|---|---|
| **TotalCashValue** / **SettledCash** (`SettledCashByDate`, format `YYYYMMDD:amount`) | Investable cash; settled-vs-total gates same-day reuse. Parse with `accounts.parse_settled_cash_by_date` (there is **no** flat `SettledCash` tag — confirmed live). |
| **BuyingPower** | Whether a sleeve slice at target size can be opened. |
| **AvailableFunds** | Headroom before initial-margin exhaustion. |
| **ExcessLiquidity** | Headroom before a maintenance-margin call — the SOFT-warn signal for the options overlay. |
| **FullInitMarginReq** / **FullMaintMarginReq** | Current margin load; the denominator for "can this account carry more S8 risk?". |
| **Cushion** | `(ExcessLiquidity / NetLiquidation)` — a single at-a-glance margin-health number. |
| **NetLiquidation** | The sizing base: `sleeve $ = NetLiq × template weight`. |
| **GrossPositionValue** | Leverage / exposure sanity check across the blended book. |
| **current positions** | The blended holdings the sleeve ledger must reconcile against (via `ib.positions(account)` — as `rebalance_execute` already does). |

**Sources (from research — state them explicitly):**

1. **Real-time (socket):** `reqAccountSummary` for the tags above, and
   `reqAccountUpdatesMulti` (the account-level, no-`modelCode` form) for streaming
   position/value updates. This is what `accounts.discover` / `rebalance_execute` already read via
   `ib.accountSummary` and `ib.positions`.
2. **EOD (Flex Web Service, token-auth, parse with `ibflex`):** the durable nightly snapshot.
   Two Flex sections matter here — **Account Capabilities** (the account's *margin/cash* type) and
   **Trading Permissions** (which asset classes the account is approved for). Flex is the coarse
   cross-check against the intraday socket reads.
3. **Options LEVEL specifically** (needed for the capability gate, §5): the socket API does **not**
   cleanly expose the client's approved options-trading level. Use the **FA Account Management
   Web API** `GET /gw/api/v1/accounts/{id}/details` (onboarding-gated OAuth — a **separate
   integration**, not the gateway socket). Because that OAuth integration is heavier and gated:
   - **Fallback:** a **manual CRM field** (`options_level`, set by Andrew per account at
     onboarding) is the near-term source of truth, **plus** a **nightly Flex coarse cross-check**
     (Trading Permissions asset classes) to catch drift between the manual field and reality.
   - Wire the Web API `details` call as the eventual automated source; until it's integrated, the
     manual field + Flex cross-check is the shipped path.

---

## 5. The capability gate (foolproof strategy selection)

The template dropdown in the CRM must make it **impossible** to assign an account a sleeve it
cannot legally or financially run.

### 5.1 Requirements are DERIVED, never hand-tagged

Each **sleeve** declares its requirements as a function of its **strategy**:

| Sleeve | Derived requirement |
|---|---|
| S8-Overlay (`s8_british_ic`) | **options Level 3** + **margin account** + **SPX-options (index-option) permission** |
| S0-* (`adaptive_all_weather`) | none special (cash-or-margin, no options) |

A **template's** requirements are the **union** of its sleeves' requirements. So an overlay
template (which contains an S8 sleeve) **auto-requires options L3** — nobody tags the template by
hand; it's computed from composition. Add an S8 sleeve to any template and the L3 requirement
appears automatically.

### 5.2 The gate behavior (per account, in the assignment UI)

For each candidate template, evaluate its requirements against the account's §4 snapshot:

- **HARD gray-out (block assignment):** a **missing permission**. If the account lacks options
  Level 3 (or index-option permission, or is a cash account), any overlay template is
  **disabled** in the dropdown with the reason shown. This is a categorical wall — no override in
  the CRM (fix the permission at IBKR first).
- **SOFT warn (allow, but flag):** **insufficient buying-power / margin** for the sleeve *at
  target size*. Compute the sleeve's opening margin need at `NetLiq × weight` and compare to
  **BuyingPower / ExcessLiquidity / Cushion**. If it would breach, show a warning (e.g. "S8 overlay
  at 25% would consume X of Y available; Cushion drops to Z%") but let Andrew proceed knowingly.
  Buying power fluctuates; permissions do not — hence hard vs. soft.

### 5.3 Requirement inputs

- **options level / index-option permission** ← §4 source #3 (Web API `details`, or the manual
  field + Flex cross-check fallback).
- **margin vs cash account type** ← Flex **Account Capabilities**.
- **buying-power / margin headroom** ← socket `reqAccountSummary` tags (§4).

---

## 6. The trade-construction & execution flow (the core algorithm)

This is the heart. It **reuses the existing Option-B engine** (`rebalance_engine` → `order_router`
→ `rebalance_execute`) and extends it from "one tier target per whole account" to "one target per
**sleeve** within a blended account." Existing primitives are named where they slot in.

**Step-numbered algorithm** (one full cycle):

1. **Trigger.** A cycle starts on either (a) a **strategy recompute** (the shared brain produced a
   new target basket for a sleeve), or (b) a **template change** in the CRM (an account's weights
   changed → it must rebalance to the new split). A template change is a rebalance trigger but
   **never** an auto-transmit (§9).

2. **Per strategy-group, compute the target basket (once).** For each FA group (= one sleeve =
   one (strategy, tier)), call the **shared strategy brain** to get that sleeve's target — exactly
   as `rebalance_run._targets_by_version()` does today, one `strategy_target.Target` per tier. This
   is computed **once per group**, not per account (fairness + efficiency).

3. **Per account in the group, size the sleeve.**
   `sleeve_$ = account NetLiquidation × template weight` for that sleeve — reuse
   `model_portfolio.sleeve_capital(net_liq, policy)` (the sizing math survives; only the *execution*
   changed). Then `investable = sleeve_$ × (1 − cash_reserve_pct)` via
   `rebalance_engine.compute_investable` / `model_portfolio._investable_for_sleeve`.

4. **Per account, compute the sleeve delta.**
   `delta = sleeve_target_shares − sleeve_attributed_current_holdings`, where the *current* side
   comes from the **sleeve ledger** (`attributed_positions` for `(account, sleeve)`), **not** the
   blended `ib.positions(account)`. This is the key Option-A change: the delta is against the
   sleeve's own book, so two sleeves in one account are diffed independently. The per-symbol,
   integer-share, account-level no-trade band logic (`rebalance_engine.plan_account` /
   `band_breached`, band = `config.REBALANCE_BAND_PCT` = 0.03) applies **per sleeve**.

5. **Aggregate same-instrument deltas across the group into ONE block per instrument.**
   Reuse `rebalance_engine.aggregate_blocks` (keyed `(group, symbol, side)`) → one `BlockOrder`
   per instrument with a `per_account` split, then `route_blocks` → a `RoutePlan`
   (`route="fa_block"`, `fa_group`, `fa_method=""`, `per_account_split`). The `per_account_split`
   is the explicit **`ContractsOrShares`** the group is set to. A block that touches only one
   account falls back to `route="direct"` (`order_router.build`).

6. **PRE-CHECK each account's margin / buying-power can support its slice.**
   Before any transmit, for each account in the block confirm its slice fits its **BuyingPower /
   ExcessLiquidity** (§4). Reuse `risk_manager.evaluate(...)` per account (as `rebalance_execute`
   step [6] does) **and** add the blended-account margin headroom check (new — §11). If an account
   cannot support its slice: **reject or scale** that account's share (drop it from the block's
   split; the block re-forms without it). Fail closed — a `HALTED`/veto stops the whole cycle with
   nothing transmitted.

   > **CONSTRAINT (verified 2026-07-23, §13.4): this pre-check must be built ENTIRELY from our own
   > account-summary reads — there is NO broker margin preview for an FA block order.**
   > `whatIfOrder` is not usable on an allocation order (with `transmit=True` the FA master returns
   > nothing at all; with `transmit=False` IBKR errors 321 and the call hangs). **Placing is the only
   > way IBKR will validate a block.** Any design that assumed a what-if gate in front of a block is
   > revised out — the gate is ours, computed from `reqAccountSummary` tags (§4) per account, never
   > an IBKR-returned margin delta.

7. **Place the block behind review → arm → transmit.**
   Exactly the existing armed path (`rebalance_execute.execute_armed` / `_run_armed_session`):
   acquire the gateway lock, back up the live GROUPS XML (`backup_fa_groups`), set that one group's
   `ContractsOrShares` to `per_account_split` via `set_group_contracts_or_shares` (a serialized
   `replaceFA(1, xml)` write), build the block with `order_router.build_fa_block(symbol, side,
   qty, limit, fa_group, fa_method="", as_of, ib)` (HARD PRICE GUARD `_check_limit_price` runs
   first), then `order_router.place(..., armed=True)` — one block at a time. The four-part gate
   (`READONLY=False` ∧ `DRY_RUN=False` ∧ `armed=True` ∧ exact `--arm-i-understand` token) is
   untouched and fails closed. **Never `whatIfOrder` a group order** — it returns nothing at best
   and hangs the caller at worst (§13.4).

8. **On fills, reconcile back into each account's sleeve ledger — from POSITIONS, not executions.**
   **Verified 2026-07-23 (§13.3): IBKR returns allocation-order executions ONLY at the FA master
   (`DF8922141`).** `execDetails` for a block carry the master's `acctNumber`, not the allocated
   subaccount's, and `reqExecutions(ExecutionFilter(acctCode=<DU…>))` returned **nothing** for any
   subaccount on this paper FA. So the attribution input is the **post-fill per-account position
   delta** (`reqPositions` / `ib.positions(account)` + `avgCost`), cross-checked EOD against a
   **Flex/activity statement** — *not* per-account `execDetails`. The sleeve is still known from the
   block's **group → sleeve** map, and the block's own price/commission come from the master-level
   execution + `commissionReport`. Update `attributed_positions` / `attributed_cash`, bump
   `ledger_version`, set `last_reconciled_at`, then run the checksum (§7). The dedup gate
   (`order_router.already_present` → `LegState`) still protects against double-sends across
   restarts.

**S8 (multi-leg) note:** S0 legs are single-instrument `Stock` orders and drop straight into the
block machinery above. S8 credit spreads are **two-leg BAG/combo** orders — they still route
through an FA group (block fills at one price across the overlay group's accounts) but use the
options-safe order path (capped LMT → `REL` peg → marketable, per `config.ORDER_LADDER[INDEX_OPTION]`;
**never MIDPRICE/Adaptive** on options). **The combo-as-FA-block path is now PROVEN — PASSED live on
paper 2026-07-23 (§13.2): a two-leg BAG rode an FA block and both legs allocated coherently to both
member accounts at identical avgCosts.** Spread-based sleeves therefore do **not** need per-account
routing. (`config.LADDER_FA_BLOCKS` is still `False`; FA-block × MIDPRICE/Adaptive remains
unconfirmed and untested — the proven path is a plain LMT combo.)

---

## 7. The sleeve ledger & reconciliation (the hard part of Option A)

Because IBKR shows only the **blended** account, the CRM ledger is the **only** source of
per-sleeve positions and P&L. This is the part with no existing code.

### 7.1 orderRef encoding (attribution key)

Every order carries an `orderRef` that encodes the sleeve so fills attribute correctly. Two cases:

- **Block orders** allocate to accounts by the group's `ContractsOrShares`, so a single block
  carries **one** `orderRef` keyed on the **group** — today
  `paperbot:<fa_group>:<as_of>:<side>:<symbol>` (`order_router.build_fa_block`). The **sleeve**
  comes from the **group → sleeve** map. The **account** does **NOT** come from
  `execution.acctNumber` — **verified 2026-07-23 (§13.3), the only execution IBKR returns for an
  allocation order is the FA master's**, so `acctNumber` on a block fill is `DF8922141`, never a
  `DU…` sub. The account split must be recovered from the **`ContractsOrShares` split we ourselves
  wrote for that block** and confirmed against the **per-account position delta** afterwards. So
  `(sleeve, account)` is recoverable from `(orderRef group, our own per-account split)` and
  *verified* from positions — **without** changing the block orderRef.
- **Direct (single-account) legs** should extend the ref with the sleeve so two sleeves in one
  account never collide — reuse the scheme already proven in
  `model_portfolio.model_order_ref`: `paperbot:<account>:<sleeve>:<as_of>:<side>:<symbol>`
  (it was written for `modelCode` but the shape — adding a sleeve tag — is exactly what the ledger
  needs; keep the idea, drop the `modelCode` semantics).

> **Design choice to confirm:** the block path recovers the sleeve from the group, not from the
> orderRef. That is sufficient **only while one group == one sleeve** (true by construction here).
> Keep that invariant; if a group ever served two sleeves, the block orderRef would need a sleeve
> tag too.

### 7.2 Attribution on fill

**REVISED 2026-07-23 — there are no per-subaccount executions to attribute from (§13.3).** The
block's **master-level** `execDetails` + `commissionReport` give the block's price, quantity and
total commission (one record, at `DF8922141`). The **per-account split** comes from a `reqPositions`
snapshot taken before/after the block, whose `avgCost` also carries each account's pro-rata share of
commission (observed: PDBC filled 2 @ 18.14 with commission 0.368706, and both subaccounts booked
`avgCost = 18.3244` = price + half the commission). From that delta → add shares to
`attributed_positions[(account, sleeve)]`, subtract/add cash (fill notional + its commission share)
to `attributed_cash`. Positive delta buys into the sleeve; negative sells out of it. The sleeve's
P&L is derived from its attributed cost basis vs. mark — the CRM computes it because IBKR won't
split it.

**Reconciliation must therefore be designed around positions + Flex, not around the execution
stream.** Treat the API as a **dead end for per-account execution records**: the durable
per-account audit artifact is the **Flex / activity statement**, and the intraday artifact is the
position delta. This is a real weakening of the compliance trail relative to what §7 originally
assumed, and the design owns it: **our own written `ContractsOrShares` split is the intended
allocation record, the position delta is the achieved-allocation proof, and Flex is the
independent EOD confirmation.**

### 7.3 NETTING WATCH (mandatory, even if low-risk)

Flag any cycle where **two sleeves in one account would trade the same instrument**. Here the risk
is **low** — S0 trades broad ETFs, S8 trades SPX index options, so overlap is unusual — **but it
must be handled**, because the broker holds one *blended* position and will **net** two sleeve
orders the ledger thinks are separate. Rules:

- **Detect:** before block aggregation, scan each account's per-sleeve deltas for a shared
  `symbol`/`conId`. If found, raise a `NETTING` flag on that account for that instrument.
- **Handle:** do **not** silently send offsetting orders. Options:
  (a) **net at the account level** and place the *net* delta, then split the resulting fill back
  across the two sleeves pro-rata in the ledger; or (b) **hold the smaller sleeve's leg** and
  alert. Default to (a) with an audit note. The point is the **ledger stays correct** even though
  the broker only ever saw one net order.
- This is exactly the failure mode the research flagged as the reason single-blended-account
  designs are dangerous; the netting watch is the mitigation that makes Option A safe.

### 7.4 Reconciliation drift & checksum (required)

The ledger **will** drift from broker truth (partial fills, manual actions, corporate actions,
dividends, rounding). Guardrail:

- **Checksum on every reconcile:** for each account,
  `Σ_sleeves attributed_positions == ib.positions(account)` (per symbol) and
  `Σ_sleeves attributed_cash ≈ TotalCashValue`. Any mismatch beyond a tolerance raises a
  **`LEDGER_DRIFT`** alert and **blocks that account from further automated trading** until a human
  reconciles (fail closed — never trade on a book you can't prove).
- **Unattributable holdings** (a blended position no sleeve claims — a corporate action, a manual
  trade) surface for review, mirroring the existing `ALIEN` handling in
  `rebalance_engine`/`reconcile` (`_NO_AUTOTRADE_STATUSES`). They are never auto-swept.
- **Nightly Flex cross-check** (§4 source #2) is the independent second opinion on the blended
  totals the socket read produced.

---

## 8. CRM ↔ desk integration & transport (still OPEN)

Division of labor (unchanged in spirit from the handoff, restated for Option A):

| Concern | Owner |
|---|---|
| Template definitions + sleeve weights (blessed numbers) | **CRM** |
| Account → template assignment + audit trail | **CRM** |
| Group definitions + which accounts are in which group | **CRM** (writes) / desk (executes `replaceFA`) |
| The sleeve ledger (per-sleeve positions/cash/P&L) | **CRM** |
| Capability snapshots (§4) read from IBKR | desk reads → **CRM** consumes |
| Strategy target computation + order construction + transmit | **desk** |

- **CRM writes** policy, assignments, template/sleeve weights, and group membership intent; **CRM
  reads** the capability snapshots the desk pulls. **Desk executes.**
- **Group membership sync:** the CRM also drives **FA group membership** so IBKR's groups match
  template assignments. Mechanically this is a `replaceFA(1, xml)` write to the GROUPS XML —
  the desk already reads/writes that XML (`rebalance_execute.set_group_contracts_or_shares` reads
  `requestFA(1)`, mutates one group's `<ListOfAccts>`, writes back `replaceFA(1, xml)`, preserving
  every other group). **Extend that to also add/remove `<Account>` members** when membership
  changes, not just the per-order `ContractsOrShares` amounts. Always `backup_fa_groups` first
  (destructive full-XML overwrite).
- **Transport — OPEN QUESTION (do not build until Andrew picks):**
  - **A. Versioned JSON file** — CRM writes a canonical `account_policies.json` + a
    `sleeve_ledger.json` (each carrying a monotonic `ledger_version` / `updated_at`) to a path the
    desk reads. Dead simple, diffable, no new infra; needs atomic-write discipline.
  - **B. Shared DB table** — natural history/versioning + concurrent-safety; heavier infra +
    schema coordination.
  Either way the contract is: **CRM is writer, desk is reader**, payload is a **versioned**
  policy + ledger so the desk can tell when something changed and treat it as a trigger. **Ledger
  transport is new vs. the handoff** (the handoff only moved the 3-field policy; Option A must also
  move the sleeve ledger, which is larger and mutates on every fill — a point in favor of B).

---

## 9. What's built vs. to-build

### Built (the head start — Option B machinery, verified)

| Capability | Where |
|---|---|
| FA-group **block order** construction (one price, split by `ContractsOrShares`) | `order_router.build_fa_block` (sets `faGroup`, `faMethod=""`, deterministic block `orderRef`) |
| Pure multi-account rebalance brain (per-account target shares, deltas, no-trade band, block aggregation, routing) | `rebalance_engine` (`plan_account`, `band_breached`, `aggregate_blocks`, `route_blocks`, `build_plan`, `RoutePlan`, `TIER_GROUPS`) |
| Armed executor: gateway lock, FA-XML backup, `replaceFA` `ContractsOrShares` write, one-block-at-a-time place, reconcile readout | `rebalance_execute` (`execute_armed`, `_run_armed_session`, `set_group_contracts_or_shares`, `backup_fa_groups`) |
| Review→arm→transmit gate + dedup gate + HARD PRICE GUARD + laddered execution | `order_router` (`transmit_guard`, `already_present`/`LegState`, `_check_limit_price`, `place_laddered`, ladder builders; options-safe `REL`; S5 conditional/OCA seam) |
| Per-account IBKR reads (NetLiq, cash, positions, funded/enrolled) | `accounts.discover` / `AccountInfo`, `parse_settled_cash_by_date` |
| Sleeve **sizing / policy / validation** concepts (keep — `modelCode` execution is dead) | `model_portfolio` (`AllocationPolicy`, `validate_policy` @ `1e-6`, `sleeve_capital`, `model_share_targets/deltas`, `SleevePlan`, the sleeve-tagged `orderRef` idea) |

### To-build (net-new for Option A)

1. **The sleeve ledger + reconciliation** (§3.5, §7) — the biggest new piece. Per-`(account,
   sleeve)` positions/cash/P&L, fill attribution by `orderRef`/`acctNumber`, netting watch,
   drift checksum. **No existing code.**
2. **Capability gate + Account Management API integration** (§5) — options-level reads (Web API
   `details` OAuth, or manual field + Flex cross-check), derived requirements, hard/soft gating.
   **No existing code.**
3. **Template layer** (§2, §3.2) — templates, sleeve weights, the assignment + audit tables.
   Reuse `validate_policy`'s numeric rules; the entities are new.
4. **Group-membership sync** (§8) — extend `set_group_contracts_or_shares` from amounts-only to
   **membership** (`<ListOfAccts>` add/remove) driven by template assignments.
5. **Per-sleeve delta wiring** (§6 step 4) — feed **sleeve** capital + **sleeve-ledger** current
   holdings into the plan, instead of whole-account NetLiq + blended `ib.positions`. The engine
   math exists; the *inputs* must change to be per-sleeve (§11).
6. **Transport** (§8) — versioned JSON vs DB, carrying both policy and the mutating ledger.

> **Sacred, unchanged:** the **review → arm → transmit** gate. **Zero real-money transmit today**
> — everything runs PAPER (port 4002, `DU…` subs under `DF8922141`) behind
> `READONLY=True`/`DRY_RUN=True` defaults and the exact-token arm. S8's live-pilot exception
> (`PILOT_MODE`) is a separate, zero-transmit path and does not change here.

---

## 10. Open items

1. **Overlay-tier strategy decision (§2):** does an overlay template reuse the same S0 tier, or a
   de-risked S0? **Frozen config under rule #1** — needs out-of-sample/per-regime validation +
   Andrew's sign-off before any weight is set. The CRM only stores the blessed result.
2. **Transport choice (§8):** versioned JSON file vs. shared DB — Andrew's call. Ledger size +
   fill-frequency lean toward DB, but not decided. **Do not build until chosen.**
3. **CP Web API `/fa` endpoints:** whether to *also* read the new Client Portal Web API `/fa`
   endpoints for group/allocation management. **Rejected for now** — the socket `requestFA` /
   `replaceFA` path is proven and the Web API adds OAuth + session-keepalive churn. Revisit only if
   REST/browser access is independently required (the options-level `details` call in §4 is the one
   Web API dependency we do accept, and only as a gated fallback).
4. ~~**Gateway test of the group block path**~~ — **CLOSED / PASSED 2026-07-23.** Both halves ran
   live on the paper FA and passed: a group block filled **both** member accounts at one price with
   the `ContractsOrShares` split (§13.1), and a **two-leg S8-shaped combo rode an FA block**
   successfully (§13.2). The `whatIf` half of the original probe turned out to be **impossible** —
   see §13.4. Full evidence in **§13**. (`config.LADDER_FA_BLOCKS` remains `False`; FA-block ×
   MIDPRICE/Adaptive is still unconfirmed and was not part of this test.)
5. **`OrderAllocation` vs `replaceFA`-per-block (§13.6):** **RESOLVED 2026-07-23 — keep
   `faGroup`/`replaceFA`.** Research established `OrderAllocation` is an **inbound-only** class on
   `OrderState` (added TWS API 10.33, 2024-12-17), *not* an outbound submission mechanism — so the
   swap the original decision imagined does not exist. The real hot-path hazard is handled
   architecturally: rewrite a group's XML only on **membership** change, never per-order; serialize
   the writes. Conductor **#50 CLOSED**. **Follow-up RESOLVED 2026-07-23 (§13.7.2):** the per-order
   `replaceFA` **is structurally required for the unequal-whole-contract (options) case** — a stable
   method (Percent) removes it **only** for fractional-tolerant equity sleeves (Percent allocates exact
   *fractional* shares on odd sizes, disqualifying for whole contracts). So the mitigation is
   **operational** (§12), not elimination. Net verdict unchanged: **keep `faGroup`/`replaceFA`.**

---

## 11. Gaps where existing Option-B code does NOT yet cover the spec (flagged honestly)

These are the concrete places a developer will hit a wall — called out so they aren't discovered
mid-build:

1. **`rebalance_engine.plan_account` sizes against the WHOLE account, not a sleeve.** It takes one
   `net_liq` and one tier `target`, and diffs against the account's **full** `positions`. Option A
   needs it to size against **`NetLiq × template weight`** and diff against the **sleeve ledger's**
   attributed positions. The band/delta/aggregation math is reusable, but the **caller must feed
   per-sleeve capital and per-sleeve current holdings** — that wiring does not exist. (`model_portfolio`
   has the per-sleeve sizing math but was built for the dead `modelCode` path.)

2. **The block `orderRef` carries no sleeve/account tag — and `execution.acctNumber` cannot supply
   the account.** `build_fa_block` keys the block on the **group** only (`paperbot:<fa_group>:…`).
   The **group → sleeve** map still **does not exist yet**, and the invariant **one group == one
   sleeve** must be enforced. **Worse than originally written (verified 2026-07-23, §13.3):** the
   account side cannot come from `execution.acctNumber` at all — allocation-order executions arrive
   only at the FA master. Attribution must be driven from the written `ContractsOrShares` split and
   confirmed by per-account position deltas (§7.1/§7.2). Direct legs need the sleeve-tagged ref from
   `model_portfolio.model_order_ref` re-purposed.

3. **No per-sleeve fill attribution / ledger writer exists.** `rebalance_execute` reconciles by
   re-reading blended `ib.positions(account)` against a tier model — it has **no** notion of
   attributing a fill to a sleeve. The entire §7 attribution + checksum layer is net-new. The
   position-delta-based attribution mandated by §13.3 makes this **more** work, not less: it needs a
   before/after positions snapshot per block rather than a passive execution listener.

4. **`set_group_contracts_or_shares` edits amounts, not membership.** It rewrites `<ListOfAccts>`
   with the per-order share split for a placement, then (implicitly) that group's members are
   whatever the placement listed. For durable **membership sync** (§8) the CRM needs a separate,
   deliberate membership write that is **not** tied to a single order's `ContractsOrShares`. The
   XML plumbing is there; the membership-management semantics are not.

5. **No netting detection anywhere.** `rebalance_engine.aggregate_blocks` aggregates by
   `(version, symbol, side)` **across accounts**, never **across sleeves within one account**. The
   netting watch (§7.3) has no code and must be added before two sleeves can safely share an
   instrument. (Low probability given S0-ETF vs S8-options, but unhandled today.)

6. **No margin/buying-power pre-check for a blended, multi-sleeve account — and IBKR will not help.**
   `risk_manager.evaluate` checks per-account NAV/position/daily-loss limits, but there is **no**
   check that an account's **ExcessLiquidity/BuyingPower** can carry an **added options sleeve** on
   top of its existing ETF sleeve. §6 step 6's margin headroom check is net-new — and per §13.4 it
   must be **entirely self-computed**, because `whatIfOrder` gives no margin preview for an FA block
   order. There is no broker-side pre-trade gate available to us on the block path.

7. **No capability/permission reads at all.** Nothing in the codebase reads options level, account
   type, or trading permissions. The §5 gate — and its Web API/Flex sources — is entirely to-build.

8. ~~**FA-block × multi-leg combo is unproven.**~~ **RESOLVED 2026-07-23 — PASSED (§13.2).** A
   two-leg SPY vertical BAG was placed as an FA block on `Balanced` and allocated coherently to both
   member accounts. §10 item 4 is closed. **Still open within this gap:** `config.LADDER_FA_BLOCKS`
   remains `False` and **FA-block × MIDPRICE/Adaptive is still unconfirmed** — the proven combo path
   is a plain LMT, not a laddered/adaptive one.

---

## 12. Operational refinements (design session 2026-07-22, Andrew)

Five refinements worked out with Andrew. These **refine** the sections named; where they add
detail, they win. No frozen numbers are set here — mechanics only.

### 12.1 Reconciliation is TRIAGED, not binary-freeze (refines §7.4)

The §7.4 checksum stays, but "any mismatch → freeze" is wrong on its own — it would bury the
desk in alerts for ordinary events (a dividend on a held position). The freeze is **per account**
already (never system-wide); the missing piece is grading *what* the drift is:

- **Explain drift from the broker's own transaction ledger — do NOT rebuild it from holdings.**
  Rather than predict what a dividend *should* be (holdings × rate × a corporate-action calendar),
  read IBKR's **activity ledger**, where every entry is already **labeled by the broker** (dividend,
  interest, payment-in-lieu, corporate action, fee, trade). Explaining a drift is then a lookup:
  cash moved $X → a labeled transaction for $X on that symbol exists → **explained, book it,
  no alert.** Less work than a calendar and it catches adjustments/reversals a calendar never would.
  Source = the **Flex activity sections** (extends the nightly Flex pull already in §4 source #2).
- **Timing consequence — the ledger is EOD, not live.** So intraday drift is held as
  **`UNEXPLAINED_PENDING`, not frozen.** The nightly transaction sweep clears the bulk
  automatically; only the **residual that survives the sweep** (drift with no matching broker
  transaction) escalates to the exclude-and-alert path. Don't panic on a mismatch we haven't had a
  chance to explain yet.
- Net behavior: tolerance band absorbs rounding → transaction ledger explains ordinary events →
  only genuinely unexplained residual latches the account out (§12.3).

### 12.2 Two-tier data cadence (refines §4)

Split the per-account reads by how fast they change:

- **Heavy Flex (account type, capabilities, trading permissions) — once daily**, flagged on change.
  This is account-management truth that rarely moves.
- **Balances / margin / buying-power (`reqAccountSummary` socket tags) — re-pulled PER TRANCHE,
  pre-flight.** A lightweight real-time socket read (already used at execution time) is cheap enough
  to run right before every scheduled option fire (e.g. pull at 09:03 for a 09:05 tranche). This
  pull **freezes the roster**: it decides which accounts are in, at what whole-contract count, and
  **locks that in before the order opens.** More tranches = more of these cheap pulls; fine.
- Keep distinct from §12.1: the pre-flight pull answers *"is the money there right now to size this
  tranche"* (live socket); ledger reconciliation answers *"did the books drift and can the broker's
  transaction log explain it"* (EOD Flex). Two needs, two cadences.

### 12.3 Faults LATCH — the unifying rule (refines §5.2, §7.3, §7.4)

Ledger drift, unexplained transaction, and below-floor size are the same rule in three hats:
**a fault pulls the account out, alerts ONCE, and the account sits idle for the REST OF THE DAY
until a human clears it. It never silently re-fires.** No re-evaluating fresh each tranche, no
assuming anyone is watching the alert screen. An account already latched-out is excluded at
pre-flight (§12.2) **without** re-alerting.

Two **distinct alert types** — different problems, different resolutions:

- **`TRADE_SKIPPED_LATCHED`** — operational; account is out for the day, resolve when convenient.
- **`TEMPLATE_NO_LONGER_QUALIFIES`** — the account's *situation changed* so its assigned template is
  no longer valid (classic case: a client runs the overlay for months, then makes a large withdrawal
  to buy a house and drops under the contract floor). This is **not** a trade hiccup — the fix is
  **reassigning the account to a fitting strategy**, not clearing a flag so it retries and fails
  again tomorrow. It points at reassignment, not retry.

### 12.4 Whole-contract sizing floor, per-firing / peak-concurrent (refines §5.2, §6 step 4)

Options are atomic — an account holds ≥1 whole contract or zero; there is no fractional contract.
Two rules the base spec's "integer-share" language does not cover:

- **Round each account to whole contracts independently** — no account rides another's fractional
  remainder.
- **Minimum-viable floor = 1 contract × (max firings open at once) × margin per spread.** If the
  strategy fires N times a day and those 0DTE spreads stay open until expiry, **peak concurrent
  exposure is N contracts**, so the floor is the capital to carry one contract at the day's
  worst concurrent moment — not one contract flat. An account below that **sits out entirely** and
  raises `TEMPLATE_NO_LONGER_QUALIFIES` (§12.3). This feeds the capability gate's soft-warn (§5.2)
  and means small accounts carry more weight-drift on the options sleeve than on the ETF sleeve —
  the design owns that instead of pretending contracts are continuous.
- **Sub-decision deferred to Andrew (frozen, rule #1):** an account above zero but below the
  full-schedule floor — sit out entirely, or run a **reduced schedule** (fewer firings/contracts)?
  Andrew's answer this session: **sit out entirely.** The CRM enforces whatever floor is blessed;
  it does not decide the schedule.

### 12.5 Cadence is CONFIG, not code (refines §6, §12.4)

The tranche schedule / firing spacing is **read as a parameter**, never hardcoded, so the §12.4
floor math (peak concurrent contracts × margin) recomputes automatically when the cadence changes.
**This does not loosen rule #1** — the cadence *values* stay frozen/blessed; we merely refuse to
bake them into code so a future blessed change needs no rewrite. Configurable ≠ tunable-on-a-whim.
Consequence: **no concrete floor number is computed until the tranche cadence is settled** (still
open as of this session) — the *formula* is what gets coded; the number drops in once blessed.

---

## 13. VERIFIED GATEWAY TEST RESULTS — 2026-07-23 (paper, live orders)

The load-bearing §10.4 test **ran and PASSED.** Everything in this section was **observed live on
the PAPER account (FA master `DF8922141`, port 4002) on 2026-07-23** — real orders, real fills, not
a simulation and not a what-if. Claims are limited to what was observed; nothing is extrapolated.

**Net verdict: FA Account Groups execute Option A's block model correctly, for single-leg AND for
multi-leg combos. The execution architecture is validated.** Two limitations came out of the same
session — no per-subaccount execution records (§13.3) and no what-if pre-trade gate (§13.4) — both
of which change *how we reconcile and how we pre-check*, not *whether the architecture works*.

### 13.1 Test 0 — single-leg FA block: **PASSED**

One order: `BUY 2 PDBC`, `faGroup="Balanced"`, `faMethod=""`, DAY marketable limit, clientId 35.

| Evidence | Observed |
|---|---|
| Fill | **ONE block**, single `execId` `00025b49.6a697500.01.01`, **2 shares @ 18.14** |
| Commission | 0.368706 |
| `permId` | 1979856224 |
| `orderRef` | `paperbot:Balanced:fa_block_fill_test:BUY:PDBC` |
| Where the execution appeared | **FA master `DF8922141` ONLY** |
| Per-account result (`reqPositions`) | `DU8922143` PDBC = **1**, `DU8922144` PDBC = **1**, both `avgCost` **18.3244** (= 18.14 + half the commission each). Master **flat**. |
| Errors | **Zero rejections. No error 10226.** |

**Reads:** the block filled at one price, split across both member accounts per
`ContractsOrShares`, commission was allocated pro-rata into `avgCost`, and the master netted flat.
This is exactly the fairness/best-execution property §1 relies on. `faMethod=""` was accepted
without complaint (consistent with the documented behavior cited in §3.4).

### 13.2 The open unknown — multi-leg COMBO on an FA block: **PASSED**

One BAG order, an S8-shaped vertical:

- SPY, expiry **20260814** (22 DTE), **BUY 1× 739C** (`conId` 898044756) / **SELL 1× 740C**
  (`conId` 898044796), ratio 1:1, **BUY 2 spreads**, **LMT 0.68 debit**
- `faGroup="Balanced"`, `faMethod=""`, `account=""`

| Evidence | Observed |
|---|---|
| Fill | **2 @ 0.66** in ~11 s |
| `permId` | 1979856236 |
| Errors | **Zero IBKR errors** |
| Where the executions appeared | **`DF8922141` ONLY** — legs `BOT 2 @ 12.27` / `SLD 2 @ 11.61`, plus a **BAG-level** execution (`conId` 28812380) `BOT 2 @ 0.66` |
| Per-account result (`reqPositions`) | `DU8922143` and `DU8922144` **each** +1 × 739C / −1 × 740C, **identical** avgCosts (1227.4473 / 1160.5255). Master **netted flat**. |

**CONCLUSION — this is the result that unblocks the S8 overlay:** a combo **can** ride an FA block;
**both legs allocate coherently** to every member account at identical cost; therefore
**spread-based sleeves do NOT need per-account routing.** §11 gap 8 is closed.

*Scope of the claim:* proven for a **plain LMT** 1:1 two-leg equity-option vertical on a 2-account
group. Not proven for MIDPRICE/Adaptive (`config.LADDER_FA_BLOCKS` is still `False`), not proven for
SPX/SPXW index options specifically, and not proven at larger group sizes. Those are extensions, not
doubts about the mechanism.

### 13.3 Reporting limitation — executions come back ONLY at the FA master

`reqExecutions(ExecutionFilter(acctCode=…))` was run **individually for all six managed accounts**.
It returned records for **`DF8922141` ONLY** — **nothing** for any `DU…` subaccount.

This matches IBKR's documented wording — *"Advisors executing allocation orders will receive
execution details and commissions for the allocation order itself. To receive allocation details and
commissions for a specific subaccount `IBApi.EClient.reqExecutions` can be used"* — **except that on
this paper FA the per-subaccount filter returned nothing.**

> **PARTLY OVERTURNED 2026-07-23 afternoon (§13.7.1):** this "master-only" result was taken while
> connected as a **non-master clientId (35)**. Re-run connecting as the **MASTER client id (0)**,
> per-subaccount `Execution.acctNumber` rows **DO** return **for EQUITY blocks** — so the morning "API
> is a dead end, use Flex" conclusion is **reversed for equities.** BUT **options/combos and closing
> orders remained master-only even as master**, so a complete per-account **options** trail still
> requires **Flex / activity statements.** `reqPositions` is the reliable per-account proof for
> equities too. See §13.7.1.

**Design consequence (already applied to §6 step 8, §7.1, §7.2, §11 gap 2):** treat the API as a
**dead end for per-account execution records.** Per-account proof of allocation must come from
**`reqPositions` + `avgCost`**, or from a **Flex / activity statement**. Reconciliation and the
compliance trail are designed around positions + Flex, not around the execution stream.

### 13.4 `whatIf` is NOT usable for FA allocation orders

Two distinct findings, both verified:

1. **A real bug in our own code path.** `ib_async`'s `whatIfOrder` copies the order with
   `whatIf=True` but **does not set `transmit`**. IBKR replies **error 321** — *"Error validating
   request.-'bD' : cause - What-If order should have transmit flag set to TRUE."* — and then
   **never resolves the future**, so `ib.whatIfOrder()` **hangs forever**. This affects
   `order_router.what_if()` for **ANY `transmit=False` order, not just FA ones**. It needs a hard
   timeout. *(Not fixed in this session — conductor **#48**.)*
2. **With `transmit=True`, the FA master returns nothing at all** for a group order. Tested at both
   30 s and 90 s deadlines, with **zero error events**. No IBKR documentation claims what-if support
   for allocation orders.

**CONSEQUENCE: there is no margin-preview / pre-trade gate available for FA block orders. Placing is
the only way to validate.** Any design element that assumed a what-if gate in front of a block is
**revised out** (§6 step 6, §11 gap 6). The pre-check is ours to compute from `reqAccountSummary`.

### 13.5 `paperbot\fa_block_test.py`'s recorded result is SUSPECT

That script's conclusion — *"the FA master ACCEPTS a group order, what-if only"* — used **exactly
the path §13.4 just showed is broken**: `order_router.what_if()` on a `transmit=False` order, which
**would have hung rather than returned an acceptance.** The recorded result therefore cannot be
trusted as evidence of anything.

**Flagged for re-verification or retirement.** Note that its underlying question is now answered far
more strongly by §13.1/§13.2 (an actual fill beats a what-if), so **retiring it is the likely right
call** — but that is a deliberate decision, not something to assume here. Conductor **#49**.

### 13.6 RESOLVED 2026-07-23 — keep `faGroup`/`replaceFA`; `OrderAllocation` is inbound-only

**Decision: keep the `faGroup` / `replaceFA` submission path.** The choice this section originally
posed turned out to be **largely a false choice.** Research on 2026-07-23 (deep, adversarially
verified, high-confidence with citations) established that **`OrderAllocation` is not an outbound
order-submission mechanism at all** — so there is no library to swap *to* for submission.

**What `OrderAllocation` actually is (high-confidence, documented):** an **INBOUND** class carried on
`OrderState` — the server *returns* it to describe an allocation, in `whatIf`/order-preview and in
order state. It was added in **TWS API 10.33** (IBKR Campus changelog dated **2024-12-17**). Its
fields are `Account`, `Position`, `PositionDesired`, `PositionAfter`, `DesiredAllocQty`,
`AllowedAllocQty`, `IsMonetary` — i.e. a *description* of an allocation, not a *directive* to place
one. There is **no "attach `OrderAllocation` to the order instead of `replaceFA`" path.** Outbound
allocation is still driven by `Order.faGroup` / `faMethod` / `faPercentage`.

*Sources:* IBKR Campus TWS API changelog (2024-12-17 entry, `OrderAllocation` added to `OrderState`);
two independent wire-protocol ports agreeing field-for-field — **scmhub/ibapi** (Go) and
**wboayue/rust-ibapi** (Rust) — decode the `OrderAllocation` array only on the *inbound* `openOrder`/
`orderStatus`/`whatIf` decode, never on the outbound `placeOrder` encode.

Consequences for this design:

- **Keep `faGroup`/`replaceFA` for submission.** It is current, documented, un-removed, and the path
  `ib_async` 2.1.0 supports. There is no submission mechanism to switch libraries *for* — the switch
  the original trade-off table imagined does not exist.
- **`ib_async` 2.1.0 has no `orderAllocations`** on `OrderState`. If the CRM ever needs to *read* the
  preview allocation array (inbound), it would need the official `ibapi` or a port — but that is a
  **read** concern, not a submission one, and `ib_async` remains fully sufficient for the legacy
  `faGroup`/`replaceFA` path we use.

**The real concern — `replaceFA` in the hot path — is addressed architecturally, not by a library
swap.** The genuine hazard the original decision worried about (a shared GROUPS-XML write per order)
is removed by design, not by `OrderAllocation`:

- **Only rewrite a group's XML when its MEMBERSHIP changes — never per-order.** The per-order mutation
  exists only because `ContractsOrShares` encodes share counts *as group config*.
- **Serialize the writes** (already the design intent: gateway lock + full-XML backup).
- **Follow-up RESOLVED 2026-07-23 (§13.7.2):** a stable method **does** remove the per-order
  `replaceFA` — but only where fractional shares are acceptable. **Percent** (configured once) allocated
  even *and* uneven order sizes with **zero per-order `replaceFA`**, but allocates **exact fractional
  shares on odd sizes** (`BUY 3 → 1.5/1.5`) — **disqualifying for whole contracts.** So for the CRM
  brain's core case — **unequal split of WHOLE contracts** (S8 options) — only **`ContractsOrShares`**
  works, and its per-account amounts change every order, making the **per-order `replaceFA` write
  STRUCTURALLY REQUIRED, not eliminable.** The hot-path hazard therefore **cannot be engineered away**
  for the primary use case; it is **managed operationally** (serialize, backup-before-write, read-back
  verify + fault-latch — the §12 refinements). Percent remains a stable, write-free option for
  fractional-tolerant **equity** sleeves only. This refines the *why*; it does **not** reverse the
  keep-`faGroup`/`replaceFA` verdict. (`EqualQuantity`'s odd-size rounding is the one untested cell —
  new conductor open item.)

*(Historical note: the original §13.6 posed this as an open `OrderAllocation`-vs-`replaceFA` decision
and carried a trade-off table premised on `OrderAllocation` being an alternative *submission* path.
That premise was wrong — `OrderAllocation` is inbound-only — so the table is superseded by this
resolution. Also relevant: no FA-allocation breaking changes appear in the 2025 (10.35–10.42) or 2026
(10.43–10.48) production release notes; the last FA-relevant change was the 2022 v981/10.22
groups/profiles unification we are already on.)*

### 13.7 Afternoon paper tests — 2026-07-23 (master-connect, allocation methods, order lifecycle)

A second live paper session the **afternoon of 2026-07-23** (PAPER account, port 4002, connecting as
the gateway's **MASTER API client id — clientId 0**; the gateway's `OverrideTwsMasterClientID` is empty
so 0 is the default master) settled three questions the morning test (§13.1–13.6) had left open or
recorded too pessimistically. Everything below is **verified by observation on 2026-07-23**; the one
untested cell is called out explicitly and tracked as a new open item.

#### 13.7.1 Per-subaccount executions ARE visible to a master-connect — for EQUITY blocks (settles the morning "master-only, use Flex" reversal)

Connecting as the **master client id** DOES return per-subaccount `Execution.acctNumber` rows —
**overturning the morning session's "allocation executions come back only at the FA master, use Flex"
conclusion** (§13.3), which was taken while connected as a non-master clientId (35).

| Evidence | Observed |
|---|---|
| Instrument | PDBC equity block |
| Per-subaccount execs (master-connect) | `DU8922143` and `DU8922144` **each surfaced their own 1-share execution** — execIds `0001106d.6a61995c.01.01` and `0001106d.6a61995a.01.01` |
| `ExecutionFilter(acctCode=<DU…>)` | Returned each subaccount's leg **individually** |

**BUT INCOMPLETE — a decisive nuance:** the **SPY option combo and ALL closing orders** attributed
**only to the FA master `DF8922141`**, even as master with explicit `acctCode` filters — no
sub-account legs came back. This was **NOT** session-cache aging: the surviving sub-account legs were
the **OLDEST** fills, so it is not a "recent fills not yet propagated" artifact.

**CONCLUSION:** per-account execution records are API-visible for **EQUITY blocks** via master-connect,
but **options/combos and closing orders are master-only** on this paper FA. So a **complete per-account
options trail still requires Flex / activity statements.** Also confirmed: **`reqPositions` (not
`reqExecutions`) is the reliable per-account allocation proof for equities too** — positions reconcile
per subaccount regardless of the execution-stream gap.

#### 13.7.2 A stable allocation method removes the per-order `replaceFA` — but only where fractional shares are acceptable (resolves the #50 architectural follow-up)

A **Percent** method group (`defaultMethod=Percent`, 50/50), configured **once**, allocated
`BUY 4 → 2/2` and `BUY 2 → 1/1` across `DU8922143`/`DU8922144` with **ZERO `replaceFA` between
orders** — proving a **stable method removes the shared-config hot-path write** the #50 follow-up
worried about.

**HOWEVER — the disqualifying limitation:** on **ODD** order sizes, Percent allocates **EXACT
FRACTIONAL shares**, not whole: `BUY 3 → 1.5/1.5`, `BUY 5 → 2.5/2.5` (deterministic exact-half). This
is **DISQUALIFYING for whole-contract sizing** — options cannot be fractional.

**THE DECISION MATRIX** (the resolution):

| Need | Method | Per-order `replaceFA`? |
|---|---|---|
| EQUAL split, whole shares, stable | **No stored-group method exists** — `EqualQuantity` is **REJECTED as a group `defaultMethod`** (error 10260, verified 2026-07-23 pm — §13.7.5); it is an *order-time* method only. Falls back to `ContractsOrShares` | Per-order write required (via `ContractsOrShares`) |
| UNEQUAL split, fractional acceptable (equity sleeves, e.g. S0) | **Percent** | **None** — stable, write-free |
| UNEQUAL split, **WHOLE contracts** (options sleeves, e.g. S8 — the CRM brain's core case) | **ContractsOrShares ONLY** | **Per-order write STRUCTURALLY REQUIRED** — the per-account amounts change every order |

**MATRIX NOW COMPLETE (2026-07-23 pm):** the only two methods valid as a stored-group `defaultMethod`
on this paper master are **`ContractsOrShares`** and **`Percent`**. **`EqualQuantity` is not a
stored-group method** (rejected, error 10260 — §13.7.5); `EqualQuantity`/`NetLiq` are **order-time**
allocation methods. There is therefore **no stable whole-contract group method** — for unequal whole
contracts, `ContractsOrShares` + its per-order `replaceFA` is the only path, now confirmed from two
directions (`Percent` gives fractionals on odd sizes; `EqualQuantity` is rejected as a group method).

**CONSEQUENCE for this design:** the `replaceFA` hot-path corruption risk behind #50 **CANNOT be
engineered away for the unequal-whole-contract case** — the CRM brain's primary use case (S8 options
with explicit per-account contract counts). It must be **MANAGED operationally**: serialize writes,
backup-before-write, read-back verify + fault-latch on mismatch — **exactly the §12 refinements already
specify.** For equity-only sleeves that tolerate fractional shares, **Percent is a stable, write-free
alternative.**

This does **NOT reverse the #50 decision** (keep `faGroup`/`replaceFA`, §13.6). It **explains WHY** the
per-order write is unavoidable for the primary use case and points the mitigation at **operations, not
elimination.**

#### 13.7.3 FA block modify/cancel lifecycle works (previously never exercised)

The morning test proved only **place-and-fill**. This session proved the **full working-order
lifecycle** on an FA group order:

| Step | Observed |
|---|---|
| Place | Resting (non-marketable) **DAY LIMIT** FA block on a Percent group → **Submitted**, `orderId` 174 / `permId` 1979857105 |
| Modify | Re-priced → **still Submitted**, `orderId` **and** `permId` both **stable** across the modify |
| Cancel | Reached **Cancelled**, `filled=0`, gone from `reqAllOpenOrders` (IBKR error 202 is the normal cancel ack) |

Nothing filled. **Place → modify → cancel is proven for FA group orders**, not just place-and-fill.

#### 13.7.4 Operational note — `reqPositions` needs a settle delay after a fill

`reqPositions` returns a **stale snapshot** if read immediately after a fill on a fresh master
connection; a **~4–5 s settle** before reading resolves it. Any monitoring that reads positions right
after an order **must include a settle delay or re-read** (reinforces the §12.2 pre-flight read
discipline and the §7.2 position-delta attribution, which now must wait for the settle before
snapshotting).

#### 13.7.5 `EqualQuantity` is REJECTED as a group `defaultMethod` (error 10260) — the matrix's last cell, closed

The one untested cell of the §13.7.2 matrix — `EqualQuantity`'s odd-size rounding — is **moot**:
`EqualQuantity` cannot be a stored-group method at all. Attempting to create a group with
`defaultMethod=EqualQuantity` on the paper FA master (`DF8922141`, port 4002, master clientId 0,
**verified by observation 2026-07-23 pm**) was **REJECTED** with:

> `Error 10260: Group <name> has unsupported method (EqualQuantity)`

The `replaceFA` write was refused **atomically** — no group created; the three existing groups
(Balanced / Conservative / Growth, all `ContractsOrShares`) stayed **byte-identical to backup**.
`EqualQuantity` is an **order-time** allocation method, **not** a valid stored-group `defaultMethod` on
this master. The odd-size rounding question is therefore never reached.

**CONCLUSION — the matrix is COMPLETE.** The only two methods valid as a stored-group `defaultMethod`
are **`ContractsOrShares`** and **`Percent`**; `EqualQuantity` (and `NetLiq`) are order-time methods,
not stored-group methods. There is **no stable whole-contract group method**, so the CRM brain's core
case (unequal split of whole option contracts) is served by **`ContractsOrShares` + per-order
`replaceFA` only** — now confirmed from two directions (`Percent` → fractionals on odd sizes;
`EqualQuantity` → rejected as a group method). This **reinforces, does not change,** the #50 resolution
(§13.6 / §13.7.2): the per-order write is structurally required and its mitigation is operational (§12).
*(`NetLiq`/`AvailableEquity` as group `defaultMethod`s were **not** tested — the two-viable conclusion
is grounded in the `ContractsOrShares`/`Percent`/`EqualQuantity` observations above.)*

---

*End of spec. Grounded in `paperbot/{rebalance_engine,order_router,rebalance_execute,accounts,config,model_portfolio}.py`
and `docs/{CRM_HANDOFF_model_allocation,MODEL_PORTFOLIO_RESEARCH}.md` as of 2026-07-21;
§12 refinements added 2026-07-22; **§13 verified live-paper test results added 2026-07-23** (and the
corrections it forced into §3.4, §6, §7.1, §7.2, §10, §11).*
