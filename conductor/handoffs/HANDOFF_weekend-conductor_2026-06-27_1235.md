# HANDOFF — weekend conductor sitting (2026-06-27 ~12:35 CT)

Self-contained STATE for a clean Monday pickup. NOT task orders. The rebalance procedure
itself lives in `MONDAY_RUNBOOK.md` (repo root) — this file is the surrounding context.

Today one conductor session worked across all four lanes, delegating to background workers.
Recent commits this sitting: 36800f3, 4f9b30a, 2a24612, 36764f6, 22dee54, 6187fda, 78cdabe.

---

## LANE A — Strategy & Backtester

**Done**
- Backtester data MOVED off Google Drive → `C:\TradingDesk-Local\bt_data\` (Drive sync was
  corrupting the parquet/CSV data). Loader + downloader are now absolute-path-aware; config
  repointed; in-repo `backtester/data/` kept with a `.gitkeep` only. 89 tests pass.
- GFC/2008 tables REGENERATED on extended **2007+** Tiingo history and committed (78cdabe).
  `strategies/config.DATA_START` is now `2007-01-01`.
  - Balanced 2007→2026: CAGR **8.5%**, maxDD **-10.2%**, Calmar **0.83**, Sortino **1.16**;
    GFC-window maxDD -7.1%; **calendar-2008 +8.3%** (was +3.4% pre-margin); 2022 -6.1%.
  - 2015-26 headline UNCHANGED: CAGR 7.45%, maxDD -10.20%, Calmar 0.73.
  - Stale "history-missing / pre-margin / needs-re-run" caveats removed from VALIDATION.md.
- Paperbot↔backtester byte-parity RE-PROVEN with `REGIME_TREND_MARGIN=0.03` (max abs diff 0.0).

**Open / owed**
- **Andrew gut-check owed:** the 2008 result moved a lot (+3.4% → +8.3%). Confirm it looks
  right before relying on the GFC numbers. Not a blocker for paper use, but flagged.

**Where things live**
- Active data: `C:\TradingDesk-Local\bt_data\`. Good-2010 backup: `C:\TradingDesk-Local\bt_data_backup_2010_good`.
- Tables: `backtester/VALIDATION.md`. Margin knob: `strategies/strategies/config.py`.

---

## LANE B — Data Warehouse / Collector

**Done**
- DuckDB `options_eod` "non-empty parquets only" fix CONFIRMED already in committed
  `storage.py` (`_nonempty_parquets()` + `rebuild_catalog()`); verified vs the LIVE ~102k-file
  warehouse — zero-column markers EXCLUDED (kept on disk), 0 corrupt, view builds clean. No change needed.
- KILLED a rogue duplicate `download.py` (system-Python, 6–12 GB, warehouse-race hazard).
- FIXED + committed (6187fda) the index-root crash in `ibkr_forward.py` (`_to_df` fillna on NaN
  spot for SPX/SPXW/VIX/NDX/RUT/XSP). Last night's forward run failed 30/43 symbols on exactly this;
  future runs are clean.
- ThetaData historical grab COMPLETE (`GRAB_END=20260625`); supervisor self-heals via Task Scheduler.

**In progress**
- A worker is backfilling **2026-06-26 EOD** from ThetaData (the symbols last night's IBKR forward
  run dropped). Check it finished cleanly on Monday.

**Watch-out**
- Do NOT delete empty/zero-column parquets — `have_day` relies on their presence as day-markers.

---

## LANE C — Paperbot Execution  (PAPER ONLY — nothing transmitted)

**Done**
- Multi-account rebalance ENGINE built + committed: `paperbot/rebalance_engine.py`. Computes per-account
  integer target shares, carves out the cash-flow reserve, applies an **account-level all-or-nothing band**
  (triggered off required trade-size vs NetLiq, not raw weight drift), aggregates into block orders with a
  per-account `ContractsOrShares` split, emits an empty FA method (per the Err-10226 finding), and never
  whatIfOrders a group. Tests pass.
- `recon_report.plan_account` ALIGNED to the engine's account-level band so the readout matches the actor.
- Runner built + committed (22dee54): `paperbot/rebalance_run.py` — multi-account dry-run runner with the
  review→arm→transmit gate; reads live FA groups via requestFA and fails closed on a name mismatch;
  transmits nothing. clientId **37**.

**State of the accounts**
- 5 client subs DU8922142–146, each ~$1.1M paper, all enrolled, **all FLAT (cash)** → each needs a full
  initial rebalance.
- FA groups in place: Conservative→DU142; Balanced→DU143,144; Growth→DU145,146 (method ContractsOrShares).
  Prior config backed up at `state\paperbot\fa_groups_backup.xml`.

**Open — held for Monday**
- The FIRST live rebalance is HELD for **MONDAY**. Two reasons, both expected and not bugs:
  1. The paper gateway's account-data feed is DOWN over the weekend — `accounts.py` / `fa_probe.py` hang
     at the connect-time account sync. So the live read-only review (real net-liq/positions) can't run today.
  2. Market is closed.
- Build + dry-run review are complete. **Transmit is Monday.**

**Monday plan** → see `MONDAY_RUNBOOK.md` (repo root) for the exact steps. In short: live read-only review
→ resolve group names via requestFA (fail-closed on mismatch) → set each group's ContractsOrShares = engine
split → **Andrew arms** → transmit blocks → watch fills → reconcile to model.

**Account model reminder**
- Trade the **DU sub-accounts**; FA master DF8922141 rejects direct orders + hangs reads. Paper gateway hard
  read-only lock is OFF — software arming is the control. clientIds in use: 30, 34, 35, 36, 37.

---

## LANE D — Reporting
- UNCHANGED. RRG retired; harness repurposed into the EOD status digest (`eod_report.py`), scheduled (CT).
  `daily_run.py` + `connections/ibkr.py` carry the gateway `java_version=17` launch fix.

---

## Housekeeping this sitting
- Handoffs consolidated into `conductor/handoffs/`. The stray "Andrew is pissed off" folder was deleted.
- Working style today: conductor delegates to background workers, stays decisive (it's a PAPER account),
  and doesn't block on inline work or over-ask.

## Monday one-glance
1. Lane B: confirm the 2026-06-26 EOD backfill finished clean.
2. Lane C: run `MONDAY_RUNBOOK.md` for the first live rebalance (Andrew arms; transmit).
3. Lane A (no rush): Andrew eyeballs the new 2008 (+8.3%) result.
