# RESUME HERE — data collector pickup (2026-06-26)

A fresh-session handoff. The prior session ran out of its drive (the tax-favored Google Drive is
being disconnected). Read this + `REORG_HANDOFF_2026-06-26.md` first. Everything important lives
off the tax-favored drive already; nothing was lost.

**Run Python with:** `C:\TradingDesk-Local\venv\Scripts\python.exe`
**Collector code:** `…\TradingDesk\datacollector\`  ·  **Warehouse (data):** `C:\TradingDesk-Local\warehouse\`

---

## THE ONE BLOCKER — ThetaData key is gone, must be re-fetched

The `thetadata_key` lived only in the deleted `C:\Users\andre\backtester\.env`; no local copy
survived (the resurrected Drive copy was an older Tiingo-only snapshot). Confirmed: none of the 3
surviving `.env` files has it. **Recovery = the ThetaData account portal:**

1. thetadata.net → log in → **API key** (regenerate if not shown — old one is dead anyway).
2. Add to the off-Drive secrets file as a new line — name it to match `config.THETA_KEY_NAME`:
   ```
   # in C:\TradingDesk-Local\secrets\.env
   thetadata_key=<paste key>
   ```
3. Then in `datacollector\config.py`, repoint:
   ```
   SECRET_ENV = pathlib.Path(r"C:\TradingDesk-Local\secrets\.env")   # was C:\Users\andre\backtester\.env (deleted)
   ```
   (`THETA_KEY_NAME` stays `"thetadata_key"`.) Verify masked (never print the value).

Until this is done, the Terminal can't start and nothing collects.

---

## IMMEDIATE NEXT ACTIONS (in order)

1. **Restore key + repoint `SECRET_ENV`** (above).
2. **Fix the catalog build** — `datacollector\storage.py::rebuild_catalog()` currently globs ALL
   parquet with `read_parquet(union_by_name=true)`, which DuckDB 1.5.4 **refuses** because many
   files are zero-column "no-data-day" markers. Fix (code-only): enumerate parquet files, keep only
   those with `pyarrow.parquet.read_metadata(f).num_columns > 0`, and build the view from that list.
   **Do NOT delete the empty files** — `have_day()` relies on them so the collector won't re-pull
   those days. (The 40-vs-41 column difference is fine for `union_by_name`; only zero-column breaks it.)
3. **Restart collection** with the self-healing supervisor (see Robustness below). It resumes —
   skips everything already on disk, finishes SPY/XSP, then pulls the 41 untouched roots.
4. **Fill the OI gaps** — ~35 real files are missing the `open_interest` column (VIX 31, VIXY 2,
   VXX 2; the OI endpoint returned empty those days). Re-pull those specific (symbol, day) files to
   try to fill OI. (Identify them by column count 40 vs 41, delete, let the collector re-pull.)
5. **Verify NDX** — once the Terminal is up, probe whether ThetaData actually has NDX index-option
   history. Right now NDX is **2,181 of 2,214 days empty** (~98.5%); looks like the data mostly does
   not exist at our tier, not a pull error. User wants NDX kept — but if ThetaData genuinely lacks
   it, **QQQ** (already in the universe) is the available, far more liquid Nasdaq-100 proxy. Report
   findings and decide.

---

## USER DECISIONS ALREADY LOCKED IN (don't re-ask)

- **Keep NDX**, try to get the files (but see #5 — may not exist on ThetaData).
- **Continue the full universe** — the reorg only changed file destinations, not the mission.
  All 50 roots in `config.UNIVERSE` (9 have data, 41 untouched).
- **Fill in missing data** (the OI gaps) while the subscription is still active.
- **Robustness:** user wants it to run unattended and asked for the recommended method (below).

## ROBUSTNESS — the recommended run method (build this)

Two failure modes, two layers:
- **Supervisor** (`datacollector\supervisor.py`, already built + has a singleton heartbeat guard):
  restarts the Terminal when its data-farm drops, restarts the download when it stalls (10-min
  no-new-files watchdog), loops until the grab completes. Logs to `warehouse\supervisor.log`,
  heartbeat `warehouse\supervisor_heartbeat.txt`.
- **Windows Scheduled Task** at logon → `C:\TradingDesk-Local\warehouse\run_supervisor.bat` →
  gives process survival + reboot survival (same pattern as the "RRG Daily Poll" task and the
  `C:\IBC` Gateway auto-launch). **Creating the task needs admin** (an elevated PowerShell) — this
  is a *user* action; an unprivileged shell gets "Access is denied". Suggested command (user runs
  elevated; delete the task when the grab finishes):
  ```powershell
  schtasks /create /tn "ThetaDataSupervisor" /tr "C:\TradingDesk-Local\warehouse\run_supervisor.bat" /sc onlogon /f
  schtasks /run    /tn "ThetaDataSupervisor"
  # when done:  schtasks /delete /tn "ThetaDataSupervisor" /f
  ```
  Lesson from last time: do NOT run the grab as a session-tied background job — it dies when the
  session/app closes (that's what caused the overnight stall). Note: the venv `python.exe` is a
  redirector stub, so each logical process shows as TWO in the process list (stub + real py3.12) —
  verify "one supervisor" via the **heartbeat**, not raw process count.

---

## CURRENT DATA STATE (warehouse, 2026-06-26)

~8.1 GB. 9 roots present; per-root data-day / empty-day counts:

| root | data | empty | note |
|---|---|---|---|
| SPX | 2130 | 84 | complete |
| SPXW | 2131 | 83 | complete |
| VIX | 2162 | 52 | 31 files missing OI |
| RUT | 1871 | 343 | usable |
| NDX | 33 | 2181 | ThetaData barely covers it — verify/decide |
| VXX | 2061 | 152 | 2 files missing OI |
| VIXY | 2127 | 86 | 2 files missing OI |
| SPY | 1906 | 74 | partial (history ~2020+) — RESUME to finish |
| XSP | 1097 | 41 | partial — RESUME to finish |

**Not started (41 roots):** QQQ, IWM, DIA, RSP, all 11 sectors, HYG/LQD/JNK, TLT/IEF/SHY,
GLD/SLV/GDX/USO/UNG, and the 15 single names. Data-file schema (non-empty) = 41 columns incl.
gamma, implied_vol, underlying_price, open_interest, full greeks. `GRAB_END=20260625` (bump if you
want today's day; current-day always fails the EOD endpoint anyway).

## BIGGER PICTURE (context, not blocking)
- This collector feeds the gamma/MSR + condor strategies. Strategy roster: `datacollector\STRATEGIES.md`.
- Downstream projects (separate sessions): the **IBKR forward collector** (record each new day free
  after the one-time ThetaData grab — connection PROVEN: ib_async, Gateway 127.0.0.1:4002, paper
  DU…141 has LIVE data; see `datacollector\ibkr_*.py`, `IBKR_SETUP.md`) and the **paper/live
  trading engine** (`docs\HANDOFF.md` — paper-first, dry-run-first, human-gated to live).

---
*Start: tell me to read this file. First real step = restore the ThetaData key (blocker), then the
catalog fix, then resume collection under the supervisor.*
