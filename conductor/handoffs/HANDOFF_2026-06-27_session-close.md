# SESSION-CLOSE HANDOFF — 2026-06-27 ~19:15 CT

> Single source of truth for picking up next session. Read this first, then `conductor/STATUS.md`.
> Everything here is PAPER. Nothing has been transmitted. The first live paper rebalance is HELD for MONDAY.

---

## 30-SECOND ORIENTATION
- The desk **runs on its own** right now. Three things are alive on Andrew's PC and **survive this session closing** (they're Windows Scheduled Tasks / background processes, not tied to Claude):
  1. **SPXW 1-minute collector** — downloading, self-healing via Task Scheduler `Spxw1mCollector`. ~4% done, ETA ~July 1.
  2. **ThetaData terminal** — up (needed by the collector + the daily EOD grab).
  3. **Dashboard** — running at `http://localhost:8501` (phone: `http://192.168.4.20:8501`). Also relaunchable any time via the new **Desktop icon**.
- Daily automated jobs (Tiingo prices, ThetaData EOD grab, GEX rebuild, EOD email) run on their own via Task Scheduler — no Claude needed.
- **Claude/this session is the BUILDER, not the runtime.** Closing the session does not stop the desk.

## WHAT WAS DONE THIS SESSION (newest first)
- **Desktop one-click launcher** for the dashboard — `dashboard/launch_dashboard.bat` + `launch_dashboard.ps1`, Desktop shortcut `Trading Desk Dashboard.lnk`. If 8501 is already serving it just opens the browser; else starts Streamlit hidden (no console window, no duplicate processes) and opens the browser. Committed `9efef15`.
- Committed the live **smart-filter** change to `collect_spxw_1m.py` (drop-empty OHLC + store-on-change QUOTE) so on-disk source matches the running collector. Committed `4518aa9`.
- (Earlier this session) Phase-1 read-only **dashboard** built (`dashboard/app.py`, 4 tabs: Health / Gamma / Backtests / Accounts). Committed `de0c102`.
- (Earlier) gamma overlay, weekly-cadence, and flow de-risk gate all tested → **REJECTED** (S0's regime engine already captures the edge; anti-curve-fit discipline held).

## LIVE STATE SNAPSHOT (as of 19:15 CT)
- Collector: **51/1170 days (4.36%)**, ~1.70 GB on disk, avg ~256 s/day, **ETA ~2026-07-01 02:39**. PID 27608 (supervisor 10016). Progress heartbeat: `C:\TradingDesk-Local\warehouse\spxw_1m_progress.json`.
- 1 collector error logged: **day 20260529** (7 OHLC expirations failed) — self-heals / re-pull on a later pass; not blocking.
- Dashboard: PID 21636 on 8501. ThetaData terminal: up.

---

## OPEN ITEMS — running tally (carry forward)
**In progress (autonomous, no action needed):**
- [1] SPXW 1-min collector → ETA ~July 1. Just let it run; supervisor restarts it on crash/logon.

**Time-gated — MONDAY:**
- [6] **First live paper rebalance** (market + gateway must be up). All 5 client subs DU8922142–146 are funded ~$1.1M, enrolled, FLAT → each needs a full initial rebalance.
- [7] **Pre-arm check (do BEFORE arming):** eyeball the `replaceFA` FA-XML **tag casing** against a live `requestFA(1)` dump — run `paperbot/fa_probe.py`. Couldn't be confirmed offline. This gates [6].
- Runbook: **`MONDAY_RUNBOOK.md`** (repo root). Flow: `python rebalance_execute.py` (dry review, transmits nothing) → fa_probe casing check → `python rebalance_execute.py --arm-i-understand` (armed). clientId 38.

**Blocked on data (~July 1):**
- [5] S2 / S3 intraday condor backtests — need the 1-min SPXW data. S3 v1 EOD control already exists (the floor to beat: CAGR -6.12% / maxDD -53.45%).

**Owed by Andrew:**
- [8] **Gut-check the 2008/GFC number.** The Balanced calendar-2008 return jumped +3.4% → **+8.3%** after the regime margin fix + extended 2007 data. It's a big move — Andrew said he'd sanity-confirm it looks right before we lean on the GFC tables. See `backtester/VALIDATION.md` §2 and STATUS.md §A.

**Don't forget:**
- [11] **Do NOT cancel the ThetaData subscription** until the 1-min pull finishes (~July 1).

**Future (not started):**
- Dashboard **Phase 2** (run backtests from the UI) + **Phase 3** (gated arm/transmit from the UI). Phase 3 is what removes the command line for the Monday rebalance entirely. Phase 3 should reuse the existing 4-condition arm gate in `rebalance_execute.py`.

## KNOWN ROUGH EDGES (minor, non-blocking)
- `datacollector/features/gex.py:98` `_gamma_flip` throws **ZeroDivisionError when spot==0** for a symbol (seen on a thin symbol during the full GEX rebuild, e.g. NDX with only ~34 days). The full build still finished (exit=0, 35 derived tables); it just skips/crashes that one symbol. Worth a guard (`if not spot: return nan`) next time gex.py is touched.
- 4 disposable one-off Friday-backfill scripts are untracked in `datacollector/` (`backfill_20260626.py`, `repull_20260626_*.py`). Safe to delete; left in place.

## KEY FACTS / GUARDRAILS (don't relearn the hard way)
- Two roots: **CODE** in the Drive `TradingDesk` folder; **DATA/STATE/VENV/SECRETS** local on `C:\TradingDesk-Local` (never Drive-synced — Drive corrupts running files).
- venv `Scripts\python(w).exe` is a **relauncher stub** (spawns base interpreter as a child → duplicate-process bug). Launch the **base** interpreter `...\Python312\pythonw.exe` directly + put venv site-packages on `PYTHONPATH`. (Pattern in `run_spxw_1m.bat` and the new dashboard launcher.)
- Trade the **DU sub-accounts**, never the FA master DF8922141 (rejects direct orders + hangs reads).
- FA block allocation is governed by each **group's `ContractsOrShares`** config, not an order-level faMethod (order-level `faMethod="NetLiq"` → Err 10226). The engine computes explicit per-account shares.
- `whatIfOrder` on the paper gateway **HANGS** — never whatIf a group order.
- clientId registry: `connections/clientids.py`. In use: paperbot=30, flatten=34, fa_block=35, fa_admin=36, rebalance_run=37, rebalance_exec=38. Don't collide.
- Local git repo (in Drive, **no remote**). Commit after each change-set.
- PAPER only; never say "live" in the real-money sense; review→arm→transmit human gate is sacred.

## HOW TO RESUME NEXT SESSION
1. Read this file + `conductor/STATUS.md`.
2. Check collector progress: read `C:\TradingDesk-Local\warehouse\spxw_1m_progress.json`. If it finished (days_done==days_total) → unblock [5] S2/S3 backtests.
3. If it's Monday + market open + gateway up → run the [6]/[7] rebalance sequence per `MONDAY_RUNBOOK.md`.
4. Ask Andrew for his [8] GFC gut-check verdict if still owed.
