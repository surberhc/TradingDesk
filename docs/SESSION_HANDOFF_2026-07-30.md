# Session Handoff — 2026-07-30 — S8 collector red-flag fixed; Control Plane target panel now shows the cash reserve

Written for a reader with no memory of this session. Two small, completed pieces today; the larger Control Plane mission from 2026-07-29 is UNCHANGED and still the active next mission (see §3 and `docs/SESSION_HANDOFF_2026-07-29.md`).

## 1. SHIPPED TODAY

### (a) S8 market-data collector — pre-open red-flag fixed at the root
`S8Collector_Session` (Task Scheduler) showed a red last-run result `0x2` all day. Root cause (verified in the day log `C:\TradingDesk-Local\s8_pilot\logs\s8_collector_20260730.log`): the task starts **08:06 CT**, 24 min before the **08:30 CT** open; the collector's startup data-wait was only 600s (~10 min), so it gave up at ~08:16 with **rc=2** — its by-design "restart me" code — while the market was still closed. Because the successful all-day instance does not post a result until teardown, the most-recent COMPLETED result stayed rc=2, so Task Scheduler painted it red all day. It is NOT a broken path.

Fix: `livebot/s8_collector.py` — `STARTUP_DATA_WAIT_SECS` 600.0 → **2400.0** (~40 min). A single 08:06 launch now stays connected and waits through the open, latches the first valid SPX spot ~08:30, and runs green all day — no pre-open rc=2. The data-wait is DOWNSTREAM of the `s8_startup` connect-retry, so a genuinely dead gateway is still caught fast on the separate connect path; the longer window only covers "connected, no ticks yet" (pre-open). Parse-checked OK; no test asserts on the 600 constant (`test_s8_collector` passes its own explicit `timeout_secs`). The wrapper `run_s8_collector.cmd` and the Scheduled Task definition were NOT changed.

**OPEN — verify tomorrow (2026-07-31) AM:** confirm `S8Collector_Session` last-run result is **0x0** (green), not 0x2, on the next scheduled run. Today's already-latched instance keeps the pre-fix red 0x2 until its own teardown.

### (b) Control Plane S0 Growth target panel — cash reserve now shown (builds on 2026-07-29 #66)
`dashboard/desk/page_control_plane.py::_render_target_panel`. The panel rendered the raw broker-free model target (risk-only weights normalized to ~100%, no cash line), so it looked fully invested with no cash. It now shows the DEPLOYED book: each risk weight scaled by `(1 − buffer)` plus a synthetic **CASH** line = `investable.buffer_pct()` (currently 0.015 = 1.5%), summing to exactly 100%. Display-only; reads the buffer from config (no hardcode); defensive fallback to the raw book on any error. Parse-checked OK; `buffer_pct()` confirmed 0.015. No strategy/engine/deploy code changed.

## 2. DECISION (Andrew, 2026-07-30) — the 1.5% cash buffer stays OUT of the model
`cash_reserve_pct = 0.015` (`config.RISK_LIMITS`) is an **execution-side overlay only**:
- Applied exactly once, at deploy, in `investable.compute_investable()` — buys sized against 98.5% of NAV, so the funded account holds ~1.5% cash (covers monthly fees; Andrew's floor is ≥1%). This was already true before today; nothing on the money path changed.
- Do NOT bake it into the backtest/model, and do NOT inject a CASH weight into `strategy_target.current_target()` raw weights — the deploy already applies the buffer to those weights, so a model-side cash line would DOUBLE-COUNT (0.985² ≈ 97% invested) at `reconcile.py` share-sizing.
- The backtest stays pure 100%-invested (T-bills are the defensive sleeve, an investable ETF — not cash).
- Where cash IS shown as a real book (risk×(1−buffer) + CASH, sums to 100%): the Control Plane panel (§1b) and `reconcile.py` (already appends a CASH line at `buffer_pct()`).
- No model-portfolio time-series tracker exists yet; the model book is recomputed from the backtest on demand, and only `nav_history.csv` persists (the LIVE account's total NetLiq, cash included). If a model-portfolio tracker is built later, record the buffer-inclusive book so it ties to the account from inception.
(Also captured in Claude memory: `tradingdesk-cash-buffer-overlay`.)

## 3. STILL OPEN — carried forward from 2026-07-29 (UNCHANGED today)
The Control Plane Phase 1 mission remains the active next mission. Nothing below was touched today.
- **STAGE 3 arm/execute wiring — PARKED** pending owner review (the first in-app transmit trigger: typed-confirm + a *measured* gateway read-only probe + subprocess-invoke the UNCHANGED `s0_live_deploy --arm-i-understand --conform`). Do NOT build unattended.
- **Propose-and-arm monthly-email design — open points A/B/C/D** (proposal cadence beyond the monthly signal; what earns a proposal; notify channel; freshness policy). Settled: posture = propose-and-arm (b); build Phase 1 on the unchanged `s0_live_deploy`; maker-checker single-operator for now; Phase 3 margin gate #57 deferred to trigger (next account added is another OWN account before any client account).
- **Follow-ups:** (a) de-paper Layer 1 (#68) — the home Pulse tile "Strategy 0 — Paper only — real-money OFF" is STALE after the 2026-07-29 trust deploy (likely `dashboard/desk/deskdata.py`); must NOT touch the real paper-account plumbing (`connections/ibkr_paper.py`, PAPER_PORT=4002, paper watchdog, `is_paper`). (b) the running **:8502 desk app is an OLDER process** started before the Control Plane page existed — **restart it to guarantee the new page AND today's panel change (§1b) actually show**. (c) the `launch.json` :8502 config is gitignored.
- Full mission detail + key file pointers: `docs/SESSION_HANDOFF_2026-07-29.md` and `docs/PRODUCTION_REBALANCE_CONTROL_PLANE.md`.

## 4. Master Ops note
Today was TradingDesk operational work only. Per the business-line firewall and "Master Ops holds portfolio-level only," NOTHING was written to `00_Master_Ops/To_Do.md`, and the open CRM `00_Master_Ops/Handoff_Active.md` (STEP 3 → Ted, etc.) was deliberately NOT cleared — today did not touch it.