# TradingDesk Scheduler — Job Inventory + Unification Plan

*Last updated: 2026-06-30 · Status: PLAN + account monitor now LIVE. This is the single organizing
home for everything on the desk that runs on a clock. Nothing here changes a task,
launcher, or script — it is a ground-truth inventory (read from Windows Task Scheduler
and the repo) plus a design plan for unifying scheduling and surfacing it in the GUI.*

> **All times are stored in Windows Task Scheduler as LOCAL machine time.** The machine
> runs on Central Time (CT), so the "Trigger (CT)" column below is the local clock time
> the task fires. If the machine's timezone ever changes, every trigger shifts with it —
> there is no per-task timezone setting. Flagged as an open question below.

---

## 1. Inventory (lead here — one row per job)

Nine TradingDesk jobs are registered in Windows Task Scheduler today (8 enabled, 1
disabled/retired). All launch a `.bat` wrapper in `C:\TradingDesk-Local\warehouse\`,
which calls the venv python (`C:\TradingDesk-Local\venv\Scripts\pythonw.exe`, or the base
Python 3.12 for the two self-healing supervisors) against a script in the Drive repo.
The **account monitor** (#7) is now LIVE — it was the last held job; the gateway-lock
interlock (it SKIPs cleanly if a rebalance holds the lock) made scheduling it safe.

| # | Job (Task name) | What it does (plain English) | Trigger / cadence (CT) | External services it touches | Defined where | Status |
|---|-----------------|------------------------------|------------------------|------------------------------|---------------|--------|
| 1 | **Spxw1mCollector** | Launches the self-healing SPXW **1-minute** option collector supervisor. Pulls 1-min NBBO quotes + OHLC for every SPXW contract, one trading day at a time, resumable, until the backfill window is done. Singleton-locked. | Daily **6:00 AM**, and at logon | **ThetaData LOCAL Terminal** (REST gateway `127.0.0.1:25503`) → which itself streams from ThetaData's cloud subscription | TS `Spxw1mCollector` → `run_spxw_1m.bat` → `datacollector\spxw_1m_supervisor.py` (→ `collect_spxw_1m.py`) | **LIVE** |
| 2 | **ThetaTerminalWatchdog** | Keeps the ThetaData Terminal alive. TCP-probes port 25503; if down past a debounce, relaunches `start_terminal.py` (never two). The collector's supervisor restarts the *collector*; this restarts the *terminal*. | Daily **6:00 AM**, and at logon | **ThetaData LOCAL Terminal** port `25503` (probe + relaunch only) | TS `ThetaTerminalWatchdog` → `run_theta_watchdog.bat` → `datacollector\theta_terminal_watchdog.py` | **LIVE** |
| 3 | **TiingoDailyUpdate** | Runs the backtester's data downloader: pulls every universe ticker's daily adjusted close plus 10y Treasury / VIX / HY-credit, rewrites per-ticker parquet, rebuilds `data/_manifest.json`, writes a status JSON for the EOD report. | Daily **4:30 PM** | **Tiingo API** (HTTPS), **FRED API** (optional, for some series) — keys loaded from off-Drive `C:\TradingDesk-Local\secrets\.env` | TS `TiingoDailyUpdate` → `run_tiingo.bat` → `dailyreport\tiingo_daily.py` (→ backtester `src/download_data.py`) | **LIVE** |
| 4 | **ThetaEodDaily** | One daily EOD pass of the **full-chain** option collector (EOD greeks + open interest, full universe, today + ~5-day self-heal). Writes the per-(root,day) parquet warehouse. **Replaced** the IBKR forward collector (#8). | Daily **5:30 PM** | **ThetaData LOCAL Terminal** port `25503` (full-chain pull) | TS `ThetaEodDaily` → `run_eoddaily.bat` → `datacollector\eod_daily.py` | **LIVE** |
| 5 | **GexDailyBuild** | Fast **incremental** dealer-gamma (GEX) rebuild — appends only the newest day's gamma features for SPX/SPXW/SPY/QQQ to the derived tables so the 9 PM report's "Dealer Gamma" section is fresh. `--latest` only (seconds), not the heavy full rebuild. | Daily **7:30 PM** | **Local warehouse parquet only** (reads files written by #4; no network). *Note: a full rebuild would hit the Terminal, but `--latest` does not.* | TS `GexDailyBuild` → `run_gex.bat` → `datacollector\build_features.py --latest` | **LIVE** |
| 6 | **EodReport** | The end-of-day digest. Runs LAST — reads every job's status artifact (status JSONs + heartbeats + Tiingo manifest) and emails ONE summary. Re-runs nothing; a crashed job shows red, never takes the report down. | Daily **9:00 PM** | **SMTP / Gmail** (`smtp.gmail.com:587`, STARTTLS) to send the email; reads local files only otherwise | TS `EodReport` → `run_eod.bat` → `dailyreport\eod_report.py` (→ `mailer.py`) | **LIVE** |
| 7 | **AccountMonitorDaily** | Per-account cashflow monitor. **READ-ONLY / PROPOSE-ONLY** connect to the IBKR paper Gateway (clientId 40), reads NetLiquidation / TotalCashValue / SettledCashByDate + today's fills for the 5 enrolled DU sub-accounts, compares against a saved baseline + operator earmarks, and PRINTS a verdict (deposit detected → rebalance nudge; sale-raised cash → nudge; earmarked cash → fenced; else HOLD/IN_BAND). Acquires the single-process **gateway lock** for the whole read-only session and **SKIPs cleanly** if a rebalance holds it (F2 interlock). Persists only a local baseline file. **Transmits nothing** — no order, no whatIfOrder, no FA/gateway config write. | Daily **4:30 PM** | **IBKR paper Gateway** (`127.0.0.1:4002`, clientId 40, `readonly=True`); auto-launches the Gateway read-only if down | TS `AccountMonitorDaily` → `run_account_monitor.bat` → `paperbot\account_monitor_run.py` (+ pure core `account_monitor.py`) | **LIVE** |
| 8 | **ThetaForwardDaily** | Old IBKR forward option collector (band-limited ±50-strike snapshot through the live Gateway). **Retired** — superseded by #4 (full-chain ThetaData pull, no Gateway needed). Left disabled for reference. | *Disabled* (was daily 5:30 PM) | (was: **IBKR paper Gateway** port 4002) | TS `ThetaForwardDaily` (Disabled) → `run_forward.bat` → `datacollector\forward_daily.py` | **HELD / retired** |
| 9 | **ForwardFillLive** | New nightly EOD forward-fill of option chains via the SECOND, independent IBKR Gateway connection added for market-data gathering — same universe/root as the retired IBKR forward collector (#8), just repointed at the new, restricted, read-only-only personal-live-account connection (port 4001) instead of the paper Gateway. Cannot place, modify, or cancel an order — the connection module has no order-placement surface by construction. Writes its own jobstatus key (`forward_live`), distinct from #8's retired `forward` key so the two can never collide. | Daily, mirrors #8's old slot (was 5:30 PM) — **not yet scheduled** | **IBKR live-data Gateway** `127.0.0.1:4001` (restricted, read-only-only, single personal account) | (planned) TS `ForwardFillLive` → `run_forward_live.bat` → `datacollector\forward_daily_live.py` (→ `ibkr_forward_live.py`) | **STAGED / NOT YET REGISTERED** |
| 10 | **GatewayWatchdogLive** | New sibling watchdog for the live-data Gateway (port 4001): detects a wedged/down instance and recovers it by killing the stuck process and bringing up exactly one fresh instance, with the same hard rate-limiting so it can never become a hot loop. Must run **elevated** — the Gateway process runs elevated and a non-elevated kill silently no-ops. Reuses the existing paper-side watchdog's kill/helper internals, scoped to this instance (port 4001, `C:\IBC-Live`); its own state file keeps the two watchdogs' state from ever colliding. | Every 5 min (mirrors the existing paper-side Gateway watchdog's cadence) — **not yet scheduled** | **IBKR live-data Gateway** `127.0.0.1:4001` (restricted, read-only-only, single personal account) | (planned) TS `GatewayWatchdogLive` (elevated) → (bat wrapper TBD, mirrors the paper-side watchdog's launcher) → `connections\connections\gateway_watchdog_live.py` | **STAGED / NOT YET REGISTERED** |

> **ForwardFillLive + GatewayWatchdogLive are STAGED, NOT YET REGISTERED (2026-07-10).** Both
> rows document a new, independent, read-only-only IBKR Gateway connection (port 4001, a
> separate, deliberately access-restricted personal live account) added purely for market-data
> gathering — paperbot execution is untouched and remains exclusively on the paper Gateway
> (port 4002). Neither task is in Windows Task Scheduler yet; registration is pending Andrew
> physically standing up the second IBC/Gateway install (`C:\IBC-Live`). This is purely
> additive — no existing task in the inventory above changed.

> **Account monitor stand-up is DONE (2026-06-30).** The account monitor (#7) is now
> registered as `AccountMonitorDaily`, daily 4:30 PM CT, read-only / propose-only. The F2
> Gateway-sharing question that held it (see below) is resolved by the **gateway lock**: the
> monitor acquires the single-process mutex for its whole read-only session and SKIPs the
> cycle cleanly if a rebalance holds the lock, so it can never read mid-rebalance state. The
> remaining open questions below (orchestration, alerting) are about the *rest* of the chain,
> not the monitor.

### Related infra not on Task Scheduler (context)
- **`supervisor.py` / `run_supervisor.bat`** — the original one-time ThetaData warehouse-grab
  supervisor. The `.bat` exists; there is **no live `ThetaDataSupervisor` task** in the
  current Task Scheduler list (the one-time backfill it served is complete). Listed here so
  it is not mistaken for a live job.
- **`dailyreport\RRG_sched_check.bat`** — leftover from the retired RRG pipeline; not a
  current TradingDesk job.
- **Dashboard** (`dashboard\run_dashboard.bat` / `launch_dashboard.bat`) — the Streamlit
  monitor; launched on demand, not on a clock.

---

## 2. Timing / conflict map (CT)

```
 4:30 PM  TiingoDailyUpdate ........ Tiingo + FRED (cloud)          [data pull]
 4:30 PM  AccountMonitorDaily ...... IBKR paper Gateway :4002       [read-only, gateway-lock-guarded]
 5:30 PM  ThetaEodDaily ............ ThetaData Terminal :25503      [data pull, full chain]
 7:30 PM  GexDailyBuild ............ local parquet (reads #4)       [derive]
 9:00 PM  EodReport ................ SMTP (reads everyone's status) [report — runs LAST]
   (the monitor shares the 4:30 slot with Tiingo but touches a different service — Tiingo
    hits the cloud, the monitor the IBKR Gateway — so no contention; the gateway lock makes
    it safe even if a rebalance is in flight: it SKIPs that cycle, see F2)

 6:00 AM  Spxw1mCollector + ThetaTerminalWatchdog (both, + at logon)  [intraday data infra]
 all day  ThetaTerminalWatchdog probes :25503 continuously; Spxw1mCollector runs resumably
```

The evening chain is **deliberately ordered**: pull data (4:30, 5:30) → derive (7:30) →
report (9:00). That ordering is real and load-bearing — see D1 below.

### Conflict / contention flags

- **F1 — ThetaData Terminal (`:25503`) is shared by three consumers.** The 1-min
  collector (#1, all day), the EOD full-chain pull (#4, 5:30 PM), and the watchdog (#2,
  continuous) all hit the same single local Terminal. **Today this is tolerated, not
  coordinated:** `eod_daily.py` explicitly notes it shares the Terminal and so tolerates
  timeouts/retries; the client already retries; one bad root never aborts a run. Risk:
  a heavy 5:30 EOD pull competing with an active 1-min pull could slow both. No hard
  collision (they write different trees), but it is the busiest shared resource — a unified
  scheduler should at minimum *know* both are using it.
- **F2 — IBKR Gateway / clientId contention (RESOLVED by the gateway lock).** The
  account monitor (#7) connects to the paper Gateway on clientId 40, read-only. The
  **rebalance path** (the gated executor, clientId 38) and any forward-collector revival
  (#8, old clientId) also use the Gateway. **Monitor and rebalance are mutually exclusive
  on the Gateway** — a rebalance is a transmit operation and the monitor must not read
  mid-flight account state that is changing under it. This is now **enforced**, not just a
  rule of thumb: the monitor acquires the single-process **gateway lock**
  (`C:\TradingDesk-Local\state\paperbot\gateway.lock`) for its entire read-only session, and
  because it is automated + read-only it yields to a human rebalance — `on_busy="skip"` means
  if a rebalance holds the lock the monitor waits briefly then **SKIPs the cycle cleanly** (a
  non-event; the next day's run catches up). This interlock is what unblocked scheduling the
  monitor (was the central reason it was HELD).
- **F3 — Ordering dependency: data must finish before derive/report.** GexDailyBuild
  (7:30) reads the parquet that ThetaEodDaily (5:30) writes; EodReport (9:00) reads the
  status artifacts all upstream jobs write. The 2-hour gaps are generous buffers, but they
  are **implicit** — nothing enforces "5:30 finished before 7:30 starts." A slow/failed
  5:30 pull silently degrades the 7:30 derive and the 9:00 report (which correctly shows
  the section stale rather than crashing). A unified orchestrator could make this a real
  dependency edge instead of a hopeful gap.
- **F4 — External-API rate limits.** Tiingo (free tier) and FRED both rate-limit;
  `tiingo_daily.py` already paces for the free tier and is resumable. No conflict today
  (one daily call), but any added cadence (e.g. intraday refresh) must respect these.
- **F5 — Gmail / SMTP send.** Single daily send; well under any limit. Failure mode is a
  silent non-delivery — the report has no second channel. Noted under alerting (D4).
- **F6 — Logon double-fire.** #1 and #2 are registered both "Daily 6:00 AM" *and* "at
  logon." Both scripts are **singleton-locked** (PID/heartbeat locks), so a logon shortly
  after 6 AM cannot spawn a duplicate — by design. Worth keeping in mind if the launch
  convention is ever refactored.

---

## 3. Open design questions

- **D1 — One unified orchestrator vs. independent Task Scheduler entries.** Today: 8
  independent Windows tasks, coordinated only by hand-tuned clock offsets and generous
  gaps. *Pro of staying independent:* dead simple, survives reboot/logon natively, each
  job is isolated (one crash can't cascade). *Con:* ordering (F3) and resource sharing
  (F1, F2) are implicit, not enforced; there's no single place to see "did the whole
  evening chain succeed." *A middle path* — keep Task Scheduler as the trigger but route
  the evening chain through one thin "evening runner" that enforces order and can refuse to
  start the monitor while a rebalance holds the Gateway — captures most of the benefit
  without giving up reboot-resilience. **Recommendation: evaluate the middle path; do not
  collapse the self-healing supervisors into it (see D2).**
- **D2 — Who owns retry / restart?** Two different models coexist today and both are
  correct for their job: (a) the **self-healing supervisors** (#1 collector, #2 terminal
  watchdog) own their own restart loops, singleton locks, and stall-watchdogs — they must
  stay independent and reboot/logon-launched; a central runner should NOT absorb them. (b)
  the **one-shot evening jobs** (#3–#6) are "daily trigger + run once + exit," relying on
  Task Scheduler to recur and on idempotency to self-heal holes on the next run. The open
  question is only about (b): should the evening one-shots gain a light central
  ordering/retry layer, or keep relying on next-day idempotency + the EOD report's
  red/stale signal?
- **D3 — Holidays / market-closed days.** Every data job has a weekday guard, but **none
  is holiday-aware** (Thanksgiving, July 4, etc. still fire and pull an empty/duplicate
  day, healed by idempotency). Question: add a shared market-calendar gate (one helper all
  jobs consult), or keep relying on idempotency + weekday guard and accept harmless
  no-op runs on holidays? A shared calendar would also let the monitor skip closed days
  cleanly.
- **D4 — Failure alerting.** Today the ONLY alert channel is the 9 PM EodReport email,
  which shows each job red/stale. Gaps: (i) if a job dies *after* 9 PM or the report
  itself fails (F5), nothing alerts; (ii) the self-healing supervisors heal silently, so a
  flapping terminal isn't surfaced until someone reads the watchdog log. Question: is the
  daily digest sufficient, or do we want a real-time alert (push/email) on
  supervisor-restart-storms or a missed evening job?
- **D5 — When (and how) does the account monitor get scheduled? — RESOLVED (2026-06-30).**
  Chosen: option (b), the interlock. The monitor is registered as `AccountMonitorDaily` at
  daily 4:30 PM CT and uses the **gateway lock** to make the Gateway window safe — it acquires
  the lock for its read-only session and SKIPs cleanly if a rebalance holds it (see F2), so it
  never needs a hand-chosen slot that "provably" avoids the manual rebalance. The on-demand
  dashboard panel (option c, §4) remains a complementary nicety, not a replacement.

---

## 4. GUI relationship — scheduling + monitor in the dashboard

The Streamlit dashboard (`dashboard/app.py`) already has a **Health** tab that surfaces
collector progress, EOD coverage, the status JSONs, and **Windows task states** — so the
scheduler is *already partly visible* there. Cross-reference: **`docs\DASHBOARD_ROADMAP.md`**
(the full, bucketed dashboard plan; everything there is still read-only until Phase 3 is
explicitly opened).

How scheduling + the monitor should surface, building on what exists:

- **Scheduler status panel (read-only, Phase 1-shaped).** Extend the Health tab from "task
  states" to a real scheduler view: the inventory table above (live/planned/held), last
  run + last result per job, next fire time, and a red/green for the evening chain as a
  whole. This is pure display over data Task Scheduler + the status artifacts already
  expose — no new order path, fits the dashboard's read-only guarantee.
- **Account monitor as an on-demand panel.** The monitor is already a clean read-only,
  propose-only shell. Surfacing its per-account verdict table in the dashboard (button-
  triggered, like the existing Accounts tab's read-only Gateway read) is the safest way to
  "schedule" it — it sidesteps D5 entirely by letting the operator run it when the Gateway
  is known-clear, instead of registering a clock task. This dovetails with the Accounts tab
  already in the roadmap.

### Andrew's idea — the fresh-cash GUI decision (earmark vs. rebalance)

Today, when fresh cash appears in an account, the monitor's response depends on a
hand-maintained file: operator EARMARKS live in `monitor_earmarks.json` under `STATE_DIR`
(off Drive), and the monitor reads them to decide whether new cash is "fenced" (a pending
withdrawal) or "investable" (rebalance it). Editing a JSON file by hand is the friction.

**Proposal (Andrew's steer): surface that decision as a one-click GUI control.** When the
monitor detects a deposit or sale-raised cash on an account, the dashboard shows a prompt /
checkbox:

> *"$X of fresh cash detected in account DU…14N. Is this cash for a withdrawal
> (earmark / fence it) or to be rebalanced into the model?"*  → **[ Earmark ]  [ Rebalance ]**

- **Earmark** writes the earmark into `monitor_earmarks.json` for that account (the exact
  thing the operator edits by hand today) — the cash is fenced and the reserve/withdrawal
  fail-safe holds it for the upcoming distribution.
- **Rebalance** clears/withholds the earmark so the next rebalance treats the cash as
  investable (the "sale-raised / deposit nudge" the monitor already computes).

This turns the monitor's two existing propose-only signals — **earmark** and the
**sale-raised / deposit rebalance-nudge** — into a single UI decision instead of a
STATE_DIR file edit. It writes only the same local earmark file the monitor already reads;
it does **not** place orders (a rebalance is still run through the existing gated executor
on the CLI). Cross-references: the cashflow mechanics (reserve, withdrawal fail-safe,
deposit/earmark) are specced in **`docs\ACCOUNT_CASHFLOW_MANAGEMENT.md`**; the dashboard
phasing and read-only wall are in **`docs\DASHBOARD_ROADMAP.md`** (this control is a
Phase-1/2-shaped, write-only-to-local-state addition — it never weakens the order-path
guarantees).

---

## 5. One-line summary for the owner

There are **8 live clock jobs** (an evening data→derive→report chain at 4:30 / 5:30 / 7:30 /
9:00 PM CT, plus the read-only account monitor at 4:30 PM, plus an all-day 1-minute collector
and its terminal watchdog) and **1 retired** (the old IBKR forward pull). The account monitor
— previously the one held job — went **LIVE 2026-06-30**: the gateway-lock interlock (it SKIPs
if a rebalance holds the lock) resolved the "when can it safely share the Gateway" question.
The remaining decision is **whether the evening chain should gain a light ordering layer** or
keep relying on its current hand-tuned time gaps + idempotency. The cleanest near-term GUI win
is surfacing the scheduler in the dashboard's existing Health tab and running the monitor
on-demand with a one-click earmark-vs-rebalance prompt for fresh cash.
