# Tastytrade / CBOE / Academic Research — Premium-Selling & Iron Condor Mechanics

**Compiled 2026-07-04** from a deep, multi-source, adversarially-verified research pass (19 sources, 69 claims extracted, 25 verified, 17 confirmed / 8 refuted). Purpose: an evidence-based parameter set for the S7 premium-income rebuild and the broader premium-selling suite — how professionals actually run this, not in-a-bubble guesses. See docs/PREREG_S7_income_condor_2026-07-04.md and backtester/s7_income_condor.py.

## Evidence-based dials (with confidence)

| Dial | Evidence-based value | Confidence | Source |
|---|---|---|---|
| DTE at entry | 30-45 (tastytrade 45 "sweet spot"; CBOE CNDR = 30/monthly) | Confirmed | tastytrade, CBOE, ApexVol |
| Short-strike delta | 16 (tastytrade) to 20 (CBOE CNDR) | Confirmed (primary for 20) | CBOE CNDR methodology |
| Wings (long strikes) | DELTA-BASED ~5-delta longs — NOT fixed points | Confirmed (primary) | CBOE CNDR |
| Profit target | Close at 50% of credit received | Confirmed (tastytrade calls it illustrative) | tastytrade, CBOE study |
| 21-DTE time stop | tastytrade practice; CBOE holds to expiry instead | Weak / disputed | tastytrade only |
| IV-rank entry filter | Directionally real, specific thresholds UNVERIFIED | Mixed (see below) | CBOE (dir.) / blogs refuted |
| Underlying | ATM cash-secured puts strongest (PUT index Sharpe 0.65 vs SPX 0.49, 32yr) | Confirmed (primary) | CBOE PUT study |

## The three corrections for the S7 rebuild
1. **Wings must be DELTA-BASED (~5-delta longs), not fixed points.** The CBOE CNDR institutional benchmark buys 5-delta protection. S7's first build used fixed 25-point wings -> ~1:3 reward:risk -> needed ~75% win vs ~63% realized -> structural loss. Wing width sets the break-even threshold; short-delta sets the win rate (decoupled). Narrower/delta-based wings LOWER the break-even bar.
2. **IV-rank is NOT the proven savior.** Directional support is real (CBOE: higher-VIX-quintile entries earn more) but every specific threshold claim (IVR>30, IVR 50+) was REFUTED 0-3, the profitable CBOE CNDR benchmark uses NO IV filter, and high-IV months hold the worst losses. Test it as a VARIANT; do not build the thesis on it.
3. **The strongest documented edge is ATM cash-secured PUT writing, not the OTM condor** (PUT index Sharpe 0.65 over 32 years, primary-sourced). Make the CSP a first-class arm/benchmark, not an afterthought. The 16-delta OTM strangle/condor underperforms.

## Load-bearing corroboration of our own results
An independent **11-year SPX backtest of tastytrade's EXACT 16-delta / 45-DTE formula**: 65% win rate, average winner +10%, average loser -24%, poor net returns (sjoptions.com, confirmed 3-0). This is the SAME wins-small-loses-big asymmetry our own 0DTE and first-S7 tests found — external validation that the method is sound and the naive retail formula genuinely underperforms on SPX.

## The volatility risk premium (the engine)
- VIX (implied) averaged 19.3% vs 15.1% realized S&P 500 vol, 1990-2018 — a 4.2pt structural gap (CBOE PUT study, primary, 3-0).
- CBOE PUT index (systematic 1-month ATM cash-secured SPX puts): compound return 9.54% vs SPX 9.80%, std 9.95% vs 14.93%, Sharpe 0.65 vs 0.49 over 32+ years (primary, 3-0).
- Higher beginning-of-month VIX predicts higher PUT returns across quintiles — BUT the paper cautions the high-VIX quintile also contains the worst-loss months (primary, 3-0).

## CONFIRMED claims (17, verified 3-0 or 2-1)
- CBOE CNDR sells 0.20-delta OTM SPX puts+calls, buys 0.05-delta wings. [cboe.com CNDR]
- CNDR/BFLY roll monthly, AM-settled (~30 DTE), one-lot, hold-to-expiry, NO IV filter. [CNDR methodology PDF]
- CNDR strikes selected before 11am ET on third-Friday roll dates. [CNDR PDF]
- CBOE PUT index: Sharpe 0.65 vs SPX 0.49, comparable return, ~2/3 the volatility, 32yr. [PutWrite study]
- VRP: VIX 19.3% vs realized 15.1% (1990-2018), 4.2pt. [PutWrite study]
- Higher entry VIX -> higher put-write returns, but worst losses cluster in high-VIX. [PutWrite study]
- tastytrade iron-condor guide: close at 50% of original credit (framed illustratively). [tastytrade]
- VRP exists because implied vol systematically exceeds realized. [quantpedia]
- Enter condors ~45 DTE, short strikes ~20-30 delta. [20percentfreedom blog, 2-1]
- Manage at 50% of max profit, most efficient for ~20pt-wide condors. [20percentfreedom, 2-1]
- tastytrade research: ~45.75 DTE = superior avg P&L per trade. [sweetvolatility, 2-1]
- tastytrade mechanical exit = close winners at 50% of max profit. [sweetvolatility, 3-0]
- 11-year SPX backtest of the exact 16-delta/45-DTE formula: 65% win, +10% avg win, -24% avg loss, poor returns. [sjoptions, 3-0]
- ApexVol: 30-45 DTE at entry for iron condors. [apexvol, 3-0]

## REFUTED claims (8, killed 0-3) — do NOT rely on these
- "IVR>30 gives similar/better outcomes" and "IVR>30 raises ROI up to 60%" and "30% IVR entry threshold." [menthorq — all unverifiable]
- "tastytrade's canonical SPX strangle filters entry to IVR 50-100%." [sjoptions — unverifiable]
- "IVR>50 -> collect >45% of strike width for a better condor." [20percentfreedom — unverifiable]
- SJ Options' specific portfolio-sizing/margin blow-up figures. [unverifiable]
- ApexVol's specific delta-to-POP mapping (16d~84% up to 30d~70%). [unverifiable]
- "Selling vol loses heavily in crisis" framing as attributed. [quantpedia — unverifiable attribution]

## Bottom line
The VRP is real and primary-sourced, but the evidence says the edge lives in CONSTRUCTION (delta-based wings, ATM cash-secured puts) far more than in the retail 16-delta OTM condor, and the specific IV-rank rules are unproven. Rebuild S7 accordingly; benchmark against the CBOE CNDR construction and the cash-secured PUT.
