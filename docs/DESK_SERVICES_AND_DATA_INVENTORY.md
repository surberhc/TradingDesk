# TradingDesk — Services & Data Inventory
_Snapshot as of 2026-07-21. Regenerate by re-running the services + data audits. This is a reference, not a live status board._

## 1. Scheduled services — FIRING OK

| Service (task) | Runs (script) | Purpose | Schedule (CT) | Last run + result |
|---|---|---|---|---|
| LiveTradeGatewayOpen_0815CT | `C:\IBC-Live-Trade\StartGatewayLiveTrade.bat` | Opens S8 LIVE gateway (port 4003, zero-transmit) | Weekly Mon–Fri 08:15 | 07/21 running |
| S8UnifiedService_Session | `livebot\run_s8_service.cmd` → `s8_service.py` | S8 all-day pilot (PILOT_MODE reader, logs WOULD-HAVE-TRANSMITTED) | Weekly Mon–Fri 08:25 | 07/21 running |
| S8Collector_Session | `livebot\run_s8_collector.cmd` → `s8_collector.py` | S8 intraday ATM-band collector (readonly 4003) | Weekly Mon–Fri 08:31 | 07/21 running |
| S8SessionTeardown | `livebot\run_s8_teardown.cmd` → `s8_reap.py --all` | Reaps orphan S8 python after close | Weekly Mon–Fri 15:05 | 07/20 rc=0 |
| MorningExecuteDaily | `warehouse\run_morning_execute.bat` → `paperbot\morning_execute_run.py` | Executes staged paper trade list (PILOT_MODE, transmits nothing) | Daily 08:50 | 07/21 rc=0 (resumed; see notes) |
| EodReport | `warehouse\run_eod.bat` → `dailyreport\eod_report.py` | EOD regime/report email | Daily 21:00 | 07/20 rc=0, emailed |
| TiingoDailyUpdate | `warehouse\run_tiingo.bat` → `dailyreport\tiingo_daily.py` | Tiingo EOD equity prices | Daily 19:00 & 20:45 | 07/20 rc=0 (partial=rate-limit, fine) |
| ThetaEodDaily | `warehouse\run_eoddaily.bat` → `datacollector\eod_daily.py` | ThetaData full-chain EOD grab (50 roots) | Daily 17:30 | 07/20 rc=0 |
| GexDailyBuild | `warehouse\run_gex.bat` → `datacollector\build_features.py --latest` | Incremental GEX/dealer-gamma build | Daily 19:30 | 07/20 rc=0 (newest 20260717) |
| RepoBackupDaily | `datacollector\run_repo_backup.cmd` → `repo_backup.py` | Verified git-bundle backup to Drive | Daily 20:00 + AtLogon | 07/20 rc=0 (head c61f8e4) |
| ThetaTerminalWatchdog | `warehouse\run_theta_watchdog.bat` → `theta_terminal_watchdog.py` | Keeps ThetaData terminal alive (port 25503) | Every 1 min + AtBoot | 07/21 healthy |
| HeartbeatStalenessAlarm | `warehouse\run_heartbeat_alarm.bat` → `heartbeat_alarm.py` | Pages on cold subsystem heartbeats | Every 15 min + AtBoot | 07/21 rc=0 |
| DataBackupDaily | `datacollector\run_data_backup.cmd` → `data_backup.py` | Verified rclone backup of ~99 GB warehouse to Drive | Daily 21:00 + AtLogon | Fixed 2026-07-21 (commit 0d5ba7d whitelisted `state/last_email_ok.txt` after a false 07-20 verify failure); next clean run expected 07/21 21:00 |

## 2. Scheduled services — PAUSED ON PURPOSE (not failures)

| Service | Target | Why paused |
|---|---|---|
| AccountMonitorDaily | `run_nightly_monitor.bat` | Needs paper gateway (4002), down for port migration (Disabled) |
| GatewayWatchdog | `run_gateway_watchdog.bat` | Watches paper gateway (4002), same migration (Disabled) |
| Spxw1mCollector | `run_spxw_1m.bat` | Intraday SPXW 1-min collection intentionally paused; collector retired 2026-07-10 (Disabled) |
| UniverseDownloadEod | `run_universe_download_eod.bat` | Universe bulk download paused mid-run (Disabled) |
| CanslimOverlayPipeline / CanslimOverlayWatchdog / CanslimIbkrPriceGapfill | `run_overlay_*.bat` / `run_canslim_ibkr_pull.bat` | CANSLIM overlay retired in the 07/xx data-collection review (Disabled) |
| InvesTech Phase1 Feed / Phase2 Breadth | `investech\phase{1,2}_feed\run_feed.cmd` | InvesTech feeds disabled (Disabled) |
| GatewayArmRestart | `warehouse\run_gateway_arm_restart.bat` | On-demand only, no trigger (Ready, idle) |

**Note:** the paper-gateway (port 4002) port migration is mid-flight (newest commit repointed the paper launcher to `C:\IBC`); all paper-gateway-dependent tasks stay disabled until it finishes. The S8 LIVE gateway (4003) is separate and fully up.

## 3. Data-collection inventory

| Dataset | Source | Scope | Granularity | Intended range/cadence | Storage location + format + size | Actual coverage | Last update |
|---|---|---|---|---|---|---|---|
| EOD option chains (forward-maintained) | ThetaData | 50 roots (SPX/SPXW/SPY/XSP/VIX/NDX, 11 XL sectors, credit/rates/real-assets, ~15 mega-caps) | EOD greeks+IV+OI | 2018→present nightly (ThetaEodDaily 17:30) | `-Local\warehouse\raw\options\{SYM}\{YYYYMMDD}.parquet` | 2018-01-01→2026-07-20 complete | current |
| EOD option chains (bulk-only extras) | ThetaData | 91 roots | EOD greeks+IV+OI | one-time bulk 2018→2026 | same tree | 2018→2026-07-03 (68 roots) / 2026-07-08 (22 roots) | frozen (backfill in progress 2026-07-21) |
| _EOD total_ | — | 141 roots | — | — | 68.7 GB, 312,338 files | — | — |
| SPXW 1-minute intraday | ThetaData | SPXW full chain incl 0DTE | 1-min ohlc+quote | 2022→present | `-Local\warehouse\raw\options_1m\SPXW\{ohlc,quote}\{YYYYMMDD}.parquet`, ~31 GB | 2022-01-03→2026-07-01 | collector retired 2026-07-10 (gap backfill in progress 2026-07-21) |
| SPX 1-minute intraday | ThetaData | SPX chain | 1-min ohlc+quote | — | `-Local\warehouse\raw\options_1m\SPX\…`, ~4.8 GB | 2025-05-01→2026-06-18 | retired (backfill in progress) |
| Intraday snapshots | ThetaData | ~40 roots @ 10:00/12:00/14:00/15:45 | point snapshots | 2018→2026 | `-Local\warehouse\raw\options_snap\{SYM}\{time}\` | active roots 2018→2026-07-08 | frozen |
| Derived GEX/dealer-gamma dailies | computed from EOD chains | 36 roots | daily | nightly (GexDailyBuild 19:30) | `-Local\warehouse\derived\{SYM}_gex_daily.parquet` | 2018-01-02→2026-07-17 current | current |
| ThetaData equity options (CANSLIM) | ThetaData | 56 names | per-option daily EOD | 2023→2026 | `-Local\canslim\thetadata_equity\{SYM}\{YYYYMM}.parquet`, 2.0 GB | 2023-01→2026-07-02 | CANSLIM shelved |
| Tiingo EOD (backtester) | Tiingo (+FRED) | 32 tickers | daily adjClose | 2007→present nightly 19:00+20:45 | `-Local\bt_data\*.parquet`, ~2.9 MB | SPY 2007-01-03→2026-07-20; _hy_oas 2023-07→2026-07-17 | current |
| CANSLIM prices | Tiingo+IBKR gapfill | 6,005/16,725 symbols (36%) | daily OHLCV+adj | 2010→present | `-Local\canslim\prices\{SYM}.parquet`, 508 MB | 2010-01-04→2026-07-06 | PAUSED (port migration) |
| CANSLIM fundamentals | SEC EDGAR | 14,946 CIKs | quarterly PIT | 2009→present | `-Local\canslim\edgar\*_full\shard=*.parquet` | 2009-04-15→2026-06-30, 1.13M rows | 2026-07-02 |
| MSR features | Tier-1-Alpha newsletter PDFs | SPX + 17 sector ETFs | daily | 2025-05→2026-06 (frozen) | `TradingDesk\msr\msr.db`, 1.56 MB | 2025-05-01→2026-06-18 complete (281 reports) | static |
| IBKR forward option collector | IBKR 4001 | SPX/SPXW deep chain | EOD | future crossover | `-Local\warehouse\raw\options_ibkr\` | EMPTY — never ran | n/a |

## 4. Known data holes & backfill status

| Dataset | Missing | Backfill source | Deadline-bound? | Status |
|---|---|---|---|---|
| SPXW 1-min | 2026-07-02→present | ThetaData only | YES (~July 25) | BACKFILLING 2026-07-21 |
| SPX 1-min | 2026-06-19→present | ThetaData only | YES | BACKFILLING 2026-07-21 |
| 91 bulk-only EOD roots | ~12-day tail (07-03/08→07-20) | ThetaData | YES | BACKFILLING 2026-07-21 |
| Pre-2022 SPXW 1-min | 2018–2021 | ThetaData | YES but large | SKIPPED (0DTE barely existed; not worth the 4-day risk) |
| NDX (EOD+snap+1-min) | ~all history | NOT backfillable at this tier | n/a | SKIPPED (QQQ is proxy; re-probe before cancel optional) |
| thetadata_equity (CANSLIM) | 2026-07-03→present | ThetaData | YES | not pursued (CANSLIM shelved) |
| CANSLIM prices | ~10,720 symbols unpulled; stale since 07-06 | Tiingo/IBKR | No | deliberately paused (port migration) |
| _hy_oas macro | stale ~3 days | FRED | No | benign (FRED rolling-3yr limit) |

## 5. ThetaData deadline

The subscription is a rented month, estimated to lapse **~2026-07-25 (≈4 days from this snapshot)**. This is the **desk's own estimate** from `docs\REMAINING_WINDOW_DATA_GRAB_PLAN.md` and a 2026-07-13 note — **NOT a vendor-confirmed billing date**; Andrew should confirm the exact date. After it lapses, no ThetaData history is recreatable. The IBKR forward crossover (`raw\options_ibkr`) is built but has never run and can only go forward.

## 6. Open follow-ups (as of 2026-07-21)

- Confirm exact ThetaData billing/expiry date.
- MorningExecuteDaily ran a dead My-Drive path 07/18–07/20 (masked by rc=0, nothing staged); self-corrected 07/21 — confirm no staged morning was silently skipped.
- Finish paper-gateway (4002) port migration, then re-enable AccountMonitorDaily + GatewayWatchdog.
- `ddoi_*` derived features stale (→2026-06-29); `universe_membership.csv` stale (352 rows) — recompute before any selection backtest.
