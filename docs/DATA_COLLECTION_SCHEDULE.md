# TradingDesk — Data Collection Schedule (canonical inventory + bulletproofing audit)

*Generated 2026-07-02 by a READ-ONLY audit. Nothing here was created, modified, enabled,*
*disabled, or run. Every "RECOMMENDED-CHANGE" is a proposal that needs Andrew's approval —*
*do not execute any of them from this doc.*

All times are **machine-local**. The box runs **Central Time (CT)**; Windows Task Scheduler
stores triggers in local time with no per-task timezone. Times shown are the local clock the
task fires.

Sources for every fact below: `Get-ScheduledTask` / `Get-ScheduledTaskInfo` (2026-07-02
~11:40 CT), the `.bat` launchers in `C:\TradingDesk-Local\warehouse\`, the target scripts in
the repo, and the live heartbeat/progress JSONs in the warehouse.

---

## 1. Canonical table

| Data produced | Source | Frequency (CT) | Task name | Logon | Self-heal / watchdog | In EOD email? | Status |
|---|---|---|---|---|---|---|---|
| Full-chain EOD option greeks + OI, whole universe (today + ~5d self-heal) → `raw/options/{SYM}/{day}.parquet` | ThetaData local Terminal (25503) | Daily **5:30 PM** | **ThetaEodDaily** | Password (survives logoff) | Idempotent + 5-day look-back self-heal; Terminal kept up by ThetaTerminalWatchdog | **YES** ("Daily Options Grab" / forward section) | **ACTIVE** |
| Keeps ThetaData Terminal alive (TCP-probe 25503, relaunch on death) | — (probe + relaunch) | **At logon + daily 6:00 AM**, resident (RestartCount 999 / 1-min) | **ThetaTerminalWatchdog** | Password | Is the watchdog; singleton lock; self-restarts (999×) | No (indirectly, via forward/GEX freshness) | **ACTIVE** (Running now) |
| Backtester daily dataset: adj-close for universe + UST10y/VIX/HY-credit; rebuilds `data/_manifest.json` | Tiingo API (+FRED) | Daily **4:30 PM** | **TiingoDailyUpdate** | Password | Idempotent re-pull; status JSON | **YES** (Tiingo section) | **ACTIVE** |
| Incremental dealer-gamma (GEX) latest-day features for SPX/SPXW/SPY/QQQ → derived tables | Derived from warehouse parquet (no network) | Daily **7:30 PM** | **GexDailyBuild** | Password | `--latest` re-runnable; reads #ThetaEodDaily output | **YES** (Dealer Gamma section) | **ACTIVE** |
| The EOD digest email itself (reads every job's status artifact) | SMTP/Gmail; local files | Daily **9:00 PM** | **EodReport** | Password | Per-section timeout + fallback email; reciprocal watchdog via HeartbeatStalenessAlarm | **is** the email | **ACTIVE** |
| Read-only per-account cashflow monitor (propose-only; NetLiq/cash/fills for 5 DU subs) → `account_monitor` status JSON + baseline | IBKR paper Gateway 4002 (readonly, clientId 40) | Daily **4:30 PM** | **AccountMonitorDaily** | Password | Gateway-lock interlock (SKIPs cleanly under rebalance); writes status JSON | **NO builder wired** (JSON written, never rendered) | **ACTIVE** (gap: not surfaced) |
| Data-collector heartbeat **staleness alarm** (watches the supervisors themselves) | Reads local heartbeats; emails on cold | **At boot + every 15 min** (daily-with-15m-rep, RestartCount 3) | **HeartbeatStalenessAlarm** | Password | Is the alarm; OS re-fires every 15m; stamps `heartbeat_alarm_ran.txt` | **YES** (Staleness Alarm section, reciprocal) | **ACTIVE** |
| CAN SLIM options-overlay: resumable ThetaData equity-options pull → overlay backtest on real quotes → status JSON + email | ThetaData local Terminal (25503) | Daily **6:00 PM** (catch-up on miss) | **CanslimOverlayPipeline** | **Interactive** (needs login) | Own watchdog + heartbeat + completion flag; emails on done/fail; writes `canslim_overlay_real` status JSON | **NO builder wired** (JSON written, never rendered) + **self-emails independently** | **ACTIVE** (has never successfully run — see §4c) |
| Watchdog for the canslim overlay pipeline (relaunch on death/stall) | — | **Every 20 min from 6:00 PM** | **CanslimOverlayWatchdog** | **Interactive** | Is the watchdog; OS re-fires every 20m | No | **ACTIVE** (has never run — see §4c) |
| **SPXW 1-minute** option backfill (NBBO + OHLC, 2022-01-01..yesterday) | ThetaData local Terminal (25503) | Daily every **30 min from 6:00 AM** + at boot | **Spxw1mCollector** | Password | Supervisor + ThetaTerminalWatchdog; singleton lock | No | **RETIRE?** — backfill 100% COMPLETE (1127/1127 days, 28.8 GB); now a pure no-op every 30 min (see §3) |
| **SPX-root 1-minute** backfill, sharded K=4 (2025-05-01..2026-06-18, ~296 days) | ThetaData local Terminal (25503) | **At logon**, every 5 min (singleton) | **Spx1mParallel** | Interactive | Sharded self-heal supervisor; singleton lock | No | **ACTIVE RIGHT NOW** — NOT idle. ~42% done, 4 shards live, ETA ~13:54 today (see §3) |
| InvesTech newsletter feed (Phase 1) — **external to TradingDesk** | (unknown — code on unmounted **H:\**) | Daily **10:30 PM** | **InvesTech Phase1 Feed** | Interactive | Unknown; StartWhenAvailable=**False** (no catch-up) | **NO** (not a TradingDesk job) | **EXTERNAL / failing** (last result = 1) |
| InvesTech breadth feed (Phase 2) — **external to TradingDesk** | (unknown — code on unmounted **H:\**) | Daily **11:00 PM** | **InvesTech Phase2 Breadth** | Interactive | Unknown; StartWhenAvailable=**False** | **NO** | **EXTERNAL / failing** (last result = 1) |

Retired/disabled (not counted above): **ThetaForwardDaily** (disabled; superseded by ThetaEodDaily).

---

## 2. Daily timeline + contention (shared-resource view)

The scarce shared resources: **(T)** the single ThetaData Terminal on `127.0.0.1:25503`;
**(G)** the IB paper Gateway on `127.0.0.1:4002`; **(CPU/disk)** the box.

```
06:00  ThetaTerminalWatchdog daily arm + Spxw1mCollector arm (both no-op most days)
06:00→ Spxw1mCollector fires every 30 min all day — NO-OP (backfill complete)          (T, but instant)
all-day Spx1mParallel (logon) — 4 SHARDS BURNING THE TERMINAL until ~13:54 today       (T, heavy)
       ThetaTerminalWatchdog resident (probe only)                                     (T, light)
       HeartbeatStalenessAlarm every 15 min (reads files, may email)                   (disk)

16:30  TiingoDailyUpdate         (Tiingo API; ~mins)                                    (network)
16:30  AccountMonitorDaily       (Gateway 4002 read-only, holds gateway.lock)          (G)   ← same minute, different resource
17:30  ThetaEodDaily             (full-chain pull, up to 1h)                            (T)  ★
18:00  CanslimOverlayPipeline    (equity-options pull + backtest, budget up to 8h)     (T)  ★★  ← overlaps ThetaEodDaily's tail
18:00  CanslimOverlayWatchdog    every 20 min (relaunches pipeline if dead)            (—)
19:30  GexDailyBuild             (derived-only, --latest, seconds — NO Terminal)       (disk) ← safe
21:00  EodReport                 (reads status + emails)                               (SMTP)
22:30  InvesTech Phase1 (external)
23:00  InvesTech Phase2 (external)
```

### Contention findings (VERIFIED FACT)

1. **★ ThetaEodDaily (5:30 PM) → CanslimOverlayPipeline (6:00 PM) on the same Terminal.**
   ThetaEodDaily's ExecutionTimeLimit is **1 h** (can run to 6:30 PM). Canslim starts at
   6:00 PM. So there is a **guaranteed ~30-min window where both hit the one Terminal at
   once** if the EOD grab runs long. Neither aborts the other (both retry on timeout), but
   this slows both and raises stall risk. GexDailyBuild at 7:30 PM is `--latest` derived-only
   and does **not** touch the Terminal, so despite the prompt's framing it is **not** a
   Terminal contender — the real 3-way "Terminal at night" is ThetaEodDaily + Canslim only.

2. **★★ Spx1mParallel is running RIGHT NOW and hammering the Terminal with 4 concurrent
   shards** (measured throughput knee K=4). It is scheduled to finish ~13:54 today, well
   before the 5:30 PM Terminal jobs — **so tonight is clear IF it finishes on time.** But if
   it slips past 5:30 PM (vendor slowness, restart), it will collide with ThetaEodDaily and
   then Canslim on the same Terminal — a **3-way Terminal contention** (Spx1mParallel ×4 +
   ThetaEodDaily + Canslim). This is the single biggest live risk today.

3. **16:30 double-fire, but no real contention:** TiingoDailyUpdate and AccountMonitorDaily
   both fire at 4:30 PM. Tiingo uses the network only; the monitor uses the Gateway (4002)
   under the **gateway.lock** mutex. Different resources → **no contention.** The gateway lock
   only interlocks the monitor against a human rebalance, which is the correct design.

4. **Spxw1mCollector fires every 30 min all day** but is a pure no-op (backfill complete),
   so its Terminal touch is instantaneous — negligible contention, but pure noise.

---

## 3. Resolved open questions

**Q1 — Is `Spxw1mCollector` a live daily top-up or a retire-able leftover? → RETIRE (leftover).**
VERIFIED: `collect_spxw_1m.py` has a fixed `START_DAY = date(2022,1,1)` and excludes every day
`>= today` (the Terminal 400s on the current session's `expiration=*`), so it can only ever
add *yesterday* — it was architected as a one-time historical backfill, never a rolling feed.
Progress JSON: `days_done 1127 / days_total 1127, pct 100.0, 28.825 GB, errors 0`. Supervisor
heartbeat = `COMPLETE`. Each 30-min fire now spawns a supervisor + collector that both
immediately see 100%-done and exit 0. It is dead scheduled work. **Caveat:** retiring it leaves
**no** 1-minute SPXW top-up running; if a durable rolling 1-min feed is ever wanted, that is a
separate deliberate build (the current collector cannot pull "today").

**Q2 — What are the InvesTech tasks? → EXTERNAL to TradingDesk; not a desk data feed.**
VERIFIED: both run `cmd /c "H:\My Drive\TFR_Ops\Research\InvesTech\{phase1_feed,phase2_feed}\run_feed.cmd"`.
The code lives under **`H:\My Drive\TFR_Ops\...`**, which is **not mounted in this session**, so
the feed source/module/self-heal could not be read. What is certain: (a) there is **zero**
`investech` reference anywhere in the TradingDesk repo, (b) they are **not** in `EodReport`'s
`SECTIONS`, so they are **not surfaced** in the desk EOD email, (c) both had **last result = 1
(error)** on 2026-07-01, and (d) both have **StartWhenAvailable = False** (a missed window is
simply skipped, no catch-up). These belong to the **TFR_Ops** project, not TradingDesk — they
share the machine only. Recommend treating them as out-of-scope for the desk, but flag the
repeated failures + no-catch-up to whoever owns TFR_Ops. **Likely cause of result=1
(hypothesis, unverified):** the `H:` (TFR_Ops Google Drive) account is not mounted at run
time — this machine currently exposes only C/G/J drives and `H:\...\InvesTech` = path-not-found
— so `run_feed.cmd` cannot find its own working directory. Diagnosing further requires mounting
that account.

**Q3 — Is `Spx1mParallel` idle/retired? → NO. It is ACTIVELY RUNNING right now.**
VERIFIED (contradicts the prompt's premise): `spx_1m_parallel_progress.json` (updated
2026-07-02 11:41) shows **4 live shards** (PIDs 12452 / 9084 / 19452 / 19344), `days_done 124 /
296, pct 41.89`, ETA `~2026-07-02 13:54`, backfilling SPX-root 1-min for 2025-05-01..2026-06-18.
`NextRunTime` is blank only because it is a **logon-triggered** singleton supervisor (logon
triggers don't populate NextRun), **not** because it is retired. Its `LastResult = 267011`
("has not run via the scheduler") means this instance was launched **manually this session**,
not by the logon task. It is a one-time backfill nearing completion — **do not kill it**; it is
Terminal-heavy and finishing today.

---

## 4. Bulletproofing audit

### 4a. Self-heal / watchdog coverage matrix

| Job | Retries/self-heal on failure? | Watchdogged? | Failure surfaced? |
|---|---|---|---|
| ThetaEodDaily | 5-day look-back self-heal + per-root tolerance | Terminal by ThetaTerminalWatchdog | **YES** — EOD forward section |
| ThetaTerminalWatchdog | self-restarts 999× / 1-min; singleton | is the watchdog | No direct line (its effect shows in downstream freshness) |
| TiingoDailyUpdate | idempotent re-pull next day | no dedicated watchdog | **YES** — Tiingo section |
| GexDailyBuild | `--latest` re-runnable | no | **YES** — Dealer Gamma section (freshness) |
| EodReport | per-section timeout + fallback error email | reciprocal via HeartbeatStalenessAlarm | is the email; writes `eod_report` status |
| AccountMonitorDaily | gateway-lock SKIP is a clean non-event; next day catches up | no | **GAP — writes `account_monitor` JSON but EOD has no builder for it** |
| HeartbeatStalenessAlarm | OS re-fires every 15 min; RestartCount 3 | is the alarm; reciprocal EOD "Staleness Alarm" section watches *it* | **YES** |
| CanslimOverlayPipeline | own watchdog + resume + completion flag; **self-emails** on done/fail | CanslimOverlayWatchdog (every 20 min) | **self-email YES; EOD section GAP** — writes `canslim_overlay_real` JSON but EOD has no builder for it |
| CanslimOverlayWatchdog | OS re-fires every 20 min | is the watchdog | No |
| Spxw1mCollector | supervisor + Terminal watchdog | yes | No (and now moot — complete) |
| Spx1mParallel | sharded self-heal supervisor; restarts dead shards; 429 back-off | self-supervising | No (heartbeat only; not in EOD) |
| InvesTech Phase1/2 | **unknown** (H: unmounted); StartWhenAvailable=False = no catch-up | **unknown** | **No** (external) |

**Coverage GAPS (VERIFIED FACT):**
- **AccountMonitorDaily** and **CanslimOverlayPipeline** both write desk status JSONs
  (`account_monitor`, `canslim_overlay_real`) but **`EodReport.SECTIONS` has no builder that
  renders either key** — so a silent failure of either is invisible in the nightly digest.
  (Canslim partly mitigates this by self-emailing; the account monitor has no independent
  email — its only signal is a JSON nobody reads.)
- **Spx1mParallel** has no EOD line and no staleness-alarm coverage (the alarm watches the
  SPXW supervisor heartbeat, not this one). While it's a finite backfill this matters little,
  but it is currently the heaviest live job with zero surfaced status.

### 4b. Reliability (logon type, failed runs, missed-window)

- **Logoff survival:** the Password-logon (run-whether-logged-on) jobs — ThetaEodDaily,
  TiingoDailyUpdate, GexDailyBuild, EodReport, AccountMonitorDaily, HeartbeatStalenessAlarm,
  ThetaTerminalWatchdog, Spxw1mCollector — **survive logoff.** The **Interactive** jobs —
  **CanslimOverlayPipeline, CanslimOverlayWatchdog, Spx1mParallel, and both InvesTech feeds** —
  **only run while a user is logged in.** (VERIFIED FACT.)
- **Failed / never-run last results (VERIFIED FACT):**
  - `EodReport` LastResult = **1** (2026-07-01 21:00) — the report exits 1 when the email did
    not send. Needs a look (SMTP hiccup or a section fail cascaded). The digest is the top of
    the alerting pyramid, so a silent EOD failure is high-severity.
  - `InvesTech Phase1` and `Phase2` LastResult = **1** (external; see Q2).
  - `CanslimOverlayPipeline` and `CanslimOverlayWatchdog` LastRunTime = `1999-11-30`,
    LastResult = **267011 ("has never run")**. Next fire 2026-07-02 18:00 will be their first
    real run. Because they are **Interactive**, they will only run if the box is logged in at
    6 PM — untested and unproven.
  - `Spxw1mCollector` LastResult 0, `ThetaEodDaily` 0, `Tiingo` 0, `GEX` 0,
    `AccountMonitor` 0, `HeartbeatStalenessAlarm` 0 — all green.
- **Missed-window handling:** all desk jobs are `StartWhenAvailable = True` (catch up if the
  box was off) **except the two InvesTech feeds** (`False` = a missed night is lost).

### 4c. Gaps (VERIFIED FACT)

- **EDGAR periodic refresh is unscheduled.** `EodReport.build_edgar()` monitors the
  point-in-time fundamentals table and flags **stale after 45 days**, but **no scheduled task
  refreshes it** — it's a manual rebuild. It will silently age into "stale" with nothing to
  fix it.
- **IBKR-capabilities refresh is unscheduled.** Per memory (IBKR-first data sourcing), the
  `connections/IBKR_CAPABILITIES.md` snapshot is meant to be periodically refreshed; there is
  no task doing it.
- **Two status JSONs written but never rendered** (account_monitor, canslim_overlay_real) — see 4a.
- **Spxw1mCollector** fires 48×/day as a pure no-op (see Q1).
- **Canslim + Spx1mParallel + InvesTech are Interactive** (see 4b) — logoff-fragile.

---

## 5. Prioritized bulletproofing fix-list

> Each item is tagged **[FACT]** (verified observation) or **[PROPOSAL — needs Andrew]**
> (a change to make; **not executed** — this doc is read-only + recommend).

**P1 — tonight's live risk**
1. **[FACT]** `Spx1mParallel` is running now (4 shards, ETA ~13:54 CT). **[PROPOSAL — needs Andrew]**
   *Do nothing to it* unless it slips past ~4:30 PM; if it's still running near 5:00 PM,
   decide whether to let it collide with ThetaEodDaily/Canslim on the Terminal or pause it
   until after the 5:30/6:00 PM jobs. (Leave it alone if it finishes on schedule.)
2. **[FACT]** `ThetaEodDaily` (1h budget from 5:30) and `CanslimOverlayPipeline` (6:00) can
   overlap on the one Terminal for ~30 min. **[PROPOSAL — needs Andrew]** Stagger Canslim to
   start **after ThetaEodDaily reliably completes** (e.g. 6:45–7:00 PM) so the nightly Terminal
   jobs are strictly serialized. (Do not change without blessing — it's an order-of-operations
   change to a scheduled task.)

**P2 — surfacing / alerting gaps**
3. **[FACT]** `EodReport` last run exited **1** (email not sent). **[PROPOSAL — needs Andrew]**
   Investigate the 2026-07-01 EOD log (`C:\TradingDesk-Local\state\dailyreport\eod_report.log`)
   and the mailer path; a silent digest failure blinds the whole alerting pyramid.
4. **[FACT]** `account_monitor` and `canslim_overlay_real` status JSONs are written but have no
   `EodReport` builder. **[PROPOSAL — needs Andrew]** Add two section builders to
   `dailyreport/eod_report.py SECTIONS` so both surface in the digest (canslim already
   self-emails; the account monitor has no other signal). Low-risk, additive.
5. **[FACT]** `Spx1mParallel` has no surfaced status and isn't covered by the staleness alarm.
   **[PROPOSAL — needs Andrew]** While it's a finite backfill, optionally add it to the
   staleness alarm's watch set (or accept it as ephemeral and drop when done).

**P3 — retire dead work**
6. **[FACT]** `Spxw1mCollector` backfill is 100% complete (1127/1127, `COMPLETE` heartbeat);
   the task now no-ops 48×/day. **[PROPOSAL — needs Andrew]** **Retire / disable the
   `Spxw1mCollector` scheduled task.** (Per liveness rubric, a task that can never meaningfully
   advance is noise that dilutes real alarms.) Note the caveat: this leaves no rolling 1-min
   SPXW top-up — a separate build if ever wanted.

**P4 — unscheduled periodic refreshes**
7. **[FACT]** EDGAR fundamentals will go "stale" at 45 days with nothing to refresh it.
   **[PROPOSAL — needs Andrew]** Schedule a **monthly** EDGAR rebuild (well before the 45-day
   threshold) so the EOD "EDGAR Fundamentals" section stays green on its own.
8. **[FACT]** No task refreshes the IBKR-capabilities snapshot. **[PROPOSAL — needs Andrew]**
   Schedule a periodic (monthly/quarterly) `IBKR_CAPABILITIES.md` refresh.

**P5 — logoff fragility**
9. **[FACT]** `CanslimOverlayPipeline`, `CanslimOverlayWatchdog`, `Spx1mParallel`, and both
   InvesTech feeds are **Interactive** — they don't run when logged off, and Canslim has never
   yet run. **[PROPOSAL — needs Andrew]** Harden the two **canslim** tasks to
   **run-whether-logged-on (Password logon)** — this requires storing the Windows account
   password on the task (an explicit credential action, Andrew's call). Spx1mParallel is a
   finishing one-off (leave as-is); the InvesTech feeds are external (TFR_Ops owner's call).

**P6 — external, flagged not owned**
10. **[FACT]** `InvesTech Phase1/2` failed (result 1) on 2026-07-01, have `StartWhenAvailable
    = False` (no catch-up), and their code is on the unmounted **H:\** drive — not part of
    TradingDesk. **[PROPOSAL — needs Andrew]** Hand these to the TFR_Ops owner to fix the
    failures + reconsider the no-catch-up setting; out of scope for the desk otherwise.
```
