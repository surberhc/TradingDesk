# 0DTE Iron-Condor MANAGEMENT terrain map — finished-window report

_Generated 2026-07-03. Window 2022-01-03 -> 2026-07-01, 1127 session-days (1061 traded, 66 no-trade/skip, 0 crash-skipped). PAPER / research only._

## 1. Crash diagnosis + hardening

The full-history run had died TWICE from MEMORY exhaustion: each day loads a ~5M-row 1-min
NBBO quote parquet, and RAM climbed monotonically across days (the per-day `DayData` frame
was never released) until the OS killed the process around 600-800 days. A partial CSV of
~659 completed days (2022-01-03 .. 2024-08-16) survived because the engine appends+flushes
per day and resume-skips days already present.

Fix (research script only; frozen strategy/regime config untouched):
1. **Memory hygiene** — the per-day loop now `del`s the big `DayData`/record references and
   calls `gc.collect()` every iteration, so RAM resets each day instead of accumulating.
2. **Fresh-process chunk loop** — a new `--max-new-days N` CLI knob processes at most N
   not-yet-done days from the resume point, then exits cleanly (the partial CSV is durable
   per day). The window was then completed as a loop of ~90-day chunks, each a FRESH process
   so RAM starts at zero — this is what beat the OOM.
3. Per-day `try/except` -> `skip_reason` hardening was already present; it logged **0**
   crash-skips across all 468 newly-processed days, and 0 OOM deaths.

Window is now COMPLETE end-to-end: 2022-01-03 -> 2026-07-01 (last available SPXW 1-min
quote date), 1127 session-days, 1061 traded.

## 2. Total P&L ($) by ARM x FILL FRACTION — OVERALL

Fill fractions of the NET COMBO spread: `mid`=0% (optimistic), `f25`=25%, `f50`=50% (**HEADLINE**), `full`=100% worst-side (the control's honest bound). The fraction propagates through the profit-target triggers.

|  | mid | f25 | f50 | full |
| --- | --- | --- | --- | --- |
| A_hold | 5,830 | -2,531 | -13,289 | -32,905 |
| B_pt25 | 3,190 | -4,660 | -13,026 | -33,760 |
| B_pt50 | 3,782 | -6,204 | -15,455 | -34,770 |
| B_pt75 | 8,457 | -1,142 | -13,636 | -35,315 |
| C_t1500 | 1,755 | -6,398 | -15,485 | -35,475 |
| C_t1530 | 715 | -7,528 | -16,631 | -37,145 |
| D_combo | 107 | -9,144 | -17,503 | -36,955 |

### TRAIN (2022-01 .. 2024-06)

|  | mid | f25 | f50 | full |
| --- | --- | --- | --- | --- |
| A_hold | 1,462 | -3,415 | -10,840 | -21,990 |
| B_pt25 | 1,302 | -3,255 | -7,459 | -20,015 |
| B_pt50 | 2,645 | -3,996 | -9,923 | -21,115 |
| B_pt75 | 4,465 | -907 | -8,941 | -23,205 |
| C_t1500 | 1,130 | -3,610 | -8,794 | -20,060 |
| C_t1530 | -970 | -5,479 | -10,806 | -22,545 |
| D_combo | -453 | -6,528 | -11,358 | -22,595 |

### TEST / OOS (2024-07 .. end)

|  | mid | f25 | f50 | full |
| --- | --- | --- | --- | --- |
| A_hold | 4,368 | 884 | -2,449 | -10,915 |
| B_pt25 | 1,888 | -1,405 | -5,568 | -13,745 |
| B_pt50 | 1,137 | -2,208 | -5,533 | -13,655 |
| B_pt75 | 3,992 | -235 | -4,695 | -12,110 |
| C_t1500 | 625 | -2,788 | -6,691 | -15,415 |
| C_t1530 | 1,685 | -2,049 | -5,825 | -14,600 |
| D_combo | 560 | -2,616 | -6,145 | -14,360 |

## 3. Full per-arm stats at the HEADLINE 50% fill

| arm | trades | total_$ | win_rate | avg_$ | worst_day_$ | p05_$ | std_$ | sharpe_ann | sortino_ann | avg_hold_min |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A_hold | 1,061.000 | -13,288.750 | 0.676 | -12.520 | -2,021.250 | -253.750 | 160.000 | -1.243 | -0.788 | 107.400 |
| B_pt25 | 1,061.000 | -13,026.250 | 0.827 | -12.280 | -540.000 | -221.250 | 88.820 | -2.194 | -0.945 | 53.900 |
| B_pt50 | 1,061.000 | -15,455.000 | 0.754 | -14.570 | -2,021.250 | -238.750 | 129.630 | -1.784 | -0.936 | 76.000 |
| B_pt75 | 1,061.000 | -13,636.250 | 0.704 | -12.850 | -2,021.250 | -250.000 | 146.940 | -1.389 | -0.829 | 93.400 |
| C_t1500 | 1,061.000 | -15,485.000 | 0.590 | -14.590 | -267.500 | -180.000 | 70.560 | -3.283 | -2.209 | 58.100 |
| C_t1530 | 1,061.000 | -16,631.250 | 0.637 | -15.680 | -2,021.250 | -218.750 | 116.000 | -2.145 | -1.379 | 84.200 |
| D_combo | 1,061.000 | -17,502.500 | 0.666 | -16.500 | -2,021.250 | -217.500 | 112.640 | -2.325 | -1.407 | 72.000 |

## 4. Per-year total P&L per arm (headline 50% fill)

| arm | 2022 | 2023 | 2024 | 2025 | 2026 |
| --- | --- | --- | --- | --- | --- |
| A_hold | -6,294 | -4,149 | -51 | -1,614 | -1,181 |
| B_pt25 | -3,916 | -3,145 | -1,665 | -2,631 | -1,669 |
| B_pt50 | -7,034 | -3,368 | 124 | -2,784 | -2,394 |
| B_pt75 | -6,175 | -2,898 | -125 | -2,646 | -1,793 |
| C_t1500 | -3,285 | -4,116 | -1,891 | -4,209 | -1,984 |
| C_t1530 | -5,321 | -4,316 | -1,173 | -3,791 | -2,030 |
| D_combo | -5,844 | -4,764 | -771 | -4,041 | -2,083 |

## 5. Per-regime total P&L per arm (headline 50% fill)

### by gamma_regime

| arm | negative | neutral | positive |
| --- | --- | --- | --- |
| A_hold | -7,729 | -1,010 | -4,550 |
| B_pt25 | -5,901 | -104 | -7,021 |
| B_pt50 | -9,941 | 416 | -5,930 |
| B_pt75 | -9,148 | 66 | -4,555 |
| C_t1500 | -7,876 | -629 | -6,980 |
| C_t1530 | -8,373 | -629 | -7,630 |
| D_combo | -9,261 | 250 | -8,491 |

### by vix_regime

| arm | backwardation | contango |
| --- | --- | --- |
| A_hold | -3,063 | -10,226 |
| B_pt25 | -3,516 | -9,510 |
| B_pt50 | -4,630 | -10,825 |
| B_pt75 | -3,963 | -9,674 |
| C_t1500 | -3,360 | -12,125 |
| C_t1530 | -5,228 | -11,404 |
| D_combo | -5,550 | -11,953 |

## 6. Matched random-exit PLACEBO (headline 50% fill)

Run only for arms whose headline-fill total beats hold-to-settle (A_hold). `arm_beats_placebo=True` means the management logic clears the 5% bar vs a random exit matched to the same mean holding time.

| arm | arm_total_$ | placebo_p50_$ | placebo_p95_$ | frac_placebo_ge_arm | arm_beats_placebo | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| B_pt25 | -1.303e+04 | -1.888e+04 | -1.412e+04 | 0.0175 | True | nan |
| B_pt50 | nan | nan | nan | nan | nan | does not beat A_hold at headline fill |
| B_pt75 | nan | nan | nan | nan | nan | does not beat A_hold at headline fill |
| C_t1500 | nan | nan | nan | nan | nan | does not beat A_hold at headline fill |
| C_t1530 | nan | nan | nan | nan | nan | does not beat A_hold at headline fill |
| D_combo | nan | nan | nan | nan | nan | does not beat A_hold at headline fill |

## 7. 45-DTE benchmark (longer-duration comparator)

```
{'trades': 89, 'total_$': -53205.0, 'win_rate': 0.3708, 'avg_$': -597.81, 'worst_$': -4775.0, 'std_$': 1142.97, 'avg_hold_days': 28.9, 'trades_per_yr': 10.5, 'sharpe_ann': -1.695, 'sortino_ann': -1.229}
```

## 8. VERDICT + precondition outcome

**(a) Real net-positive edge at the 50% fill holding mid->50% AND OOS?** Arms passing that band test: NONE.

**(b) Do management arms (B/C/D) beat plain hold-to-settle (A) on TOTAL P&L at the 50% fill?** Arms beating A on total-$: ['B_pt25'] (A_hold headline total = $-13,289).

   Arms beating A on WIN RATE: ['B_pt25', 'B_pt50', 'B_pt75'] (A_hold win rate = 0.676).

**(c) Base-edge PRECONDITION for the regime-modulation follow-on:** NOT MET. Arms clearing BOTH the mid->50%/OOS band AND the placebo: NONE.


> **The regime-conditioned profit-target modulation follow-on stays GATED and is NOT run.** Per the pre-registration, a dynamic overlay is not used to rescue an edgeless base.
