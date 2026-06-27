# LANE STATUS  (last updated by: Conductor/dispatch, 2026-06-26)

> Seeded from one session's view + shared memory. The Conductor should CONFIRM each lane
> against the actual sessions and correct anything stale.
>
> RAW SESSION HANDOFFS now live in `conductor/handoffs/` (dated drop folder; read its
> README). This sitting's four: `HANDOFF_paperbot_2026-06-26_1958.md`,
> `HANDOFF_paperbot-optionB_2026-06-26_2001.md`,
> `HANDOFF_datacollector-reporting_2026-06-26_2000.md`,
> `HANDOFF_backtester-ma200_2026-06-26_2000.md`.

## A — Strategy & Backtester
- 200d-MA fragility fix ADOPTED: `REGIME_TREND_MARGIN=0.03` (regime-only early-exit margin).
  Full handoff: `handoffs/HANDOFF_backtester-ma200_2026-06-26_2000.md`.
- DONE 2026-06-27: `backtester/data/` MOVED off Drive to `C:\TradingDesk-Local\bt_data\`
  (config repointed; loader/downloader made absolute-path-aware; Drive copies deleted; data/
  folder kept w/ .gitkeep). 89 tests pass; loads 2010-01-04→2026-06-26. Drive-sync instability RESOLVED.
- OPEN: (1) the 2 uncommitted doc edits (`backtester/README.md`, `VALIDATION.md`) — left for conductor git;
  (2) regenerate GFC (2008) tables — DEFERRED. C-drive search DONE 2026-06-27: full pre-2010 universe is
  NOT on disk — only AGG+LQD reach 2005; a SPY-only CSV (2008+) sits at msr\Flow Project\flow_verdict\data\
  spy_hist_2008_2026.csv; TLT/VTI/sectors/gold/commodities all start 2010. Only path = Tiingo re-download
  with earlier DATA_START + key loaded. HOLD until ThetaData download finishes (Andrew: don't compete for bandwidth).
  (3) DONE 2026-06-27: paperbot byte-parity RE-PROVEN. paperbot strategy_target.current_target() calls
  src.backtest.run_backtest (single weight path) → targets byte-identical to the backtester with
  REGIME_TREND_MARGIN=0.03 (max abs diff 0.0 across 4 dates × 3 versions). Paper-use prerequisite cleared.

## B — Data Warehouse / Collector
- RESOLVED 2026-06-27: the DuckDB `options_eod` "non-empty parquets only" fix is ALREADY in the
  committed code (storage.py `_nonempty_parquets()` + `rebuild_catalog()`); the prior STATUS line was
  STALE. Verified against the LIVE warehouse: 102,148 parquet across 47 symbols, 7,659 zero-column
  markers correctly EXCLUDED (kept on disk), 0 corrupt; view builds clean. No code change needed.
  Optional future perf nicety: skip footer read via `_manifest.json` row count (not a correctness issue).
- ThetaData terminal: was stopped for the move; has been seen running this session.
- Do NOT delete empty/zero-column parquets (have_day relies on them).

## C — Paperbot Execution
> Two threads in this lane. Serialize any order placement / gateway / git between them.
- **Option B build — advanced through Increment 3 (2026-06-26).** Full handoff:
  `conductor/handoffs/HANDOFF_paperbot-optionB_2026-06-26_2001.md`. All READ-ONLY / what-if —
  nothing transmitted, no FA config written.
  - BUILT + verified: per-tier `ENROLLMENT`; multi-account discovery (`accounts.py`);
    distribution-reserve + schedule (`cashflows.py`, empty until Andrew feeds it); daily
    reconciliation/drift/**block-aggregation** report (`recon_report.py`); no-trade band;
    version stamping (v0.4.0); read-only FA-config probe (`fa_probe.py`).
  - Account reality: 5 client subs DU8922142–146 each FUNDED ~$1.1M paper, all enrolled,
    currently all-cash → each needs a full initial rebalance.
  - **OPEN (do first):** `fa_block_test.py` (does the FA master accept an API block order?)
    stalled with no output → **result UNKNOWN; re-run in the FOREGROUND.**
  - **DECISION (APPROVED 2026-06-26):** after the block what-if passes, create the 3 paper
    allocation groups (Conservative/Balanced/Growth), replacing the leftover test group.
    Paper only, reversible, no client orders by this step. (See DECISIONS `optionB-create-groups`.)
- **Flatten thread (Andrew APPROVED 2026-06-26):** sweep DU142-146, sell every leftover
  (incl. GOOG in DU143 + 1 test PDBC share in DU142), confirm zero, log each. See
  `handoffs/HANDOFF_paperbot_2026-06-26_1958.md`. **Must finish BEFORE the first Option B
  block rebalance** (invest from a blank slate).
  - **DONE & VERIFIED 2026-06-26 16:10 CT.** Flatten EXECUTED in extended hours (outsideRth=True);
    confirmed by IBKR execution feed (execIds): DU146 SLD 100 SPY @730.57, DU143 SLD 100 GOOG @335.70,
    DU142 SLD 1 PDBC @15.81. **All 5 DU subs FLAT (0 positions), zero open orders.** The earlier
    "flatten-monday / market-closed" note was a PARKED session reading a STALE pre-flatten snapshot —
    obsolete, no Monday action needed (see DECISIONS corrections).
  - **Block-order proof DONE & VERIFIED 2026-06-26:** master ACCEPTS + SPLITS a group block order
    (3 PDBC -> 1 each into DU142/143/144, then flattened; execIds on file; all flat again). Order-level
    NetLiq is NOT available (Err 10226) -> allocation must be explicit shares (ContractsOrShares).
  - **DECISION (Andrew APPROVED A, 2026-06-26):** execution model = engine computes each account's
    explicit target shares (net liq + reserve + band) and places blocks against ContractsOrShares groups.
  - **DONE & VERIFIED 2026-06-26 ~22:08 CT:** 3 tier FA groups created via replaceFA + re-probed —
    Conservative->DU142; Balanced->DU143,144; Growth->DU145,146; method ContractsOrShares; test_group
    dropped (prior config backed up to state\paperbot\fa_groups_backup.xml). SAFE STATE at session end:
    all 5 DU subs FLAT, 0 open orders, nothing transmitted, FA config consistent. NEXT (separately gated,
    needs Andrew's go): build the multi-account block engine (recon -> per-acct shares -> set group amounts
    -> block -> arm -> transmit). clientId registry now: flatten=34, fa_block=35, fa_admin=36.
  - DONE 2026-06-27 (OFFLINE, no transmit): computation core built — `paperbot/rebalance_engine.py`
    (per-acct integer target shares, reserve carve-out, 3% band, block aggregation w/ per-account
    `ContractsOrShares` split; emits empty FA method per Err-10226 fix; never whatIfOrders a group).
    14 pytest tests pass. version 0.4.0→0.5.0. NOT committed (conductor git).
    BAND DECISION RESOLVED (Andrew 2026-06-27): ACCOUNT-LEVEL all-or-nothing. Engine updated + a
    distinguishing test added; 15 pytest tests pass. NOTE: engine triggers the rebalance off the
    required TRADE SIZE vs NetLiq (not reconcile's raw weight-vs-model drift) so the cash-reserve gap
    can't falsely flag a correctly-invested account / defeat the band on >~60%-weight holdings.
    FOLLOW-UP DONE 2026-06-27: recon_report.plan_account aligned to the engine's account-level trade-size
    band (readout now matches the actor); verified offline; 15 engine tests still pass.
    Architecture reference written: docs/ARCHITECTURE.md (code-on-Drive vs data-on-C: map, caveats).
    REMAINING LIVE STEPS (conductor, serialized, gated): align tier group names
    (tier_conservative/balanced/growth vs Conservative/Balanced/Growth); per-rebalance set each group's
    ContractsOrShares = RoutePlan split; wire build_plan→order_router behind READONLY+DRY_RUN+armed; then place.
- Account model: trade **DU sub-accounts**; FA master DF8922141 rejects direct orders +
  hangs reads (connect with explicit `account=`). Paper gateway hard read-only lock is OFF
  (software arming is the control).

## D — Reporting
- RRG retired; harness repurposed into an EOD status digest (`eod_report.py`). Scheduled (CT).
- `daily_run.py` got the same gateway java_version=17 fix this session.

## Shared plumbing
- `connections/ibkr.py` carries the gateway launch fix (`java_version=17`).
- Project is now a local git repo (in Drive, no remote); commit after each change-set.
- clientId registry in `connections/clientids.py` (paperbot=30); don't collide.
