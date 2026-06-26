# RESUME HERE — desk handoff (2026-06-26, evening)

Canonical pickup file. A fresh session (incl. **dispatch**) should read THIS first, then the
project memory loads the durable facts automatically. Supersedes `RESUME_HERE_2026-06-26.md`
(that was the data-collector pickup — that work is now done).

**Run Python with:** `C:\TradingDesk-Local\venv\Scripts\python.exe`
**Code root (Drive, synced):** `…\TradingDesk\`  ·  **Data/state/runtime (local C:, NOT synced):** `C:\TradingDesk-Local\`

---

## ⚠️ READ BEFORE ACTING — environment + the parallel session

- **This work is MACHINE-LOCAL.** It needs THIS Windows box: the venv, IB Gateway (paper,
  `127.0.0.1:4002`), the Windows Task Scheduler jobs, the ThetaData Terminal, and
  `C:\TradingDesk-Local\` state. **If dispatch runs OFF this machine** it can author/edit code
  in Drive, but it CANNOT run the live collectors, reach the Gateway, see the scheduled tasks,
  or read `C:\TradingDesk-Local\`. In that case: make code changes only, and leave anything
  that must run for the user to do on-box. Confirm which you are before running live commands.
- **A SECOND session is active in this same repo** (the paperbot / trading-engine build). It
  shares the filesystem AND the IB Gateway. Rules to avoid collisions:
  - IBKR clientIds are registered in `connections\connections\clientids.py`. **Paperbot = 30,
    our forward collector = 25.** Never run two clients on one id. Pick a NEW id from the
    registry for anything new.
  - The Gateway is shared — keep data-collector connections `readonly=True` and modest on
    market-data lines (~100 cap).
  - Both sessions edit files live (they've already touched `clientids.py` and memory mid-session).
    Re-read a file before editing if in doubt.
- **Timezone: this machine runs CENTRAL TIME.** All scheduled tasks fire on local CT
  (market close = 3:00 PM CT / 4:00 PM ET). Don't mislabel times as ET.
- **Secrets** live off-Drive: `C:\TradingDesk-Local\secrets\.env` (THETADATA_API_KEY,
  TIINGO_API_KEY) and `C:\Users\andre\rrg_secrets.env` (email). Never print their values.
- **Paper only.** Never say "live"; real-money / port 4001 is out of scope.
- **Supervising long ops:** never background a long/risky run and wait passively — use a tight
  timeout, flushed prints, and kill-and-diagnose on stall.

---

## CURRENT STATE (2026-06-26 ~3:00 PM CT)

**1. ThetaData one-time warehouse grab — RUNNING.** Self-healing supervisor (`ThetaDataSupervisor`
scheduled task, or `run_supervisor.bat`) pulling the full 50-root EOD option history. ~21,400
parquet files, ~11 GB, climbing ~13/min. Heartbeat: `C:\TradingDesk-Local\warehouse\supervisor_heartbeat.txt`.
Catalog view `options_eod` in `warehouse\catalog.duckdb` (built over non-empty parquet only).
When the heartbeat reads `COMPLETE`, cancel the ThetaData subscription and delete that task.

**2. IBKR forward collector — BUILT, PROVEN, SCHEDULED (first full run tonight 5:30 PM CT).**
`datacollector\ibkr_forward.py` (collector) + `forward_daily.py` (daily wrapper, reconnect-tolerant)
+ `run_forward.bat`. Captures a "wide band" EOD chain (`config.FORWARD_STRIKE_BAND=50`,
`FORWARD_MAX_EXPIRATIONS=12`) for all 50 roots → same 41-col warehouse schema via `storage.write_day`.
clientId 25, readonly. Mappings fixed live: RUT→exchange RUSSELL, BRKB→IBKR symbol "BRK B".
NDX confirmed available on IBKR (so it's captured forward even though ThetaData lacks its history).

**3. End-of-day report — BUILT, TESTED (real email sent + confirmed by user), SCHEDULED.**
`dailyreport\` (see its `README.md`). Decoupled: each job writes `status\<job>.json`; `eod_report.py`
aggregates → one concise HTML digest emailed via `mailer.py` (reuses RRG Gmail creds; recipient is
still the tax-favored address by the user's choice). RRG regime pipeline RETIRED (task deleted, code kept).

**Daily schedule (Windows Task Scheduler, local CT):**
| Time CT | Task | Launcher |
|---|---|---|
| 4:30 PM | TiingoDailyUpdate | `run_tiingo.bat` → `dailyreport\tiingo_daily.py` |
| 5:30 PM | ThetaForwardDaily | `run_forward.bat` → `datacollector\forward_daily.py` |
| 9:00 PM | EodReport | `run_eod.bat` → `dailyreport\eod_report.py` |
Launchers live in `C:\TradingDesk-Local\warehouse\`. Logs + status in `C:\TradingDesk-Local\state\dailyreport\`.

---

## IMMEDIATE NEXT ACTIONS (in order)

1. **Verify tonight's first full pipeline run.** After 9 PM CT: check the digest email looks right;
   read `state\dailyreport\eod_report.log` + the `status\*.json` (expect `forward.json` to now exist).
2. **Tune the EOD report time.** 9 PM is a buffer because the wide-band forward run's exact duration
   is UNVERIFIED (the timing survey disconnected before qualify-time was measured). Read the forward
   run's start→finish from `state\dailyreport\` / `warehouse\forward_heartbeat.txt`; if it finishes by
   ~7:30 PM, pull EodReport earlier (e.g. 8 PM) via `schtasks /delete` + `/create /f` (NOTE:
   `schtasks /change /st` prompts for a password and hangs non-interactively).
3. **Confirm Gateway auto-restart took effect.** `AutoRestartTime=11:45 PM` was written to
   `C:\IBC\config.ini`; it activates on the next Gateway launch. After the Gateway next cycles,
   eyeball Lock-and-Exit shows 11:45 PM.

## OPEN DECISIONS (don't drop — ask the user)

- **Git.** `TradingDesk` is a local git repo (no remote); memory says "commit after each change-set."
  A large change-set this session (`dailyreport\`, `datacollector\`, `connections\`) is UNCOMMITTED —
  held back to avoid colliding with the parallel session. Ask whether to commit or let that session do it.
- **Report recipient.** Still the tax-favored address (`andrew@taxfavoredretirement.com`) per the user's
  "keep current" choice, even though that account is being disconnected. Offer to repoint `RRG_MAIL_TO`
  in `C:\Users\andre\rrg_secrets.env` to `andrew@surberhc.com`.
- **NDX follow-ups** (see memory `options-warehouse`): (a) re-probe NDX on ThetaData BEFORE cancelling
  the subscription; (b) confirm the forward collector keeps capturing NDX; (c) validate QQQ history.

## ON DECK / FUTURE

- **Strategy EOD section** — `eod_report.SECTIONS` has a reserved `build_strategy()` slot; wire it in
  once strategies run (the user explicitly wants an end-of-day strategy update in the digest).
- The trading-engine build is the parallel session's project (`docs\HANDOFF.md`).

---

## HOW TO RUN / CHECK THINGS

```
# watch the ThetaData grab
"C:\TradingDesk-Local\venv\Scripts\python.exe" "C:\TradingDesk-Local\warehouse\monitor_download.py"
# run the EOD digest now (builds + emails)
"C:\TradingDesk-Local\venv\Scripts\python.exe" "…\TradingDesk\dailyreport\eod_report.py"
# refresh Tiingo now
"C:\TradingDesk-Local\venv\Scripts\python.exe" "…\TradingDesk\dailyreport\tiingo_daily.py"
# forward collector, safe slice test
"C:\TradingDesk-Local\venv\Scripts\python.exe" "…\TradingDesk\datacollector\ibkr_forward.py" --test SPY
```

Deeper detail: `dailyreport\README.md` (report layout), `docs\REORG_HANDOFF_2026-06-26.md` (the big
relocation), `datacollector\IBKR_SETUP.md` (Gateway settings), project memory (durable facts).
