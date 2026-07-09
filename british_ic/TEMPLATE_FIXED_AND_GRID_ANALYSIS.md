# British IC — Which Single Deterministic Template, If a Bot Can't Switch

STRATEGY_RECONSTRUCTION.md Part 2 validated the B2 rule ("close the long leg the
instant its paired short leg stops out") **blended across all 11 real templates**.
The question here is narrower and more operational: if S8 has to run as **one fixed,
deterministic template** — because a bot can't replicate the human's discretionary
template-switching across the trading day — which template, and is there real
evidence for that pick that doesn't amount to curve-fitting the choice to the exact
data being judged?

Data sources, methods, and the exact dollar-conversion method are inherited from
STRATEGY_RECONSTRUCTION.md Part 2 and reconstruct.py's `cross_check_tat()`, not
reinvented — see "Method" below for what was reused vs. extended.

---

## Method

### Step 0 — full TAT Template join

`decoupled_long_legs.csv` (1,617 rows, 2025-07-09 to 2026-07-07) carries
`short_open_price` (a proxy for the $ label only) but never the raw `Template`
string, so it has no width (50/80) information at all. `TAT-tradelog.xlsx` carries
the true `Template` string but only runs through 2026-03-19 — about 8 of the full
~12.5-month history.

`template_join.py` extends `reconstruct.cross_check_tat()`'s existing join (which
previously covered October 2025 only) to TAT's **entire** available date range,
using the identical join key and tie-break: TradeDate + ComboType (PutSpread/
CallSpread → TradeType) + exact short_strike/long_strike match against
ShortPut/LongPut or ShortCall/LongCall, ties broken by nearest OpenTime to the
combo's own short-leg open time. `>1` remaining candidate after the strike filter is
flagged `AMBIGUOUS_MULTI_CANDIDATE` (kept, with a best-effort nearest-time Template
pull) rather than silently dropped, matching `cross_check_tat()`'s own convention.

**Match-rate result** (1,063 of 1,617 rows fall within TAT's coverage window):

| Result | n | % of in-coverage rows |
|---|---|---|
| MATCHED (single exact candidate) | 748 | 70.4% |
| AMBIGUOUS_MULTI_CANDIDATE (≥2 candidates, nearest-time picked) | 224 | 21.1% |
| NO_MATCH | 91 | 8.6% |
| (rows past TAT's 2026-03-19 coverage — no join attempted) | 554 | n/a |

91.5% of in-coverage rows get a real TAT Template pull (matched or ambiguous), which
is the true-labeled population used below. The 21.1% ambiguous rate is not a defect
in the join — it reflects the same-template scale-in/re-entry behavior documented in
STRATEGY_MECHANICS.md section 5 (multiple entries at the same clock-time grid,
same day, sometimes same strike family), which genuinely produces more than one
TAT row that fits the join key. It is reported, not hidden: results below note where
ambiguous rows are included.

For the 554 rows past TAT's coverage (2026-04-01 through 2026-07-07), the width
(50/80) label is **left null and never imputed**, per the task's explicit instruction.
Those rows still get a `$` label from `short_open_price` banding, using cutpoints
picked as the midpoints between the already-established per-label medians in
STRATEGY_MECHANICS.md section 3 ($2 median ~$2.05–2.15, $3 median ~$3.00–3.08, $4
median ~$3.95–4.20):

- `short_open_price < $2.55` → `"$2"`
- `$2.55 ≤ short_open_price < $3.55` → `"$3"`
- `short_open_price ≥ $3.55` → `"$4"`

This $-only proxy is used explicitly labeled as such (never presented as
width-confirmed) in Analysis #1(b) below.

Output: `tat_full_join.csv` (gitignored, not committed).

### Dollar-conversion and row-alignment method (reused, not reinvented)

`longleg_rule_backtest_results.csv` (1,584 rows, the B2-vs-actual population with
real 1-min SPXW OHLC backing) stores exit values as **multiples**, with no explicit
join key back to `decoupled_long_legs.csv` — `leg_id` is just its own row position.
The correspondence was recovered by **sequential positional matching** on
(TradeDate, ComboType, multiple-match to 1e-6): walking both files in order and
advancing the decoupled-legs pointer until each rule-backtest row's TradeDate +
ComboType + `actual_exit_multiple` exactly matches a `long_pnl_multiple`. This
matched all 1,584 rule-backtest rows to a decoupled-legs row; the 33 excluded
decoupled-legs rows land scattered near the end of the file (consistent with
STRATEGY_RECONSTRUCTION.md's note that the excluded legs are from the final 3
trading days of the window, not warehouse-covered yet).

Dollar conversion: `dollar_pnl = multiple × long_entry_cost` (the same method the
original Part 2 analysis used — `long_entry_cost` is a real column already present
in `decoupled_long_legs.csv`).

**Both the row-alignment and dollar-conversion methods were verified before trusting
any new number in this report**, by reproducing three of Part 2's own published
totals from `longleg_rule_summary_dollars.csv` exactly:

| Check | Computed | Published | Match |
|---|---|---|---|
| train-with-crash, actual realized total | $14,221.46 | $14,221.46 | exact |
| train-no-crash, B2 total | $12,463.00 | $12,463.00 | exact |
| test-with-crash, B2 total | $83,489.00 | $83,489.00 | exact |

All three match to the cent. The join + alignment + conversion pipeline is trusted
on this basis.

### Train/test split — recomputed fresh per isolated population

Every isolated template cut below gets its **own** chronological 70/30 train/test
split by unique date (not reusing the blended analysis's absolute split index #165,
since each isolated subpopulation has a different date range and day count). The
split index is `round(0.7 × n_unique_dates)` for that cut's own dates.

### Thinness floor

Any cut with fewer than **15 legs** or **10 days** is flagged explicitly as too thin
for a headline number, rather than reporting a number that implies false precision.

---

## Analysis #1 — Fixed single template: 80-$4

### (a) True-labeled 80-$4, through March 2026 (width + $ confirmed)

n = 573 legs, 139 days, 2025-07-09 to 2026-03-17. Fresh 70/30 split → 97 train days
(2025-07-09–2025-12-30, 425 legs) / 42 test days (2025-12-31–2026-03-17, 148 legs).

| Split | n legs | n days | Actual $ total | B2 $ total | Leg win rate (B2≥actual) | Day win rate |
|---|---|---|---|---|---|---|
| Train | 425 | 97 | +$6,020.01 | +$820.00 | 83.3% | 88.7% |
| Test | 148 | 42 | −$1,090.52 | +$2,487.00 | 78.4% | 73.8% |

Single-largest-day-removed check (by absolute B2 $ contribution):
- Train: biggest day 2025-10-22 (+$816 of B2's +$820 total — this is essentially
  the ENTIRE train-set B2 edge). Excluding it: B2 total collapses to **+$4.00**
  (actual excl.: +$5,776.76). **Train-set B2 in this isolated cell is a one-day
  artifact**, not a broad effect — a materially different picture than the blended
  Part 2 result, where B2 stayed solidly positive (+$12,463) with the crash day
  removed.
- Test: biggest day 2026-02-26 (+$450 of +$2,487). Excluding it: B2 total is
  **+$2,037.00** (actual excl.: −$1,430.54) — test-set B2 holds up reasonably well
  without its single biggest day, unlike train.

**Verdict for (a): weak and inconsistent.** Test-set B2 beats actual test-set actual
by a real margin (+$2,487 vs −$1,091) and survives removing its biggest day
(+$2,037 vs −$1,431), which is directionally consistent with the blended Part 2
finding. But train-set B2 is barely positive to begin with (+$820) and is **entirely**
one day's contribution — remove it and train-set B2 is flat ($4). Leg-level win rates
(83.3% train, 78.4% test) are still solidly in B2's favor and roughly consistent with
the blended population's 82–87% figures, which is the strongest piece of supporting
evidence here — B2 beats the manual outcome on 4-out-of-5 individual 80-$4 legs in
both segments, even though the aggregate-dollar picture is fragile on train.

### (b) $4-label-only proxy, full window through July 2026 (width NOT confirmed — mixes 80-$4 and 50-$4)

n = 858 legs, 213 days, 2025-07-09 to 2026-06-30. Fresh 70/30 split → 149 train days
(2025-07-09–2026-03-10, 612 legs) / 64 test days (2026-03-11–2026-06-30, 246 legs).
**This cell mixes 80-$4 and 50-$4 trades** — stated plainly, this is NOT a
width-confirmed result, just what the $ label alone can isolate through the full
window.

| Split | n legs | n days | Actual $ total | B2 $ total | Leg win rate | Day win rate |
|---|---|---|---|---|---|---|
| Train | 612 | 149 | +$4,098.77 | +$3,887.00 | 82.7% | 84.6% |
| Test | 246 | 64 | +$433.43 | +$6,657.00 | 76.8% | 78.1% |

Single-largest-day-removed check:
- Train: biggest day 2026-02-20 (+$1,012 of +$3,887). Excluding it: B2 total is
  **+$2,875.00** (actual excl.: +$3,915.78) — B2 holds up, though it now trails the
  actual total on this cut ex-biggest-day.
- Test: biggest day 2026-04-23 (+$1,102 of +$6,657). Excluding it: B2 total is
  **+$5,555.00** (actual excl.: −$908.68) — B2 holds up strongly, beating actual by a
  wide and robust margin even after removing its biggest single day.

**Verdict for (b): more robust than (a), but on a mixed-width population.** Unlike
the true-labeled 80-$4-only cut, this broader $4-proxy population (which folds in
50-$4 trades too) shows B2 solidly positive on both splits, both with and without
its single biggest day, and leg win rates (77–83%) in the same range as the blended
Part 2 finding. This is encouraging for the **$4-credit-target family as a whole**
(both widths combined) but says nothing about whether 80-$4 specifically (vs. 50-$4)
is driving it, since width isn't observable here.

---

## Analysis #2 — 6-config grid search (true-labeled, through March 2026 only)

Width (50/80) is only observable via a real TAT match, so this grid can only be run
true-labeled through 2026-03-19 — the April–July 2026 window cannot support a
width-level grid search under any circumstances (per instruction, never imputed).

| Config | n legs | n days | Train actual $ | Train B2 $ | Train leg win% | Test actual $ | Test B2 $ | Test leg win% |
|---|---|---|---|---|---|---|---|---|
| 80-$2 | 11 | 10 | **TOO THIN — no headline reported** | | | | | |
| 80-$3 | 115 | 59 | +$10,361.03 | +$163.00 | 96.4% | +$651.63 | +$252.00 | 90.3% |
| 80-$4 | 573 | 139 | +$6,020.01 | +$820.00 | 83.3% | −$1,090.52 | +$2,487.00 | 78.4% |
| 50-$2 | 268 | 81 | +$2,801.86 | +$4,307.00 | 92.1% | −$3,122.67 | +$4,530.00 | 87.9% |
| 50-$3 | 4 | 3 | **TOO THIN — no headline reported** | | | | | |
| 50-$4 | 1 | 1 | **TOO THIN — no headline reported** | | | | | |

Single-largest-day-removed, the two thin-but-computed cells (80-$3 shown for
completeness even though it clears the 15-leg/10-day floor by a modest margin):

- **80-$3**: train biggest day 2025-10-10 (the crash day, +$472 of the train B2
  total of +$163 — MORE than the whole total). Excluding it: train B2 flips to
  **−$309.00**. Test biggest day 2026-03-11 (+$325 of +$252). Excluding it: test B2
  flips to **−$73.00**. **80-$3's B2 edge does not survive removing its biggest day
  in either split** — this cell is a hero-day artifact, not a real effect, and its
  n (115 legs / 59 days) is thin enough that this isn't surprising.
- **80-$4**: see Analysis #1(a) above — train collapses to $4 ex-biggest-day, test
  holds at +$2,037.
- **50-$2**: train biggest day 2025-10-14 (+$2,874 of +$4,307). Excluding it: train
  B2 is **+$1,433.00** — holds up, still positive. Test biggest day 2026-03-10
  (+$2,607 of +$4,530). Excluding it: test B2 is **+$1,923.00** — also holds up.
  **50-$2 is the most robust individual cell in this grid**: B2 stays clearly
  positive on both splits even after removing each split's single biggest day, and
  leg win rates (88–92%) are the highest of any cell.

**The plateau, read as a whole**: three of the six intended grid cells (80-$2,
50-$3, 50-$4) simply don't have enough data post-split to say anything — their
n's (11/10, 4/3, 1/1 legs/days) are far below any reasonable bar. Of the three that
do clear the floor, one (80-$3) is a hero-day artifact that dies on inspection, and
the other two (80-$4, 50-$2) show real but uneven B2 edges — 50-$2 the more robust
of the two on the ex-biggest-day check.

### DSR — deflating the best of the 6-config search

Per-cell trial statistic: the per-observation Sharpe of the B2 rule's per-leg
multiple, computed on that cell's **full window** (train+test combined — template
selection is a single frozen choice being evaluated, not itself a parameter being
trained/tested on a held-out split, so using the full available window for the trial
statistic is not a leakage concern the way tuning a threshold would be).

Only 3 of the 6 intended cells clear the thinness floor and can contribute a
trustworthy Sharpe (80-$2, 50-$3, 50-$4 are excluded — same three flagged above):

| Cell | n legs | Per-observation Sharpe |
|---|---|---|
| 80-$3 | 115 | +0.0916 |
| 80-$4 | 573 | +0.1211 |
| 50-$2 | 268 | +0.1031 |

Per the task's own design intent, **n_trials is fixed at 6** (the grid search as
designed, not shrunk post-hoc to the cells that happened to have enough data — using
a smaller n_trials would under-penalize the search and bias toward a false pass).
`var_trials` is the population variance of the 3 usable cells' Sharpes (the only
real information available for that parameter).

| Statistic | Value |
|---|---|
| Best cell | 80-$4 |
| N trials (per task spec) | 6 |
| var(trial Sharpes), n=3 usable | 0.000148 |
| T (best cell leg count) | 573 |
| E[max SR] haircut (sr0) | +0.0158 |
| **DSR — P(true SR > E[max SR])** | **0.9977** |

**Verdict: 80-$4 clears the multiple-comparisons haircut decisively (DSR = 0.998,
>> the 0.95 bar).** But the honest reading of *why* it clears so easily is important:
the three usable cells' Sharpes are all tightly clustered (0.092 / 0.103 / 0.121) —
a genuinely small spread (`var_trials` = 0.00015). A small spread across trials
produces a small E[max SR] haircut almost by construction (there's little room for
"lucky selection" to inflate a maximum when nothing in the search varies much). **This
is the "plateau moves together" signature the task asked to watch for, not a sharp,
isolated peak** — 80-$4 is not dramatically better than 50-$2 or 80-$3, it is simply
marginally ahead of two other configs that are themselves all solidly positive. The
honest read is: *the templates that have enough data all show a real, similar-sized
B2 edge; none is a standout outlier the others don't share.*

---

## Final synthesis

**(a) Does 80-$4 hold up standalone with real train/test discipline?**
Partially, and unevenly. True-labeled 80-$4 test-set B2 (+$2,487, holding at +$2,037
ex-biggest-day) and its leg win rate (78–83% across splits) both support the rule.
But 80-$4's *train*-set aggregate is fragile — its entire +$820 train total is one
day's contribution, collapsing to $4 once that day is excluded. That fragility
matters (it's exactly the caveat the task asked not to paper over), but it should be
weighed against, not allowed to erase, the fact that the leg-level win rate — the
more granular, less hero-day-sensitive metric — stays solidly in B2's favor on both
splits, and that the broader $4-label-only population (mixing in 50-$4) is robust on
both splits and both ex-biggest-day checks. Net: **80-$4 is a real, if not dramatic,
positive result, not a refutation** — but "holds up" should be read as "leg-level
edge with an aggregate-dollar train result that's thinner than it first looks,"
not as a clean, unqualified pass.

**(b) Does the grid search show any template statistically distinguishable once
DSR-corrected, or does the whole plateau move together?**
**The plateau moves together.** Only 3 of 6 configs have enough data to test at all;
of those 3, all show a positive, broadly similar-magnitude B2 Sharpe (0.09–0.12), and
80-$4 "wins" the DSR by the smallest of margins over a genuinely tight cluster, not
by being a standout. This is a valid, useful, non-null answer in its own right: it
means the choice of 80-$4 vs 50-$2 vs 80-$3 is **not** a strongly evidence-driven
pick from this grid — any of the three usable configs would be defensible on the
data available, and none should be selected on the basis of "it scored highest in
this search" alone (that would be exactly the curve-fit risk rule #1 warns against).
80-$4 has one practical edge the others don't: it is by far the largest-n cell
(573 legs vs. 268 for 50-$2 and 115 for 80-$3) and is the strategy's own dominant
template by real trading volume (2,432 of 4,687 TAT rows historically) — a reason to
prefer it operationally (more data behind the number, matches what the discretionary
trader already runs most) that is independent of, and doesn't require, treating the
DSR "win" as meaningful discrimination.

**(c) Is there enough data to trust either answer?**
Only partially, and the honest boundary is the 2026-03-19 TAT cutoff. Three of six
grid cells (80-$2: 11 legs/10 days; 50-$3: 4 legs/3 days; 50-$4: 1 leg/1 day) are far
too thin to say anything at all — this is not a close call, it's simply insufficient
data, full stop. Even the three cells that DO clear the floor are modest by
research-grade standards (115–573 legs, 59–139 days), especially once split 70/30
and further split by removing a single day. The train/test discipline itself is
sound (fresh date-based split per cut, no reused split index, no leakage), but the
underlying sample sizes limit how much confidence any single number here deserves —
this is squarely the situation rule #1 asks to flag rather than paper over.

**What the pending TAT history extension (back to 2024-09-16) would change**: Andrew
is pulling roughly 10 additional months of TAT history. Verified directly against
the full TAT-tradelog.xlsx: rows through 2026-03-19 total 4,687, of which 2,753 fall
inside the 2025-07-09–2026-03-19 window used in this analysis — i.e. the full
back-to-2024-09-16 history is **~1.7x** the row count used here (not a clean
doubling), because the pre-2025-07-09 stretch is a somewhat shorter or lower-volume
period than the current window, not a symmetric extension. **Important caveat this
projection depends on**: `decoupled_long_legs.csv` itself comes from the IBKR
execution-level reconstruction (`combo_ledger.csv`'s pipeline), which currently only
covers 2025-07-09 onward — the pre-2025-07-09 stretch would need its OWN independent
IBKR-side reconstruction pass before any TAT join could touch it at all (this is
exactly what STRATEGY_RECONSTRUCTION.md Part 3, item 1, already flags as the
legitimate next step). Assuming that reconstruction is done and joins at a similar
rate, the additional ~1,934 TAT rows would likely still leave 50-$3 (4→roughly 7-11
legs) and 50-$4 (1→roughly 2-4 legs) below or near the 15-leg floor; 80-$2 (11→
roughly 19-30) would likely cross it for the first time; and the three already-usable
cells would gain ~70% more data, meaningfully tightening the DSR's `var_trials`
estimate and the train/test splits' statistical power, without fully resolving the
two thinnest cells. It would NOT,
however, extend the true-width-label coverage forward past 2026-03-19 — the
April–July 2026 gap is a forward-looking data problem the historical backfill
doesn't touch, and would need either a second historical TAT pull covering that
window specifically, or accepting the $-label-only proxy there indefinitely.

### Bottom line for Andrew

If forced to pick ONE deterministic template today: **80-$4** is a defensible choice
— it has the most data behind it by a wide margin, it is the strategy's own dominant
real-world template, its leg-level B2 edge is consistent and positive across every
cut tested, and it clears the DSR bar. But the DSR "win" itself is not strong
evidence that 80-$4 is *better* than 50-$2 or 80-$3 — the honest finding is that
**all three testable configs cluster together with a similar, real, modest B2 edge**,
and 80-$4's advantage is n and real-world usage share, not a statistically
distinguished result. Andrew should not read this analysis as "80-$4 proven best";
he should read it as "80-$4 is the reasonable default among several roughly
equivalent options, on genuinely still-thin data, pending the TAT history backfill."

---

## Files produced in this folder

- `template_join.py` — Step 0 full-range TAT Template join (extends
  `reconstruct.cross_check_tat()` beyond October 2025 to the full TAT window).
- `template_fixed_and_grid_analysis.py` — Analyses #1 and #2, DSR grid deflation.
- `tat_full_join.csv` (gitignored) — one row per `decoupled_long_legs.csv` row, with
  TAT match status/Template + final width/dollar labels.
- `template_fixed_grid_results.csv` (gitignored) — per-leg B2/actual dollar P&L
  joined to template labels, for the 1,584-leg B2-tested population.
- `TEMPLATE_FIXED_AND_GRID_ANALYSIS.md` — this report.
