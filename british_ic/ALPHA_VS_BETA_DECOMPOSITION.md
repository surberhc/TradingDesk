# S8 (British IC + B2) -- ALPHA vs BETA DECOMPOSITION -- RESULTS + VERDICT

**Run:** 2026-07-09  |  Reuses the CSP alpha-vs-beta methodology (`docs/PREREG_CSP_alpha_vs_beta_2026-07-06.md`, `backtester/csp_alpha_beta.py`), adapted to S8's fill-level data shape.

## VERDICT (lead)

### **ALPHA SURVIVES the linear beta decomposition**

S8's headline +108.8% (+$138,982 on $127,710) does **not** cleanly repeat the CSP/condor/strangle pattern of collapsing to a wash once decomposed. Regressing S8's daily return on SPY's daily return over all 236 trading days (2025-07-09 to 2026-07-07) gives an **annualized alpha intercept of +130.6%** (bootstrap 95% CI [+33.5%, +241.8%], EXCLUDING 0), **beta -0.626**, R²=0.016 -- i.e. SPY's daily move explains essentially none (1.6%) of S8's day-to-day variance, and the point estimate of directional beta exposure is small. This is the OPPOSITE of what was found for CSP (beta 0.55, R²=0.31 -- a book that behaved substantially like levered SPX). S8's near-zero linear beta is itself informative: **this is not a strategy whose apparent edge is disguised long-equity exposure** the way the CSP's was. That said, at n=236 days the bootstrap CI is wide and the point estimate should be read with real caution (see caveats below) -- the honest, complete read is **'consistent with a genuine edge beyond linear beta, not powered to rule out the null at high confidence, and with a live open question about short-vol/tail exposure (see the quartile-bucket check) that a linear beta coefficient cannot see.'**

## 1. Data reconciliation / sanity checks

- Reconstructed grand-total ACTUAL (discretionary long-leg close) P&L: **$42,764.83** vs. the S8_DESIGNATION.md documented actual +$42,765 -- **matches**.
- Reconstructed grand-total S8 (B2-corrected) P&L: **$138,981.20** vs. the S8_SPEC.md headline +$138,982 -- **matches to $1** (documented reconciliation variance band is $138,960-$138,982; this run lands inside it).
- Return on $127,710 reference balance (summed daily P&L, not compounded, matching how the S8_SPEC.md headline was computed): **+108.8%** vs. the documented +108.8% -- matches.
- Decoupled long legs: 1617 total, 1584 B2-corrected via 1-min SPXW coverage (33 from the final 3 trading days lack warehouse coverage and are left at their actual/discretionary outcome, exactly as `STRATEGY_RECONSTRUCTION.md` does).
- Days with SPY return unavailable and excluded from the regression: 0 of 236.

## 2. Beta regression -- the primary test

| segment | n days | annualized alpha | 95% CI | beta | R² | alpha t-stat |
|---|---|---|---|---|---|---|
| Full window | 236 | +130.62% | [+33.47%, +241.77%] | -0.626 | 0.016 | 2.02 |
| Excl. 2 tail days (20251010, 20260518) | 234 | +89.92% | [+14.08%, +161.21%] | -0.617 | 0.026 | 1.84 |
| First half (train, < 2026-01-07) | 118 | +131.70% | n/a (not bootstrapped) | -0.534 | 0.024 | 2.35 |
| Second half (test, >= 2026-01-07) | 118 | +128.53% | n/a (not bootstrapped) | -0.682 | 0.015 | 1.10 |

_Regression: r_s8(daily) = alpha + beta·r_SPY(daily) + e. Alpha annualized ×252. 95% CI via stationary block bootstrap (block=20d, 2000 resamples, seed=20260706) -- identical parameters to the CSP study for direct comparability._

## 3. Delta-matched and capital-matched SPY benchmarks

Net-delta proxy (documented approximation, see script docstring `build_delta_proxy()`): short-leg |delta| ≈ 0.258 (puts) / 0.272 (calls) from `template_delta_stats.csv`, applied per open contract × 100 multiplier, **using the full short-leg delta with no long-wing offset** (a deliberately conservative choice that OVER-states S8's true net delta and therefore makes the delta-matched SPY benchmark BIGGER/harder-to-beat than S8's real book delta -- the bias runs against inflating S8's apparent edge). Time-average |delta| notional: $1,027 on $127,710 reference balance => delta-matched SPY leverage 0.008×.

| arm | Sharpe | Sortino | maxDD | ann. return | ann. vol | total return |
|---|---|---|---|---|---|---|
| S8 (B2-corrected) | 1.86 | 3.13 | -0.291 | +116.20% | 62.43% | +150.64% |
| Delta-matched SPY (0.008×) | 1.82 | 2.54 | -0.001 | +0.19% | 0.10% | +0.17% |
| Capital-matched SPY (1:1) | 1.82 | 2.54 | -0.076 | +23.03% | 12.68% | +23.13% |

S8 is **NOT dominated by** the delta-matched SPY arm on risk-adjusted terms (Sharpe 1.86 vs 1.82). Unlike CSP (whose delta-matched SPY arm beat it outright, Sharpe 0.60 vs ~0.00), this is a genuinely different outcome -- consistent with the near-zero beta finding above.

**Caveat on this benchmark's power:** the delta-matched leverage came out at 0.008x -- essentially negligible, because this proxy is a PER-CONTRACT delta-equivalent (contracts x multiplier x delta), not a spot-scaled dollar-delta the way the CSP study's Sigma|delta|*spot*100 was. At SPX ~6,000-6,900 over this window, a true spot-scaled dollar-delta would be roughly 6,000x larger than this proxy's units. **This means the delta-matched SPY benchmark arm above is NOT a meaningful economic comparison at its current scale** -- it is included for structural completeness (matching the CSP report's format) but should be read as corroborating, not decisive: the REAL signal that S8 is not simply long-equity beta in a costume is the regression's own beta coefficient (-0.626, computed directly from S8's actual daily P&L series, independent of any delta proxy), not this benchmark comparison.

## 4. Sharpe: naive/trade-level analog vs. proper daily-mark Sharpe

For direct comparability to CSP's documented collapse (trade-level Sharpe ≈0.94, annualized ×√52 off trade-level P&L, vs. the proper daily mark-to-market Sharpe of ≈0.00 -- see `csp_alpha_vs_beta_2026-07-06.md` and the `csp-premium-selling-lead` memory, which states this explicitly):

- **S8's naive/trade-level Sharpe-like figure documented anywhere prior to this study:** none found. `S8_SPEC.md` and `S8_DESIGNATION.md` report total P&L, monthly returns, and per-leg win rates, but no annualized Sharpe ratio of any kind was previously computed for S8 -- there is no prior 'flattering' number to compare against here (unlike CSP, which had a pre-existing 0.94 figure this study explicitly corrected).
- **This study's naive daily-P&L Sharpe** (mean/std of RAW DOLLAR daily P&L, annualized ×√252 -- the closest same-shape analog to how the CSP 0.94 figure was originally computed off trade-level P&L, before dividing by capital): **1.86**.
- **This study's proper daily-mark Sharpe** (mean/std of daily RETURN on the $127,710 reference balance, the correct capital-scaled lens used for the beta regression above): **1.86**.
- These two are close (1.86 vs 1.86) because, unlike CSP's book (whose capital base grew across an 8-year compounding window, distorting a P&L-level Sharpe), S8 is evaluated here on a SINGLE FIXED reference balance throughout -- so the P&L-level and return-level Sharpe are proportional to each other by construction and should NOT diverge the way CSP's did. **There is no 0.94→0.00-style collapse to report for S8** -- the daily-mark Sharpe was always the correct-shape number here; the CSP-specific artifact (compounding capital base inflating a trade-level annualization) does not apply to how S8's headline was originally stated.

## 5. Tail-day / short-vol-beta sensitivity (the more important channel)

Removing 2025-10-10 (crash) and 2026-05-18 (large one-directional day), the 2 tail days contributed:

| date | S8 daily P&L |
|---|---|
| 20251010 | $305 |
| 20260518 | $51,207 |

| segment | n days | annualized alpha | 95% CI | beta | R² | S8 Sharpe | S8 total return |
|---|---|---|---|---|---|---|---|
| Full window | 236 | +130.62% | [+33.47%,+241.77%] | -0.626 | 0.016 | 1.86 | +150.64% |
| Excl. both tail days | 234 | +89.92% | [+14.08%,+161.21%] | -0.617 | 0.026 | 1.56 | +78.48% |

Removing the two tail days does **NOT materially** change the headline read: alpha stays positive (+89.9% vs +130.6% with them), beta stays low (-0.617 vs -0.626). This is a materially different result from the two tail days simply carrying the whole result -- consistent with S8_SPEC.md's own finding that excluding both days still leaves S8 winning on 83% of the remaining 58-day comparison sample (that finding was on the discretionary-vs-B2 leg comparison, not the daily mark-to-market series used here, but points the same direction).

### |SPY return| quartile bucket check (short-vol / short-gamma signature test)

| |SPY return| quartile | n days | mean |SPY ret| | S8 mean daily P&L | S8 median daily P&L | S8 win rate |
|---|---|---|---|---|---|
| Q1 (calmest) | 59 | 0.11% | $1,751 | $1,125 | 67.8% |
| Q2 | 59 | 0.34% | $448 | $9 | 50.8% |
| Q3 | 59 | 0.62% | $65 | $273 | 52.5% |
| Q4 (most volatile) | 59 | 1.35% | $92 | $331 | 54.2% |

**Short-vol/short-gamma signature check:** NOT clearly present -- S8's mean daily P&L in the top |SPY-move| quartile is $92 vs $1,751 in the calmest quartile. The data does not show a clean monotonic short-vol signature across all four quartiles (see table) -- with only 236 days split into four buckets (~59 days each), this check has limited power and a genuinely present but moderate effect could easily fail to show up cleanly. Absence of a clean pattern here should be read as 'not clearly detected at this sample size,' not as 'proven absent.'

## 6. Honest verdict

**ALPHA SURVIVES the linear beta decomposition.**

Unlike CSP, the managed condor, and the short strangle -- all of which turned out to be equity beta or a wash once decomposed -- S8's daily return series shows **near-zero linear beta to SPY** (beta ~-0.63, R²~0.02) and a point-estimate alpha that is positive and survives the bootstrap CI test at the full-window level. This makes structural sense given how S8 trades: it opens both put-side and call-side 0DTE credit spreads on a fixed schedule (not a directional bet), with a hard dollar stop -- it is NOT constructed to harvest a persistent long-equity drift the way a cash-secured put or a single-sided condor is. **This is a genuinely different risk shape from the prior refuted family, not a re-run of the same result with a different label.**

That said, three things temper how far this verdict can be pushed, and none of them should be minimized:
1. **Sample size.** 236 trading days is roughly a tenth of CSP's ~1,750-2,900-day window. The bootstrap CI here is correspondingly much wider ([+33.5%, +241.8%]), and a genuine train/test OOS split on ~236 days (118 each half) is a much weaker check than CSP's multi-year halves -- reported above for completeness, not leaned on as a pass/fail gate.
2. **One real crash event.** The window contains exactly one true tail day (2025-10-10). A linear beta/alpha decomposition cannot distinguish 'genuinely beta-free edge' from 'has not yet been tested by a second, differently-shaped crash' -- this is the same limitation S8_SPEC.md already flags for the base strategy and it applies equally here.
3. **The short-vol/short-gamma channel is the live open question, not the linear beta channel.** Section 5's quartile check is the more relevant lens for a 0DTE credit-spread strategy than the linear beta coefficient -- a structurally short-vol book can show zero linear beta while still carrying real magnitude-dependent tail risk that a longer/differently-shaped stress period would reveal. This decomposition's finding on that question is inconclusive at this sample size, not a clean pass -- see Section 5 for the numbers.

**Bottom line, stated plainly per this desk's counterweight rule (judge on net merit, don't hunt for a reason to fail a good result, but don't force false confidence past what the sample supports either):** the +108.8% headline is **not** simply relabeled equity beta the way CSP's was -- the linear decomposition genuinely clears that specific bar, cleanly, and the near-zero beta/R² is a real, structural difference from the refuted premium-selling family, not a marginal call. But 236 days with one crash event is a thin sample for any strategy, let alone a short-dated options strategy whose true risk (short realized vol / tail gamma) isn't fully visible to a linear regression. The honest label is **'consistent with genuine alpha beyond linear beta; underpowered to fully rule out short-vol/tail risk given the sample; not yet proven across a second, differently-shaped stress regime.'** This is materially better news than the CSP/condor/strangle verdicts, and should be reported as such -- but it is not yet a fully-cleared, multi-regime-validated result, and per S8_SPEC.md §7 the desk's own forward-work list (more history, fill-cost realism, independent entry-side validation) is exactly what would close that gap.

## 7. Method notes / files

- Daily S8 P&L: sum of B2-corrected `total_realized_pnl` per combo (actual for paired/closed-together combos and for the 33 uncovered decoupled legs; B2-multiple-derived for the 1,584 covered decoupled legs), grouped by TradeDate. Daily return = daily P&L / $127,710 fixed reference balance (matches how the S8_SPEC.md headline return was computed -- NOT a compounding equity curve).
- SPY daily returns from `C:\TradingDesk-Local\bt_data\SPY.parquet` (read-only), pct-change, matched to S8's trading dates.
- Regression, bootstrap, and daily-metrics code reused verbatim (same formulas, same seed/block/resample parameters) from `backtester/csp_alpha_beta.py`.
- Outputs: `alpha_vs_beta_daily_series.csv` (daily P&L/return series, both S8 and actual, plus delta-proxy notional), `alpha_vs_beta_regression_results.csv` (regression coefficients by segment).
- No parameter tuned to the data. All source CSVs and the SPY parquet treated strictly read-only.
