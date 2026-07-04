# CAN SLIM options-overlay HYBRID — RE-RUN ON REAL ThetaData QUOTES (definitive)

_Same approved spec as the modeled run (cheap ~ATM call insurance -> convert to stock when DELTA crosses the trigger -> take delivery -> core E3 exit; never roll; head-to-head vs stock; shakeout-vs-theta decomposition). The ONLY change: the option leg uses **REAL historical single-stock quotes** — entry premium = real ASK, conversion = real reported DELTA. This RETIRES the modeled Black-Scholes prices and the 40/60/80% IV sweep: real IV is baked into the real premium + real delta, so this is the DEFINITIVE per-cell answer._

- Start capital **$650,000** (same as the stock engine).
- **Liquid universe: 55 names, 75 entries.** Priced on real quotes: **73** entries (base cell); **2** had no real chain on/near the entry day and are EXCLUDED from the head-to-head (never faked).
- **Median real entry IV (base cell): 52%** — the observed vol that replaces the modeled sweep.

## Head-to-head — STOCK book vs REAL-QUOTE OPTION book (base: 6mo / ATM / delta 0.85 / 7%)

| Book | Total ret | Max DD | Win% | Final equity | #converted | #worthless |
|---|---:|---:|---:|---:|---:|---:|
| STOCK (buy pivot, E3 exit) | +46.2% | -6.5% | 36% | $950,166 | — | — |
| OPTION (real quotes) | +23.9% | -10.1% | 44% | $805,656 | 50/73 | 23 |

## Per-year (bucketed by EXIT date)

| Book | 2023H2 | 2024 | 2025 | 2026H1 | total |
|---|---:|---:|---:|---:|---:|
| STOCK E3 | $-30k | $+99k | $+172k | $+59k | $+300k |
| OPTION real | $-2k | $-20k | $+93k | $+84k | $+156k |

## FULL GRID on real quotes (every cell reported; ranked by total ret; NO IV sweep — real IV)

_tag = tenor / strike / delta-trig / budget. Stock book: +46.2% total / -6.5% maxDD._

| Cell | Total ret | Max DD | Win% | Final $ | vs STOCK ($) | conv/worthless | priced | medIV |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2mo/OTM5/d85/14% | +115.7% | -13.6% | 37% | $1,402,148 | $+452k | 39/34 | 73 | 53% |
| 2mo/OTM5/d90/14% | +115.1% | -16.3% | 37% | $1,398,252 | $+448k | 35/38 | 73 | 53% |
| 2mo/OTM5/d80/14% | +109.0% | -16.7% | 38% | $1,358,446 | $+408k | 41/32 | 73 | 53% |
| 2mo/ATM/d85/14% | +92.4% | -9.8% | 40% | $1,250,789 | $+301k | 45/28 | 73 | 53% |
| 2mo/ATM/d80/14% | +89.5% | -9.8% | 40% | $1,231,527 | $+281k | 49/24 | 73 | 53% |
| 2mo/ATM/d90/14% | +88.2% | -12.8% | 40% | $1,223,067 | $+273k | 43/30 | 73 | 53% |
| 3mo/OTM5/d85/14% | +73.9% | -11.1% | 40% | $1,130,628 | $+180k | 43/29 | 72 | 53% |
| 3mo/OTM5/d80/14% | +72.1% | -10.9% | 40% | $1,118,384 | $+168k | 45/27 | 72 | 53% |
| 3mo/OTM5/d90/14% | +71.7% | -13.4% | 39% | $1,116,345 | $+166k | 38/34 | 72 | 53% |
| 2mo/ITM5/d85/14% | +69.5% | -8.7% | 41% | $1,101,887 | $+152k | 54/19 | 73 | 54% |
| 2mo/ITM5/d80/14% | +67.5% | -8.6% | 40% | $1,088,945 | $+139k | 59/14 | 73 | 54% |
| 2mo/ITM5/d90/14% | +67.0% | -11.5% | 41% | $1,085,568 | $+135k | 46/27 | 73 | 54% |
| 3mo/ATM/d85/14% | +61.6% | -9.4% | 42% | $1,050,381 | $+100k | 47/25 | 72 | 52% |
| 3mo/ATM/d90/14% | +60.9% | -11.4% | 44% | $1,045,718 | $+96k | 44/28 | 72 | 52% |
| 2mo/OTM5/d85/7% | +56.2% | -8.6% | 37% | $1,015,144 | $+65k | 39/34 | 73 | 53% |
| 2mo/OTM5/d90/7% | +53.7% | -11.9% | 37% | $999,369 | $+49k | 35/38 | 73 | 53% |
| 4mo/OTM5/d80/14% | +53.5% | -10.5% | 42% | $998,060 | $+48k | 50/23 | 73 | 53% |
| 2mo/OTM5/d80/7% | +53.3% | -10.4% | 38% | $996,291 | $+46k | 41/32 | 73 | 53% |
| 6mo/OTM5/d90/14% | +52.0% | -9.7% | 40% | $988,258 | $+38k | 44/29 | 73 | 51% |
| 6mo/OTM5/d85/14% | +52.0% | -9.7% | 40% | $987,860 | $+38k | 47/26 | 73 | 51% |
| 4mo/OTM5/d85/14% | +50.3% | -12.4% | 42% | $977,167 | $+27k | 47/26 | 73 | 53% |
| 3mo/ATM/d80/14% | +50.3% | -11.0% | 39% | $977,057 | $+27k | 48/24 | 72 | 52% |
| 4mo/OTM5/d90/14% | +50.2% | -11.4% | 42% | $976,202 | $+26k | 43/30 | 73 | 53% |
| 4mo/ATM/d90/14% | +50.1% | -11.1% | 52% | $975,358 | $+25k | 49/24 | 73 | 52% |
| 4mo/ATM/d80/14% | +49.8% | -9.7% | 48% | $973,606 | $+23k | 52/21 | 73 | 52% |
| 6mo/ITM5/d90/14% | +48.4% | -10.4% | 45% | $964,283 | $+14k | 53/20 | 73 | 53% |
| 6mo/ITM5/d85/14% | +47.7% | -10.7% | 41% | $959,760 | $+10k | 54/19 | 73 | 53% |
| 4mo/ATM/d85/14% | +46.3% | -11.9% | 48% | $950,695 | $+1k | 50/23 | 73 | 52% |
| 3mo/ITM5/d90/14% | +45.3% | -10.9% | 46% | $944,328 | $-6k | 50/22 | 72 | 53% |
| 9mo/OTM5/d90/14% | +45.3% | -13.7% | 37% | $944,317 | $-6k | 44/29 | 73 | 52% |
| 9mo/OTM5/d85/14% | +45.2% | -14.7% | 38% | $943,595 | $-7k | 48/25 | 73 | 52% |
| 6mo/ATM/d85/14% | +43.3% | -13.4% | 44% | $931,549 | $-19k | 50/23 | 73 | 52% |
| 2mo/ATM/d85/7% | +43.3% | -6.3% | 40% | $931,305 | $-19k | 45/28 | 73 | 53% |
| 2mo/ATM/d80/7% | +42.0% | -6.3% | 40% | $922,797 | $-27k | 49/24 | 73 | 53% |
| 4mo/ITM5/d90/14% | +41.4% | -11.8% | 49% | $919,420 | $-31k | 52/21 | 73 | 54% |
| 4mo/ITM5/d85/14% | +40.5% | -11.8% | 47% | $913,349 | $-37k | 52/21 | 73 | 54% |
| 3mo/ITM5/d85/14% | +40.5% | -10.8% | 43% | $913,262 | $-37k | 54/18 | 72 | 53% |
| 6mo/OTM5/d80/14% | +39.2% | -7.3% | 38% | $904,756 | $-45k | 50/23 | 73 | 51% |
| 2mo/ATM/d90/7% | +39.0% | -9.7% | 40% | $903,582 | $-47k | 43/30 | 73 | 53% |
| 3mo/ITM5/d80/14% | +38.6% | -9.7% | 40% | $900,686 | $-49k | 58/14 | 72 | 53% |
| 6mo/ATM/d80/14% | +36.8% | -9.7% | 44% | $889,202 | $-61k | 54/19 | 73 | 52% |
| 2mo/ITM5/d85/7% | +35.6% | -5.4% | 41% | $881,442 | $-69k | 54/19 | 73 | 54% |
| 3mo/OTM5/d85/7% | +35.2% | -7.3% | 40% | $878,784 | $-71k | 43/29 | 72 | 53% |
| 3mo/ATM/d85/7% | +34.6% | -6.4% | 42% | $874,890 | $-75k | 47/25 | 72 | 52% |
| 3mo/OTM5/d80/7% | +34.1% | -7.2% | 40% | $871,653 | $-79k | 45/27 | 72 | 53% |
| 6mo/ATM/d90/14% | +33.8% | -12.0% | 42% | $869,711 | $-80k | 49/24 | 73 | 52% |
| 4mo/ITM5/d80/14% | +33.8% | -11.7% | 42% | $869,476 | $-81k | 58/15 | 73 | 54% |
| 2mo/ITM5/d80/7% | +33.7% | -5.3% | 40% | $868,771 | $-81k | 59/14 | 73 | 54% |
| 6mo/ITM5/d80/14% | +32.4% | -8.7% | 38% | $860,864 | $-89k | 56/17 | 73 | 53% |
| 3mo/ATM/d90/7% | +32.3% | -9.2% | 44% | $859,894 | $-90k | 44/28 | 72 | 52% |
| 2mo/ITM5/d90/7% | +32.2% | -8.6% | 41% | $859,426 | $-91k | 46/27 | 73 | 54% |
| 3mo/OTM5/d90/7% | +32.1% | -10.3% | 39% | $858,970 | $-91k | 38/34 | 72 | 53% |
| 9mo/OTM5/d80/14% | +32.0% | -15.9% | 38% | $858,018 | $-92k | 51/22 | 73 | 52% |
| 9mo/ATM/d85/14% | +30.0% | -16.8% | 41% | $844,810 | $-105k | 51/22 | 73 | 51% |
| 3mo/ATM/d80/7% | +29.9% | -7.2% | 39% | $844,473 | $-106k | 48/24 | 72 | 52% |
| 4mo/OTM5/d80/7% | +29.7% | -6.6% | 42% | $842,900 | $-107k | 50/23 | 73 | 53% |
| 3mo/ITM5/d90/7% | +28.4% | -9.0% | 46% | $834,408 | $-116k | 50/22 | 72 | 53% |
| 3mo/ITM5/d85/7% | +28.0% | -7.4% | 43% | $831,781 | $-118k | 54/18 | 72 | 53% |
| 4mo/ATM/d80/7% | +27.8% | -6.7% | 48% | $830,406 | $-120k | 52/21 | 73 | 52% |
| 9mo/ITM5/d90/14% | +27.1% | -17.5% | 40% | $826,035 | $-124k | 53/20 | 73 | 53% |
| 6mo/ITM5/d90/7% | +26.5% | -9.1% | 45% | $822,541 | $-128k | 53/20 | 73 | 53% |
| 4mo/OTM5/d85/7% | +26.5% | -9.4% | 42% | $822,007 | $-128k | 47/26 | 73 | 53% |
| 3mo/ITM5/d80/7% | +26.4% | -6.7% | 40% | $821,783 | $-128k | 58/14 | 72 | 53% |
| 4mo/OTM5/d90/7% | +26.4% | -8.9% | 42% | $821,524 | $-129k | 43/30 | 73 | 53% |
| 9mo/ITM5/d85/14% | +25.9% | -17.8% | 38% | $818,402 | $-132k | 53/20 | 73 | 53% |
| 4mo/ATM/d90/7% | +25.7% | -8.9% | 52% | $817,172 | $-133k | 49/24 | 73 | 52% |
| 6mo/ITM5/d85/7% | +25.6% | -9.8% | 41% | $816,217 | $-134k | 54/19 | 73 | 53% |
| 9mo/ATM/d80/14% | +25.5% | -14.5% | 38% | $815,686 | $-134k | 54/19 | 73 | 51% |
| 4mo/ATM/d85/7% | +24.2% | -9.2% | 48% | $807,495 | $-143k | 50/23 | 73 | 52% |
| 6mo/OTM5/d85/7% | +24.2% | -9.2% | 40% | $807,190 | $-143k | 47/26 | 73 | 51% |
| 6mo/ATM/d85/7% | +23.9% | -10.1% | 44% | $805,656 | $-145k | 50/23 | 73 | 52% |
| 6mo/OTM5/d90/7% | +23.9% | -9.2% | 40% | $805,311 | $-145k | 44/29 | 73 | 51% |
| 4mo/ITM5/d90/7% | +23.5% | -9.3% | 49% | $802,823 | $-147k | 52/21 | 73 | 54% |
| 4mo/ITM5/d85/7% | +22.8% | -9.4% | 47% | $798,066 | $-152k | 52/21 | 73 | 54% |
| 6mo/ATM/d90/7% | +20.8% | -9.4% | 42% | $785,160 | $-165k | 49/24 | 73 | 52% |
| 9mo/ITM5/d80/14% | +20.4% | -16.1% | 34% | $782,830 | $-167k | 57/16 | 73 | 53% |
| 4mo/ITM5/d80/7% | +19.7% | -8.2% | 42% | $778,007 | $-172k | 58/15 | 73 | 54% |
| 9mo/ATM/d90/14% | +17.7% | -18.9% | 40% | $765,076 | $-185k | 49/24 | 73 | 51% |
| 6mo/ATM/d80/7% | +17.3% | -7.1% | 44% | $762,388 | $-188k | 54/19 | 73 | 52% |
| 9mo/OTM5/d85/7% | +15.4% | -15.2% | 38% | $750,229 | $-200k | 48/25 | 73 | 52% |
| 6mo/ITM5/d80/7% | +15.4% | -7.4% | 38% | $750,204 | $-200k | 56/17 | 73 | 53% |
| 9mo/OTM5/d90/7% | +15.4% | -14.7% | 37% | $749,931 | $-200k | 44/29 | 73 | 52% |
| 6mo/OTM5/d80/7% | +14.4% | -6.7% | 38% | $743,424 | $-207k | 50/23 | 73 | 51% |
| 9mo/ATM/d85/7% | +13.7% | -16.7% | 41% | $739,263 | $-211k | 51/22 | 73 | 51% |
| 9mo/ITM5/d90/7% | +12.8% | -16.9% | 40% | $733,196 | $-217k | 53/20 | 73 | 53% |
| 9mo/ITM5/d85/7% | +12.2% | -17.0% | 38% | $729,174 | $-221k | 53/20 | 73 | 53% |
| 9mo/OTM5/d80/7% | +10.9% | -13.3% | 38% | $720,758 | $-229k | 51/22 | 73 | 52% |
| 9mo/ATM/d80/7% | +9.9% | -12.8% | 38% | $714,596 | $-236k | 54/19 | 73 | 51% |
| 9mo/ATM/d90/7% | +8.8% | -17.5% | 40% | $707,229 | $-243k | 49/24 | 73 | 51% |
| 9mo/ITM5/d80/7% | +8.4% | -13.3% | 34% | $704,458 | $-246k | 57/16 | 73 | 53% |

- **Best cell:** `2mo/OTM5/d85/14%` +115.7% ($+452k vs stock). **Worst:** `9mo/ITM5/d80/7%` +8.4% ($-246k vs stock). Grid is exploratory sensitivity, NOT a recommendation.

## DECOMPOSITION on real quotes (base cell), in dollars

**(a) SHAKEOUT-SURVIVAL WINS** — the -7% stock stop ejected the name, the option survived, converted, and finished a WINNER:

| Name | Buy | Stock E3 ret | Stock $ | Option $ | Gain to option $ | kind |
|---|---|---:|---:|---:|---:|---|
| SMCI | 2023-12-19 | -7.0% | $-4k | $+45k | $+49k | itm-expiry-run |
| PLTR | 2024-07-05 | -11.5% | $-6k | $+35k | $+41k | itm-expiry-run |
| GEV | 2025-01-07 | -10.5% | $-8k | $+15k | $+23k | delta-convert-run |
| VRT | 2025-08-28 | -7.8% | $-4k | $+14k | $+18k | itm-expiry-run |
| RKLB | 2025-06-09 | -7.0% | $-3k | $+13k | $+17k | itm-expiry-run |
| HIMS | 2024-10-22 | -7.0% | $-4k | $+9k | $+13k | itm-expiry-run |
| FIX | 2024-08-27 | -8.4% | $-6k | $+6k | $+12k | delta-convert-run |
| TOST | 2024-05-09 | -11.9% | $-7k | $+4k | $+11k | itm-expiry-run |
| APH | 2026-01-12 | -10.4% | $-6k | $+2k | $+9k | delta-convert-run |
| PLTR | 2025-04-15 | -7.0% | $-2k | $+5k | $+7k | itm-expiry-run |
| AXON | 2023-11-08 | -7.0% | $-4k | $+2k | $+7k | delta-convert-run |
| PLTR | 2023-11-15 | -14.1% | $-6k | $+0k | $+6k | delta-convert-run |
| PLTR | 2025-05-05 | -8.9% | $-5k | $+0k | $+5k | delta-convert-run |

**(b) THETA/STALL LOSSES** — the stock went flat/small (never stopped), the option bled to worthless:

| Name | Buy | Stock E3 ret | Stock $ | Option $ | Loss to option $ | kind |
|---|---|---:|---:|---:|---:|---|
| NVDA | 2024-05-15 | +25.0% | $+6k | $-25k | $+31k | expired-worthless |
| NVDA | 2024-05-15 | +25.0% | $+6k | $-25k | $+31k | expired-worthless |
| SMR | 2025-05-23 | +30.1% | $+11k | $-2k | $+13k | expired-worthless |
| CLS | 2026-04-13 | -0.7% | $0k | $-8k | $+8k | expired-worthless |
| ANET | 2025-09-09 | -1.0% | $-1k | $-4k | $+4k | expired-worthless |
| CCJ | 2024-04-12 | +0.9% | $+0k | $-3k | $+4k | expired-worthless |
| DDOG | 2023-12-26 | +-0.0% | $0k | $-3k | $+3k | expired-worthless |
| UBER | 2025-05-12 | -1.1% | $-1k | $-3k | $+2k | expired-worthless |
| DXCM | 2023-06-29 | -4.6% | $-2k | $-1k | $0k | expired-worthless |

**NET (a) - (b) = $+123k** (shakeout-survival wins $+217k minus theta/stall losses $+94k).
_Honesty note (not in the named net): on WINNERS the option gave up **$+118k** to notional-cap under-participation; on names where BOTH lost it 'saved' $-54k purely by betting less._

## VERDICT (real quotes — definitive for this liquid subset & window)

- **Mixed on real quotes** — base cell option +23.9% vs stock +46.2%; some cells beat the stock, some don't (see grid). The cell-dependence is itself the finding — not a robust standalone edge.
- **Why:** shakeout-survival wins $+217k vs theta/stall losses $+94k (net $+123k), plus notional-cap drag on winners $+118k.

### Hard limits (curve-fit + honesty guards, rule #1)
- **Real quotes now** (retires the modeled-BS / IV-sweep caveat): entry premium = real ASK (spread paid), conversion = real reported delta, real per-name IV. Conversion is exercise-at-strike (cash round-trip), so no option exit-spread is modeled (it is exercised, not sold) — a small friendly assumption disclosed here.
- **Missing quotes excluded, not faked:** 2 entries had no usable real contract on/near the entry day and are dropped from the head-to-head.
- **Liquid-option subset only**, small sample, bull-heavy 2023-2026 window (cannot test a bear regime). Selection is HIS; this tests the OVERLAY, not stock-picking.
- **Full grid reported** so nothing is cherry-picked.
