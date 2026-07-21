# MODEL_PORTFOLIO_SPEC.md — IBKR Model Portfolios "sleeve" system

Status: **foundational, additive module landed** (`paperbot/model_portfolio.py` +
`paperbot/test_model_portfolio.py`). READ-ONLY / compute-only. Nothing here transmits,
connects at import, or edits any existing order-flow / frozen-config / arming state. This
is built to go **LIVE** eventually, but **paper-first** and behind the same
review → arm → transmit gate as the rest of the desk (see `CLAUDE.md`).

Related: `conductor/ACCOUNT_ALLOCATION.md` (the account → strategy map),
`conductor/DECISIONS.md` (Option A/B execution-model decisions),
`paperbot/rebalance_engine.py` (the FA-block rebalancer this reuses in spirit),
`paperbot/order_router.py` (the order builder + PRICE GUARD this reuses without editing).

---

## 1. Goal

Allocate **one client account** across **multiple strategy sleeves** — e.g. a client whose
account runs **75% S0** (Adaptive All-Weather) and **25% S8** (SPX 0DTE) — with each sleeve
tracked, sized, and rebalanced independently, all under our **FA master DF8922141**.

This differs from the existing multi-account (Option B / FA-block) design in
`rebalance_engine.py`, which splits **one strategy across many accounts** (per-tier FA
groups). The sleeve system is the transpose: **many strategies inside one account**, keyed
on IBKR **modelCode** rather than on the account number.

The module is config-driven and extensible: add a sleeve to `MODEL_REGISTRY` and a client
policy is just another `AllocationPolicy(account, {modelCode: weight})`. There is **no
hardcoded 75/25** — that is one worked example; arbitrary per-client policies are supported
and validated (weights sum to ~1.0).

---

## 2. IBKR mechanism — Model Portfolios

- **Models** are IBKR's native construct for slicing a single account into strategy
  buckets. Each model has a **modelCode** (a string, e.g. `S0_ALLWEATHER`).
- **Order routing:** an order tagged with `Order.modelCode` (plus the client `account`, under
  the FA master connection) is booked against that model's slice.
- **Per-model reads:** `reqPositionsMulti(account, modelCode)` and
  `reqAccountUpdatesMulti(account, modelCode)` return state **per model**, so S0 vs S8
  exposure is read separately.
- **Model lifecycle is UI-only.** Creating a model, defining its target allocation, and
  triggering IBKR's own rebalance are **TWS/Client-Portal actions with no API surface**.
  Therefore **our client does NOT ask IBKR to rebalance** — instead it **computes the drift
  itself** and **places its own model-tagged orders** to hit target. IBKR's built-in
  auto-rebalance is deliberately not used; we own the drift math (the compliance trail lives
  in our orders, not in an opaque IBKR rebalance).

### Fungibility (the overlapping-instrument case)

When two models in the same account hold the **same instrument** (e.g. S0 and S8 both long
SPY), IBKR tracks **independent vs. model** positions and the multi API returns **one row
per (contract, model)**. Our module mirrors this: everything is keyed by modelCode, so the
two SPY sleeves get **separate targets and separate deltas — never netted**. Order refs also
carry the model (`paperbot:<account>:<model>:<as_of>:<side>:<symbol>`) so overlapping legs
never collide in the dedup gate.

---

## 3. Client library reality (ib_async 2.1.0) — the load-bearing finding

We use **`ib_async`** (the maintained ib_insync fork), **not native `ibapi`**. Verified
against the installed 2.1.0, not assumed:

| Capability | native `ibapi` | our `ib_async` 2.1.0 | Verdict |
|---|---|---|---|
| `Order.modelCode` | yes | **yes** (field present) | **Clean** — model routing works |
| `reqAccountUpdatesMulti(account, modelCode)` | yes | **yes** on the `IB` facade; `AccountValue.modelCode` field present | **Clean** — per-model account values work |
| `reqPositionsMulti(account, modelCode)` | yes | **partial** — see below | **Gap — workaround** |

**The `reqPositionsMulti` gap:** the high-level `IB` facade does **not** expose
`reqPositionsMulti`. The low-level `ib.client.reqPositionsMulti(reqId, account, modelCode)`
**does** send the correct TWS wire message, and the wrapper defines `positionMulti` /
`positionMultiEnd` callbacks — **but both are no-op stubs (`pass`)**, so ib_async **drops the
responses** and offers no accessor, event, or future. Plain `ib.positions()` returns
`Position` rows with **no `modelCode` field**, so it cannot separate sleeves.

**How the module handles each:**
- **Account values (clean):** `read_model_account_values` / `net_liq_for_model` use
  `reqAccountUpdatesMulti` + the real `AccountValue.modelCode` field. Fully supported.
- **Positions (workaround, clearly flagged):** `read_model_positions` **temporarily installs
  its own collector** on `ib.wrapper.positionMulti` / `positionMultiEnd` (instance-attribute
  shadowing — verified safe on 2.1.0), fires the request, pumps `ib.sleep()` until the End
  marker or a timeout, cancels the subscription, and **restores the originals in a
  `finally`**. It is read-only (a data request, never an order) and fails soft. The pure
  parsing core (`parse_model_positions`) is unit-tested; the live capture needs a gateway.
  **TODO:** replace this with a real ib_async wrapper method (upstream PR or a thin vendored
  `Wrapper` subclass that stores positionMulti rows) so we stop depending on an internal.

---

## 4. Module surface (`paperbot/model_portfolio.py`)

All pure/compute functions are broker-free and unit-tested; the read wrappers are the only
functions that touch a live `ib` handle, and they are strictly read-only.

- **(a) Registry + policy:** `ModelSleeve`, `MODEL_REGISTRY`, `AllocationPolicy`,
  `validate_policy` (sum-to-one, known-model, [0,1], finite), `validate_account_policies`,
  `FA_MASTER_ACCOUNT = "DF8922141"`, `is_fa_master`. Example policies:
  `EXAMPLE_POLICY_75_25` (75% S0 / 25% S8), `EXAMPLE_POLICY_S0_ONLY`, `EXAMPLE_ACCOUNT_POLICIES`.
- **(b) Sizing:** `sleeve_capital(net_liq, policy)` → `{modelCode: net_liq*weight}`. Pure.
- **(c) Model-aware routing:** `build_model_limit_order(...)` (mirrors
  `order_router._base_fields`/`build_fa_block`, reuses its `_check_limit_price` PRICE GUARD
  and `BuiltOrder`, `transmit=False` always), `apply_model_fields`, `model_order_ref`.
- **(d) Read wrappers:** `read_model_account_values`, `net_liq_for_model`,
  `read_model_positions` (workaround above), `parse_model_positions` (pure).
- **(e) Rebalancer brain (pure):** `model_share_targets`, `model_share_deltas` (per-model
  signed deltas, optional per-sleeve all-or-nothing band mirroring
  `rebalance_engine.band_breached`), `plan_account_sleeves` (policy → capital → deltas).

---

## 5. Safety posture

- **Additive.** No existing module was edited. `order_router.py`, frozen strategy/regime
  config, and gateway arming state are untouched. `model_portfolio` only **imports** from
  `order_router`/`config` (no side effects).
- **Read-only / compute-only.** No import-time connection. The read wrappers issue only
  IBKR **data** requests. Nothing builds-and-arms; every built order carries `transmit=False`.
- **Behind the gate.** Any future transmit path goes through the existing
  `order_router.transmit_guard` (READONLY + DRY_RUN + armed, fails closed) and the
  pre-transmit dedup gate — this module produces reviewable what-if plans only.
- **Runs under the FA master `DF8922141`** — the API connection point; client sleeve orders
  carry the client `account` + `modelCode` under that master.

---

## 6. OPEN design fork (needs Andrew's decision)

**Model granularity: per-strategy vs. per-strategy × risk-tier.**

- **Per-strategy (current):** one model per strategy — `S0_ALLWEATHER`, `S8_ZERODTE`. Simple;
  matches how sleeves are described.
- **Per-strategy × risk-tier:** S0 already tiers internally (Conservative / Balanced /
  Growth via `config.ENROLLMENT` + `VALID_VERSIONS`). If a client's S0 sleeve should carry a
  specific tier, the model would need to encode it too — e.g. `S0_ALLWEATHER_BALANCED` — so
  the model's holdings match that tier's target weights.

The registry supports either (just add rows), but the choice affects how many IBKR models we
create per account and how `weights_by_model` is sourced from the shared strategy brain.
**Recommendation:** stay per-strategy until a real client needs a non-default S0 tier inside
a sleeve; revisit as per-strategy × tier only when that concrete need appears — not
pre-emptively. Flagged here rather than decided.

---

## 7. Not yet built (future, deliberately gated)

- Model creation / target-allocation setup in the IBKR UI (manual, UI-only).
- Live wiring into a runner (fetch per-model NLV/positions → `plan_account_sleeves` →
  `build_model_limit_order` → the arm gate).
- Upstreaming real `positionMulti` handling into ib_async (removes the §3 workaround).
- A parity/what-if probe on the paper FA master confirming a model-tagged order is accepted
  and booked to the right model slice (paper, reversible), before any arm.
