# Top-15 S&P Leaders Synthetic Equity Project
## Master Technical Research, Data, Backtest, and Execution Specification

**Document status:** Definitive project kickoff specification  
**Version:** 1.0  
**Date:** September 5, 2026  
**Primary research window:** September 2021 through September 2026  
**Secondary modern-regime window:** January 2023 through September 2026  
**Primary options data source:** ThetaData  
**Primary objective:** Determine whether a concentrated Top-15 S&P 500 leader portfolio can be implemented more efficiently with defined-risk long-dated call exposure, then use the released capital to increase return on portfolio equity without destroying the strategy's crash behavior.

---

# 1. Executive Summary

This project has two linked components:

1. **Security-selection engine:** Own only the largest S&P 500 economic issuers rather than all 500 constituents.
2. **Capital-efficiency engine:** Replace some or all stock exposure with long-dated, high-delta calls so the portfolio can obtain similar or greater equity exposure with less capital committed.

The central thesis is not that options create free alpha. A true synthetic long created with a long call plus short put largely recreates stock economics and also recreates stock downside. The research focus is therefore **long-call stock replacement and related defined-risk call structures**, where loss is limited to premium paid while capital can be redeployed.

The portfolio-level question is:

> Can the strategy obtain superior CAGR, return on equity, and/or return per unit of permanent capital while keeping drawdown, premium-at-risk, and crash losses within acceptable limits?

The project must not optimize to the oldest available options history simply because it exists. The options market has changed materially through electronic market making, retail participation, weekly and same-day expirations, OPRA message growth, algorithmic execution, and liquidity concentration in mega-cap underliers. Accordingly:

- **2021–2026 is the primary optimization regime.**
- **2023–2026 is the current-market confirmation regime.**
- Older periods are secondary stress/reference periods only.
- The stock-selection study can use longer history because the question is different.

The most important design constraint is:

> **Do not download the entire options market.**

Data acquisition will be driven by a precomputed quarterly **issuer eligibility manifest**. Full option-chain snapshots are needed only on decision dates and limited diagnostic dates. Once a contract is selected, only that contract and limited surrounding reference contracts are followed through time.

---

# 2. Frozen Findings From Phase I: Top-15 Equity Strategy

## 2.1 Original research question

Would an aggressive portfolio holding only the largest S&P 500 constituents produce a better risk/return result than owning the entire S&P 500?

The study compared concentration levels including:

- Top 10
- Top 15
- Top 20
- Top 25
- Top 30
- Top 40
- Top 50

and evaluated the role of rebalance cadence and weighting methodology.

## 2.2 Key findings

The historical evidence showed:

- Mega-cap concentration produced substantially higher recent-period returns than the broad S&P 500.
- Top 10 historically generated the strongest raw return in several recent windows, but with significantly greater single-name concentration.
- Top 20 retained much of the excess-return effect with meaningfully improved diversification.
- The **concentration/diversification elbow appears near 15–20 economic issuers**.
- Adding substantially more than approximately 20–30 names increasingly diluted the intended mega-cap leadership exposure.
- The benefit of cap weighting became materially stronger in the modern era than in earlier periods.
- Equal weighting was more competitive in earlier regimes but increasingly diluted the mega-cap leadership effect in recent regimes.
- Capping extreme position weights preserved most of the capitalization effect while reducing tail concentration better than fully equalizing the portfolio.

## 2.3 Chosen base architecture

The working equity control strategy is:

### CONTROL-15

- **Universe:** Point-in-time S&P 500 constituents
- **Selection:** 15 largest economic issuers by float-adjusted market capitalization
- **Share classes:** Multiple share classes count as one economic issuer for selection/concentration purposes
- **Weighting:** Market-cap proportional
- **Maximum issuer weight:** Test 12% and 15%; freeze winner before final production implementation
- **Reconstitution:** Quarterly
- **Rebalance:** Quarterly
- **Dividends:** Reinvested
- **Leverage:** None
- **Sector caps:** None
- **Benchmark:** S&P 500 Total Return
- **Primary portfolio comparison:** CONTROL-15, not SPY alone

## 2.4 Why economic issuers matter

GOOG and GOOGL, for example, are separate ticker lines but one economic company. A portfolio that counts them as two of 15 diversification slots would overstate diversification.

Required rules:

- Aggregate economic-company market capitalization before ranking.
- One issuer occupies one Top-15 slot.
- If implementation uses multiple classes, combined exposure is subject to the issuer cap.
- Maintain a permanent `issuer_id` separate from the ticker.

---

# 3. Frozen Research Philosophy

These decisions are frozen unless new evidence specifically invalidates them.

## 3.1 Regime weighting

Do not optimize on one full-history CAGR.

Primary relevance hierarchy:

1. **2021–2026:** primary options optimization
2. **2023–2026:** current-market confirmation
3. **2020:** crash/out-of-regime diagnostic
4. **Pre-2020 options data:** exploratory/stress only; never the primary parameter selector

## 3.2 Why modern data receives the most weight

The modern options market differs structurally from older markets because of:

- algorithmic/electronic market making,
- massive OPRA message growth,
- much higher retail participation,
- weekly/daily expiration proliferation,
- 0DTE activity,
- tighter concentration of options volume in the largest underliers,
- improvements in quote frequency and execution technology.

This does **not** mean old data is useless. It means old option quotes should not determine modern delta, DTE, roll, liquidity, spread, or leverage choices.

## 3.3 Primary research objective

Optimize **portfolio-level capital efficiency**, not option-level return.

The objective function must consider:

- CAGR,
- return on NAV,
- return on deployed option premium,
- return on permanent capital,
- maximum drawdown,
- expected shortfall,
- premium-at-risk,
- crash survival,
- execution friction,
- tracking to intended delta exposure,
- and ability to redeploy released capital.

---

# 4. Scope Guardrails — Preventing Research Rabbit Holes

The following are **IN SCOPE** for Version 1:

- Top-15 S&P leader portfolio
- Long-dated long calls as stock replacement
- Deep-ITM/high-delta calls
- Defined-risk call structures if required by the data
- 1.00x to 2.00x initial delta-adjusted portfolio exposure
- 12% and 15% issuer caps
- Capital released by stock replacement
- T-bills/cash as the neutral capital-reserve benchmark
- Additional Top-15 exposure as alpha redeployment
- Later: one separately defined independent alpha sleeve
- Historical NBBO bid/ask execution modeling
- Greeks, IV, OI, volume, DTE, spread and liquidity analysis
- Quarterly selection and rebalance rules
- Rule-based roll logic

The following are **OUT OF SCOPE** for Version 1:

- 0DTE strategies
- day trading
- gamma scalping
- short-volatility programs
- naked short puts
- naked short calls
- covered calls as an income strategy
- collars as a primary strategy
- routine protective-put hedging
- VIX trading
- intraday market making
- dispersion trading
- earnings-event options trading
- discretionary technical-entry timing
- momentum overlays
- sector rotation inside the Top-15 experiment
- machine-learning optimization
- dynamic changing of N unless the fixed Top-15 strategy fails
- tax optimization
- portfolio margin optimization
- broker-specific margin arbitrage
- futures substitution
- crypto options
- index-option substitution for the entire basket

Any future addition must be entered into a formal **Change Log** with a stated reason and cannot silently enter the backtest.

---

# 5. Research Questions — Required Answers

The project is complete only when it answers these questions.

## 5.1 Equity control questions

1. Does Top-15 outperform the S&P 500 on a rolling basis?
2. Does a 12% or 15% issuer cap produce the better modern risk/return result?
3. How concentrated is the portfolio by effective number of names?
4. How much return is generated by the top 3, top 5, and top 10 holdings?
5. Does the strategy depend excessively on one issuer or sector?

## 5.2 Synthetic stock-replacement questions

1. What call delta best balances stock participation and capital efficiency?
2. What initial DTE produces the best combination of:
   - theta efficiency,
   - spread quality,
   - liquidity,
   - capital efficiency,
   - and tracking?
3. When should calls be rolled?
4. Does fixed-DTE rolling outperform delta-triggered rolling?
5. How much premium is required to create $1 of delta-adjusted equity exposure?
6. How does this relationship vary by issuer and volatility regime?
7. How much performance drag comes from:
   - time value,
   - spreads,
   - IV,
   - dividends not received,
   - and roll friction?
8. Does the long-call portfolio improve crash behavior at equal initial delta exposure?
9. At what leverage does additional notional destroy that crash advantage?
10. What maximum premium-at-risk should the portfolio permit?

## 5.3 Capital redeployment questions

After identifying the best stock-replacement engine:

1. Is it better to keep released cash in T-bills?
2. Is it better to use released capital to increase Top-15 notional?
3. What is the optimal initial gross delta exposure:
   - 1.00x,
   - 1.25x,
   - 1.50x,
   - 1.75x,
   - 2.00x?
4. Is a hybrid structure superior:
   - e.g. 1.25x Top-15 exposure plus a separate alpha sleeve?
5. How much dry powder must remain available to re-delta after large declines?

---

# 6. Candidate Option Architectures

## 6.1 Architecture A — Long-dated high-delta calls

This is the primary candidate.

For each Top-15 issuer:

- buy a long-dated call,
- target a specified delta,
- size by delta-adjusted notional,
- do not size by premium dollars alone.

Target contract grid:

- delta: 0.75 / 0.80 / 0.85 / 0.90 / 0.95
- initial DTE: 365 / 540 / 730 days
- roll DTE: 90 / 180 / 270 days

## 6.2 Architecture B — True synthetic long

Long call + short put, same strike and expiration.

Purpose:

- benchmark only.

Reason it is not preferred:

- it largely recreates stock downside,
- the short put eliminates much of the defined-loss property,
- it does not solve the crash-risk objective.

Use Architecture B only to measure financing/parity differences and as a reference implementation.

## 6.3 Architecture C — Defined-risk call structure

Explore only if plain long calls show unacceptable:

- time-value drag,
- capital usage,
- or liquidity.

This architecture must remain net long, defined risk, and consistent with the stock-replacement objective.

Do not introduce short downside exposure.

---

# 7. Core Option Sizing Rules

## 7.1 Delta-adjusted exposure

For a call:

`Delta Adjusted Exposure = Contracts × 100 × Underlying Price × Delta`

Portfolio target weights are based on this exposure.

## 7.2 Contract count

Approximate contracts:

`Contracts = Target Dollar Exposure / (100 × Underlying Price × Delta)`

Contract rounding must be explicitly modeled.

For small model accounts where one contract causes material tracking error, calculate:

- actual weight,
- target weight,
- tracking error,
- and minimum viable account size.

## 7.3 Portfolio exposure

`Gross Initial Delta Exposure = Sum(abs(delta-adjusted exposures)) / NAV`

Test:

- 1.00x
- 1.25x
- 1.50x
- 1.75x
- 2.00x

## 7.4 Premium-at-risk

`Premium at Risk = Net Long Option Premium / NAV`

Test hard limits:

- 35%
- 50%
- 65%
- 80%

No candidate can be selected without showing both:

- gross delta exposure,
- premium-at-risk.

A 1.50x portfolio using 55% NAV in premium is materially different from one using 90% NAV in premium.

---

# 8. Rebalance and Selection Calendar

The strategy follows a quarterly structure aligned with the S&P-style March/June/September/December cadence.

## 8.1 Ranking/reference rule

Use the **last NYSE trading session of the prior month** as the ranking reference date:

- February month-end → March rebalance
- May month-end → June rebalance
- August month-end → September rebalance
- November month-end → December rebalance

At the reference date:

1. determine point-in-time S&P membership,
2. obtain float-adjusted market capitalization or official index weight,
3. aggregate share classes into economic issuers,
4. rank issuers,
5. select top 15,
6. compute target weights.

No future information may be used.

## 8.2 Implementation rule

Implementation occurs on the **first NYSE trading session after the third Friday** of March, June, September, and December.

This separates the ranking reference date from trade implementation and prevents look-ahead.

## 8.3 Primary study schedule

| Cycle | Ranking Reference Date | Third Friday | Implementation Date |
|---|---|---|---|
| 2021 Q1 | 2021-02-26 | 2021-03-19 | 2021-03-22 |
| 2021 Q2 | 2021-05-28 | 2021-06-18 | 2021-06-21 |
| 2021 Q3 | 2021-08-31 | 2021-09-17 | 2021-09-20 |
| 2021 Q4 | 2021-11-30 | 2021-12-17 | 2021-12-20 |
| 2022 Q1 | 2022-02-28 | 2022-03-18 | 2022-03-21 |
| 2022 Q2 | 2022-05-31 | 2022-06-17 | 2022-06-21 |
| 2022 Q3 | 2022-08-31 | 2022-09-16 | 2022-09-19 |
| 2022 Q4 | 2022-11-30 | 2022-12-16 | 2022-12-19 |
| 2023 Q1 | 2023-02-28 | 2023-03-17 | 2023-03-20 |
| 2023 Q2 | 2023-05-31 | 2023-06-16 | 2023-06-20 |
| 2023 Q3 | 2023-08-31 | 2023-09-15 | 2023-09-18 |
| 2023 Q4 | 2023-11-30 | 2023-12-15 | 2023-12-18 |
| 2024 Q1 | 2024-02-29 | 2024-03-15 | 2024-03-18 |
| 2024 Q2 | 2024-05-31 | 2024-06-21 | 2024-06-24 |
| 2024 Q3 | 2024-08-30 | 2024-09-20 | 2024-09-23 |
| 2024 Q4 | 2024-11-29 | 2024-12-20 | 2024-12-23 |
| 2025 Q1 | 2025-02-28 | 2025-03-21 | 2025-03-24 |
| 2025 Q2 | 2025-05-30 | 2025-06-20 | 2025-06-23 |
| 2025 Q3 | 2025-08-29 | 2025-09-19 | 2025-09-22 |
| 2025 Q4 | 2025-11-28 | 2025-12-19 | 2025-12-22 |
| 2026 Q1 | 2026-02-27 | 2026-03-20 | 2026-03-23 |
| 2026 Q2 | 2026-05-29 | 2026-06-19 | 2026-06-22 |
| 2026 Q3 | 2026-08-31 | 2026-09-18 | 2026-09-21 |

**Note:** The 2026 Q3 implementation date occurs after this document date; use 2026-08-31 only for ranking data already available as of September 5, 2026. Do not fabricate future execution.

---

# 9. Mandatory Issuer Eligibility Manifest

This manifest is the key to avoiding unnecessary ThetaData downloads.

## 9.1 Rule

**No historical option data may be pulled until the issuer manifest has been created.**

Required file:

`issuer_manifest.csv`

Required columns:

- `cycle_id`
- `ranking_reference_date`
- `implementation_date`
- `issuer_id`
- `issuer_name`
- `rank`
- `ticker_primary`
- `ticker_secondary`
- `share_class_group`
- `sp500_member_flag`
- `float_market_cap`
- `sp500_weight`
- `raw_target_weight`
- `capped_target_weight_12`
- `capped_target_weight_15`
- `source`
- `source_timestamp`
- `quality_flag`

## 9.2 Historical membership source

Use a point-in-time S&P constituent source, not today's membership projected backward.

Public historical constituent repositories are acceptable for research if cross-checked. Production/compliance usage should retain the original source file and checksum.

## 9.3 Historical weighting source

Preferred hierarchy:

1. official historical S&P index weights / float market caps,
2. historical SPY constituent weights as a close index proxy,
3. reconstructed float market cap from point-in-time shares × price,
4. ordinary market cap only as a documented fallback.

The selected weighting source must be frozen before the option backtest.

## 9.4 Ticker aliases

Maintain an alias table:

`symbol_aliases.csv`

Required because tickers can change and option symbology can differ.

Columns:

- `issuer_id`
- `effective_start`
- `effective_end`
- `equity_ticker`
- `theta_option_root`
- `old_ticker`
- `corporate_action`
- `notes`

Examples requiring attention include:

- FB → META
- GOOG / GOOGL economic aggregation
- BRK.B vs BRK-B / vendor-specific symbols
- stock splits
- option root changes
- mergers/spinoffs

---

# 10. ThetaData Acquisition Strategy

ThetaData v3 supports historical NBBO quotes, expirations, strikes, open interest, first-order Greeks and related data. Historical quote requests can be interval-sampled, and ThetaData reports the last quote at each requested interval. Open interest represents the prior trading day's closing OI as reported by OPRA.

## 10.1 Principle

**Event-driven extraction, not market-wide extraction.**

We need two types of data:

### A. Chain-selection data
Needed only when a new option contract must be selected.

### B. Position-monitoring data
Needed only for contracts actually held, plus a minimal surrounding reference set.

This reduces the amount of data by orders of magnitude.

---

# 11. Exact ThetaData Pull Layers

## Layer 0 — Metadata only

For each eligible ticker in `issuer_manifest.csv`:

Pull:

- available option dates,
- expirations,
- strikes/contracts,
- symbol/root mappings.

Purpose:

- verify historical coverage,
- identify eligible expirations,
- identify corporate-action gaps.

Do not download quotes yet.

## Layer 1 — Decision-date chain snapshots

For every implementation date and every Top-15 ticker:

Required time samples:

- 10:00 ET
- 12:00 ET
- 14:00 ET
- 15:45 ET

Purpose:

- test whether time-of-day meaningfully changes execution,
- select primary production execution time later.

After the timing test is complete, freeze one execution time and stop collecting the other times for further research.

### Expiration filter

Only expirations with DTE in:

- 330–400 days
- 500–580 days
- 690–770 days

These bands surround the 365 / 540 / 730 DTE targets while allowing listed-expiration availability.

### Right filter

- calls only for primary research

Puts are pulled only for the limited true-synthetic benchmark.

### Strike/delta filter

The desired delta grid is:

- 0.75
- 0.80
- 0.85
- 0.90
- 0.95

On selection dates, retrieve enough strikes to include approximately:

- delta 0.70 through 0.98

Do not collect far-OTM calls that cannot qualify.

### Minimum surrounding contracts

For every target delta, retain:

- closest contract by delta,
- next higher-delta strike,
- next lower-delta strike.

This allows sensitivity tests without repulling the chain.

## Layer 2 — Liquidity data on selection dates

For all candidate contracts retain:

- NBBO bid
- NBBO ask
- bid size
- ask size
- timestamp
- bid exchange
- ask exchange
- quote conditions
- open interest
- option volume if available
- underlying price
- implied volatility
- delta
- theta
- vega
- rho
- lambda
- IV error / quality field if supplied

ThetaData first-order Greeks include bid, ask, delta, theta, vega, rho, implied volatility and underlying price.

## Layer 3 — Held-contract daily monitoring

Once the strategy chooses a contract, follow **only that contract** every trading day until exit/roll.

Daily sample:

- freeze one time after the timing study; default research candidate = 15:45 ET

Required daily fields:

- bid
- ask
- bid size
- ask size
- delta
- theta
- vega
- rho
- implied vol
- underlying price
- open interest
- DTE
- option volume where available

Do **not** download the entire chain daily.

## Layer 4 — Roll-trigger chain refresh

A new chain snapshot is pulled only when one of the following occurs:

- scheduled quarterly rebalance,
- DTE reaches roll threshold,
- delta breaches the active delta band,
- issuer leaves Top 15,
- issuer enters Top 15,
- corporate action invalidates contract,
- quote/liquidity failure requires substitute contract.

At that moment, repeat Layer 1 only for the affected issuer.

## Layer 5 — Diagnostics

Full/expanded chains may be pulled for a small number of predetermined diagnostic dates:

- 2022 selloff peak-stress dates
- selected 2023–2026 volatility episodes
- largest portfolio drawdown dates
- largest one-day issuer declines

These diagnostics are for understanding behavior, not for optimizing using future knowledge.

---

# 12. Data Fields — Master Schema

## 12.1 `option_chain_snapshot`

Required columns:

- `date`
- `timestamp`
- `cycle_id`
- `issuer_id`
- `symbol`
- `expiration`
- `dte`
- `strike`
- `right`
- `underlying_price`
- `bid`
- `ask`
- `mid`
- `spread_abs`
- `spread_pct_mid`
- `bid_size`
- `ask_size`
- `open_interest`
- `volume`
- `delta`
- `theta`
- `vega`
- `rho`
- `lambda`
- `implied_vol`
- `iv_error`
- `intrinsic_value`
- `extrinsic_value`
- `moneyness`
- `target_delta_bucket`
- `candidate_flag`
- `quality_flag`

Derived:

`mid = (bid + ask) / 2`

`spread_pct_mid = (ask - bid) / mid`

`intrinsic_value_call = max(underlying_price - strike, 0)`

`extrinsic_value = mid - intrinsic_value`

## 12.2 `held_contract_daily`

Required columns:

- `date`
- `issuer_id`
- `symbol`
- `contract_id`
- `expiration`
- `strike`
- `right`
- `dte`
- `underlying_price`
- `bid`
- `ask`
- `mid`
- `delta`
- `theta`
- `vega`
- `rho`
- `implied_vol`
- `open_interest`
- `volume`
- `position_contracts`
- `option_market_value`
- `delta_adjusted_exposure`
- `target_exposure`
- `weight_tracking_error`
- `premium_at_risk`
- `roll_trigger_flag`
- `roll_trigger_reason`

## 12.3 `equity_daily`

For each issuer held during the study:

- `date`
- `issuer_id`
- `ticker`
- `open`
- `high`
- `low`
- `close`
- `adjusted_close`
- `volume`
- `split_factor`
- `cash_dividend`
- `total_return_factor`

## 12.4 `rates_daily`

- date
- overnight/reference rate
- 1M
- 3M
- 6M
- 1Y
- 2Y

Use actual risk-free/Treasury/SOFR data appropriate for the option model and released-cash return.

## 12.5 `portfolio_daily`

- date
- strategy_id
- NAV
- cash
- T-bill balance
- option market value
- total premium at risk
- gross delta exposure
- net delta exposure
- weighted average delta
- weighted average DTE
- weighted average IV
- weighted average spread %
- daily P&L
- cumulative return
- peak NAV
- drawdown
- turnover
- execution cost
- theta attribution
- vega attribution
- underlying attribution

---

# 13. Liquidity and Data-Quality Filters

A contract cannot be selected unless it passes all required filters.

Initial research filters to test:

### Spread
- <= 1.0% of mid
- <= 2.0% of mid
- <= 3.0% of mid
- no hard spread filter

### Open interest
- >= 100
- >= 500
- >= 1,000
- no hard OI filter if spread/size are strong

### Quote quality
Reject if:

- bid <= 0
- ask <= bid
- crossed market
- stale/invalid condition
- missing underlying price
- missing delta
- extreme IV-error quality flag
- corporate-action ambiguity

### Size
Record NBBO displayed size; do not assume an institutional-size fill is available from a one-lot quote.

The backtest must separately model account sizes where order size materially exceeds displayed NBBO.

---

# 14. Execution Price Models

Every strategy must be tested under at least three execution models.

## 14.1 Optimistic

Buy at mid.  
Sell at mid.

## 14.2 Realistic

For buys:

`mid + 25% × (ask - mid)`

For sells:

`mid - 25% × (mid - bid)`

## 14.3 Conservative

Buy at ask.  
Sell at bid.

No strategy should be accepted if its alpha exists only under midpoint execution.

Optional fourth model after empirical fill research:

- liquidity-scaled slippage based on spread, quote size, and order size.

---

# 15. Contract Selection Algorithm

For each issuer and each DTE bucket:

1. Identify eligible expirations.
2. Select expiration nearest target DTE.
3. Pull call candidates with delta approximately 0.70–0.98.
4. Apply data-quality filters.
5. Apply liquidity filters.
6. For each target delta:
   - select contract with minimum absolute delta error,
   - retain one adjacent strike on each side.
7. Record:
   - premium,
   - intrinsic value,
   - time value,
   - spread,
   - OI,
   - IV,
   - Greeks,
   - capital required per $1 of delta exposure.
8. Do not use future performance to choose between multiple qualifying contracts.

Primary tie-break hierarchy:

1. smallest absolute delta error,
2. tighter spread %,
3. greater open interest,
4. greater displayed size,
5. lower extrinsic-value cost per $ of delta exposure.

---

# 16. Roll Logic Tests

## 16.1 Fixed-DTE roll

Test:

- roll at 90 DTE
- roll at 180 DTE
- roll at 270 DTE

## 16.2 Delta-band roll

Test lower delta triggers:

- 0.70
- 0.60
- 0.50

Upper-delta triggers:

- 0.97
- 0.98
- 0.99

## 16.3 Hybrid

Roll when either:

- DTE <= threshold,
- OR delta leaves the permitted band,
- OR issuer membership changes.

Hybrid must be tested only after fixed-DTE results are established.

---

# 17. Quarterly Portfolio Rebalancing

At every quarterly implementation:

1. update Top-15 issuer membership,
2. calculate capped stock target weights,
3. remove exited issuers,
4. add new issuers,
5. compute option delta exposure required,
6. determine whether existing contracts can be retained,
7. trade only where required,
8. calculate realized execution cost,
9. reset portfolio exposure to the target strategy level.

Between quarterly dates:

- allow issuer weights to drift,
- do not continuously rebalance to target weight,
- only roll/re-strike when an explicit option rule fires.

---

# 18. Test Matrix

## 18.1 Primary variables

### Delta
- 0.75
- 0.80
- 0.85
- 0.90
- 0.95

### Entry DTE
- 365
- 540
- 730

### Roll DTE
- 90
- 180
- 270

### Gross delta exposure
- 1.00x
- 1.25x
- 1.50x
- 1.75x
- 2.00x

### Premium-at-risk cap
- 35%
- 50%
- 65%
- 80%

### Issuer cap
- 12%
- 15%

### Execution assumption
- midpoint
- realistic 25%-of-half-spread
- bid/ask

This is intentionally a broad first-pass grid.

## 18.2 Sequential optimization

Do **not** optimize all dimensions simultaneously at first.

Use staged testing:

### Stage A
Freeze:
- CONTROL-15 issuer weights
- 1.00x exposure
- no alpha sleeve

Optimize:
- delta
- DTE
- roll rule

### Stage B
Freeze winning stock-replacement structure.

Optimize:
- gross delta exposure
- premium-at-risk

### Stage C
Freeze winning synthetic exposure.

Compare released-capital uses:
- T-bills
- added Top-15 exposure
- later: separate alpha sleeve

This sequencing prevents parameter interaction from becoming a rabbit hole.

---

# 19. Required Backtest Periods

## Primary
September 2021 – latest available date in 2026

## Current-market confirmation
January 2023 – latest available date

## Calendar subperiods
- 2022
- 2023
- 2024
- 2025
- 2026 YTD

## Rolling
- rolling 1-year
- rolling 3-year
- rolling 5-year where available

## Older stress/reference
Use older data only after parameter selection is frozen.

Older periods do not get to re-optimize the strategy.

---

# 20. Crash and Stress Matrix

The strategy must survive deterministic shocks.

## 20.1 Basket shocks

- -10%
- -20%
- -30%
- -40%
- -50%
- -60%

## 20.2 Single-issuer shocks

- -30%
- -50%
- -75%
- -100%

## 20.3 IV shocks

At each price shock:

- IV +10 points
- IV +20 points
- IV +40 points
- IV +60 points

## 20.4 Required outputs

- portfolio NAV
- option premium remaining
- delta after shock
- gross delta exposure after shock
- premium lost
- cash/T-bill balance
- recovery return required
- ability to re-delta without new capital
- resulting issuer concentrations
- maximum possible additional loss

---

# 21. Crash-Recovery Logic

Long calls naturally lose delta as the market falls.

This automatic deleveraging may be a feature.

Test two recovery philosophies.

## 21.1 Passive recovery

Do nothing until scheduled roll/rebalance.

Benefit:
- preserves automatic deleveraging.

Risk:
- may participate weakly in sharp rebounds.

## 21.2 Re-delta recovery

If delta falls below a trigger:

- close/roll existing call,
- purchase a new target-delta call.

Test triggers:

- <0.70
- <0.60
- <0.50

Measure whether the strategy is effectively buying exposure after declines and whether it improves recovery or merely increases losses.

---

# 22. Released-Capital Accounting

No freed capital may disappear from the model.

For every day:

`NAV = option market value + cash + T-bill assets + alpha sleeve assets`

When calls require less capital than shares:

- unused capital defaults to T-bills in the neutral benchmark.

The difference between:

- stock capital required,
- and option premium/collateral required,

must be explicitly visible.

---

# 23. Capital-Deployment Architectures

## S1 — Pure replacement
- 1.00x Top-15 delta exposure
- all excess capital in T-bills

Purpose:
- isolate whether long calls are a good stock-replacement engine.

## S2 — Moderate leverage
- 1.25x or 1.50x Top-15 delta exposure
- residual capital in T-bills

Purpose:
- determine whether capital efficiency can create superior portfolio CAGR while retaining defined-risk properties.

## S3 — High leverage
- 1.75x or 2.00x exposure

Purpose:
- find where leverage becomes counterproductive.

Not presumed to be a production candidate.

## S4 — Alpha redeployment
- approximately 1.00x Top-15 exposure
- released capital assigned to a separately specified alpha sleeve

Not tested until S1–S3 are understood.

## S5 — Hybrid
Example:
- 1.25x Top-15
- residual capital to alpha sleeve
- mandatory liquidity reserve

Test only after S4 is defined.

---

# 24. Performance Metrics

## Return
- CAGR
- cumulative return
- annual return
- rolling return
- excess CAGR vs CONTROL-15
- excess CAGR vs S&P 500 TR
- upside capture
- rolling win rate

## Risk
- annualized volatility
- maximum drawdown
- average drawdown
- recovery time
- worst day
- worst week
- worst month
- expected shortfall / CVaR
- Sortino
- Sharpe
- downside capture

## Capital efficiency
- return on NAV
- return on premium
- return on deployed capital
- delta-notional per NAV
- delta-notional per premium dollar
- average uncommitted capital
- T-bill income
- financing/carry impact

## Option-specific
- average delta
- average DTE
- average IV
- average bid/ask spread %
- realized roll cost
- theta drag
- vega contribution
- premium lost on expired/rolled contracts
- percentage of calls finishing ITM
- average intrinsic/extrinsic composition
- contract turnover

## Concentration
- largest issuer weight
- top-3 weight
- top-5 weight
- effective number of issuers
- sector concentration
- option-premium concentration by issuer

---

# 25. Required Attribution

Approximate option P&L decomposition should include:

- underlying/delta effect
- gamma/convexity effect
- theta
- vega/IV
- execution
- cash/T-bill return
- corporate-action/dividend effect

The strategy is not accepted merely because the final NAV is high.

We must know **why** it worked.

---

# 26. Pass/Fail Framework

A synthetic candidate must pass all of the following before advancing.

## Gate 1 — Stock replacement
At 1.00x initial delta exposure:

- tracks CONTROL-15 reasonably,
- does not suffer unacceptable return drag,
- has feasible spreads/liquidity,
- demonstrates meaningful capital release.

## Gate 2 — Execution robustness
Candidate remains viable under:

- realistic execution,
- conservative execution.

If alpha disappears at realistic execution, reject it.

## Gate 3 — Crash behavior
Candidate must show:

- defined premium loss,
- survivable portfolio NAV,
- no hidden short-put-style liability,
- sufficient remaining capital to operate after major declines.

## Gate 4 — Parameter stability
Winner cannot depend on one exact parameter.

Example:

If 0.85 delta works but 0.80 and 0.90 both fail badly, treat as overfit.

Preferred solutions lie on a **broad parameter plateau**.

## Gate 5 — Regime robustness
Must work acceptably in:

- 2022,
- 2023–2024,
- 2025–2026,
- not just one bull market.

## Gate 6 — Leverage
Additional exposure must improve portfolio-level return/risk rather than merely increase CAGR.

---

# 27. Anti-Overfitting Rules

1. No selecting parameters from the full matrix and then reporting the same period as validation.
2. Use 2021–2024/2025 for development and reserve the latest meaningful period for holdout where practical.
3. Prefer stable parameter neighborhoods.
4. Penalize turnover and complexity.
5. Every new rule must improve more than one regime.
6. No post-hoc exception for one ticker.
7. No discretionary trade overrides in backtest.
8. No removing an adverse year.
9. No using a quote that would not have been available at the simulated decision time.
10. No future Greeks, future OI, or later corporate-action knowledge.

---

# 28. Data Acquisition Order

Do not reverse this order.

## Step 1
Finalize historical point-in-time S&P Top-15 issuer manifest.

## Step 2
Freeze:
- 12% or 15% issuer cap methodology for initial test,
- quarter schedule,
- ticker aliases.

## Step 3
Query ThetaData metadata:
- available dates,
- expirations,
- strikes.

## Step 4
Pull decision-date candidate chains only.

## Step 5
Run a small pilot:
- one quarter,
- three issuers,
- all delta/DTE targets.

Validate schemas and calculations.

## Step 6
Pull all decision-date chains for 2021–2026.

## Step 7
Select contracts algorithmically.

## Step 8
Pull daily history only for selected contracts.

## Step 9
Run Stage A stock-replacement tests.

## Step 10
Pull additional chains only for roll events created by the tested rules.

## Step 11
Run leverage/premium Stage B.

## Step 12
Only after winning core architecture exists, define and test alpha redeployment.

---

# 29. Estimated Data Footprint Logic

We are deliberately avoiding full-universe downloads.

Primary quarterly decisions:

- approximately 20 completed quarterly cycles through mid-2026,
- 15 issuers per cycle,
- approximately 300 issuer-cycle selection events.

At each event:

- three DTE zones,
- delta approximately 0.70–0.98,
- calls only,
- limited intraday samples.

After contract selection:

- only selected contract(s) followed daily.

Even allowing multiple delta/DTE candidates, this should be a tiny fraction of all U.S. options history.

The project should never require downloading:

- every ticker,
- every strike,
- every expiration,
- every minute,
- every trading day.

---

# 30. ThetaData Technical Notes

Current ThetaData v3 documentation confirms:

- historical option quote endpoint returns NBBO quotes reported by OPRA,
- interval sampling is supported,
- quotes include bid/ask prices and sizes,
- expiration and strike listing endpoints are available,
- historical open interest is available,
- open interest normally reflects the prior trading day's close,
- first-order historical Greeks include:
  - bid,
  - ask,
  - delta,
  - theta,
  - vega,
  - rho,
  - implied volatility,
  - IV error,
  - underlying timestamp,
  - underlying price,
- history endpoints can be queried by symbol, expiration, strike, right and date/time range.

Primary documentation:
- https://docs.thetadata.us/operations/option_history_quote.html
- https://docs.thetadata.us/operations/option_history_greeks_first_order.html
- https://docs.thetadata.us/operations/option_history_open_interest.html
- https://docs.thetadata.us/operations/option_list_expirations.html
- https://docs.thetadata.us/operations/option_list_strikes.html

---

# 31. Source Register

## S&P historical membership research

Public point-in-time component sources identified for research/cross-checking include:

- https://github.com/hanshof/sp500_constituents
- https://github.com/fja05680/sp500
- https://github.com/chinobing/historical_sp500_constituents

These are research sources, not official S&P licenses. Retain source provenance.

## ThetaData

Primary options source:
- https://www.thetadata.net/
- https://docs.thetadata.us/

## Phase-I concentration research

Research sources previously used include S&P Dow Jones Indices official Top-10/Top-20 materials and independent point-in-time S&P studies. Their findings informed the design, but the final production backtest must rely on the frozen datasets retained with this project.

---

# 32. Project Folder Structure

Recommended:

```text
/top15_synthetic/
    MASTER_SPEC.md
    /config/
        strategy_config.yaml
        theta_config.yaml
        symbol_aliases.csv
    /manifests/
        issuer_manifest.csv
        option_pull_manifest.csv
        held_contract_manifest.csv
    /raw/
        /sp500/
        /theta/
        /rates/
        /equity/
    /processed/
        option_chain_snapshot.parquet
        held_contract_daily.parquet
        equity_daily.parquet
        rates_daily.parquet
        portfolio_daily.parquet
    /backtests/
        /control15/
        /stage_a_stock_replacement/
        /stage_b_leverage/
        /stage_c_redeployment/
    /reports/
        performance_summary.csv
        rolling_returns.csv
        drawdowns.csv
        trade_log.csv
        attribution.csv
        stress_tests.csv
    /audit/
        source_checksums.csv
        run_log.csv
        assumptions.md
        change_log.md
```

---

# 33. Required Configuration File

`strategy_config.yaml`

Must contain all research choices so no code hard-codes assumptions.

Example keys:

```yaml
study:
  start_date: 2021-09-01
  end_date: latest
  current_regime_start: 2023-01-01

equity:
  n_issuers: 15
  rebalance_frequency: quarterly
  issuer_caps: [0.12, 0.15]

options:
  rights: [call]
  target_deltas: [0.75, 0.80, 0.85, 0.90, 0.95]
  target_entry_dte: [365, 540, 730]
  roll_dte: [90, 180, 270]
  gross_delta_exposure: [1.00, 1.25, 1.50, 1.75, 2.00]
  premium_at_risk_caps: [0.35, 0.50, 0.65, 0.80]
  execution_times_et: ["10:00", "12:00", "14:00", "15:45"]

execution:
  models: [mid, realistic, bid_ask]

liquidity:
  spread_pct_max_tests: [0.01, 0.02, 0.03, null]
  open_interest_min_tests: [100, 500, 1000, 0]
```

Every backtest output must include a hash or serialized copy of the configuration.

---

# 34. Master Option Pull Manifest

Before querying ThetaData, generate:

`option_pull_manifest.csv`

Fields:

- request_id
- issuer_id
- symbol
- decision_date
- request_type
- expiration_min
- expiration_max
- target_dte_bucket
- right
- delta_min
- delta_max
- time_et
- interval
- purpose
- status
- rows_returned
- quality_flag

Allowed `purpose` values:

- initial_selection
- quarterly_rebalance
- dte_roll
- delta_roll
- membership_change
- corporate_action
- diagnostic

If a proposed request cannot be assigned one of these purposes, it should not be executed without a spec change.

---

# 35. Required Audit Trail

Every simulated trade must record:

- decision timestamp
- data available at decision timestamp
- contract candidates
- rejected candidates and reasons
- selected contract
- theoretical target exposure
- actual rounded exposure
- simulated fill
- spread
- execution model
- commission assumption
- before/after NAV
- before/after cash
- before/after premium-at-risk
- before/after gross delta exposure

This makes the research reproducible.

---

# 36. Decision Sequence — No Rabbit Holes

The project must answer questions in this exact order:

### Decision 1
Does Top-15 remain the correct issuer count?

**Already provisionally answered: yes. Do not reopen unless synthetic results reveal a structural problem.**

### Decision 2
12% vs 15% issuer cap.

### Decision 3
Can long calls replace stock efficiently at 1.00x?

### Decision 4
Optimal delta.

### Decision 5
Optimal DTE.

### Decision 6
Optimal roll method.

### Decision 7
Maximum acceptable premium-at-risk.

### Decision 8
Optimal gross delta exposure.

### Decision 9
Best use of released capital.

### Decision 10
Production execution rules.

Do not jump to Decision 9 before Decisions 3–8 are resolved.

---

# 37. Expected Research Hypotheses

These are hypotheses only and must not be treated as conclusions.

## H1
The best stock-replacement region will likely be approximately **0.80–0.90 delta**.

## H2
Approximately **12–24 months DTE** will likely outperform shorter-dated calls for stock replacement because of lower theta sensitivity and fewer rolls.

## H3
Approximately **1.25x–1.50x gross initial delta exposure** may occupy the best portfolio-level leverage zone.

## H4
A portfolio premium-at-risk limit around **50–65% of NAV** may preserve enough crash survival while allowing meaningful capital efficiency.

## H5
Pure 2.00x exposure will likely sacrifice too much of the defined-risk advantage.

## H6
A hybrid of approximately 1.25x Top-15 plus a separately diversified alpha sleeve may eventually outperform simply levering the Top-15 to 1.75x–2.00x.

Every hypothesis is falsifiable.

---

# 38. What Counts as Success

The project succeeds if it produces a simple, executable architecture such as:

> Hold the 15 largest S&P 500 economic issuers.  
> Cap each at X%.  
> Replace stock exposure with calls near Y delta and Z DTE.  
> Roll at a defined DTE/delta trigger.  
> Maintain no more than P% of NAV in option premium.  
> Target Gx gross initial delta exposure.  
> Keep or redeploy remaining capital according to a fixed rule.

The result should be understandable in a few sentences even though the research underneath is detailed.

Complexity that cannot demonstrate material improvement is rejected.

---

# 39. Immediate Next Actions

1. Build the quarterly `issuer_manifest.csv` for 2021–2026.
2. Resolve share-class aggregation and ticker aliases.
3. Freeze initial 12%/15% control portfolios.
4. Confirm ThetaData historical availability for every issuer-cycle.
5. Build `option_pull_manifest.csv`.
6. Run a three-issuer / one-quarter pilot.
7. Validate quote, Greek, OI and execution calculations.
8. Pull remaining decision-date data.
9. Run Stage A.
10. Do not add an alpha sleeve until Stage A and Stage B are complete.

---

# 40. Final Project Principle

The strategy is not:

> "Use options because options are leveraged."

The strategy is:

> **Use a concentrated, rules-based mega-cap leadership portfolio as the return engine; use long-dated defined-risk options only if they can manufacture that exposure more efficiently; and redeploy only the capital that is truly released after realistic execution, liquidity, and risk constraints.**

Every dataset, rule, and test in this project must serve that objective.
