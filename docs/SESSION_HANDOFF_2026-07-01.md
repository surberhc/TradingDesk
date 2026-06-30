# Session handoff — 2026-06-30 (close)

Research / PAPER only. Nothing live traded. paperbot v0.12.0.

## Orientation
Built the account cashflow-management layer end-to-end, added a single-process gateway mutex, scheduled the account monitor daily, hardened all desk scheduled tasks to run unattended, added a reference paper + a regime lead + a live-resilience design stub, and laid down a root CLAUDE.md operating contract. The 1-min SPXW collector finishes tonight (~23:24 CT), which unblocks the S5 real harvest engine — the #1 next build.

## Shipped this session (committed; paperbot v0.6.0 → v0.12.0)
- **Root CLAUDE.md** operating contract (cd58157). Finding: there is NO byte-parity test — paperbot↔backtester parity is STRUCTURAL (paperbot/strategy_target.py calls the backtester's own run_backtest); the old "byte-identical" hashes are stale/unreproducible.
- **Cashflow-management layer (paperbot):** Slice 1 consolidate buffer math → investable.py (f6444f9, v0.6.0); Slice 2 buffer 5%→1.5% (ea497fb, v0.7.0); Slice 3 explicit execution-side CASH bucket + honest drift (1729775, v0.8.0); Slice 5 monitor brain account_monitor.py, pure propose-only Verdict/decide (84c91fd, v0.9.0); Slice 6a deposit detection (402c762, v0.10.0); Slice 6b earmark fence + sale-raised nudge + live read-only shell account_monitor_run.py clientId 40 (b10eb6b, v0.11.0). Decisions: cash bucket EXECUTION-SIDE (backtester untouched); buffer 1.5%→1.0% later; monitor full-generic but propose-only. Slice 4 (real client withdrawal SCHEDULE data) PARKED — personal client data, far off; SCHEDULE empty, fixture-tested only.
- **Gateway lock** (single-process mutex; reuses collector PID-lock pattern; lock file C:\TradingDesk-Local\state\paperbot\gateway.lock off-Drive; heartbeat lease): Slice 1 gateway_lock.py + tests (328dbdf, inert); Slice 2 monitor SKIP-on-busy (3c11179); Slice 3 rebalance run+execute WAIT-then-REFUSE naming holder (c98c418, v0.12.0). Ruled params: monitor wait 10s→skip; reclaim after 5min silent heartbeat; rebalance wait 30s→refuse. Resolves scheduler F2.
- **Account monitor SCHEDULED:** AccountMonitorDaily, daily 16:30 CT, read-only/propose-only, gateway-lock-guarded, clientId 40 (SCHEDULER_PLAN.md updated d5d1623). Live test-run verified (5 DU accounts HOLD/IN_BAND, lock released).
- **All 8 desk scheduled tasks HARDENED** to "run whether logged on or not" (LogonType=Password) via C:\TradingDesk-Local\warehouse\harden_scheduled_tasks.ps1. Survives sign-out / overnight reboot. CAVEAT: re-run that script after any Windows password change.
- **Reference library** (docs/reference/, PDFs gitignored): Lost Decades + Tactical Options (6705c96); AsymmetricReturns / AllianceBernstein (651739a) — best external statement of S5's convexity/tail thesis.
- **Regime backlog** docs/REGIME_RESEARCH_BACKLOG.md (a9ead99) — NEW lead: breadth-thrust as re-entry trigger (HIGH curve-fit risk; must beat fixed MAX_LAG without the bear-rally trap, OOS beyond 2022).
- **Scheduler plan** docs/SCHEDULER_PLAN.md (e3fb53a) — 8-job inventory, timing/conflict map, GUI earmark-checkbox idea.
- **LIVE_RESILIENCE stub** docs/LIVE_RESILIENCE.md (7131349) — server-resting-protection gap list; S5/intraday-live BLOCKED on it; #1 prereq = kill-the-gateway paper probe (IBKR offline-trigger unproven).
- **S5 foundations:** 1-min data reader backtester/s5_intraday_data.py (0988763, tested on real on-disk days); ledger experiment — endogenous self-funding ledger WINS twitchy-bleed (~+1.3pp), ties full-cycle; nothing adopted.

## Running unattended
- SPXW 1-min collector — self-healing (Task Scheduler), ETA tonight ~23:24 CT (82% at 09:41). Finishing unblocks S5 harvest + S2/S3 + DDOI.
- Paper gateway — up, read-only/locked (monitor relaunches it daily). Nothing armed.

## Pick up here (next session, prioritized)
1. **S5 real harvest engine** — #1 build once the 1-min data is down (tonight). Offensive/income half on the new s5_intraday_data.py reader; real harvest rate / calm-day count / loss distribution. Anti-curve-fit gate.
2. **S2/S3 condor backtests + DDOI gamma build** — also unblocked by the 1-min data.
3. **Intraday-gamma early-exit revisit** — data-gated; HIGH curve-fit risk, start alert-only.
4. **Scheduler design polish** (SCHEDULER_PLAN.md): evening-chain ordering (F3), failure alerting, holiday calendar.
5. **Monitor reps + GUI** — hands-on reps; surface verdicts + the withdrawal-vs-rebalance earmark checkbox in the dashboard.

## Parked / future
- Cashflow Slice 4 (real client withdrawal data) — far off.
- Live-resilience build — future; kill-the-gateway probe is the gating prereq; S5-live blocked.
- Gateway lock optional Slice 4 (wrap read-only probes) — minor.

## Ops notes
- paperbot v0.12.0. Tests: paperbot 200 passing; backtester 104 passing (untouched).
- Re-run harden_scheduled_tasks.ps1 after any Windows password change.
- Desk tasks now run whether logged on or not — survives reboot-to-login-screen.
