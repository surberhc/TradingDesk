# SESSION-CLOSE HANDOFF — 2026-06-28 ~19:00 CT

> Single source of truth for picking up next session. Read this first, then `conductor/STATUS.md`.
> Everything here is PAPER. Nothing has been transmitted. The first live paper rebalance is HELD for MONDAY.

---

## 30-SECOND ORIENTATION
- The desk **runs on its own** right now. Three things are alive on Andrew's PC and **survive this session closing** (Windows Scheduled Tasks / background processes, not tied to Claude):
  1. **SPXW 1-minute collector** — downloading, self-healing via Task Scheduler `Spxw1mCollector`. **~32% done, ETA ~June 30** (see live snapshot below).
  2. **ThetaData terminal** — up (needed by the collector + the daily EOD grab). It **died once this session and was manually recovered** — see the OUTAGE note; a watchdog is still NOT built.
  3. **Dashboard** — running at `http://localhost:8501` (phone: `http://192.168.4.20:8501`). Relaunchable any time via the Desktop icon "Trading Desk Dashboard".
- Daily automated jobs (Tiingo prices, ThetaData EOD grab, GEX rebuild, EOD email) run on their own via Task Scheduler — no Claude needed.
- **Claude/this session is the BUILDER, not the runtime.** Closing the session does not stop the desk.
- This was a heavy **research + audit** session. The headline: **the regime engine is ROBUST — nothing structural cleanly improves it without a cost.** Config was NOT touched. Details below.

## WHAT WAS DONE THIS SESSION (newest first)
- **Dashboard restyle** — compact density pass (`6b3609a`), dark theme + colored metrics + status row + last-refreshed line + badge legend (`acb0bb6`), clipped-title fix (`99865da`). The deeper look-improvement roadmap (gamma grid, GEX zero-line chart, consistent formatting) is **NOT yet implemented** — quick wins only.
- **GFC +8.3% — AUDITED CLEAN + active-2008-navigation CONFIRMED.** The +8.3% calendar-2008 number closed clean under a 3-way audit (committed `55cd4c1`). **PLUS** a definitive warm-up test (separate dir `bt_data_ext2005`; canonical `bt_data` untouched) **CONFIRMED the active 2008 navigation is REAL, not a warm-up artifact** — trend/breadth/vol signals warmed by 2006-03 and the engine was already at CapitalPreservation by late-2007/Jan-2008, *before* the worst of the crash. CAVEAT: the CREDIT half of the GFC-entry read is **unwarmable** (HYG inception ~2007-04), so the entry was driven by trend/breadth/vol, **not** credit. `VALIDATION.md` §5 softened accordingly.
- **Gamma symbol verdict held + characterized.** Daily S0 signal stays on **SPX root** (best matches the Tier1Alpha vendor labels: 69.8% vs SPXW 60.1% / combined 65.5%-null). The residual gap is a **genuine method difference** on the NEGATIVE-gamma side (our static dealer-sign vs vendors' inferred DDOI) — closing it needs DDOI-style trade-direction inference off the 1-min data (future build). Gamma is **HORIZON-DEPENDENT**: daily SPX for S0; intraday SPXW/0DTE for S2/S3. `gex.py` spot<=0 ZeroDivisionError fixed (`a953389`). SqueezeMetrics/SpotGamma methodology captured in memory.
- **Vol-control borrows as an S0 OVERLAY: TESTED → REJECTED** (subordinate to the regime band; the band already collapses exposure in crises). Pulled VIX3M/VIX9D/VVIX into `bt_data`. `FRED_API_KEY` added to `secrets/.env` and verified (authenticates, but HY OAS returns only ~2023-06+ due to ICE's rolling-3yr restriction — **can't warm 2008**); docs committed `610507c`.
- **NEW STRATEGY LEAD — S4 "SPX vol-control fund."** The SAME vol-control mechanics (daily exposure = `min(leverage_cap, target_vol / realized_vol)`), but as a **STANDALONE single-asset SPX fund** (an FIA-style vol-control index replica), **NOT** an overlay on S0 — which sidesteps the "subordinate to the regime band" rejection above. `target_vol` and `leverage_cap` both swept. **Buildable today.** OPEN new-strategy lead. (Memory note `s4-spx-vol-control-fund` exists.)
- **Regime-engine structural exploration — NOTHING ADOPTED, config untouched** (all in-process tests). See the next section; this is the intellectual core of the session.

## REGIME ENGINE — STRUCTURAL EXPLORATION (read this; the conclusion is a NEGATIVE result and that is the point)
- **Characterization of the bleed:** it is **re-entry lag + shallow-dip whipsaws**, NOT bad crisis exits. Recent examples: 2025 cut to 0% equity on a ~−8.6% dip, 2026 cut 85%→24% on a ~−5–8% dip — **both round-tripped**. The deep-crisis exits are GOOD; leave them alone.
- **Existing knobs are a robust plateau** and cannot fix the whipsaw.
- **Re-entry `MAX_LAG` 6→3:** looked free in Stage A, but the **final per-episode safety gate FAILED it** — 3 episodes worsen by >100bp (2008 tail −118bp, 2011 −114bp, 2015-16 −152bp). It is a **risk-budget TRADE-OFF**, not a free win: full-window maxDD is byte-identical (−10.20%, no new tail risk) and it WINS episode-NAV in 2009 (+2.76%) and 2011 (+0.95%); the only genuine loser is the SIDEWAYS 2015-16 grind. The benefit is smaller than first quoted (2022 lag ~14→12 months). **HELD — NOT adopted. Config untouched (`REENTRY_MAX_LAG_MONTHS` stays 6).**
- **Exit gates can't fix the whipsaw either:** a dumb drawdown-DEPTH gate FAILS (depth cannot separate whipsaws from real-crash first legs — they overlap at −7% to −9%); **gamma / vol / term-structure overlays ALSO cannot separate them.** The exit-whipsaw is **not signal-separable by any overlay we tried.**
- **NET:** the regime engine is **ROBUST — no structural tweak (re-entry knobs, exit gates, gamma/vol/term-structure overlays) cleanly improves it without a cost.** This is a **curve-fit-PREVENTING** result and the right outcome to record.
- **PRIME OPEN LEAD (next session):** the re-entry failure is isolated to the **override firing in SIDEWAYS grinds, not clean V's.** Tighten the override's `sharp_recovery` trigger to fire **only on clean V-recoveries**, then re-run the per-episode gate. **HIGH curve-fit risk** — needs a principled trigger + an OOS re-test before anything is adopted. Until/unless it clears, re-entry stays at `MAX_LAG = 6`.

## LIVE STATE SNAPSHOT (as of ~18:47 CT, from `spxw_1m_progress.json`)
- Collector: **374 / 1170 days (31.97%)**, ~10.49 GB on disk, current day 20241227, avg ~220 s/day, **0 errors**, **ETA ~2026-06-30 19:30** (~48.7h remaining). Heartbeat: `C:\TradingDesk-Local\warehouse\spxw_1m_progress.json`.
- ThetaData terminal: up (recovered after the ~14:47 outage). Dashboard: up on 8501.

## OUTAGE + RECOVERY (root cause + the open watchdog item)
- ~**14:47** the **ThetaData terminal + collector + dashboard all died.** Recovered: terminal relaunched via `datacollector/start_terminal.py`; the collector **resumed from day ~342, NOT from scratch** (the progress heartbeat + on-disk dedupe made the restart cheap).
- **ROOT CAUSE:** there is **no watchdog that auto-restarts the ThetaData TERMINAL** (the collector's own supervisor restarts the *collector*, but if the terminal on port 25503 dies, the collector just stalls). 
- **OPEN:** build a **port-25503 watchdog scheduled task** that relaunches the terminal if the port goes dark. Recommended, not yet built.

---

## OPEN ITEMS — running tally (carry forward)
**In progress (autonomous, no action needed):**
- [1] SPXW 1-min collector → ETA ~June 30. Let it run; supervisor restarts it on crash/logon. **Do NOT cancel the ThetaData sub until it finishes.**

**Time-gated — MONDAY:**
- [6] **First live paper rebalance** (the big one) — market + gateway must be up. All 5 client subs DU8922142–146 are funded ~$1.1M, enrolled, FLAT → each needs a full initial rebalance.
- [7] **Pre-arm check (do BEFORE arming):** eyeball the `replaceFA` FA-XML **tag casing** against a live `requestFA(1)` dump — run `paperbot/fa_probe.py`. Gates [6].
- Runbook: **`MONDAY_RUNBOOK.md`** (repo root). Flow: `python rebalance_execute.py` (dry review, transmits nothing) → fa_probe casing check → `python rebalance_execute.py --arm-i-understand` (armed). clientId 38.

**Open research leads (nothing adopted):**
- **Regime `sharp_recovery` refinement** — the PRIME lead (clean-V-only override trigger + per-episode gate re-run). HIGH curve-fit risk. Re-entry stays `MAX_LAG=6` until it clears.
- **S4 SPX vol-control fund** — new, buildable, standalone single-asset strategy idea.
- **DDOI gamma build** — trade-direction inference off the 1-min data to close the negative-gamma method gap vs vendor labels. Blocked on the 1-min pull.
- **Intraday-gamma early-exit revisit** (~June 30 per memory) — can intraday gamma give the monthly core an earlier exit / faster re-entry? HIGH curve-fit risk; alert-only first.

**Infra (recommended, not built):**
- **ThetaData terminal WATCHDOG** (port-25503 scheduled task). See the OUTAGE note.

**Blocked on data (~June 30, when the collector finishes):**
- [5] S2 / S3 intraday condor backtests — need the 1-min SPXW data. S3 v1 EOD control already exists (floor to beat: CAGR −6.12% / maxDD −53.45%).
- **PV-band ("Probable Volatility Bands") chart** for Andrew — ready to build on SPX.

**Owed by Andrew:**
- [8] **Final nod on the GFC +8.3% number.** It is now INDEPENDENTLY AUDITED CLEAN (3 passes) and the active-2008-navigation is CONFIRMED real — pending only Andrew's sign-off to fully close. See `backtester/VALIDATION.md` §2/§5 and `backtester/output/gfc_decomposition_2026-06-28.md`.

**Future (not started):**
- Dashboard look-improvement roadmap (gamma grid, GEX zero-line chart, consistent formatting) — beyond the quick wins done this session.
- Dashboard Phase 2 (run backtests from the UI) + Phase 3 (gated arm/transmit from the UI).

## KNOWN ROUGH EDGES (minor, non-blocking)
- 4 disposable one-off Friday-backfill scripts are untracked in `datacollector/` (`backfill_20260626.py`, `repull_20260626_*.py`). Safe to delete; left in place.
- `FRED_API_KEY` authenticates but ICE HY OAS only returns ~2023-06+ (rolling-3yr restriction) — cannot warm 2008; HYG/IEF proxy retained for history.

## KEY FACTS / GUARDRAILS (don't relearn the hard way)
- Two roots: **CODE** in the Drive `TradingDesk` folder; **DATA/STATE/VENV/SECRETS** local on `C:\TradingDesk-Local` (never Drive-synced — Drive corrupts running files).
- **Gamma is HORIZON-DEPENDENT:** daily SPX root for S0 (validated, matches vendor labels best); separate intraday SPXW/0DTE signal for S2/S3.
- venv `Scripts\python(w).exe` is a relauncher stub (duplicate-process bug). Launch the **base** interpreter `...\Python312\pythonw.exe` directly + put venv site-packages on `PYTHONPATH`.
- Trade the **DU sub-accounts**, never the FA master DF8922141 (rejects direct orders + hangs reads).
- FA block allocation is governed by each **group's `ContractsOrShares`** config, not an order-level faMethod (`faMethod="NetLiq"` → Err 10226). The engine computes explicit per-account shares.
- `whatIfOrder` on the paper gateway **HANGS** — never whatIf a group order.
- clientId registry: `connections/clientids.py`. In use: paperbot=30, flatten=34, fa_block=35, fa_admin=36, rebalance_run=37, rebalance_exec=38. Don't collide.
- Local git repo (in Drive, **no remote**). Commit after each change-set.
- PAPER only; never say "live" in the real-money sense; review→arm→transmit human gate is sacred.
- **Regime config is UNTOUCHED** (`REGIME_TREND_MARGIN=0.03`, `REENTRY_MAX_LAG_MONTHS=6`). The re-entry 6→3 change was TESTED and HELD — do not let stale notes claim it was adopted.

## HOW TO RESUME NEXT SESSION
1. Read this file + `conductor/STATUS.md`.
2. Check collector progress: read `C:\TradingDesk-Local\warehouse\spxw_1m_progress.json`. If it finished (days_done==days_total) → unblock [5] S2/S3 backtests, the DDOI gamma build, the PV-band chart, and the intraday-gamma early-exit revisit.
3. If it's Monday + market open + gateway up → run the [6]/[7] rebalance sequence per `MONDAY_RUNBOOK.md`.
4. If the ThetaData terminal is down again → relaunch via `datacollector/start_terminal.py`; consider building the port-25503 watchdog.
5. Ask Andrew for his [8] GFC final-nod if still owed.
