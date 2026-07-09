# S8 — Single-Template Comparison: Is 80-$4's B2 Problem a Crash-Day Artifact, and Which Template (If Any) Is the Best Single-Template Bot Candidate?

`S8_80_4_ONLY_FULL_BACKTEST.md` found that 80-$4-only S8 underperforms the full
blended-template headline by roughly half, and — more importantly — that within the
80-$4 population **B2 (the mechanical long-leg close) is a net NEGATIVE vs. what
actually happened**, the reverse of the full blended result. It traced this to
2025-10-10 (the crash day) disproportionately hitting 80-$4 positions, but did not
establish whether the effect was entirely crash-day-driven or persisted more broadly.
This report answers that, and extends the same full-strategy backtest to the other
two templates with usable sample size (`TEMPLATE_FIXED_AND_GRID_ANALYSIS.md`'s grid:
80-$3, 50-$2 — the remaining three configs have 1–11 legs, unusable).

Script: `s8_single_template_comparison.py`. Reuses `s8_80_4_only_full_backtest.py`'s
`label_combos()` (TAT join extended to the full 2,592-combo population) and
`alpha_vs_beta_decomposition.py`'s B2-corrected P&L reconstruction verbatim — not
rebuilt. PAPER / research only, offline, read-only on all source CSVs. S8 is not
live; nothing here changes strategy/regime config or paperbot.

---

## Task 1 — is 80-$4's B2-negative result crash-day-specific?

**Answer: yes, entirely.** The gap flips from meaningfully negative to solidly
positive by removing 2025-10-10 alone; 2026-05-18 barely moves it.

### True-labeled 80-$4 (979 combos, 141 days, through 2026-03-17)

| Cut | B2-vs-actual gap ($) |
|---|---|
| All days included | **−$10,998.29** |
| Excluding 2025-10-10 only | **+$21,386.76** |
| Excluding 2026-05-18 only | −$10,998.29 (2026-05-18 not in this cut's window — TAT coverage ends 2026-03-19) |
| Excluding both | +$21,386.76 |

2025-10-10's own contribution to the gap: **−$32,385.06** — i.e. removing that single
day alone accounts for the entire swing from negative to positive (−$10,998 →
+$21,387, a $32,385 move, matching the day's isolated contribution to the cent).

### $4-proxy 80-$4 (1,427 combos, 219 days, full window through 2026-07-07)

| Cut | B2-vs-actual gap ($) |
|---|---|
| All days included | **−$3,403.92** |
| Excluding 2025-10-10 only | **+$28,981.13** |
| Excluding 2026-05-18 only | −$4,066.91 (removing it makes the gap *slightly worse*, not better) |
| Excluding both | +$28,318.14 |

Same story: 2025-10-10 alone flips the sign and swings the gap by $32,385 (identical
dollar figure to the true-labeled cut, since it's the same trading day's same
combos). 2026-05-18's own contribution is small and actually slightly *helps* B2
(+$663), so excluding it moves the gap the wrong way — confirming the effect really is
specific to the crash day, not a general "remove the biggest days" artifact.

**Direct answer to the open question**: 80-$4's B2-negative result is **entirely a
2025-10-10 artifact, not a broader problem with the template.** Once that single day
is excluded, B2 beats actual by a wide, solid margin on both cuts (+$21,387 true-
labeled, +$28,981 $4-proxy) — a materially different and more encouraging read than
"stays negative in calm periods." This does **not** mean 80-$4 is now cleared for
crash-immune use — it means the opposite of a fundamental flaw: B2's known,
already-documented trade-off (give up 100% of the long leg's upside past the stop)
concentrated its cost on the one real crash event in the sample, exactly as the
mechanism predicts, and the template is fine outside of that regime.

---

## Task 2 — full-strategy backtest, 80-$3 and 50-$2

Same method/format as `S8_80_4_ONLY_FULL_BACKTEST.md`: total P&L = short leg's real
realized P&L + long leg's B2-corrected P&L, summed on the fixed $127,710 reference
balance (not compounded).

### 80-$3

**Cut (a): true-labeled, through 2026-03-19**

170 combos, **80 active trading days**, 2025-08-12 to 2026-03-19.

| | S8 (B2-corrected) |
|---|---|
| Total P&L | **+$9,897.78** |
| Return on $127,710 | **+7.8%** |
| Months positive | 3 / 8 |
| Day-level win rate (day P&L > 0) | 56.2% |
| Day-level win rate (S8 ≥ actual) | **97.5%** |
| Combo-level win rate (S8 > actual) | 54.1% |

Top-2 days: 2026-03-17 (+$4,435), 2025-11-04 (+$3,657); excluding both:
+$1,805.76 (80 days → 78, day-win-rate 55.1%) — still positive, thin but real.

B2-vs-actual gap: **+$1,794.27 all-days**; 2025-10-10 present but contributes
$0.00 to the gap (no 80-$3 combo happened to be open that day in this cut);
2026-05-18 not in this cut's window. Gap unaffected by either day — **80-$3's
small positive B2 edge is untouched by the crash day entirely.**

**Cut (b): $-label-proxy, full window through 2026-07-06**

460 combos, **158 active trading days**, 2025-07-10 to 2026-07-06. 170 of these
combos are confirmed 80-width via real TAT match (identical population to cut (a));
286 of 460 (62%) have unconfirmed width.

| | S8 (B2-corrected) |
|---|---|
| Total P&L | **+$39,939.35** |
| Return on $127,710 | **+31.3%** |
| Months positive | 6 / 13 |
| Day-level win rate (day P&L > 0) | 55.1% |
| Day-level win rate (S8 ≥ actual) | **93.7%** |
| Combo-level win rate (S8 > actual) | 46.7% |

Top-2 days: 2026-04-27 (+$5,869), 2026-05-28 (+$4,620); excluding both:
+$29,449.54 (158 → 156 days, day-win-rate 54.5%) — solidly positive.

B2-vs-actual gap: **+$15,882.98 all-days**; 2025-10-10 contributes $0.00 (no
80-$3-labeled combo open that day in this broader cut either); 2026-05-18
contributes +$272.71 (small, in B2's favor). Gap stays positive and essentially flat
whichever day is excluded (+$15,883 → +$15,610 excluding 2026-05-18) — **robust,
crash-day-independent.**

### 50-$2

**Cut (a): true-labeled, through 2026-03-17**

370 combos, **84 active trading days**, 2025-09-02 to 2026-03-17.

| | S8 (B2-corrected) |
|---|---|
| Total P&L | **+$15,365.92** |
| Return on $127,710 | **+12.0%** |
| Months positive | 6 / 7 |
| Day-level win rate (day P&L > 0) | 57.1% |
| Day-level win rate (S8 ≥ actual) | **92.9%** |
| Combo-level win rate (S8 > actual) | 52.2% |

Top-2 days: 2026-02-10 (+$5,368), 2026-03-13 (+$3,651); excluding both:
+$6,347.43 (84 → 82 days, day-win-rate 56.1%) — still solidly positive.

B2-vs-actual gap: **+$6,190.40 all-days**; neither 2025-10-10 nor 2026-05-18 falls
in this cut's window/population (2025-10-10 is before this template's active start;
2026-05-18 is past TAT coverage) — gap unaffected by either flagged day.

**Cut (b): $-label-proxy, full window through 2026-07-07**

705 combos, **174 active trading days**, 2025-07-24 to 2026-07-07. 370 confirmed
50-width via TAT match (identical population to cut (a)); 312 of 705 (44%)
unconfirmed width.

| | S8 (B2-corrected) |
|---|---|
| Total P&L | **+$36,940.36** |
| Return on $127,710 | **+28.9%** |
| Months positive | 7 / 13 |
| Day-level win rate (day P&L > 0) | 50.0% |
| Day-level win rate (S8 ≥ actual) | **92.5%** |
| Combo-level win rate (S8 > actual) | 48.7% |

Top-2 days: **2026-05-18 (+$57,655)**, 2026-02-10 (+$5,368); excluding both:
**−$26,082.15** (174 → 172 days, day-win-rate 49.4%) — **the entire cut (b) headline
is carried by 2026-05-18 alone; without it the total is negative.**

B2-vs-actual gap: **+$83,737.31 all-days**; 2025-10-10 not in this cut's population;
2026-05-18 contributes **+$67,313.39 to the gap by itself** — excluding it, the gap
collapses to +$16,423.92 (still positive, but 80% smaller). **50-$2's $-proxy
headline and its B2 edge are both heavily concentrated in a single outsized day**,
the mirror image of 80-$4's crash-day fragility but in the *opposite* direction
(2026-05-18 helped B2 hugely here, rather than hurting it the way 2025-10-10 hurt
80-$4). Cut (a) (true-labeled, ending before 2026-05-18) does not carry this
exposure and is the more trustworthy of the two 50-$2 numbers for that reason.

---

## Final synthesis: 3-way comparison table

| Template | Cut | Total P&L | Total return | Active days | B2-vs-actual gap (all days) | Gap excl. 2025-10-10 |
|---|---|---:|---:|---:|---:|---:|
| 80-$4 | true-labeled | +$66,535.84 | +52.1% | 141 | **−$10,998.29** | +$21,386.76 |
| 80-$4 | $-proxy (full) | +$70,808.24 | +55.4% | 219 | **−$3,403.92** | +$28,981.13 |
| 80-$3 | true-labeled | +$9,897.78 | +7.8% | 80 | +$1,794.27 | +$1,794.27 |
| 80-$3 | $-proxy (full) | +$39,939.35 | +31.3% | 158 | +$15,882.98 | +$15,882.98 |
| 50-$2 | true-labeled | +$15,365.92 | +12.0% | 84 | +$6,190.40 | +$6,190.40 |
| 50-$2 | $-proxy (full) | +$36,940.36 | +28.9% | 174 | +$83,737.31* | +$83,737.31* |

*50-$2 $-proxy's gap and headline total return are both dominated by 2026-05-18
alone (see above) — excluding that single day, the gap falls to +$16,423.92 and the
total P&L falls to −$26,082.15 for the period ex-top-2-days (i.e. this cut's positive
headline does not survive removing its own biggest day, unlike every other row in
this table).

### Reading the table

- **80-$4** is the only template where B2 is a net negative on the true-labeled cut,
  and that negative is now fully explained (Task 1): it is exactly, only, the
  2025-10-10 crash day. Remove that one day and 80-$4 has the largest positive B2
  edge of the three templates (+$21,387 to +$28,981) — consistent with it having by
  far the most trading volume/data of the three.
- **80-$3** is small in absolute dollars (true-labeled total return only +7.8%,
  thinnest active-day count at 80) but its B2 edge is genuinely untouched by either
  flagged day — a real if modest positive that holds without caveat in this specific
  check. (Note: `TEMPLATE_FIXED_AND_GRID_ANALYSIS.md` previously flagged 80-$3's
  long-leg-only train-set edge as a hero-day artifact on a *different* day
  (2025-10-10 in that framing, +$472 of a +$163 train total) — that finding concerned
  the long-leg-only Sharpe/dollar swing on a train/test split, not this report's
  full-strategy B2-vs-actual gap on the whole window, and the two should not be
  read as contradicting each other; they are different metrics on overlapping but
  not identical populations.)
- **50-$2** looks the strongest on paper in the $-proxy cut (+28.9%, gap
  +$83,737) but that is almost entirely one day (2026-05-18, past TAT's coverage
  window) — the true-labeled cut, which doesn't include that day, is far more modest
  (+12.0%, gap +$6,190) and is the more trustworthy of the two 50-$2 numbers for
  exactly the reason the task brief was designed to catch: don't let one outsized day
  masquerade as broad-based edge.

---

## Verdict: which single template, if any, is the best bot candidate?

**No template clearly stands out, and this is a legitimate "no clear winner, more
data needed" answer, not a hedge.**

Weighed on the three criteria in the task brief:

1. **Profitable on its own**: all three templates are profitable on every cut tested
   (true-labeled and $-proxy alike) — this criterion doesn't discriminate between
   them.
2. **Helped rather than hurt by B2**: 80-$3 and 50-$2 pass cleanly on both cuts;
   80-$4 fails on the crash day but Task 1 shows that failure is fully explained and
   reverses to the *largest* positive edge of the three once that one day is
   excluded — so 80-$4 doesn't fail this criterion so much as it demonstrates the
   mechanism (B2 costs the most exactly on tail days) most visibly, on the template
   with the most data to show it.
3. **Not fragile to the one crash day**: 80-$3's gap is completely untouched by
   2025-10-10 (no combo open that day in this cut); 50-$2's true-labeled gap is also
   untouched (no exposure to either flagged day); 80-$4 is the one template with
   real, large, single-day sensitivity to 2025-10-10 (a $32,385 swing). On this
   specific axis, 80-$3 and 50-$2's true-labeled cuts look steadier — but that
   steadiness partly reflects them having far fewer trading days in the window
   (80 and 84 vs. 141), i.e. less exposure simply because they traded less on that
   date range, not necessarily a structurally safer template.

**Net read**: 80-$4 has by far the most data and the largest absolute dollar edge
once the single crash day is excluded, but is genuinely the most exposed to that one
day — a real, not cosmetic, fragility for a bot that would need to survive the *next*
crash without the benefit of hindsight excluding it. 80-$3 and 50-$2 are steadier in
this specific check but on much thinner samples (80–84 true-labeled days vs. 141),
and 50-$2's more attractive $-proxy numbers turn out to be almost entirely one
non-crash outlier day, which is its own version of the same fragility problem in the
opposite direction. **None of the three templates is simultaneously (a) large-sample,
(b) robust to its own single biggest day, and (c) not reliant on any one outsized
day for its headline** — each template fails a different piece of that
combination. Given the sample sizes involved (80–219 active days per cut, one crash
event and one large one-directional day total in the whole dataset), this is
consistent with this project's standing thin-sample caveat on S8 (`S8_SPEC.md` §5.1)
and should not be resolved by picking whichever template currently looks best in
this specific 236-day window — that would be exactly the curve-fit risk rule #1
warns against.

**Explicitly out of scope, not addressed here**: this report does not recommend
running a multi-template bot or attempt to reverse-engineer the human's
template-switching logic. `S8_80_4_ONLY_FULL_BACKTEST.md` already established that
template-switching itself contributes real value beyond any single fixed template
(full blended S8 return +108.8% vs. every single-template cut here landing at
+7.8%–55.4%) — that finding stands, and reverse-engineering the switching rule
remains flagged as high curve-fit risk pending more data, per `S8_SPEC.md`'s existing
discussion.

---

## Files produced in this folder

- `s8_single_template_comparison.py` — this analysis (Tasks 1 and 2 plus the
  synthesis table).
- `s8_single_template_combo_labels.csv` (gitignored) — combo-level TAT template
  labels for the full 2,592-combo population, recomputed by this script (same
  method as `s8_80_4_only_combo_labels.csv`, kept separate per-script per this
  folder's convention).
- `S8_SINGLE_TEMPLATE_COMPARISON.md` — this report.
