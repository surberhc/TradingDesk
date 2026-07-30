# Session Handoff — Production Rebalance Control Plane (2026-07-30)

*Written for a reader with no memory of this session. Distinct from
`docs/SESSION_HANDOFF_2026-07-30.md` (owned by another session — do not
conflate). This file covers only the Control Plane / execution-engine work.*

---

## 1. The arc

This was a multi-day work session on the **Production Rebalance Control Plane**
(spec: `docs/PRODUCTION_REBALANCE_CONTROL_PLANE.md`; tracked as #64/#66). It
moved through three phases:

1. **Design → live tool.** Took the Control Plane from a design doc to a
   working, live-verified in-app surface: a read-only S0 plan+preview page that
   then walks through the review → arm → execute gate, reusing the hardened
   `s0_live_deploy` executor. Verified live (read-only) against the real-money
   account.
2. **Phase 2 refactor — shared safe-execution engine.** Generalized the
   per-strategy deploy hardening into ONE shared "execute this plan safely on
   this account" primitive (`safe_execute.execute_plan`), unified the arm-gate
   into a single `armed_session` context manager, and consolidated the
   duplicated gateway read-only probe idiom onto one shared
   `connections.gateway_probe` primitive.
3. **Phase 3 start — multi-account (CRM) execution.** Delivered slice 1 (preview
   only): a human-blessed roster + a CRM-plan → engine adapter that previews
   per-account rebalances through the same shared engine. Transmits nothing yet.

The through-line: **the human is always the transmit trigger.** Nothing in any
phase transmits a real order without a deliberate, gated, armed decision.

---

## 2. Commits (chronological)

Each with its one-line purpose.

| Commit | What it did |
|--------|-------------|
| `53542d2` | #67 fix — (Control Plane fix that unblocked the page) |
| `8263bb3` | Read-only Control Plane page (in-app S0 plan + preview surface) |
| `4a57044` | Stage-3 arm/execute wiring on the Control Plane page |
| `1c84e36` | Step-flow (the review → arm → execute step progression UI) |
| `de14185` | Armed-state probe (detect/report gateway armed state) |
| `7632d8c` | Step-1 status fix |
| `29252708` | Month-end notice v1 (dailyreport) |
| `74da848` | **Phase 2** — safe-execution engine extraction, `safe_execute.execute_plan` (paperbot v0.26.0) |
| `5645030` | Arm-gate unification Step 1 — shared `armed_session` context manager; deploy engine adopts it (v0.27.0) |
| `d0ff69f` | Arm-gate unification Step 2 — `rebalance_execute` adopts `armed_session` (v0.27.1) |
| `280bc95` | Probe consolidation — arming(4002) + safe_execute(4003) delegate to `connections.gateway_probe` (v0.28.0) |
| `e70dfc4` | **#70 fold** — s0_live_exec's third probe copy folded onto shared probe (v0.28.1) |
| `58faa2b` | **Phase 3 slice 1** — `roster.py` + `crm_execute.py`, PREVIEW-only multi-account adapter (non-order-affecting) |
| `68266a1` | **Month-end exact-verdict email v2** — Job A snapshot + Job B exact TRADE/NO-TRADE notice (dailyreport) |

Plus the conductor STATUS commits (log/render bookkeeping) interleaved.

---

## 3. The five owner decisions + outcomes

While the owner was out, he had pre-decided five next-items one at a time. Status:

1. **Month-end exact-verdict email** → **BUILT** (`68266a1`). Two jobs (see §5).
2. **Propose-and-arm scheduler (Phase 4)** → **PENDING WALKTHROUGH.** Owner chose
   "walk me through A/B/C, then build" — so it was deliberately NOT built while he
   was out. The A/B/C walkthrough (cadence / what earns a proposal / notify
   channel) is owed to him.
3. **Control Plane execute mechanism** → **keep as a subprocess** (not in-process).
   Decided; no code change needed.
4. **#70 probe fold** → **DONE** (`e70dfc4`).
5. **Phase 3** → **STARTED**; slice 1 (preview-only) done (`58faa2b`).

---

## 4. Account resolution (real-money) — READ THIS

The S0 real-money execution account is **U14438624** (the funded trust account).

- On **2026-07-28** the owner explicitly **retargeted S0 execution to U14438624**.
  The prior individual account U5721712 held ~$957 and is PDT-blocked (margin
  account under $25k), so it could not run the strategy.
- On **2026-07-29** the deploy **conformed U14438624 to S0 Growth** (#62).
- `s0_live_deploy.ALLOWED_ACCOUNT`, the Control Plane page, and the new month-end
  snapshot job **all correctly use U14438624.**

**The landmine:** `paperbot/s0_live.py` (around lines 14-17, 38-40, 64-85) still
carries an emphatic **stale** comment/constant from the pre-retarget pilot era:
it says "U14438624 is S8's trust account, S0 must NEVER touch it" and sets
`S0_LIVE_ACCOUNT=U5721712`, filtering every read to U5721712. That is now the
exact opposite of the truth. Filed as **#71** — reconcile the comment and decide
whether `S0_LIVE_ACCOUNT` should point at U14438624 or whether s0_live.py's
morning-pilot lane is retired outright. **Get owner input on the final intended
account map before flipping the constant** — it's a real-money target.

---

## 5. What fires tomorrow (2026-07-31)

Tomorrow is a signal day, so the month-end exact-verdict pipeline fires:

- **Job A — `s0_month_end_snapshot.py`, ~14:50 CT.** Read-only 4003 snapshot of
  U14438624 (new clientId `s0_month_end_snapshot=60`). Captures the pre-close
  state.
- **Job B — `s0_month_end_notice.py`, 19:15 CT.** Loads the snapshot, sizes the
  plan on the final close, and emails the **exact** verdict: TRADE / NO-TRADE /
  could-not-read.

**Caveats:** both tasks are registered **Interactive** (run only when the user is
logged on) and depend on the **4003 gateway being up** at those times. If the
machine is locked / logged off, or the gateway is down, the jobs will not
produce a verdict. See §6 pending item (4) about making them whether-logged-on.

---

## 6. Pending for owner (top of mind)

1. **Scheduler A/B/C walkthrough** (Phase 4). Owner wants to be walked through
   cadence / what earns a proposal / notify channel before it's built. Owed.
2. **Phase 3 order-affecting steps** — held for review (#72). Two engine changes
   needed to ARM a multi-account/ongoing rebalance:
   - **Decouple conform from the transmit permit.** `execute_plan` hardcodes
     `permit = (armed and conform and ...)`, so an ongoing REBALANCE
     (conform=False) can never transmit. Introduce a DEPLOY-vs-REBALANCE
     *purpose* (DEPLOY still requires conform; defaults preserve S0 exactly;
     version bump + characterization parity test).
   - **Self-computed per-account margin pre-flight** (#57) — pure
     `margin_preflight_ok` over `reqAccountSummary` rows, appended to
     execute_plan's connection-dependent reasons. The official-ibapi FA-block
     whatIf live-test stays a separate owner-gated branch (new non-PyPI dep),
     only when a 2nd funded account joins.
3. **Confirm/repoint s0_live.py account map** (#71) — see §4. Real-money target;
   needs owner's call on the intended final map.
4. **Make the month-end tasks whether-logged-on?** They are currently
   Interactive (§5). Converting to run-whether-logged-on needs the owner's
   **elevated (admin) run of the register `.ps1`** — point-and-click, his action.

**Standing open items** (from the conductor): #3, #12, #57, #65, #66, #68
(de-paper Layer 1), plus the new #71 and #72. Recently closed: #67, #69, #70.

---

## 7. Operational notes

- **Dashboard.** Runs on the **VENV python**, bound to **127.0.0.1:8502**. It
  **must be restarted (not refreshed)** to pick up page-code changes — a browser
  refresh alone will show stale page code. (Do not kill the persistent
  system-python :8502 instance carelessly; verify page changes with AppTest or a
  throwaway port per project convention.)
- **Control Plane live status.** The Control Plane is **live-verified read-only**
  against **U14438624**. That account **already conforms to S0 Growth** (07-29
  deploy), so there is **nothing to trade right now** — a fresh preview will show
  a no-op / no-trade plan. That's expected, not a bug.
- **Zero-transmit posture intact.** Every phase above transmits nothing without
  the deliberate armed decision. Phase 3 slice 1 is preview-only; the
  order-affecting steps that would let a rebalance ARM are held for review (#72).
