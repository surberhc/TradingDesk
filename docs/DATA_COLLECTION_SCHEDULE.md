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

> **Updated 2026-07-13 — re-verified live against `Get-ScheduledTask` / `Get-ScheduledTaskInfo`;
> this doc had drifted since 2026-07-02. Corrections made this pass (see inline table/section
> edits for detail):**
> - **TiingoDailyUpdate**: moved 2026-07-09 from a single 4:30 PM trigger to two — 7:00 PM CT
>   (primary) and 8:45 PM CT (reconfirm) — vendor per-ticker publish lag meant some tickers
>   weren't posted yet at 4:30 PM.
> - **AccountMonitorDaily**: now **DISABLED** (2026-07-09, deliberate — paper Gateway kept down
>   to avoid a market-data-entitlement collision with another advisor). `GatewayWatchdog` and
>   `CanslimIbkrPriceGapfill` (gateway-management tasks, not data-collection jobs, so not
>   otherwise in this doc's table) were disabled the same day for the same reason.
> - **CanslimOverlayPipeline / CanslimOverlayWatchdog**: no longer "has never run" — Pipeline's
>   first real run landed 2026-07-02 (rc=0); Watchdog has been firing every 20 min since,
>   rc=0 each time.
> - **Spxw1mCollector**: the §5-P3 "RETIRE?" proposal was acted on — task is now **DISABLED**.
> - **InvesTech Phase1/2**: no longer external/unmounted — the code now lives inside the
>   TradingDesk repo (`investech\phase1_feed\`, `investech\phase2_feed\`, moved in 2026-07-10)
>   and both tasks are now running clean (rc=0), not failing. The InvesTech *project* itself was
>   separately **shelved** 2026-07-10 (Andrew: "we don't need it") — the tasks are still
>   Ready/firing daily, accumulating data passively with no consumer; whether to disable them is
>   still an open question, not yet decided.
> - **UniverseDownloadEod** (not in this doc's original table — started 2026-07-04, after this
>   doc's 2026-07-02 snapshot): stopped 2026-07-10 at ~26.3% (CAN SLIM link disproven; the
>   strangle-basket justification was refuted), task disabled, data on disk preserved not deleted.
> - **Spx1mParallel**: the one-time SPX-root backfill this doc describes as "ACTIVE RIGHT NOW"
>   has since finished and the task itself is **no longer registered** in Task Scheduler at all
>   (confirmed absent from current `Get-ScheduledTask` output) — the sections below describing it
>   as live/running are now historical, not current.
> - **ForwardFillLive / GatewayWatchdogLive** (from `SCHEDULER_PLAN.md`, not in this doc): still
>   not registered as of 2026-07-13, still blocked on IBKR live-side login approval.

---

## 1. Canonical table

| Data produced | Source | Frequency (CT) | Task name | Logon | Self-heal / watchdog | In EOD email? | Status |
|---|---|---|---|---|---|---|---|
| Full-chain EOD option greeks + OI, whole universe (today + ~5d self-heal) → `raw/options/{SYM}/{day}.parquet` | ThetaData local Terminal (25503) | Daily **5:30 PM** | **ThetaEodDaily** | Password (survives logoff) | Idempotent + 5-day look-back self-heal; Terminal kept up by ThetaTerminalWatchdog | **YES** ("Daily Options Grab" / forward section) | **ACTIVE** |
| Keeps ThetaData Terminal alive (TCP-probe 25503, relaunch on death) | — (probe + relaunch) | **At logon + daily 6:00 AM**, resident (RestartCount 999 / 1-min) | **ThetaTerminalWatchdog** | Password | Is the watchdog; singleton lock; self-restarts (999×) | No (indirectly, via forward/GEX freshness) | **ACTIVE** (Running now) |
| Backtester daily dataset: adj-close for universe + UST10y/VIX/HY-credit; rebuilds `data/_manifest.json` | Tiingo API (+FRED) | Daily **7:00 PM** (primary) **+ 8:45 PM** (reconfirm/full re-pull) — moved 2026-07-09 from a single 4:30 PM trigger (vendor per-ticker publish lag) | **TiingoDailyUpdate** | Password | Idempotent re-pull; status JSON | **YES** (Tiingo section) | **ACTIVE** |
| Incremental dealer-gamma (GEX) latest-day features for SPX/SPXW/SPY/QQQ → derived tables | Derived from warehouse parquet (no network) | Daily **7:30 PM** | **GexDailyBuild** | Password | `--latest` re-runnable; reads #ThetaEodDaily output | **YES** (Dealer Gamma section) | **ACTIVE** |
| The EOD digest email itself (reads every job's status artifact) | SMTP/Gmail; local files | Daily **9:00 PM** | **EodReport** | Password | Per-section timeout + fallback email; reciprocal watchdog via HeartbeatStalenessAlarm | **is** the email | **ACTIVE** |
| Read-only per-account cashflow monitor (propose-only; NetLiq/cash/fills for 5 DU subs) → `account_monitor` status JSON + baseline | IBKR paper Gateway 4002 (readonly, clientId 40) | Daily **4:30 PM** (task disabled, does not fire) | **AccountMonitorDaily** | Password | Gateway-lock interlock (SKIPs cleanly under rebalance); writes status JSON | **NO builder wired** (JSON written, never rendered) | **DISABLED** (2026-07-09, deliberate — paper Gateway kept down to avoid a market-data-entitlement collision with another advisor; not a failure) |
| Data-collector heartbeat **staleness alarm** (watches the supervisors themselves) | Reads local heartbeats; emails on cold | **At boot + every 15 min** (daily-with-15m-rep, RestartCount 3) | **HeartbeatStalenessAlarm** | Password | Is the alarm; OS re-fires every 15m; stamps `heartbeat_alarm_ran.txt` | **YES** (Staleness Alarm section, reciprocal) | **ACTIVE** |
| CAN SLIM options-overlay: resumable ThetaData equity-options pull → overlay backtest on real quotes → status JSON + email | ThetaData local Terminal (25503) | Daily **6:00 PM** (catch-up on miss) | **CanslimOverlayPipeline** | **Interactive** (needs login) | Own watchdog + heartbeat + completion flag; emails on done/fail; writes `canslim_overlay_real` status JSON | **NO builder wired** (JSON written, never rendered) + **self-emails independently** | **ACTIVE** — first successful run landed 2026-07-02 (rc=0); last observed run 2026-07-02, rc=0 (see updated §4c) |
| Watchdog for the canslim overlay pipeline (relaunch on death/stall) | — | **Every 20 min from 6:00 PM** | **CanslimOverlayWatchdog** | **Interactive** | Is the watchdog; OS re-fires every 20m | No | **ACTIVE** — firing every 20 min, rc=0 each time as of 2026-07-13 (see updated §4c) |
| **SPXW 1-minute** option backfill (NBBO + OHLC, 2022-01-01..yesterday) | ThetaData local Terminal (25503) | Daily every **30 min from 6:00 AM** + at boot (task disabled, does not fire) | **Spxw1mCollector** | Password | Supervisor + ThetaTerminalWatchdog; singleton lock | No | **DISABLED / RETIRED** — backfill hit 100% COMPLETE (1127/1127 days, 28.8 GB) and the task was disabled per the §5-P3 proposal (see §3) |
| **SPX-root 1-minute** backfill, sharded K=4 (2025-05-01..2026-06-18, ~296 days) | ThetaData local Terminal (25503) | **At logon**, every 5 min (singleton) — task **no longer registered** | **Spx1mParallel** | Interactive | Sharded self-heal supervisor; singleton lock | No | **FINISHED / TASK REMOVED** — backfill completed after this doc's original 2026-07-02 snapshot; confirmed absent from `Get-ScheduledTask` as of 2026-07-13 (see §3) |
| InvesTech newsletter feed (Phase 1) — **now inside TradingDesk** (`investech\phase1_feed\`) | Own fetchers, moved into the TradingDesk repo 2026-07-10 | Daily **10:30 PM** | **InvesTech Phase1 Feed** | Interactive | Unknown; StartWhenAvailable=**False** (no catch-up) | **NO** (project shelved, not wired to EOD) | **RUNNING, rc=0** — project shelved 2026-07-10 ("we don't need it," Andrew's call) but the task is still Ready and firing daily; whether to disable it is an open question (see updated Q2) |
| InvesTech breadth feed (Phase 2) — **now inside TradingDesk** (`investech\phase2_feed\`) | Own fetchers, moved into the TradingDesk repo 2026-07-10 | Daily **4:45 PM** (not 11:00 PM as previously recorded) | **InvesTech Phase2 Breadth** | Interactive | Unknown; StartWhenAvailable=**False** | **NO** | **RUNNING, rc=0** — same shelved-but-still-firing status as Phase 1 (see updated Q2) |

Retired/disabled (not counted above): **ThetaForwardDaily** (disabled; superseded by ThetaEodDaily).
**[2026-07-13 addendum, also now disabled/retired — see rows above + top-of-doc note for
detail]:** `AccountMonitorDaily`, `Spxw1mCollector` (both counted in the table above with
their current status). Also disabled the same day for the gateway-quarantine reason, but
never in this table's original scope since they're gateway-management, not data-collection,
tasks: `GatewayWatchdog`, `CanslimIbkrPriceGapfill`. Also disabled: `UniverseDownloadEod`
(started after this doc's original snapshot; see top-of-doc note).

---

## 2. Daily timeline + contention (shared-resource view)

The scarce shared resources: **(T)** the single ThetaData Terminal on `127.0.0.1:25503`;
**(G)** the IB paper Gateway on `127.0.0.1:4002`; **(CPU/disk)** the box.

```
[2026-07-13: re-verified against live Get-ScheduledTask output; this replaces the original
 2026-07-02 diagram, which had Spxw1mCollector/Spx1mParallel as live/running, Tiingo at
 16:30 only, AccountMonitorDaily as live, InvesTech as external at 22:30/23:00. See the
 dated note at the top of this doc for the full list of what changed and why.]

06:00  ThetaTerminalWatchdog daily arm (Spxw1mCollector arm removed — task disabled)
       ThetaTerminalWatchdog resident all day (probe only)                             (T, light)
       HeartbeatStalenessAlarm every 15 min (reads files, may email)                   (disk)

16:45  InvesTech Phase2 Breadth  (in-repo, shelved project, still fires)               (—)
       [AccountMonitorDaily formerly fired 16:30 — task now DISABLED, does not fire]   (G)
17:30  ThetaEodDaily             (full-chain pull, up to 1h)                            (T)  ★
18:00  CanslimOverlayPipeline    (equity-options pull + backtest, budget up to 8h)     (T)  ★★  ← overlaps ThetaEodDaily's tail
18:00  CanslimOverlayWatchdog    every 20 min (relaunches pipeline if dead)            (—)
19:00  TiingoDailyUpdate         (Tiingo API; primary pull, moved here 2026-07-09)      (network)
19:30  GexDailyBuild             (derived-only, --latest, seconds — NO Terminal)       (disk) ← safe
20:45  TiingoDailyUpdate         (Tiingo API; reconfirm/full re-pull, added 2026-07-09) (network)
21:00  EodReport                 (reads status + emails)                               (SMTP)
22:30  InvesTech Phase1 Feed     (in-repo, shelved project, still fires)               (—)

[Spxw1mCollector: DISABLED/RETIRED, no longer fires. Spx1mParallel: one-time backfill
 finished, task no longer registered at all — see §3 for both.]
```

### Contention findings (VERIFIED FACT as of 2026-07-02; items 2–4 now historical — see
2026-07-13 annotations inline)

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
   **[2026-07-13: HISTORICAL — this backfill finished and the `Spx1mParallel` task is no
   longer registered at all; this risk no longer exists.]**

3. **16:30 double-fire, but no real contention:** TiingoDailyUpdate and AccountMonitorDaily
   both fire at 4:30 PM. Tiingo uses the network only; the monitor uses the Gateway (4002)
   under the **gateway.lock** mutex. Different resources → **no contention.** The gateway lock
   only interlocks the monitor against a human rebalance, which is the correct design.
   **[2026-07-13: HISTORICAL — TiingoDailyUpdate moved to 7:00/8:45 PM (2026-07-09) and
   AccountMonitorDaily is now DISABLED (2026-07-09); there is no 4:30 PM double-fire today.]**

4. **Spxw1mCollector fires every 30 min all day** but is a pure no-op (backfill complete),
   so its Terminal touch is instantaneous — negligible contention, but pure noise.
   **[2026-07-13: HISTORICAL — the task has since been disabled outright (see Q1 below),
   so it no longer fires at all, noise or otherwise.]**

---

## 3. Resolved open questions

**Q1 — Is `Spxw1mCollector` a live daily top-up or a retire-able leftover? → RETIRE (leftover).
DONE — task is now DISABLED (confirmed 2026-07-13).**
VERIFIED: `collect_spxw_1m.py` has a fixed `START_DAY = date(2022,1,1)` and excludes every day
`>= today` (the Terminal 400s on the current session's `expiration=*`), so it can only ever
add *yesterday* — it was architected as a one-time historical backfill, never a rolling feed.
Progress JSON: `days_done 1127 / days_total 1127, pct 100.0, 28.825 GB, errors 0`. Supervisor
heartbeat = `COMPLETE`. The §5-P3 retire proposal below was acted on: `Spxw1mCollector` is
confirmed **State: Disabled** as of 2026-07-13 — it no longer fires at all (not even as a
no-op). **Caveat still stands:** retiring it leaves **no** 1-minute SPXW top-up running; if a
durable rolling 1-min feed is ever wanted, that is a separate deliberate build (the collector
that ran here could never pull "today" anyway).

**Q2 — What are the InvesTech tasks? → EXTERNAL to TradingDesk; not a desk data feed.**
*(2026-07-02 finding, since SUPERSEDED — see 2026-07-13 update immediately below.)*
VERIFIED-AT-THE-TIME: both ran `cmd /c "H:\My Drive\TFR_Ops\Research\InvesTech\{phase1_feed,phase2_feed}\run_feed.cmd"`.
The code lived under **`H:\My Drive\TFR_Ops\...`**, which was **not mounted in that session**, so
the feed source/module/self-heal could not be read. What was certain then: (a) there was **zero**
`investech` reference anywhere in the TradingDesk repo, (b) they were **not** in `EodReport`'s
`SECTIONS`, so they were **not surfaced** in the desk EOD email, (c) both had **last result = 1
(error)** on 2026-07-01, and (d) both had **StartWhenAvailable = False** (a missed window is
simply skipped, no catch-up).

> **2026-07-13 UPDATE — Q2's premise has changed.** As of 2026-07-10 the InvesTech code moved
> **into the TradingDesk repo itself**: `investech\phase1_feed\` and `investech\phase2_feed\`
> (currently untracked in git except `thetadata.py`/`config.py`). This is no longer an
> unreadable external TFR_Ops dependency — it is a folder in this repo. Separately, and for an
> unrelated reason, the InvesTech *project* was **shelved 2026-07-10** ("we don't need it,"
> Andrew's call — see `investech\PROJECT_STATUS.md` and memory `investech-project-shelved`).
> Both tasks are now confirmed **State: Ready** and running clean (**rc=0**, not the old
> result=1 failures — whatever broke them in early July was since fixed, likely incidentally,
> when the ThetaData v2→v3 client port happened before shelving). They fire daily (Phase1
> ~22:30, Phase2 ~16:45) and accumulate data passively with **no consumer**, since the project
> is shelved. This is a known, still-open loose end — **whether to disable the two tasks is an
> open question Andrew has not yet decided**, not a bug to silently fix. `EodReport.SECTIONS`
> still has no builder for either, unchanged.

**Q3 — Is `Spx1mParallel` idle/retired? → NO. It is ACTIVELY RUNNING right now.**
*(2026-07-02 finding, since SUPERSEDED — see 2026-07-13 update immediately below.)*
VERIFIED-AT-THE-TIME (contradicted the prompt's premise): `spx_1m_parallel_progress.json`
(updated 2026-07-02 11:41) showed **4 live shards** (PIDs 12452 / 9084 / 19452 / 19344),
`days_done 124 / 296, pct 41.89`, ETA `~2026-07-02 13:54`, backfilling SPX-root 1-min for
2025-05-01..2026-06-18. `NextRunTime` was blank only because it was a **logon-triggered**
singleton supervisor (logon triggers don't populate NextRun), **not** because it was retired.

> **2026-07-13 UPDATE — the backfill finished and the task is gone.** `Spx1mParallel` no
> longer appears in `Get-ScheduledTask` output at all (confirmed absent, not merely disabled).
> The one-time backfill this section describes completed sometime after 2026-07-02 and the
> task registration was subsequently removed. Nothing here needs action; it is pure history now.

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
| AccountMonitorDaily | gateway-lock SKIP is a clean non-event; next day catches up | no | **GAP — writes `account_monitor` JSON but EOD has no builder for it. [2026-07-13: task DISABLED, doesn't run at all right now — see top-of-doc note]** |
| HeartbeatStalenessAlarm | OS re-fires every 15 min; RestartCount 3 | is the alarm; reciprocal EOD "Staleness Alarm" section watches *it* | **YES** |
| CanslimOverlayPipeline | own watchdog + resume + completion flag; **self-emails** on done/fail | CanslimOverlayWatchdog (every 20 min) | **self-email YES; EOD section GAP** — writes `canslim_overlay_real` JSON but EOD has no builder for it. **[2026-07-13: now has real run history, first success 2026-07-02 rc=0 — see updated 4b]** |
| CanslimOverlayWatchdog | OS re-fires every 20 min | is the watchdog | No. **[2026-07-13: confirmed actively firing every 20 min, rc=0]** |
| Spxw1mCollector | supervisor + Terminal watchdog | yes | No (and now moot — complete). **[2026-07-13: task DISABLED outright]** |
| Spx1mParallel | sharded self-heal supervisor; restarts dead shards; 429 back-off | self-supervising | No (heartbeat only; not in EOD). **[2026-07-13: backfill finished, task no longer registered — this row is historical]** |
| InvesTech Phase1/2 | **now known** — code moved into TradingDesk repo 2026-07-10 (`investech\phase1_feed\`, `investech\phase2_feed\`); StartWhenAvailable=False = no catch-up | **unknown** (in-repo code, self-heal not audited this pass) | **No** (project shelved 2026-07-10, not wired to EOD; tasks still fire — see updated Q2) |

**Coverage GAPS (VERIFIED FACT):**
- **AccountMonitorDaily** and **CanslimOverlayPipeline** both write desk status JSONs
  (`account_monitor`, `canslim_overlay_real`) but **`EodReport.SECTIONS` has no builder that
  renders either key** — so a silent failure of either is invisible in the nightly digest.
  (Canslim partly mitigates this by self-emailing; the account monitor has no independent
  email — its only signal is a JSON nobody reads.)
- ~~**Spx1mParallel** has no EOD line and no staleness-alarm coverage (the alarm watches the
  SPXW supervisor heartbeat, not this one). While it's a finite backfill this matters little,
  but it is currently the heaviest live job with zero surfaced status.~~ **[2026-07-13: MOOT —
  the backfill finished and the task no longer exists; there is nothing left to surface.]**

### 4b. Reliability (logon type, failed runs, missed-window)

- **Logoff survival (as of 2026-07-02):** the Password-logon (run-whether-logged-on) jobs —
  ThetaEodDaily, TiingoDailyUpdate, GexDailyBuild, EodReport, AccountMonitorDaily,
  HeartbeatStalenessAlarm, ThetaTerminalWatchdog, Spxw1mCollector — **survive logoff.** The
  **Interactive** jobs — **CanslimOverlayPipeline, CanslimOverlayWatchdog, Spx1mParallel, and
  both InvesTech feeds** — **only run while a user is logged in.** (VERIFIED FACT.) Not
  re-audited for logon-type changes this pass (2026-07-13) beyond what's noted below —
  AccountMonitorDaily and Spxw1mCollector are now disabled (so their logon-type is moot while
  off) and Spx1mParallel's task no longer exists.
- **Failed / never-run last results (2026-07-02 VERIFIED FACT, since updated where noted):**
  - `EodReport` LastResult = **1** (2026-07-01 21:00) — the report exits 1 when the email did
    not send. Needs a look (SMTP hiccup or a section fail cascaded). The digest is the top of
    the alerting pyramid, so a silent EOD failure is high-severity. *(Not re-checked this pass;
    if still unresolved it remains open.)*
  - ~~`InvesTech Phase1` and `Phase2` LastResult = **1** (external; see Q2).~~ **[2026-07-13:
    SUPERSEDED — both now run clean, rc=0, per `Get-ScheduledTaskInfo`; see updated Q2.]**
  - ~~`CanslimOverlayPipeline` and `CanslimOverlayWatchdog` LastRunTime = `1999-11-30`,
    LastResult = **267011 ("has never run")**.~~ **[2026-07-13: SUPERSEDED — CanslimOverlayPipeline's
    LastRunTime is now 2026-07-02 20:20, LastResult 0 (that 6 PM run was in fact its first real
    run and it succeeded); CanslimOverlayWatchdog has run continuously since, LastRunTime
    2026-07-13 08:40, LastResult 0. Both Interactive-logon, so this confirms the box has stayed
    logged in through their fire times.]**
  - `Spxw1mCollector` LastResult 0, `ThetaEodDaily` 0, `Tiingo` 0, `GEX` 0,
    `AccountMonitor` 0, `HeartbeatStalenessAlarm` 0 — all green (as of 2026-07-02; Spxw1mCollector
    and AccountMonitorDaily are now disabled so no longer accumulate new results).
- **Missed-window handling:** all desk jobs are `StartWhenAvailable = True` (catch up if the
  box was off) **except the two InvesTech feeds** (`False` = a missed night is lost) — not
  re-verified this pass.

### 4c. Gaps (VERIFIED FACT)

- **EDGAR periodic refresh is unscheduled.** `EodReport.build_edgar()` monitors the
  point-in-time fundamentals table and flags **stale after 45 days**, but **no scheduled task
  refreshes it** — it's a manual rebuild. It will silently age into "stale" with nothing to
  fix it.
- **IBKR-capabilities refresh is unscheduled.** Per memory (IBKR-first data sourcing), the
  `connections/IBKR_CAPABILITIES.md` snapshot is meant to be periodically refreshed; there is
  no task doing it.
- **Two status JSONs written but never rendered** (account_monitor, canslim_overlay_real) — see 4a.
- ~~**Spxw1mCollector** fires 48×/day as a pure no-op (see Q1).~~ **[2026-07-13: RESOLVED —
  task disabled, no longer fires at all.]**
- **Canslim + InvesTech are Interactive** (see 4b) — logoff-fragile. (`Spx1mParallel`'s
  interactivity is moot as of 2026-07-13 — its task no longer exists.)

---

## 5. Prioritized bulletproofing fix-list

> Each item is tagged **[FACT]** (verified observation) or **[PROPOSAL — needs Andrew]**
> (a change to make; **not executed** — this doc is read-only + recommend).

**P1 — tonight's live risk (as of 2026-07-02)**
1. ~~**[FACT]** `Spx1mParallel` is running now (4 shards, ETA ~13:54 CT). **[PROPOSAL — needs
   Andrew]** *Do nothing to it* unless it slips past ~4:30 PM...~~ **[2026-07-13: MOOT —
   the backfill finished and the task no longer exists. No action was ever needed beyond
   what's recorded here.]**
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
5. ~~**[FACT]** `Spx1mParallel` has no surfaced status and isn't covered by the staleness alarm.
   **[PROPOSAL — needs Andrew]** While it's a finite backfill, optionally add it to the
   staleness alarm's watch set (or accept it as ephemeral and drop when done).~~ **[2026-07-13:
   MOOT — the backfill finished and the task no longer exists; nothing left to surface.]**

**P3 — retire dead work**
6. **[FACT]** `Spxw1mCollector` backfill is 100% complete (1127/1127, `COMPLETE` heartbeat);
   the task now no-ops 48×/day. **[PROPOSAL — needs Andrew]** **Retire / disable the
   `Spxw1mCollector` scheduled task.** (Per liveness rubric, a task that can never meaningfully
   advance is noise that dilutes real alarms.) Note the caveat: this leaves no rolling 1-min
   SPXW top-up — a separate build if ever wanted.
   **[2026-07-13: DONE — task confirmed State: Disabled.]**

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
   **[2026-07-13 UPDATE: premise partly changed.]** Canslim has since run repeatedly and
   successfully (Pipeline first success 2026-07-02, Watchdog firing every 20 min, both rc=0),
   so the *risk* this item flags (Interactive + logged-off = never runs) hasn't materialized —
   the box has stayed logged in through their fire times — but the underlying fragility is
   unchanged and the proposal still stands if Andrew wants to close the risk permanently.
   `Spx1mParallel`'s task no longer exists (backfill finished), so that clause is moot. The
   InvesTech feeds are **no longer external** (see P6 below) — the "TFR_Ops owner's call"
   framing for them is stale.

**P6 — external, flagged not owned**
10. **[FACT]** `InvesTech Phase1/2` failed (result 1) on 2026-07-01, have `StartWhenAvailable
    = False` (no catch-up), and their code is on the unmounted **H:\** drive — not part of
    TradingDesk. **[PROPOSAL — needs Andrew]** Hand these to the TFR_Ops owner to fix the
    failures + reconsider the no-catch-up setting; out of scope for the desk otherwise.
    **[2026-07-13 UPDATE: premise superseded, see updated Q2 in §3.]** The code now lives
    inside the TradingDesk repo (`investech\phase1_feed\`, `investech\phase2_feed\`, moved
    2026-07-10) — this is a TradingDesk-owned item now, not TFR_Ops's. Both tasks run clean
    (rc=0), so the original failure this item flagged is resolved (incidentally, not by this
    proposal). Separately, the InvesTech *project* was **shelved 2026-07-10** (Andrew: "we
    don't need it") — the two tasks are still Ready and firing daily with no consumer for
    their output. **Open question, not yet decided: disable the two tasks now that the
    project is shelved, or leave them accumulating data passively in case InvesTech is
    revisited?** This replaces the old "hand to TFR_Ops" recommendation.
```
