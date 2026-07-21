# CRM Design — "CRM as the Brain" over FA Account Groups (Option A, blended accounts)

**Date:** 2026-07-21
**Conductor:** items **#42** (Model Portfolios foundation), **#43** (gateway verification); entries **#67–#70**.
**Status:** design LOCKED on **Architecture Option A** (one blended account per client; FA Account
Groups + our own engine; **no** IBKR Model Portfolios for automation). Buildable spec — a
developer can implement against it.

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
| **GroupDef** | `fa_group_name` (PK), `sleeve_id` (FK), `fa_method` | The FA group as it must exist on the gateway. `fa_method` is stored as **`""`** — the group's `ContractsOrShares` allocation governs; an order-level `faMethod="NetLiq"` is **rejected (Error 10226)** (see `order_router._base_fields` and `rebalance_engine` module docstring). |
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

7. **Place the block behind review → arm → transmit.**
   Exactly the existing armed path (`rebalance_execute.execute_armed` / `_run_armed_session`):
   acquire the gateway lock, back up the live GROUPS XML (`backup_fa_groups`), set that one group's
   `ContractsOrShares` to `per_account_split` via `set_group_contracts_or_shares` (a serialized
   `replaceFA(1, xml)` write), build the block with `order_router.build_fa_block(symbol, side,
   qty, limit, fa_group, fa_method="", as_of, ib)` (HARD PRICE GUARD `_check_limit_price` runs
   first), then `order_router.place(..., armed=True)` — one block at a time. The four-part gate
   (`READONLY=False` ∧ `DRY_RUN=False` ∧ `armed=True` ∧ exact `--arm-i-understand` token) is
   untouched and fails closed. **Never `whatIfOrder` a group order (it hangs).**

8. **On fills, reconcile via `orderRef` back into each account's sleeve ledger.**
   The block fills come back per allocated account (`execDetails` carry `execution.acctNumber`).
   Attribute each account's share of the block to `(account, sleeve)` — the sleeve is known from
   the block's **group → sleeve** mapping. Update `attributed_positions` / `attributed_cash`, bump
   `ledger_version`, set `last_reconciled_at`, then run the checksum (§7). The dedup gate
   (`order_router.already_present` → `LegState`) still protects against double-sends across
   restarts.

**S8 (multi-leg) note:** S0 legs are single-instrument `Stock` orders and drop straight into the
block machinery above. S8 credit spreads are **two-leg BAG/combo** orders — they still route
through an FA group (block fills at one price across the overlay group's accounts) but use the
options-safe order path (capped LMT → `REL` peg → marketable, per `config.ORDER_LADDER[INDEX_OPTION]`;
**never MIDPRICE/Adaptive** on options). The combo-as-FA-block path is **not yet exercised** and is
a gateway-test item (§10, §11).

---

## 7. The sleeve ledger & reconciliation (the hard part of Option A)

Because IBKR shows only the **blended** account, the CRM ledger is the **only** source of
per-sleeve positions and P&L. This is the part with no existing code.

### 7.1 orderRef encoding (attribution key)

Every order carries an `orderRef` that encodes the sleeve so fills attribute correctly. Two cases:

- **Block orders** allocate to accounts by the group's `ContractsOrShares`, so a single block
  carries **one** `orderRef` keyed on the **group** — today
  `paperbot:<fa_group>:<as_of>:<side>:<symbol>` (`order_router.build_fa_block`). The **account**
  comes from `execution.acctNumber` on each fill; the **sleeve** comes from the **group → sleeve**
  map. So `(sleeve, account)` is recoverable from `(orderRef group, acctNumber)` **without**
  changing the block orderRef.
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

`execDetails` + `commissionReport` per account → add shares to `attributed_positions[(account,
sleeve)]`, subtract/add cash (fill notional + commission) to `attributed_cash`. Positive delta
buys into the sleeve; negative sells out of it. The sleeve's P&L is derived from its attributed
cost basis vs. mark — the CRM computes it because IBKR won't split it.

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
4. **Gateway test of the group block path:** a same-price allocation `whatIf`/live probe proving a
   **group block fills every member account at one price** with the `ContractsOrShares` split, and
   that an **S8 multi-leg combo can ride an FA block** (`config.LADDER_FA_BLOCKS` is currently
   `False`; FA-block × MIDPRICE/Adaptive is unconfirmed). This is the load-bearing unrun test for
   Option A execution — analogous to the research's "Test 0," now re-pointed at groups+combos
   instead of models.

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

2. **The block `orderRef` carries no sleeve/account tag.** `build_fa_block` keys the block on the
   **group** only (`paperbot:<fa_group>:…`). Attribution to `(account, sleeve)` relies on
   `execution.acctNumber` + a **group → sleeve** map that **does not exist yet**, and on the
   invariant **one group == one sleeve** (which must be enforced). Direct legs need the
   sleeve-tagged ref from `model_portfolio.model_order_ref` re-purposed.

3. **No per-sleeve fill attribution / ledger writer exists.** `rebalance_execute` reconciles by
   re-reading blended `ib.positions(account)` against a tier model — it has **no** notion of
   attributing a fill to a sleeve. The entire §7 attribution + checksum layer is net-new.

4. **`set_group_contracts_or_shares` edits amounts, not membership.** It rewrites `<ListOfAccts>`
   with the per-order share split for a placement, then (implicitly) that group's members are
   whatever the placement listed. For durable **membership sync** (§8) the CRM needs a separate,
   deliberate membership write that is **not** tied to a single order's `ContractsOrShares`. The
   XML plumbing is there; the membership-management semantics are not.

5. **No netting detection anywhere.** `rebalance_engine.aggregate_blocks` aggregates by
   `(version, symbol, side)` **across accounts**, never **across sleeves within one account**. The
   netting watch (§7.3) has no code and must be added before two sleeves can safely share an
   instrument. (Low probability given S0-ETF vs S8-options, but unhandled today.)

6. **No margin/buying-power pre-check for a blended, multi-sleeve account.** `risk_manager.evaluate`
   checks per-account NAV/position/daily-loss limits, but there is **no** check that an account's
   **ExcessLiquidity/BuyingPower** can carry an **added options sleeve** on top of its existing ETF
   sleeve. §6 step 6's margin headroom check is net-new.

7. **No capability/permission reads at all.** Nothing in the codebase reads options level, account
   type, or trading permissions. The §5 gate — and its Web API/Flex sources — is entirely to-build.

8. **FA-block × multi-leg combo is unproven.** `config.LADDER_FA_BLOCKS = False`; FA-block
   compatibility with MIDPRICE/Adaptive is explicitly unconfirmed, and no S8 combo has been placed
   as an FA block. §10 item 4 must PASS before the S8 overlay can execute via groups.

---

*End of spec. Grounded in `paperbot/{rebalance_engine,order_router,rebalance_execute,accounts,config,model_portfolio}.py`
and `docs/{CRM_HANDOFF_model_allocation,MODEL_PORTFOLIO_RESEARCH}.md` as of 2026-07-21. Not committed.*
