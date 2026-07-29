# Production Strategy & Rebalance Control Plane

**Status: DRAFT proposal for review — dated 2026-07-29.**
Nothing in this document arms, transmits, schedules, or authorizes any real-money order. It is a
design/scaffold spec. No executor or UI code is written here. The desk's two non-negotiables and the
sacred **review → arm → transmit** gate govern everything below.

> **Sacred design principle (repeated throughout, load-bearing):** the human is ALWAYS the transmit
> trigger. The app *prepares, plans, previews, arms-affordances, monitors, and audits* — a real-money
> order only ever transmits on the operator's deliberate, explicit, armed action (typed confirm **plus**
> a physically write-enabled gateway). Never AI-initiated. Never automatic. Never on a schedule. The
> control plane is institutional maker-checker built AROUND that gate, never a way to bypass it.

---

## 0. Why this exists

Today, deploying/rebalancing a real-money account means running a one-off desktop `.cmd` that launches a
bespoke Python executor (`s0_live_deploy.py`) pinned to a single account. That worked for the first S0
trust deploy (U14438624, conductor #62), and the executor is now genuinely hardened (two-phase cash-gated,
straggler re-price, per-run ref — paperbot v0.25.1). But it does not scale: every new strategy or account
would need another bespoke click-tool, each re-deriving the same safety checklist, each logging only to a
transient console window. The [Safe Execution Contract](SAFE_EXECUTION_CONTRACT.md) (2026-07-28) already
declared the fix: **one shared safe-execute layer, not per-strategy click-tools** (conductor #64).

This document specifies the institutional replacement: a **Shared Safe Execution Engine** plus an **in-app
Control Plane** that manages strategies and rebalances real-money accounts from inside the Trading Desk
app, behind the sacred gate.

---

## 1. What already exists (the honest inventory)

The single most important finding: **most of the machinery already exists and is proven.** This is an
extraction-and-surfacing job, not a rewrite.

### 1.1 The one transmit chokepoint — `paperbot/order_router.py`
Every real order the desk can send passes through `order_router`. This is the wall the engine is built ON.
- `transmit_guard(armed)` — fails **closed**: refuses unless `armed` AND `not config.READONLY` AND
  `not config.DRY_RUN`. The committed desk-wide default is READONLY/DRY_RUN = True, so nothing transmits
  unless a caller deliberately flips both in-process behind the gate.
- `_check_limit_price(...)` — the HARD fat-finger price guard (limit must sit within a sane band of the
  quote); rejects, never clamps silently.
- `build_marketable_limit(...)` / `build_fa_block(...)` — order builders (marketable LIMIT only; never a
  market order). Whole-share.
- `place(...)` / `place_laddered(...)` — the actual `ib.placeOrder`, laddered rungs, transmit=True flip.
- `already_present(...)` — **broker-truth** idempotency: reads `reqAllOpenOrders` working refs +
  `reqExecutions` today's fills per orderRef; classifies FRESH/WORKING/COMPLETE/PARTIAL/UNKNOWN; only FRESH
  transmits; fails CLOSED (any read error → UNKNOWN → skip).
- `_order_ref(account, as_of, side, symbol)` — the deterministic order-ref format.
- `transmit_journal.py` — append-only per-leg ATTEMPTING/SENT/CYCLE_COMPLETE tripwire (off-Drive
  `STATE_DIR`) for the crash-between-place-and-confirm window.

### 1.2 The planning brain — pure, no broker, already multi-account
- `rebalance_engine.plan_account(account, version, net_liq, positions, target, prices, universe)` →
  `AccountPlan` with `.orders` (signed integer deltas), `.alien_lines` (non-universe holdings the ALIEN
  guard refuses to auto-trade), `.investable`, plus the no-trade band and reconcile. **Pure**: no broker
  connection, no transmission.
- `strategy_target.current_target(version=...)` → `Target` (weights, prices, as_of, price_date) from the
  shared backtester brain, stale-data guarded.
- The whole `crm/` package — SQLite-backed **Option A** multi-account world
  (`docs/CRM_DESIGN_groups_brain.md`): `domain`, `sleeve_ledger`, `capability`, `latch`, `brain`, `store`,
  plus the `crm_rebalance.py` what-if bridge. **Multi-account planning already exists** — it is what-if /
  read-only today. What it lacks is (a) a safe execution engine underneath and (b) an app control surface
  on top. Both are what this spec adds.

### 1.3 The proven execution guarantees — `paperbot/s0_live_deploy.py` (v0.25.1)
The reference implementation of the contract's execution discipline, all of which becomes the shared
engine: two-phase cash-gated (buys ≤ realized `TotalCashValue`, re-read live between phases — see
`s0_live_deploy.py:817-878`), sells-before-buys ordering (`build_deploy_legs`, `:388-389`), per-run order
ref (`_deploy_ref`/`_run_id`, `:171-184`), single-account wall (`_account_safety_ok`, `:204-211`),
inline gateway read-only arming probe (`_probe_gateway_readonly`, `:214-253`), per-order notional cap
≤50% NetLiq + total-buy ≤ investable (`:778-788`), whole-share scaling to realized cash
(`_scale_buys_to_cash`, `:393-435`), straggler re-price/chase (`_transmit_phase`, `:529-564`),
kill-switch sentinel (`:199-201`), DRY-RUN/preview default (`main`, `:628`), fail-closed batch semantics.

### 1.4 The audit stores (two, today disjoint — a key design point)
- `paperbot/ledger.py` — append-only per-RUN JSONL (`runs.jsonl`) + human line (`paperbot.log`) under
  off-Drive `STATE_DIR`. `record_run(record)`.
- `dashboard/desk/eventlog.py` — the **durable in-app SQLite** event store the History page reads;
  `record_event(ts, source, category, message, ...)`. This is the app-facing audit trail. **Finding: the
  desk `emergency._emit` audit call is silently broken — none of its four calling conventions match
  `record_event`'s real signature, so emergency actions are NOT being logged.** The control plane must call
  `record_event` with its real signature, and this bug should be fixed as part of Phase 1.

### 1.5 The app it plugs into — `dashboard/desk/`
Isolated Streamlit app on :8502 (`desk_app.py`, `st.navigation`). Six pages today (Pulse, Feeds,
History/EventLog, Strategy 0, Strategy 8, Research). `emergency.py` is the only "action" surface — a
persistent guarded bar with a real OS-level Halt and an **inert** Flatten scaffold (zero order-transmit
code). **The app has NO order-transmit path today.** The control plane is the *first* — and it must live
behind the typed-confirm/arm affordance modeled on `emergency.py`. `theme.py` supplies the reusable tokens
and components; the new page reuses them.

---

## 2. Deliverable 1 — The Shared Safe Execution Engine

### 2.1 Goal
Generalize `s0_live_deploy`'s guarantees into ONE reusable primitive that deploy, ongoing rebalance, and
every future strategy/account call. `s0_live_deploy` proved the checks in one bespoke file; the engine
promotes them into the shared layer (conductor #64) so every caller inherits them and no future click-tool
re-derives them. **It is built ON `order_router`, not a rewrite of it.**

### 2.2 Two things to unify first (they diverge today)
1. **Two divergent arm-gate implementations.** Both use the same `--arm-i-understand` token but differ:
   - `rebalance_execute` — flip-and-**leave** (sets config flags for the session), acquires `gateway_lock`,
     runs `risk_manager`, and takes an FA `replaceFA` XML backup before touching FA config.
   - `s0_live_deploy` / `s0_live_exec` — flip-and-**restore-in-finally** (in-process, never leaks past the
     transmit), single-account wall, notional caps, inline gateway read-only probe.
   The engine adopts the **flip-and-restore-in-finally** discipline (strictly safer — the enablement can
   never outlive the batch) and folds in `rebalance_execute`'s `gateway_lock` + FA XML backup for the FA
   path. One arm gate, one token, one code path.
2. **Two duplicated gateway read-only probes hardcoded to different ports** — `arming.py` (4002) vs
   `s0_live_exec` (4003). Consolidate into ONE **port-parameterized** probe
   (`probe_api_readonly(ib, port=...)`) using the zero-transmission cancel-a-fabricated-order technique,
   fail-closed (no decisive signal → treat as read-only → refuse).

### 2.3 The engine API/contract
A single module (proposed `paperbot/safe_execute.py`) exposing one primitive:

```
execute_plan(plan_request, *, mode) -> ExecutionResult
```

**`ExecutionРequest` (inputs) — pure, broker-agnostic:**
| field | meaning |
|---|---|
| `account` | target account id |
| `strategy_version` | e.g. "Growth" / "Balanced" (frozen config, rule #1) |
| `plan` | an `AccountPlan` from `rebalance_engine.plan_account` (signed integer deltas + alien_lines) |
| `target` | the `Target` (for reconcile + weights) |
| `quotes` / `prices` | fresh, sane market data (stale/None/zero rejected at build) |
| `allowed_accounts` | the account wall — single id or an allow-list/roster; ANY other refused |
| `caps` | `per_order_notional_pct_nlv`, `total_buy_le_investable`, `max_total_notional` |
| `conform` | opt-in: turn ALIEN holdings into liquidation sells (deploy only; default off) |
| `run_id` | per-run stamp (idempotency namespace) |

**`mode` (states):** `PREVIEW` (default; sizes, builds the ordered leg list, transmits nothing) →
`ARMED` (transmit only if the full gate passes). There is no third auto mode.

**Pre-flight gate (contract §A — every item, fail-closed, collect ALL reasons before deciding):**
arm token present · READONLY=False AND DRY_RUN=False AND armed AND gateway physically write-enabled
(port-parameterized read-only probe) AND kill-switch absent · account ∈ `allowed_accounts` · fresh+sane
quotes · fresh target (stale-data guard) · real cash/BuyingPower read live · whole-share · per-order
notional ≤ cap · total ≤ investable/available cash · price guard (`_check_limit_price`) · **position-based
idempotency** (delta-vs-current-positions is the source of truth; `already_present` blocks only a currently
WORKING/open duplicate, never a leg merely because an identical order filled earlier). **No IBKR
model-clear gate** — removed 2026-07-29; the owner manages IBKR Model Portfolios manually.

**Execution discipline (contract §B):** two-phase cash-gated (sells → re-read realized `TotalCashValue` →
buys sized to that cash, hard invariant `buy_notional ≤ cash*(1-buffer)`) · sells-before-buys · fill
confirm + bounded straggler re-price/chase · fail-closed on cancelled/rejected/ambiguous (halt the batch,
never plow into the next phase) · serialized gateway access (`gateway_lock`).

**Post (contract §C):** reconcile resulting positions to target + report residual/unfilled loudly · write
the DURABLE audit trail (both stores — see §4) · disarm (flip flags back in finally; recommend gateway
back to read-only) + exposure sanity (account not negative / over-levered).

**`ExecutionResult` (outputs):** ordered leg list (as previewed) · per-leg results
(requested/filled/status/reprices/skipped/reason) · realized-cash reading · reconcile residual · a
stable `run_id` and the exact audit records emitted · a single terminal status
(`PREVIEW_ONLY` / `COMPLETE` / `PARTIAL_LOUD` / `BLOCKED{reasons}`).

### 2.4 Reuse-vs-new map (module level) — see also §7
| Concern | Becomes shared engine | Stays caller-specific |
|---|---|---|
| transmit guard, price guard, builders, place/ladder, broker-truth dedup, order-ref | `order_router` (already shared — reuse) | — |
| planning (deltas, band, ALIEN, investable), targets | `rebalance_engine`, `strategy_target` (reuse) | — |
| two-phase cash-gate, sells-first, scale-to-cash, straggler re-price, fail-closed | **NEW: `safe_execute.execute_plan`** (extracted from `s0_live_deploy`) | — |
| arm token + flip-and-restore + gateway_lock + FA XML backup | **NEW: one unified arm gate** in the engine | — |
| gateway read-only probe | **NEW: one port-parameterized probe** (consolidate `arming`+`s0_live_exec`) | — |
| account wall / caps / conform | engine inputs | the *values* per caller |
| durable audit | engine writes both `ledger.record_run` + `eventlog.record_event` | — |
| `s0_live_deploy.py` | retires to a **thin trigger** that builds the request and calls the engine | connect/account-pin only |

---

## 3. Deliverable 2 — The in-app Control Plane

### 3.1 The operator flow (per strategy × account)
A strict, linear, mostly-read-only pipeline; only the final step transmits, and only on the human's armed
act:

```
  TARGET vs CURRENT (drift)   → read-only
        │
     BUILD PLAN               → read-only preview (engine PREVIEW mode)
        │
      REVIEW                  → read-only; operator inspects every leg, totals, reconcile
        │
       ARM                    → deliberate: typed confirm + physically-armed-gateway probe
        │
     EXECUTE                  → engine ARMED mode (the human is the trigger)
        │
   MONITOR (live fills)       → read-only stream of per-leg fills/reprices
        │
      AUDIT                   → immutable record of the whole run
```

### 3.2 New dashboard surface (fits the existing `st.navigation` app)
A new page **"Control Plane"** (proposed `dashboard/desk/page_control_plane.py`), registered exactly like
the other pages in `desk_app.py`'s `st.navigation`. Reuses `theme.py` tokens/components and plain-English
labels (per the dashboard-labels standard — no vague "OK"/"warning"). Components:

- **Strategy × Account selector** (read-only) — pick a strategy version + an enrolled account.
- **Drift panel** (read-only) — target weights vs current holdings, per-symbol drift, band status. Sourced
  from `rebalance_engine` + `strategy_target`; identical brain to the executor.
- **Plan preview** (read-only) — the full ordered leg list the engine would build in PREVIEW mode: sells
  first then buys, whole-share, priced, with per-order notional, totals, investable, reconcile-to-target,
  and every ALIEN/unpriceable/blocking reason. This IS the "read-only full-plan preview" the S0 deployment
  plan step 6.2 already calls for.
- **The ARM affordance** (the only action control) — modeled on `emergency.py`'s guarded pattern:
  1. a hard-to-fat-finger **typed confirmation** (operator types the account id or a fixed phrase, not a
     single click),
  2. a live **gateway-armed probe** result shown inline (read-only API OFF = physically armed) — the app
     does not and cannot flip the gateway toggle; that is the operator's physical act in TWS/IBC,
  3. the exact preview hash/summary the operator is arming AGAINST, so arm binds to the reviewed plan,
  4. an explicit **EXECUTE** button that is inert until (1)+(2)+(3) are all satisfied.
- **Live monitor** (read-only) — per-leg fill/reprice/skip status streaming from the engine's result,
  loud on anything unfilled/partial.
- **Audit panel** (read-only) — the immutable run record from the event log.

**Read-only vs action:** everything is read-only except the single EXECUTE control, which is inert unless
the typed confirm + armed-gateway probe + plan-binding all pass. Preview requires no arming and transmits
nothing.

### 3.3 Keeping arming deliberate
The typed-confirm + physically-armed-gateway two-key requirement means the app alone can NEVER transmit:
the gateway write-enable is a physical act outside the app (TWS/IBC Read-Only API toggle), measured — never
set — by the app via the read-only probe. Miss either key and EXECUTE stays inert and the run is a preview.

---

## 4. Deliverable 3 — Governance & safety model

- **Maker-checker.** Maker = the desk/engine that *computes and proposes* the plan (deterministic, from the
  frozen brain). Checker = the human operator who *reviews and arms*. The two roles are structurally
  separate: the maker can never arm; the checker never edits the frozen strategy. (Single-operator desk
  today; the pattern scales to a distinct second approver later without redesign — see §5.)
- **Explicit typed arm** — `--arm-i-understand` semantics surfaced as an in-app typed confirmation, never a
  defaulted/remembered state.
- **Physically-armed-gateway probe** — the port-parameterized read-only probe; fail-closed.
- **Per-account wall + caps** — allow-list of enrolled accounts; per-order ≤ %NLV; total ≤ investable/cash.
- **Dry-run default** — PREVIEW is the default mode; transmit is the exception requiring every gate.
- **Kill switch** — the `AUTOTRADE_DISABLED` sentinel + the desk `kill_switch.py`; present → preview-only.
- **Complete immutable audit trail** — every plan / arm / fire / fill / error → `eventlog.record_event`
  (in-app, using its REAL signature — and fixing the broken `emergency._emit`) AND `ledger.record_run`
  (off-Drive JSONL). Each record captures **who / what / when / the exact preview armed against / the
  fills / any error**. Append-only; never rewritten.
- **Idempotent + reconcilable** — position-based idempotency (re-run completes only the remaining gap) +
  broker-truth `already_present` + the transmit journal tripwire; post-run reconcile to target.
- **Clean disarm/rollback** — flip-and-restore-in-finally guarantees the enablement never outlives the
  batch; recommend gateway back to read-only after; a partial/failed run leaves a loud, reconcilable state
  (never a silent hole).

### 4.1 What must NEVER be automated (enumerated, non-negotiable)
1. The **transmit trigger** — a real order only ever fires on the operator's deliberate armed act.
2. **Arming** — neither the typed confirm nor the gateway write-enable may be defaulted, remembered,
   auto-checked, or set by the app/AI.
3. The **gateway Read-Only toggle** — a physical human act in TWS/IBC; the app only *measures* it.
4. **Flipping READONLY/DRY_RUN** to False anywhere except in-process, behind a passed gate, restored in a
   finally.
5. **Editing frozen strategy/regime/band/sizing config** (rule #1) — out of scope for the control plane
   entirely.
6. **Scheduling any transmit.** No cron, no task, no timer may fire an order. (Proposals may be *computed*
   on a schedule — §5 — but never armed or fired by one.)

---

## 5. Deliverable 4 — Automation posture

Two options, both keeping the human as the SOLE trigger:

**(a) Fully manual.** The operator opens the Control Plane, builds a plan, reviews, arms, fires. Nothing is
computed until the operator asks. Simplest; zero background moving parts. Cost: drift can go unnoticed
between sessions; the operator must remember to check.

**(b) Propose-and-arm (RECOMMENDED).** The desk computes drift/rebalance **proposals** on a schedule (reusing
the existing `nightly_monitor_run` band-breach logic + `crm_rebalance` what-if) and **notifies** (email +
an in-app "Proposals" inbox on the Control Plane page). The operator reviews the surfaced proposal, and
only then arms and fires. The scheduler computes and notifies; it **never** arms, never flips a flag, never
transmits. This is exactly the boundary the existing `PILOT_MODE`/`WOULD HAVE TRANSMITTED` pilot already
respects.

**Recommendation: (b).** It removes the "did I forget to check drift?" failure mode without touching the
gate — the human still does the exact same review → arm → transmit. It reuses machinery that already exists
(the nightly monitor already stages band-breached route lists; it just needs to surface them as in-app
proposals instead of only email). **How proposals surface:** a read-only "Proposals" list at the top of the
Control Plane page — each row is a strategy×account with a band-breach reason and a "Review" button that
opens the exact same read-only Plan Preview flow. Nothing about a proposal is armed or fired until the
operator drives the manual arm sequence.

---

## 6. Deliverable 5 — Multi-account / multi-strategy scaling

From today's single U14438624 S0 deploy to N strategies across the CRM FA-groups world, **with no
per-account bespoke scripts**:

- **The engine is account-agnostic.** `execute_plan` takes `account` + `allowed_accounts` as inputs; the
  single-account wall generalizes to an enrolled-roster allow-list. The same primitive serves account 1 or
  account N.
- **Planning already scales.** `crm/` (Option A: one blended account per client + FA Account Groups + our
  own sleeve ledger) already produces per-account plans; `crm_rebalance.py` is the what-if bridge. Wiring
  it to the engine gives multi-account execution behind the same gate.
- **Per-account margin gate (#57) — HONEST GAP.** There is no broker margin-preview for FA block orders
  today: `ib_async` advertises `MaxClientVersion 178`, below the **195** the `orderAllocations` preview
  array needs. Until resolved, the per-subaccount margin check must be **self-computed from
  `reqAccountSummary`** (CRM design §6 step 6), OR the official-`ibapi` master live-test (#57) must confirm
  the preview array populates for block what-ifs — a NEW dependency to flag to the owner, run as a cheap
  live test FIRST, build only if it passes. Either way the gate is an engine pre-flight input, not a
  bespoke per-account script.
- **Sleeve ledger + account_monitor (#61).** The CRM `sleeve_ledger` tracks per-sleeve holdings within a
  blended account; `account_monitor`'s cashflow ruleset folds into the CRM per-account read→decide→latch
  loop when that loop is built. The control plane consumes their output (drift, coverage, capacity) as
  read-only plan inputs — it does not re-implement them.
- **Control plane scales by iteration, not duplication.** The Strategy×Account selector enumerates enrolled
  accounts; each runs the identical flow through the identical engine. Adding a strategy = adding a frozen
  config version; adding an account = enrolling it in the roster. No new click-tool, ever.

---

## 7. Deliverable 6 — Phased build plan

Smallest-robust-slice first; each phase ships behind the gate and reuses the proven layer.

### Phase 1 — In-app S0 plan + preview (read-only), then arm/execute reusing the existing executor
- **Scope:** Add the Control Plane page with the read-only Drift + Plan Preview for S0 × U14438624 (the
  deployment plan's step 6.2 preview, in-app). Wire the ARM affordance + EXECUTE to invoke the **existing,
  already-hardened `s0_live_deploy` executor unchanged** behind the typed-confirm/armed-gateway gate. Fix
  the broken `emergency._emit` audit call and route control-plane events through `eventlog.record_event`
  with its real signature.
- **Reuse:** `s0_live_deploy` (v0.25.1, as-is), `rebalance_engine`, `strategy_target`, `order_router`,
  `theme.py`, `eventlog.py`.
- **New:** the page + the ARM/EXECUTE affordance + the audit wiring + the `_emit` fix.
- **Exit criteria:** operator can, in-app, preview the exact S0 leg list read-only; arm via typed confirm +
  measured armed gateway; fire the existing executor; watch fills; see the immutable audit record. A
  missing key ⇒ preview only. Zero change to the executor's proven behavior.
- **Tests:** page renders read-only with no broker/no arm; EXECUTE inert unless both keys + plan-binding
  pass; `record_event` receives correct fields; `_emit` regression test; no transmit in PREVIEW.

### Phase 2 — Extract the Shared Safe Execution Engine (#64)
- **Scope:** Promote `s0_live_deploy`'s guarantees into `paperbot/safe_execute.execute_plan`; unify the two
  arm gates and the two gateway probes (§2.2); retire `s0_live_deploy` to a thin trigger that calls the
  engine. Control plane calls the engine directly.
- **Reuse:** everything in `order_router` + the planning brain; the executor's logic (moved, not rewritten).
- **New:** `safe_execute.py`, the unified arm gate, the port-parameterized probe, engine audit writes.
- **Exit criteria:** the engine passes the whole Safe Execution Contract §A–C; `s0_live_deploy` behavior is
  reproduced bit-for-bit through the engine (characterization tests diff old vs new leg lists + gate
  outcomes); paperbot suite green; version bump (order-affecting).
- **Tests:** two-phase cash-gate invariant; sells-before-buys; straggler re-price; fail-closed on cancel;
  account wall; caps; idempotency (re-run completes gap, doesn't double-send); probe fail-closed;
  arm-restore-in-finally; full parity vs `s0_live_deploy`.

### Phase 3 — Multi-account / CRM + self-computed per-account margin gate (#57)
- **Scope:** Wire `crm_rebalance` → the engine; generalize the account wall to the enrolled roster; add the
  **self-computed** per-account margin gate (`reqAccountSummary`) as an engine pre-flight (and run the #57
  official-`ibapi` live test to decide if a real broker preview is available). Surface N accounts in the
  selector.
- **Reuse:** `crm/` package, `sleeve_ledger`, engine from Phase 2.
- **New:** roster wall, margin-gate pre-flight, CRM→engine adapter.
- **Exit criteria:** two enrolled accounts each plan+preview+arm+execute through the identical engine;
  margin gate refuses a subaccount that can't absorb its slice; crm + paperbot suites green.
- **Tests:** roster allow/deny; per-account margin refuse; multi-account reconcile; sleeve-ledger
  consistency; the #57 live-test PASS/FAIL branch.

### Phase 4 — Propose-and-arm scheduler
- **Scope:** Schedule the drift/proposal computation (reuse `nightly_monitor_run` + `crm_rebalance`
  what-if); surface proposals in the in-app inbox + email. **Compute + notify only — never arm/fire.**
- **Reuse:** existing nightly monitor staging, mailer, the Control Plane review flow.
- **New:** the proposals inbox component + the notify wiring.
- **Exit criteria:** a band-breach proposal appears in-app and by email; opening it lands in the identical
  read-only preview; no proposal can arm or transmit anything; kill-switch respected.
- **Tests:** scheduler stages a proposal and transmits nothing; proposal → preview binding; scheduler never
  flips a flag / never calls place().

---

## 8. Deliverable 7 — Reuse-vs-new map (consolidated)

| Layer | Reuse as-is | Extend | Build new |
|---|---|---|---|
| Transmit chokepoint | `order_router` (guard, price guard, builders, place/ladder, dedup, ref) | — | — |
| Planning brain | `rebalance_engine`, `strategy_target`, `crm/` (domain/sleeve_ledger/capability/latch/brain/store), `crm_rebalance` | — | — |
| Execution discipline | logic inside `s0_live_deploy` (moved) | — | `safe_execute.execute_plan` |
| Arm gate | `--arm-i-understand` token, `rebalance_execute` gateway_lock + FA XML backup | unify with the flip-and-restore-in-finally discipline | one unified gate |
| Gateway probe | the zero-transmit cancel technique | port-parameterize | one probe (retire the 4002/4003 duplicates in `arming`+`s0_live_exec`) |
| Audit | `ledger.record_run`, `transmit_journal`, `eventlog.record_event` | fix broken `emergency._emit` | engine audit writes to both stores |
| App | `desk_app.py` nav, `theme.py`, `emergency.py` typed-confirm pattern | — | `page_control_plane.py` + ARM affordance + proposals inbox |
| Margin gate (#57) | `reqAccountSummary` self-compute (CRM §6) | official-`ibapi` live test | self-computed pre-flight |

---

## 9. Deliverable 8 — Risks + open decisions

### Risks
- **The engine touches the one transmit chokepoint.** Extraction (Phase 2) is order-affecting; the mitigant
  is strict characterization parity vs `s0_live_deploy` + version bump + full suite, and shipping Phase 1
  on the *unchanged* executor first so the app surface is proven before the engine is refactored.
- **`ib_async` MaxClientVersion 178 < 195** blocks a real FA-block margin preview → the #57 gate is
  self-computed until the official-`ibapi` live test says otherwise. Self-computed margin can be wrong at
  the edges; treat it as a conservative refuse-if-uncertain gate.
- **Shared-checkout / concurrent sessions** (#45) — the app writes only the event-log DB and off-Drive
  state; keep it that way so the control plane never races another lane on repo files. Gateway access is
  serialized by `gateway_lock`.
- **Broken audit today** (`emergency._emit`) proves audit wiring can silently fail — every engine audit
  write needs a test asserting the record actually lands with the real signature.
- **Single-operator maker-checker** — today maker and checker are the same person; the separation is
  structural, not enforced by a second human. Acceptable for a one-owner desk, but state it plainly.

### Open decisions for the owner (3–5)
1. **Automation posture** — confirm **propose-and-arm (b)** over fully-manual (a). (Recommendation: b.)
2. **Margin gate path (#57)** — approve installing the official `ibapi` (a NEW dependency, separate
   namespace) to run the cheap live master test, or stay with the self-computed `reqAccountSummary` gate?
3. **Roster scope for Phase 3** — which second account enrolls first (the trigger for #57/#61 work), and is
   it a CRM blended client account or another own account?
4. **Second-approver maker-checker** — keep single-operator (maker=checker) for now, or design in a distinct
   second-human approval step before real client capital goes live?
5. **Engine extraction timing (#64)** — ship Phase 1 on the unchanged `s0_live_deploy` first (recommended),
   or extract the shared engine before building the app surface? (Recommendation: Phase 1 first.)

---

*End of DRAFT. No code written; no trading state, gateway, or order touched — read-only design only.*
