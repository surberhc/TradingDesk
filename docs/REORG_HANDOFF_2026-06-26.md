# Reorg & Relocation Handoff — 2026-06-26

What a different Claude session moved, where everything lives now, and the one
outstanding item. Read this first if you're picking up any part of this project.

## TL;DR
The whole project was consolidated into **one Drive root + one local root**:
- **CODE** (synced, backed up): `C:\Users\andre\My Drive (andrew@surberhc.com)\TradingDesk\`
- **DATA / STATE / RUNTIME** (local C:, never synced): `C:\TradingDesk-Local\`

The tax-favored Google Drive (`andrew@taxfavoredretirement.com`) is being **removed
from this machine** — nothing project-related should reference it anymore.

## Where everything lives now

### Drive — `…\My Drive (andrew@surberhc.com)\TradingDesk\`  (CODE)
| Folder | What it is | Notes |
|---|---|---|
| `strategies\` | shared strategy brain (pip package `strategies`) | `all_weather.py` + `parts\`; backtester AND paperbot import it |
| `backtester\` | research / backtest engine | imports `strategies`; 89 tests pass |
| `paperbot\` | PAPER execution engine (dry-run) | imports `strategies` + `connections`; account DU…141, port 4002 |
| `connections\` | shared access layer (pip package `connections`) | `ibkr.py`, `tiingo.py`, `clientids.py` |
| `datacollector\` | options collector (ThetaData → warehouse) | `config.py` `DATA_ROOT` → `C:\TradingDesk-Local\warehouse` |
| `dailyreport\` | 5 PM RRG regime email | `STATE_DIR` → `C:\TradingDesk-Local\state\dailyreport` |
| `msr\` | newsletter PDF → features feed (was "Tier 1 Alpha") | self-contained ETL + PDF archive |
| `docs\` | specs / handoffs / plans | incl. this file + `HANDOFF.md` |

### Local — `C:\TradingDesk-Local\`  (DATA / STATE / RUNTIME)
| Folder | What it is | Was |
|---|---|---|
| `warehouse\` | 8.5 GB options parquet + `catalog.duckdb` + `lib\` (ThetaTerminal jar) | `C:\MarketData` |
| `state\dailyreport\` | `rrg.db`, report outputs, logs | `C:\TradingState\dailyreport` |
| `venv\` | Python 3.12 runtime (the ONE venv) | `C:\Users\andre\backtester-venv` (deleted) |

Run Python with: `C:\TradingDesk-Local\venv\Scripts\python.exe`
(`strategies` + `connections` are installed editable into it.)

### Left in place on purpose (do NOT relocate)
- `C:\IBC\` — Gateway auto-launch (`StartGateway.bat`, IBController). `ReadOnlyApi=yes`
  in `C:\IBC\config.ini` BLOCKS order transmission until deliberately flipped.
- `C:\Jts\` — the IB Gateway/TWS install + **your account data**.
- System Pythons: `C:\Python314` and `…\pythoncore-3.14` (the RRG scheduled task runs on 3.14).
- `C:\Users\andre\rrg_secrets.env` — email app password (kept in home, off Drive).
- `TIINGO_API_KEY` — Windows user env var.

## Wiring (already repointed to the new paths)
- **Editable installs:** `strategies`, `connections` in `C:\TradingDesk-Local\venv`.
- **Scheduled task** "RRG Daily Poll" (5 PM) → `…\TradingDesk\dailyreport\daily_run.py`,
  runs on `pythoncore-3.14` (NOT the venv). State writes to `C:\TradingDesk-Local\state\dailyreport`.
- **Collector launchers** (`run_supervisor.bat`, `monitor_download.py`) now live in
  `C:\TradingDesk-Local\warehouse\` and point at the SurberHC `datacollector` code + the new venv.
- **clientId registry** (`connections\clientids.py`): 1 = dailyreport poller · 9 = gateway
  health-check · 21-24 = datacollector tests · **30 = paperbot**. Retired strays: 2, 7.
- **Paper account:** DU…141, paper Gateway port **4002**. (Real-money / port 4001 is OUT of scope.)

## Deleted / retired this session (gone for good)
Old tax-favored `TradingDesk`, stale `C:\Users\andre\backtester` (+its .venv),
`backtester_BACKUP_2026-06-25`, old `C:\Users\andre\strategy_core`, old
`C:\Users\andre\backtester-venv`. Home strays (`rrg_poller.py`, `rrg_backfill.py`,
`ibkr_test.py`) and old `_retired` archive. `C:\MarketData` and `C:\TradingState` were
MOVED (not deleted) into `C:\TradingDesk-Local\`.

## ⚠️ OUTSTANDING — for the warehouse/collector session (NOT done here, by request)
The DuckDB catalog view **`options_eod` still points at the OLD `C:\MarketData` path and
cannot be rebuilt at the new path yet.** Cause (PRE-EXISTING, not from the move):
- Many parquet files are **empty / zero-column** "no-data-day" markers — NDX is 2,181 of
  2,214 empty; other symbols a few % each. DuckDB 1.5.4's `read_parquet(union_by_name=true)`
  refuses to scan zero-column files, so the view won't build. `storage.rebuild_catalog()`
  hits the same wall.
- **DO NOT delete/quarantine the empty files** — `storage.have_day()` relies on their
  existence so the collector doesn't re-pull those days; removing them = thousands of re-downloads.
- **Recommended fix (code-only):** change `storage.rebuild_catalog()` to build the view over
  only the NON-empty parquets (e.g. enumerate files, filter by parquet column count, build the
  view from that list), then rebuild. `config.DATA_ROOT` already points at the new warehouse,
  so once the view-build is fixed it lands in the right place.
- The ThetaData **terminal was STOPPED** to move the warehouse (download was already paused).
  Restart via `C:\TradingDesk-Local\warehouse\run_supervisor.bat` or `datacollector\start_terminal.py`.

## Verification done this session
Backtester re-proven byte-identical from the new location every step (NAV `2.3109336358`,
NAV hash `427ef39c…`, weights hash `efd6d002…`, 89/89 tests). `strategies` + `connections`
import from SurberHC via the new venv. No reference to the tax-favored drive or the old venv
remains in any active location.


## Secrets (recovered 2026-06-26)
- TIINGO_API_KEY (backtester data downloads) was in a .env that got deleted during cleanup, then RECOVERED from a re-synced copy on the tax-favored drive. It now lives as a Windows User env var (off-Drive) plus a backup at C:/TradingDesk-Local/secrets/.env . download_data.py reads it via os.environ (no .env on Drive needed).
- rrg_secrets.env (email app password): untouched at C:/Users/andre/rrg_secrets.env (read via USERPROFILE).
