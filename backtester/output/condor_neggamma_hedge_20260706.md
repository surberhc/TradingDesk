# 0DTE Iron-Condor NEGATIVE-GAMMA HEDGE overlay (Arm 2) — report

_Generated 2026-07-06. Window 2022-01-03 -> 2026-07-01, 1127 session-days (1061 traded, 414 negative-gamma hedge days). PAPER / research only. Base = managed condor profit-target-25% (B_pt25), NO hedge. Headline hedge = two-sided 0.05-delta long tail (put+call), fired ONLY on prior-EOD negative-gamma days (causal)._

## VERDICT

**FAIL** at the headline 50% fill. The neg-gamma hedge does NOT pass the pre-registered bar (improve the neg-gamma bucket net of cost AND cap its tail AND not turn the book into a worse-than-base wash AND beat the random-day placebo).

| check | result |
| --- | --- |
| neg-gamma bucket $ improves (net of hedge cost) | NO ($-5901.25 -> $-18413.75) |
| neg-gamma tail capped (worst-day AND p05 improve) | YES (worst $-540.0 -> $-475.0; p05 $-238.38 -> $-180.44) |
| overall book not worse than base | NO ($-13026.25 -> $-25538.75) |
| beats matched random-day placebo (frac < 0.05) | NO (frac placebo>=real = 0.9427) |

## 1. Negative-gamma bucket — base vs base+hedge, across the fill band

The bucket this hedge targets. `total_$` net of hedge cost; `worst_day_$` and `p05_$` are the tail (the whole point is capping these).

| fill | arm | n | total_$ | avg_$ | win_rate | worst_day_$ | p05_$ | std_$ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mid | base | 414 | 1077.5 | 2.6 | 0.8889 | -315.0 | -233.37 | 81.92 |
| mid | hedged | 414 | -4592.5 | -11.09 | 0.285 | -470.0 | -100.88 | 115.95 |
| f25 | base | 414 | -2293.13 | -5.54 | 0.8599 | -415.0 | -236.5 | 89.59 |
| f25 | hedged | 414 | -12184.38 | -29.43 | 0.1473 | -433.12 | -160.25 | 114.1 |
| f50 | base | 414 | -5901.25 | -14.25 | 0.8261 | -540.0 | -238.38 | 97.62 |
| f50 | hedged | 414 | -18413.75 | -44.48 | 0.0821 | -475.0 | -180.44 | 107.9 |
| full | base | 414 | -16160.0 | -39.03 | 0.7053 | -380.0 | -235.0 | 105.68 |
| full | hedged | 414 | -29425.0 | -71.07 | 0.0725 | -430.0 | -228.5 | 108.52 |

## 2. Overall book — base-only vs negative-gamma-hedged, across the fill band

Does hedging only neg-gamma days turn the whole book into a wash worse than base? (base pt25 P&L on all traded days; hedge added only on neg-gamma days.)

| fill | arm | n | total_$ | avg_$ | win_rate | worst_day_$ | p05_$ |
| --- | --- | --- | --- | --- | --- | --- | --- |
| mid | base | 1061 | 3190.0 | 3.01 | 0.8944 | -327.5 | -217.5 |
| mid | hedged | 1061 | -2480.0 | -2.34 | 0.6588 | -470.0 | -180.0 |
| f25 | base | 1061 | -4660.0 | -4.39 | 0.8643 | -415.0 | -223.13 |
| f25 | hedged | 1061 | -14551.25 | -13.71 | 0.5862 | -433.12 | -196.88 |
| f50 | base | 1061 | -13026.25 | -12.28 | 0.8266 | -540.0 | -221.25 |
| f50 | hedged | 1061 | -25538.75 | -24.07 | 0.5363 | -475.0 | -203.75 |
| full | base | 1061 | -33760.0 | -31.82 | 0.722 | -380.0 | -220.0 |
| full | hedged | 1061 | -47025.0 | -44.32 | 0.475 | -430.0 | -215.0 |

## 3. OOS split (headline 50% fill) — train 2022..2024-06 / test 2024-07..end

| half | scope | arm | n | total_$ | worst_day_$ | p05_$ |
| --- | --- | --- | --- | --- | --- | --- |
| train | neg-bucket | base | 232 | -3538.75 | -540.0 | -247.37 |
| train | neg-bucket | hedged | 232 | -12251.25 | -475.0 | -201.0 |
| train | book | base | 576 | -7458.75 | -540.0 | -234.06 |
| train | book | hedged | 576 | -16171.25 | -475.0 | -212.81 |
| test | neg-bucket | base | 182 | -2362.5 | -307.5 | -214.88 |
| test | neg-bucket | hedged | 182 | -6162.5 | -427.5 | -143.31 |
| test | book | base | 485 | -5567.5 | -307.5 | -207.5 |
| test | book | hedged | 485 | -9367.5 | -427.5 | -183.75 |

## 4. Per-year total P&L (headline 50% fill)

| year | neg_n | neg_base_$ | neg_hedged_$ | book_base_$ | book_hedged_$ |
| --- | --- | --- | --- | --- | --- |
| 2022 | 115 | -2294.0 | -8724.0 | -3916.0 | -10346.0 |
| 2023 | 84 | -1335.0 | -2535.0 | -3145.0 | -4345.0 |
| 2024 | 74 | 281.0 | -2279.0 | -1665.0 | -4225.0 |
| 2025 | 94 | -1851.0 | -4714.0 | -2631.0 | -5494.0 |
| 2026 | 47 | -703.0 | -163.0 | -1669.0 | -1129.0 |

## 5. Matched random-day placebo (headline 50% fill)

Hedge a RANDOM set of the same number of traded days instead of the neg-gamma days; many draws. `neggamma_beats_placebo=True` means the neg-gamma-targeted book is in the top 5% vs random-day hedging.

```
{'k_hedge_days': 414, 'pool_days': 1061, 'base_only_$': -13026.25, 'real_neggamma_book_$': -25538.75, 'placebo_p50_$': -22688.13, 'placebo_p05_$': -25627.56, 'placebo_p95_$': -19695.38, 'frac_placebo_ge_real': 0.9427, 'neggamma_beats_placebo': False}
```

## 6. Hedge-variant sensitivity (headline 50% fill, neg-gamma bucket total_$)

Two-sided `both` is the pre-registered headline; `put`/`call` shown so no side is cherry-picked after the fact.

| variant | neg_hedged_total_$ | neg_worst_$ | neg_p05_$ |
| --- | --- | --- | --- |
| both | -18413.75 | -475.0 | -180.44 |
| put | -15288.75 | -388.75 | -290.0 |
| call | -9026.25 | -626.25 | -261.31 |
| (base, no hedge) | -5901.25 | -540.0 | -238.38 |
