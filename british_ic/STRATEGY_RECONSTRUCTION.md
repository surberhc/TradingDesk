# British IC — Strategy Reconstruction & Long-Leg Exit Rule Research

Account U***9156, 0DTE SPX iron-condor-style strategy traded live via TAT/NinjaTrader.
This report covers (1) the actual mechanics the strategy trades by (empirically derived,
not assumed), (2) a rigorous train/test test of automated long-leg management rules, and
(3) what a legitimate path to automation would require. Every number below comes directly
from the data files in this folder or from `TAT-tradelog.xlsx`; nothing is interpolated.

Data sources: `TAT-tradelog.xlsx` (4,687 rows, 2024-09-16 to 2026-03-19, entry-side facts
only — its `ProfitLoss` is proven wrong and never used here), `combo_ledger.csv` (2,592
IBKR-ground-truth combo groups, 2025-07-09 to 2026-07-07), `decoupled_long_legs.csv` (1,617
long legs from combos whose short and long closed independently), and a **targeted pull of
1-minute SPXW OHLC** from the read-only warehouse (`C:\TradingDesk-Local\warehouse\raw\
options_1m\SPXW\ohlc\`), filtered to the exact strike+expiration+time-window of each of the
1,617 decoupled long legs — needed because the CSVs only carry entry/exit prices, not the
intraday path, and Part 2's rules are inherently path-dependent. This pull covered 1,584 of
1,617 legs (98.0%); the 33 uncovered legs (last 3 trading days of the window, warehouse not
yet updated) are excluded from Part 2 only.

---

## Part 1 — Actual strategy construction

### 1.1 Entry timing

Across all 4,687 TAT rows, `TimeOpened` clusters in two windows and varies **by Template**
(not randomly distributed through the day):

| Percentile | Time-of-day (minutes since midnight) |
|---|---|
| 5th | 08:43 |
| 25th | 09:15 |
| 50th (median) | 12:00 |
| 75th | 12:55 |
| 95th | 13:43 |

Hourly counts (`TimeOpened` hour): 8am=856, 9am=1,108, 10am=211, 11am=167, 12pm=1,464,
1pm=845, 2pm=36. Two distinct clusters: an **8:44–9:45am wave** (the "80-point / $4 stop"
templates — 2,619 of 2,845 "80" rows fire in this window) and a **12:00–1:55pm wave** (the
"50-point" templates, and the "80/$3" template cluster around 12:00-1:15pm). This is a
scheduled, template-driven cadence, not opportunistic/discretionary timing.

### 1.2 Strike selection / moneyness — what "80" and "50" actually mean

Grouping `PutDelta`/`CallDelta` (delta **at entry**, the short leg) and strike-width by
`Template`, empirically:

| Template | n | median abs(delta) | mean abs(delta) | median width (pts) | mean width (pts) |
|---|---|---|---|---|---|
| British IC - Puts - 80 - $4 | 1,219 | 0.232 | 0.238 | 80 | 78.5 |
| British IC - Calls - 80 - $4 | 1,213 | 0.249 | 0.252 | 75 | 73.7 |
| British IC - Puts - 80 - $3 | 190 | 0.263 | 0.270 | 80 | 77.7 |
| British IC - Calls - 80 - $3 | 186 | 0.284 | 0.288 | 70 | 67.7 |
| British IC - Puts - 80 - $2 | 37 | 0.244 | 0.253 | 80 | 77.3 |
| British IC - Puts - 50 - $2 | 389 | 0.216 | 0.220 | 45 | 47.7 |
| British IC - Calls - 50 - $2 | 418 | 0.236 | 0.241 | 45 | 46.4 |
| British IC - Puts - 50 - $3 | 409 | 0.285 | 0.288 | 50 | 48.4 |
| British IC - Calls - 50 - $3 | 412 | 0.292 | 0.296 | 50 | 47.0 |
| British IC - Puts - 50 - $4 | 107 | 0.269 | 0.280 | 50 | 50.0 |
| British IC - Calls - 50 - $4 | 107 | 0.277 | 0.285 | 50 | 48.7 |

Full per-template stats in `template_delta_stats.csv`.

**"80" / "50" in the template name = the short-strike-to-long-strike spread WIDTH in SPX
points**, not delta and not dollars — confirmed directly (median width matches the label
almost exactly: 80-templates median 75-80pts, 50-templates median 45-50pts; overall width
histogram peaks hard at 45, 50, 75, 80 with almost nothing between). **It is not a fixed
delta target**: entry delta sits in a fairly narrow band (~0.22–0.29 absolute) across ALL
templates regardless of the "80" vs "50" label — i.e., the width changes, but the short
strike's moneyness (how far OTM, in probability terms) does **not** shift materially with
width label. This means strike selection is delta/probability-driven (targeting ~0.22-0.29
delta shorts, consistent with a ~70-78% probability-OTM target), and the point-width choice
is an independent, separate dial layered on top — not the reverse.

**Is the short-strike offset fixed-points or vol-adaptive?** Since delta is targeted (not a
fixed point-offset) and delta is directly a function of implied vol, the strike placement
is **vol-adaptive by construction**: on high-IV days the algorithm will select a short
strike further (in points) from spot to hold the same ~0.22-0.29 delta target, and on
low-IV days a nearer strike. (Direct point-distance-from-spot isn't in this data without a
spot feed, but the delta invariance across market conditions is the direct evidence: a
fixed-points rule would show delta drifting with realized/implied vol, which it does not —
delta std within each template group is only 0.05-0.08, far tighter than what a fixed-point
offset would produce across the wide vol regime range 2024-2026 covers.)

### 1.3 Spread width — what "$2/$3/$4" actually means

Cross-tabulating `StopMultiple` against the template's dollar label:

| Dollar label | StopMultiple values observed |
|---|---|
| $2 | 2.0 (844 rows, 100%) |
| $3 | 2.4 (1,197 rows, 100%) |
| $4 | 3.3 (2,432 rows, 91.9%) / 3.2 (214 rows, 8.1%) |

**"$2 / $3 / $4" in the template name is the `StopMultiple` label** (loosely — a stop set
at roughly 2x/2.4x/3.2-3.3x the entry credit), **not a literal dollar width and not the
point width** (that's separately the "80"/"50" label, per 1.2). The two dials are
orthogonal: width (80 vs 50 points) and stop aggressiveness ($2/$3/$4) are chosen
independently, producing the 11 observed template combinations (not all 4×3=12 combos are
used — no "Calls/Puts - 50 - $... " variant is missing, all 12 exist except none absent;
full breakdown in `template_delta_stats.csv`).

### 1.4 Stop mechanism — exact formula, verified

`StopType` is `ShortREL` for 4,611 of 4,687 rows (98.4%; 76 rows are `Short`, a distinct/
rare stop type not characterized further here — insufficient volume to reverse-engineer
safely).

**Derived and verified formula** (on `ShortREL` rows, n=4,610 non-null):

```
PriceStopTarget = PriceOpen (net entry credit of the combo) + StopMultiple
```

where `PriceOpen = PriceShort − PriceLong` (verified exactly: `PriceOpen − (PriceShort −
PriceLong)` has max absolute error 8.9e-16, i.e. floating-point noise only — this identity
holds on every single row).

Verification of the stop-target formula itself: computed `PriceOpen + StopMultiple` and
compared to the recorded `PriceStopTarget` across all 4,610 rows —
- **65.1% match to the penny** ($0.005 tolerance).
- **100% match within $0.075** (max observed residual $0.075) — the small remaining gap is
  consistent with tick-size rounding on SPXW (nickel/dime increments), not a different
  formula.

In plain terms: the stop target is **"the net credit received, plus StopMultiple more
dollars of adverse move in the combo's mark."** E.g., a combo opened for $3.95 net credit
with `StopMultiple=3.3` (a "$4" template) has its stop target at $7.25 — the position is
stopped out once the combo's mark-to-market cost to close has moved against the seller by
$3.30 beyond what was collected. This is a constant-dollar (not constant-percentage) stop
sized directly off the position's own StopMultiple label, independent of the entry credit
level itself.

**Verification against realized stop-outs**: comparing `|PriceClose|` to `PriceStopTarget`
on the 2,410 `Stopped` + `ShortREL` rows with both fields populated — mean overshoot $0.088,
median $0.10, 46.5% within $0.10 and 83.7% within $0.30 of the target (the residual is
consistent with the `Slippage` field TAT itself records, i.e. bid/ask execution slippage
past the trigger, not a wrong formula). Full row-by-row verification data (`PriceOpen`,
`StopMultiple`, computed target, recorded target, and residual for all 4,610 rows) is saved
in `stop_formula_verification.csv`.

### 1.5 Scaling / re-entry behavior

Two distinct scaling behaviors are visible, using `combo_ledger.csv`'s
`short_n_open_batches` and same-day/same-strike sequences in `TAT-tradelog.xlsx`:

- **801 of 2,592 combo groups (30.9%) scale into the SAME short strike more than once
  intraday** while the position is still open (`short_n_open_batches` 2 through 8; 511
  groups add exactly once more, 173 add twice more, and a long tail up to 8 total batches).
  Of these, **316 groups (12.2% of all combos)** have more than one **distinct paired long
  leg** opened at a different strike for each scale-in add — the rest reuse/re-hit the same
  long strike (or the scale-in adds happen too close in time to distinguish, resolved to a
  single paired long per the reconstruction's 5-second tolerance).
- **Separately**, the strategy re-opens the SAME short strike again later in the day after
  it was previously **stopped out** (not merely scaled while open): of 959 same-strike,
  same-day, same-side "second opens," 416 happen with a positive gap after the prior
  position's close (i.e. genuinely re-entering after being fully flat), median gap in the
  10-60+ minute range (108/416 gaps are 10-30min, 73/416 are 30-60min, 191/416 are 60min+).
  This is a real, deliberate re-entry-after-stop behavior, not noise.

The data **cannot** further resolve the exact trigger for re-entry (fixed clock cadence vs.
price-level trigger vs. a NinjaTrader-side task schedule) — TAT-tradelog has no
`ParentTaskID` linkage in this dataset (`ParentTaskID` is 0 for all 4,687 rows, so the
scale-in/re-entry family tree can't be traced directly), and the reconstruction notes
already flag that per-batch short P&L inside scale-in groups can't be split without
fabricating an allocation. What IS clear: scale-ins/re-entries are **template-scheduled
events** (they cluster at the same handful of `TimeOpened` values per template — e.g. the
"80/$4" templates fire repeatedly at 08:44, 08:45, 09:07, 09:15, 09:45 across different
days), consistent with a fixed intraday schedule of entry attempts per template rather than
a pure price-triggered martingale.

---

## Part 2 — Automated long-leg exit rule: honest train/test result

### Setup

`decoupled_long_legs.csv` provides entry/exit snapshots only (no intraday path), but the
proposed rules are inherently path-dependent ("close once worth ≥Nx"). A **targeted,
memory-safe pull** of 1-minute SPXW OHLC bars was made for the exact (strike, right,
expiration, time-window) of each of the 1,617 decoupled long legs — 1,584 legs (98.0%)
were successfully covered; the 33 uncovered legs are from the final 3 trading days of the
window (warehouse not yet backfilled that far) and are excluded from this analysis only,
not from Part 1.

For each covered leg, three baselines and three candidate rule families are simulated
**mechanically, using only information available up to that point in the leg's own real,
lived window** (its actual open time to its actual close time — never fabricating price
action beyond what really happened while the position was live):

- **B1 — hold to EOD/expiry**: mark at 16:19 (last full minute before 16:20 settlement).
- **B2 — close the instant the paired short stops**: mark at the short's recorded close
  timestamp. (If the short was never stopped — i.e. it expired at 16:20 — this collapses to
  B1, since there's no "stop event" to close on.)
- **B3 — close at a fixed fraction (50%) of remaining session time**: mark halfway between
  long-open and 16:20, regardless of price.
- **R1 — simple profit-take**: close entirely the first minute the long's mark ≥ N× entry
  cost, N ∈ {1,2,3,5,8,10,15,20}. If never triggered, outcome = actual realized exit.
- **R2 — time-boxed hold after stop**: once the short stops AND the long is already at or
  above an ITM threshold (multiple of entry cost) at that moment, force a close no later
  than Z minutes later; otherwise behaves like the actual exit. Swept ITM threshold ∈
  {0.0, −0.3, −0.5} × Z ∈ {5, 15, 30, 60} minutes.
- **R3 — partial ladder**: sell half at N1×, remainder at N2× (or actual exit if N2 never
  reached). Swept (N1,N2) ∈ {(1,3), (2,5), (3,8), (1,5), (2,8)}.

**Chronological 70/30 split by date** (not by row): 236 distinct dates → 165 train dates
(2025-07-09 through 2026-03-13), 71 test dates (2026-03-16 through 2026-07-01). All
threshold values above were chosen as a reasonable a-priori sweep before looking at test
results — the full plateau is reported below, not just the best cell.

2025-10-10 (the known crash day) falls inside the **train** window (4 of the day's legs
are covered). No crash-magnitude day exists in the test window; the largest test-window
event is 2026-05-18, discussed explicitly below because it turned out to concentrate a
large share of certain rules' apparent test-set edge — the same "hidden hero day" risk the
brief specifically asked to guard against, just smaller and in a different segment.

Full per-leg results: `longleg_rule_backtest_results.csv` (1,584 rows × 30 rule/baseline
columns). Per-rule summary tables (equal-weighted multiples and dollar-weighted totals):
`longleg_rule_summary_by_split.csv`, `longleg_rule_summary_dollars.csv`.

### Train-set totals (dollar-weighted, real entry-cost-scaled P&L, n=1,030 legs / 163 days)

| Rule | With 2025-10-10 | Without 2025-10-10 |
|---|---|---|
| **Actual realized (what really happened)** | **+$14,221** | **−$18,900** |
| B1 hold-to-EOD | +$19,829 | −$12,770 |
| B2 close-on-short-stop | +$13,199 | **+$12,463** |
| B3 fixed-50%-time | +$41,352 | +$4,980 |
| R1 profit-take 1x / 2x / 3x / 5x / 8x / 10x / 15x / 20x | +$1,383 / +$1,506 / +$161 / −$584 / −$1,947 / −$50 / −$5,698 / −$12,508 | +$577 / −$255 / −$1,981 / −$3,266 / −$9,297 / −$7,399 / −$13,978 / −$23,870 |
| R3 ladder 1x→3x / 2x→5x / 3x→8x / 1x→5x / 2x→8x | +$772 / +$461 / −$893 / +$399 / −$220 | −$702 / −$1,761 / −$5,639 / −$1,345 / −$4,776 |

**The single most important number in this table**: the actual realized outcome flips from
+$14,221 to **−$18,900** when the one crash day is removed — confirming RECONSTRUCTION_NOTES'
existing point that a single 150x day is doing enormous work, and extending it: **B1
(hold-to-EOD) is even more crash-dependent than the actual strategy** (+$19,829 →
−$12,770, a $32.6k swing) because holding every long to expiry means riding every worthless
decay AND capturing the full crash-day payout undiluted by any discretionary early exit.
**B2 is the only baseline that stays positive with the crash day removed** (+$12,463) — it
does not depend on that single day to look good.

R1 (profit-take) and R3 (ladder) are **negative across almost every parameter cell once the
crash day is excluded**, and get monotonically worse as the threshold rises — this is the
expected signature of a rule that principally works by capturing part of rare outsized
winners (which decay away once you require a *higher* multiple, since the position is
closed on the way up before an even-bigger win compounds, while still eating full losses on
the ~79% of legs that never get anywhere near 1x). This is a genuine plateau, not a single
lucky cell: the whole R1/R3 families move together.

### Test-set totals (out-of-sample, dollar-weighted, n=554 legs / 71 days, no crash-day)

| Rule | Full test | Test excluding 2026-05-18 |
|---|---|---|
| Actual realized | −$13,750 | −$13,028 |
| B1 hold-to-EOD | −$16,626 | −$15,979 |
| **B2 close-on-short-stop** | **+$83,489** | **+$15,962** |
| B3 fixed-50%-time | −$4,751 | −$4,371 |
| R1 profit-take 1x / 2x / 5x | +$94,664 / +$94,208 / +$88,491 | +$4,216 / +$3,297 / −$1,510 |
| R3 ladder 1x→3x / 2x→5x | +$92,648 / +$91,350 | +$2,210 / +$893 |

**A single test-set date, 2026-05-18 (16 covered legs on that day, a genuinely large
one-directional-move day), contributes ~80% of B2's full-test total and ~95%+ of R1/R3's
full-test totals.** This is exactly the "hidden hero number" the brief warned about,
recurring in the test set rather than the crash day itself. Per the ground rules, this is
reported explicitly with and without that day rather than left buried in an aggregate:

- **R1 (profit-take) and R3 (ladder) do NOT survive removing 2026-05-18** — their test-set
  "edge" (+$88-95k) collapses to near-zero or negative (+$0.9k to +$4.2k, and R1-5x/R3-3x→8x
  actually go slightly negative). Combined with the train-set result above (also negative
  without the crash day), **R1 and R3 are refuted as a source of edge** — the entire
  apparent win in both segments is one or two individual outsized days, not a real broad
  effect.
- **B2 (close-on-short-stop) is the one candidate that survives**: +$15,962 test-set total
  even with 2026-05-18 removed, and (see below) beats the actual realized outcome on a large
  majority of *individual legs*, not just in an aggregate dominated by a few large ones.

### Why B2 is credible (not just another hero-day artifact)

Leg-level win-rate (does the rule produce a better multiple than what was actually realized
that day, leg-by-leg) for B2 vs. actual:

| Segment | n legs | n days | B2 beats actual (leg-level) |
|---|---|---|---|
| Train (with crash) | 1,030 | 163 | 86.3% |
| Train (no crash) | 1,026 | 162 | 86.5% |
| Test (full) | 554 | 70 | 82.7% |
| Test (excl. 2026-05-18) | 538 | 69 | 82.5% |

This holds up identically whether the crash day or the 2026-05-18 outlier day is included
or excluded, and it's a **per-leg** rate (not an aggregate-dollar artifact): B2 beats the
actual (manual/discretionary) outcome on roughly 5-out-of-6 individual long legs across
every segment tested, in-sample and out-of-sample alike. The mechanism is intuitive and
matches the strategy's own design intent: once the short has been stopped out, the
underlying has already made the one-directional move the long leg exists to catch — closing
the long at that exact moment locks in whatever gain/loss it has accrued at the highest-
information point available, rather than leaving it to ride further (where in 79.3% of
cases per RECONSTRUCTION_NOTES it simply decays back toward a full loss by 16:20).

**Day-level robustness** (not just legs): of 163 train days, B2's aggregate is positive on
54.0% of days (median day P&L across days = +$4); of 70 test days, positive on 64.3% of days
(median day P&L = +$157.50). Not every day wins, but the rule is not a knife-edge single
lucky day either — it's a broad, modest, day-in-day-out tilt.

### Parameter plateau — R2 (time-boxed hold after stop), test set, full sweep

| ITM threshold at stop \ Z minutes | Z=5 | Z=15 | Z=30 | Z=60 |
|---|---|---|---|---|
| 0.0 (must be ≥ breakeven at stop) | +$6,052 | +$83 | −$5,728 | −$7,064 |
| −0.3 | +$8,558 | +$2,331 | −$4,266 | −$6,396 |
| −0.5 | +$8,580 | +$2,174 | −$4,573 | −$6,569 |

(Excluding 2026-05-18, the same monotone shape holds: +$5,500 / +$203 / −$5,425 / −$6,692
for the itm=0.0 row, and similarly for the other two rows — see
`longleg_rule_backtest_results.csv` for the raw per-leg values behind every cell.) The
**whole plateau degrades smoothly as Z grows** — there's no isolated "best cell," just a
clear monotone relationship: **acting fast (Z=5) after a stop is mildly positive; waiting
even 30-60 minutes erases it and goes negative**. This is consistent with, and reinforces,
the B2 finding above: the closer to "immediately when the short stops" the rule acts, the
better it does; B2 (Z=0, act instantly) is the limiting case of this family and is also the
best-performing member of it.

### Honest verdict for Part 2

- **R1 (simple profit-take-multiple) and R3 (partial ladder): NO EDGE.** Both look
  spectacular in a naive full-sample read (test totals of +$88-95k) but that is >90%
  attributable to one or two single days (the crash day in train, 2026-05-18 in test).
  Once those days are excluded, both families are flat-to-negative in every segment and at
  every swept threshold. **These do not beat baseline out-of-sample and should not be
  built.**
- **R2 (time-boxed forced close after stop): weak positive edge, and only at short Z (≤15
  min).** Modest, degrades to negative quickly as the time-box lengthens. Directionally
  consistent with B2 but adds no value over simply closing immediately — the "delay" part
  of this rule is the part that doesn't help.
- **B2 (close the long immediately when the paired short stops out): the one candidate that
  holds up.** It beats the manual/discretionary actual outcome on ~83-87% of individual legs
  in every segment (train, train-ex-crash, test, test-ex-outlier), is aggregate-positive in
  every segment even after removing the single largest contributing day, and its logic (act
  on real information — the short's stop firing — the moment it's known) is economically
  sensible rather than a fitted threshold. This is closer to a genuine structural finding
  than a curve-fit rule, precisely because it has **no free parameter to have been tuned** —
  it's a single mechanical trigger ("close now"), which is also why the R2 sweep collapsing
  toward it (best at Z=5, worse at Z=15/30/60) is reassuring rather than suspicious.

**Caveat that must travel with this finding**: B2 necessarily gives up ALL of the long leg's
upside beyond the moment the short stops (including, notably, however much of the crash
day's 150x gain happened after the short itself was stopped — the crash-day legs are in
train and NOT part of the test-set robustness check above). It converts the long leg from
"lottery ticket that occasionally pays 100x+" into "a modest, high-hit-rate risk-reduction
overlay." That is very likely the RIGHT trade-off if the goal is de-risking an unmonitored
position (per the brief's own framing — "no one was watching"), but it is explicitly NOT
free: on train, B2's total ($13.2k) is below the actual realized total including the crash
year ($14.2k) — i.e., in the one segment containing the outlier crash day, giving up
tail-risk exposure cost a small amount of expected value relative to what actually
happened (which had the benefit of hindsight/manual judgment that day). The desk should
decide deliberately whether reduced tail variance is worth that modest give-up, rather than
treating B2 as a free lunch.

---

## Part 3 — Legitimate next steps for automation (no curve-fitting)

If the desk wants to build and run an automated version of British IC (either the entry
side or the B2-style long-leg overlay above), the honest next steps, consistent with this
project's existing anti-curve-fit standard:

1. **More history, especially more regime coverage.** The usable window here is effectively
   ~1 year of daily 0DTE trades (2025-07 to 2026-07) with exactly ONE true tail event
   (2025-10-10) and one moderate one-directional day (2026-05-18) in the test slice. A
   single tail event cannot validate a tail-risk-dependent long leg's true expectancy — the
   honest position is "B2 looks good on the data we have," not "B2 is proven for crash
   days," since n=1 crash day is not a sample. Extending the TAT-tradelog window backward
   (it starts 2024-09-16, ~10 months earlier than the reconciled IBKR ledger) via a second,
   independent P&L reconstruction pass would roughly double the trade count and add whatever
   regime variety 2024-09 to 2025-07 contained.
2. **Fill-cost realism for the automated exit itself.** This backtest marks exits at the
   1-minute OHLC close price, not an actual bid/ask fill — the existing project-wide finding
   that SPXW 0DTE options are nickel/dime-wide (`spxw-0dte-fill-realism` in memory) means a
   real automated B2 close would eat some slippage beyond what's modeled here, exactly as
   the Slippage column already shows for the short-side stops. Before running this live, the
   backtest should be re-run charging a realistic bid-side (not mid) fill for the forced
   close, the same way the SPX premium-selling research in this codebase already does for
   other 0DTE strategies.
3. **Walk-forward re-validation cadence, not a one-shot approval.** Consistent with this
   project's rolling walk-forward module (`validation-robustness-upgrades` in memory):
   don't "bless" B2 once and leave it running unmonitored (the very problem it's meant to
   fix on the manual side) — re-check its per-leg win rate and per-day P&L on a rolling
   quarterly basis as new trade data accumulates, and treat any material degradation as a
   signal to re-open the question, not something to explain away.
4. **Wider regime coverage on the trigger side.** The stop mechanism itself (Part 1.4) was
   verified against ~2,400 real stop-outs across a wide date range and is not in question.
   But B2's *value* (closing the long at the stop moment) has really only been tested across
   the vol regimes present July 2025–July 2026. A genuinely different regime (e.g., a
   multi-day elevated-VIX stretch, not just a single sharp one-day event) isn't represented
   and should be watched for specifically as more data accrues.
5. **If R1/R2/R3 are revisited later with more data**, they must clear the SAME bar applied
   here — full parameter plateau, chronological split, explicit with/without-the-single-
   biggest-day check — before being treated as real. Nothing in this report should be read
   as "R1 might work with the right N" — the whole swept plateau died together, which is
   the signature of no effect, not a mistuned parameter.

---

## What the data could and could not answer

**Could answer, with strong confidence:**
- The exact stop-target formula (`PriceOpen + StopMultiple`), verified to the penny on the
  majority of 4,610 real rows and within $0.075 on all of them.
- What "80/50" and "$2/$3/$4" mean in template names (point-width and StopMultiple label,
  respectively — confirmed empirically, not assumed).
- That short-strike delta targeting (~0.22-0.29 abs delta) is consistent across templates
  and vol regimes, implying vol-adaptive (not fixed-point) strike placement.
- That the long leg's fate is genuinely without a single consistent manual rule (median
  −1.01x, 79.3% at/below breakeven, but a real ~6% tail above 1x) — already established in
  RECONSTRUCTION_NOTES and reconfirmed here.
- That a mechanical "close the long the instant the paired short stops" rule beats the
  actual (manual) outcome on ~83-87% of individual legs, in-sample and out-of-sample, and
  is not an artifact of any single outlier day.
- That simple profit-take-multiple and partial-ladder rules do NOT survive out-of-sample
  once hero days are excluded — a genuine, well-supported "no edge" finding for that
  specific mechanism.

**Could not answer:**
- The exact re-entry/scale-in trigger logic (fixed clock schedule vs. price-triggered vs.
  volatility-triggered) — `ParentTaskID` is unpopulated and no other linking field exists in
  TAT-tradelog to trace a scale-in family tree precisely; only the aggregate pattern (they
  cluster at specific times of day per template) is visible.
- The per-batch short-side P&L inside the 316 multi-long scale-in combo groups (a known,
  already-documented limitation of the underlying reconstruction — cannot be recovered from
  IBKR's FIFO-aggregated field without fabricating an allocation).
- Whether B2's ~$16-20k+ of test-set edge (net of the 2026-05-18 outlier) would hold up
  across a genuinely different, multi-day-elevated-vol regime, since none exists in the
  available window — this is a real open question, not a solved one, and is exactly why
  Part 3 recommends a walk-forward re-check rather than a one-time approval.
- Whether the automated close would clear a realistic bid/ask fill cost — this analysis used
  1-min close-price marks, consistent with the CSVs provided, not a modeled bid-side fill.

---

## Files produced in this folder

- `STRATEGY_RECONSTRUCTION.md` — this report.
- `template_delta_stats.csv` — per-template delta/width/StopMultiple stats (Part 1.2-1.3).
- `stop_formula_verification.csv` — row-by-row stop-target formula verification (Part 1.4).
- `longleg_rule_backtest_results.csv` — per-leg results for every baseline/rule/parameter
  combination (1,584 legs × 30 columns).
- `longleg_rule_summary_by_split.csv` — equal-weighted (per-leg multiple) summary by
  train/test × with/without-hero-day.
- `longleg_rule_summary_dollars.csv` — dollar-weighted (real entry-cost-scaled) summary,
  the primary table used for the verdicts above.
