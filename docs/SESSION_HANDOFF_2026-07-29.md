# Session Handoff — 2026-07-29 — S0 trust deploy COMPLETE; next mission = the Control Plane

Written for a reader with **no memory** of this session. Everything below reflects what was
actually shipped and committed on 2026-07-29 — not recalled. The desk's two non-negotiables and
the sacred **review → arm → transmit** gate govern everything here.

---

## 1. SHIPPED TODAY

### (a) S0 trust-account deploy — COMPLETE (conductor #62 closed)
The first completed real-money strategy-capital deployment. On **2026-07-29** the remaining sleeve
was bought on **U14438624**: **SELL USFR ×1 + BUY SPY ×44 @ ~$737.72 (~$32,460)**, run through the
two-phase cash-gated executor (realized cash $34,722 vs buy budget $34,375). **Both legs FILLED,
no negative balance.** A post-run read-only preview shows **0 remaining legs** — the account now
conforms to the S0 Growth target. Builds on the 07-28 tiny-test milestone that proved
arm→transmit→fill. Conductor **#62 is closed**; plan doc: `docs/S0_TRUST_ACCOUNT_DEPLOYMENT_PLAN.md`.

### (b) paperbot v0.25.1 — model-clear gate removed
Commit `1f117c6`. Removed the `--model-clear` affirmation gate + IBKR model-overlay detection from
`s0_live_deploy` per the account owner's explicit direction (**the owner manages IBKR Model
Portfolios manually**). **493 paperbot tests pass**; all other deploy safety gates unchanged. The
desktop launcher was updated to `--arm-i-understand --conform`.

### (c) Full desk dashboard rebuild — isolated Streamlit app on :8502
Commits `48463c5` + `55825a1` + `6a49e0b` (the S0 page, committed this session). A new isolated app
(`dashboard/desk/desk_app.py`, `st.navigation`) on **:8502** — the old `app.py` on :8501 is
untouched. **Six pages:** Desk Pulse (3-tile simplified home), Feeds & Connections, History & Event
Log (durable SQLite event store + a `DeskEventLogScan` task), Strategy 0, Strategy 8, and a Research
shelf. A guarded emergency bar provides a real OS-level Halt and an **inert** Flatten scaffold
(zero order-transmit code). Plain-English labels throughout (per the dashboard-labels standard).
The **Strategy 0 page** now leads with a five-rung regime ladder (current rung highlighted, heading
arrow from logged score history) and Today / Month-to-date / Year-to-date performance computed from
the model on EOD data (no gateway), with re-risk gates per version and risk metrics demoted to a
details expander. Read-only monitor; no order-affecting change.

---

## 2. NEXT-SESSION MISSION — build the Production Rebalance / Strategy Control Plane

**Spec (read it first): `docs/PRODUCTION_REBALANCE_CONTROL_PLANE.md` (DRAFT, 2026-07-29).**
Conductor item **#66** tracks this; it relates to **#64** (the shared safe-execution primitive).

The institutional replacement for one-off desktop `.cmd` executors: a **Shared Safe Execution
Engine** plus an **in-app Control Plane** that plans and rebalances real-money accounts from inside
the desk app, **behind the sacred gate**. The single most important finding of the spec: most of the
machinery already exists and is proven — this is an extraction-and-surfacing job, not a rewrite.

### 5 OPEN OWNER DECISIONS — answer these before building
1. **Automation posture** — confirm **propose-and-arm (b)** (schedule computes drift proposals +
   notifies; human still arms/fires) over fully-manual (a). Recommendation: **(b)**.
2. **Margin-gate path (#57)** — approve installing the official `ibapi` (NEW dep, separate
   namespace) to run a cheap live master test, or stay with the self-computed `reqAccountSummary`
   gate?
3. **Roster scope for Phase 3** — which second account enrolls first (triggers #57/#61): a CRM
   blended client account, or another own account?
4. **Second-approver maker-checker** — keep single-operator (maker=checker) for now, or design in a
   distinct second-human approval step before real client capital goes live?
5. **Engine-extraction timing (#64)** — ship Phase 1 on the **unchanged** `s0_live_deploy` first
   (recommended), or extract the shared engine before building the app surface?

### Phase 1 (the first slice)
In-app S0 rebalance for **S0 × U14438624**: a **read-only** Drift + Plan Preview (the deployment
plan's step 6.2 preview, surfaced in-app), then an **ARM affordance + EXECUTE** that invokes the
**existing, already-hardened `s0_live_deploy` executor unchanged**, behind the typed-confirm +
physically-armed-gateway probe — the review → arm → transmit gate. Also fix the broken
`emergency._emit` audit call (see #67) and route control-plane events through
`eventlog.record_event` with its real signature. A missing key ⇒ preview only; zero change to the
executor's proven behavior.

---

## 3. KEY FILE POINTERS

- `paperbot/order_router.py` — the **single transmit chokepoint** (fail-closed `transmit_guard`,
  hard price guard, order builders, `place`/`place_laddered`, broker-truth `already_present` dedup,
  deterministic order-ref). The engine is built ON this, not a rewrite of it.
- `paperbot/s0_live_deploy.py` — the **hardened reference executor** (v0.25.1): two-phase
  cash-gated, sells-before-buys, straggler re-price, per-run ref, single-account wall, notional
  caps, gateway read-only probe, DRY-RUN default.
- `paperbot/rebalance_engine.py` + `paperbot/strategy_target.py` — the **pure planning brain**
  (signed integer deltas, no-trade band, ALIEN guard, investable; targets from the shared backtester
  brain, stale-data guarded). No broker, no transmission.
- `crm/` package + `paperbot/crm_rebalance.py` — the **multi-account brain** (Option A: blended
  account + FA Account Groups + our sleeve ledger; the what-if bridge). Multi-account planning
  already exists; what it lacks is a safe execution engine underneath and an app surface on top.
- `dashboard/desk/` — the app. Add a **Control Plane page** via the `st.Page`/`st.navigation`
  pattern in `desk_app.py`; reuse `theme.py` tokens + plain-English labels.
- `dashboard/desk/eventlog.py` — the **durable SQLite audit store** (`record_event(ts, source,
  category, message, ...)`), read by the History page.
- `dashboard/desk/emergency.py` — the **typed-confirm / arm gate model** to reuse for the ARM
  affordance — AND the location of the **broken `_emit`** to fix (#67): none of its four calling
  conventions match `record_event`'s signature, so emergency Halt/Flatten actions currently log
  nothing.

Reference specs: `docs/SAFE_EXECUTION_CONTRACT.md` (the checklist every order path must enforce,
conductor #64) and `docs/PRODUCTION_REBALANCE_CONTROL_PLANE.md` (this mission).

---

## 4. GUARDRAILS REMINDER

- **The human is ALWAYS the transmit trigger.** The app prepares, plans, previews, arms-affordances,
  monitors, and audits; a real order only ever transmits on the operator's deliberate, explicit,
  armed act. Never AI-initiated, never automatic, never on a schedule.
- **review → arm → transmit stays sacred** — typed confirm PLUS a physically write-enabled gateway
  (the Read-Only API toggle is a human act in TWS/IBC; the app only *measures* it, never sets it).
- **The owner manages IBKR Model Portfolios manually** — there is no model-clear gate anymore (removed
  2026-07-29); do not re-add one.
- **Rule #1: strategy / regime / band / sizing config is FROZEN** — no tuning without Andrew's
  explicit blessing; the control plane never edits frozen config.

---

## UPDATE — later 2026-07-29 — Control Plane Phase 1 started (read-only shipped; arm/execute parked)

### The four owner decisions (conductor #66)
1. **Automation posture = propose-and-arm (option b).** Owner wants to walk through the proposal
   mechanics before any scheduler is built. Agreed so far: the actionable S0 rebalance email is
   **monthly**, sent the evening of the month-end signal date, with a concise subject —
   `S0: TRADE tomorrow` / `S0: NO trade tomorrow`. Idle-cash and deposit/withdrawal alerts are
   handled separately as low-key in-app inbox notices, not emails.
2. **Build Phase 1 on the UNCHANGED `s0_live_deploy` first.** Extract a shared execution engine
   only in Phase 2 — no refactor of the hardened deploy path yet.
3. **Maker-checker = single-operator for now**, but designed so a distinct second approver can slot
   in *before* any real CLIENT capital is managed.
4. **Phase 3 (margin gate #57, second account) DEFERRED to trigger.** Stay self-computed on margin,
   no official ibapi margin call yet; the next account added will be another OWN account before any
   client account.

### Propose-and-arm monthly-email design — still-open points
The monthly email is the proposal surface under the propose-and-arm posture. Design agreed above.
**Still open with owner (A/B/C/D):**
- **(A) proposal cadence** — beyond the monthly signal, when else (if ever) does a proposal fire?
- **(B) what earns a proposal** — the trigger conditions that justify surfacing one.
- **(C) notify channel** — email vs in-app inbox vs both, per proposal type.
- **(D) freshness policy** — leaning advisory-only / recompute-live-at-arm, still to confirm.

### Cadence finding (verified in code)
Live S0 is **pure-monthly**. Path: `strategy_target.current_target` → `run_backtest` with
`adaptive_fast_regimes` left unset → `REBALANCE_FREQUENCY='monthly'`. Signal is taken on the last
trading day of the month; the trade is placed T+1. The regime-adaptive "weekly-while-de-risked" mode
exists in `all_weather.py` but is **NOT wired into the live path**.

### Conductor #67 CLOSED
`emergency._emit` audit-logging bug fixed (commit **53542d2**). It had guessed four call shapes that
all mismatched `eventlog.record_event(ts, source, category, message, severity)`; it now builds a
plain-English sentence and calls `record_event` correctly. `test_emergency_emit.py` (4 tests) locks
the regression by asserting an event actually persists (before, zero did). Monitoring only; no
version bump.

### Read-only Control Plane page SHIPPED (commit 8263bb3)
`dashboard/desk/page_control_plane.py`. What it **does**:
- Broker-free **S0 Growth target panel** (computes the current target without touching the gateway).
- On-demand **rebalance PREVIEW** that runs `s0_live_deploy` in preview mode (no arm/conform flags,
  transmits nothing) and shows the resulting leg list.
- Registered in `desk_app.py` nav; the Streamlit `AppTest` render check passes.

What it **does NOT** do: no arm control, no execute control, nothing that can transmit.

### STAGE 3 PARKED — arm/execute wiring (do NOT build unattended)
This is the first in-app transmit trigger and is parked pending owner review. Exact intended design:
- An **ARM affordance** — typed confirmation plus a *measured* gateway read-only probe (the app
  only measures the Read-Only API state, it never sets it).
- An **EXECUTE** action that subprocess-invokes the **unchanged** `s0_live_deploy` with
  `--arm-i-understand --conform` (mirrors the existing desktop launcher exactly).
- Both sit behind the sacred **review → arm → transmit** gate.

### Follow-ups
- **(a) De-paper Layer 1** (conductor #68, area dashboard): remove user-facing "paper" language where
  it does NOT mean the real IBKR paper account (DU/DF…141, port 4002). Concrete misleading item — the
  home Pulse tile reads "Strategy 0 — Paper only — real-money OFF", now STALE/incorrect after the
  2026-07-29 trust-account real-money deploy (source likely `dashboard/desk/deskdata.py`). Layers 2
  (operational identifiers) and 3 (folder rename + out-of-repo Task Scheduler / shortcut audit) are
  deferred. Must NOT touch `connections/ibkr_paper.py`, `PAPER_PORT=4002`, the paper gateway
  watchdog, or `is_paper` checks — those correctly mean the real paper account.
- **(b) The :8502 desk app currently running is an OLDER process** (system python, bound to 0.0.0.0)
  started before this Control Plane page existed. It serves current on-disk code to fresh browser
  sessions, but a **restart is the safe way to guarantee the new page shows**. It was deliberately
  **NOT killed** — killing it would leave the owner with no dashboard.
- **(c)** The `launch.json` desk-dashboard(8502) config exists on disk but is **gitignored**
  (`.claude/` is ignored), so it is not version-controlled.
