# 0DTE Iron-Condor PURE HOLD-TO-CASH-SETTLEMENT (ARM 5) -- finished-window report

_Generated 2026-07-06. Window 2022-01-03 -> 2026-07-01, 1127 session-days (1087 traded, 40 no-trade/skip, 0 crash-skipped). PAPER / research only._

Pre-registration: `docs/PREREG_condor_reopen_2026-07-06.md` (Arm 5, the hold-to-settlement card noted in `output/condor_width_sweep_20260706.md`). Entry chassis frozen from the control (14:00 entry, 0.15-delta shorts, honest 4-leg ENTRY fills, $0.30 min-credit floor). Width ladder = Arm 1's **5 (control) / 10 / 20 / 30 / 50-pt**.

## 0. The mechanism under test (stated prominently)

**Management is NONE.** No profit target, no stop, no early close. Every position is held to 16:00 and resolved at COSTLESS CASH INTRINSIC against the recovered 16:00 index level S* -- SPXW 0DTE options are European and cash-settled, so **ZERO exit bid/ask spread is crossed**. Put-side loss = min(max(K_short_put - S*, 0), width); call-side loss = min(max(S* - K_short_call, 0), width); settle P&L = entry_credit - losses. The ENTRY credit still uses the honest fill band (mid/f25/f50/full). This is the single thing that distinguishes Arm 5 from the prior 'hold-to-settle' A_hold arm, which closed at the last quoted minute's full 4-leg bid/ask debit (a real exit spread) and lost -$32,905 at full fill on 5pt.

## 1. Total P&L ($) by WIDTH x ENTRY-FILL FRACTION -- OVERALL

`mid`=0% (optimistic entry), `f25`=25%, `f50`=50% (**HEADLINE**), `full`=100% worst-side (the control's honest entry bound). The EXIT is costless cash settlement at every fill -- only the entry credit moves with the fraction.

| width | mid | f25 | f50 | full |
| --- | --- | --- | --- | --- |
| w5 | 9,363 | 4,956 | 550 | -8,262 |
| w10 | 19,225 | 14,614 | 10,004 | 783 |
| w20 | 28,013 | 23,141 | 18,269 | 8,525 |
| w30 | 38,993 | 34,206 | 29,420 | 19,848 |
| w50 | 46,123 | 41,571 | 37,019 | 27,915 |

### TRAIN (2022-01 .. 2024-06)

| width | mid | f25 | f50 | full |
| --- | --- | --- | --- | --- |
| w5 | 6,283 | 3,737 | 1,191 | -3,900 |
| w10 | 10,438 | 7,706 | 4,975 | -487 |
| w20 | 15,223 | 12,329 | 9,436 | 3,650 |
| w30 | 21,015 | 18,151 | 15,286 | 9,558 |
| w50 | 23,075 | 20,374 | 17,674 | 12,273 |

### TEST / OOS (2024-07 .. end)

| width | mid | f25 | f50 | full |
| --- | --- | --- | --- | --- |
| w5 | 3,080 | 1,219 | -641 | -4,362 |
| w10 | 8,788 | 6,908 | 5,029 | 1,270 |
| w20 | 12,790 | 10,811 | 8,833 | 4,875 |
| w30 | 17,978 | 16,056 | 14,134 | 10,290 |
| w50 | 23,048 | 21,196 | 19,345 | 15,643 |

## 2. TAIL IS FIRST-CLASS -- per-width stats at the HEADLINE f50 fill

No management => breach risk is the whole story. `breach_rate` = fraction of days the index settled beyond a short strike; `avg_breach_loss_$` = mean P&L on those days (credit minus capped intrinsic).

| width | trades | total_$ | win_rate | avg_$ | worst_day_$ | p01_$ | p05_$ | std_$ | sharpe_ann | sortino_ann | breach_rate | n_breach | avg_breach_loss_$ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| w5 | 1,061.000 | 550.000 | 0.791 | 0.520 | -455.000 | -437.500 | -412.500 | 182.630 | 0.045 | 0.023 | 0.237 | 251.000 | -290.380 |
| w10 | 1,079.000 | 10,003.750 | 0.803 | 9.270 | -923.750 | -890.550 | -823.750 | 314.890 | 0.467 | 0.231 | 0.236 | 255.000 | -453.790 |
| w20 | 1,083.000 | 18,268.750 | 0.817 | 16.870 | -1,892.500 | -1,799.880 | -1,371.750 | 498.790 | 0.537 | 0.252 | 0.236 | 256.000 | -638.790 |
| w30 | 1,083.000 | 29,420.000 | 0.823 | 27.170 | -2,840.000 | -2,654.880 | -1,336.750 | 597.660 | 0.722 | 0.332 | 0.236 | 256.000 | -706.140 |
| w50 | 1,079.000 | 37,018.750 | 0.826 | 34.310 | -4,840.000 | -3,978.230 | -1,324.500 | 726.690 | 0.749 | 0.339 | 0.237 | 256.000 | -772.160 |

## 3. Per-year total P&L per width (headline f50 fill)

The tail test: is a positive total driven by calm years while a single breach year is catastrophic? A calm-carry-plus-catastrophe shape is NOT adoptable.

| width | 2022 | 2023 | 2024 | 2025 | 2026 |
| --- | --- | --- | --- | --- | --- |
| w5 | 1,164 | -880 | 371 | 668 | -772 |
| w10 | 2,240 | 1,564 | 1,850 | 4,284 | 66 |
| w20 | 3,019 | 3,692 | 2,314 | 10,844 | -1,600 |
| w30 | 7,091 | 4,750 | 2,541 | 15,970 | -932 |
| w50 | 9,846 | 5,539 | 1,771 | 21,784 | -1,921 |

## 4. Per-regime total P&L per width (headline f50 fill)

### by gamma_regime

| width | negative | neutral | positive |
| --- | --- | --- | --- |
| w5 | -1,667 | -2,330 | 4,548 |
| w10 | -264 | -2,251 | 12,519 |
| w20 | 4,393 | -3,859 | 17,735 |
| w30 | 12,013 | -5,205 | 22,613 |
| w50 | 16,115 | -5,471 | 26,375 |

### by vix_regime

| width | backwardation | contango |
| --- | --- | --- |
| w5 | 1,129 | -579 |
| w10 | 4,908 | 5,096 |
| w20 | 10,525 | 7,744 |
| w30 | 19,561 | 9,859 |
| w50 | 30,138 | 6,881 |

## 5. Head-to-head vs Arm 1 (pt25-managed) at f50, per width

Does removing the exit spread (pure cash settle, this arm) actually beat MANAGING the same condor (Arm 1's 25%-profit-target + 2x stop, which crosses the exit spread)? Both at the f50 headline fill.

| width | arm5_cashsettle_$ | arm1_pt25_managed_$ | arm5_minus_arm1_$ |
| --- | --- | --- | --- |
| w5 | 550 | -13,026 | 13,576 |
| w10 | 10,004 | -10,148 | 20,152 |
| w20 | 18,269 | -2,503 | 20,772 |
| w30 | 29,420 | -3,690 | 33,110 |
| w50 | 37,019 | -2,146 | 39,165 |

## 6. Matched random-day-out PLACEBO (headline f50 fill)

The exit is deterministic (cash settlement), so the apt placebo is random PARTICIPATION: trade a random subset of the SAME days and total the book. If the arm's full-participation total is not in the top 5% tail, being in the market -- not the structure -- is the source. Run only for widths net-positive overall at f50.

| width | arm_total_$ | placebo_p50_$ | placebo_p95_$ | frac_placebo_ge_arm | arm_beats_placebo |
| --- | --- | --- | --- | --- | --- |
| w5 | 550 | 441.2 | 4,450 | 0.4732 | False |
| w10 | 1e+04 | 8,019 | 1.439e+04 | 0.3084 | False |
| w20 | 1.827e+04 | 1.448e+04 | 2.458e+04 | 0.2628 | False |
| w30 | 2.942e+04 | 2.276e+04 | 3.529e+04 | 0.2024 | False |
| w50 | 3.702e+04 | 2.935e+04 | 4.44e+04 | 0.208 | False |

## 7. VERDICT

**f50 total P&L by width:** w5=$550, w10=$10,004, w20=$18,269, w30=$29,420, w50=$37,019.

**(a) Positive plateau across >=3 ADJACENT widths at f50?** Longest adjacent-positive run = 5 width(s) -> YES.

**(b) OOS-stable (positive in the TEST half)?** f50 test-half by width: w5=$-641, w10=$5,029, w20=$8,833, w30=$14,134, w50=$19,345.

**(c) Survivable tail?** See Section 2 (worst-day / p01 / breach loss) and Section 3 (per-year). A positive total that is a calm-carry-plus-catastrophe shape does NOT count as survivable.

**(d) Beats matched random-day-out placebo?** Widths clearing the 5% bar: NONE.

**Widths clearing f50-positive AND OOS-positive AND placebo:** NONE.


> **VERDICT: REFUTED.** No positive plateau of >=3 adjacent widths that is also OOS-stable and beats the matched placebo. Removing the exit spread via pure cash settlement does not, on its own, rescue the 0DTE condor at a realistic (f50) entry fill. See the tail sections for whether any positive cell is merely calm-carry masking breach catastrophe.
