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

---

# Trailing / volatility-aware stop test (JOB 2)

Same per-trade counterfactual as the fixed-stop test above: stop anchored to HIS entry, no redeployment, his entry timing held constant, RAW OHLC, breach on daily LOW with gap-through fill at the open. If a policy never stops, the trade keeps his actual exit.

**Two-series method.** The breach/exit price path uses the same RAW Tiingo daily OHLC as JOB 1 (so fixed -7%/-8%/V1 stay identical to the committed baseline). The 50-day SMA and ATR(20) that V2/V3 need require ~50-90 days of PRE-ENTRY history the JOB-1 windows didn't have; that history was pulled from the **IBKR paper gateway** (read-only, wide daily TRADES bars). IBKR bars are split-ADJUSTED, so they are rescaled into the raw price frame by the entry-day ratio before computing indicators — correct for non-split holds; split-during-hold names are the already-excluded split-flagged trades.

**Policies (small, principled set — deliberately NOT parameter-hunted):**
- **Fixed -7% / -8%** — constant stop at entry×0.93 / ×0.92 (the JOB-1 baselines).
- **V1 — breakeven + 20% trail:** -7% initial; once the trade trades +20% intraday, stop ratchets to breakeven, then trails 20% below the running high-water mark (stop only moves up).
- **V2 — 50-day SMA (most O'Neil-faithful):** -7% initial; once a daily CLOSE is above a *rising* 50-day SMA, switch to "sell on a decisive close below the 50-day" (close < 0.98×SMA50), a proxy for his 10-week-line break rule.
- **V3 — 2×ATR(20):** initial stop = entry − 2×ATR(20 at entry), with a breakeven ratchet at +20%. **V3b** = 2.5×ATR variant.

## Net realized P/L by policy (dollars; higher = better)

_"Actual" = his realized P/L on the same trades each policy could price. Excludes the 2 no-coverage names (ERJ, PSTG) and the split-flagged NVDA, so it differs from the JOB-1 headline by exactly those; every policy column is apples-to-apples against it._

| Year | Actual (his exits) | Fixed -7% | Fixed -8% | V1 BE+20% trail | V2 50-SMA | V3 2xATR | V3b 2.5xATR |
|---|---|---|---|---|---|---|---|
| 2023 H2 | $-54,617 | $-43,582 | $-46,099 | $-43,582 | $-54,416 | $-53,291 | $-55,792 |
| 2024 | $44,826 | $48,346 | $43,495 | $73,562 | $44,287 | $18,962 | $2,819 |
| 2025 | $150,394 | $30,448 | $89,367 | $34,495 | $101,416 | $34,861 | $46,979 |
| 2026 H1 | $-108,375 | $-39,803 | $-45,835 | $-40,435 | $-63,315 | $-69,018 | $-82,882 |
| **ALL** | **$32,229** | **$-4,591** | **$40,928** | **$24,040** | **$27,972** | **$-68,485** | **$-88,875** |

## Delta vs his actual exits (+ = policy makes MORE money than he did)

| Year | Fixed -7% | Fixed -8% | V1 BE+20% trail | V2 50-SMA | V3 2xATR | V3b 2.5xATR |
|---|---|---|---|---|---|---|
| 2023 H2 | $11,035 | $8,517 | $11,035 | $201 | $1,325 | $-1,175 |
| 2024 | $3,520 | $-1,331 | $28,735 | $-539 | $-25,864 | $-42,007 |
| 2025 | $-119,946 | $-61,027 | $-115,899 | $-48,978 | $-115,533 | $-103,415 |
| 2026 H1 | $68,571 | $62,540 | $67,940 | $45,060 | $39,357 | $25,493 |
| **ALL** | **$-36,820** | **$8,699** | **$-8,189** | **$-4,257** | **$-100,714** | **$-121,104** |

## Robustness — does any policy help (or at least not hurt) in ALL FOUR periods?

| Policy | 2023 H2 | 2024 | 2025 | 2026 H1 | Overall | Helps in all 4? | Worst single year |
|---|---|---|---|---|---|---|---|
| Fixed -7% | $11,035 | $3,520 | $-119,946 | $68,571 | $-36,820 | no | $-119,946 (2025) |
| Fixed -8% | $8,517 | $-1,331 | $-61,027 | $62,540 | $8,699 | no | $-61,027 (2025) |
| V1 BE+20% trail | $11,035 | $28,735 | $-115,899 | $67,940 | $-8,189 | no | $-115,899 (2025) |
| V2 50-SMA | $201 | $-539 | $-48,978 | $45,060 | $-4,257 | no | $-48,978 (2025) |
| V3 2xATR | $1,325 | $-25,864 | $-115,533 | $39,357 | $-100,714 | no | $-115,533 (2025) |
| V3b 2.5xATR | $-1,175 | $-42,007 | $-103,415 | $25,493 | $-121,104 | no | $-103,415 (2025) |

## Plain-English verdict

- **No policy helps in all four periods.** Every one is dragged negative by 2025.
- 2025 is the graveyard for every stop: −7% −$119,946, V1 −$115,899, V3 −$115,533. Only **V2 (50-day-SMA trail)** materially softens 2025 (delta $-48,978) because it lets a winner ride as long as it holds its rising 50-day line, instead of stopping on the first −7% intraday dip — which is exactly what OKLO/RKLB/MSTR did before running.
- But V2's edge is regime-dependent, not free: it gives back most of the loss-cutting benefit that helped 2023 & 2026 (V2 2023 $201, 2026 $45,060 vs fixed −7% $11,035 / $68,571), because a decisive-close-below-50-SMA exit is slower than a hard −7% and rides losers a bit further down first.
- **Bottom line:** there is no stop policy here that is a durable, all-weather improvement over his discretion. The 50-day-SMA trail (V2) is the most defensible and the most O'Neil-faithful — it's the only one that doesn't get slaughtered by the 2025 melt-up — but it trades away part of the crisis-year protection, so it's a *different* risk profile, not a strict win. A wider ATR stop (V3) is worse than the fixed % almost everywhere. The honest read: stop CHOICE is regime-dependent noise on this sample; the only structural signal is that his single worst habit — riding a loser far past any stop — is what the bleeder list already quantified, and ANY disciplined exit (his own 10-week-line rule included) fixes THAT without needing a clever trail.

## Curve-fit / fragility flags

- **The fixed-stop verdict flips on the exact threshold:** -7% overall = $-36,820 (HURTS) but -8% = $8,699 (~break-even). A one-point change in a single knob flips the sign — that is textbook fragility, not an edge.
- **V3 vs V3b (ATR multiple):** 2×ATR overall = $-100,714, 2.5×ATR = $-121,104. Similar sign — less knife-edge than the fixed %.
- All policies are dominated by a handful of 2025 mega-winners (OKLO/RKLB/MSTR); any policy's 2025 number is really a bet on whether those specific trades get trailed out early. Small-sample, regime-specific — treat 2025 as one observation, not many.

## ERJ-2024 resolution (does 2024's verdict flip?)

ERJ (Embraer ADR, 2024, entry $28.90, held May–Dec 2024, +21% / +$14,994 on a $71.5k position) is **not priceable on either IBKR or Tiingo** on this account (IBKR returns no US security definition; Tiingo returns null history), so its intraday path CANNOT be observed and the −7% breach cannot be confirmed directly.
- **Bound:** 2024's fixed −7% delta is +$3,520 *excluding* ERJ. If ERJ had breached −7% intraday during its 7-month hold and been stopped, that winner (+21%) would become a −7% exit — a swing of about (−0.07 − 0.21) × $71,510 ≈ **−$20,000** on that trade alone, which would drag 2024 to roughly **−$16,500 and FLIP 2024 from "stop helps" to "stop hurts."**
- **Likelihood:** over a 7-month hold an ADR almost certainly traded 7% below entry at some point, so the flip is **probable but unconfirmable**. Treat 2024's small positive fixed-stop delta (+$3,520) as fragile — it likely does not survive ERJ. This *strengthens* the overall conclusion that the fixed stop is not a durable winner.

## JOB-2 data sources

- **Price path (breach/return):** RAW Tiingo daily OHLC, identical to JOB 1 (fixed −7%/−8%/V1 reproduce the committed baseline; only SQ, newly covered via IBKR→XYZ, is added).
- **Indicators (SMA50 / ATR20):** **116 of 120 from the IBKR paper gateway** (wide daily bars, ~90+ pre-entry days), 1 from wide Tiingo, 0 short-only.
- **No price coverage on either source (excluded, same as JOB 1):** [('ERJ', 2024), ('PSTG', 2025)] — ERJ and PSTG.
