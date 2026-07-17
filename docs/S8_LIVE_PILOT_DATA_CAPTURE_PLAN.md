# S8 Live-Pilot Data Capture — Build Plan

Status: APPROVED (Andrew, 2026-07-17). Tracked spec for the S8 live-pilot data-capture
build. Build against this document, not the chat thread.

## Purpose
Turn the S8 zero-transmit live pilot into a real, richly-instrumented forward test:
capture as much real data as possible at the moment each (would-be) trade fires and
throughout its life, and monitor exits LIVE rather than reconstructing them after the
fact. The 1-minute SPXW warehouse is a completed historical backfill (through 2026-07-01,
collector idle since 2026-07-02) — it holds NO forward data, so live capture is the only
source of truth for pilot trades.

## Non-negotiables
- **Observation layer only.** This changes NOTHING about how S8 picks entries or exits
  (rule #1 stays clean). It wraps the frozen strategy in capture + monitoring. Exit
  detection reuses the frozen `s8_strategy.stop_price` + B2 rules — never a reimplementation.
- **Zero-transmit preserved end to end:** PILOT_MODE=True, connect readonly=True, gateway
  ReadOnlyApi=yes. Every new component ships with tests asserting no transmit path exists.

## Decisions (locked 2026-07-17)
1. **Tick fidelity:** FULL TICK on the open position's two legs; sampled for broad context.
2. **Collector breadth:** ATM ± a band sized to cover the templates' strike range — NOT the
   full 0DTE chain.
3. **Collector source:** IBKR live, off the 4003 gateway (not a revived ThetaData pipeline).
4. **Backup:** trade-summary records backed up reliably (piggyback the git-bundle backup);
   bulky tick/market parquet is local-only, accepted exposure (same as the 99 GB warehouse).
5. **Retention:** keep everything during the pilot; add rotation later only under disk pressure.

## Components
1. **Gateway lifecycle automation** (scheduled tasks): pre-market launch of
   StartGatewayLiveTrade.bat (weekdays), optional post-close stop; IBC handles
   crash-relaunch + overnight maintenance restart. Only human step: the morning 2FA.
2. **Rich entry capture** (upgrade to the runner): replace the bare chain snapshot with a
   streaming subscription that subscribes to model greeks and WAITS for them to populate
   (fixes the observed short_delta=null). At each entry capture, for both legs:
   quotes/sizes/volume/OI, delta/gamma/vega/theta/IV, plus SPX spot, VIX, session realized
   vol, credit, stop level, B2 details, timestamps. Reuses s8_strategy pick logic unchanged.
3. **Streaming shadow-monitor service** (the large new piece): a persistent intraday process
   holding every open pilot position in crash-safe state, streaming each position's legs +
   underlying tick-by-tick, detecting the exit via the frozen stop/B2 rules, and recording a
   full data grab at the exit moment; anything still open at close is marked expiry/EOD.
   Survives gateway drops/restarts without losing positions or double-recording. Its stateful
   open-position tracking inherently prevents slot re-entry (subsumes the old idempotency-guard
   concern).
4. **Intraday market collector** (ongoing context): samples the ATM-band chain + underlying +
   VIX through the session, off the same gateway (own clientId).
5. **Reporting layer** (later): DuckDB views over the stores — entry/exit characterization,
   greeks at entry vs exit, P&L, max adverse excursion, win-rate by template/regime.

## Storage
Local on C:\TradingDesk-Local\ — NEVER Google Drive (Drive corrupts market data; that is the
reason for the code/data split). Dedicated tree, mirroring the datacollector warehouse
convention (parquet + DuckDB catalog):

    C:\TradingDesk-Local\s8_pilot\
      trades\    one durable record per trade (entry+exit summary) — JSONL, append-only, analysis-grade
      ticks\     per-trade full-tick leg time-series (quotes+greeks) — Parquet, date-partitioned
      market\    intraday ATM-band chain / underlying / VIX context — Parquet, date-partitioned
      state\     live open-positions state for crash recovery — JSON (tiny)
      catalog.duckdb   queryable views over the parquet/jsonl
      logs\

- Trade records (JSONL): a few KB/day, human-inspectable, crash-safe — the irreplaceable layer; backed up.
- Ticks + market (Parquet): the bulk. Volume driven by tick cadence + collector breadth.
  Rough order at the chosen fidelity: tens-to-low-hundreds of MB/day → ~1-3 GB/month range,
  higher with dense trading. Local-only, accepted exposure.
- Disk: C: has ~178 GB free of 464; warehouse already uses ~99 GB. Comfortable for the pilot;
  revisit retention/external drive if it grows.

## Build sequence (phased; each ships offline tests + a live smoke check, zero-transmit asserted)
0. Storage + schemas (data root, trade-record schema, tick/market parquet schema, state store).
1. Rich entry capture (greeks/IV) — builds on today's runner, offline-testable.
2. Streaming shadow-monitor service — the large one (state, reconnect, exit detection, crash recovery).
3. Intraday collector.
4. Gateway + service auto-start tasks.
5. Reporting.

## Risks
1. **Data-line budget (~100 lines, shared account-wide):** open spreads (2 legs each) + ATM
   collector + underlying can bump the cap. Mitigation: recycle a position's lines on exit,
   size the collector band against remaining budget, prioritize legs over collector. Read the
   actual line entitlement off the live gateway before finalizing the collector band.
2. **Crash-safe correctness (hardest):** must resume after a gateway/machine/service drop
   without losing an open position or double-recording an exit. Mitigation: durable state
   written on open, idempotent recording keyed by trade id, reconcile-on-restart; tests that
   kill+restart mid-cycle. Most engineering care goes here.
3. **Greeks settle-delay:** model greeks arrive after a short delay (cause of the observed
   null). Mitigation: subscribe, wait with a bounded timeout, record what's present and flag
   incomplete rather than silently writing nulls.
4. **Scope:** component 3 is a genuine long-running service, comparable to the original 5-stage
   S8 build. Handle via the phased sequence above; do not rush the crash-safe layer.

## Current state / provenance (as of 2026-07-17)
- Live-Trade gateway (lane 3, port 4003) stood up: C:\IBC-Live-Trade\ (StartGatewayLiveTrade.bat
  + config.ini scaffold), transmit-capable login, ReadOnlyApi=yes for the pilot.
- s8_config.ACCOUNT set to U14438624 (trust test account, last-4 8624; chosen from the two
  accounts under the login, the other being individual U5721712). Commit 1ef7ee3.
- Two order-gating bugs fixed (commit 1ef7ee3, v0.16.0): two-account accountSummary filtering
  (filter_account_summary) + margin detection via BuyingPower > NetLiquidation (AccountType
  reports "TRUST", not "MARGIN"). Runner made self-contained for connections/strategies imports
  (commit 25605b6) after finding the venv editable installs still point at the deleted My Drive path.
- Live smoke run PASSED end-to-end (2026-07-17 12:33 CT): connected read-only, read 8624's
  summary (filtered), margin preflight PASSED, live 0DTE chain snapshot, picked Puts-80-$4
  short 7480/long 7445 (35-wide, credit 4.05), logged WOULD HAVE TRANSMITTED, zero transmit.

## Separate open item (not part of this build)
The venv editable installs for connections/strategies point at the deleted pre-2026-07-16
My Drive path, so scheduled scripts across the desk (nightly-monitor, morning-execute,
dailyreport, datacollector) likely fail to import connections. Fix (regenerate editable
installs from C:\TradingDesk) is deferred to Andrew's timeline; the S8 runner was made
self-contained so S8 does not depend on it.
