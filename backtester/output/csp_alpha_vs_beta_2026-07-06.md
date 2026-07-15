# CSP — ALPHA vs BETA — RESULTS + VERDICT

**Run:** 2026-07-06  |  **Runtime:** 104s  |  pre-registered in `docs/PREREG_CSP_alpha_vs_beta_2026-07-06.md` (committed BEFORE this run).

## VERDICT (lead)

### **BETA — CSP return is explained by equity beta, not VRP alpha (REFUTED-as-alpha)**

Headline: ATM cash-secured put, 45 DTE, hold-to-expiry, weekly ladder, f=0.5.

- **Annualized alpha intercept: -6.79%** (bootstrap 95% CI [-15.67%, +3.75%], SPANS 0), **beta 0.551**, R² 0.308, alpha t-stat -1.07.
- The book behaves like **0.55×** the SPX daily return; R²=0.31 of its variance is explained by SPX alone.
- Book time-average dollar-delta $1,197,909 on $2,525,250 reserved capital => delta-matched SPX leverage 0.474×.
- +$718k benchmark reproduction (CSP45 f0.5 total P&L): **$718,071**.

### Five pre-registered pass criteria

1. **Positive alpha, CI excl. 0, across mid->0.50 band:** FAIL — band alphas [-0.0643, -0.0661, -0.0679], headline CI [-15.67%,+3.75%].
2. **CSP not dominated by delta-matched SPX (Sharpe & Sortino):** FAIL — CSP Sharpe -0.00/Sortino -0.00 vs delta-matched SPX Sharpe 0.60/Sortino 0.74.
3. **OOS positive alpha in BOTH halves:** FAIL — train -2.71% (n=624), test -9.40% (n=1126).
4. **Plateau across DTE x fill:** FAIL — 0% of DTE×fill cells have positive alpha.
5. **Full-cycle alpha positive (crisis survivability):** FAIL — annualized alpha -6.79%.

## Beta regression grid (alpha annualized) — DTE × fill

| dte | f | n_days | total P&L $ | alpha_ann | 95% CI | beta | R² | t(alpha) | CSP Sharpe | CSP Sortino | CSP maxDD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 30 | 0.0 | 1750 | 449,206 | -1.02% | [-17.47%,+18.80%] | 0.523 | 0.125 | -0.10 | 0.18 | 0.26 | -0.332 |
| 30 | 0.25 | 1750 | 440,365 | -1.17% | [-17.68%,+18.69%] | 0.526 | 0.126 | -0.11 | 0.18 | 0.26 | -0.333 |
| 30 | 0.5 | 1750 | 431,523 | -1.31% | [-17.89%,+18.53%] | 0.529 | 0.127 | -0.12 | 0.17 | 0.25 | -0.333 |
| 30 | 1.0 | 1750 | 413,841 | -1.61% | n/a | 0.535 | 0.129 | -0.15 | 0.16 | 0.24 | -0.335 |
| 45 | 0.0 | 1750 | 734,821 | -6.43% | [-15.24%,+4.18%] | 0.545 | 0.306 | -1.02 | 0.01 | 0.01 | -0.273 |
| 45 | 0.25 | 1750 | 726,446 | -6.61% | [-15.46%,+3.97%] | 0.548 | 0.307 | -1.04 | 0.01 | 0.01 | -0.273 |
| 45 | 0.5 | 1750 | 718,071 | -6.79% | [-15.67%,+3.75%] | 0.551 | 0.308 | -1.07 | -0.00 | -0.00 | -0.274 |
| 45 | 1.0 | 1750 | 701,321 | -7.15% | [-16.12%,+3.53%] | 0.556 | 0.310 | -1.12 | -0.02 | -0.02 | -0.275 |

## Benchmark comparison (headline book, daily returns on reserved capital)

| arm | Sharpe | Sortino | maxDD | ann. return | ann. vol | total return |
|---|---|---|---|---|---|---|
| CSP 45DTE f0.5 | -0.00 | -0.00 | -0.274 | -0.04% | 20.10% | -13.07% |
| Delta-matched SPX (0.474×) | 0.60 | 0.74 | -0.172 | +5.81% | 9.62% | +45.00% |
| Capital-matched SPX (1:1) | 0.60 | 0.74 | -0.339 | +12.26% | 20.27% | +102.96% |

_Delta-matched SPX = long SPX sized so its dollar-delta equals the CSP book's time-average dollar-delta, held on the same reserved capital (leverage = avg dollar-delta / avg reserved capital). This is the intuitive 'just hold the matched index exposure' benchmark. Capital-matched = full reserved capital in SPX 1:1 (the CSP is expected to trail this on raw return in a bull market — that alone is not a refutation; risk-adjusted underperformance is)._

## OOS split (headline) — alpha must be positive in BOTH halves

| half | window | n_days | alpha_ann | beta |
|---|---|---|---|---|
| train | 2018-06→2021-12 | 624 | -2.71% | 0.481 |
| test | 2022-01→2026-07 | 1126 | -9.40% | 0.627 |

## Per-crisis (headline CSP daily-return compounded total over each window)

| window | n_days | CSP total return |
|---|---|---|
| 2018Q4 | 63 | -4.02% |
| COVID | 62 | +9.05% |
| 2022 | 250 | -10.17% |
_Short puts are expected to bleed here; the full-cycle alpha is the question, not any single crisis._

## Data window & coverage

- Trading days in window: 2111 (2018-06-01..2026-07-03).
- SPX daily-return series: 2030 days (2018-06-04..2026-07-02), source = warehouse `underlying_price` (continuous across the NBBO blackout since only quotes, not the underlying, were zeroed).
- Weekly ladder entries: 423; genuinely quoted: 319; blackout-skipped weeks: 104 (2020-08-13→2021-12-31 NBBO blackout).
- CSP book daily marks reuse the S7 honest-fill helper (_buy_price on the put leg) and the shared price-map cache; strictly causal (a day's mark reads only that day's quotes). Corruption handled via the S7 clean-delta path.

## Method notes

- Daily CSP book mark-to-market: equity(d) = Σ premium collected − Σ current buy-back liability at fill f; expiry marks use settled intrinsic. Daily return = Δequity ÷ reserved capital (Σ K·100 over open puts that day).
- Regression r_csp = alpha + beta·r_spx + e on aligned daily returns; alpha annualized ×252. 95% CI via stationary block bootstrap (block≈20d, 2000 resamples, seed 20260706).
- No parameter tuned to the data. Warehouse read-only. Frozen config untouched.
