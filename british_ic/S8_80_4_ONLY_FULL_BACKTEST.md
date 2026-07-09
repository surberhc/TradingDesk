# S8 — 80-$4-Only, Full Strategy Backtest ("If the Bot Only Ran This One Template")

`TEMPLATE_FIXED_AND_GRID_ANALYSIS.md` isolated 80-$4's **long-leg-only** B2 edge (the B2
rule vs. discretionary close, measured only on the long leg's own dollar swing). That is
not the same question as this one. Here: **if the account had traded ONLY the 80-$4
template (Puts and Calls) for the entire window, instead of the real day-to-day template
mix, what would TOTAL S8 performance (short leg's real realized P&L + long leg under the
B2 rule) have looked like** — in the same headline shape as `docs/S8_SPEC.md` §4.

Script: `s8_80_4_only_full_backtest.py`. PAPER / research only, offline, read-only on all
source CSVs. S8 is not live; nothing here changes strategy/regime config or paperbot.

---

## Method (reused, not rebuilt)

1. **Template labeling extended to the FULL combo population.** `tat_full_join.csv`
   (from `template_join.py`) only covers `decoupled_long_legs.csv`'s 1,617 rows — combos
   where the long leg closed *separately* from the short (`closed_together == False`,
   1,317 of `combo_ledger.csv`'s 2,592 combos). The other 1,275 combos
   (`closed_together == True`, long closed with the short — no B2 correction needed,
   actual already equals S8 for those) never appear in that file at all and would be
   silently dropped from an 80-$4-only cut if only the existing join were reused as-is.
   This script therefore reruns the **identical join logic** (same key: TradeDate +
   ComboType + exact short/long strike match, nearest-OpenTime tiebreak,
   `AMBIGUOUS_MULTI_CANDIDATE` kept not dropped, same `$2.55`/`$3.55` proxy cutpoints)
   on `reconstruct.explode_combo_groups_to_pairs()`'s exploded view of **all 2,592**
   combos, then rolls the resulting label back up to combo grain
   (`short_conid`+`short_open_dt`, the same combo-level key
   `alpha_vs_beta_decomposition.py` already uses).
2. **Full-strategy P&L reused verbatim** from
   `alpha_vs_beta_decomposition.load_b2_corrected_ledger()`: `pnl_actual` = real
   `total_realized_pnl` as traded; `pnl_s8` = `pnl_actual` + the B2 long-leg correction
   from `longleg_rule_backtest_results.csv` (uncovered legs — the final 3 trading days,
   no 1-min SPXW warehouse coverage yet — kept at actual, exactly as the original
   analysis does). **Verified before trusting anything downstream**: this script
   reproduces the S8_SPEC.md headline total ($138,982) to within $1 before any
   filtering is applied.
3. Combo-ledger population filtered to 80-$4 combos, two cuts, then summed daily/monthly
   exactly as `docs/S8_SPEC.md` §4 reports (fixed $127,710 reference balance, not
   compounded).

---

## Headline results

### Cut (a): TRUE-LABELED 80-$4 (real TAT Template match, through 2026-03-17)

979 combos, **141 active trading days**, 2025-07-09 to 2026-03-17.

| | Actual (as-traded) | S8 (B2-corrected) |
|---|---|---|
| Total P&L | **+$77,534** | **+$66,536** |
| Return on $127,710 reference balance | **+60.7%** | **+52.1%** |
| Months positive (of 9) | — | 7 |
| Day-level win rate (day P&L > 0) | — | 59.6% |
| Day-level win rate (S8 day total ≥ actual day total) | — | **84.4%** |
| Combo-level win rate (S8 combo total > actual combo total) | — | 43.9%* |

*The combo-level number looks low next to the 84–87% figures elsewhere in this project
because it's a strict `>` on combo totals that are frequently near-zero or tied (many
80-$4 combos close for a small loss under both actual and S8, producing many
non-strict-improvement combos that aren't real losses for S8 either). The day-level
"S8 ≥ actual" rate (84.4%) is the more informative, less noise-sensitive figure and lines
up with this project's other B2 findings (83–87%).

### Cut (b): $4-LABEL-PROXY, full window through 2026-07-07 (width NOT confirmed — mixes 80-$4 and 50-$4)

1,427 combos, **219 active trading days**, 2025-07-09 to 2026-07-07.

| | Actual (as-traded) | S8 (B2-corrected) |
|---|---|---|
| Total P&L | **+$74,212** | **+$70,808** |
| Return on $127,710 reference balance | **+58.1%** | **+55.4%** |
| Months positive (of 13) | — | 8 |
| Day-level win rate (day P&L > 0) | — | 55.7% |
| Day-level win rate (S8 day total ≥ actual day total) | — | 83.1% |
| Combo-level win rate (S8 combo total > actual combo total) | — | 44.4% |

**Width-confirmation breakdown within cut (b)**: of the 1,427 combos, 979 are
confirmed 80-width via a real TAT match (identical population to cut (a)), 2 are
confirmed 50-width, and **446 (31%) have an UNCONFIRMED width** — past TAT's
2026-03-19 coverage or no TAT match at all, carrying only the `$`-label proxy. Do not
read cut (b) as "80-$4 extended through July" — it is genuinely a mixed-width, mostly-
$4-credit-target population for the April–July 2026 stretch.

---

## Direct comparison to the full blended S8 headline (+$138,982 / +108.8%)

**Both 80-$4-only cuts underperform the full blended S8 headline substantially, in both
dollar and percentage terms** — roughly half the total return (52–55% vs. 108.8%) on
about 60–90% of the active days. This is the honest, unglamorous answer: **the
80-$4-only restriction would have meant a materially smaller-return strategy than the
real, template-switching British IC account produced**, not a comparable or better one.

**More striking: within the 80-$4-only population specifically, the B2 correction (S8)
does WORSE than what actually happened** (+$66,536 vs. +$77,534 for cut (a); +$70,808
vs. +$74,212 for cut (b)) — the reverse of the full blended population, where S8 beats
actual by a wide margin (+$138,982 vs. +$42,765). This is not a bug; it traces to a
single, already-documented mechanism: **the 2025-10-10 crash day is disproportionately
concentrated in the 80-$4 template**, and B2's core trade-off (giving up 100% of the
long leg's upside past the moment the short stops) costs the most exactly on that kind
of day. Two 80-$4 combos on 2025-10-10 alone account for **+$32,429** of the
actual-beats-S8 gap in cut (a) — the human's discretionary hold on that specific day
outperformed the mechanical B2 close by a wide margin on this template, consistent with
`STRATEGY_RECONSTRUCTION.md` Part 2's own finding that "the 2025-10-10 crash day is
actually *lower* under S8 than what actually happened" and `S8_SPEC.md`'s explicit
caveat that Oct 2025 and Jan 2026 are the clearest months where actual beats S8. This
restricted cut simply concentrates that same known effect rather than diluting it across
other templates the way the blended population does.

**Verdict: the 80-$4-only version is WORSE than the all-templates-blended S8 headline in
total-return terms, on both the S8/B2 basis and the actual-as-traded basis** — the
template-switching itself, not just the B2 correction, is contributing real value to the
blended headline. This is a genuinely different and more negative finding than the
long-leg-only cut in `TEMPLATE_FIXED_AND_GRID_ANALYSIS.md`, which found 80-$4's B2 edge
"real, if not dramatic, positive" — that finding was narrowly about the long leg's own
dollar swing, not the full strategy total, and the two should not be conflated.

---

## Two-largest-single-day robustness check

| Cut | Top-2 days | Sum of top-2 | Total excl. top-2 | Days excl. top-2 | Day-win-rate excl. top-2 |
|---|---|---|---|---|---|
| (a) true-labeled | 2025-12-09 (+$8,507), 2025-08-18 (+$6,399) | +$14,906 | **+$51,630** | 139 | 59.0% |
| (b) $4-proxy | 2025-12-09 (+$8,507), 2026-05-08 (+$7,593) | +$16,099 | **+$54,708** | 217 | 55.3% |

Both cuts stay solidly positive after removing their two largest single-day
contributors (78% and 77% of the headline S8 total survives, respectively) — the
result is a broad day-to-day tilt, not two lucky days. This part of the finding is
robust; what is not robust is the comparison to the full-blend headline or to the
same population's own actual outcome (see above).

---

## Sample-size honesty: active days

| Population | Active trading days |
|---|---|
| Full blended S8 (all templates + unmatched/unclaimed legs) | **236** (matches `S8_SPEC.md`) |
| Cut (a) 80-$4 true-labeled only | **141** (60% of full) |
| Cut (b) $4-proxy only | **219** (93% of full) |

**80-$4-only trading would have meant materially fewer active days, not the same 236**
— 95 fewer days in cut (a) alone. This is not a subtle caveat: the full blended S8
headline benefits from the OTHER templates (80-$3, 50-$2, 80-$2, 50-$3, 50-$4) firing
on the 95 days 80-$4 didn't trade, and those other-template days are not free
diversification the 80-$4-only version would have kept — a bot running only 80-$4
genuinely sits out on more than a third of the days the real account was active.

---

## Bottom line

If forced to run only the 80-$4 template, both honestly-computed cuts land at roughly
**half the full blended S8 headline's total return** (+52% to +55% vs. +108.8%), on
meaningfully fewer active trading days (60–93% of the full 236), and — specific to this
restricted population — **the B2 correction itself is a net negative relative to what
actually happened**, because 80-$4 disproportionately carries the one crash day where
B2's give-up-the-tail trade-off costs the most. None of this refutes B2 as a rule (the
day-level "S8 ≥ actual" win rate, 83–84%, and the two-tail-day robustness check both
hold up fine in isolation) — it says the **template-switching**, not just the B2 exit
correction, is doing real work in the full blended headline, and a single-template bot
would give up a large share of both the return and the B2 rule's own edge.

---

## Files produced in this folder

- `s8_80_4_only_full_backtest.py` — this analysis.
- `s8_80_4_only_combo_labels.csv` (gitignored) — all 2,592 `combo_ledger.csv` combos with
  a combo-level `final_width_label`/`final_dollar_label`/`tat_match`, extending the
  existing decoupled-legs-only join to the full combo population.
- `S8_80_4_ONLY_FULL_BACKTEST.md` — this report.
