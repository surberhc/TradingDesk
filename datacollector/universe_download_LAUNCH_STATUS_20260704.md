# Expanded-universe options pull — LAUNCH STATUS + reliability (2026-07-04)

## What is LIVE right now
- **Priority-1 EOD pull is RUNNING**: `universe_download.py --launcher --layer eod --only-new
  --shards 4`, detached supervisor **pid 21388** + 4 shards, pulling the **90 new roots**
  (2018-01-01..2026-07-03, ~199,800 sym-days, ETA ~25 h at K=4). Progress climbing, heartbeat
  fresh, **zero PermissionErrors / EOD-FAILs since the manifest-contention fix.**
- **Independent stall alarm is ARMED**: the download is wired into the already-scheduled
  `HeartbeatStalenessAlarm` (fires every 15 min, boot-triggered). Dry-run confirms
  `universe_dl: FRESH — no alert`. If the supervisor dies, the heartbeat goes cold and within
  60 min the alarm emails Andrew (same Gmail path as the EOD digest).

## Key file paths (all LOCAL, never Drive)
- Heartbeat (mtime advances per sym-day; `COMPLETE` on finish):
  `C:\TradingDesk-Local\warehouse\universe_dl_state\universe_dl_heartbeat.txt`
- Progress JSON: `...\universe_dl_state\universe_dl_progress.json`
- Supervisor log: `...\universe_dl_state\universe_dl_supervisor.log`
- Per-shard logs/progress: `...\universe_dl_state\universe_dl_shard{0..3}.{log,json}`
- Singleton lock: `...\universe_dl_state\universe_dl_supervisor.lock`
- EOD data: `C:\TradingDesk-Local\warehouse\raw\options\{SYM}\{YYYYMMDD}.parquet`
- Snapshots (Priority 2, not yet started): `...\raw\options_snap\{SYM}\{HHMM}\{YYYYMMDD}.parquet`
- Launcher bat (for the scheduled task): `C:\TradingDesk-Local\warehouse\run_universe_download_eod.bat`

## Status check command
```
cat "C:\TradingDesk-Local\warehouse\universe_dl_state\universe_dl_progress.json"
"C:\TradingDesk-Local\venv\Scripts\python.exe" "...\datacollector\heartbeat_alarm.py" --dry-run
```

## 11-mode liveness rubric — coverage
| # | Failure mode | Covered by | Status |
|---|---|---|---|
| 1 | Crash (a shard dies) | Supervisor restarts a dead-but-unfinished shard each 30s tick; resumes via skip-done | **COVERED (live)** |
| 2 | Stall / hang | Independent `HeartbeatStalenessAlarm` (separate process, 15-min fire) emails if heartbeat cold >60 min | **COVERED (live)** |
| 3 | Duplicate instance | Cross-process singleton lock (paperbot.gateway_lock; atomic O_EXCL + reclaim). Verified: 2nd launcher busy-skips while live | **COVERED (live)** |
| 4 | Partial / corrupt output | Atomic temp+os.replace per parquet; a day is "done" only if its file(s) exist; a torn temp is ignored + overwritten on resume | **COVERED (live)** |
| 5 | Poison day | Per-day try/except in the worker -> log + skip; one bad day never aborts the run | **COVERED (live)** |
| 6 | Dependency down (terminal) | Launcher no-ops if terminal unreachable (next trigger retries); `_get_csv` retries w/ backoff; persistent outage -> the alarm pages | **COVERED (live for retry/backoff; the "next trigger" leg needs the scheduled task — see BLOCKED)** |
| 7 | Supervisor death | **The scheduled task's every-15-min re-trigger revives it (singleton makes re-fire safe).** | **BLOCKED — needs the scheduled task (see below). Until then, only a manual relaunch revives a dead supervisor; the alarm still PAGES so it won't be silent.** |
| 8 | Reboot / power outage | On-boot trigger + resume-from-checkpoint | **BLOCKED — needs the on-boot scheduled task.** |
| 9 | Missed window | `StartWhenAvailable` + every-15-min catch-up trigger | **BLOCKED — needs the scheduled task.** |
| 10 | Logoff | Whether-logged-on (Password/SYSTEM) task | **BLOCKED — needs the scheduled task. The current detached process survives app-close but NOT a reboot.** |
| 11 | Unnoticed death | Independent `HeartbeatStalenessAlarm` email | **COVERED (live)** |

## BLOCKED pending Andrew — scheduled-task registration
Andrew's original instruction explicitly reserved scheduled-task registration for his own
approval ("DO NOT register a Windows scheduled task yet ... wait for Andrew's approval"). Per
the operating contract, a coordinator OK does not override a step the user reserved for
himself, and the auto-mode guard correctly blocked the `Register-ScheduledTask` calls. So the
DURABLE, machine-side, reboot-proof legs (modes 7-10) are **built and ready but NOT yet
registered**. They need Andrew's go to run these two registrations:

1. **`UniverseDownloadEod`** — whether-logged-on, triggers = AtStartup + AtLogon + every-15-min
   (10-yr), StartWhenAvailable, ExecTimeLimit=none, action = `run_universe_download_eod.bat`.
   Mirrors the proven `CanslimIbkrPriceGapfill` task exactly. The launcher is singleton-safe so
   overlapping triggers no-op.
2. **`ThetaTerminalWatchdogBoot`** (the terminal boot-hole fix) — a SEPARATE SYSTEM, AtStartup
   task invoking the SAME `run_theta_watchdog.bat`. `ThetaTerminalWatchdog` today has only
   LogOn+Daily triggers, so after a power-outage reboot with nobody logged in the terminal
   could stay down for hours. The watchdog is singleton-guarded (`theta_watchdog.lock`), so a
   boot-time invocation is a safe no-op if it's already up. (Option A — adding an AtStartup
   trigger to the existing task in place — was also blocked by the guard; Option B, this
   separate additive task, is the non-destructive choice.)

Ready-to-run registration commands are in the coordinator report / can be re-issued once
Andrew approves. Everything else is done and running.

## Conflict check (pre-launch, verified)
- ThetaData terminal UP (port 25503). SPX/SPXW 1-min collectors idle (backfills complete;
  `Spxw1mCollector` a no-op, `Spx1mParallel` finished 7/2). No forward/overlay pull running.
- Nightly terminal contention windows (CT): `ThetaEodDaily` 17:30 (+1h), `CanslimOverlayPipeline`
  18:00 (no next-run set — has never run). This 5-day pull WILL overlap those nightly; the
  supervisor backs off on 429/error bursts, so it yields gracefully. Acceptable, but the pull
  runs slower during those windows (already reflected in the 60-min alarm threshold).

## The end-to-end unattended chain (once the two tasks are registered)
power outage -> boot -> `ThetaTerminalWatchdogBoot` (SYSTEM, AtStartup) starts the terminal
watchdog -> terminal comes up on 25503 -> `UniverseDownloadEod` (AtStartup) fires -> launcher
sees terminal up + scope incomplete + no live singleton -> supervises K=4, resuming from the
on-disk checkpoint -> heartbeat advances -> `HeartbeatStalenessAlarm` (boot-triggered, 15-min)
confirms fresh. Every link is built; only the two `Register-ScheduledTask` calls await Andrew.
