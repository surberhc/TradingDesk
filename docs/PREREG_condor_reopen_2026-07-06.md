# PRE-REGISTRATION — 0DTE Iron-Condor reopen: width / hedge / fills / higher-DTE

_Registered 2026-07-06, BEFORE any of these arms were run. PAPER / research only. Frozen strategy/regime config is NOT touched — these are research scripts on the warehouse._

## Why we reopened
The prior refutations (S6 exit-matrix, S2/S3 gap gate, S2/S3 morning-rvol, and the `condor_management_experiment` exit-dial map) share one honest finding, visible in `output/condor_management_20260703.md`: the 0DTE condor **harvests theta (positive at mid fill)** and **management improves risk** (profit-target-25% lifted win rate 67.6%->82.7%, cut worst day -$2,021 -> -$540) — but the **4-leg bid/ask spread on thin 0DTE premium** swamps the edge at realistic (>=50%) fills. The binding constraint is TRANSACTION COST, not strategy logic. Negative-gamma is the structurally worst regime bucket in every run.

Andrew's directive: stop treating the overlay as an on/off switch. Test the levers that attack the real constraint and cap the worst days — width, a neg-gamma hedge, real fills, and the higher-DTE regime where premium is thick vs cost. All four in scope.

## Shared anti-curve-fit spine (applies to EVERY arm — rule #1)
- **Pre-registered grids, NOT swept-to-winner.** The grids below are plain choices fixed now.
- **Plateau not peak.** A result is signal only as a robust positive plateau across >=3 adjacent grid cells at a realistic fill AND out-of-sample. A single winning cell is a mirage.
- **Matched placebo (decisive).** Any arm that beats its baseline must also beat a placebo matched to the same exposure/holding profile (random-day or random-exit as appropriate). Same bar that killed the re-entry ladder, gap gate, and morning-rvol arms.
- **OOS split at 2024-06-30** (train 2022-2024 / test 2024-2026) reported for every arm.
- **Per-regime (gamma x VIX) and per-year P&L** broken out. 0DTE window is 2022+ (small tail).
- **Honest 4-leg fills** via the control's own bid/ask close/open. P&L reported across the fill band mid / f25 / f50(HEADLINE) / full. No modeled slippage discount, no mid-only claim.
- **Judge in the real role (counterweight rule).** A hedge need not profit every year; it is judged on whether it caps the worst bucket net of its own cost without making calm days a wash.

## Arm 1 — STRIKE WIDTH sweep (attacks cost directly)
- Wing widths: **10 / 20 / 30 / 50-pt** vs the 5-pt control. Short strike frozen 0.15-delta.
- Fixed management = the prior run's best risk arm (profit-target 25% + 2x stop).
- Report per width: credit collected, **4-leg spread cost as % of credit**, net P&L across the fill band, OOS, per-regime, per-year.
- PASS: positive plateau across >=3 adjacent widths at f50 AND OOS AND beats matched placebo.

## Arm 2 — NEGATIVE-GAMMA HEDGE overlay (Andrew's core thesis)
- On prior-EOD **negative-gamma** days ONLY (existing DayClassifier forecast, causal), add one cheap defined long tail: buy a further-OTM wing at ~**0.05-delta**, honest ask fill, cost booked.
- Baseline = the managed base (pt25) with NO hedge. Compare base vs base+hedge.
- PASS: improves the **negative-gamma bucket dollars net of hedge cost** without bleeding calm days into an overall wash, AND beats a random-day-hedge placebo of equal hedge count.

## Arm 3 — FILL REALISM (recalibrates the yardstick for 1,2,4)
- Warehouse analysis (fast, read-only): distribution of the **4-leg net bid/ask as % of entry credit** at 14:00, by year and regime. Answers whether f50-worst-side is fair or pessimistic.
- Gold-standard follow-on (SEPARATE deliberate gateway action, flagged not bundled): a few 1-lot live PAPER condor fills to measure actual mid-vs-fill.

## Arm 4 — HIGHER-DTE pivot (where the "condors work" evidence actually comes from)
- **30 and 45-DTE** condor, managed textbook: close at **50% of credit OR 21-DTE**, honest costs.
- Uses the EOD warehouse chains (s3_condor_control path), NOT the 1-min feed.
- Same plateau / placebo / OOS / per-regime bar. (The naive 45-DTE benchmark that lost -$53k had no management and 89 trades — this is the proper test.)

## Decision rule (stated before results)
Adopt nothing on a single positive cell. A lever advances only if it clears the shared spine in the role it's meant to play. A clean refutation is a valid, publishable outcome — but so is a robust plateau, and we will not move the goalposts to avoid either.
