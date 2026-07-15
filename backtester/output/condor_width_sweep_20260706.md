# 0DTE Iron-Condor STRIKE-WIDTH sweep (ARM 1) -- finished-window report

_Generated 2026-07-06. Window 2022-01-03 -> 2026-07-01, 1127 session-days (1087 traded, 40 no-trade/skip, 0 crash-skipped). PAPER / research only._

Pre-registration: `docs/PREREG_condor_reopen_2026-07-06.md`, Arm 1. Entry chassis frozen from the control (14:00 entry, 0.15-delta shorts, 16:00 settlement, honest 4-leg fills). Management fixed = profit-target 25% OR 2x stop OR settle. ONLY the wing width is swept: **5 (control) / 10 / 20 / 30 / 50-pt**.

## 1. Credit collected vs 4-leg spread cost (the crux)

Does widening collect more credit while the bid/ask spread eats a SMALLER fraction of it? `spread_cost_pct_of_credit` = (mid credit - worst-side credit) / mid credit, per day, averaged.

| width | n_days | tot_credit_mid_$ | tot_credit_full_$ | avg_credit_mid_pts | avg_credit_full_pts | avg_spread_cost_pts | spread_cost_pct_of_credit |
| --- | --- | --- | --- | --- | --- | --- | --- |
| w5 | 1,061 | 1.054e+05 | 8.78e+04 | 0.994 | 0.828 | 0.166 | 0.1663 |
| w10 | 1,079 | 1.742e+05 | 1.558e+05 | 1.615 | 1.444 | 0.171 | 0.1039 |
| w20 | 1,083 | 2.482e+05 | 2.287e+05 | 2.292 | 2.112 | 0.18 | 0.0759 |
| w30 | 1,083 | 2.848e+05 | 2.656e+05 | 2.63 | 2.453 | 0.177 | 0.0657 |
| w50 | 1,079 | 3.167e+05 | 2.984e+05 | 2.935 | 2.766 | 0.169 | 0.0576 |

## 2. Total P&L ($) by WIDTH x FILL FRACTION -- OVERALL

`mid`=0% (optimistic), `f25`=25%, `f50`=50% (**HEADLINE**), `full`=100% worst-side (the control's honest bound). The fraction propagates through the 25% profit-target trigger.

| width | mid | f25 | f50 | full |
| --- | --- | --- | --- | --- |
| w5 | 3,190 | -4,660 | -13,026 | -33,760 |
| w10 | 8,492 | -2,686 | -10,148 | -28,545 |
| w20 | 11,972 | 3,619 | -2,503 | -24,060 |
| w30 | 10,208 | 2,378 | -3,690 | -19,970 |
| w50 | 16,615 | 5,961 | -2,146 | -16,185 |

### TRAIN (2022-01 .. 2024-06)

| width | mid | f25 | f50 | full |
| --- | --- | --- | --- | --- |
| w5 | 1,302 | -3,255 | -7,459 | -20,015 |
| w10 | 5,230 | -2,158 | -6,990 | -15,745 |
| w20 | 9,030 | 3,690 | -475 | -12,685 |
| w30 | 9,778 | 4,713 | -670 | -10,755 |
| w50 | 14,878 | 9,238 | 2,417 | -8,110 |

### TEST / OOS (2024-07 .. end)

| width | mid | f25 | f50 | full |
| --- | --- | --- | --- | --- |
| w5 | 1,888 | -1,405 | -5,568 | -13,745 |
| w10 | 3,262 | -528 | -3,158 | -12,800 |
| w20 | 2,943 | -71 | -2,028 | -11,375 |
| w30 | 430 | -2,335 | -3,020 | -9,215 |
| w50 | 1,738 | -3,278 | -4,564 | -8,075 |

## 3. Full per-width stats at the HEADLINE f50 fill

| width | trades | total_$ | win_rate | avg_$ | worst_day_$ | p05_$ | std_$ | sharpe_ann | sortino_ann | avg_hold_min |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| w5 | 1,061.000 | -13,026.250 | 0.827 | -12.280 | -540.000 | -221.250 | 88.820 | -2.194 | -0.945 | 53.900 |
| w10 | 1,079.000 | -10,147.500 | 0.855 | -9.400 | -860.000 | -365.120 | 136.800 | -1.091 | -0.437 | 45.300 |
| w20 | 1,083.000 | -2,502.500 | 0.876 | -2.310 | -1,092.500 | -467.370 | 184.290 | -0.199 | -0.075 | 39.400 |
| w30 | 1,083.000 | -3,690.000 | 0.876 | -3.410 | -1,451.250 | -551.750 | 218.230 | -0.248 | -0.093 | 37.600 |
| w50 | 1,079.000 | -2,146.250 | 0.877 | -1.990 | -1,431.250 | -576.380 | 241.540 | -0.131 | -0.049 | 35.900 |

## 4. Per-year total P&L per width (headline f50 fill)

| width | 2022 | 2023 | 2024 | 2025 | 2026 |
| --- | --- | --- | --- | --- | --- |
| w5 | -3,916 | -3,145 | -1,665 | -2,631 | -1,669 |
| w10 | -2,861 | -3,724 | -806 | -2,864 | 107 |
| w20 | 3,051 | -2,440 | -620 | -1,658 | -836 |
| w30 | 2,544 | -2,111 | -471 | -2,229 | -1,423 |
| w50 | 5,331 | -1,768 | -608 | -4,898 | -205 |

## 5. Per-regime total P&L per width (headline f50 fill)

### by gamma_regime

| width | negative | neutral | positive |
| --- | --- | --- | --- |
| w5 | -5,901 | -104 | -7,021 |
| w10 | -5,965 | 1,644 | -5,826 |
| w20 | -474 | 3,202 | -5,231 |
| w30 | 47 | 3,596 | -7,334 |
| w50 | 3,630 | 3,844 | -9,620 |

### by vix_regime

| width | backwardation | contango |
| --- | --- | --- |
| w5 | -3,516 | -9,510 |
| w10 | -2,864 | -7,284 |
| w20 | 2,686 | -5,189 |
| w30 | 3,174 | -6,864 |
| w50 | 7,620 | -9,766 |

## 6. Matched random-exit PLACEBO (headline f50 fill)

Run only for widths whose f50 total beats the 5-pt control. `arm_beats_placebo=True` means the width clears the 5% bar vs a random exit matched to its mean holding time.

| width | arm_total_$ | placebo_p50_$ | placebo_p95_$ | frac_placebo_ge_arm | arm_beats_placebo |
| --- | --- | --- | --- | --- | --- |
| w10 | -1.015e+04 | -1.508e+04 | -1.184e+04 | 0.004 | True |
| w20 | -2,502 | -1.308e+04 | -8,848 | 0 | True |
| w30 | -3,690 | -1.243e+04 | -7,509 | 0.0025 | True |
| w50 | -2,146 | -1.002e+04 | -4,333 | 0.0065 | True |

## 7. VERDICT

**f50 total P&L by width:** w5=$-13,026, w10=$-10,148, w20=$-2,503, w30=$-3,690, w50=$-2,146.

**(a) Positive plateau across >=3 ADJACENT widths at f50?** Longest adjacent-positive run = 0 width(s) -> NO.

**(b) OOS-stable (positive in the TEST half)?** f50 test-half by width: w5=$-5,568, w10=$-3,158, w20=$-2,028, w30=$-3,020, w50=$-4,564.

**(c) Beats matched placebo?** Widths clearing the 5% bar: ['w10', 'w20', 'w30', 'w50'].

**Widths clearing ALL THREE (f50-positive AND OOS-positive AND beat placebo):** NONE.


> **VERDICT: REFUTED.** No positive plateau of >=3 adjacent widths that is also OOS-stable and beats the matched placebo. Widening the wings does not rescue the 0DTE condor: the extra credit does not outrun the 4-leg cost at a realistic (f50) fill. A single positive cell, if any, is a mirage and is NOT a pass. Consistent with the cost-bound finding in the management terrain map.
