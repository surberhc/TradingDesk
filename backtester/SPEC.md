# SPEC.md — Adaptive All-Weather Core (Smoothness-First Revision)

This is the authoritative description of the strategy logic. Build to this. When this file and any other source disagree, this file wins for strategy logic.

## 0. Mandate (this shapes every design choice)

The job of this strategy is to deliver a SMOOTHER RIDE for a retiree / pre-retiree book that is sensitive to sequence-of-returns risk — NOT to beat the S&P 500. Lower drawdown and lower downside capture are the goal, even at the cost of lagging in strong bull markets. Judge the model by max drawdown, worst rolling 12-month return, downside capture, and longest underperformance stretch — not by CAGR. CAGR is a constraint ("don't lag SPY too badly in bulls"), not the objective.

## 1. Architecture: engines, in priority order

Build each as its own module in src/engines/ with its own unit test.

PRIMARY engines (these create the smooth ride):
- Regime Engine — owns the de-risking decision. Outputs a Market Health Score (0-100) and maps it to a regime, which sets the equity BAND.
- Duration Filter / Inflation-Deflation Engine — owns what KIND of defense: cash, short, intermediate, or long Treasuries. This is the model's edge.
- Defensive Engine — ranks defensive assets; T-bills are the default.

SUBORDINATE:
- Volatility Multiplier — trims the equity allocation WITHIN the regime band. It is NOT an independent de-risking lever. It can only pick a point inside the band the regime engine already set; it cannot zero out equity on its own.

SATELLITE (optional, small, can be turned off via config):
- Equity Leadership / Sector Engine — broad beta is the equity core; sector tilt is an optional small overlay (default OFF or small).

Real assets (collapsed):
- A single "confirmed inflation hedge" slot (gold OR TIPS OR broad commodities), capped, filled only on independent trend+momentum confirmation.
- REVISED 2026-06-24: this is now its OWN third sleeve (equity / real assets / defense), sized by a deliberate, version-scaled target (config.REAL_ASSET_SLEEVE_TARGET — Conservative 10% / Balanced 15% / Growth 20%), carved out BEFORE the defensive sleeve rather than competing inside the defense budget. Rationale: gold was acting as a return source, not defense, and lumping it into the defense budget muddied risk and inverted the version ordering.
- DIVERSIFIED 2026-06-24: the sleeve is no longer a single "best" pick but a small BASKET — GOLD + BROAD COMMODITIES (PDBC; TIPS excluded as it behaves like Treasuries). Each leg is trend-gated independently (§6 gate) and present legs are INVERSE-VOL weighted (config.REAL_ASSET_BASKET, REAL_ASSET_VOL_LOOKBACK). Gold and commodities are ~uncorrelated (~0.05), so the basket's vol (~13.5%) is lower than either leg alone — it diversifies rather than adds risk. §12 category caps remain hard ceilings. See README "real-asset sleeve diversified".

## 2. Universe

Equity core: SPY, VTI, RSP (broad beta).
Sector satellite (optional): the 11 SPDR sectors — XLC, XLY, XLP, XLE, XLF, XLV, XLI, XLB, XLRE, XLK, XLU.
Defensive: SGOV, BIL (T-bills); SHY, VGSH (short Treasuries); USFR, TFLO (floating-rate); IEF (intermediate); TLT (long).
Real-asset slot: GLDM/IAU (gold); SCHP/STIP (TIPS); PDBC/DBC (commodities).
Benchmarks for reporting: SPY, a 60/40 (SPY/IEF) blend, and T-bills (BIL/SGOV).

Inception-aware: Some ETFs did not exist for the whole backtest window. The data loader must exclude any asset from a given month if it had not yet started trading by that month. Never forward-fill or fabricate pre-inception data. Backtest start floor: 2015-01-01 (full defensive universe exists by then). Primary period of interest: 2017-present (captures 2018-Q4, 2020 COVID, 2022 stock/bond bear). The engine must handle assets being absent early gracefully.

## 3. Data timing (correctness-critical)

- Signals computed on month-end adjusted total-return data.
- Trades execute on the first trading day of the next month (T+1).
- Only data available on/before the signal date may inform that signal. No look-ahead, including for any macro series (yields, credit) used.
- Use adjusted/total-return prices throughout (dividends + splits handled).

## 4. Regime Engine — Market Health Score (3 components, equal weight)

Score is 0-100. Three components, each contributes up to ~33.3.

1. Broad equity trend — SPY/VTI vs 200-day and 10-month moving averages; 6-month total return; 200-day slope. Full marks when above both averages, positive 6-month return, positive slope. Partial for mixed.
2. Breadth — % of the 11 sectors above their 200-day average; RSP vs SPY trend. Full marks when broad participation; low when narrow/deteriorating.
3. Stress (credit + volatility) — high-yield spread trend (or a HY-vs-Treasury ETF ratio proxy if spread data unavailable) + VIX level vs its own trend. Full marks when spreads stable/tightening and volatility calm.

Rate/inflation inputs do NOT go in this score — they live in the Duration Engine (section 6) where they drive an actual decision.

Regime thresholds and equity bands:
- 75-100  Risk-On             -> equity 80-100% of the client-version allowance
- 55-74   Risk-On/Narrowing   -> 60-80%
- 40-54   Caution             -> 35-60%
- 25-39   Defensive           -> 10-35%
- 0-24    Capital Preservation -> 0-15%

Hysteresis / whipsaw control:
- Require a confirmation buffer: a regime change requires the triggering signal to hold for a configurable number of trading days (default 2; range 2-4), OR the score to move decisively.
- Do not change regime if the score crosses a threshold by fewer than 3 points.
- If the score drops more than 10 points, allow immediate regime reduction.
- On recovery from a defensive regime, use the staged re-entry ladder (section 9), not an instant jump to full exposure.

## 5. Equity composition

- Equity core is broad beta (SPY/VTI/RSP), chosen above 200-day/10-month trend.
- Sector tilt is an OPTIONAL overlay, default small or off (config flag SECTOR_TILT_PCT, default 0; allowable 0-30% of the equity sleeve).
- If sector tilt is on: score sectors on a SIMPLE basis only — 6-month and 3-month relative strength vs SPY, plus a 200-day trend gate. No 8-factor score.
- Max single sector weight 15%. Sector count when used: 3-4.

## 6. Duration Filter & Inflation/Deflation Engine (the edge — full strength)

Long Treasuries are NOT default defense. They are a separate rate bet that must earn exposure.

Long Treasury PERMISSION rule (need ~4 of 5; hard-bans override):
1. TLT/SPTL above 200-day MA (or total-return index above 10-month MA).
2. TLT positive over 3 months.
3. TLT outperforming T-bills over 3 months.
4. 10-year yield flat or falling (below 200-day average or below prior month).
5. Long-Treasury drawdown not worse than a tested threshold (e.g. -10% from 252-day high).
Plus confirmation that stocks are weak while duration stabilizes (deflationary character, not inflationary).

Long Treasury BAN rule (any one bans long duration):
- TLT below 200-day AND below 10-month MA.
- 10-year yield above its 200-day average and rising.
- T-bills outperforming long Treasuries over 3 and/or 6 months.
- Stocks and bonds both trending down.
- Inflationary-bear filter active (below).
- Long-Treasury drawdown beyond tested threshold.

Inflationary-bear filter (the 2022 guard) — active when a majority hold:
- SPY/VTI below 200-day/10-month MA.
- 10-year yield above 200-day average and rising.
- TLT below 200-day MA.
- T-bills outperform intermediate and long Treasuries.
- Gold/commodities/T-bills/floating-rate outperform long bonds.
When active: ban long Treasuries; cap intermediate Treasuries low (0-15/20%); default defense = T-bills/ultra-short/floating-rate; real assets only if they independently pass trend+momentum.

Deflationary-panic filter — when growth-shock character dominates:
- SPY weak / in drawdown; 10-year yield falling; IEF/TLT improving vs T-bills; credit widening, volatility rising; commodities weak.
When active: allow intermediate Treasuries; allow long Treasuries up to cap IF permission rule passes; keep T-bills as ballast.

Duration caps by regime (% of total portfolio):
- Long Treasuries: Risk-On 0-10, Caution 0-15, Defensive/CapPres 0-25. Never the full defensive sleeve.
- Intermediate: Risk-On 0-10, Caution 0-25, Defensive 0-40 (lower if inflationary filter active).
- Short Treasuries: up to 20 / 50 / 100.
- T-bills/ultra-short/floating: 0-20 / 20-70 / 50-100 (the default fallback).

## 7. Defensive Engine

Rank defensive candidates on: 3-month return (25), 6-month return (20), absolute trend (20), relative return vs T-bills (15), volatility penalty (10), drawdown penalty (10). T-bills always eligible and are the fallback when nothing else earns its slot. Do not force diversification into weak assets.

## 8. Volatility multiplier (subordinate — a trim, not a cutter)

Within the regime's equity band, choose the allocation point using realized volatility. Gentle buckets: ~100% / 85% / 70% of the band's mid/high point as realized vol rises; floor at the band's bottom. It can never set equity below the regime band's floor. Vol lookback 63 trading days. Target vol by version: Conservative 8-10%, Balanced 10-12%, Growth 12-15% (config).

## 9. Re-entry ladder (keep, but bound the lag)

After a defensive period, rebuild equity in stages, not all at once:
- Stage 1: SPY back above 50-day MA, top sectors improving, vol stops rising -> 25%
- Stage 2: SPY above 200-day/10-month OR score back above 40 -> 50%
- Stage 3: >=6 of 11 sectors above 200-day OR breadth materially improves -> 75%
- Stage 4: score above 55-75 (by version), credit stable, vol normalizing -> 100%
- Rollback one stage if a stage's conditions fail and credit/vol deteriorate.
- Include a configurable MAX-LAG override so a sharp V-recovery cannot strand the portfolio in cash indefinitely.

## 10. Client versions (allocation ranges per regime)

Three versions — Conservative, Balanced, Growth — selected via config. Each maps the regime to equity/defense/real-asset ranges. Conservative caps equity lower and holds more T-bills; Growth allows more equity and more tracking error. Expose the ranges in config so they can be tuned in one place.

## 11. Portfolio assembly (order of operations each rebalance)

1. Compute Market Health Score -> regime -> equity band.
2. Apply volatility multiplier -> pick equity target inside the band.
3. Select equity holdings (broad beta + optional sector tilt).
4. Run duration filter + inflation/deflation engine -> duration permission/caps.
5. Rank defensive + real-asset candidates -> fill defense sleeve (T-bills default).
6. Apply all caps, floors, T-bill minimums, and (in taxable mode) turnover bands.
7. Apply whipsaw controls (rank buffer, 10-point replacement threshold, current-holding tie-break) and the re-entry ladder if recovering.
8. Produce target weights + reason codes for the month.

## 12. Portfolio-level caps/floors

Max single sector 15% (lower than the original 20% — smoothness). Max gold 25%, commodities 20%, TIPS 20%, long Treasury 25%, intermediate 40%. T-bill minimums per regime as in section 6. Long duration may never be the entire defensive sleeve.

## 13. Trading frictions to model

- Apply a per-trade cost assumption (config; default a few basis points) so the backtest is not frictionless.
- Track turnover; report monthly and annual turnover.
- Month-end signal, T+1 execution, optional one-day buffer.

## 14. Metrics the report must produce (the real yardstick)

Return: CAGR, annual returns, rolling 12-month returns.
Risk (PRIMARY): max drawdown, worst rolling 12-month, downside deviation, worst rolling 3-month and 3-year.
Risk-adjusted: Sharpe, Sortino, Calmar (return/max-drawdown).
Behavior: beta vs SPY, downside capture, upside capture, and the LONGEST stretch of underperformance vs SPY (in months) — call this out prominently.
Regime attribution: time spent in each regime; contribution of equity vs defense vs duration vs real-asset decisions.
Compare the strategy against SPY, 60/40, and T-bills on all of the above.

## 15. The HTML report (primary output)

Generate a single self-contained HTML file in output/ with:
- Equity curve of the strategy vs SPY vs 60/40 vs T-bills.
- Drawdown chart (underwater plot) for the strategy vs SPY.
- The full metrics table.
- A regime-over-time band/timeline showing which regime was active each month.
- A monthly allocation stacked-area chart (equity / defense / real-asset).
Charts via plotly, embedded so the file opens standalone in a browser.

## 16. Validation (build these as tests / switches)

- Parameter sensitivity: make key parameters easy to vary (config) so the user can test 50/200-day vs 10-month, 3/6/12-month lookbacks, etc.
- A simple walk-forward switch (build params on an early period, evaluate later).
- Confirm no look-ahead via an explicit test: shifting execution to T+1 must change results vs same-day execution; a test should assert signals never use future data.
