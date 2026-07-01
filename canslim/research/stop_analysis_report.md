# Hard Stop-Loss Counterfactual — APS Managed Account (2023 H2 – 2026 H1)

**Question:** Would enforcing a hard stop on every trade have made more or less money than the trader's discretion? And specifically: how often did a trade fall past the stop and then recover to pay off, vs. how often did holding past the stop just deepen the loss?

Stop is anchored to HIS entry (Cost/sh): `stop_px = entry * (1 - X%)`. Breach = first day after entry with intraday LOW ≤ stop_px. Counterfactual fill = day's open if it gapped below the stop, else the stop price. RAW (unadjusted) Tiingo OHLC used to match his recorded fills. `delta_$ = (hardstop_ret - actual_ret) * $cost` — positive means the hard stop would have IMPROVED P/L.

## Headline

Over the full history, enforcing a **−7% hard stop** would have changed realized P/L by **$-37,523** → the trader's discretion produced **LESS money (discretion wins)**.

- Actual realized P/L (non-excluded trades): **$55,042**
- Hard-stop counterfactual P/L: **$17,518**
- Net effect of the −7% stop: **$-37,523** (stop would have HURT)

## Aggregate tables

### −7% stop

| Year | Trades | Breached | Discretion HELPED | of which weathered→profit | Discretion HURT | Actual P/L | Hard-stop P/L | Net effect of stop (Σ delta_$) |
|---|---|---|---|---|---|---|---|---|
| 2023 | 13 | 6 | 0 | 0 | 6 | $-36,632 | $-25,597 | $11,035 |
| 2024 | 40 | 22 | 9 | 4 | 13 | $44,826 | $48,346 | $3,520 |
| 2025 | 45 | 30 | 15 | 7 | 15 | $155,222 | $34,572 | $-120,650 |
| 2026 | 22 | 16 | 4 | 0 | 12 | $-108,375 | $-39,803 | $68,571 |
| **ALL** | **120** | **74** | **28** | **11** | **46** | **$55,042** | **$17,518** | **$-37,523** |

### −8% (sensitivity) stop

| Year | Trades | Breached | Discretion HELPED | of which weathered→profit | Discretion HURT | Actual P/L | Hard-stop P/L | Net effect of stop (Σ delta_$) |
|---|---|---|---|---|---|---|---|---|
| 2023 | 13 | 6 | 0 | 0 | 6 | $-36,632 | $-28,115 | $8,517 |
| 2024 | 40 | 20 | 7 | 3 | 13 | $44,826 | $43,495 | $-1,331 |
| 2025 | 45 | 26 | 12 | 6 | 14 | $155,222 | $94,081 | $-61,141 |
| 2026 | 22 | 15 | 4 | 0 | 11 | $-108,375 | $-45,835 | $62,540 |
| **ALL** | **120** | **67** | **23** | **9** | **44** | **$55,042** | **$63,626** | **$8,585** |

## Rescue list — 'weathered the storm and came out on top' (−7%)

Trades that fell below the −7% stop intraday but were held and finished PROFITABLE. A hard stop would have cut these winners short.

| Symbol | Year | Deepest drawdown below entry | Final actual return | $ the stop would have COST (−delta_$) |
|---|---|---|---|---|
| OKLO | 2025 | -28.8% | 31.8% | $15,092 |
| RKLB | 2025 | -21.2% | 59.7% | $32,885 |
| IBIT | 2025 | -16.1% | 10.0% | $12,567 |
| PLTR | 2025 | -14.9% | 6.3% | $7,883 |
| MNSO | 2024 | -10.8% | 1.8% | $3,646 |
| MSTR | 2025 | -10.7% | 36.4% | $23,322 |
| MSTR | 2024 | -10.7% | 113.4% | $20,854 |
| IOT | 2024 | -10.7% | 0.0% | $3,872 |
| AMSC | 2025 | -9.0% | 1.4% | $4,374 |
| OKLO | 2025 | -7.8% | 138.4% | $63,333 |
| AAPL | 2024 | -7.1% | 0.1% | $5,090 |

## Bleeder list — holding past −7% deepened the loss (−7%)

Trades that breached −7% and where a hard stop would have SAVED money (actual return worse than the stop exit).

| Symbol | Year | Actual return | Hard-stop return | $ saved by the stop (delta_$) |
|---|---|---|---|---|
| APLD | 2026 | -41.5% | -7.6% | $23,424 |
| AXON | 2025 | -24.2% | -7.0% | $15,760 |
| AMSC | 2024 | -28.3% | -7.0% | $15,580 |
| WLDN | 2026 | -32.6% | -7.0% | $12,448 |
| AGX | 2025 | -20.2% | -7.1% | $10,629 |
| IREN | 2026 | -27.2% | -7.0% | $10,182 |
| AXGN | 2025 | -21.7% | -7.0% | $5,579 |
| VIAV | 2026 | -15.7% | -7.0% | $5,512 |
| PRCT | 2024 | -16.6% | -7.0% | $5,400 |
| ACMR | 2024 | -18.1% | -7.0% | $5,328 |
| ALMU | 2026 | -16.7% | -7.0% | $4,587 |
| CRDO | 2026 | -13.1% | -7.0% | $4,503 |
| TSSI | 2025 | -15.1% | -7.0% | $4,099 |
| URBN | 2026 | -12.9% | -7.0% | $3,610 |
| SCCO | 2024 | -11.9% | -7.0% | $2,916 |
| NBIS | 2026 | -12.1% | -7.0% | $2,862 |
| TSLA | 2026 | -11.5% | -7.0% | $2,811 |
| DUOL | 2023 | -13.6% | -7.0% | $2,773 |
| PLTR | 2023 | -13.2% | -7.0% | $2,548 |
| TOST | 2024 | -11.3% | -7.0% | $2,546 |
| QUBT | 2025 | -18.2% | -7.0% | $2,546 |
| APP | 2026 | -11.0% | -7.0% | $2,446 |
| ADMA | 2025 | -17.8% | -7.5% | $2,428 |
| IR | 2024 | -10.8% | -7.7% | $2,211 |
| ELF | 2024 | -10.7% | -7.0% | $2,149 |
| HXL | 2023 | -11.2% | -7.0% | $2,131 |
| LOAR | 2025 | -14.2% | -7.0% | $1,988 |
| STRL | 2026 | -10.3% | -7.0% | $1,956 |
| TOST | 2025 | -10.0% | -7.0% | $1,836 |
| NTGR | 2025 | -9.9% | -7.0% | $1,592 |
| VRT | 2025 | -14.4% | -11.3% | $1,585 |
| TGTX | 2025 | -14.0% | -7.0% | $1,525 |
| FTNT | 2025 | -10.2% | -7.0% | $1,502 |
| TS | 2024 | -10.5% | -7.0% | $1,441 |
| APPF | 2023 | -9.8% | -7.0% | $1,436 |
| HIMS | 2024 | -9.4% | -7.0% | $1,413 |
| IONQ | 2023 | -14.2% | -7.6% | $1,409 |
| PLTR | 2024 | -10.7% | -8.0% | $1,376 |
| FIX | 2024 | -8.7% | -7.0% | $1,198 |
| ROAD | 2025 | -9.6% | -7.0% | $1,180 |
| IBIT | 2024 | -8.3% | -7.0% | $1,117 |
| SMCI | 2023 | -8.3% | -7.0% | $739 |
| SYM | 2025 | -7.9% | -7.0% | $510 |
| YELP | 2024 | -8.2% | -7.0% | $478 |
| ADMA | 2025 | -8.3% | -7.0% | $346 |
| SEI | 2026 | -7.0% | -7.0% | $36 |

## Data quality / exclusions

**Split / ticker-mismatch (excluded from dollar aggregates):**
- NVDA (2024): split/ticker mismatch (tiingo~946.30 vs entry 94.22)
- NVDA (2024): split/ticker mismatch (tiingo~946.30 vs entry 94.22)

**Coverage gaps (no daily OHLC available on this Tiingo key — EXCLUDED from all dollar aggregates):**
These 3 windows could not be price-pathed, so their trades are dropped from the tables above (same treatment as NVDA/SQQQ). Their journal size and outcome are listed so you can judge whether they could move a per-year conclusion:

| Symbol | Year | Journal % return | $ cost (position size) | Journal $ P/L | Why uncovered |
|---|---|---|---|---|---|
| ERJ | 2024 | 21.0% | $71,510 | $14,994 | Embraer ADR — null price history on this key |
| SQ | 2025 | -8.2% | $58,922 | $-4,828 | renamed to XYZ (Block Inc) mid-2025; SQ symbol dead on this key |
| PSTG | 2025 | -6.0% | $76,287 | $-4,550 | Pure Storage — null price history on this key |

**Needle-moving check on the 3 excluded gaps:**
- ERJ (2024):  ⚠️ LOUD FLAG: worst-case swing (~$20,000) is a large fraction of 2024's net delta ($3,520) — could shift that year.


**Missing dates in the journal (cannot fetch price path — excluded):**
- SQQQ (2023): no purchase/sold date recorded; actual return -72.5%, loss $-17,985. A hard stop would very likely have helped materially here, but it cannot be quantified without dates.

## Caveats

- **Redeployment held constant.** Freed capital from an early stop is NOT reinvested here; in reality a hard stop changes which positions exist and frees cash to redeploy. This is a first-order, per-trade estimate — not a full path simulation.
- **Open positions at year-end are not in the closed-trade log**, so trades still open aren't measured.
- **RAW (unadjusted) prices** used to match his fills; dividends/splits during a hold could shift a breach flag slightly. Split-suspect trades are excluded and listed above.
- **Intraday path assumption:** breach is detected on daily LOW; the exact fill within a breaching day is approximated (open if gapped through the stop, else the stop price). Real slippage on a gap could be worse.
- Stop is anchored to HIS entry price (Cost/sh), not to a Tiingo reference, per the brief.