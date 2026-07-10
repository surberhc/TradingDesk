# Session Handoff — 2026-07-10 — S8 / British IC deep-dive

## ⚠️ TOP-LINE WARNING — READ THIS FIRST

**The S8 mechanical simulator built this session (`british_ic/s8_mechanical_simulator.py`,
`s8_sim_calibration.py`, `s8_sim_calibration_compare.py`, `s8_sim_extended_run.py`) FAILED
calibration and must NOT be trusted or built upon without a fresh diagnosis.** It does not
reproduce the real account's known P&L on the one window we have ground truth for. Do not run
the 2022-2026 extended backtest, do not cite its output, and do not assume the fix already
applied ("empirical per-slot firing probability") is the right model — see below, it's likely
wrong in a fairly fundamental way. Conductor item **#18** tracks this; read it before touching
this code again.

---

## What's solid and can be trusted

All of the following are committed, reviewed, and stand on their own:

1. **S8 designated as a standalone strategy spec.** `docs/S8_SPEC.md` created (commit `709d7cd`),
   corrected to clarify the live TAT account remains an ongoing out-of-sample data source, not
   retired (commit `ba734a2`).
2. **Long-leg exit slippage isolated** from the blended short+long measurement: real, small
   (median ~2x half-spread, ~$0.05–0.16/contract) — doesn't materially threaten S8's backtested
   edge. `british_ic/LONGLEG_SLIPPAGE_ISOLATION.md`, commit `563f4cd`.
3. **Template-evolution story refuted.** Andrew's recollection that the account "evolved" from
   $2→$3→$4 templates and 50-wide→80-wide was tested against real `combo_ledger.csv` data and
   refuted — the template mix still cycles as of the most recent data (July 2026), no convergence
   to any single template. (Reported directly, no standalone file.)
4. **Fixed-template (80-$4) backtest + 6-config grid search.**
   `british_ic/TEMPLATE_FIXED_AND_GRID_ANALYSIS.md`, commit `b966972`. Only 3 of 6 templates
   (80-$3, 80-$4, 50-$2) had enough sample to test; Sharpes cluster tightly, no statistically
   distinguished winner, 80-$4 favored mainly on data volume.
5. **Alpha-vs-beta decomposition: S8 survives.** The +108.8% headline holds up against a linear
   beta test against SPY (beta −0.63, R²=0.016, annualized alpha +130.6%, 95% CI excludes zero) —
   genuinely different from the refuted CSP/condor/short-strangle family, which all collapsed to
   equity beta under the identical test. `british_ic/ALPHA_VS_BETA_DECOMPOSITION.md` (commit
   `6a489c6`), folded into `docs/S8_SPEC.md` §8 (commit `c71f7ee`). Real caveats: thin sample
   (236 days vs CSP's ~2,000+), out-of-sample second half not independently significant (t-stat
   1.10 vs 2.35), and the short-vol/tail-risk channel — arguably the more relevant risk for a
   0DTE book — is still inconclusive.
6. **80-$4-only full-strategy backtest** (the whole strategy on this one template, not just the
   long-leg edge): underperforms the full blended S8 headline by about half, and B2 is net-negative
   on this template in aggregate. `british_ic/S8_80_4_ONLY_FULL_BACKTEST.md`, commit `3e6fb14`.
7. **80-$4's B2 shortfall isolated as a single-day artifact.** Excluding the 2025-10-10 crash day,
   80-$4 has the LARGEST positive B2 edge of any template tested. Same full-strategy backtest also
   run on 80-$3 and 50-$2 for comparison. `british_ic/S8_SINGLE_TEMPLATE_COMPARISON.md`, commit
   `5a65938`. Verdict: no single template clearly best; 80-$4 is the most defensible pick (most
   data, largest ex-crash edge) but not statistically proven superior to 80-$3/50-$2.

## What FAILED — do not trust or build on

**The 2022–2026 mechanical simulator extension.** Goal was to test S8's rules against ~4.5 years
of real SPXW 1-min data (confirmed on disk) instead of just the known 1-year real-fills window, for
more regime coverage. This required building a from-scratch simulator that independently
re-derives trades from raw market data using the entry-timing/strike-selection/stop/B2 rules —
*not* reconstructing from real account fills. Commits: `d3374ba` (simulator skeleton), `77693a5`
(empirical entry-schedule rebuild), `044d93e` (Stage-B template comment polish, no functional
change), `ed6ff50` (29x performance fix, no logic change), `501dc24` (calibration-compare +
extended-run scripts).

Calibration against the KNOWN 2025–2026 real window — the only way to sanity-check the simulator
before trusting it on 2022–2024, where there's no ground truth — came back badly wrong:

- **First attempt:** −$1,440,129 total sim P&L vs. the real account's +$138,982. Traced to entries
  firing 7x too often per day (76.5/day vs. real 11.0/day) — a schedule-derivation step treated
  "this time-slot appeared on X% of historical days" as "fires every day" instead of "fires with
  probability X%."
- **Fixed** (converted to a real per-slot empirical firing probability, deterministically seeded
  per date/template/slot) and **re-run on the full 247-day window:** trade frequency improved a lot
  (15.1/day, much closer to real 11.0/day) but **total P&L was STILL negative** (−$233,203 vs. the
  real +$138,982), and the stop rate barely moved (39.8% vs. the original 40.4%). The frequency fix
  was real and necessary but **not sufficient** — a deeper, still-undiagnosed mismatch remains.

**The reframe that matters most:** late in the session Andrew corrected a wrong assumption that had
been driving the entire diagnosis. The TAT system is a **fully automated algorithmic program**, not
a human making daily discretionary calls. That means the real explanation for why any given
scheduled slot only fires on 73–85% of its "eligible" days is a **discoverable mechanical rule**
(an IV filter, a capital/position constraint, something in the data) — not human judgment, and
*not* something to model as a random probability draw the way the fix did. This means the
probability-weighted-draw fix was itself likely the wrong model, not just an incomplete one. The
next session on this thread should start from that corrected understanding, not from patching the
existing probability-draw approach further.

Session ended here at Andrew's request to stop and wrap up, without resolving this. See conductor
item **#18**.

## Two open questions, unresolved

1. **Does TAT's automated system also control the long leg's exit, or is that a separate process?**
   If the same automated system manages long-leg exits, "B2" isn't a proposed correction we
   invented — it's us discovering an already-existing real rule that we haven't yet correctly
   identified from the data. If the long-leg exit is genuinely separate/manual, the current B2
   characterization in `docs/S8_SPEC.md` stands as-is. Raised by Andrew, not resolved. Conductor
   item **#19**.
2. **Which single template (if any) should a bot run?** 80-$4 is the most defensible pick (most
   sample data, largest ex-crash B2 edge) but not proven statistically superior to 80-$3 or 50-$2 —
   Sharpes cluster tightly across the three templates with usable sample. See
   `british_ic/TEMPLATE_FIXED_AND_GRID_ANALYSIS.md` (commit `b966972`) and
   `british_ic/S8_SINGLE_TEMPLATE_COMPARISON.md` (commit `5a65938`). Conductor item **#20**.

## Process friction this session (separate from the S8 substance)

Significant time was burned on process-management mistakes — accidentally running duplicate copies
of the same long-running script concurrently, multiple times (see commit `501dc24`'s note: this
session's Bash background invocations were silently duplicated by the harness into two processes
across two separate Python installs, causing CPU/IO contention and making log-based progress
tracking unreliable) — and on background-agent coordination confusion (nested sub-agents did not
trust redirect/stop instructions relayed through the messaging system, treating them as unverified
"impersonation," causing real stalls and wasted compute before being resolved). Andrew was
explicitly frustrated by this. Worth being deliberate about background-process hygiene and
sub-agent trust/redirect handling in future long-running sessions.

## Tracking

- Closed conductor items **#14** (long-leg slippage isolation — done) and **#4** (review
  `british_ic/longleg_slippage_isolation.py` + `investech/` — the british_ic half is resolved/
  committed; `investech/` re-opened separately, see below).
- Opened **#17** (review `investech/`, unclassified, carried forward), **#18** (simulator
  calibration failure, do-not-trust warning), **#19** (automated long-leg-exit question), **#20**
  (which template, still undecided).
- Full narrative logged via `conductor/cli.py log` (entry dated 2026-07-10). See
  `conductor/STATUS.md` for current open-item list.
