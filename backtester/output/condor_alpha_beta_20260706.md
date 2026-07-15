# ARM 6 — Is Arm 5's condor P&L structural ALPHA or short-vol/short-gamma BETA?

_Run 2026-07-06. Runtime 2810s. PAPER / research only, OFFLINE, warehouse read-only. Frozen config untouched._

## VERDICT (lead)

### **BETA — the edge is harvested short-vol/short-gamma premium; a passive ATM short straddle on the same days harvests the same premium — the condor structure adds nothing the naive short-vol position doesn't**

- **Headline (w50, f50, full window): intercept alpha = $406.99/day (t = 14.56)**, bootstrap 95% CI [$341.63, $470.45]/day (EXCLUDES 0), R² 0.402.
- Regression factors are the **realized 14:00->16:00 intraday move** (signed), its magnitude |move|, and move² — the exact exposure window of the trade (entered 14:00, cash-settled 16:00). NOT close-to-close daily returns.
- OOS: intercept $396.90/day (train, n=585) vs $460.06/day (test, n=494) — stable.
- Passive ATM short-straddle benchmark on the SAME days: condor $37,019 vs straddle $98,175 => condor-minus-straddle spread **$-61,156** (condor adds NOTHING passive short-vol doesn't).

> **0DTE tail-sample limitation (flagged prominently):** SPX 0DTE dailies begin ~2022, so the entire sample is 2022-01→2026-07. COVID-scale (2020) and 2018-Q4 intraday crashes are **OUT of sample** — the window does not contain a severe systemic intraday tail. Any 'survives the tail' read is bounded by this; see §4.

## The exposure window (stated first — load-bearing)

Arm 5 enters at 14:00 (`entry_spot`) and resolves at 16:00 costless cash intrinsic against the recovered 16:00 level S* (`settle_spot`). The **entire** P&L is a function of the realized **14:00->16:00 move** = `(settle_spot - entry_spot)/entry_spot`. A short condor is short realized variance over exactly this 2-hour window, so the regression controls for that move (signed), |move|, and move². The intercept = premium harvested independent of the realized move.

- Realized 14:00->16:00 move over 1087 traded days: mean +0.0047%, std 0.4609%, p01 -1.457%, p99 +1.397%.

## §1 Alpha-vs-beta regression — daily P&L $ ~ intercept + b·move + b·|move| + b·move²

Per clean width (w20/w30/w50) at the f50 fill; intercept ($/day) = alpha.

| width | subset | n | **alpha $/day** | t(alpha) | 95% CI $/day | R² | β(move) | β(|move|) | β(move²) | total P&L $ |
|---|---|---|---|---|---|---|---|---|---|---|
| w20 | all | 1083 | **347.20** | 18.46 | [315.42, 383.80] | 0.423 | -4,577 | -120,671 | 1,716,922 | 18,269 |
| w20 | train | 587 | **353.87** | 12.80 | n/a | 0.420 | -7,424 | -102,705 | 1,025,880 | 9,436 |
| w20 | test | 496 | **367.06** | 14.77 | n/a | 0.481 | -1,153 | -161,426 | 3,010,471 | 8,833 |
| w30 | all | 1083 | **383.24** | 16.83 | [335.91, 429.97] | 0.409 | -6,551 | -120,926 | 539,937 | 29,420 |
| w30 | train | 588 | **377.21** | 11.21 | n/a | 0.396 | -10,751 | -96,020 | -384,415 | 15,286 |
| w30 | test | 495 | **423.53** | 14.21 | n/a | 0.485 | 65 | -175,621 | 2,284,265 | 14,134 |
| w50 | all | 1079 | **406.99** | 14.56 | [341.63, 470.45] | 0.402 | -5,189 | -112,622 | -1,453,047 | 37,019 |
| w50 | train | 585 | **396.90** | 9.49 | n/a | 0.369 | -8,534 | -88,912 | -1,940,608 | 17,674 |
| w50 | test | 494 | **460.06** | 12.70 | n/a | 0.494 | -3,693 | -171,350 | -181,708 | 19,345 |

_Reading: a positive, OOS-stable intercept whose CI excludes 0 = premium harvested independent of the realized move (alpha). An intercept that collapses across the train/test split or whose CI spans 0 once |move| is included = beta._

## §2 Passive short-premium benchmark — ATM short straddle, same days

Delta-neutral ATM short straddle entered 14:00, held to costless 16:00 cash settlement, honest entry fills (f50). The naive always-short-premium comparator (short vol, ~0 net delta, NO wings). If the condor merely tracks it, the 'edge' is beta.

| width | n | condor $ | straddle $ | spread (C−S) $ | condor Sharpe | straddle Sharpe |
|---|---|---|---|---|---|---|
| w20 | 1083 | 18,269 | 98,180 | -79,911 | 0.54 | 0.98 |
| w30 | 1083 | 29,420 | 102,105 | -72,685 | 0.72 | 1.02 |
| w50 | 1079 | 37,019 | 98,175 | -61,156 | 0.75 | 0.98 |

### Head-to-head by year & OOS (w50, f50)

| bucket | condor $ | straddle $ | spread (C−S) $ |
|---|---|---|---|
| 2022 | 9,846 | 10,493 | -646 |
| 2023 | 5,539 | -565 | 6,104 |
| 2024 | 1,771 | 33,508 | -31,736 |
| 2025 | 21,784 | 58,053 | -36,269 |
| 2026 | -1,921 | -3,313 | 1,391 |
| **train** | 17,674 | 26,400 | -8,726 |
| **test** | 19,345 | 71,775 | -52,430 |

_The condor is defined-risk (capped wings); the passive straddle is uncapped. A positive, OOS-stable spread means the condor STRUCTURE earns something the naive short-vol position does not — that would be structural, not beta. A spread ≈0 or negative means the condor just harvests the same short-vol premium._

## §3 VRP decomposition — premium sold vs realized cost paid

A short condor's P&L IS the realized VRP over the 14:00->16:00 window: it collects the entry credit and pays the capped intrinsic the realized move produced. VRP = premium sold − realized cost (= total P&L, by identity).

| width | premium sold $ | realized cost $ | VRP (=P&L) $ | train VRP $ | test VRP $ | γ+ VRP $ | γ0 VRP $ | γ− VRP $ |
|---|---|---|---|---|---|---|---|---|
| w20 | 238,464 | 220,195 | 18,269 | 9,436 | 8,833 | 17,735 | -3,859 | 4,393 |
| w30 | 275,212 | 245,792 | 29,420 | 15,286 | 14,134 | 22,613 | -5,205 | 12,013 |
| w50 | 307,554 | 270,535 | 37,019 | 17,674 | 19,345 | 26,375 | -5,471 | 16,115 |

_γ+ = positive-gamma (calm) regime, γ− = negative-gamma (stress). If the VRP is concentrated in the calm (γ+) bucket and thin/negative in stress, the carry is regime-dependent short-vol premium._

## §4 Tail-sufficiency & defined-risk stress

**(a) Does the 2022-2026 window even contain severe intraday tails?**

- Worst realized 14:00->16:00 move: **-3.170%** (2024-12-18); best +2.568%. p01 -1.457%, p05 -0.703%.
- Days with a ≤ −2% 2h move: 4; ≤ −3%: 1 out of 1087.
- **Limitation:** the worst 2h move in-sample is only ~3.2%. A COVID-scale intraday crash (−7% to −12% in 2h) is OUT of sample (0DTE dailies start ~2022). The window does NOT stress-test a systemic tail.

**(b) Defined-risk stress — capped per-width loss under a hypothetical adverse 14:00->16:00 move** (median entry credit as the representative trade):

| width | −1% | −2% | −3% | −5% | −7% | max-capped loss $ |
|---|---|---|---|---|---|---|
| w20 | 1,789 | 1,789 | 1,789 | 1,789 | 1,789 | 1,789 |
| w30 | 2,733 | 2,765 | 2,765 | 2,765 | 2,765 | 2,765 |
| w50 | 3,215 | 4,748 | 4,748 | 4,748 | 4,748 | 4,748 |

_Loss is per-contract $ (credit − capped intrinsic). Beyond the far wing the loss is fully capped at width×100 − credit — that cap is what makes the strategy defined-risk; a −5% and a −50% crash cost the same capped amount._

**(c) Cluster stress — how many fully-capped worst-case days erase the calm carry** (w50, f50):

| width | observed total $ | per-day capped loss $ | days-to-erase | total after 3-day cluster $ | after 5-day $ | after 10-day $ |
|---|---|---|---|---|---|---|
| w20 | 18,269 | 1,789 | 10.2 | 12,903 | 9,325 | 381 |
| w30 | 29,420 | 2,765 | 10.6 | 21,125 | 15,595 | 1,770 |
| w50 | 37,019 | 4,748 | 7.8 | 22,776 | 13,281 | -10,456 |

_'days-to-erase' = how many fully-capped max-loss days it takes to wipe the observed multi-year total. A small number means the positive total is calm-carry a cluster of tail days would erase; a large number means the carry has real cushion. Note the cap bounds each day's loss — this is the upside of defined-risk vs the uncapped straddle in §2._

## Method notes

- Data: Arm 5 per-day P&L per width per fill from `output/condor_cashsettle_hold/condor_cashsettle_hold_days.csv` (entry_spot @14:00, recovered settle_spot @16:00 = S*).
- Regression + block bootstrap reuse `csp_alpha_beta` (`ols_alpha_beta` generalized to multi-factor here; `_stationary_blocks` block=20, 2000 resamples, seed 20260706).
- Passive straddle benchmark reuses `s6_control`/`s6_recon` (14:00 entry, recovered spot, honest fills) and Arm-5's own recovered settle_spot, so condor & straddle are marked on the IDENTICAL 14:00->16:00 move. Costless cash settlement (European SPXW).
- OOS split 2024-06-30 (train ≤ split < test). No parameter tuned to the data. Warehouse strictly read-only.
