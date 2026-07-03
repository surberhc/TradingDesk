# S2/S3 morning-realized-vol signal — three pre-registered arms (2026-07-03)

**Scope.** The prior S2/S3 intraday-condor study (`s2s3_intraday_condor.py`, report
`output/s2s3_intraday_condor_20260701.md`) refuted the morning-GAP gate at the placebo but
surfaced ONE relationship that was materially stronger and stable OOS: **morning realized
vol (09:31–11:00) predicts the post-entry PM range (14:00–16:00), Spearman rho +0.586
(train +0.577 / test +0.563).** This is a single, pre-registered test of whether that
predictiveness converts into **avoided losses** on the SAME losing fixed-0.15-delta 0DTE
iron-condor control, at honest fills, versus a MATCHED RANDOM PLACEBO — the same discipline
that killed the gap gate. Engine: `s2s3_morning_rvol.py`, which REUSES the control / fills /
day-sampling / OOS split / sub-buckets / 3000-draw placebo machinery from
`s2s3_intraday_condor` + `s6_control` + `s6_matrix` UNCHANGED. Apples-to-apples with the
refuted gap gate. Data: SPXW 1-min warehouse, 1060 traded days, OOS split 2024-06-30
(train 576 / test 484). Nothing wired in; candidate research only.

## Bottom line

**All three arms FAIL. The morning-rvol flag does not convert into avoided losses — REFUTED
at the placebo, and again at the sub-bucket plateau.** Consistent with the gap-gate
refutation and the S6 four-way refutation. Nothing here is adoptable.

| Arm | Total P&L | Gain vs control | bar1 beats ctrl | bar2 placebo ≥98% | bar3 plateau | bar4 avoided-losses | VERDICT |
|---|---|---|---|---|---|---|---|
| **Control** (fixed 0.15δ 0DTE iron condor) | **−$32,870** | — | — | — | — | — | Losing yardstick |
| **A — GATE** (sit out flagged days) | −$20,295 | +$12,575 | ✅ | ❌ **87.0%** | ❌ | ✅ | **FAIL** |
| **B — DOWNSIZE** (0.5× on flagged days) | −$26,582 | +$6,288 | ✅ | ❌ **86.9%** | ❌ | ✅ | **FAIL** |
| **C — WIDEN** (0.10δ on flagged days) | −$28,260 | +$4,610 | ✅ | ❌ **97.2%** | ❌ | ✅ | **FAIL** |

Every arm raises a NEGATIVE control toward zero (loss-reduction, not profit — the book is
still deeply negative), and every arm dies at the two bars that separate signal from the
trade-fewer/smaller-on-a-losing-book artifact: **the placebo (#2) and the plateau (#3).**

## The signal (pre-declared, fit on TRAIN half only, applied unseen)

- OLS on the train half: `pred_pm_range = 8.5940 * am_rvol_pct + 0.2131` (slope > 0 →
  monotone, so the top-third of the prediction = top-third of morning vol; the flag is
  robust to the functional form and no form was searched).
- Top-third cutoff = the 67th percentile of the **train-half** predictions only = **0.6628**,
  applied unseen to all days. ONE cutoff, no threshold search.
- **Flagged: 313 traded days** (198 train / 124 test; 322 incl. non-traded).

## Per-arm verdict against all four bar items

### Arm A — GATE (sit out flagged days entirely) — FAIL

- **Bar 1 (beats control): PASS.** Arm −$20,295 vs control −$32,870 → gain **+$12,575**.
- **Bar 2 (beats matched random placebo ≥98% of 3000 draws): FAIL.** The arm beats random
  sit-out of the SAME 313-day count in only **87.0%** of draws (need ≥98%). Random sit-out
  averages **+$9,786** (sd $2,422, p95 $13,915, **p98 $14,830**) — the arm's +$12,575 sits
  *below* the random p95. On a negative-EV book, sitting out any 313 days removes negative-EV
  trades; the morning-vol flag does not pick losers meaningfully better than a coin flip.
- **Bar 3 (both halves + every sub-bucket): FAIL.** Holds in both halves (train +$9,040,
  test +$3,535) but two sub-buckets **flip negative: pos/back −$460, neu/cont −$30.** A
  plateau must hold across ALL day-types.
- **Bar 4 (gain from avoided losses, not trimming): PASS (but moot).** Change on flagged
  losing days +$28,280 vs change on flagged winning days −$15,705 (avoided losses dominate;
  64% of gross change). The mechanic *does* cut more loss-dollars than profit-dollars — but
  it cuts them no better than random selection (bar 2), so this is not evidence of signal.

### Arm B — DOWNSIZE (0.5× size on flagged days) — FAIL

- **Bar 1: PASS.** Arm −$26,582 vs −$32,870 → gain **+$6,288** (exactly half of Arm A's gain,
  as expected from halving instead of zeroing the same flagged days).
- **Bar 2: FAIL.** Beats matched random 0.5×-downsize in only **86.9%** of draws (need ≥98%).
  Random downsize averages +$4,893 (sd $1,211, p95 $6,958, **p98 $7,415**); the arm's +$6,288
  is below the random p95. Same artifact as Arm A, scaled by 0.5.
- **Bar 3: FAIL.** Both halves positive (train +$4,520, test +$1,768) but **pos/back −$230,
  neu/cont −$15** flip negative.
- **Bar 4: PASS (moot).** Loss-day change +$14,140 vs win-day change −$7,853 (avoided losses
  dominate, 64%) — same day set as A, halved.

### Arm C — WIDEN (0.10δ condor on flagged days) — FAIL

- **Bar 1: PASS.** Arm −$28,260 vs −$32,870 → gain **+$4,610**.
- **Bar 2: FAIL (closest of the three).** Beats matched random widen (0.10δ swapped onto a
  random 313-day set drawn from all traded days) in **97.2%** of draws — just short of the
  98% bar (need ≥98%). Random widen averages +$928 (sd $1,792, p95 $4,011, **p98 $4,790**);
  the arm's +$4,610 is between the random p95 and p98. It is the only arm that clears the
  *old* 95% bar — but the 98% bar was raised precisely as the multiple-comparisons guard for
  running three arms, and even at 97.2% the arm still fails, AND (below) fails the plateau in
  THREE buckets. A single arm barely squeaking under a raised bar while the other two fail
  badly is the signature of noise, not signal.
- **Bar 3: FAIL (worst plateau).** Both halves positive (train +$3,990, test +$620) but
  **THREE sub-buckets flip negative: pos/back −$785, neu/cont −$230, neu/back −$145.** Widening
  actively hurts in the pos-gamma/backwardation and both neutral-gamma buckets.
- **Bar 4: PASS (moot).** Loss-day change +$14,520 vs win-day change −$9,910 (avoided losses
  dominate, 59%).

## Both-halves + gamma/VIX sub-bucket tables (delta vs control, $)

`delta_$ = arm total − control total` on the traded days in each cell. Negatives (plateau
breaks) in **bold**.

**Arm A — GATE**

| bucket | n | control_$ | arm_$ | delta_$ |
|---|---|---|---|---|
| ALL | 1060 | −32,870 | −20,295 | +12,575 |
| half=train | 576 | −21,990 | −12,950 | +9,040 |
| half=test | 484 | −10,880 | −7,345 | +3,535 |
| pos/cont | 470 | −11,700 | −9,275 | +2,425 |
| pos/back | 74 | −2,065 | −2,525 | **−460** |
| neg/cont | 242 | −10,075 | −5,980 | +4,095 |
| neg/back | 172 | −7,290 | −945 | +6,345 |
| neu/cont | 86 | −1,795 | −1,825 | **−30** |
| neu/back | 16 | +55 | +255 | +200 |

**Arm B — DOWNSIZE 0.5×**

| bucket | n | control_$ | arm_$ | delta_$ |
|---|---|---|---|---|
| ALL | 1060 | −32,870 | −26,582 | +6,288 |
| half=train | 576 | −21,990 | −17,470 | +4,520 |
| half=test | 484 | −10,880 | −9,112 | +1,768 |
| pos/cont | 470 | −11,700 | −10,487 | +1,213 |
| pos/back | 74 | −2,065 | −2,295 | **−230** |
| neg/cont | 242 | −10,075 | −8,027 | +2,048 |
| neg/back | 172 | −7,290 | −4,117 | +3,173 |
| neu/cont | 86 | −1,795 | −1,810 | **−15** |
| neu/back | 16 | +55 | +155 | +100 |

**Arm C — WIDEN 0.10δ**

| bucket | n | control_$ | arm_$ | delta_$ |
|---|---|---|---|---|
| ALL | 1060 | −32,870 | −28,260 | +4,610 |
| half=train | 576 | −21,990 | −18,000 | +3,990 |
| half=test | 484 | −10,880 | −10,260 | +620 |
| pos/cont | 470 | −11,700 | −11,175 | +525 |
| pos/back | 74 | −2,065 | −2,850 | **−785** |
| neg/cont | 242 | −10,075 | −8,775 | +1,300 |
| neg/back | 172 | −7,290 | −3,345 | +3,945 |
| neu/cont | 86 | −1,795 | −2,025 | **−230** |
| neu/back | 16 | +55 | −90 | **−145** |

Every arm's gain concentrates in the neg-gamma buckets (esp. neg/back) and evaporates or
reverses in pos-gamma/backwardation and neutral — not a plateau.

## Matched-placebo win-rates (3000 draws each, seed 7 — same as the gap-gate placebo)

| Arm | transform | arm gain | random mean | random p95 | random p98 | arm beats random | ≥98%? |
|---|---|---|---|---|---|---|---|
| A GATE | random sit-out, 313 days | +$12,575 | +$9,786 | +$13,915 | +$14,830 | **87.0%** | ❌ |
| B DOWNSIZE | random 0.5× , 313 days | +$6,288 | +$4,893 | +$6,958 | +$7,415 | **86.9%** | ❌ |
| C WIDEN | random 0.10δ, 313 days | +$4,610 | +$928 | +$4,011 | +$4,790 | **97.2%** | ❌ |

The placebo is the decisive test and it refutes all three. A and B's gains fall *below* their
random p95 — the flag is worse than a coin flip at picking which days to skip/shrink. C's
gain sits between random p95 and p98 (the widen transform has a lower random baseline because
widening a random day rarely helps, so its bar is easier to clear on paper) yet it still
fails the raised 98% bar and fails the plateau in three buckets.

## Avoided-losses decomposition (bar #4)

Flagged traded days split by the sign of the CONTROL P&L. `change = arm − control`; a positive
change on a control-losing day = a loss avoided; a negative change on a control-winning day =
a profit forgone.

| Arm | flagged losers (n) | flagged winners (n) | control loss on flagged | control win on flagged | change on losing days (avoided loss) | change on winning days (forgone) | net gain | avoided-losses dominate? |
|---|---|---|---|---|---|---|---|---|
| A | 122 | 191 | −$28,280 | +$15,705 | **+$28,280** | −$15,705 | +$12,575 | Yes (64%) |
| B | 122 | 191 | −$28,280 | +$15,705 | +$14,140 | −$7,853 | +$6,288 | Yes (64%) |
| C | 122 | 191 | −$28,280 | +$15,705 | +$14,520 | −$9,910 | +$4,610 | Yes (59%) |

Bar 4 passes mechanically for all three — the net gain does come from cutting more loss than
profit. **But this is exactly what any indiscriminate exposure-reduction on a losing book
produces**, which is why bar 4 alone is not sufficient: of the 313 flagged days, 191 (61%)
were control WINNERS, so the flag is throwing out profitable days at nearly the base rate. The
placebo (bar 2) is what proves the flag does not beat chance at this — and it does not.

## Why this dies where the raw signal was real

The measurement `am_rvol → pm_range` (rho +0.59) is genuine and stable — morning vol really
does predict the afternoon *range*. But a bigger range is not a bigger *loss*: on this short-
vol condor, the entry credit and short-strike distance are set by the SAME vol that drives the
afternoon range, so high-morning-vol days come in with wider strikes and fatter credit that
largely offset the larger move. High-vol flagged days are only modestly worse than average
(122 losers vs 191 winners among the flagged), so removing/shrinking/widening them helps no
more than removing/shrinking/widening a random 313 days. The predictiveness of *range* does
not convert into avoidable *losses* at honest fills — identical in spirit to the gap-gate
finding, now confirmed for the stronger signal too.

## Anti-look-ahead / anti-curve-fit provenance

- `am_rvol_pct` is from 09:31–11:00 bars, all ≤ the 14:00 entry → causal.
- The OLS line AND the top-third cutoff are fit on the TRAIN half only (days ≤ 2024-06-30)
  and applied unseen to the test half. Pinned by
  `tests/test_s2s3_morning_rvol.py::test_flag_cutoff_is_fit_on_train_only_and_applied_unseen`
  (inflating the test half's morning vol 10× leaves the train-only cutoff unchanged).
- No parameter swept: top-third flag, 0.5× downsize, 0.10 widen delta, 98% placebo bar, and
  the 2024-06-30 split are all pre-declared and FROZEN; pinned by
  `test_frozen_preregistered_constants` so a silent retune fails a test.
- The control, fills, and exit scan are `s6_control`'s own causal engine, unchanged; Arm C's
  0.10-delta re-sim calls `ctrl._build_iron_condor` / `ctrl._scan_exit` verbatim (structural
  agreement, asserted by callable identity in `test_widen_engine_is_the_controls_own_callables`).
- OOS unit = the DAY (1060 samples), sliced by prior-EOD gamma × VIX regime.
- Standing causality guard `tests/test_no_lookahead.py`: **2 passed**.

## Verification

- New arm-mechanics tests `tests/test_s2s3_morning_rvol.py`: **12 passed** (frozen constants,
  train-only fit + unseen cutoff, missing-am_rvol-not-flagged, the three arm transforms,
  plateau flip, avoided-losses decomposition, and placebo firing on a random-noise flag vs
  passing when the flag targets the true losers).
- Full backtester suite: **191 passed** (3 pre-existing FutureWarnings, unrelated).
- Causality guard: **2 passed**.

## Artifacts

- `output/s2s3_research/s2s3_morning_rvol_days.csv` — per-day: control P&L, morning observables,
  `pred_pm_range`, `flag_cutoff`, `flagged`, `widen_pnl` (arm), `widen_pnl_all` (placebo
  denominator), regime, half.
- `C:\TradingDesk-Local\state\s2s3_morning_rvol\arm_c_widen_ALL.csv` — off-Drive cache of the
  0.10-delta P&L for every traded day (the Arm-C re-simulation, idempotent + resumable).

## Single most important takeaway

The one surviving S2/S3 signal (morning rvol → afternoon range, rho +0.59) is a real
*range* predictor but **does not convert into avoided losses on the fixed 0DTE iron condor**
under any of the three pre-registered conditioning mechanics (gate / downsize / widen). All
three fail the matched placebo and the sub-bucket plateau — the same refutation pattern as the
gap gate and the S6 stack. The intraday 0DTE iron-condor line of research is now refuted from
four independent angles (S6 exit-matrix, S2/S3 gap gate, and these three morning-rvol arms).
Park it. Any further variant would be a NEW pre-registered test requiring Andrew's blessing,
not a retune of this one.
