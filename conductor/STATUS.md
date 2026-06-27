# LANE STATUS  (last updated by: Conductor, 2026-06-27 ~12:35 CT)

> Live dashboard. RAW SESSION HANDOFFS live in `conductor/handoffs/` (dated drop folder;
> read its README). Latest weekend handoff: `HANDOFF_weekend-conductor_2026-06-27_1235.md`.
> First live paperbot rebalance is HELD for **MONDAY** — steps in `MONDAY_RUNBOOK.md` (repo root).

## OPEN ITEMS — running tally (updated 2026-06-27; mirrors the session task list)
**In progress:** [1] SPXW 1-min collector (~44/1170, ETA ~June 30; 1 clean instance, smart-filtered).
**Open / next:** [2] re-pull 1-min day 20260529 (self-heals on the collector's next run) · dashboard Phase 2 (backtester controls) + Phase 3 (gated trading controls) — Phase-1 monitor DONE.
**Blocked / time-gated:** [5] S2/S3 condor backtests — blocked on 1-min data (~June 30) · [6] Monday first live paper rebalance — gated to Monday (gateway+market), needs [7] first · [7] Monday pre-arm: verify replaceFA FA-XML tag casing · [11] don't cancel ThetaData sub until pulls complete.
**Owed:** [8] Andrew gut-check the 2008 GFC +8.3% number.
**Recently closed:** dashboard Phase-1 monitor [12]; S3 v1 condor control [4]; flow de-risk gate [3] (tested→rejected); cosmetics [9]; killed redundant cron [10]; gamma overlay + weekly cadence (both tested→rejected); GEX rebuild+calibration (70%); daily grab→ThetaData (IBKR retired); EOD Dealer-Gamma section; Friday 6/26 data fix; full top-down audit.

## A — Strategy & Backtester
- 200d-MA fragility fix ADOPTED: `REGIME_TREND_MARGIN=0.03` (regime-only early-exit margin).
- DONE 2026-06-27: `backtester/data/` MOVED off Google Drive to `C:\TradingDesk-Local\bt_data\`
  (Drive sync was corrupting the data). config repointed; loader/downloader are absolute-path-aware;
  data/ kept w/ .gitkeep. 89 tests pass. Drive-sync instability RESOLVED.
- DONE 2026-06-27 (committed 78cdabe): GFC/2008 tables REGENERATED on extended 2007+ Tiingo data.
  `config.DATA_START` now `2007-01-01`. Balanced 2007→2026: **CAGR 8.5% / maxDD -10.2% / Calmar 0.83 /
  Sortino 1.16**; GFC-window maxDD -7.1%; **calendar-2008 +8.3%** (up from old +3.4% pre-margin) — 2022 -6.1%.
  2015-26 headline UNCHANGED (CAGR 7.45% / maxDD -10.20% / Calmar 0.73). Good 2010 data backed up at
  `C:\TradingDesk-Local\bt_data_backup_2010_good`.
  - **FLAGGED for Andrew's gut-check:** the 2008 jump (+3.4% → +8.3%) is a big move; sanity-confirm it
    looks right before leaning on the GFC numbers.
- DONE 2026-06-27: paperbot byte-parity RE-PROVEN — paperbot targets are byte-identical to the backtester
  with `REGIME_TREND_MARGIN=0.03` (max abs diff 0.0). Paper-use prerequisite cleared.

## B — Data Warehouse / Collector
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
