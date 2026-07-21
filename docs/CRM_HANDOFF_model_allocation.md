# CRM Handoff — Model Portfolio Allocation (interface contract)

**Conductor item:** #42
**Date:** 2026-07-20
**Audience:** the CRM build team (no prior knowledge of the TradingDesk repo assumed)
**Status:** foundation module built + live-probe-verified on the paper FA master; transmit
path, reallocation automation, and the CRM↔desk transport layer are NOT yet built.

This document is the **interface contract** between the CRM you are about to build and the
existing TradingDesk execution system. It is written to stand alone — you should be able to
build against it without reading the TradingDesk code. Where a rule comes from real code, the
source file is named so it can be verified.

---

## 1. Purpose

The CRM is being asked to own the **allocation decision and its audit trail**: a place where
Andrew sets, for one client account, how that account's capital is split across strategy
"sleeves" (e.g. *account X = 75% S0 / 25% S8*), records **who set it, when, the effective
date, and the prior value**, and emits that decision as a structured, versioned **allocation
policy**. The TradingDesk reads that policy and executes it (sizes each sleeve, computes
orders, and — behind a human review→arm→transmit gate — places them). The CRM is the system
of record for *intent*; the desk is the system of record for *execution*.

---

## 2. Division of responsibility

The boundary is deliberate and must not blur: **the CRM never places or transmits an order.**
It records intent as data. The desk executes that intent behind its safety gate.

| Concern | Owner |
|---|---|
| The allocation **decision** (which sleeves, what weights) | **CRM** |
| The **audit trail** (who set what, when, effective date, prior value, timestamp) | **CRM** |
| The **client ↔ IBKR-account mapping** (client → `DU…` sub-account) | **CRM** |
| The controlled **list of available sleeves** (the dropdown) | **CRM** (mirrors the desk registry — see §4) |
| Reading the policy | **TradingDesk** |
| Sizing each sleeve (NetLiq × weight) | **TradingDesk** |
| Computing per-model share targets/drift via the shared strategy brain | **TradingDesk** |
| Placing `modelCode`-tagged orders behind review→arm→transmit | **TradingDesk** |
| Per-model reconciliation (positions, NetLiq per sleeve) | **TradingDesk** |
| Choosing individual securities / holdings inside a sleeve | **TradingDesk** (never the CRM) |

**Explicit rule:** the CRM records **intent only**. Nothing the CRM does can cause an order to
be placed or transmitted. Execution happens on the desk, and only behind a deliberate, armed,
human decision (this is the desk's non-negotiable rule #2 — no real-money transmission without
an explicit armed decision; everything today is paper anyway).

---

## 3. The data contract — the allocation policy

This mirrors the real `AllocationPolicy` dataclass in
`paperbot/model_portfolio.py`. The desk ingests exactly these fields; the CRM must be able to
emit them.

### Fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `account` | string | yes | The IBKR **client sub-account** number (a `DU…` account under the FA master). Non-empty. |
| `weights` | map<string, number> | yes | `modelCode → weight`. Each key is a **registered sleeve** (§4). Each value is a **fraction of the account's NetLiq**. |
| `label` | string | optional | Human-readable name for the split, e.g. `"75% S0 / 25% S8"`. Free text; the desk does not parse it. |

> **Note:** the desk's `AllocationPolicy` carries **only** `account`, `weights`, and `label`.
> The audit-trail fields in §6 (who/when/effective-date/prior) are **CRM-owned metadata** that
> wrap the policy — they are the compliance record, not part of what the desk's sizing math
> consumes. The CRM stores the richer record; it emits the three-field policy to the desk.

### Validation rules (exact — from `validate_policy` in `paperbot/model_portfolio.py`)

The CRM must enforce these **before** saving/emitting a policy, because the desk will reject
any policy that fails them:

1. **Account is non-empty** — a policy must name an account.
2. **Weights map is non-empty** — at least one sleeve.
3. **Every `modelCode` is a registered sleeve** — each key must exist in the desk's model
   registry (§4). An unknown/misspelled code is rejected (it would otherwise silently route to,
   or read, the wrong sleeve).
4. **Every weight is a finite number in `[0.0, 1.0]`** — no `NaN`, no infinity, no negative, no
   value above 1.
5. **Weights sum to `1.0`** within an absolute tolerance of `1e-6` (`POLICY_WEIGHT_TOL`).

If the CRM stores an account→policy *map*, the map key for each entry must equal that entry's
own `account` value (a copy-paste guard — enforced by `validate_account_policies`).

### Example — two-sleeve policy (75% S0 / 25% S8)

```json
{
  "account": "DU8922142",
  "weights": {
    "S0_ALLWEATHER": 0.75,
    "S8_ZERODTE": 0.25
  },
  "label": "75% S0 / 25% S8"
}
```

### Example — single-sleeve policy (100% one model)

```json
{
  "account": "DU8922143",
  "weights": {
    "S0_ALLWEATHER": 1.0
  },
  "label": "100% S0"
}
```

Weights are **fractions of the account's NetLiq**, not dollar amounts. The desk multiplies
NetLiq × weight at execution time (§5); the CRM never sends dollars.

---

## 4. The sleeve / model vocabulary (authoritative, controlled)

Each sleeve is one strategy mapped to one IBKR "model" (a `modelCode`). This is the current
registry (`MODEL_REGISTRY` in `paperbot/model_portfolio.py`). **The CRM's sleeve dropdown must
match this list exactly.**

| `modelCode` | strategy key | description |
|---|---|---|
| `S0_ALLWEATHER` | `adaptive_all_weather` | S0 Adaptive All-Weather multi-asset ETF sleeve |
| `S8_ZERODTE` | `s8_british_ic` | S8 SPX 0DTE scheduled credit-spread sleeve |

Rules:

- This list is **authoritative and controlled**. The CRM must not offer a sleeve that is not in
  it, and must not let Andrew hand-type an arbitrary code.
- Adding a sleeve requires **two** coordinated steps: **(a)** a new registry entry in
  `paperbot/model_portfolio.py`, **and (b)** creating the matching model in the IBKR TWS UI
  (see §8 — model creation is UI-only). The CRM dropdown then follows.
- **`modelCode` strings must match the IBKR TWS model names byte-for-byte.** A rename in TWS, a
  trailing space, or a case difference orphans every order/read keyed on the old string and
  routes to the wrong sleeve or no sleeve. Treat these strings as exact identifiers, not labels.

---

## 5. Lifecycle & execution flow

```
  [CRM]                                  [TradingDesk]
   |                                          |
   | 1. Andrew sets/updates a policy          |
   |    (account + weights), with audit       |
   |    metadata (who/when/effective/prior)   |
   |                                          |
   | 2. Policy stored + emitted ------------> | 3. Desk reads the policy
   |    (canonical store, §7)                 |
   |                                          | 4. Desk reads per-sleeve NetLiq
   |                                          |    and sizes each sleeve
   |                                          |    (NetLiq × weight)
   |                                          |
   |                                          | 5. Desk computes per-model share
   |                                          |    drift via the shared strategy brain
   |                                          |
   |                                          | 6. Orders STAGED (transmit=False)
   |                                          |
   |                                          | 7. Human REVIEW
   |                                          | 8. ARM
   |                                          | 9. TRANSMIT (paper today; zero real money)
```

Key points:

- A **policy change is a rebalance trigger**: when the CRM changes an account's weights, the
  desk's next run will size to the new split and stage the trades needed to get there.
- A policy change **never auto-transmits**. It still passes through review → arm → transmit
  like any other order flow. There is no path from "CRM saved a new weight" straight to a live
  order.
- Everything today executes on the **paper** account. No real money moves.

---

## 6. What the CRM must store per account (the compliance record)

This is the audit trail behind every allocation change — the compliance record the desk relies
on the CRM to keep. Store, per account, and **retain history** (never overwrite in place):

| Field | Purpose |
|---|---|
| **Client identifier** | The CRM's own client key/name. |
| **IBKR account number** | The `DU…` sub-account under the FA master (§8). This is the `account` field the desk consumes. |
| **Policy** | The `modelCode → weight` map (the emitted data contract, §3). |
| **Effective date/time** | When this policy takes effect. |
| **Set-by (user)** | Who made the change (Andrew, or a named operator). |
| **Timestamp** | When the record was written. |
| **Prior policy** | The immediately preceding `modelCode → weight` map (previous value), so every change is diffable. |

Frame this as: *every allocation change produces a new, immutable audit row* — who, when,
effective when, from-what, to-what. This history is the compliance trail; the desk does not
keep it, so it must live in the CRM.

---

## 7. Transport — OPEN QUESTION (needs Andrew's + CRM's decision)

**How does a policy get from the CRM to the desk?** This is **undecided** and needs Andrew's
call. Below is a recommendation and the trade-offs, not a decision.

**Recommendation:** a **versioned, desk-readable canonical store** that the desk reads and the
CRM writes — the desk polls/reads it on each run. Two concrete shapes:

| Option | How | Pros | Cons |
|---|---|---|---|
| **A. JSON policy file** | CRM writes a canonical `account_policies.json` (array of the §3 objects) to a path the desk reads. | Dead simple; human-readable; trivially version-controllable/diffable; no new infra. | File-locking / atomic-write discipline needed; no built-in history unless you keep prior files. |
| **B. Shared DB table** | CRM writes rows to a table the desk queries. | Natural history/versioning; concurrent-safe; queryable audit trail. | New infra + schema coordination; heavier than the desk needs today. |

Either way the contract is the same: the CRM is the **writer**, the desk is the **reader**, and
the payload is the validated §3 policy. Keep it **versioned** (a monotonic version or updated-at
per account) so the desk can tell when a policy changed and treat that as a rebalance trigger.

**This section is explicitly open — do not build the transport until Andrew picks A, B, or
something else.**

---

## 8. Constraints & gotchas the CRM team MUST know

Verified against the live paper gateway on **2026-07-20**:

- **The FA master is `DF8922141`.** Client sub-accounts `DU8922142`–`DU8922146` exist under it,
  each with real paper NetLiq (~$1.08M–$1.12M). Policies name a **`DU…` sub-account**, never
  the `DF…` master — the master is the umbrella/connection point and is never traded directly.
- **A model must be CREATED in the IBKR TWS UI first.** Model creation, target-allocation setup,
  and IBKR's own rebalance are **UI-only — there is NO API to create or configure a model.** Two
  models exist today: `S0_ALLWEATHER` and `S8_ZERODTE`.
- **A model is INVISIBLE to the account API until an account is actually reallocated/invested
  into it.** "Create model" and "allocate account into model" are **two distinct steps**. A CRM
  policy only becomes *real* once the account has been reallocated into its models in TWS.
  Until then the desk cannot read per-model state for it. (Consequence for the desk: a broad or
  unallocated-model read never returns an end-marker and would hang — the desk's read wrapper is
  timeout-bounded to handle exactly this; it is not something the CRM triggers, but it explains
  why an "allocated but not yet invested" model shows nothing.)
- **`modelCode` must match the TWS model name byte-for-byte** (§4). Exact-match, case-sensitive,
  no stray whitespace.
- **Weights must sum to `1.0`** (tolerance `1e-6`), each in `[0, 1]`, finite (§3).
- **One account can hold multiple sleeves.** If two sleeves hold the same instrument (e.g. both
  long SPY), IBKR tracks each model's position **independently** — the desk keeps them separate
  and never nets them (the "fungibility" case). The CRM does not need to handle this; just know
  overlapping holdings across sleeves are fine.
- **The desk owns holdings. The CRM MUST NOT specify individual securities.** The CRM sets *how
  much* goes to each sleeve; *what* each sleeve buys is computed by the strategy brain on the
  desk. A policy is weights-per-model, never tickers.
- **Nothing transmits real money yet.** All execution is paper, behind review→arm→transmit.

---

## 9. Open design fork affecting the CRM data model

**Per-strategy vs. per-strategy × risk-tier granularity.** Currently one model per strategy
(`S0_ALLWEATHER`, `S8_ZERODTE`). But **S0 tiers internally** as Conservative / Balanced /
Growth. If the desk later decides a client's S0 sleeve should carry a *specific* tier, the model
must encode the tier too — e.g. `S0_ALLWEATHER_BALANCED`, `S0_ALLWEATHER_GROWTH` — and the
**sleeve vocabulary grows** accordingly. The CRM's model list would then need to reflect the
finer-grained set.

**This is undecided.** The current recommendation on the desk is to stay per-strategy until a
real client needs a non-default S0 tier inside a sleeve. **Implication for the CRM:** keep the
sleeve list **config-driven, not hardcoded** — sourced from (or synced to) the desk registry —
so that expanding to per-tier sleeves is a data change, not a code change.

---

## 10. Status / provenance

**Date:** 2026-07-20.

**Built and verified:**
- Foundation module `paperbot/model_portfolio.py` (registry, `AllocationPolicy` + validation,
  per-sleeve sizing, model-tagged order builder with `transmit=False`, per-model read wrappers,
  pure drift/rebalance math). Additive, read-only/compute-only — forms no connection, transmits
  no order, no import-time side effects.
- Design doc `docs/MODEL_PORTFOLIO_SPEC.md`.
- Account map `conductor/ACCOUNT_ALLOCATION.md` (FA master `DF8922141`, sub-accounts
  `DU8922142`–`DU8922146`).
- **Live-probe-verified (paper FA master, 2026-07-20):** sub-accounts exist with real paper
  NetLiq; per-model account-value plumbing works; `S0_ALLWEATHER` and `S8_ZERODTE` models
  created in TWS; the model-invisible-until-allocated behavior confirmed.

**NOT built (future, deliberately gated):**
- The **transmit path** (no real-money transmission; §7 transport also undecided).
- **Reallocation automation** — allocating an account into a model is a manual TWS action
  (UI-only, no API).
- The **CRM↔desk transport layer** (§7 — open).
- A live what-if probe confirming a model-tagged order books to the right slice before any arm.

**Commit references (for provenance):** the foundation module + spec landed on `main` prior to
this handoff; see `paperbot/model_portfolio.py`, `paperbot/test_model_portfolio.py`,
`docs/MODEL_PORTFOLIO_SPEC.md`, and `conductor/ACCOUNT_ALLOCATION.md` in the TradingDesk repo
for the authoritative, current definitions. If anything in this handoff and those files ever
disagree, **the code is authoritative** — re-derive the contract from `model_portfolio.py`.
