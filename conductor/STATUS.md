# LANE STATUS  (last updated by: Conductor, 2026-06-28 ~19:00 CT — SESSION CLOSE)

> Live dashboard. RAW SESSION HANDOFFS live in `conductor/handoffs/` (dated drop folder).
> **PICK UP HERE NEXT SESSION → `conductor/handoffs/HANDOFF_2026-06-28_session-close.md`** (full session-close handoff).
> First live paperbot rebalance is HELD for **MONDAY** — steps in `MONDAY_RUNBOOK.md` (repo root).
> Desk runs on its own: collector + ThetaData terminal + dashboard survive this session closing (Task Scheduler / background).
> **Regime config is UNTOUCHED this session.** The re-entry MAX_LAG 6→3 change was TESTED and **HELD — NOT adopted.** No strategy/data code or config changed; this was a research + audit session.

## OPEN ITEMS — running tally (updated 2026-06-28 ~19:00; mirrors the session task list)
**In progress (autonomous):** [1] SPXW 1-min collector — **374/1170 (~32%)** as of 2026-06-28 18:47, ~10.49 GB, current day 20241227, **0 errors**, avg ~220 s/day, **ETA ~2026-06-30 19:30**. Self-healing via Task Scheduler. (Survived a terminal outage mid-session — resumed from day ~342, not from scratch.)
**Open research leads (NOTHING adopted):** regime `sharp_recovery` clean-V-only refinement (PRIME lead, HIGH curve-fit risk) · S4 SPX vol-control fund (new buildable strategy idea) · DDOI gamma build (negative-gamma method gap, blocked on 1-min data) · intraday-gamma early-exit revisit (~June 30).
**Open infra:** ThetaData terminal **port-25503 WATCHDOG** — recommended after this session's outage, NOT built.
**Open dashboard:** look-improvement roadmap (gamma grid, GEX zero-line chart, consistent formatting) — quick-win restyle done this session, deeper roadmap NOT done · Phase 2 (backtester controls) + Phase 3 (gated trading controls).
**Blocked / time-gated:** [5] S2/S3 condor backtests + PV-band chart + DDOI build — blocked on 1-min data (~June 30) · [6] Monday first live paper rebalance — gated to Monday (gateway+market), needs [7] first · [7] Monday pre-arm: verify replaceFA FA-XML tag casing (`fa_probe.py`) · [11] don't cancel ThetaData sub until pulls complete (~June 30).
**Owed:** _(none open)_ — [8] 2008 GFC +8.3% **CLOSED 2026-06-29 (closed on Andrew's nod).** See Recently closed.
**TESTED → REJECTED this session:** vol-control borrows as an S0 OVERLAY (subordinate to the regime band) · re-entry MAX_LAG 6→3 (failed the per-episode safety gate — a risk-budget trade-off, not a free win; HELD) · drawdown-depth exit gate + gamma/vol/term-structure exit overlays (the exit-whipsaw is NOT signal-separable).
**FIXED 2026-06-28:** `features/gex.py` spot<=0 ZeroDivisionError guarded (`a953389`) · `FRED_API_KEY` added + verified (`610507c`; ICE HY OAS limited to ~2023-06+ by rolling-3yr restriction — can't warm 2008) · dashboard restyle (`6b3609a` / `acb0bb6` / `99865da`).
**Recently closed:** **[8] 2008 GFC +8.3% — CLOSED 2026-06-29 (Andrew's nod).** Audit fully flushed: 3 independent passes (data-integrity + method re-derivation from raw NAV + margin sweep showing a PLATEAU not a peak) + look-ahead tests, all PASS, PLUS active-2008-navigation CONFIRMED via warm-up test (separate `bt_data_ext2005`; engine at CapitalPreservation by Jan-2008, led by trend/breadth/vol). Documented caveat (a limitation, NOT an open task): the CREDIT half of the GFC-entry read is unwarmable (HYG inception ~2007-04), so entry was driven by trend/breadth/vol not credit (`VALIDATION.md` §5 softened accordingly). Evidence: `backtester/output/gfc_decomposition_2026-06-28.md`. desktop launcher [13]; dashboard Phase-1 monitor [12]; collector smart-filter committed; re-pull day 20260529 (self-heals); S3 v1 condor control [4]; flow de-risk gate [3] (tested→rejected); cosmetics [9]; killed redundant cron [10]; gamma overlay + weekly cadence (both tested→rejected); GEX rebuild+calibration (70%); daily grab→ThetaData (IBKR retired); EOD Dealer-Gamma section; Friday 6/26 data fix; full top-down audit.

## A — Strategy & Backtester
- 200d-MA fragility fix ADOPTED: `REGIME_TREND_MARGIN=0.03` (regime-only early-exit margin).
- **REGIME ENGINE STRUCTURAL EXPLORATION 2026-06-28 — NOTHING ADOPTED, config UNTOUCHED (all in-process tests).**
  Conclusion is a NEGATIVE (curve-fit-PREVENTING) result: the engine is ROBUST and no structural tweak cleanly improves it without a cost.
  * **Bleed characterized:** it is RE-ENTRY LAG + SHALLOW-DIP WHIPSAWS (2025 cut to 0% on ~−8.6%, 2026 cut 85%→24% on ~−5–8%, both round-tripped). The deep-crisis EXITS are GOOD — leave them alone.
  * Existing knobs = a robust plateau; can't fix the whipsaw.
  * **Re-entry `MAX_LAG` 6→3: TESTED → HELD (NOT adopted).** Looked free in Stage A but FAILED the final per-episode safety gate — 3 episodes worsen >100bp (2008 tail −118bp, 2011 −114bp, 2015-16 −152bp). It's a risk-budget TRADE-OFF: full-window maxDD byte-identical (−10.20%, no new tail risk), WINS episode-NAV 2009 (+2.76%) & 2011 (+0.95%); only genuine loser is the SIDEWAYS 2015-16 grind. Benefit smaller than first quoted (2022 lag ~14→12mo). **`REENTRY_MAX_LAG_MONTHS` stays 6.**
  * **Exit gates can't fix it:** a drawdown-DEPTH gate FAILS (depth can't separate whipsaws from real-crash first legs — both −7% to −9%); gamma/vol/term-structure overlays ALSO can't separate them. The exit-whipsaw is NOT signal-separable by any overlay.
  * **PRIME OPEN LEAD (next session):** tighten the override's `sharp_recovery` trigger to fire only on CLEAN V-recoveries (the failure is the override firing in SIDEWAYS grinds), then re-run the per-episode gate. HIGH curve-fit risk; needs a principled trigger + OOS re-test. Until/unless it clears, re-entry stays `MAX_LAG=6`.
- **Vol-control borrows as an S0 OVERLAY 2026-06-28: TESTED → REJECTED** (subordinate to the regime band — the band already collapses exposure in crises). VIX3M/VIX9D/VVIX pulled to `bt_data`. `FRED_API_KEY` added + verified (committed `610507c`); ICE HY OAS limited to ~2023-06+ (rolling-3yr restriction) — can't warm 2008; HYG/IEF proxy retained for history.
- **NEW STRATEGY LEAD — S4 "SPX vol-control fund" (OPEN, not built):** the SAME vol-control mechanics (`exposure = min(leverage_cap, target_vol/realized_vol)`, daily) as a STANDALONE single-asset SPX fund (FIA-style vol-control index replica), NOT an S0 overlay — which sidesteps the "subordinate to the regime band" rejection. `target_vol` and `leverage_cap` both swept. Buildable today. (Memory note `s4-spx-vol-control-fund`.)
- **GFC active-2008-navigation CONFIRMED real 2026-06-28:** a definitive warm-up test (separate dir `bt_data_ext2005`; canonical `bt_data` untouched) shows trend/breadth/vol warmed by 2006-03 and the engine at CapitalPreservation by late-2007/Jan-2008, BEFORE the worst leg — the de-risk is active navigation, not a warm-up artifact. CAVEAT: the CREDIT half of the GFC-entry read is unwarmable (HYG inception ~2007-04), so the entry was driven by trend/breadth/vol, not credit. `VALIDATION.md` §5 softened accordingly.
- **Gamma symbol verdict HELD + characterized 2026-06-28:** daily S0 signal stays on SPX root (matches Tier1Alpha vendor labels 69.8% vs SPXW 60.1% / combined 65.5%-null). The residual gap is a genuine method diff on the NEGATIVE-gamma side (our static dealer-sign vs vendors' inferred DDOI) — closing it needs a DDOI-style trade-direction build off the 1-min data (future). Gamma is HORIZON-DEPENDENT (daily SPX for S0; intraday SPXW/0DTE for S2/S3).
- DONE 2026-06-27: `backtester/data/` MOVED off Google Drive to `C:\TradingDesk-Local\bt_data\`
  (Drive sync was corrupting the data). config repointed; loader/downloader are absolute-path-aware;
  data/ kept w/ .gitkeep. 89 tests pass. Drive-sync instability RESOLVED.
- DONE 2026-06-27 (committed 78cdabe): GFC/2008 tables REGENERATED on extended 2007+ Tiingo data.
  `config.DATA_START` now `2007-01-01`. Balanced 2007→2026: **CAGR 8.5% / maxDD -10.2% / Calmar 0.83 /
  Sortino 1.16**; GFC-window maxDD -7.1%; **calendar-2008 +8.3%** (up from old +3.4% pre-margin) — 2022 -6.1%.
  2015-26 headline UNCHANGED (CAGR 7.45% / maxDD -10.20% / Calmar 0.73). Good 2010 data backed up at
  `C:\TradingDesk-Local\bt_data_backup_2010_good`.
  - **AUDITED CLEAN 2026-06-28 (3 independent passes):** (1) data-integrity — no corruption/stale data,
    2008 prices real-world correct, +8.28% explained by sane holdings (0% equity / ~24% Treasuries / ~5% gold
    / rest cash); (2) method re-derivation from raw NAV — reproduces exactly; identical −7.13% GFC maxDD is
    mechanically forced (byte-identical fully-de-risked book through the deep-stress leg; margin changes only
    the recovery, not the depth); (3) margin sweep — PLATEAU not peak (cal-2008 flat at +8.28% for ALL margins
    ≥0.01, so 0.03 is NOT 2008-tuned; Calmar spread 0.034 across 0.03–0.05), 95/95 tests pass incl. both
    no-look-ahead tests, T+1 lag verified real, start-date perturbation leaves cal-2008 unchanged. Decomposition:
    ~44% of the +3.4%→+8.3% jump is the data refetch, ~56% is the general early-de-risk property (which COSTS
    full-window CAGR while shaving drawdown) — neither is curve-fit. Evidence: `backtester/output/gfc_decomposition_2026-06-28.md`.
- DONE 2026-06-27: paperbot byte-parity RE-PROVEN — paperbot targets are byte-identical to the backtester
  with `REGIME_TREND_MARGIN=0.03` (max abs diff 0.0). Paper-use prerequisite cleared.

## B — Data Warehouse / Collector
- **OUTAGE + RECOVERY 2026-06-28 ~14:47:** ThetaData terminal + collector + dashboard all died. Recovered —
  terminal relaunched via `datacollector/start_terminal.py`; collector RESUMED from day ~342 (not from scratch,
  thanks to the progress heartbeat + on-disk dedupe). **ROOT CAUSE:** no watchdog auto-restarts the ThetaData
  TERMINAL (the collector's supervisor restarts the *collector*, but a dead terminal on port 25503 just stalls it).
  **OPEN:** build a port-25503 watchdog scheduled task (recommended, not built).
- Collector snapshot 2026-06-28 18:47: **374/1170 days (31.97%)**, ~10.49 GB, 0 errors, avg ~220 s/day, ETA ~2026-06-30 19:30.
- RESOLVED 2026-06-27: the DuckDB `options_eod` "non-empty parquets only" fix is ALREADY in committed
  `storage.py` (`_nonempty_parquets()` + `rebuild_catalog()`). Verified against the LIVE ~102k-file
  warehouse — zero-column markers correctly EXCLUDED (kept on disk), 0 corrupt, view builds clean.
- KILLED 2026-06-27: a rogue duplicate `download.py` (running on system-Python, 6–12 GB, a
  warehouse-race hazard). Gone.
- FIXED 2026-06-27 (committed 6187fda): index-root crash in `ibkr_forward.py` (`_to_df` fillna on NaN
  spot for SPX/SPXW/VIX/NDX/RUT/XSP). Last night's forward run failed 30/43 symbols on this bug; future
  runs are now clean.
- ThetaData historical grab COMPLETE (`GRAB_END=20260625`); supervisor self-heals via Task Scheduler.
- VERIFIED COMPLETE 2026-06-27: the **2026-06-26 EOD** ThetaData backfill is done — all 30 previously-failed
  symbols now have Friday's data (**50/50 symbols present**, none empty/corrupt). Covers the symbols last
  night's forward run dropped.
- Do NOT delete empty/zero-column parquets (`have_day` relies on them).

## C — Paperbot Execution
> All PAPER. Nothing transmitted. review→arm→transmit gate intact. Serialize any order / gateway / git.
- Multi-account rebalance ENGINE built + committed: `paperbot/rebalance_engine.py` (per-acct integer
  target shares, reserve carve-out, **account-level all-or-nothing band**, block aggregation w/ per-account
  `ContractsOrShares` split; emits empty FA method per the Err-10226 fix; never whatIfOrders a group).
  Engine triggers off required **trade size vs NetLiq**, not raw weight-vs-model drift. Tests pass.
- `recon_report.plan_account` ALIGNED to the engine's account-level trade-size band (readout matches the actor).
- Runner built + committed (22dee54): `paperbot/rebalance_run.py` — multi-account dry-run runner,
  review→arm→transmit gate, reads live FA groups via requestFA + fails closed on name mismatch, transmits
  nothing. clientId **37**.
- **Transmit EXECUTOR BUILT + committed (9220716): `paperbot/rebalance_execute.py`, clientId 38** — the
  transmit-CAPABLE Monday sibling of the runner. Default run = read-only DRY review (transmits nothing,
  writes no FA config). Armed transmit requires the **4-condition gate**: `READONLY=False` AND `DRY_RUN=False`
  AND `armed=True` AND the exact CLI token `--arm-i-understand` (which flips READONLY/DRY_RUN in-process; no
  auto-arm). Armed flow in code: discover → build_plan → resolve_tier_groups (fail-closed) → risk_manager →
  BACK UP FA config → set each group's ContractsOrShares via `replaceFA` → place blocks ONE at a time (never
  whatIfOrders a group) → reconcile. `order_router` now **rejects NaN/<=0 limit prices** (hard price guard).
  **33 tests pass.** Monday CLI: dry review `python rebalance_execute.py`; armed `python rebalance_execute.py
  --arm-i-understand`.
- **MONDAY FLAG:** the executor's `set_group_contracts_or_shares` XML tag casing must be **eyeballed against
  a live `requestFA(1)` dump (run `fa_probe.py`) before the armed run** — couldn't be confirmed offline. New
  pre-arm step added to `MONDAY_RUNBOOK.md`.
- Account reality: 5 client subs DU8922142–146, each FUNDED ~$1.1M paper, all enrolled, all FLAT (cash) →
  each needs a full initial rebalance. FA groups exist: Conservative→DU142; Balanced→DU143,144;
  Growth→DU145,146 (method ContractsOrShares; prior config backed up to `state\paperbot\fa_groups_backup.xml`).
- **FIRST LIVE REBALANCE — HELD FOR MONDAY.** Two blockers, both expected: (1) the paper gateway's
  account-data feed is DOWN for the weekend (account reads HANG at connect-time sync), so the live read-only
  review can't run today; (2) market is closed. Build + dry-run review are done; **transmit is Monday**.
  - MONDAY steps live in `MONDAY_RUNBOOK.md` (repo root) — being written by a worker this session.
- Account model: trade the **DU sub-accounts**; FA master DF8922141 rejects direct orders + hangs reads.
  Paper gateway hard read-only lock is OFF (software arming is the control).

## D — Reporting
- UNCHANGED. RRG retired; harness repurposed into the EOD status digest (`eod_report.py`), scheduled (CT).
- `daily_run.py` + `connections/ibkr.py` carry the gateway `java_version=17` launch fix.

## Shared plumbing
- Local git repo (in Drive, no remote); commit after each change-set.
- clientId registry in `connections/clientids.py`. In use this lane: paperbot=30, flatten=34, fa_block=35,
  fa_admin=36, rebalance_run=37, rebalance_exec=38. Don't collide.
- Handoffs consolidated into `conductor/handoffs/`; the stray "Andrew is pissed off" folder was deleted.
