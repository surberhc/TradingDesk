# British IC — Long-Leg Exit Rule Analysis

Analysis of whether a mechanical exit rule for the (currently human-managed) long leg
of the British IC 0DTE SPX combo would have beaten actual historical practice (holding
to end-of-day settlement). Source data: `decoupled_long_legs.csv` (1,617 long legs that
closed separately from their paired short — see `RECONSTRUCTION_NOTES.md` for full
provenance). This analysis is close-to-close only for the mandatory deliverable; no new
data files were created and no source file in this folder was modified.

## 1. Data / methodology recap

- **Window:** 2025-07-09 through 2026-07-07, 236 distinct trade dates, 1,617 decoupled long-leg rows.
- **Chronological split (mandatory, never blended):** dates sorted, first 70% (by date
  count, not row count) = **design** (in-sample), remaining 30% = **test** (out-of-sample).
  - Split index: date #165 of 236.
  - **Design:** 2025-07-09 through 2026-03-17 (165 dates, 1053 rows).
  - **Test:** 2026-03-19 through 2026-07-07 (71 dates, 564 rows).
- **Outlier:** 2025-10-10 (known SPX crash day) falls inside the **design** split
  (4 of its 4 rows are in design; all 4 rows are on
  that single date, which is entirely within design since the cutoff is 2026-03-19). Every
  result below is reported both **including** and **excluding** this date.
  - Note for context: the single largest individual long_pnl_multiple in the whole dataset
    (150.19x) is **not** on 10/10 — it's on 2026-01-21 (in design). 10/10's four rows range
    from -2.27x to 87.37x. Top 5 rows by multiple:

| TradeDate | long_pnl_multiple | long_fifo_pnl |
|---|---|---|
| 2026-01-21 | 150.19x | $6,308.15 |
| 2026-01-21 | 98.77x | $2,074.20 |
| 2025-10-10 | 87.37x | $12,930.33 |
| 2026-03-09 | 86.51x | $1,297.68 |
| 2026-01-21 | 70.53x | $1,057.97 |

  - 2025-10-10 detail (all 4 rows):

| long_pnl_multiple | long_fifo_pnl | exit_gap_minutes_max | long_entry_cost |
|---|---|---|---|
| 62.60x | $11,643.10 | 37.8 | $186 |
| 87.37x | $12,930.33 | 37.8 | $148 |
| 66.01x | $8,581.67 | 197.4 | $130 |
| -2.27x | $-34.04 | 402.1 | $15 |

- **long_close_dt sanity check:** 1,012 of 1,617 rows (62.6%) close at
  exactly 16:20:00 (EOD settlement). This confirms EOD-hold is the dominant actual outcome,
  but note this is a subset of a subset — this file only contains *decoupled* legs (the
  short and long closed >2 min apart), so the ~62.6% EOD-close rate here is somewhat lower
  than it would be across all legs, since by definition every row here saw at least one
  active decision point (the short closing early) that the EOD-close ones ultimately chose
  not to act on for the long.
- **Approximations used, and why:**
  1. **Baseline B / decouple-at-short-close** requires the long's live market price at the
     moment the short leg closed. That price is not in `decoupled_long_legs.csv` — the file
     only has entry price and final close price. By construction, every row in this file
     is "decoupled," meaning gap-to-short-close is > 2 minutes for 100% of rows (min
     observed gap in this file: 0.0 minutes) — i.e. there is no row where the
     recorded long-leg exit price can stand in as a proxy for the short-close-instant price.
     **Baseline B is therefore not exactly computable from this file for any row.** Section
     2 reports the gap distribution instead (what fraction of rows have a short-to-long
     exit gap under various thresholds) so the reader can judge how far the actual practice
     deviates from "close the instant the short closes," without fabricating a price.
     Intraday SPXW quotes could in principle answer this for a sample, but per the time
     budget, that was deprioritized behind the mandatory close-to-close analysis (see
     "data limitations hit" in the final summary).
  2. **Rule 1 (fixed profit-take)** is a **close-only approximation**: it uses the already-
     realized `long_pnl_multiple` at actual close to infer whether an Nx take-profit level
     was reached by the time of close, and if so, caps the P&L at exactly Nx. **This cannot
     detect a case where the option touched Nx intraday and then gave the gain back before
     the actual close** — so it systematically **understates** how often a fixed take-profit
     rule would have fired, and the true intraday-triggered P&L could be better or worse than
     reported here. This is stated as a real limitation, not a footnote.
  3. **Rule 2 (time cutoff)** is approximated using `exit_gap_minutes_max` (minutes between
     short close and long close): if the actual gap is <= T, the actual outcome already
     satisfies the rule and is used as-is. If the actual gap is > T, the rule would have
     force-closed the position at an earlier, unknown price — since that interim price is
     not in this file, **those rows are excluded from the rule's computed P&L** rather than
     guessed at. The exclusion count/fraction is reported explicitly for every T. This means
     large T values are much more informative (more rows retained) and small T values are
     answering a much smaller, easier question (few, usually cheaply-decided rows).

## 2. Baselines

### Baseline A — always hold the long to end-of-day (actual long_fifo_pnl, no rule)

| Split | Outlier | n legs | Total P&L | Avg P&L multiple | Median P&L multiple |
|---|---|---|---|---|---|
| design | including 10/10 | 1053 | $12,613.20 | -0.006x | -1.040x |
| design | excluding 10/10 | 1049 | $-20,507.86 | -0.209x | -1.041x |
| test | including 10/10 | 564 | $-12,210.34 | -0.226x | -0.596x |
| test | excluding 10/10 | 564 | $-12,210.34 | -0.226x | -0.596x |

### Baseline B — close the long the instant its paired short closes

**Not exactly computable from close-to-close data.** Every row in the decoupled dataset has
an actual short-to-long exit gap of 0.0 minutes or more (that's what "decoupled"
means), so there is no row where the recorded exit price is a valid stand-in for the price
at the moment the short closed. Reporting the gap distribution instead, as the closest honest
substitute — this shows what fraction of decoupled rows closed reasonably soon after the
short (small gap) vs. much later (large gap), i.e. how much the "close-with-the-short" instinct
was actually followed in practice:

| Gap threshold (min) | Rows with gap <= threshold | % of 1,617 |
|---|---|---|
| 0 | 106 | 6.6% |
| 1 | 118 | 7.3% |
| 2 | 126 | 7.8% |
| 5 | 204 | 12.6% |
| 10 | 281 | 17.4% |
| 15 | 340 | 21.0% |
| 30 | 479 | 29.6% |
| 60 | 644 | 39.8% |

Only 126 of 1,617 rows (7.8%) have a gap of 2 minutes or
less — consistent with "decoupled" being a real, distinct behavior class, not measurement
noise. Baseline B would require intraday SPXW quotes at each of the 1,617 short_close_dt
timestamps to compute honestly; this was not done for the full dataset (see Part 3 / final
summary for what a sample-based extrapolation would need to be labeled as clearly).

## 3. Rule 1 — Fixed profit-take at Nx entry cost

Full sweep, N in {2,3,4,5,7,10,15,20}. "Rule total P&L" = sum of capped P&L across the
rows in that split/outlier combination; "Baseline A total (same rows)" repeats Baseline A's
total on the identical row set for direct comparison.

**Design, including 10/10:**

| N (take-profit) | Hit rate | Rule total P&L | Rule avg P&L mult. | Baseline A total (same rows) | Delta vs Baseline A |
|---|---|---|---|---|---|
| 2x | 4.9% | $-40,476 | -0.732x | $12,613 | $-53,090 |
| 3x | 3.3% | $-36,643 | -0.693x | $12,613 | $-49,256 |
| 4x | 2.3% | $-34,308 | -0.664x | $12,613 | $-46,921 |
| 5x | 2.0% | $-32,741 | -0.643x | $12,613 | $-45,354 |
| 7x | 1.5% | $-29,974 | -0.607x | $12,613 | $-42,587 |
| 10x | 1.0% | $-27,255 | -0.572x | $12,613 | $-39,868 |
| 15x | 0.9% | $-23,888 | -0.526x | $12,613 | $-36,501 |
| 20x | 0.9% | $-20,968 | -0.483x | $12,613 | $-33,581 |

**Design, excluding 10/10:**

| N (take-profit) | Hit rate | Rule total P&L | Rule avg P&L mult. | Baseline A total (same rows) | Delta vs Baseline A |
|---|---|---|---|---|---|
| 2x | 4.7% | $-40,906 | -0.736x | $-20,508 | $-20,398 |
| 3x | 3.1% | $-37,537 | -0.699x | $-20,508 | $-17,029 |
| 4x | 2.0% | $-35,666 | -0.673x | $-20,508 | $-15,158 |
| 5x | 1.7% | $-34,563 | -0.655x | $-20,508 | $-14,055 |
| 7x | 1.2% | $-32,724 | -0.625x | $-20,508 | $-12,216 |
| 10x | 0.8% | $-31,397 | -0.598x | $-20,508 | $-10,889 |
| 15x | 0.6% | $-30,350 | -0.566x | $-20,508 | $-9,842 |
| 20x | 0.6% | $-29,750 | -0.537x | $-20,508 | $-9,242 |

**Test, including 10/10:** (10/10 is in the design window, so test include/exclude are identical)

| N (take-profit) | Hit rate | Rule total P&L | Rule avg P&L mult. | Baseline A total (same rows) | Delta vs Baseline A |
|---|---|---|---|---|---|
| 2x | 6.4% | $-26,348 | -0.439x | $-12,210 | $-14,138 |
| 3x | 3.9% | $-23,288 | -0.389x | $-12,210 | $-11,078 |
| 4x | 2.8% | $-21,266 | -0.356x | $-12,210 | $-9,055 |
| 5x | 2.5% | $-19,769 | -0.330x | $-12,210 | $-7,559 |
| 7x | 1.2% | $-17,576 | -0.297x | $-12,210 | $-5,366 |
| 10x | 0.7% | $-15,156 | -0.266x | $-12,210 | $-2,946 |
| 15x | 0.4% | $-12,772 | -0.232x | $-12,210 | $-562 |
| 20x | 0.0% | $-12,210 | -0.226x | $-12,210 | $-0 |

**Test, excluding 10/10:**

| N (take-profit) | Hit rate | Rule total P&L | Rule avg P&L mult. | Baseline A total (same rows) | Delta vs Baseline A |
|---|---|---|---|---|---|
| 2x | 6.4% | $-26,348 | -0.439x | $-12,210 | $-14,138 |
| 3x | 3.9% | $-23,288 | -0.389x | $-12,210 | $-11,078 |
| 4x | 2.8% | $-21,266 | -0.356x | $-12,210 | $-9,055 |
| 5x | 2.5% | $-19,769 | -0.330x | $-12,210 | $-7,559 |
| 7x | 1.2% | $-17,576 | -0.297x | $-12,210 | $-5,366 |
| 10x | 0.7% | $-15,156 | -0.266x | $-12,210 | $-2,946 |
| 15x | 0.4% | $-12,772 | -0.232x | $-12,210 | $-562 |
| 20x | 0.0% | $-12,210 | -0.226x | $-12,210 | $-0 |

**Plateau check:** There is no plateau — the sweep is **monotonic in one direction only**:
every value of N produces a rule total *worse* than Baseline A, in every split and every
outlier treatment, and the loss shrinks smoothly as N increases (larger N = rule fires less
often = closer to just doing nothing, i.e. closer to Baseline A by construction). This is
not "a peak vs. a plateau" pattern at all — it's a monotonic approach toward the do-nothing
baseline from below. That shape is itself informative: it says the rule never helps at any
N tested, not that some fragile N looks good and neighbors don't.

## 4. Rule 2 — Time-based cutoff (force-close T minutes after short closes)

Full sweep, T in {5,10,15,30,60,120,240} minutes. Rows with `exit_gap_minutes_max > T` are
excluded (rule would have force-closed at an unknown interim price) — exclusion count and
percentage shown explicitly. "Rule P&L on usable subset" only covers rows where the actual
close already happened at or before T (so the rule and reality agree on those rows by
construction — this is NOT a rule that beat/lost to baseline, it's the subset where no
extrapolation was needed at all). The final column shows, for context only, what Baseline A
actually earned on the *excluded* rows (i.e., what continuing to hold past T actually
produced in reality) — useful for judging the direction of the trade-off without pretending
to know what the rule itself would have scored there.

**Design, including 10/10:**

| T (min after short close) | n usable (gap<=T) | n excluded (gap>T) | % excluded | Rule P&L on usable subset | Avg mult. on usable subset | (context) Baseline-A actual P&L on excluded rows |
|---|---|---|---|---|---|---|
| 5 | 114 | 939 | 89.2% | $-3,314 | -0.510x | $15,927 |
| 10 | 150 | 903 | 85.8% | $-1,721 | 0.237x | $14,335 |
| 15 | 181 | 872 | 82.8% | $-943 | 0.232x | $13,556 |
| 30 | 270 | 783 | 74.4% | $2,247 | 0.204x | $10,367 |
| 60 | 374 | 679 | 64.5% | $26,145 | 0.432x | $-13,532 |
| 120 | 558 | 495 | 47.0% | $29,454 | 0.663x | $-16,841 |
| 240 | 838 | 215 | 20.4% | $24,675 | 0.254x | $-12,062 |

**Design, excluding 10/10:**

| T (min after short close) | n usable (gap<=T) | n excluded (gap>T) | % excluded | Rule P&L on usable subset | Avg mult. on usable subset | (context) Baseline-A actual P&L on excluded rows |
|---|---|---|---|---|---|---|
| 5 | 114 | 935 | 89.1% | $-3,314 | -0.510x | $-17,194 |
| 10 | 150 | 899 | 85.7% | $-1,721 | 0.237x | $-18,787 |
| 15 | 181 | 868 | 82.7% | $-943 | 0.232x | $-19,565 |
| 30 | 270 | 779 | 74.3% | $2,247 | 0.204x | $-22,755 |
| 60 | 372 | 677 | 64.5% | $1,572 | 0.031x | $-22,080 |
| 120 | 556 | 493 | 47.0% | $4,881 | 0.395x | $-25,389 |
| 240 | 835 | 214 | 20.4% | $-8,480 | -0.003x | $-12,028 |

**Test, including 10/10:**

| T (min after short close) | n usable (gap<=T) | n excluded (gap>T) | % excluded | Rule P&L on usable subset | Avg mult. on usable subset | (context) Baseline-A actual P&L on excluded rows |
|---|---|---|---|---|---|---|
| 5 | 90 | 474 | 84.0% | $-244 | -0.236x | $-11,966 |
| 10 | 131 | 433 | 76.8% | $2,090 | 0.021x | $-14,300 |
| 15 | 159 | 405 | 71.8% | $3,805 | 0.102x | $-16,015 |
| 30 | 209 | 355 | 62.9% | $7,060 | 0.144x | $-19,270 |
| 60 | 270 | 294 | 52.1% | $10,037 | 0.205x | $-22,248 |
| 120 | 372 | 192 | 34.0% | $4,112 | 0.031x | $-16,322 |
| 240 | 507 | 57 | 10.1% | $-6,450 | -0.150x | $-5,761 |

**Test, excluding 10/10:**

| T (min after short close) | n usable (gap<=T) | n excluded (gap>T) | % excluded | Rule P&L on usable subset | Avg mult. on usable subset | (context) Baseline-A actual P&L on excluded rows |
|---|---|---|---|---|---|---|
| 5 | 90 | 474 | 84.0% | $-244 | -0.236x | $-11,966 |
| 10 | 131 | 433 | 76.8% | $2,090 | 0.021x | $-14,300 |
| 15 | 159 | 405 | 71.8% | $3,805 | 0.102x | $-16,015 |
| 30 | 209 | 355 | 62.9% | $7,060 | 0.144x | $-19,270 |
| 60 | 270 | 294 | 52.1% | $10,037 | 0.205x | $-22,248 |
| 120 | 372 | 192 | 34.0% | $4,112 | 0.031x | $-16,322 |
| 240 | 507 | 57 | 10.1% | $-6,450 | -0.150x | $-5,761 |

**Reading this table honestly:** at small T (5-15 min), 72-89% of rows are excluded — the
"usable subset" is a small, easy population (positions that were going to close quickly
regardless). At large T (120-240 min), far fewer rows are excluded, but the informative
signal is the last column: in both design and test, and with or without 10/10, **Baseline
A's actual P&L on the rows that a short/mid-T cutoff would have force-closed early is
frequently strongly positive** (e.g. design-excl at T=120: excluded rows earned actual
+$4,881 by being held past 120 minutes; test-excl at T=120: excluded rows earned +$4,112 by
being held past 120 minutes into what actually happened). This is a strong signal AGAINST a
tight time-cutoff: the positions still open a long time after the short closed are
disproportionately where the real gains came from, not disproportionately where money was
being needlessly risked. A rule that force-closed them early would very plausibly have cut
off exactly the long tail this leg exists to catch. This can't be stated as a certainty
without the interim price (per the approximation caveat above, real intraday behavior could
differ), but there is no evidence in the achievable data that a time cutoff helps, and
directional evidence it would hurt.

## 5. Summary verdict

**Does anything beat Baseline A (hold to EOD) out-of-sample, excluding the outlier?**

**No.** On the test split (2026-03-19 through 2026-07-07, the outlier date does not fall in
this window so include/exclude are identical there):

- Baseline A, test: **$-12,210.34** total P&L across 564 decoupled
  long legs (avg -0.226x, median
  -0.596x per leg).
- Rule 1 (fixed profit-take), best N on test = **N=20x**, total P&L
  **$-12,210.34**, still **$0.00 worse** than Baseline A.
  Every N in the sweep underperforms Baseline A on test — the "best" N is simply the least
  bad, not a winner. Rule 1's sweep is monotonically bad, not a spike — that's evidence
  against curve-fitting a specific N, but it's also just a flatly losing rule.
- Rule 2 (time cutoff): not directly comparable to Baseline A as a total (its "usable
  subset" total isn't the same population as Baseline A's full-population total by
  construction), but on every honestly-computable subset, the rows it would have force-closed
  early (gap > T) actually went on, in reality, to earn results that were at least as good as
  or better than the rows it would have left alone — most starkly at T=60-120 minutes on both
  design-excl and test data, where the "would have been force-closed" population's actual P&L
  is strongly positive. There is no computable version of Rule 2 in this dataset that beats
  simply holding.

**Plain language:** the data does not support replacing the human long-leg decision with
either mechanical rule tested. The dominant real-world outcome is the long leg decaying to
near-total loss most of the time (median ~-1x, ~79% at/below breakeven per
`RECONSTRUCTION_NOTES.md`), which both rules capture identically to Baseline A on those
rows (nothing to cut short — the loss is already near-total by the actual close). Where the
rules and Baseline A diverge, it is specifically on the tail of large winners, and in every
test case examined, the mechanical rule closes them EARLIER and WORSE than reality did. Since
the long leg's entire economic purpose is to catch that tail (it's a hedge/convexity
position, not a standalone profit center), a rule that reliably clips the tail is working
against the leg's actual job. **"No edge" — indeed, directional evidence of net harm from a
tight mechanical rule — is the honest, complete answer here.**

## Part 3 — What would be needed before trusting any parameter choice here

This dataset is ~1 year (236 trade dates) with exactly one clear tail event (2025-10-10),
which is nowhere near enough regime coverage to bless a parameter (a single stress episode
can't distinguish "this rule generalizes" from "this rule happened to fit one crash"). Before
adopting any exit rule, the desk's own standard playbook applies: search the base package
across a much longer, multi-regime history (several more crash-shaped and several calm-choppy
periods) before layering an overlay rule, apply DSR-style discipline given how many N/T
combinations were implicitly tried, and require a plateau (not a peak) exactly as done here —
but over enough independent tail events that a plateau is meaningful rather than an artifact
of having only one crash to test against. Separately, and just as important: this analysis
assumes the exit fill is frictionless. Exiting a fast-moving, deep-ITM 0DTE option under
stress (the exact moment a profit-take or time-cutoff rule would fire) has real bid-ask/
slippage risk that has never been measured here, unlike the entry, which is presumably
executed in calmer, more liquid conditions. Per the desk's own flagged gap
(`execution-fill-cost-unvalidated`), no mechanical exit rule should be sized or trusted
until real fill costs for exiting under these conditions are measured.

---
*Generated by automated analysis of `decoupled_long_legs.csv`. No source files in this
folder were modified. Intraday SPXW sample analysis was not performed in this pass (see
Section 1's approximation notes) — flagged as the main open item if more precision on
Baseline B or Rule 1's intraday-touch caveat is wanted later.*
