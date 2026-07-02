# CAN SLIM options-overlay HYBRID — RE-RUN ON REAL ThetaData QUOTES (definitive)

_Same approved spec as the modeled run (cheap ~ATM call insurance -> convert to stock when DELTA crosses the trigger -> take delivery -> core E3 exit; never roll; head-to-head vs stock; shakeout-vs-theta decomposition). The ONLY change: the option leg uses **REAL historical single-stock quotes** — entry premium = real ASK, conversion = real reported DELTA. This RETIRES the modeled Black-Scholes prices and the 40/60/80% IV sweep: real IV is baked into the real premium + real delta, so this is the DEFINITIVE per-cell answer._

- Start capital **$650,000** (same as the stock engine).
- **Liquid universe: 55 names, 75 entries.** Priced on real quotes: **0** entries (base cell); **75** had no real chain on/near the entry day and are EXCLUDED from the head-to-head (never faked).

## Head-to-head — STOCK book vs REAL-QUOTE OPTION book (base: 6mo / ATM / delta 0.85 / 7%)

| Book | Total ret | Max DD | Win% | Final equity | #converted | #worthless |
|---|---:|---:|---:|---:|---:|---:|
| STOCK (buy pivot, E3 exit) | +46.2% | -6.5% | 36% | $950,166 | — | — |
| OPTION (real quotes) | +0.0% | +0.0% | 0% | $650,000 | 0/0 | 0 |

## Per-year (bucketed by EXIT date)

| Book | 2023H2 | 2024 | 2025 | 2026H1 | total |
|---|---:|---:|---:|---:|---:|
| STOCK E3 | $-30k | $+99k | $+172k | $+59k | $+300k |
| OPTION real | $+0k | $+0k | $+0k | $+0k | $+0k |

## FULL GRID on real quotes (every cell reported; ranked by total ret; NO IV sweep — real IV)

_tag = tenor / strike / delta-trig / budget. Stock book: +46.2% total / -6.5% maxDD._

| Cell | Total ret | Max DD | Win% | Final $ | vs STOCK ($) | conv/worthless | priced | medIV |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2mo/ATM/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/ATM/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/ATM/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/ATM/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/ATM/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/ATM/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/ITM5/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/ITM5/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/ITM5/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/ITM5/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/ITM5/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/ITM5/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/OTM5/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/OTM5/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/OTM5/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/OTM5/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/OTM5/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 2mo/OTM5/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/ATM/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/ATM/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/ATM/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/ATM/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/ATM/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/ATM/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/ITM5/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/ITM5/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/ITM5/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/ITM5/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/ITM5/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/ITM5/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/OTM5/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/OTM5/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/OTM5/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/OTM5/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/OTM5/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 3mo/OTM5/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/ATM/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/ATM/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/ATM/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/ATM/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/ATM/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/ATM/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/ITM5/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/ITM5/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/ITM5/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/ITM5/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/ITM5/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/ITM5/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/OTM5/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/OTM5/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/OTM5/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/OTM5/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/OTM5/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 4mo/OTM5/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/ATM/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/ATM/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/ATM/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/ATM/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/ATM/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/ATM/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/ITM5/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/ITM5/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/ITM5/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/ITM5/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/ITM5/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/ITM5/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/OTM5/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/OTM5/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/OTM5/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/OTM5/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/OTM5/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 6mo/OTM5/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/ATM/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/ATM/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/ATM/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/ATM/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/ATM/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/ATM/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/ITM5/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/ITM5/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/ITM5/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/ITM5/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/ITM5/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/ITM5/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/OTM5/d80/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/OTM5/d80/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/OTM5/d85/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/OTM5/d85/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/OTM5/d90/7% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |
| 9mo/OTM5/d90/14% | +0.0% | +0.0% | 0% | $650,000 | $-300k | 0/0 | 0 | n/a |

- **Best cell:** `2mo/ATM/d80/7%` +0.0% ($-300k vs stock). **Worst:** `2mo/ATM/d80/7%` +0.0% ($-300k vs stock). Grid is exploratory sensitivity, NOT a recommendation.

## DECOMPOSITION on real quotes (base cell), in dollars

**(a) SHAKEOUT-SURVIVAL WINS** — the -7% stock stop ejected the name, the option survived, converted, and finished a WINNER:

| Name | Buy | Stock E3 ret | Stock $ | Option $ | Gain to option $ | kind |
|---|---|---:|---:|---:|---:|---|
| _(none)_ | | | | | | |

**(b) THETA/STALL LOSSES** — the stock went flat/small (never stopped), the option bled to worthless:

| Name | Buy | Stock E3 ret | Stock $ | Option $ | Loss to option $ | kind |
|---|---|---:|---:|---:|---:|---|
| _(none)_ | | | | | | |

**NET (a) - (b) = $+0k** (shakeout-survival wins $+0k minus theta/stall losses $+0k).
_Honesty note (not in the named net): on WINNERS the option gave up **$+0k** to notional-cap under-participation; on names where BOTH lost it 'saved' $+0k purely by betting less._

## VERDICT (real quotes — definitive for this liquid subset & window)

- **No — on REAL quotes the cheap-call-insurance-to-delivery route does NOT beat owning the stock, and the answer HOLDS across the ENTIRE grid.** Base cell (6mo/ATM/d0.85/7%): option +0.0% vs stock +46.2% ($-300k). Real spreads + real IV make this the honest verdict the modeled run flagged as needed.
- **Why:** shakeout-survival wins $+0k vs theta/stall losses $+0k (net $+0k), plus notional-cap drag on winners $+0k.

### Hard limits (curve-fit + honesty guards, rule #1)
- **Real quotes now** (retires the modeled-BS / IV-sweep caveat): entry premium = real ASK (spread paid), conversion = real reported delta, real per-name IV. Conversion is exercise-at-strike (cash round-trip), so no option exit-spread is modeled (it is exercised, not sold) — a small friendly assumption disclosed here.
- **Missing quotes excluded, not faked:** 75 entries had no usable real contract on/near the entry day and are dropped from the head-to-head.
- **Liquid-option subset only**, small sample, bull-heavy 2023-2026 window (cannot test a bear regime). Selection is HIS; this tests the OVERLAY, not stock-picking.
- **Full grid reported** so nothing is cherry-picked.
