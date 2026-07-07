# 0DTE Condor BASE-PACKAGE Grid — Anti-Curve-Fit Verdict

**Date:** 2026-07-07
**Inputs:** `output/condor_base_grid/condor_base_grid.csv` (3,024 rows) + 6 dayrow shards
**Grid:** 36 base configs (entry {945, 1130, 1400} × delta {0.10, 0.15, 0.20, 0.30} × wing {5, 10, 20}) × 7 management arms × 4 fills {mid, f25, f50, full} × 3 scopes {full, train=2022-01→2024-06, test/OOS=2024-07→2026-06}
**Headline fill:** f50 (half-spread). **Honest worst case:** full. **Bar to clear:** positive expectancy at f50 on OOS, ideally still positive at full.

> Note on fields: `expectancy` and `avg_pnl` are the same number (per-trade $ P&L; max abs diff 0.0001, rounding only). All figures below use `expectancy`. Scope label `test` = the OOS window.

---

## THE VERDICT — REFUTE

**The 0DTE condor does not survive honest costs on out-of-sample data.** At the headline f50 (half-spread) fill, 21 of 252 base-config × arm cells show positive OOS expectancy — but the moment we charge the honest worst-case (`full`) fill, **every single one of those 21 goes negative (0/252 positive at full)**. The best OOS config at f50 — entry 14:00, 0.20-delta, 20-wide wings, hold-to-settle (A_hold) — earns +$22.24/trade at f50 yet loses −$3.03/trade at full. The apparent f50 edge is entirely the half-spread we chose not to pay; it is a fills artifact, not a strategy edge. The multiple-testing check is even more damning: that best config's per-observation Sharpe is 0.048 (annualized ≈ 0.77), but the Deflated Sharpe Ratio against N=252 trials is **0.0001** — the expected-max-Sharpe benchmark from having searched the grid (SR₀ = 0.224) is roughly 4.6× larger than what we actually observed, so the result is fully consistent with selection luck. Train→OOS consistency is nearly empty: only 2 of 252 configs are positive on both halves at f50, and both die at full too. Searching the base package the honest way — TRAIN-then-validate-ONCE-on-OOS, plateau-not-peak, DSR-corrected — did its job: it tells us there is **no robust base package that clears honest costs**. This is a clean refute, not a rescue. Per rule #1, the curve-fit-preventing read is the correct one here, and the data forces it unambiguously: a lone f50 peak that dies at full and fails DSR is a refute.

---

## 1. Plateau map — f50 OOS expectancy ($/trade), positives marked `+`

Positive cells are almost entirely confined to the **20-wide wing** and the **late (14:00) / mid (11:30) entries** — a thin rim on one edge of the surface, not a broad plateau. Every 0.30-delta and every 5-wide column is deeply negative. The best per-arm cells:

| Arm | Best f50 OOS cell | Exp $/trade | Positive cells in arm |
|---|---|---:|---:|
| A_hold  | 1400 / 0.20 / w20 | **+22.24** | 8/36 |
| B_pt75  | 1400 / 0.30 / w20 | +15.60 | 3/36 |
| C_t1530 | 1400 / 0.30 / w20 | +10.29 | 3/36 |
| D_combo | 1400 / 0.30 / w20 | +5.20  | 2/36 |
| B_pt50  | 1400 / 0.30 / w20 | +1.41  | 2/36 |
| C_t1500 | 1130 / 0.15 / w20 | +0.38  | 2/36 |
| B_pt25  | 1130 / 0.15 / w20 | +1.73  | 1/36 |

**Global best f50 OOS: +22.24 · Global worst: −169.45.** 21/252 cells positive.

Representative surface — **A_hold (best arm), f50 OOS**, delta (rows) × wing (cols):

```
entry=945      w5        w10       w20
 0.10      -16.58    -17.94    -71.22
 0.15      -28.96    -16.23     -1.65
 0.20      -40.72    -50.79     -7.42
 0.30     -141.83   -141.37   -133.44
entry=1130     w5        w10       w20
 0.10       -9.85    -11.89      4.51+
 0.15      -19.43    -10.72      4.32+
 0.20      -25.51    -22.59    -17.39
 0.30      -69.54    -64.78    -50.63
entry=1400     w5        w10       w20
 0.10       -6.77     -5.85      2.32+
 0.15       -5.05      5.73+    14.19+
 0.20       -7.65      3.73+    22.24+
 0.30      -37.86    -21.13     21.89+
```

The positives cluster in the bottom-right (late entry, wide wings) — but even the "best" surface is negative in ~78% of its cells.

---

## 2. Train→OOS consistency (f50)

Positive on **BOTH** train and OOS at f50: **2 of 252 configs.**

| entry / delta / wing / arm | Train exp | Train WR | OOS exp | OOS WR |
|---|---:|---:|---:|---:|
| 1130 / 0.15 / 20 / A_hold | +1.45 | 0.672 | +4.32 | 0.693 |
| 1130 / 0.15 / 20 / B_pt25 | +2.24 | 0.892 | +1.73 | 0.886 |

- Train-positive configs total: **3/252**. OOS-positive: **21/252**. Overlap: **2**.
- Overfit cells (train-positive → OOS-negative): 1.
- **Critical:** the biggest OOS winner (1400/0.20/w20/A_hold, +$22.24 OOS) is **not** positive on train — its OOS strength is not corroborated in-sample. Both of the two consistent configs earn only ~$2–4/trade at f50 and (§5) both lose at full. There is no config that is (a) positive on train, (b) positive on OOS, and (c) survives full.

---

## 3. Plateau-not-peak (f50 OOS)

Of the 21 f50-OOS positives, only 4 sit in a contiguous positive neighborhood (≥ half of adjacent delta/wing/entry cells also positive); the rest are lone islands surrounded by losers (noise):

- **PLATEAU-ish:** 1400/0.20/w20/A_hold (3/4 nbrs +), 1400/0.15/w20/A_hold (4/4 +), 1400/0.10/w20/A_hold (2/3 +), 1130/0.10/w20/A_hold (2/4 +).
- **ISLANDS (noise):** the 0.30-delta winners (1400/0.30/w20 across B_pt75, C_t1530, D_combo, B_pt50) each have 0–1 positive neighbors; likewise the 1130/0.15/w20 winners in B_pt25/B_pt50/C_t1500/C_t1530 (0/5 positive neighbors each).

The one semi-coherent region — A_hold, 14:00 entry, 20-wide wings, low-to-mid delta — is exactly the region that collapses at full cost (§5). So even the "plateau" is a plateau *of the half-spread subsidy*, not of edge.

---

## 4. Deflated Sharpe — best OOS f50 config

**Config:** entry 1400 / delta 0.20 / wing 20 / A_hold (hold-to-settle). n = 495 OOS trading days, total OOS P&L +$11,010, mean +$22.24/day.

| Quantity | Value |
|---|---:|
| Per-observation Sharpe | 0.0485 |
| Annualized Sharpe (×√252) | 0.770 |
| Skew | −1.10 |
| Kurtosis (full) | 2.88 |
| T (obs) | 495 |
| Trial-Sharpe spread (var_trials, per-obs, across 252 configs) | 0.006202 (std 0.0788) |
| E[max SR] benchmark (SR₀), N=252 | **0.2237** |
| PSR vs 0 | 0.853 |
| **DSR (P[true SR > SR₀]), N=252** | **0.0001** |

**Interpretation:** the observed per-observation Sharpe (0.048) is far below the 0.224 that a *maximum over 252 trials* would reach by chance alone. DSR ≈ 0.0001 ≪ 0.95 — the strategy **fails the multiple-comparisons haircut outright**. The raw PSR-vs-zero of 0.85 looks superficially okay, but that ignores the search; once the search is priced in (DSR), the edge vanishes. Negative skew (−1.10) further penalizes it. This is the textbook anti-curve-fit red flag.

---

## 5. Full-cost check — does the best OOS config survive at `full`?

**No. 0 of 252 configs are positive at the `full` (worst-side) fill on OOS.** Every f50-OOS positive dies:

| entry/delta/wing/arm | f50 OOS | full OOS | |
|---|---:|---:|---|
| 1400/0.20/20/A_hold | +22.24 | **−3.03** | DIES |
| 1400/0.30/20/A_hold | +21.89 | −21.94 | DIES |
| 1400/0.30/20/B_pt75 | +15.60 | −27.54 | DIES |
| 1400/0.15/20/A_hold | +14.19 | −6.35 | DIES |
| 1400/0.30/20/C_t1530 | +10.29 | −19.31 | DIES |
| 1130/0.15/20/A_hold (train+OOS consistent) | +4.32 | −25.21 | DIES |
| 1130/0.15/20/B_pt25 (train+OOS consistent) | +1.73 | −33.76 | DIES |

The half-spread between f50 and full is worth ~$18–35/trade — larger than any config's f50 edge. **The entire apparent edge is the spread we chose not to pay.**

---

## Bottom line

Methodology worked exactly as designed. We searched the base package instead of assuming it, validated once on OOS, checked plateau-not-peak, and applied the Deflated-Sharpe multiple-testing correction. All four gates converge on the same answer: **the 0DTE condor has no robust base package that clears honest costs.** The f50 positives are a fills artifact (0/252 survive full), the OOS best fails DSR (0.0001), and only 2/252 configs are even train/OOS-consistent (both die at full). **REFUTE.**
