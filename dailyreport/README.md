# dailyreport — end-of-day status digest

One email at the end of each day summarizing every automated activity on the desk.
(Originally the RRG regime pipeline — that was a test and is now **retired**; this
harness was repurposed. The old `daily_run.py` / `rrg_*.py` files remain but are no
longer scheduled. Moved to `archive/rrg/` on 2026-07-08 as explicit housekeeping —
confirmed unscheduled and unreferenced by anything active before the move.)

## What it does

Each daily job drops a small **status JSON** when it runs. The reporter reads them
all and emails one concise digest. Decoupled on purpose: a job that crashed or never
ran shows as `stale`/`fail` in the email rather than taking the report down.

```
TiingoDailyUpdate (4:30pm CT) ─┐
ThetaForwardDaily (5:30pm CT) ─┼─► each writes status\<job>.json ─► EodReport (9:00pm CT) ─► one email
ThetaData supervisor (always) ─┘     (+ native heartbeats/manifest)
```

## Files (this folder — code, in Drive, backed up)

| File | Role |
|---|---|
| `eod_report.py` | Aggregator: builds the digest from all sections, emails it. **Run last.** |
| `status.py` | Tiny helper: `write(job, status, metrics, message)` / `read(job)`. |
| `mailer.py` | Generic `send_html(subject, html)`; reuses the RRG Gmail creds. |
| `tiingo_daily.py` | Daily Tiingo refresh — runs the backtester downloader, writes status. |
| `archive/rrg/daily_run.py`, `archive/rrg/rrg_*.py` | RETIRED RRG regime pipeline (kept for reference, unscheduled). |

Sections live in `eod_report.SECTIONS`. **To add one** (e.g. a strategy update):
write a `build_*()` returning `_sec(key, title, status, headline, rows)` and append it.

## Where the moving parts live (NOT all in this folder)

- **State / logs / status artifacts** (local C:, not synced):
  `C:\TradingDesk-Local\state\dailyreport\`
  → `status\*.json` (forward, tiingo, eod_report), `eod_report.log`, `tiingo_daily.log`
- **Launchers** (local C:): `C:\TradingDesk-Local\warehouse\run_tiingo.bat`,
  `run_forward.bat`, `run_eod.bat`
- **Schedule**: Windows Task Scheduler tasks `TiingoDailyUpdate`, `ThetaForwardDaily`,
  `EodReport` (all fire on **local Central Time**).
- **Email creds**: `C:\Users\andre\rrg_secrets.env` (off Drive) — `RRG_SMTP_USER/PASS/
  FROM/TO`. Recipient is whatever `RRG_MAIL_TO` is set to there.
- **Upstream data this report reads**: ThetaData warehouse + supervisor heartbeat
  (`C:\TradingDesk-Local\warehouse\`), the IBKR forward collector
  (`..\datacollector\forward_daily.py`), the Tiingo manifest
  (`..\backtester\data\_manifest.json`).

## Run any piece by hand

```
"C:\TradingDesk-Local\venv\Scripts\python.exe" "<this folder>\tiingo_daily.py"   # refresh Tiingo now
"C:\TradingDesk-Local\venv\Scripts\python.exe" "<this folder>\eod_report.py"      # build + send the digest now
```

## Notes / gotchas

- `tiingo_daily.py` injects `TIINGO_API_KEY` (and optional `FRED_API_KEY`) from
  `C:\TradingDesk-Local\secrets\.env` into the downloader — a scheduled-task context
  may not inherit the Windows user env var. Values are never logged.
- Tiingo "stale run" QC flags on cash-like ETFs (SGOV/BIL/VGSH/USFR/TFLO) are benign;
  only zero/negative-price or split flags downgrade the section.
- EodReport is at 9:00pm CT as a buffer because the wide-band forward run's exact
  duration isn't yet verified — pull it earlier once observed.
- `schtasks /change /st` prompts for a password and hangs non-interactively; retime a
  task with `/delete` + `/create /f` instead.
