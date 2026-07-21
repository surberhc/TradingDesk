# S0 equity-sleeve broadening — study report (skill vs beta)

**Pre-registration:** `docs/PREREG_S0_equity_sleeve_broadening_2026-07-20.md` (registered before any run).
**Run:** 2026-07-20, Phase 2. Balanced version. Window **2015-02-02 → 2026-07-20** (full S0 chassis).
**Harness:** reuses the backtester's own `run_backtest()`; study knobs (`EQUITY_TILT_*`) set **at runtime only** — frozen production config untouched, warehouse read-only. Size funds joined into the price panel so the tilt can see them; production S0 stays byte-identical (tilt default OFF).
**Cost of the study:** 19 production-parity backtests, **3.20–4.03 s each** (baseline 3.20 s, mean ≈ 3.83 s, ~72 s of backtests); full harness incl. the block-bootstrap alpha CIs, the beta-matched arm, and the 2008 equity-sleeve sub-study = **130.4 s total wall-clock**.

---

## 1. The verdict — is the tilt real selection, or just more SPY beta?

**Neither. It is a small, consistent drag.** Broadening the equity sleeve into size (IJH/IJR) + leading sectors does **not** add return, does **not** add skill, and does **not** even add equity beta. Across **every** cell of the `TILT_PCT × N` grid, **both** OOS halves, **both** fund pairs, and **every** regime bucket, turning the tilt on **lowers** CAGR, Sharpe, Sortino, Calmar and final equity while leaving realized SPY-beta (~0.222) and full-period max drawdown (~-9.67%) essentially unchanged. The drag grows monotonically with `TILT_PCT`.

The killer control is even more damning than the pre-registered null ("just more beta — buy more SPY"). That null assumed the tilt at least *raises* return by *raising beta*. It does not: **realized beta is flat-to-slightly-lower than baseline in every cell**, so there is no extra beta to attribute and nothing for a "hold more SPY" portfolio to match — the beta-matched arm collapses onto the plain baseline, which already dominates every tilt cell. The honest one-liner is not "buy more SPY," it is **"do nothing — the broad-beta sleeve you already run is better."**

**Andrew's thesis — that the tilt helps in the bulls — is refuted on its own turf.** The bull-regime cumulative return is **lower than baseline in all nine cells** (-0.8 to -4.7 pts), the specific place the size/leadership premium was supposed to show up.

**Why (mechanism, not curve-fit):** over 2015–2026 US small/mid-caps broadly lagged large-cap, and a 3m/6m momentum gate chases sector leadership that mean-reverts. Layer that inside an already-small, regime-managed equity sleeve (baseline portfolio beta is only 0.22 — the all-weather chassis holds heavy defense/duration/real-assets and cuts equity in bad regimes) and the net effect is extra internal churn + cost holding a basket that underperformed broad beta. The result is a robust non-finding, not noise: it is monotone in `TILT_PCT`, near-flat in `N`, and unchanged under the IJH/IJR → VO/VB swap.

**This is a net-merit "the data says no." Per the pre-registration this report does NOT declare adopt/reject — that is Andrew's call.** What follows is the full evidence and the exchange rate.

---

## 2. The grid (IJH/IJR size funds)

Per-cell full-chassis metrics vs the tilt=0 baseline. (CSV: `s0_equity_tilt/grid_metrics.csv`.)

| Arm | CAGR | Max DD | Sharpe | Sortino | Calmar | β vs SPY | Final $1 |
|---|---|---|---|---|---|---|---|
| **BASELINE (tilt 0)** | **6.89%** | **-9.66%** | **0.655** | **0.890** | **0.712** | **0.222** | **2.152** |
| 10% / N3 | 6.78% | -9.67% | 0.640 | 0.869 | 0.702 | 0.222 | 2.128 |
| 10% / N4 | 6.77% | -9.67% | 0.638 | 0.867 | 0.700 | 0.222 | 2.124 |
| 10% / N5 | 6.77% | -9.67% | 0.640 | 0.869 | 0.700 | 0.221 | 2.125 |
| 20% / N3 | 6.68% | -9.68% | 0.623 | 0.847 | 0.691 | 0.221 | 2.104 |
| 20% / N4 | 6.65% | -9.67% | 0.620 | 0.843 | 0.687 | 0.221 | 2.097 |
| 20% / N5 | 6.66% | -9.67% | 0.623 | 0.846 | 0.688 | 0.221 | 2.099 |
| 30% / N3 | 6.58% | -9.68% | 0.605 | 0.823 | 0.679 | 0.221 | 2.080 |
| 30% / N4 | 6.53% | -9.68% | 0.602 | 0.817 | 0.674 | 0.221 | 2.069 |
| 30% / N5 | 6.54% | -9.67% | 0.606 | 0.822 | 0.676 | 0.220 | 2.072 |

Every cell is below baseline on every metric. Max drawdown is flat to ~2 bps deeper. The largest tilt (30%/N4) costs **-36 bps/yr of CAGR** for **zero** drawdown improvement.

---

## 3. Three-arm comparison — the decisive control (prereg §3)

CAPM regression of each variant's **daily excess return on SPY's** (rf = T-bill daily), plus the stationary block-bootstrap 95% CI on annualized alpha (block ≈ 20 trading days, 2000 resamples, seed `np.random.default_rng(20260720)`), and the beta-matched broad-beta arm. (CSV: `s0_equity_tilt/beta_attribution.csv`.)

| Cell | β vs SPY | CAPM α (ann.) | α 95% CI | R² |
|---|---|---|---|---|
| 10% / N3 | 0.222 | +2.30% | **[-1.19%, +5.48%]** | 0.255 |
| 10% / N4 | 0.222 | +2.28% | [-1.21%, +5.43%] | 0.255 |
| 10% / N5 | 0.221 | +2.29% | [-1.21%, +5.41%] | 0.255 |
| 20% / N3 | 0.221 | +2.21% | [-1.31%, +5.53%] | 0.250 |
| 20% / N4 | 0.221 | +2.18% | [-1.34%, +5.37%] | 0.251 |
| 20% / N5 | 0.221 | +2.19% | [-1.30%, +5.37%] | 0.251 |
| 30% / N3 | 0.221 | +2.13% | [-1.43%, +5.51%] | 0.244 |
| 30% / N4 | 0.221 | +2.08% | [-1.47%, +5.44%] | 0.246 |
| 30% / N5 | 0.220 | +2.10% | [-1.42%, +5.36%] | 0.246 |

Reading it honestly:
- The positive ~2% alpha is **the whole S0 strategy's** low-beta alpha vs SPY (a diversified all-weather book earns positive alpha vs a 100%-equity index by construction). It is **not** created by the tilt.
- **Alpha DECREASES as the tilt grows** (2.30% → 2.08%). The tilt *erodes* the baseline's existing alpha; it does not manufacture any.
- **Every CI spans zero** — no cell's alpha is statistically distinguishable from zero. There is no detectable selection skill.

**Beta-matched broad-beta arm.** Constructed as `(1-k)·baseline_S0 + k·SPY`, with `k` set so realized beta matches the cell's. Because **every tilt cell's beta ≤ baseline beta (0.222)**, the solver floors at **k = 0 for all nine cells** — i.e. there is no extra beta to match, and the beta-matched portfolio *is* the baseline. The baseline (even on the cost-free daily-return basis the matched arm is built on: CAGR 7.02%, maxDD -9.64%, Sharpe 0.675, Calmar 0.728) dominates every tilt cell. The "could we get this return more cheaply by holding more SPY?" question degenerates: the tilt delivers *less* return at the *same* beta, so the cheaper option isn't "more SPY," it's "the sleeve you already hold."

*Judgment calls flagged:* (a) CAPM uses T-bill rf for proper excess returns; (b) the matched arm is built from cost-free daily returns (`res["returns"]` excludes the rebalance turnover charge that `res["nav"]` includes) — this flatters the matched/baseline arm by ~13 bps/yr but cannot change the direction of the verdict, since the tilt loses to baseline on the cost-inclusive NAV too and k=0 anyway.

---

## 4. Regime split — Andrew's lens (bull vs crisis)

Timeline partitioned by the existing Market Health Score band: **bull/expansion = RiskOn + RiskOnNarrowing** (1,589 trading days), **defensive/crisis = Caution/Defensive/CapitalPreservation** (1,293 days). The regime engine is independent of the equity-sleeve mix, so the partition is byte-identical across all cells (verified). Cumulative return within each bucket, gain vs baseline. (CSV: `s0_equity_tilt/regime_split.csv`.)

Baseline: **bull +60.50%**, crisis +36.05%.

| Cell | Bull cum | Δ bull vs base | Crisis cum | Δ crisis vs base |
|---|---|---|---|---|
| 10% / N3 | +59.73% | **-0.77 pt** | +35.28% | -0.77 pt |
| 10% / N4 | +58.92% | -1.58 pt | +35.70% | -0.35 pt |
| 10% / N5 | +59.36% | -1.14 pt | +35.38% | -0.67 pt |
| 20% / N3 | +58.93% | -1.56 pt | +34.51% | -1.54 pt |
| 20% / N4 | +57.33% | -3.16 pt | +35.34% | -0.71 pt |
| 20% / N5 | +58.21% | -2.29 pt | +34.71% | -1.34 pt |
| 30% / N3 | +58.11% | -2.38 pt | +33.74% | -2.31 pt |
| 30% / N4 | +55.75% | **-4.75 pt** | +34.99% | -1.06 pt |
| 30% / N5 | +57.06% | -3.44 pt | +34.04% | -2.01 pt |

**The headline for the thesis: bull-regime return is lower in every single cell.** The tilt does not help in the bulls — it hurts, and the damage scales with the tilt fraction. It also gives back return in the crisis bucket (so it is not buying downside protection either).

---

## 5. OOS halves — hard anti-curve-fit gate (train 2015→2020, test 2021→present)

Gain vs baseline in each half. The pre-registered gate: a benefit must appear in **BOTH** halves. (CSV: `s0_equity_tilt/oos_halves.csv`.)

| Cell | Δ CAGR train | Δ CAGR test | Δ bull-ret train | Δ bull-ret test |
|---|---|---|---|---|
| 10% / N3 | -0.07 pt | -0.13 pt | -0.04 pt | -0.60 pt |
| 10% / N4 | -0.03 pt | -0.22 pt | -0.10 pt | -1.20 pt |
| 10% / N5 | -0.03 pt | -0.20 pt | +0.00 pt | -0.93 pt |
| 20% / N3 | -0.15 pt | -0.26 pt | -0.09 pt | -1.20 pt |
| 20% / N4 | -0.05 pt | -0.44 pt | -0.20 pt | -2.40 pt |
| 20% / N5 | -0.06 pt | -0.41 pt | +0.00 pt | -1.86 pt |
| 30% / N3 | -0.23 pt | -0.39 pt | -0.15 pt | -1.83 pt |
| 30% / N4 | -0.08 pt | -0.66 pt | -0.30 pt | -3.60 pt |
| 30% / N5 | -0.10 pt | -0.61 pt | -0.01 pt | -2.79 pt |

**GATE RESULT: cannot be satisfied — there is no benefit in either half to survive.** CAGR gain is negative in both halves for every cell; the (larger, more recent, more bull-heavy) **test** half is uniformly worse. The handful of ~0.00-pt "positive" train bull-gains are noise and flip strongly negative out-of-sample. This is a clean fail, and the negative direction is *stronger* out-of-sample — the opposite of a marginal in-sample win that decays.

---

## 6. Per-episode trade-off surface (prereg §5 — the deliverable that matters)

Each cell's max drawdown inside the named episode, side by side with its full-period bull-regime return gain. **No hard drawdown floor/ceiling** (signed off) — this is the exchange rate for Andrew to weigh. (CSV: `s0_equity_tilt/episodes.csv`.)

Baseline episode maxDD: 2015-08 **-5.71%**, 2018-Q4 **-8.56%**, 2020 COVID **-9.66%**, 2022 bear **-6.85%**.

| Cell | 2015-08 ΔDD | 2018-Q4 ΔDD | COVID ΔDD | 2022 ΔDD | ← Δ drawdown (pts) / Δ bull return → |
|---|---|---|---|---|---|
| 10% / N3 | +0.00 | -0.04 | -0.01 | +0.15 | bull -0.77 pt |
| 20% / N4 | +0.04 | -0.07 | -0.01 | +0.28 | bull -3.16 pt |
| 30% / N3 | +0.00 | -0.13 | -0.02 | +0.46 | bull -2.38 pt |
| 30% / N4 | +0.07 | -0.10 | -0.01 | +0.42 | bull -4.75 pt |
| 30% / N5 | +0.02 | -0.08 | -0.01 | +0.37 | bull -3.44 pt |

(ΔDD positive = shallower/better; negative = deeper/worse.) **The exchange rate is unambiguously bad.** Every drawdown move is sub-half-a-point: the tilt is marginally *worse* in 2018-Q4 and COVID and marginally *better* in 2022 (the momentum gate had rotated out of the funds that led the drawdown). There is no "gave up return, bought crash protection" trade — full-period max drawdown is flat/slightly worse (COVID trough dominates and the tilt deepens it a hair). You pay real bull-return (up to -4.75 pts cumulative) for essentially zero net drawdown relief.

---

## 7. Plateau + fund-swap robustness (prereg §4 — hard gate)

- **Plateau:** the surface is a clean *contiguous* region — but a contiguously **negative** one. The drag is monotone in `TILT_PCT` (10%→30% worsens CAGR 6.78%→6.53%) and near-flat across `N`. There is **no positive plateau** to adopt; the pre-registered plateau gate has nothing to stand on. The upside: this consistency means the "no" is robust, not a one-cell artifact.
- **Fund swap (IJH/IJR → VO/VB, `EQUITY_TILT_USE_ALT_SIZE`):** the finding **survives intact.** VO/VB reproduces IJH/IJR almost exactly — every cell below baseline, every bull-gain negative, same magnitudes (e.g. 20%/N4: IJH/IJR final 2.097 / bull Δ -3.16 pt vs VO/VB final 2.093 / bull Δ -3.38 pt). The negative result is not an artifact of the size-fund choice. (CSV rows tagged `VO/VB` in the grid/regime/oos/episode CSVs.)

**GATE RESULT: the (negative) finding is robust across the contiguous grid and across the fund swap.** No curve-fit rescue exists.

---

## 8. 2008 GFC sub-study — directional only

**Caveat (per prereg §4):** this is an **equity-sleeve-only** simulation (broad-beta core vs broadened sleeve, monthly-rebalanced, all-equity NAV) extended back to 2007-09 using the size/sector price history. It is **NOT** the full S0 chassis — S0's defensive/real-asset/duration sleeves are inception-thin pre-2015, so this speaks only to the *equity sleeve's internal composition* through the GFC, not to how S0 would actually have behaved. Directional, not authoritative. (CSV: `s0_equity_tilt/gfc2008.csv`.)

| Equity-sleeve arm | GFC max DD (to 2009-12) | Total ret to 2009-12 | Full-window (2007→2026) final $1 |
|---|---|---|---|
| Baseline broad-beta | **-56.09%** | -21.12% | **6.483** |
| 10% / N3 | -55.74% | -20.87% | 6.423 |
| 20% / N5 | -55.29% | -20.25% | 6.437 |
| 30% / N5 | -54.89% | -19.82% | 6.407 |
| VO/VB 20% / N4 | -55.59% | -20.90% | 6.368 |

In the pure equity sleeve, broadening **marginally shallowed** the 2008 drawdown (~0.3–1.2 pts) and slightly improved the trough-to-2009 return — the momentum gate reverted much of the sleeve to broad beta into the crash. But it is small, it is equity-sleeve-only, and over the full 2007–2026 equity-only path broad beta still finishes highest. A mild directional point in the tilt's favor that does not survive into the full-chassis 2015→ results above.

---

## 9. What this means for the adopt decision

Framed as the net-merit trade-off, for Andrew's call:

- **The exchange rate:** turning the tilt on costs **~10 to ~36 bps/yr of CAGR** (rising with `TILT_PCT`) and **-0.8 to -4.7 pts of cumulative bull-regime return**, in return for **~zero** net drawdown improvement (full-period maxDD flat-to-slightly-worse; individual episodes move <0.5 pt either way).
- **Skill vs beta:** no detectable skill (alpha CI spans zero in every cell, and alpha *falls* as the tilt grows) and no extra beta (realized β flat vs baseline). The pre-registered "just more beta, buy SPY" null is *undershot* — it is "less return at the same beta; hold the existing sleeve."
- **The thesis:** the bull-regime headline is negative in every cell — the tilt does not help in the bulls.
- **Hard gates (the "is it real" tests):**
  - **OOS both-halves — no benefit exists to survive (fail), and the drag is worse out-of-sample.**
  - **Plateau — no positive plateau (fail as an adopt signal); the negative result is contiguous and monotone (robust).**
  - **Fund-swap — finding survives the IJH/IJR → VO/VB swap unchanged (robust).**

Per rule #1 and the pre-registration, a result that cannot clear the hard gates is not a worse trade-off to negotiate — it is not a real edge. This report presents the numbers and the exchange rate; it makes **no** adopt/reject call. The study flag remains **default-OFF** regardless.

---

### Artifacts
- Per-cell + daily-series CSVs: `backtester/output/s0_equity_tilt/` (`grid_metrics.csv`, `beta_attribution.csv`, `regime_split.csv`, `oos_halves.csv`, `episodes.csv`, `gfc2008.csv`, `per_cell.csv`, `study_summary.json`, and `daily_*.csv` NAV/return series for baseline + all 18 tilt cells).
- Regression guard: backtester suite **444 passed** (baseline 444) after the study; no production code changed (study driven entirely by runtime knob assignment).
