# CAN SLIM options-overlay HYBRID — cheap-ATM-call insurance -> delta-triggered delivery -> core exit

_One question: on his LIQUID-OPTION names, does holding a cheap ~ATM call as INSURANCE (premium == the -7% stop loss), converting to stock only when the modeled call DELTA crosses a threshold (it has become a stock proxy), then managing the delivered stock with the proven winning exit (E3), beat just owning the stock from the pivot? Calls are **MODELED with Black-Scholes** (price + delta), theta decay on-path, across an **IV sweep 40/60/80%**. Full spec: research/options_overlay_spec.md._

- Start capital **$650,000** (same as the stock engine).
- **Liquid-option universe: 55 names, 75 entries** (of his 96 names / 118 trades). Chosen as a KNOWN-OPTIONABLE whitelist (large/liquid growth leaders, well-known ADRs, liquid ETFs) that plausibly had actively-quoted listed options 2019-2026. Thin small-caps / low-price names are EXCLUDED — a modeled BS price would misrepresent their real fills.
- **IN (liquid):** AAPL, AEM, AIT, ANET, APH, APP, ARM, AXON, BIRK, CCJ, CLS, CRDO, CROX, DDOG, DKS, DUOL, DXCM, ELF, FIX, FTNT, GEV, HIMS, HOOD, IBIT, IBKR, IONQ, IR, IREN, MCK, MDB, MOD, MSTR, NBIS, NVDA, OKLO, ONON, PLTR, RBLX, RKLB, SCCO, SMCI, SMR, SNPS, SQ, STRL, SYM, TOST, TSLA, TSM, UBER, UHS, URBN, VKTX, VRT, ZS
- **OUT (excluded):** AAON, ACMR, ADMA, AGX, ALMU, AMSC, APLD, APPF, AXGN, BLBD, BPMC, ERJ, HLI, HMY, HXL, IOT, KD, KRMN, LOAR, MNSO, MTRX, MTSI, NTGR, PRCT, PSTG, Q, QUBT, RKT, ROAD, SEI, SQM, TGTX, TILE, TS, TSSI, UFO, UFPT, VIAV, WAY, WLDN, YELP

## Head-to-head — STOCK book vs OPTION book (base cell: 6mo / ATM / delta 0.85 / 7% / IV 60%)

| Book | Total ret | Max DD | Win% | Final equity | #converted | #worthless |
|---|---:|---:|---:|---:|---:|---:|
| STOCK (buy pivot, E3 exit) | +34.3% | -16.9% | 33% | $872,880 | — | — |
| OPTION (base cell) | +34.4% | -3.2% | 52% | $873,591 | 55/75 | 20 |

## Per-year (bucketed by EXIT date) — bull 2024/2025 vs choppy stretches

| Book | 2023H2 | 2024 | 2025 | 2026H1 | total |
|---|---:|---:|---:|---:|---:|
| STOCK E3 | $-30k | $+23k | $+169k | $+61k | $+223k |
| OPTION base | $-2k | $+83k | $+100k | $+44k | $+224k |

## TIME-TO-CONVERSION — how long until a winner gets deep enough ITM to take delivery?

_His winners are SLOW (his realized book: median hold ~84 days, big winners ~113 days, only ~8% resolve in <=1 month; losers die in ~1 month). So the option must SURVIVE long enough for a slow winner to push the call's delta to the trigger. For eventual-WINNER names (delta reaches the trigger within a 1-yr horizon and the delivered stock finishes positive), the distribution of DAYS-FROM-ENTRY until the delta trigger (base: ATM / delta 0.85 / IV 60%):_

| n winners | 25th | 50th (median) | 75th | 90th |
|---:|---:|---:|---:|---:|
| 47 | 52d | 93d | 177d | 273d |

### Tenor crossover — winners CAPTURED (convert before expiry) vs LOST (expire first)

_The time-vs-premium tradeoff: shorter tenor = cheaper / more notional but EXPIRES on slow winners; longer tenor = survives slow winners but costs more premium / buys less notional. Base: ATM / delta 0.85 / IV 60% / 7% budget._

| Tenor | Winners captured | Winners lost (expired first) | Capture rate | Avg premium (% of alloc) | Avg notional/stock-cost |
|---|---:|---:|---:|---:|---:|
| 2mo | 15 | 32 | 32% | 6.1% | 0.6x |
| 3mo | 23 | 24 | 49% | 6.4% | 0.52x |
| 4mo | 28 | 19 | 60% | 6.7% | 0.46x |
| 6mo | 35 | 12 | 74% | 7.3% | 0.41x |
| 9mo | 42 | 5 | 89% | 8.2% | 0.37x |

- **Robust tenor (reasoned from the crossover, NOT the best backtest cell):** the median winner takes **93 days** to reach the delta trigger, the 75th percentile **177 days**, the 90th **273 days** — so any tenor shorter than ~6mo structurally EXPIRES on a large fraction of the slow winners (2mo captures just 32%, 4mo 60%). No tenor in the grid reaches 90% capture; the most robust is **9mo** (capture 89% of eventual-winners, avg premium 8.2% of alloc, notional 0.37x). The tension is real: even 9mo just misses the slowest ~10% (p90 ~273d), and buying that survival costs more premium and buys LESS notional (0.6x at 2mo -> 0.37x at 9mo). The data says a SLOW system needs a LONG tenor (6-9mo), which is exactly what erodes the notional the option can afford — this tradeoff, not a single P&L peak, is the finding. Reported as a curve; not tuned.

## IV SENSITIVITY (6mo / ATM / delta 0.85 / 7%) — does the verdict hold or flip?

| IV | Option total ret | Max DD | Win% | Final $ | vs STOCK ($) |
|---:|---:|---:|---:|---:|---:|
| 40% | +52.5% | -7.5% | 53% | $991,188 | $+118k |
| 60% | +34.4% | -3.2% | 52% | $873,591 | $+1k |
| 80% | +16.6% | -5.5% | 47% | $758,210 | $-115k |

## FULL TEST GRID (exploratory, NOT tuned — every cell reported; ranked by total ret)

_tag = tenor / strike / delta-trig / budget / IV. The stock book is +34.3% total / -16.9% maxDD for reference._

| Cell | Total ret | Max DD | Win% | Final $ | vs STOCK ($) | conv/worthless |
|---|---:|---:|---:|---:|---:|---:|
| 2mo/OTM5/d90/14%/IV40 | +220.4% | -28.1% | 40% | $2,082,671 | $+1,210k | 48/27 |
| 2mo/OTM5/d85/14%/IV40 | +220.2% | -28.1% | 40% | $2,081,201 | $+1,208k | 48/27 |
| 2mo/OTM5/d80/14%/IV40 | +189.7% | -32.2% | 39% | $1,883,135 | $+1,010k | 49/26 |
| 4mo/OTM5/d90/14%/IV40 | +157.5% | -10.5% | 47% | $1,674,050 | $+801k | 50/25 |
| 3mo/OTM5/d90/14%/IV40 | +154.9% | -26.6% | 44% | $1,656,645 | $+784k | 48/27 |
| 3mo/OTM5/d85/14%/IV40 | +151.2% | -26.6% | 44% | $1,632,956 | $+760k | 49/26 |
| 3mo/OTM5/d80/14%/IV40 | +149.2% | -26.8% | 44% | $1,619,945 | $+747k | 50/25 |
| 2mo/ATM/d90/14%/IV40 | +137.5% | -24.1% | 44% | $1,543,958 | $+671k | 52/23 |
| 4mo/OTM5/d85/14%/IV40 | +135.3% | -25.1% | 45% | $1,529,680 | $+657k | 52/23 |
| 4mo/OTM5/d80/14%/IV40 | +129.0% | -27.3% | 44% | $1,488,302 | $+615k | 53/22 |
| 2mo/ATM/d85/14%/IV40 | +121.9% | -24.6% | 43% | $1,442,117 | $+569k | 53/22 |
| 2mo/OTM5/d90/14%/IV60 | +120.9% | -11.3% | 39% | $1,435,957 | $+563k | 44/31 |
| 2mo/ATM/d80/14%/IV40 | +120.9% | -24.8% | 43% | $1,435,543 | $+563k | 54/21 |
| 6mo/OTM5/d85/14%/IV40 | +119.6% | -4.8% | 52% | $1,427,170 | $+554k | 54/21 |
| 6mo/OTM5/d90/14%/IV40 | +117.4% | -5.5% | 51% | $1,413,044 | $+540k | 52/23 |
| 3mo/ATM/d90/14%/IV40 | +116.1% | -22.8% | 47% | $1,404,563 | $+532k | 54/21 |
| 6mo/ATM/d90/14%/IV40 | +111.6% | -4.5% | 55% | $1,375,484 | $+503k | 56/19 |
| 2mo/OTM5/d90/7%/IV40 | +106.0% | -18.3% | 40% | $1,338,963 | $+466k | 48/27 |
| 2mo/OTM5/d85/7%/IV40 | +105.9% | -18.3% | 40% | $1,338,309 | $+465k | 48/27 |
| 4mo/ATM/d90/14%/IV40 | +104.6% | -21.6% | 48% | $1,330,219 | $+457k | 54/21 |
| 4mo/ATM/d85/14%/IV40 | +103.1% | -21.6% | 48% | $1,319,868 | $+447k | 55/20 |
| 6mo/OTM5/d80/14%/IV40 | +102.1% | -18.1% | 49% | $1,313,739 | $+441k | 56/19 |
| 6mo/ATM/d85/14%/IV40 | +99.4% | -13.9% | 53% | $1,296,373 | $+423k | 59/16 |
| 2mo/OTM5/d85/14%/IV60 | +99.0% | -25.0% | 39% | $1,293,779 | $+421k | 47/28 |
| 2mo/OTM5/d80/14%/IV60 | +98.0% | -25.0% | 39% | $1,286,930 | $+414k | 48/27 |
| 3mo/ATM/d85/14%/IV40 | +96.9% | -26.0% | 44% | $1,279,912 | $+407k | 57/18 |
| 4mo/ITM5/d90/14%/IV40 | +95.1% | -16.5% | 51% | $1,268,429 | $+396k | 56/19 |
| 3mo/ATM/d80/14%/IV40 | +93.8% | -25.7% | 44% | $1,259,887 | $+387k | 59/16 |
| 2mo/ITM5/d85/14%/IV40 | +91.6% | -19.1% | 48% | $1,245,336 | $+372k | 58/17 |
| 2mo/ITM5/d90/14%/IV40 | +91.6% | -19.1% | 48% | $1,245,336 | $+372k | 58/17 |
| 2mo/OTM5/d80/7%/IV40 | +91.1% | -21.1% | 39% | $1,242,124 | $+369k | 49/26 |
| 6mo/ATM/d80/14%/IV40 | +83.2% | -16.4% | 48% | $1,190,780 | $+318k | 60/15 |
| 3mo/ITM5/d90/14%/IV40 | +81.9% | -19.1% | 49% | $1,182,473 | $+310k | 59/16 |
| 6mo/ITM5/d90/14%/IV40 | +80.7% | -12.6% | 55% | $1,174,693 | $+302k | 61/14 |
| 9mo/OTM5/d90/14%/IV40 | +79.9% | -3.8% | 56% | $1,169,673 | $+297k | 51/24 |
| 2mo/ATM/d85/14%/IV60 | +79.2% | -20.5% | 41% | $1,164,482 | $+292k | 51/24 |
| 2mo/ATM/d90/14%/IV60 | +79.2% | -20.5% | 41% | $1,164,482 | $+292k | 51/24 |
| 3mo/OTM5/d90/14%/IV60 | +78.5% | -11.5% | 41% | $1,160,337 | $+287k | 45/30 |
| 3mo/OTM5/d85/14%/IV60 | +77.2% | -11.9% | 41% | $1,151,948 | $+279k | 46/29 |
| 4mo/OTM5/d90/14%/IV60 | +74.5% | -11.7% | 45% | $1,134,143 | $+261k | 49/26 |
| 2mo/ITM5/d80/14%/IV40 | +74.3% | -19.8% | 47% | $1,132,836 | $+260k | 62/13 |
| 4mo/ATM/d80/14%/IV40 | +74.0% | -24.4% | 43% | $1,130,740 | $+258k | 56/19 |
| 4mo/OTM5/d90/7%/IV40 | +73.5% | -6.5% | 47% | $1,127,904 | $+255k | 50/25 |
| 9mo/ATM/d90/14%/IV40 | +73.5% | -4.0% | 56% | $1,127,652 | $+255k | 52/23 |
| 3mo/ITM5/d85/14%/IV40 | +72.8% | -19.3% | 47% | $1,123,398 | $+251k | 61/14 |
| 6mo/ITM5/d85/14%/IV40 | +72.8% | -14.4% | 52% | $1,123,246 | $+250k | 62/13 |
| 9mo/OTM5/d85/14%/IV40 | +72.5% | -3.8% | 51% | $1,121,379 | $+248k | 51/24 |
| 2mo/ITM5/d90/14%/IV60 | +72.2% | -16.6% | 44% | $1,119,566 | $+247k | 55/20 |
| 3mo/ATM/d90/14%/IV60 | +72.1% | -11.6% | 41% | $1,118,884 | $+246k | 51/24 |
| 3mo/ITM5/d80/14%/IV40 | +71.4% | -20.0% | 45% | $1,114,136 | $+241k | 64/11 |
| 3mo/OTM5/d90/7%/IV40 | +70.6% | -16.4% | 44% | $1,109,024 | $+236k | 48/27 |
| 9mo/ATM/d85/14%/IV40 | +70.5% | -4.4% | 52% | $1,107,940 | $+235k | 56/19 |
| 4mo/OTM5/d85/14%/IV60 | +70.3% | -11.7% | 44% | $1,106,937 | $+234k | 49/26 |
| 4mo/ITM5/d85/14%/IV40 | +69.3% | -18.4% | 45% | $1,100,445 | $+228k | 59/16 |
| 3mo/OTM5/d85/7%/IV40 | +68.9% | -16.4% | 44% | $1,097,876 | $+225k | 49/26 |
| 3mo/OTM5/d80/7%/IV40 | +68.0% | -16.5% | 44% | $1,092,215 | $+219k | 50/25 |
| 2mo/ATM/d80/14%/IV60 | +67.5% | -23.1% | 40% | $1,088,833 | $+216k | 52/23 |
| 9mo/ITM5/d90/14%/IV40 | +66.5% | -4.1% | 52% | $1,082,238 | $+209k | 55/20 |
| 2mo/ATM/d90/7%/IV40 | +64.5% | -14.0% | 44% | $1,069,157 | $+196k | 52/23 |
| 3mo/ATM/d80/14%/IV60 | +63.6% | -20.6% | 41% | $1,063,197 | $+190k | 55/20 |
| 4mo/OTM5/d85/7%/IV40 | +62.6% | -15.7% | 45% | $1,056,908 | $+184k | 52/23 |
| 6mo/OTM5/d90/14%/IV60 | +61.7% | -8.0% | 53% | $1,051,298 | $+178k | 51/24 |
| 4mo/ITM5/d80/14%/IV40 | +61.7% | -18.5% | 43% | $1,050,924 | $+178k | 62/13 |
| 9mo/OTM5/d80/14%/IV40 | +61.5% | -14.9% | 51% | $1,049,469 | $+177k | 57/18 |
| 2mo/OTM5/d90/14%/IV80 | +60.9% | -13.2% | 36% | $1,045,569 | $+173k | 44/31 |
| 6mo/OTM5/d85/7%/IV40 | +60.8% | -2.5% | 52% | $1,045,085 | $+172k | 54/21 |
| 9mo/ITM5/d85/14%/IV40 | +60.8% | -11.1% | 52% | $1,045,021 | $+172k | 60/15 |
| 3mo/ATM/d85/14%/IV60 | +60.7% | -20.1% | 41% | $1,044,795 | $+172k | 53/22 |
| 2mo/OTM5/d85/14%/IV80 | +60.2% | -13.2% | 36% | $1,041,382 | $+169k | 44/31 |
| 6mo/OTM5/d90/7%/IV40 | +59.9% | -2.7% | 51% | $1,039,547 | $+167k | 52/23 |
| 3mo/OTM5/d80/14%/IV60 | +59.5% | -25.2% | 40% | $1,036,865 | $+164k | 48/27 |
| 4mo/OTM5/d80/7%/IV40 | +59.4% | -17.0% | 44% | $1,036,219 | $+163k | 53/22 |
| 9mo/ATM/d80/14%/IV40 | +58.7% | -13.7% | 49% | $1,031,758 | $+159k | 60/15 |
| 4mo/ATM/d90/14%/IV60 | +58.6% | -10.8% | 45% | $1,031,139 | $+158k | 51/24 |
| 3mo/ATM/d90/7%/IV40 | +58.4% | -13.0% | 47% | $1,029,447 | $+157k | 54/21 |
| 6mo/ATM/d90/7%/IV40 | +58.4% | -2.5% | 55% | $1,029,413 | $+157k | 56/19 |
| 4mo/ATM/d85/14%/IV60 | +58.3% | -11.4% | 44% | $1,028,644 | $+156k | 52/23 |
| 2mo/OTM5/d90/7%/IV60 | +57.8% | -6.6% | 39% | $1,025,757 | $+153k | 44/31 |
| 4mo/OTM5/d80/14%/IV60 | +57.7% | -21.7% | 43% | $1,024,913 | $+152k | 52/23 |
| 6mo/ITM5/d80/14%/IV40 | +56.9% | -16.7% | 47% | $1,019,595 | $+147k | 63/12 |
| 2mo/ATM/d85/7%/IV40 | +56.7% | -14.3% | 43% | $1,018,285 | $+145k | 53/22 |
| 2mo/ATM/d80/7%/IV40 | +56.2% | -14.4% | 43% | $1,014,998 | $+142k | 54/21 |
| 3mo/ITM5/d90/14%/IV60 | +55.7% | -18.2% | 47% | $1,012,018 | $+139k | 56/19 |
| 2mo/ITM5/d85/14%/IV60 | +55.5% | -18.5% | 41% | $1,010,550 | $+138k | 56/19 |
| 4mo/ATM/d90/7%/IV40 | +55.1% | -11.4% | 48% | $1,008,305 | $+135k | 54/21 |
| 4mo/ITM5/d90/14%/IV60 | +55.1% | -11.1% | 48% | $1,008,183 | $+135k | 53/22 |
| 4mo/ATM/d85/7%/IV40 | +54.3% | -11.4% | 48% | $1,003,130 | $+130k | 55/20 |
| 2mo/ITM5/d80/14%/IV60 | +54.3% | -18.9% | 41% | $1,002,811 | $+130k | 58/17 |
| 6mo/ATM/d90/14%/IV60 | +53.1% | -7.7% | 52% | $994,979 | $+122k | 54/21 |
| 6mo/OTM5/d80/7%/IV40 | +52.6% | -9.3% | 49% | $992,021 | $+119k | 56/19 |
| 6mo/ATM/d85/7%/IV40 | +52.5% | -7.5% | 53% | $991,188 | $+118k | 59/16 |
| 6mo/ATM/d85/14%/IV60 | +51.7% | -6.3% | 52% | $986,191 | $+113k | 55/20 |
| 6mo/OTM5/d85/14%/IV60 | +51.5% | -7.9% | 49% | $984,897 | $+112k | 51/24 |
| 4mo/ITM5/d90/7%/IV40 | +50.7% | -9.2% | 51% | $979,783 | $+107k | 56/19 |
| 6mo/ITM5/d90/14%/IV60 | +50.6% | -5.4% | 53% | $978,871 | $+106k | 59/16 |
| 2mo/ATM/d90/14%/IV80 | +49.4% | -11.7% | 39% | $971,252 | $+98k | 47/28 |
| 3mo/ATM/d85/7%/IV40 | +49.2% | -14.8% | 44% | $970,014 | $+97k | 57/18 |
| 6mo/ITM5/d85/14%/IV60 | +48.8% | -5.6% | 51% | $966,976 | $+94k | 59/16 |
| 6mo/OTM5/d80/14%/IV60 | +48.4% | -7.5% | 48% | $964,716 | $+92k | 53/22 |
| 3mo/ATM/d80/7%/IV40 | +47.8% | -14.5% | 44% | $960,607 | $+88k | 59/16 |
| 2mo/OTM5/d85/7%/IV60 | +47.6% | -14.5% | 39% | $959,705 | $+87k | 47/28 |
| 6mo/ITM5/d90/7%/IV40 | +47.6% | -7.1% | 55% | $959,441 | $+87k | 61/14 |
| 2mo/OTM5/d80/7%/IV60 | +47.1% | -14.5% | 39% | $956,281 | $+83k | 48/27 |
| 3mo/ITM5/d85/14%/IV60 | +46.8% | -18.6% | 44% | $954,051 | $+81k | 58/17 |
| 9mo/OTM5/d90/7%/IV40 | +46.3% | -2.8% | 56% | $950,829 | $+78k | 51/24 |
| 3mo/ITM5/d90/7%/IV40 | +45.9% | -10.8% | 49% | $948,619 | $+76k | 59/16 |
| 6mo/ATM/d80/7%/IV40 | +45.6% | -8.7% | 48% | $946,610 | $+74k | 60/15 |
| 2mo/ITM5/d85/7%/IV40 | +45.5% | -10.0% | 48% | $946,057 | $+73k | 58/17 |
| 2mo/ITM5/d90/7%/IV40 | +45.5% | -10.0% | 48% | $946,057 | $+73k | 58/17 |
| 4mo/ATM/d80/14%/IV60 | +45.1% | -21.9% | 43% | $943,365 | $+70k | 55/20 |
| 6mo/ITM5/d85/7%/IV40 | +44.8% | -7.9% | 52% | $941,445 | $+69k | 62/13 |
| 4mo/ITM5/d85/14%/IV60 | +44.5% | -19.3% | 47% | $939,441 | $+67k | 56/19 |
| 2mo/OTM5/d80/14%/IV80 | +44.2% | -24.7% | 36% | $937,545 | $+65k | 47/28 |
| 9mo/ITM5/d90/7%/IV40 | +43.5% | -3.3% | 52% | $932,927 | $+60k | 55/20 |
| 9mo/ATM/d90/7%/IV40 | +43.0% | -2.7% | 56% | $929,290 | $+56k | 52/23 |
| 9mo/OTM5/d85/7%/IV40 | +42.1% | -2.8% | 51% | $923,575 | $+51k | 51/24 |
| 9mo/ATM/d85/7%/IV40 | +41.3% | -3.4% | 52% | $918,493 | $+46k | 56/19 |
| 2mo/ATM/d85/7%/IV60 | +41.2% | -10.3% | 41% | $917,995 | $+45k | 51/24 |
| 2mo/ATM/d90/7%/IV60 | +41.2% | -10.3% | 41% | $917,995 | $+45k | 51/24 |
| 9mo/ITM5/d85/7%/IV40 | +41.2% | -5.0% | 52% | $917,782 | $+45k | 60/15 |
| 4mo/ATM/d80/7%/IV40 | +41.1% | -13.0% | 43% | $917,252 | $+44k | 56/19 |
| 6mo/ITM5/d80/14%/IV60 | +40.9% | -12.7% | 48% | $915,961 | $+43k | 62/13 |
| 9mo/ITM5/d80/14%/IV40 | +40.8% | -15.1% | 44% | $915,442 | $+43k | 62/13 |
| 9mo/ITM5/d90/14%/IV60 | +40.6% | -4.7% | 56% | $913,869 | $+41k | 53/22 |
| 3mo/OTM5/d90/7%/IV60 | +40.5% | -6.4% | 41% | $913,109 | $+40k | 45/30 |
| 6mo/ATM/d80/14%/IV60 | +40.4% | -15.5% | 48% | $912,713 | $+40k | 59/16 |
| 2mo/ITM5/d90/7%/IV60 | +40.3% | -8.6% | 44% | $911,674 | $+39k | 55/20 |
| 4mo/ITM5/d85/7%/IV40 | +40.1% | -10.2% | 45% | $910,599 | $+38k | 59/16 |
| 3mo/ITM5/d90/14%/IV80 | +40.0% | -11.5% | 41% | $910,275 | $+37k | 54/21 |
| 3mo/OTM5/d85/7%/IV60 | +39.8% | -6.6% | 41% | $908,755 | $+36k | 46/29 |
| 3mo/ITM5/d80/14%/IV60 | +39.4% | -20.3% | 43% | $906,034 | $+33k | 60/15 |
| 3mo/ATM/d90/7%/IV60 | +38.8% | -6.6% | 41% | $902,451 | $+30k | 51/24 |
| 2mo/ITM5/d85/14%/IV80 | +38.8% | -18.4% | 40% | $902,165 | $+29k | 55/20 |
| 2mo/ITM5/d90/14%/IV80 | +38.8% | -18.4% | 40% | $902,165 | $+29k | 55/20 |
| 4mo/OTM5/d90/7%/IV60 | +38.7% | -6.3% | 45% | $901,524 | $+29k | 49/26 |
| 3mo/ITM5/d85/7%/IV40 | +38.4% | -10.6% | 47% | $899,786 | $+27k | 61/14 |
| 3mo/ITM5/d80/7%/IV40 | +37.4% | -11.0% | 45% | $893,411 | $+21k | 64/11 |
| 9mo/ATM/d90/14%/IV60 | +37.4% | -5.7% | 49% | $893,141 | $+20k | 51/24 |
| 9mo/OTM5/d80/7%/IV40 | +37.0% | -7.3% | 51% | $890,207 | $+17k | 57/18 |
| 2mo/ITM5/d80/7%/IV40 | +36.9% | -10.3% | 47% | $889,909 | $+17k | 62/13 |
| 2mo/ATM/d80/14%/IV80 | +36.7% | -21.2% | 39% | $888,346 | $+15k | 51/24 |
| 2mo/ATM/d85/14%/IV80 | +36.7% | -21.2% | 39% | $888,346 | $+15k | 51/24 |
| 2mo/ATM/d80/7%/IV60 | +36.6% | -11.8% | 40% | $888,036 | $+15k | 52/23 |
| 4mo/OTM5/d85/7%/IV60 | +36.6% | -6.3% | 44% | $887,922 | $+15k | 49/26 |
| 4mo/ITM5/d90/7%/IV60 | +36.1% | -5.7% | 48% | $884,864 | $+12k | 53/22 |
| 9mo/ATM/d80/7%/IV40 | +35.8% | -7.1% | 49% | $882,582 | $+10k | 60/15 |
| 6mo/OTM5/d90/7%/IV60 | +35.3% | -4.4% | 53% | $879,176 | $+6k | 51/24 |
| 4mo/ATM/d90/7%/IV60 | +35.1% | -5.9% | 45% | $878,380 | $+6k | 51/24 |
| 6mo/ITM5/d80/7%/IV40 | +35.1% | -8.5% | 47% | $878,294 | $+5k | 63/12 |
| 4mo/ATM/d85/7%/IV60 | +35.1% | -6.1% | 44% | $878,196 | $+5k | 52/23 |
| 3mo/ATM/d85/14%/IV80 | +35.1% | -12.4% | 40% | $878,126 | $+5k | 51/24 |
| 6mo/ATM/d90/7%/IV60 | +35.1% | -3.5% | 52% | $878,027 | $+5k | 54/21 |
| 3mo/ATM/d90/14%/IV80 | +34.7% | -12.1% | 40% | $875,456 | $+3k | 50/25 |
| 6mo/ATM/d85/7%/IV60 | +34.4% | -3.2% | 52% | $873,591 | $+1k | 55/20 |
| 3mo/ATM/d80/7%/IV60 | +34.2% | -11.2% | 41% | $872,173 | $-1k | 55/20 |
| 6mo/ITM5/d90/7%/IV60 | +34.0% | -3.7% | 53% | $871,291 | $-2k | 59/16 |
| 4mo/ATM/d90/14%/IV80 | +34.0% | -12.8% | 47% | $871,044 | $-2k | 51/24 |
| 3mo/ATM/d85/7%/IV60 | +33.8% | -10.9% | 41% | $869,523 | $-3k | 53/22 |
| 3mo/OTM5/d90/14%/IV80 | +33.7% | -13.2% | 39% | $868,818 | $-4k | 45/30 |
| 3mo/ITM5/d90/7%/IV60 | +33.6% | -9.8% | 47% | $868,149 | $-5k | 56/19 |
| 3mo/OTM5/d85/14%/IV80 | +33.5% | -13.2% | 39% | $867,953 | $-5k | 45/30 |
| 6mo/ITM5/d85/7%/IV60 | +33.3% | -4.4% | 51% | $866,571 | $-6k | 59/16 |
| 4mo/ITM5/d80/7%/IV40 | +32.9% | -9.8% | 43% | $864,141 | $-9k | 62/13 |
| 3mo/OTM5/d80/14%/IV80 | +32.8% | -13.5% | 39% | $863,081 | $-10k | 46/29 |
| 3mo/ITM5/d85/14%/IV80 | +32.7% | -17.8% | 41% | $862,395 | $-10k | 56/19 |
| 9mo/ATM/d85/14%/IV60 | +32.6% | -5.5% | 49% | $861,743 | $-11k | 52/23 |
| 4mo/ITM5/d85/14%/IV80 | +32.5% | -10.7% | 44% | $861,282 | $-12k | 53/22 |
| 2mo/OTM5/d90/7%/IV80 | +32.5% | -7.1% | 36% | $861,109 | $-12k | 44/31 |
| 3mo/OTM5/d80/7%/IV60 | +32.4% | -13.0% | 40% | $860,466 | $-12k | 48/27 |
| 2mo/OTM5/d85/7%/IV80 | +32.2% | -7.1% | 36% | $859,155 | $-14k | 44/31 |
| 4mo/OTM5/d90/14%/IV80 | +31.7% | -13.3% | 43% | $856,266 | $-17k | 49/26 |
| 4mo/ITM5/d90/14%/IV80 | +31.6% | -10.7% | 44% | $855,449 | $-17k | 53/22 |
| 4mo/ITM5/d85/7%/IV60 | +31.5% | -9.5% | 47% | $854,690 | $-18k | 56/19 |
| 9mo/OTM5/d85/14%/IV60 | +31.5% | -5.9% | 51% | $854,439 | $-18k | 49/26 |
| 9mo/ITM5/d85/14%/IV60 | +31.3% | -6.1% | 49% | $853,569 | $-19k | 54/21 |
| 2mo/ATM/d90/7%/IV80 | +31.1% | -5.6% | 39% | $852,191 | $-21k | 47/28 |
| 6mo/OTM5/d85/7%/IV60 | +31.1% | -4.4% | 49% | $852,048 | $-21k | 51/24 |
| 4mo/OTM5/d80/7%/IV60 | +30.7% | -11.5% | 43% | $849,590 | $-23k | 52/23 |
| 9mo/OTM5/d90/14%/IV60 | +30.6% | -7.1% | 45% | $848,951 | $-24k | 49/26 |
| 3mo/ITM5/d85/7%/IV60 | +30.3% | -10.0% | 44% | $847,096 | $-26k | 58/17 |
| 6mo/ITM5/d90/14%/IV80 | +30.2% | -7.0% | 52% | $846,030 | $-27k | 59/16 |
| 2mo/ITM5/d85/7%/IV60 | +30.0% | -9.6% | 41% | $844,732 | $-28k | 56/19 |
| 9mo/ATM/d80/14%/IV60 | +29.7% | -5.5% | 47% | $843,359 | $-30k | 53/22 |
| 6mo/ATM/d90/14%/IV80 | +29.7% | -7.8% | 47% | $842,912 | $-30k | 54/21 |
| 6mo/OTM5/d80/7%/IV60 | +29.6% | -4.1% | 48% | $842,097 | $-31k | 53/22 |
| 2mo/ITM5/d80/7%/IV60 | +29.4% | -9.8% | 41% | $840,807 | $-32k | 58/17 |
| 4mo/ATM/d85/14%/IV80 | +29.3% | -12.8% | 44% | $840,504 | $-32k | 51/24 |
| 4mo/ITM5/d80/14%/IV60 | +29.1% | -20.7% | 41% | $838,872 | $-34k | 57/18 |
| 9mo/ITM5/d80/14%/IV60 | +28.9% | -9.2% | 48% | $837,937 | $-35k | 60/15 |
| 6mo/ITM5/d80/7%/IV60 | +28.8% | -7.9% | 48% | $837,373 | $-36k | 62/13 |
| 6mo/ATM/d80/7%/IV60 | +28.7% | -7.4% | 48% | $836,569 | $-36k | 59/16 |
| 4mo/ATM/d80/7%/IV60 | +28.6% | -11.6% | 43% | $836,171 | $-37k | 55/20 |
| 9mo/ITM5/d80/7%/IV40 | +28.3% | -7.6% | 44% | $834,229 | $-39k | 62/13 |
| 4mo/OTM5/d85/14%/IV80 | +28.3% | -13.5% | 41% | $833,695 | $-39k | 49/26 |
| 3mo/ITM5/d90/7%/IV80 | +28.1% | -6.3% | 41% | $832,758 | $-40k | 54/21 |
| 6mo/OTM5/d85/14%/IV80 | +27.3% | -9.0% | 48% | $827,771 | $-45k | 51/24 |
| 3mo/ITM5/d80/14%/IV80 | +26.7% | -18.3% | 39% | $823,684 | $-49k | 58/17 |
| 3mo/ITM5/d80/7%/IV60 | +26.6% | -10.8% | 43% | $822,785 | $-50k | 60/15 |
| 6mo/OTM5/d90/14%/IV80 | +26.5% | -8.9% | 47% | $822,101 | $-51k | 51/24 |
| 9mo/ITM5/d90/7%/IV60 | +25.7% | -5.4% | 56% | $817,005 | $-56k | 53/22 |
| 2mo/ATM/d80/7%/IV80 | +25.4% | -10.1% | 39% | $814,917 | $-58k | 51/24 |
| 2mo/ATM/d85/7%/IV80 | +25.4% | -10.1% | 39% | $814,917 | $-58k | 51/24 |
| 2mo/ITM5/d85/7%/IV80 | +25.4% | -9.3% | 40% | $814,876 | $-58k | 55/20 |
| 2mo/ITM5/d90/7%/IV80 | +25.4% | -9.3% | 40% | $814,876 | $-58k | 55/20 |
| 4mo/OTM5/d80/14%/IV80 | +25.3% | -13.5% | 40% | $814,135 | $-59k | 49/26 |
| 2mo/ITM5/d80/14%/IV80 | +25.2% | -20.4% | 37% | $813,846 | $-59k | 56/19 |
| 9mo/OTM5/d80/14%/IV60 | +25.1% | -6.0% | 47% | $813,228 | $-60k | 51/24 |
| 6mo/ITM5/d85/14%/IV80 | +25.1% | -7.0% | 48% | $812,904 | $-60k | 59/16 |
| 6mo/ATM/d80/14%/IV80 | +25.0% | -7.3% | 45% | $812,191 | $-61k | 55/20 |
| 2mo/OTM5/d80/7%/IV80 | +24.9% | -13.1% | 36% | $812,133 | $-61k | 47/28 |
| 6mo/ATM/d85/14%/IV80 | +24.9% | -8.2% | 47% | $811,782 | $-61k | 54/21 |
| 3mo/ATM/d80/14%/IV80 | +24.7% | -21.2% | 39% | $810,693 | $-62k | 53/22 |
| 3mo/ITM5/d85/7%/IV80 | +24.4% | -9.5% | 41% | $808,818 | $-64k | 56/19 |
| 4mo/ATM/d90/7%/IV80 | +24.2% | -6.5% | 47% | $807,123 | $-66k | 51/24 |
| 4mo/ITM5/d80/14%/IV80 | +24.1% | -17.9% | 41% | $806,750 | $-66k | 56/19 |
| 4mo/ITM5/d85/7%/IV80 | +23.6% | -6.6% | 44% | $803,233 | $-70k | 53/22 |
| 3mo/ATM/d85/7%/IV80 | +23.0% | -6.6% | 40% | $799,389 | $-73k | 51/24 |
| 9mo/ATM/d90/7%/IV60 | +22.8% | -4.1% | 49% | $798,055 | $-75k | 51/24 |
| 3mo/ATM/d90/7%/IV80 | +22.7% | -6.6% | 40% | $797,852 | $-75k | 50/25 |
| 4mo/ITM5/d90/7%/IV80 | +22.7% | -7.6% | 44% | $797,400 | $-75k | 53/22 |
| 6mo/ITM5/d90/7%/IV80 | +22.2% | -4.8% | 52% | $794,559 | $-78k | 59/16 |
| 4mo/ATM/d85/7%/IV80 | +21.9% | -6.5% | 44% | $792,154 | $-81k | 51/24 |
| 4mo/ITM5/d80/7%/IV60 | +21.8% | -10.4% | 41% | $791,730 | $-81k | 57/18 |
| 3mo/ITM5/d80/7%/IV80 | +21.5% | -9.8% | 39% | $789,972 | $-83k | 58/17 |
| 3mo/OTM5/d90/7%/IV80 | +21.4% | -6.7% | 39% | $789,338 | $-84k | 45/30 |
| 4mo/OTM5/d90/7%/IV80 | +21.4% | -6.8% | 43% | $789,175 | $-84k | 49/26 |
| 3mo/OTM5/d85/7%/IV80 | +21.4% | -6.7% | 39% | $788,905 | $-84k | 45/30 |
| 9mo/OTM5/d85/7%/IV60 | +21.3% | -4.4% | 51% | $788,701 | $-84k | 49/26 |
| 4mo/ATM/d80/14%/IV80 | +21.3% | -20.0% | 43% | $788,686 | $-84k | 54/21 |
| 9mo/ITM5/d80/7%/IV60 | +21.2% | -5.9% | 48% | $787,895 | $-85k | 60/15 |
| 6mo/ITM5/d80/14%/IV80 | +21.0% | -12.1% | 47% | $786,353 | $-87k | 61/14 |
| 3mo/OTM5/d80/7%/IV80 | +20.9% | -6.8% | 39% | $785,766 | $-87k | 46/29 |
| 9mo/ITM5/d85/7%/IV60 | +20.9% | -6.1% | 49% | $785,643 | $-87k | 54/21 |
| 9mo/ATM/d85/7%/IV60 | +20.5% | -4.2% | 49% | $782,972 | $-90k | 52/23 |
| 6mo/OTM5/d80/14%/IV80 | +20.2% | -9.3% | 45% | $781,255 | $-92k | 51/24 |
| 9mo/OTM5/d90/7%/IV60 | +19.7% | -5.8% | 45% | $778,105 | $-95k | 49/26 |
| 4mo/OTM5/d85/7%/IV80 | +19.5% | -6.9% | 41% | $777,036 | $-96k | 49/26 |
| 4mo/ITM5/d80/7%/IV80 | +19.0% | -10.7% | 41% | $773,245 | $-100k | 56/19 |
| 6mo/ATM/d90/7%/IV80 | +18.9% | -5.4% | 47% | $772,791 | $-100k | 54/21 |
| 9mo/ATM/d80/7%/IV60 | +18.8% | -4.9% | 47% | $772,475 | $-100k | 53/22 |
| 3mo/ATM/d80/7%/IV80 | +18.5% | -10.6% | 39% | $770,489 | $-102k | 53/22 |
| 9mo/OTM5/d80/7%/IV60 | +18.5% | -4.5% | 47% | $770,201 | $-103k | 51/24 |
| 4mo/OTM5/d80/7%/IV80 | +18.2% | -6.9% | 40% | $768,454 | $-104k | 49/26 |
| 4mo/ATM/d80/7%/IV80 | +18.1% | -10.2% | 43% | $767,342 | $-106k | 54/21 |
| 6mo/ITM5/d85/7%/IV80 | +17.8% | -5.0% | 48% | $765,702 | $-107k | 59/16 |
| 6mo/OTM5/d85/7%/IV80 | +17.3% | -5.6% | 48% | $762,771 | $-110k | 51/24 |
| 9mo/OTM5/d90/14%/IV80 | +17.2% | -9.4% | 43% | $761,795 | $-111k | 49/26 |
| 6mo/OTM5/d90/7%/IV80 | +17.1% | -5.6% | 47% | $761,221 | $-112k | 51/24 |
| 6mo/ATM/d80/7%/IV80 | +17.1% | -4.9% | 45% | $761,182 | $-112k | 55/20 |
| 6mo/ATM/d85/7%/IV80 | +16.6% | -5.5% | 47% | $758,210 | $-115k | 54/21 |
| 6mo/ITM5/d80/7%/IV80 | +16.3% | -7.5% | 47% | $756,043 | $-117k | 61/14 |
| 2mo/ITM5/d80/7%/IV80 | +15.2% | -10.4% | 37% | $749,116 | $-124k | 56/19 |
| 6mo/OTM5/d80/7%/IV80 | +13.7% | -6.0% | 45% | $738,938 | $-134k | 51/24 |
| 9mo/OTM5/d90/7%/IV80 | +12.2% | -8.3% | 43% | $729,056 | $-144k | 49/26 |
| 9mo/ITM5/d85/14%/IV80 | +10.3% | -9.5% | 48% | $716,906 | $-156k | 53/22 |
| 9mo/ATM/d85/14%/IV80 | +10.3% | -7.3% | 47% | $716,635 | $-156k | 51/24 |
| 9mo/ITM5/d90/14%/IV80 | +8.6% | -9.4% | 44% | $705,841 | $-167k | 53/22 |
| 9mo/ATM/d85/7%/IV80 | +8.4% | -6.2% | 47% | $704,683 | $-168k | 51/24 |
| 9mo/ITM5/d85/7%/IV80 | +8.1% | -8.7% | 48% | $702,781 | $-170k | 53/22 |
| 9mo/ITM5/d90/7%/IV80 | +6.0% | -9.2% | 44% | $688,959 | $-184k | 53/22 |
| 9mo/ITM5/d80/7%/IV80 | +5.6% | -8.4% | 45% | $686,670 | $-186k | 58/17 |
| 9mo/OTM5/d85/14%/IV80 | +5.2% | -10.7% | 41% | $683,949 | $-189k | 49/26 |
| 9mo/ATM/d90/14%/IV80 | +5.2% | -10.8% | 41% | $683,876 | $-189k | 51/24 |
| 9mo/ATM/d80/7%/IV80 | +4.2% | -8.3% | 40% | $677,378 | $-196k | 52/23 |
| 9mo/ATM/d80/14%/IV80 | +4.1% | -11.5% | 40% | $676,944 | $-196k | 52/23 |
| 9mo/ITM5/d80/14%/IV80 | +3.8% | -11.9% | 45% | $674,553 | $-198k | 58/17 |
| 9mo/OTM5/d85/7%/IV80 | +3.7% | -9.2% | 41% | $673,980 | $-199k | 49/26 |
| 9mo/ATM/d90/7%/IV80 | +3.1% | -9.7% | 41% | $670,257 | $-203k | 51/24 |
| 9mo/OTM5/d80/7%/IV80 | +2.5% | -9.9% | 37% | $666,066 | $-207k | 50/25 |
| 9mo/OTM5/d80/14%/IV80 | +2.4% | -12.8% | 37% | $665,537 | $-207k | 50/25 |

- **Best cell:** `2mo/OTM5/d90/14%/IV40` at +220.4% ($+1,210k vs stock). **Worst cell:** `9mo/OTM5/d80/14%/IV80` at +2.4% ($-207k vs stock). The grid is EXPLORATORY sensitivity, not a recommendation — do not read the best cell as a chosen strategy.

## DECOMPOSITION — the two competing forces, in dollars (base cell)

**(a) SHAKEOUT-SURVIVAL WINS** — the -7% stock stop ejected the name, but the option survived the dip, converted, and finished a WINNER:

| Name | Buy | Stock E3 ret | Stock $ | Option $ | Gain to option $ | kind |
|---|---|---:|---:|---:|---:|---|
| SMCI | 2023-12-19 | -7.0% | $-4k | $+53k | $+57k | delta-convert-run |
| STRL | 2026-02-10 | -7.9% | $-5k | $+30k | $+35k | delta-convert-run |
| PLTR | 2024-07-05 | -11.5% | $-6k | $+23k | $+29k | delta-convert-run |
| GEV | 2025-01-07 | -10.5% | $-8k | $+14k | $+22k | delta-convert-run |
| IBIT | 2024-06-03 | -7.1% | $-6k | $+7k | $+13k | delta-convert-run |
| RKLB | 2025-06-09 | -7.0% | $-3k | $+10k | $+13k | delta-convert-run |
| TOST | 2024-05-09 | -11.9% | $-7k | $+3k | $+10k | delta-convert-run |
| FIX | 2024-08-27 | -8.4% | $-6k | $+4k | $+10k | delta-convert-run |
| APH | 2026-01-12 | -10.4% | $-6k | $+1k | $+7k | delta-convert-run |
| AXON | 2023-11-08 | -7.0% | $-4k | $+2k | $+7k | delta-convert-run |
| PLTR | 2025-04-15 | -7.0% | $-2k | $+4k | $+6k | delta-convert-run |
| PLTR | 2025-05-05 | -8.9% | $-5k | $+1k | $+5k | delta-convert-run |
| VRT | 2025-08-28 | -7.8% | $-4k | $+0k | $+4k | delta-convert-run |

**(b) THETA/STALL LOSSES** — the stock went flat/small (never stopped), the option bled to worthless (lost the premium):

| Name | Buy | Stock E3 ret | Stock $ | Option $ | Loss to option $ | kind |
|---|---|---:|---:|---:|---:|---|
| CLS | 2026-04-13 | -0.7% | $0k | $-6k | $+6k | expired-worthless |
| ANET | 2025-09-09 | -1.0% | $-1k | $-3k | $+2k | expired-worthless |
| DXCM | 2023-06-29 | -4.6% | $-2k | $-2k | $+1k | expired-worthless |

**NET (a) - (b) = $+210k**  (shakeout-survival wins $+218k minus theta/stall losses $+8k).

_Honesty note (not part of the named net): on WINNERS the option gave up **$+293k** to notional-cap under-participation (it owned fewer shares than the stock, so it captured less of the run — the dominant drag), and on names where BOTH lost, the option 'saved' $-101k purely by betting less (loss-mitigation, not a real edge)._

## NOTIONAL — what does the 7% (vs 14%) premium actually buy? (IV 60%, 6mo, ATM)

| Name | Strike | Stock cost $ | ATM prem/sh | #contracts | Notional $ | Notional / stock cost |
|---|---:|---:|---:|---:|---:|---:|
| MSTR | $208.1 | $+17k | $36.94 | 1 | $+21k | 1.2x |
| SNPS | $442.2 | $+51k | $78.48 | 1 | $+44k | 0.86x |
| MCK | $413.2 | $+51k | $73.34 | 1 | $+41k | 0.81x |
| MDB | $450.3 | $+57k | $79.92 | 1 | $+45k | 0.79x |
| TSLA | $478.8 | $+62k | $84.99 | 1 | $+48k | 0.77x |
| STRL | $434.9 | $+60k | $77.18 | 1 | $+43k | 0.73x |
| APP | $462.8 | $+69k | $82.15 | 1 | $+46k | 0.67x |
| CLS | $362.3 | $+55k | $64.31 | 1 | $+36k | 0.66x |
| SMCI | $320.4 | $+58k | $56.86 | 1 | $+32k | 0.56x |
| GEV | $368.6 | $+74k | $65.41 | 1 | $+37k | 0.5x |
| FIX | $343.1 | $+71k | $60.89 | 1 | $+34k | 0.48x |
| ARM | $131.1 | $+29k | $23.26 | 1 | $+13k | 0.45x |
| DUOL | $173.9 | $+42k | $30.86 | 1 | $+17k | 0.42x |
| DKS | $242.3 | $+61k | $43.0 | 1 | $+24k | 0.4x |
| PLTR | $19.9 | $+41k | $3.53 | 8 | $+16k | 0.39x |
| RKLB | $32.0 | $+49k | $5.68 | 6 | $+19k | 0.39x |
| SYM | $71.7 | $+56k | $12.72 | 3 | $+22k | 0.39x |
| IREN | $65.8 | $+50k | $11.68 | 3 | $+20k | 0.39x |
| NVDA | $94.2 | $+25k | $16.72 | 1 | $+9k | 0.38x |
| IBIT | $40.1 | $+85k | $7.12 | 8 | $+32k | 0.38x |

_Avg notional/stock-cost: **0.41x** at 7% vs **0.7x** at 14% budget (IV 60%). A 7% budget on these high-IV names controls WELL UNDER a full stock position's notional, so even a converted winner under-owns the upside._

## VERDICT (this liquid subset, modeled prices)

- **Mixed — the verdict FLIPS with IV** (option wins at low IV, loses at high IV). That IV-dependence is itself the finding: not robust to the one input we can't observe.
- **Why:** the delta trigger keeps the call in INSURANCE mode through shakeouts (it converts only once delta >= trigger), so it DOES buy some shakeout survival (decomposition (a) = $+218k). But two costs dominate: (i) THETA/STALL — names that chopped sideways bled the premium to zero ($+8k), and (ii) NOTIONAL CAP — a 7% budget buys only ~0.41x the stock notional on these high-IV names, so even the CONVERTED winners own fewer shares and under-participate (gave up $+293k on winners). Net of the two named forces: $+210k.
- **Regime read:** the option's relative case is least-bad in CHOPPY stretches (shakeouts frequent -> survival edge earns its keep) and worst in a clean BULL (2024/2025), where the stock book's full notional simply compounds and the option's capped delta + theta drag lose the race. See the per-year table.

### Hard limits (curve-fit + honesty guards, rule #1)
- **Modeled BS prices + delta, not real fills.** No bid/ask spread, no vol surface/skew, one flat IV per run, and NO per-trade earnings IV bump/crush (per-trade earnings dates are not in the ledger — disclosed, not faked). Real spreads/skew would make the option book WORSE, so this is a friendly upper bound. Validate on real historical quotes before trusting.
- **Liquid-option subset only** (55 names) — EXCLUDES the thin small-caps this system often trades, where the 'can't be shaken out' pitch is most appealing but real option liquidity is worst. The answer does NOT transfer to those names.
- **Full grid + IV sweep reported** so nothing is cherry-picked; the grid is exploratory sensitivity, not a chosen cell.
- **Small sample, bull-heavy 2023-2026 window** — cannot test a bear regime; the let-winners-run edge flatters BOTH books in a bull.
- **Exercise/delivery capital assumption:** conversion deploys the full strike dollars (a real cash draw funded from the same start capital; modeled as a cash round-trip).
