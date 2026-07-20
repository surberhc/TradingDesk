# Phase-2 Leadership Proxy — PRELIMINARY Subset Calibration

**Date:** 2026-06-30
**Status:** Preliminary / inconclusive. **config.py NOT changed.**

## Scope & method

- **Zero new Tiingo API calls.** Calibration reads only the on-disk EOD cache:
  `phase2_feed/data/cache/<TICKER>.csv` (59 tickers, columns `date,close,high,low`).
- **Reuses the existing math.** `calibrate.py` imports and calls
  `breadth.per_ticker_signals` (point-in-time, on a date-truncated series) plus the
  same normalization used by `breadth.leadership_proxy` and the same cutoff logic as
  `breadth.classify_regime`. No formula was re-implemented differently.
- **Point-in-time:** for each InvesTech `Issue Date`, every cached series is truncated
  to rows dated `<= issue date`, then the four breadth metrics are aggregated exactly as
  `compute_breadth()` does.
- **Ground truth:** `_dataset/InvesTech_Signals.csv`. Direction mapping —
  NLC `< 0` or regime text "Distribution" → **bearish**; NLC `> 0` / "Selling Vacuum" →
  **bullish**; NLC `== 0` / "Neutral" → **neutral**.

## Coverage (honest)

- Cache window: **2025-05-06 → 2026-06-29** (~288 trading rows per name).
- The 200-day MA needs ~200 prior trading rows, so a date is only computable from
  ~2026-02-20 onward.
- **Computable issue dates: 5 of 42.** Skipped for thin lookback: **37**.
  (All 5 computable dates had the full 59-name universe eligible.)

## Comparison table (computable dates)

| Issue Date | proxy_score (base) | proxy_regime (base) | NLC Value | NLC Regime | InvesTech dir | base agree? | pct50 | pct200 | net_hl% | ad% (1-day) |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-02-20 | 60.2 | bullish | — | Distribution (AI Index -29%) | bearish | ✗ | 55.9 | 66.1 | +10.17 | +32.2 |
| 2026-03-20 | 36.3 | bearish | -14.0 | Distribution -14 reappeared | bearish | ✓ | 16.9 | 47.5 | -1.69 | -45.76 |
| 2026-04-17 | 59.6 | neutral | — | Selling Vacuum reemerged | bullish | ✗ | 61.0 | 55.9 | +5.08 | +45.76 |
| 2026-05-15 | 46.1 | neutral | 2.7 | SV +2.7 stalling / Dist 0 | bullish | ✗ | 45.8 | 52.5 | -1.69 | -32.2 |
| 2026-06-19 | 50.9 | neutral | -4.0 | Distribution -4.0 | bearish | ✗ | 52.5 | 55.9 | -3.39 | -6.78 |

**Baseline agreement: 1 / 5.**

## Why no parameter set was adopted

The core problem is not threshold placement — it is that **contemporaneous large-cap
breadth and InvesTech's NLC are roughly anti-correlated to uncorrelated on this sample**:

- The one **bearish** date with the strongest proxy breadth is **2026-02-20**
  (pct50=55.9, pct200=66.1, net new-highs positive) — yet InvesTech reads Distribution.
- The two **bullish** dates (2026-04-17, 2026-05-15) have weak/negative breadth in the
  cache (net new-lows on 05-15, 1-day A/D = -32) — yet InvesTech reads Selling Vacuum.

A single-day advance/decline tilt (`ad_pct`) also injects large noise: it swings
±45 points purely on the last session's up/down counts, which is not a regime signal.

I grid-tested several re-weightings (net-lows-heavy + A/D up-weight; trend+leadership
with A/D dropped; 50/200-only; raised cutoffs). The best, an A/D-and-new-lows-heavy set
with bull≥62 / bear≤52, reached **2 / 5** — but only by labeling almost everything
bearish. On a sample that is **3 bearish / 2 bullish**, a trivial "always bearish" guess
also scores **3 / 5**, i.e. *better* than the tuned set. The 2/5 is a bearish bias on a
bear-heavy sample, not genuine directional skill. Because no parameter change produces
**honest** alignment (and several would overfit 5 noisy points), **config.py is left
unchanged.**

### Tested (rejected) parameter sets

| Set | weights (50/200/nlhl/ad) | cutoffs (bear/bull) | agree |
|---|---|---|---|
| baseline (current) | .25/.25/.30/.20 | 40 / 60 | 1/5 |
| new-lows + A/D heavy | .15/.15/.45/.25 | 52 / 62 | 2/5* |
| trend+leadership, no A/D | .30/.30/.40/.00 | 50 / 58 | 1/5 |
| leadership heavy, no A/D | .20/.25/.55/.00 | 50 / 58 | 1/5 |
| 50/200 only | .40/.40/.20/.00 | 50 / 58 | 1/5 |

\* worse than the 3/5 "always bearish" naive baseline → not adopted.

## Recommendation

1. **Do not tune on 5 points.** Re-run this calibration once the cache holds ≥2 years of
   history so the full ~42 issue dates (2023-2026, spanning real Selling-Vacuum and
   Distribution regimes) are computable.
2. **Replace the 1-day A/D term** with a smoothed A/D-line slope (e.g. cumulative A/D vs
   its own 20-50d trend), or drop `ad_pct` from the composite — a single session's
   advances/declines is noise, not leadership.
3. The structural gap likely matters more than weights: InvesTech's NLC is **full-exchange
   NYSE+NASDAQ** breadth (every issue, incl. small/micro-caps and rate-sensitive names),
   forward-leaning, and proprietary. This proxy is **S&P-500 large-cap only**, which can
   stay healthy while broad-market downside leadership deteriorates — exactly the
   divergence seen on 2026-02-20. Wiring true exchange breadth (the existing
   `fetch_exchange_breadth` stub) is probably necessary before threshold tuning is
   meaningful.

## Limitations (plain)

- **Tiny sample:** only 5 of 42 issue dates computable; not statistically meaningful.
- **Recent dates only:** all 5 fall in 2026-02 … 2026-06; no 2023-2024 history covered.
- **S&P-500-only breadth:** large-cap subset, not InvesTech's full-exchange universe.
- **No true exchange breadth:** the NYSE/NASDAQ A/D + NH-NL sub-score is still a stub.
- **59-name cache**, not the full ~503-name S&P 500 — even the large-cap breadth is a sub-sample.
- **1-day A/D noise** dominates score swings on single snapshot dates.

Outputs: `calibration_results.csv` (full 42-row table, scores on the 5 computable rows),
`calibrate.py` (reproducible, no-network).
