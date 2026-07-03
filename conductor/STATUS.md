# LANE STATUS  (last updated by: Conductor, 2026-07-03 — overnight EOD+Tiingo fixes verified)

> Live dashboard. RAW SESSION HANDOFFS live in `conductor/handoffs/` + dated `docs/SESSION_HANDOFF_*.md`.
> **PICK UP HERE NEXT SESSION → `docs/SESSION_HANDOFF_2026-07-01.md`** (full 2026-06-30 session-close handoff).
> **First multi-account PAPER rebalance: DONE 2026-06-29 — all 5 accounts (DU142-146) in-band.** Built + proved live a dynamic laddered order router + self-verifying hands-free gateway arming + `--only-account` scope flag (memory `dynamic-order-router`, `paper-arming-and-fills`). Gateway disarmed + verified-locked.
> Desk runs on its own: collector + ThetaData **terminal-watchdog (NEW, live)** + dashboard survive session close. **Collector ~59%, ETA 2026-06-30 ~18:19 CT** — gates S5 harvest + S2/S3.
> **Regime config still UNTOUCHED.** `sharp_recovery` refinement TESTED 2026-06-29 → SHELVED (clean negative; GFC fails −118bp filter-independently). Re-entry stays MAX_LAG=6. 2008 GFC audit CLOSED.

### 2026-07-03 — Overnight EOD + Tiingo report bugs FIXED & both fixes VERIFIED this session
- Two nightly-report bugs bit 2026-07-02 night; both were already fixed in commit `cac0e9e` (21:05) and are now INDEPENDENTLY VERIFIED this session (no downloader run, no repo files modified beyond this log).
- **(1) EOD email crashed at 21:00 — `KeyError: 'partial'`** in `render_html`: the Tiingo section legitimately emits a `partial` status that `DOT`/`SEVERITY` didn't know. Fix added `partial` as a first-class status (amber `#fbbf24`, ranked stale<partial<warn so `_overall` classifies it, not as fail) + made all three DOT lookups defensive (`.get` w/ grey fallback). **VERIFIED via no-send dry-run:** real section builders + `render_html` render clean, AND a forced synthetic `partial` section with `overall='partial'` both render (12,328 chars, zero traceback) — the exact 21:00 crash path. The mailer SEND path was deliberately NOT exercised (skipped to avoid a duplicate email); it is still first-proven only at tonight's live 21:00.
- **(2) Tiingo false-`partial` every day** — `tiingo_daily.py` read the manifest from the dead on-Drive path (`backtester/data/_manifest.json`, gone since the dataset moved off-Drive), so the read was always empty → `generated=""` → `fresh=False` → forced `partial` nightly (which then dinged EOD to `warn`). Fix resolves the real manifest via `config.MANIFEST_FILE` (fallback to the known local path). **VERIFIED:** old path confirmed ABSENT; new path resolves through the real `config.MANIFEST_FILE` branch (not the fallback) to `C:\TradingDesk-Local\bt_data\_manifest.json` (exists; 32 tickers; `data_end=2026-07-02`; 0 critical QC); on the manifest's own refresh day the logic now yields `ok` — impossible before the fix. Today (07-03) it reads a LEGITIMATE `partial` (downloader hasn't refreshed yet today), not the path bug.
- **CANSLIM overlay pipeline (context, not a bug tonight):** emailed TWICE 2026-07-02 — 19:00 with placeholder numbers (false-complete resume on a stale test heartbeat, fixed `0acd9ee` 20:19), then correctly at 21:47 (option base $805,656 vs stock $950,167, −$144,511). Trust the 21:47 email. The equity-options PULL itself SUCCEEDED (2407/2407 name-months, 17.3M rows, 0 failed); the alarming `pull_equity_options_poison.json` is benign current-day (07-02) skips — ThetaData won't serve current-day EOD without an expiration.
- **WATCH tonight (21:00):** first live end-to-end confirmation of the EOD mailer SEND under the fix; and Tiingo → `ok` (instead of false `partial`) once the 16:30 CT downloader refreshes same-day — should stop needlessly dinging EOD to `warn`.

### 2026-07-02 — SPX-ROOT 1-min pull COMPLETE (DDOI overlap window) + reusable parallel harness
- **SPX-ROOT 1-minute data DOWNLOADED for the DDOI vendor-overlap window** (2025-05-01→2026-06-18, **285 real trading days**, quote+ohlc) to resolve the DDOI cross-symbol caveat (vendor labels the SPX-root regime; our only prior intraday tape was SPXW). The 11 "missing" weekdays are confirmed US market holidays with no data — recorded in `C:\TradingDesk-Local\warehouse\spx_1m_known_empty.json` (off-Drive, not in repo) so any future SPX resume excludes them and exits cleanly at 100%.
- **Reusable PARALLEL collector harness built:** `datacollector/spx_1m_parallel.py` (sharded supervisor, burst-and-release) + `datacollector/collect_spx_1m.py` (SPX sibling of the SPXW collector) + `datacollector/_probe_concurrency.py`. Commits `06e8506` / `c51b9c0` / `eb110c8`.
- **Measured finding (reusable for future pulls):** one ThetaData terminal scales cleanly to **~4 concurrent shards with ZERO rate-limiting**; SUSTAINED throughput **~2.85x vs serial** (the probe overstated at 4x); beyond 4 shards no gain (latency-bound). Cut the ~11h serial pull to ~3h.
- **Teardown:** the temp `Spx1mParallel` scheduled task + session watch-loop torn down on completion; the now-cold `spx_1m_parallel` entry REMOVED from `heartbeat_alarm.py`'s JOBS list (SPXW entry + everything else untouched) so it doesn't fire a false "spx stale" email. The HeartbeatStalenessAlarm task itself is unchanged.
- **DDOI re-run on SPX-root COMPLETE — STILL REFUTED on the matching symbol.** On the SPX-ROOT 1-min tape (285 days / 281-day vendor overlap, 2025-05-01→2026-06-18), DDOI **44.8%** vs static **69.8%** gamma_state accuracy (−24.9pp), worse in BOTH halves (H1 −27.1pp, H2 −22.7pp); negative-side recall barely moves (+3.2pp whole, inconsistent across halves). The static baseline reproduced the validated 69.8% SPX-root number, so the pipeline is sound and DDOI genuinely over-calls Negative (~44 static-Positive days flipped). **Cross-symbol caveat CLOSED** — vendor labels and tape are now both SPX-root, so this is a GENUINE method failure, not a symbol artifact. Nothing wired into frozen S0; existing static dealer-sign method retained unchanged. Commit `0af2b8c` (generalized `ddoi_gamma.py`/`ddoi_run.py` with `--symbol SPX|SPXW` + `s5_intraday_data` reader; report `output/ddoi_gamma_spxroot_20260702.md`, output/ gitignored). 179 backtester tests pass incl. causality guard.

### 2026-07-02 — EOD email silent-death FIXED + full scheduler/alerting audit & hardening
- **Nightly EOD email died silently 5 nights (6/27–7/01) — ROOT-CAUSED + FIXED (commit `efb25c9`).** TWO independent triggers, both fixed: (1) `build_system()` opened a live `ib_async` asyncio gateway socket inside a report builder — crashed the process under the non-interactive pythonw/Task-Scheduler context BEFORE the email sent (per-section try/except can't catch a process-level death); (2) `_overall()` raised `ValueError` on status `"partial"` (forward/tiingo write "partial" nightly; not in SEVERITY) — deterministic every night, likely the primary. Fix: TCP port-check replaces the ib_async probe (a status report must NEVER open a trading socket); main() fully wrapped so any failure still sends a fallback error email + 30s per-section timeouts; `_overall` hardened. EodReport task triggers were already fine (StartWhenAvailable, run-whether-logged-on) — the trigger was never the problem.
- **Adversarial 3-worker audit** of every desk task / launcher / entry-point / watchdog / timing-dependency + a pattern-sweep for the ib_async-in-report bug class. No other live-socket-in-report holes; that class is closed on all ACTIVE scheduled paths (the only remaining instance is the DISABLED `ThetaForwardDaily` orphan).
- **Alerting hardening (commit `e735894`):** (1) `mailer.py` drops a Desktop `TRADINGDESK_EMAIL_DOWN.txt` on send-failure + `last_email_ok.txt` on success = the FIRST out-of-band signal — previously EVERY report AND alarm shared the one Gmail path, so a revoked app-password blinded the whole system silently; (2) `eod_report` exits non-zero on failed send (Scheduler shows red) + new `build_alarm()` section goes red if the staleness alarm itself hasn't run in 30 min; (3) `heartbeat_alarm` generalized to `DEADLINE_JOBS` — forward/tiingo/gex/account_monitor each now get an independent daily-deadline alarm (was only spxw_1m+eod) and it writes `heartbeat_alarm_ran.txt` so the digest watches it back (mutual coverage); (4) `build_features`(gex) + `account_monitor_run` now write status JSONs (+ gex.log) — both were fully invisible before.
- **REAL DATA BUG fixed (commit `b94717b`):** the daily options grab requested the unsettled current day with `expiration=*` → HTTP 400 on EVERY current-day root → same-day dealer-gamma never landed, masked as amber "partial" by prior-day self-heal. Now enumerates live expirations per current day (settled days keep the fast `expiration=*` path); `compute_status()` returns fail/red on current-day-zero. Verified live: SPY current-day OI 410 rows, no 400; settled SPX day still 13,962 rows. No warehouse writes during testing.
- **OPEN — needs Andrew (can't be done programmatically):** run `C:\Users\andre\Desktop\harden_desk_tasks.ps1` once (prompts for Windows password) — adds ExecutionTimeLimit (PT1H, Tiingo PT2H) to the 5 nightly tasks + deletes the disabled `ThetaForwardDaily` orphan. Windows blocks changing run-whether-logged-on (Password-principal) tasks without the password. Belt-and-suspenders — in-process timeouts already cover most hang risk.
- **OPEN — optional:** `eod_daily.py --date 20260701` to backfill the one genuinely-missing same-day file (nightly self-heal will otherwise close it).
- **WATCH tonight:** the 21:00 EodReport run is the end-to-end real-world confirmation; sections should read fresh/green for the first time under the new status writes. If no email arrives, check the Desktop for `TRADINGDESK_EMAIL_DOWN.txt` (now distinguishes an email-channel failure from a silent death).
- Memory `rrg-email-pipeline` updated. NOTE: warehouse `.bat` launchers (run_eod/run_gex/etc.) live off-repo on `C:\TradingDesk-Local\` and are NOT in git.

### 2026-07-01 — S5 real harvest engine (post-1-min-data build) — HARVEST REFUTED as financing leg
- Now that the SPXW 1-min backfill is COMPLETE, spun up 3 data-gated workers: **S5 harvest engine (done), S2/S3 intraday condor (done), DDOI negative-gamma (done).** All three data-gated by the 1-min feed; all three REFUTED honestly (no curve-fit, frozen config untouched).
- **S5 harvest = DONE, full 1126-day sample (1089 traded, 2022-01-03->2026-06-30).** Rule (FROZEN, not swept): fixed 0.15-delta iron condor, ~14:00 ET entry, HONEST fills (sell bid/buy ask), 5-wide defined risk, HELD FLAT TO 16:00 settlement (no 2x stop -- the always-on tail is the backstop, so the financing leg carries no intraday stop). Distinct from the refuted S6 2x-stop chassis.
- **VERDICT: naive calm-day 0DTE harvest does NOT self-fund the tail -- it DRAINS cash.** Mean net **-$13.98/sell-day** (CALM VIX<=15 **-$16.10**, i.e. calm days WORSE), loss/win **4.27x**, win rate 77.6% (cosmetic), **0/5 calendar years net-positive**, net-negative in both time-halves + every VIX tercile. Root cause = the ~23.7% of days that breach eat the full 5-wide wing vs a thin ~$79 credit; calm-day breach rate flat ~23-24% so calm collects less credit for same risk (efficient-market signature, same as the VRP experiment). As a financing leg = **-0.57%/yr of core** vs the +1.56%/yr (0.50/25% sweet-spot) / +4.46%/yr (full deep) carry it must cover; scaling up multiplies the bleed + piles on short gamma (violates Design Rule A).
- **Does NOT touch S5's validated tail/defensive half** -- only refutes naive premium-selling as the financing SOURCE. Files: `backtester/s5_harvest_engine.py` (2552bc4), `s5_harvest_analysis.py` (06fa455/ce29724), `tests/test_s5_harvest_engine.py` (5 mechanics tests). Report `output/s5_harvest_engine_20260701.md`. Worker reports 176 backtester tests green + causality guard green (commits + report VERIFIED on disk by conductor; full suite not independently re-run while unattended). Memory `s5-financed-convexity-overlay` updated.
- **OPEN STRATEGY CALL for Andrew (do NOT decide autonomously):** finance the tail some OTHER way (further-dated theta / broken-wing-ratio / delta-hedged short-vol -- all UNTESTED), OR accept S5 as a DEFENSE-ONLY overlay whose honest cost is the ~1.56%/yr paid carry (the FIA-carrier reading, no harvest offset -- materially changes S5's economics). Naive calm-day 0DTE selling is REFUTED -- do not re-test it.

### 2026-07-01 — S2/S3 intraday 0DTE iron-condor (post-1-min-data build) — REFUTED
- Built/fixed `backtester/s2s3_intraday_condor.py` (the intraday piece the EOD `s3_condor_control.py` deferred): fixed 0DTE iron-condor CONTROL on the intraday 1-min P&L path + one deferred overlay (morning-gap gate). Full history 1126 warehouse days / 1060 tradeable (2022-01-03->2026-06-30).
- **v1 CONTROL** (fixed 0.15-delta 0DTE condor, honest fills, hold-to-settle w/ 2x stop): **-$32,870** (train -$21,990 / test -$10,880) — losing yardstick as expected; reproduces the S6 iron-condor control BYTE-FOR-BYTE on all 11 overlapping both-traded days (engine validated, structurally can't drift).
- **v2 GAP-GATE** (sit out days whose open gap > 2.0x trailing-20d median): **REFUTED by placebo.** Face-value +$4,995 both halves, but (a) only lifts a NEGATIVE book toward zero (loss-reduction, not profit), (b) FAILS the sub-bucket plateau (pos-gamma/backwardation -$90), (c) WORSE THAN RANDOM — random sit-out of the same 224 days gains $6,911 avg and beats the gate in 82% of 3000 draws. Same trade-fewer-days-on-a-losing-book artifact S6 taught. Placebo + sub-bucket check baked into the engine so the honest verdict always emits.
- **Measurement (decompose-first):** gap->day-range Spearman +0.33 (real but doesn't map to avoidable LOSSES); **morning-rvol->PM-range +0.59, stable OOS** = the one genuinely stronger lead. PV-band artifact: close within +/-1 EM ~74%, +/-2 EM ~95%; control's own shorts contained the close only 76% of days (~1-in-4 breach) = the mechanical reason it bleeds at honest fills.
- Files: `s2s3_intraday_condor.py`, `tests/test_s2s3_intraday_condor.py` (9 tests), report `output/s2s3_intraday_condor_20260701.md`, data `output/s2s3_research/*.csv`. Commits `ae0c4c0` / `02adca4` / `29dbf27`. Full suite 176 pass + causality guard 2/2 (worker-reported; commits/report VERIFIED on disk by conductor). ENV GAPS: venv lacks scipy (numpy-Spearman workaround) + matplotlib (PV-band CSV renders, PNG deferred) — minor follow-up.
- **OPEN (needs Andrew's blessing, NOT a retune):** does an afternoon-vol-scaled version (morning-rvol->PM-range, rho +0.59) ever convert range-predictiveness into avoided LOSSES when tested vs this same losing control AND the random-sit-out placebo — or die at the placebo like the gap gate? New pre-registered test only.

### 2026-07-01 — DDOI inferred-dealer-direction gamma (post-1-min-data build) — REFUTED
- Built `backtester/ddoi_gamma.py`: Lee-Ready trade-direction classifier (quote rule vs prevailing NBBO mid, tick-rule fallback) signs every SPXW 1-min print buyer/seller-initiated, aggregates to per-contract inferred dealer sign (dealer = opposite of net customer flow), rebuilds net-GEX with that INFERRED sign replacing the static long-call/short-put assumption — everything else (gamma/OI/spot/gross/Neutral band) inherited verbatim from `datacollector/features/gex.py` so the ONLY moving part is the sign. Resumable driver `ddoi_run.py` + 8 tests. Perf fix: merge_asof onto traded minutes only = byte-identical mids, 5x faster (39s->7.5s/day).
- **Honest match vs Tier-1-Alpha vendor labels (281-day overlap 2025-05->2026-06):** static baseline **60.1%** gamma_state accuracy vs DDOI **36.7%** (-23.5pp); worse in BOTH halves (H1 57.1 vs 32.1, H2 63.1 vs 41.1). DDOI raises NEGATIVE-side recall (+19pp) but ONLY by calling nearly everything Negative (static 443 -> DDOI 744 negative-days), which wrecks precision. NOT an edge. Nothing tuned; nothing wired into frozen S0 config.
- **Root cause:** SPXW tape shows customers net-BUYING options almost every day -> inferred dealer sign is short/negative far too often (flips 367 static-Positive days to Negative, mislabels 107 vendor-Positive days as Negative).
- Files: `ddoi_gamma.py`, `ddoi_run.py`, `tests/test_ddoi_gamma.py` (8 + causality = 10 pass). Report `output/ddoi_gamma_20260701.md`; per-day cache OFF-Drive `C:\TradingDesk-Local\warehouse\derived\ddoi_spxw_daily.parquet`. Commits `1af1774` / `f8f7dde` / `dc2a141` / `5eee5e0`. VERIFIED on disk by conductor.
- **CAVEAT / open (curve-fit-preventing read = treat as refuted):** vendor labels the SPX-ROOT regime while our only tape is SPXW — the failure MAY be a cross-symbol artifact (SPXW retail-heavy 0DTE net-buying flow not representing SPX-root dealer positioning) rather than a refutation of inferred-direction per se. Distinguishing would need an SPX-root intraday tape we do not warehouse. Per the `gamma-symbol-choice` note (daily signal fixed on SPX-root), treat DDOI-on-SPXW as REFUTED; don't chase.

### 2026-07-01 — S6 (Brandon W intraday 0DTE credit-spread) research spike + exit×gamma×VIX matrix
- **S6 = Brandon Wendell "SPX Cash Flow" 0DTE credit spreads** (bull put / bear call / iron condor, 5-wide, ~14:00 ET entry) processed through OUR brain (regime/gamma/vol) and tested honestly — NOT adopted as written.
- **Data sufficient, no new purchase:** 1-min SPXW NBBO warehouse (2022-04-20→2026-06-26) covers the whole 0DTE universe; intraday spot recovered via put-call parity + per-strike delta via Black-Scholes, validated vs EOD (spot median err 2.3pt; delta 0.006). Sampling unit = the DAY (~1050, stress-rich); OOS by DAY-TYPE.
- **Honest control (fixed 0.15 delta, real bid/ask fills) LOSES all 3 structures** (win 61-65%, loss/win ~2.7x — the 2x stop + slippage overwhelm the win rate).
- **Exit×gamma×VIX matrix: ALL 36 cells lose money** — no plateau, no peak. Loosening the stop makes it worse; hold-to-settle is catastrophic (loss/win 7-20x). Prior-EOD dealer-gamma sign + VIX term-structure day-gating rescues no regime bucket.
- **Verdict: documented chassis + exit-tuning + our regime/gamma/VIX day-gating (at fixed 0.15-delta) is a DEAD END** — found cheaply, honestly, no curve-fitting.
- **Only open question = STRIKE SELECTION / placement** (delta was held fixed to control DoF; Brandon's supply/demand zones never tested). That crosses the frozen-config/strategy line — **awaits Andrew's explicit greenlight**; anti-curve-fit bar = robust plateau across day-types + both time-halves.
- Possible role even if standalone-marginal: S6's premium-selling as the calm-day financing leg of S5. Code: `backtester/s6_*.py` + `tests/test_s6_*.py` (128 tests pass, causality guard green). Commits 465a74e (control), 9cf8c0a (matrix). Memory note `s6-spx-cashflow-0dte`.
- **UPDATE (later 2026-07-01) — two MORE experiments done, both NO edge:** (1) **Zone strike-selection** (`s6_zones` / `s6_strike_experiment`) does NOT beat blind 0.15-delta — zones collect more premium ($0.48 vs $0.36 bull put) but breach proportionally more; reward/risk (credit/breach) stays flat ~2.0 for every arm; the delta-matched "fooling test" arm came out byte-identical (a strike = its delta), so the zone's only lever is which delta it picks, and that doesn't beat fixed 0.15. Efficient-market signature (0.50+ credit trades breach 40%+ vs 12% for 0.30-0.50). (2) **VRP-timing day-selection** (commit c53acc5, 149 tests green) = DEAD END and WRONG-SIGNED — richest-VRP days are the WORST to sell (highest breach/loss) in both time-halves; high VRP just tags high-IV catalyst days (FOMC etc.) that run the fixed-delta spread over; inverting doesn't help.
- **VERDICT: S6 STANDALONE tested-and-REJECTED across THREE angles** (exit×gamma×VIX matrix, zone strike-selection, VRP timing) — consistent root cause = negative skew (loss/win ~2.7-2.8x) the ~62% win rate can't overcome. Strike-selection (the prior "only open question") is now CLOSED. **Open question = S6-as-S5-financing-leg (where the negative skew becomes a feature) or PARK.**
- **S6 PARKED 2026-07-01** — refuted four ways (chassis matrix, zone strike-selection, VRP timing, S5-financing-leg gate); root cause = negative skew (~2.7x loss/win) a ~62% win rate can't overcome after honest fills; lessons captured in the strategy-evaluation-playbook memory; S5 tail still validated, financing source is a future S5 question.

### 2026-07-01 — reliability hardening (reboot-survivability)
- **SPXW 1-min collector reboot-death ROOT-CAUSED + FIXED.** It sat dead ~15.5h after a Windows reboot (Jun 30 14:40) — its task had only a single daily 06:00 trigger, so nothing relaunched it. Fix: StartWhenAvailable + battery guards off on 6 tasks; BootTrigger + 30-min Repetition on the continuous collector.
- **All 8 desk scheduled tasks now REBOOT-SURVIVABLE.** Daily one-shots get StartWhenAvailable only (no BootTrigger — would misfire each reboot); the two continuous jobs get BootTrigger + Repetition.
- **NEW task HeartbeatStalenessAlarm** — runs every 15 min, boot-hardened, run-whether-logged-on; emails an alert if a monitored collector's heartbeat goes cold >15 min while the job isn't complete. Closes the "unnoticed death" failure mode.
- **Liveness rubric added to memory** (`liveness-rubric`). GOTCHA logged: verify task triggers from `Export-ScheduledTask` XML, NOT CIM trigger objects (false negatives). Modifying Password-principal tasks needs elevated shell + password re-supplied via `Register-ScheduledTask -Xml`.
- **Collector healthy** — ~92% done, finishing this afternoon.

### 2026-06-30 SESSION CLOSE — shipped
- **Cashflow layer built end-to-end** (paperbot v0.6.0→v0.12.0, Slices 1-3/5/6a/6b): execution-side CASH bucket (backtester untouched), propose-only monitor brain (`account_monitor.py`), deposit detection, withdrawal earmark fence + sale-raised nudge, live read-only shell (`account_monitor_run.py`).
- **Gateway lock interlock** (`gateway_lock.py`, single-process mutex) wiring monitor↔rebalance; **account monitor SCHEDULED** daily 16:30 CT (AccountMonitorDaily, clientId 40, read-only/propose-only, lock-guarded).
- **All 8 desk scheduled tasks HARDENED** to run-whether-logged-on (LogonType=Password) — survives sign-out/overnight reboot. CAVEAT: re-run `harden_scheduled_tasks.ps1` after any Windows password change.
- **Reference paper** AsymmetricReturns (AllianceBernstein) added — best external statement of S5's convexity/tail thesis.
- **Regime breadth-thrust re-entry lead logged** (`docs/REGIME_RESEARCH_BACKLOG.md`; HIGH curve-fit risk).
- **Scheduler plan** (`docs/SCHEDULER_PLAN.md`, 8-job inventory) + **LIVE_RESILIENCE stub** (`docs/LIVE_RESILIENCE.md`, server-resting-protection gap) written.
- **S5 1-min reader** (`backtester/s5_intraday_data.py`) + ledger experiment (endogenous self-funding wins the twitchy-bleed regime; nothing adopted).

## OPEN ITEMS — running tally (updated 2026-07-02 — session wrap)
**DONE this session (2026-07-02):** SPX-ROOT 1-min overlap-window pull (285 trading days, 2025-05..2026-06) via a NEW parallel sharded harness; DDOI re-run on SPX-root -> STILL refuted (cross-symbol caveat CLOSED, commit 0af2b8c/c6ac49b). Earlier (2026-07-01): SPXW 1-min backfill COMPLETE (1126 days); S5 harvest engine, S2/S3 intraday condor, and DDOI all built + REFUTED.
**Decisions awaiting Andrew:** (1) **S5 financing fork** — the tail/defense half is validated; naive calm-day premium-selling as its financing is REFUTED. Choose: test a different financing structure (further-dated theta / broken-wing-ratio / delta-hedged short-vol — all UNTESTED) OR accept S5 defense-only with the ~1.56%/yr carry as a paid drag. (2) **S2/S3 lead** — bless (or shelve) a NEW pre-registered test of morning-rvol->PM-range (rho +0.59, stable OOS); it must beat the losing control AND the random-sit-out placebo. (3) **SPX-root FULL history (2022+)** grab before the ThetaData sub lapses month-end — recommended SKIP (DDOI refuted; ~20h parallel / ~16GB) unless other SPX-root intraday work is foreseen.
**Open research leads (NOTHING adopted; HIGH curve-fit risk — gate hard):** regime `sharp_recovery` clean-V-only refinement (PRIME lead) · S4 SPX vol-control fund (buildable, not built) · intraday-gamma early-exit revisit (was ~June 30 — now DUE).
**Open build/infra (no strategy risk):** dashboard roadmap (Phase 2 backtester controls, Phase 3 gated trading controls, gamma grid, GEX zero-line chart) · add `scipy`+`matplotlib` to the venv (S2/S3 hit the gap; unblocks the PV-band PNG) · confirm/close the ThetaData terminal port-25503 watchdog (task `ThetaTerminalWatchdog` now shows Running — likely already built; verify + close).
**Time-gated / housekeeping:** ThetaData sub HELD till month-end (Andrew's call) -> cancel decision then · SPX-root parallel-pull temp monitoring (`Spx1mParallel` task + session watch-loop) TORN DOWN on completion · reusable ops finding saved to memory: one ThetaData terminal caps at ~3x/4-shard concurrency.
**Owed:** _(none open)_ — [8] 2008 GFC +8.3% **CLOSED 2026-06-29 (closed on Andrew's nod).** See Recently closed.
**TESTED → REJECTED this session:** vol-control borrows as an S0 OVERLAY (subordinate to the regime band) · re-entry MAX_LAG 6→3 (failed the per-episode safety gate — a risk-budget trade-off, not a free win; HELD) · drawdown-depth exit gate + gamma/vol/term-structure exit overlays (the exit-whipsaw is NOT signal-separable).
**FIXED 2026-06-28:** `features/gex.py` spot<=0 ZeroDivisionError guarded (`a953389`) · `FRED_API_KEY` added + verified (`610507c`; ICE HY OAS limited to ~2023-06+ by rolling-3yr restriction — can't warm 2008) · dashboard restyle (`6b3609a` / `acb0bb6` / `99865da`).
**Recently closed:** **[8] 2008 GFC +8.3% — CLOSED 2026-06-29 (Andrew's nod).** Audit fully flushed: 3 independent passes (data-integrity + method re-derivation from raw NAV + margin sweep showing a PLATEAU not a peak) + look-ahead tests, all PASS, PLUS active-2008-navigation CONFIRMED via warm-up test (separate `bt_data_ext2005`; engine at CapitalPreservation by Jan-2008, led by trend/breadth/vol). Documented caveat (a limitation, NOT an open task): the CREDIT half of the GFC-entry read is unwarmable (HYG inception ~2007-04), so entry was driven by trend/breadth/vol not credit (`VALIDATION.md` §5 softened accordingly). Evidence: `backtester/output/gfc_decomposition_2026-06-28.md`. desktop launcher [13]; dashboard Phase-1 monitor [12]; collector smart-filter committed; re-pull day 20260529 (self-heals); S3 v1 condor control [4]; flow de-risk gate [3] (tested→rejected); cosmetics [9]; killed redundant cron [10]; gamma overlay + weekly cadence (both tested→rejected); GEX rebuild+calibration (70%); daily grab→ThetaData (IBKR retired); EOD Dealer-Gamma section; Friday 6/26 data fix; full top-down audit.

## A — Strategy & Backtester
- 200d-MA fragility fix ADOPTED: `REGIME_TREND_MARGIN=0.03` (regime-only early-exit margin).
- **REGIME ENGINE STRUCTURAL EXPLORATION 2026-06-28 — NOTHING ADOPTED, config UNTOUCHED (all in-process tests).**
  Conclusion is a NEGATIVE (curve-fit-PREVENTING) result: the engine is ROBUST and no structural tweak cleanly improves it without a cost.
  * **Bleed characterized:** it is RE-ENTRY LAG + SHALLOW-DIP WHIPSAWS (2025 cut to 0% on ~−8.6%, 2026 cut 85%→24% on ~−5–8%, both round-tripped). The deep-crisis EXITS are GOOD — leave them alone.
  * Existing knobs = a robust plateau; can't fix the whipsaw.
  * **Re-entry `MAX_LAG` 6→3: TESTED → HELD (NOT adopted).** Looked free in Stage A but FAILED the final per-episode safety gate — 3 episodes worsen >100bp (2008 tail −118bp, 2011 −114bp, 2015-16 −152bp). It's a risk-budget TRADE-OFF: full-window maxDD byte-identical (−10.20%, no new tail risk), WINS episode-NAV 2009 (+2.76%) & 2011 (+0.95%); only genuine loser is the SIDEWAYS 2015-16 grind. Benefit smaller than first quoted (2022 lag ~14→12mo). **`REENTRY_MAX_LAG_MONTHS` stays 6.**
  * **Exit gates can't fix it:** a drawdown-DEPTH gate FAILS (depth can't separate whipsaws from real-crash first legs — both −7% to −9%); gamma/vol/term-structure overlays ALSO can't separate them. The exit-whipsaw is NOT signal-separable by any overlay.
  * **PRIME OPEN LEAD (next session):** tighten the override's `sharp_recovery` trigger to fire only on CLEAN V-recoveries (the failure is the override firing in SIDEWAYS grinds), then re-run the per-episode gate. HIGH curve-fit risk; needs a principled trigger + OOS re-test. Until/unless it clears, re-entry stays `MAX_LAG=6`.
- **Vol-control borrows as an S0 OVERLAY 2026-06-28: TESTED → REJECTED** (subordinate to the regime band — the band already collapses exposure in crises). VIX3M/VIX9D/VVIX pulled to `bt_data`. `FRED_API_KEY` added + verified (committed `610507c`); ICE HY OAS limited to ~2023-06+ (rolling-3yr restriction) — can't warm 2008; HYG/IEF proxy retained for history.
- **NEW STRATEGY LEAD — S4 "SPX vol-control fund" (OPEN, not built):** the SAME vol-control mechanics (`exposure = min(leverage_cap, target_vol/realized_vol)`, daily) as a STANDALONE single-asset SPX fund (FIA-style vol-control index replica), NOT an S0 overlay — which sidesteps the "subordinate to the regime band" rejection. `target_vol` and `leverage_cap` both swept. Buildable today. (Memory note `s4-spx-vol-control-fund`.)
- **GFC active-2008-navigation CONFIRMED real 2026-06-28:** a definitive warm-up test (separate dir `bt_data_ext2005`; canonical `bt_data` untouched) shows trend/breadth/vol warmed by 2006-03 and the engine at CapitalPreservation by late-2007/Jan-2008, BEFORE the worst leg — the de-risk is active navigation, not a warm-up artifact. CAVEAT: the CREDIT half of the GFC-entry read is unwarmable (HYG inception ~2007-04), so the entry was driven by trend/breadth/vol, not credit. `VALIDATION.md` §5 softened accordingly.
- **Gamma symbol verdict HELD + characterized 2026-06-28:** daily S0 signal stays on SPX root (matches Tier1Alpha vendor labels 69.8% vs SPXW 60.1% / combined 65.5%-null). The residual gap is a genuine method diff on the NEGATIVE-gamma side (our static dealer-sign vs vendors' inferred DDOI) — closing it needs a DDOI-style trade-direction build off the 1-min data (future). Gamma is HORIZON-DEPENDENT (daily SPX for S0; intraday SPXW/0DTE for S2/S3).
- DONE 2026-06-27: `backtester/data/` MOVED off Google Drive to `C:\TradingDesk-Local\bt_data\`
  (Drive sync was corrupting the data). config repointed; loader/downloader are absolute-path-aware;
  data/ kept w/ .gitkeep. 89 tests pass. Drive-sync instability RESOLVED.
- DONE 2026-06-27 (committed 78cdabe): GFC/2008 tables REGENERATED on extended 2007+ Tiingo data.
  `config.DATA_START` now `2007-01-01`. Balanced 2007→2026: **CAGR 8.5% / maxDD -10.2% / Calmar 0.83 /
  Sortino 1.16**; GFC-window maxDD -7.1%; **calendar-2008 +8.3%** (up from old +3.4% pre-margin) — 2022 -6.1%.
  2015-26 headline UNCHANGED (CAGR 7.45% / maxDD -10.20% / Calmar 0.73). Good 2010 data backed up at
  `C:\TradingDesk-Local\bt_data_backup_2010_good`.
  - **AUDITED CLEAN 2026-06-28 (3 independent passes):** (1) data-integrity — no corruption/stale data,
    2008 prices real-world correct, +8.28% explained by sane holdings (0% equity / ~24% Treasuries / ~5% gold
    / rest cash); (2) method re-derivation from raw NAV — reproduces exactly; identical −7.13% GFC maxDD is
    mechanically forced (byte-identical fully-de-risked book through the deep-stress leg; margin changes only
    the recovery, not the depth); (3) margin sweep — PLATEAU not peak (cal-2008 flat at +8.28% for ALL margins
    ≥0.01, so 0.03 is NOT 2008-tuned; Calmar spread 0.034 across 0.03–0.05), 95/95 tests pass incl. both
    no-look-ahead tests, T+1 lag verified real, start-date perturbation leaves cal-2008 unchanged. Decomposition:
    ~44% of the +3.4%→+8.3% jump is the data refetch, ~56% is the general early-de-risk property (which COSTS
    full-window CAGR while shaving drawdown) — neither is curve-fit. Evidence: `backtester/output/gfc_decomposition_2026-06-28.md`.
- DONE 2026-06-27: paperbot byte-parity RE-PROVEN — paperbot targets are byte-identical to the backtester
  with `REGIME_TREND_MARGIN=0.03` (max abs diff 0.0). Paper-use prerequisite cleared.

## B — Data Warehouse / Collector
- **OUTAGE + RECOVERY 2026-06-28 ~14:47:** ThetaData terminal + collector + dashboard all died. Recovered —
  terminal relaunched via `datacollector/start_terminal.py`; collector RESUMED from day ~342 (not from scratch,
  thanks to the progress heartbeat + on-disk dedupe). **ROOT CAUSE:** no watchdog auto-restarts the ThetaData
  TERMINAL (the collector's supervisor restarts the *collector*, but a dead terminal on port 25503 just stalls it).
  **OPEN:** build a port-25503 watchdog scheduled task (recommended, not built).
- Collector snapshot 2026-06-30 09:41: **~82% (961/1170 days)**, ETA tonight ~23:24 CT — finishing unblocks S5 harvest + S2/S3 + DDOI.
- RESOLVED 2026-06-27: the DuckDB `options_eod` "non-empty parquets only" fix is ALREADY in committed
  `storage.py` (`_nonempty_parquets()` + `rebuild_catalog()`). Verified against the LIVE ~102k-file
  warehouse — zero-column markers correctly EXCLUDED (kept on disk), 0 corrupt, view builds clean.
- KILLED 2026-06-27: a rogue duplicate `download.py` (running on system-Python, 6–12 GB, a
  warehouse-race hazard). Gone.
- FIXED 2026-06-27 (committed 6187fda): index-root crash in `ibkr_forward.py` (`_to_df` fillna on NaN
  spot for SPX/SPXW/VIX/NDX/RUT/XSP). Last night's forward run failed 30/43 symbols on this bug; future
  runs are now clean.
- ThetaData historical grab COMPLETE (`GRAB_END=20260625`); supervisor self-heals via Task Scheduler.
- VERIFIED COMPLETE 2026-06-27: the **2026-06-26 EOD** ThetaData backfill is done — all 30 previously-failed
  symbols now have Friday's data (**50/50 symbols present**, none empty/corrupt). Covers the symbols last
  night's forward run dropped.
- Do NOT delete empty/zero-column parquets (`have_day` relies on them).

## C — Paperbot Execution
> All PAPER. Nothing transmitted. review→arm→transmit gate intact. Serialize any order / gateway / git.
- **NEW 2026-06-30 (cashflow + gateway-lock layer, v0.12.0):** `investable.py` (consolidated buffer math, 1.5%);
  `account_monitor.py` (propose-only Verdict/decide brain) + `account_monitor_run.py` (live read-only shell,
  clientId **40**, scheduled daily 16:30 CT, gateway-lock-guarded); `gateway_lock.py` (single-process mutex
  interlocking monitor↔rebalance). Execution-side CASH bucket; deposit detection; withdrawal earmark fence + nudge.
- Multi-account rebalance ENGINE built + committed: `paperbot/rebalance_engine.py` (per-acct integer
  target shares, reserve carve-out, **account-level all-or-nothing band**, block aggregation w/ per-account
  `ContractsOrShares` split; emits empty FA method per the Err-10226 fix; never whatIfOrders a group).
  Engine triggers off required **trade size vs NetLiq**, not raw weight-vs-model drift. Tests pass.
- `recon_report.plan_account` ALIGNED to the engine's account-level trade-size band (readout matches the actor).
- Runner built + committed (22dee54): `paperbot/rebalance_run.py` — multi-account dry-run runner,
  review→arm→transmit gate, reads live FA groups via requestFA + fails closed on name mismatch, transmits
  nothing. clientId **37**.
- **Transmit EXECUTOR BUILT + committed (9220716): `paperbot/rebalance_execute.py`, clientId 38** — the
  transmit-CAPABLE Monday sibling of the runner. Default run = read-only DRY review (transmits nothing,
  writes no FA config). Armed transmit requires the **4-condition gate**: `READONLY=False` AND `DRY_RUN=False`
  AND `armed=True` AND the exact CLI token `--arm-i-understand` (which flips READONLY/DRY_RUN in-process; no
  auto-arm). Armed flow in code: discover → build_plan → resolve_tier_groups (fail-closed) → risk_manager →
  BACK UP FA config → set each group's ContractsOrShares via `replaceFA` → place blocks ONE at a time (never
  whatIfOrders a group) → reconcile. `order_router` now **rejects NaN/<=0 limit prices** (hard price guard).
  **33 tests pass.** Monday CLI: dry review `python rebalance_execute.py`; armed `python rebalance_execute.py
  --arm-i-understand`.
- **MONDAY FLAG:** the executor's `set_group_contracts_or_shares` XML tag casing must be **eyeballed against
  a live `requestFA(1)` dump (run `fa_probe.py`) before the armed run** — couldn't be confirmed offline. New
  pre-arm step added to `MONDAY_RUNBOOK.md`.
- Account reality: 5 client subs DU8922142–146, each FUNDED ~$1.1M paper, all enrolled, all FLAT (cash) →
  each needs a full initial rebalance. FA groups exist: Conservative→DU142; Balanced→DU143,144;
  Growth→DU145,146 (method ContractsOrShares; prior config backed up to `state\paperbot\fa_groups_backup.xml`).
- **FIRST LIVE REBALANCE — HELD FOR MONDAY.** Two blockers, both expected: (1) the paper gateway's
  account-data feed is DOWN for the weekend (account reads HANG at connect-time sync), so the live read-only
  review can't run today; (2) market is closed. Build + dry-run review are done; **transmit is Monday**.
  - MONDAY steps live in `MONDAY_RUNBOOK.md` (repo root) — being written by a worker this session.
- Account model: trade the **DU sub-accounts**; FA master DF8922141 rejects direct orders + hangs reads.
  Paper gateway hard read-only lock is OFF (software arming is the control).

## D — Reporting
- RRG retired; harness = the EOD status digest (`eod_report.py`), scheduled 21:00 CT.
- **HARDENED 2026-07-02** (commits `efb25c9`/`e735894`): the digest no longer opens a live IB socket (TCP port-check), always sends (fallback email + per-section timeouts), exits non-zero on send failure, and now has 7 sections incl. Dealer-Gamma + a self-watching Staleness-Alarm section. Out-of-band failure signal = Desktop `TRADINGDESK_EMAIL_DOWN.txt`. Independent per-job deadline alarms now cover forward/tiingo/gex/account_monitor (not just spxw_1m+eod). See the 2026-07-02 session section above.
- `daily_run.py` + `connections/ibkr.py` carry the gateway `java_version=17` launch fix.

## Shared plumbing
- Local git repo (in Drive, no remote); commit after each change-set.
- clientId registry in `connections/clientids.py`. In use this lane: paperbot=30, flatten=34, fa_block=35,
  fa_admin=36, rebalance_run=37, rebalance_exec=38, **account_monitor=40**. Don't collide.
- Handoffs consolidated into `conductor/handoffs/`; the stray "Andrew is pissed off" folder was deleted.
