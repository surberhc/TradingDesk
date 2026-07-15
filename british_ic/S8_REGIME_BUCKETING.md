# S8 -- REGIME-BUCKETED REAL-FILLS RESULT

Rebuild of the 2026-07-10 same-session finding (previously only narrated in the conductor log, not committed as a script -- this file closes that gap so the numbers are reproducible, not just asserted).

## Method

- Real daily S8 P&L: `alpha_vs_beta_daily_series.csv` (`pnl_s8`, 236 rows), already sanity-gated against the published +$138,982 / +$42,765 headline before this script trusts it.
- Regime label: S0's existing, FROZEN regime engine (`strategies/strategies/parts/regime.py` -- `market_health_score()` + `apply_hysteresis()`), called exactly the way `dailyreport/eod_report.py:build_s0_regime()` does (same `data_loader.load_prices/load_vix/load_hy_oas` calls, same CREDIT_PROXY denominator lookup). No new thresholds introduced for this test.
- VIX source: cboe_vix. HY OAS source: fred_BAMLH0A0HYM2.
- Zero synthetic execution: no strike selection, no fill model, no spot estimation -- purely a join of two already-real series by date.

## Result

| Regime | Days | S8 total P&L | S8 avg/day | S8 win rate |
|---|---|---|---|---|
| RiskOn | 101 | $69,077 | $684 | 59.4% |
| RiskOnNarrowing | 89 | $64,893 | $729 | 59.6% |
| CapitalPreservation | 10 | $8,357 | $836 | 50.0% |
| Defensive | 12 | $6,104 | $509 | 41.7% |
| Caution | 24 | $-9,450 | $-394 | 41.7% |

Profitable in 4 of 5 regime buckets, covering 236 of 236 days (100%).

## Caveat, stated plainly

The extreme-regime buckets are thin (single digits to low teens of days). This is NOT proof S8 survives a genuine multi-year bear market or a 2008-style event -- it is evidence the edge isn't fragile to the regime mix that actually occurred within the one year real fills exist for. A narrower claim than multi-year coverage would be, but one that depends on no synthetic execution.
