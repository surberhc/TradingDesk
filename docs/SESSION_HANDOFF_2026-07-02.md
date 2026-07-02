# Session handoff — 2026-07-02 (close)

Research / PAPER only. Nothing live traded. Frozen S0/regime config untouched throughout.

## Orientation
The 1-min SPXW backfill completing (2026-07-01) unblocked three data-gated studies. All three — the S5 harvest engine, the S2/S3 intraday condor, and DDOI inferred-dealer-gamma — were built and honestly **REFUTED** (no curve-fit, frozen config never touched). Then, to give DDOI a fair test on the RIGHT symbol (the vendor labels SPX-root; our only prior intraday tape was SPXW), we pulled **SPX-ROOT 1-min data via a new parallel sharded harness** and re-ran DDOI — **still refuted**, which closes the cross-symbol caveat. Net: three refutations stand, one data caveat resolved, one reusable parallel-pull harness + throughput finding banked.

## Results (all honest fills, frozen config, nothing adopted)
| Study | Verdict | Key numbers |
|---|---|---|
| **S5 harvest** (naive calm-day 0DTE premium-selling as the tail's financing leg) | REFUTED — does NOT self-fund the tail; it drains cash | mean **-$13.98/sell-day** (calm VIX<=15 WORSE at -$16.10), loss/win **4.27x**, win rate 77.6% (cosmetic), **0/5 calendar years net-positive**; ~= -0.57%/yr of core vs the +1.56%/yr carry it must cover |
| **S2/S3 intraday condor** (fixed 0.15-delta 0DTE, hold-to-settle w/ 2x stop) | Control loses; gap-gate REFUTED by placebo | control **-$32,870**; gap-gate worse-than-random (random sit-out beats it in 82% of 3000 draws); one surviving lead: morning-rvol->PM-range Spearman **+0.59**, stable OOS |
| **DDOI** (Lee-Ready inferred dealer sign -> net-GEX; only the sign moves) | REFUTED on BOTH symbols — genuine method failure, not a symbol artifact | SPXW **36.7%** vs static 60.1%; SPX-root **44.8%** vs static 69.8% (-24.9pp), worse in both halves; over-calls Negative because customers net-buy options nearly every day |

## Infra built this session (no strategy risk)
- **Parallel sharded SPX collector:** `datacollector/spx_1m_parallel.py` (supervisor — shards the date range into N ranges, burst-and-release, auto-restarts dead shards) + `datacollector/collect_spx_1m.py` (SPX sibling of the SPXW collector) + `datacollector/_probe_concurrency.py`.
- **Measured ThetaData terminal ceiling:** one local terminal scales cleanly to **~4 shards / ~2.85x sustained** vs serial, **zero rate-limits**, no gain past 4 (latency-bound). Cut the ~11h serial pull to ~3h. The supervisor's auto-restart was proven live on 2 real shard deaths.
- **Temp monitoring stood up and torn down:** a `Spx1mParallel` scheduled task + staleness alarm + session watch-loop, all removed on completion. The now-cold `spx_1m_parallel` entry was removed from `heartbeat_alarm.py`'s JOBS list so it can't fire a false "spx stale" email (SPXW entry + everything else untouched; the HeartbeatStalenessAlarm task itself unchanged).
- **Known-empty holiday list:** the 11 "missing" weekdays in the pull window are confirmed US market holidays with no data, recorded in off-Drive `C:\TradingDesk-Local\warehouse\spx_1m_known_empty.json` so any future SPX resume exits cleanly at 100%.

## Commits
- `e75d5f4` — SPX-root pull complete + harness findings.
- `0af2b8c` — DDOI SPX-root code (generalized `ddoi_gamma.py`/`ddoi_run.py` with `--symbol SPX|SPXW`) + verdict.
- `c6ac49b` — STATUS DDOI cross-symbol resolution.
- `06e8506` / `c51b9c0` / `eb110c8` — parallel sharded harness.

## Pick up here (next session)
**Three decisions await Andrew:**
1. **S5 financing fork (TOP).** Tail/defense half validated; naive calm-day premium-selling as its financing is REFUTED. Choose: test a DIFFERENT financing structure (further-dated theta / broken-wing-ratio / delta-hedged short-vol — all UNTESTED) OR accept S5 defense-only with the ~1.56%/yr carry as a paid drag. Do NOT re-test naive calm-day selling.
2. **S2/S3 morning-rvol test.** Bless (or shelve) a NEW pre-registered test of morning-rvol->PM-range (rho +0.59, stable OOS); it must beat the losing control AND the random-sit-out placebo, or die at the placebo like the gap gate.
3. **SPX-root FULL history (2022+) grab** before the ThetaData sub lapses month-end — recommended **SKIP** (DDOI refuted; ~20h parallel / ~16GB) unless other SPX-root intraday work is foreseen.

**Standing research leads (nothing adopted; HIGH curve-fit risk — gate hard):** regime `sharp_recovery` clean-V-only refinement (PRIME lead) · S4 SPX vol-control fund (buildable, not built) · intraday-gamma early-exit revisit (was ~June 30 — now DUE).

**Build/infra (no strategy risk):** dashboard roadmap (Phase 2 backtester controls, Phase 3 gated trading controls, gamma grid, GEX zero-line chart) · add `scipy`+`matplotlib` to the venv (S2/S3 hit the gap; unblocks the PV-band PNG) · confirm/close the terminal port-25503 watchdog item.

**Time-gated:** ThetaData sub cancel decision at month-end (Andrew's call; held till then).

## Ops notes
- Tests: backtester **179 passing** incl. the causality (no-lookahead) guard.
- Nothing wired into the frozen S0 config; regime knobs untouched.
- All PAPER. Nothing armed, nothing transmitted.
