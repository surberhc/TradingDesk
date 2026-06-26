# LANE STATUS  (last updated by: Paperbot/desktop session, 2026-06-26)

> Seeded from one session's view + shared memory. The Conductor should CONFIRM each lane
> against the actual sessions and correct anything stale.

## A — Strategy & Backtester
- 200d-MA fragility fix ADOPTED: `REGIME_TREND_MARGIN=0.03` (regime-only early-exit margin).
- OPEN: re-prove paperbot byte-parity against the new config; GFC (2008) re-run.
- Watch: `data/` reportedly has Google-Drive-sync instability — flagged in memory.

## B — Data Warehouse / Collector
- DuckDB `options_eod` view still needs the "build over non-empty parquets only" fix.
- ThetaData terminal: was stopped for the move; has been seen running this session.
- Do NOT delete empty/zero-column parquets (have_day relies on them).

## C — Paperbot Execution
> Two threads in this lane. Serialize any order placement / gateway / git between them.
- **Option B build — advanced through Increment 3 (2026-06-26).** Full handoff:
  `conductor/HANDOFF_C_paperbot_optionB.md`. All READ-ONLY / what-if — nothing transmitted,
  no FA config written.
  - BUILT + verified: per-tier `ENROLLMENT`; multi-account discovery (`accounts.py`);
    distribution-reserve + schedule (`cashflows.py`, empty until Andrew feeds it); daily
    reconciliation/drift/**block-aggregation** report (`recon_report.py`); no-trade band;
    version stamping (v0.4.0); read-only FA-config probe (`fa_probe.py`).
  - Account reality: 5 client subs DU8922142–146 each FUNDED ~$1.1M paper, all enrolled,
    currently all-cash → each needs a full initial rebalance.
  - **OPEN (do first):** `fa_block_test.py` (does the FA master accept an API block order?)
    stalled with no output → **result UNKNOWN; re-run in the FOREGROUND.**
- **Flatten thread (Andrew decided):** sweep DU142-146, sell every leftover (incl. GOOG in
  DU143 + 1 test PDBC share in DU142), confirm zero, log each. See `HANDOFF.md`. **Must
  finish BEFORE the first Option B block rebalance** (invest from a blank slate).
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
