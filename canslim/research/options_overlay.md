# CAN SLIM options-overlay HYBRID — cheap-ATM-call insurance -> delta-triggered delivery -> core exit

_One question: on his LIQUID-OPTION names, does holding a cheap ~ATM call as INSURANCE (premium == the -7% stop loss), converting to stock only when the modeled call DELTA crosses a threshold (it has become a stock proxy), then managing the delivered stock with the proven winning exit (E3), beat just owning the stock from the pivot? Calls are **MODELED with Black-Scholes** (price + delta), theta decay on-path, across an **IV sweep 40/60/80%**. Full spec: research/options_overlay_spec.md._

- Start capital **$650,000** (same as the stock engine).
- **Liquid-option universe: 55 names, 75 entries** (of his 96 names / 118 trades). Chosen as a KNOWN-OPTIONABLE whitelist (large/liquid growth leaders, well-known ADRs, liquid ETFs) that plausibly had actively-quoted listed options 2019-2026. Thin small-caps / low-price names are EXCLUDED — a modeled BS price would misrepresent their real fills.
- **IN (liquid):** AAPL, AEM, AIT, ANET, APH, APP, ARM, AXON, BIRK, CCJ, CLS, CRDO, CROX, DDOG, DKS, DUOL, DXCM, ELF, FIX, FTNT, GEV, HIMS, HOOD, IBIT, IBKR, IONQ, IR, IREN, MCK, MDB, MOD, MSTR, NBIS, NVDA, OKLO, ONON, PLTR, RBLX, RKLB, SCCO, SMCI, SMR, SNPS, SQ, STRL, SYM, TOST, TSLA, TSM, UBER, UHS, URBN, VKTX, VRT, ZS
- **OUT (excluded):** AAON, ACMR, ADMA, AGX, ALMU, AMSC, APLD, APPF, AXGN, BLBD, BPMC, ERJ, HLI, HMY, HXL, IOT, KD, KRMN, LOAR, MNSO, MTRX, MTSI, NTGR, PRCT, PSTG, Q, QUBT, RKT, ROAD, SEI, SQM, TGTX, TILE, TS, TSSI, UFO, UFPT, VIAV, WAY, WLDN, YELP

## Head-to-head — STOCK book vs OPTION book (base cell: 6mo / ATM / delta 0.85 / 7% / IV 60%)

| Book | Total ret | Max DD | Win% | Final equity | #converted | #worthless |
|---|---:|---:|---:|---:|---:|---:|
| STOCK (buy pivot, E3 exit) | +46.2% | -6.5% | 36% | $950,166 | — | — |
| OPTION (base cell) | +34.4% | -3.2% | 52% | $873,591 | 55/75 | 20 |

## Per-year (bucketed by EXIT date) — bull 2024/2025 vs choppy stretches

| Book | 2023H2 | 2024 | 2025 | 2026H1 | total |
|---|---:|---:|---:|---:|---:|
| STOCK E3 | $-30k | $+99k | $+172k | $+59k | $+300k |
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
| 40% | +58.3% | -2.8% | 55% | $1,028,686 | $+79k |
| 60% | +34.4% | -3.2% | 52% | $873,591 | $-77k |
| 80% | +16.6% | -5.5% | 47% | $758,210 | $-192k |

## FULL TEST GRID (exploratory, NOT tuned — every cell reported; ranked by total ret)

_tag = tenor / strike / delta-trig / budget / IV. The stock book is +46.2% total / -6.5% maxDD for reference._

| Cell | Total ret | Max DD | Win% | Final $ | vs STOCK ($) | conv/worthless |
|---|---:|---:|---:|---:|---:|---:|
| 2mo/OTM5/d90/14%/IV40 | +261.6% | -6.1% | 43% | $2,350,493 | $+1,400k | 48/27 |
| 2mo/OTM5/d85/14%/IV40 | +261.4% | -6.1% | 43% | $2,349,022 | $+1,399k | 48/27 |
| 2mo/OTM5/d80/14%/IV40 | +230.9% | -10.1% | 41% | $2,150,956 | $+1,201k | 49/26 |
| 3mo/OTM5/d90/14%/IV40 | +183.9% | -8.6% | 47% | $1,845,236 | $+895k | 48/27 |
| 3mo/OTM5/d85/14%/IV40 | +180.2% | -8.6% | 47% | $1,821,547 | $+871k | 49/26 |
| 3mo/OTM5/d80/14%/IV40 | +178.2% | -8.8% | 47% | $1,808,536 | $+858k | 50/25 |
| 2mo/ATM/d90/14%/IV40 | +163.6% | -7.6% | 47% | $1,713,495 | $+763k | 52/23 |
| 4mo/OTM5/d85/14%/IV40 | +159.7% | -9.3% | 48% | $1,688,141 | $+738k | 52/23 |
| 4mo/OTM5/d90/14%/IV40 | +157.5% | -10.5% | 47% | $1,674,050 | $+724k | 50/25 |
| 4mo/OTM5/d80/14%/IV40 | +153.3% | -11.4% | 47% | $1,646,763 | $+697k | 53/22 |
| 2mo/ATM/d85/14%/IV40 | +147.9% | -8.1% | 45% | $1,611,654 | $+661k | 53/22 |
| 2mo/ATM/d80/14%/IV40 | +146.9% | -8.2% | 45% | $1,605,080 | $+655k | 54/21 |
| 3mo/ATM/d90/14%/IV40 | +137.8% | -7.9% | 48% | $1,545,579 | $+595k | 54/21 |
| 2mo/OTM5/d90/7%/IV40 | +125.7% | -4.1% | 43% | $1,467,295 | $+517k | 48/27 |
| 2mo/OTM5/d85/7%/IV40 | +125.6% | -4.1% | 43% | $1,466,641 | $+516k | 48/27 |
| 2mo/OTM5/d85/14%/IV60 | +122.0% | -9.3% | 41% | $1,442,754 | $+493k | 47/28 |
| 4mo/ATM/d90/14%/IV40 | +122.0% | -9.4% | 49% | $1,442,713 | $+493k | 54/21 |
| 2mo/OTM5/d90/14%/IV60 | +120.9% | -11.3% | 39% | $1,435,957 | $+486k | 44/31 |
| 2mo/OTM5/d80/14%/IV60 | +120.9% | -9.3% | 41% | $1,435,906 | $+486k | 48/27 |
| 6mo/OTM5/d80/14%/IV40 | +120.4% | -4.7% | 51% | $1,432,585 | $+482k | 56/19 |
| 4mo/ATM/d85/14%/IV40 | +120.4% | -9.4% | 49% | $1,432,362 | $+482k | 55/20 |
| 6mo/OTM5/d85/14%/IV40 | +119.6% | -4.8% | 52% | $1,427,170 | $+477k | 54/21 |
| 3mo/ATM/d85/14%/IV40 | +118.6% | -11.2% | 45% | $1,420,927 | $+471k | 57/18 |
| 6mo/OTM5/d90/14%/IV40 | +117.4% | -5.5% | 51% | $1,413,044 | $+463k | 52/23 |
| 3mo/ATM/d80/14%/IV40 | +115.5% | -10.6% | 45% | $1,400,903 | $+451k | 59/16 |
| 6mo/ATM/d85/14%/IV40 | +112.4% | -4.7% | 55% | $1,380,346 | $+430k | 59/16 |
| 6mo/ATM/d90/14%/IV40 | +111.6% | -4.5% | 55% | $1,375,484 | $+425k | 56/19 |
| 2mo/OTM5/d80/7%/IV40 | +110.8% | -6.8% | 41% | $1,370,456 | $+420k | 49/26 |
| 2mo/ITM5/d85/14%/IV40 | +107.9% | -7.5% | 49% | $1,351,477 | $+401k | 58/17 |
| 2mo/ITM5/d90/14%/IV40 | +107.9% | -7.5% | 49% | $1,351,477 | $+401k | 58/17 |
| 4mo/ITM5/d90/14%/IV40 | +107.3% | -7.7% | 52% | $1,347,657 | $+397k | 56/19 |
| 3mo/ITM5/d90/14%/IV40 | +96.9% | -8.4% | 51% | $1,280,147 | $+330k | 59/16 |
| 2mo/ATM/d85/14%/IV60 | +96.5% | -7.7% | 43% | $1,276,975 | $+327k | 51/24 |
| 2mo/ATM/d90/14%/IV60 | +96.5% | -7.7% | 43% | $1,276,975 | $+327k | 51/24 |
| 6mo/ATM/d80/14%/IV40 | +96.1% | -7.0% | 49% | $1,274,752 | $+325k | 60/15 |
| 6mo/ITM5/d90/14%/IV40 | +91.6% | -4.0% | 56% | $1,245,454 | $+295k | 61/14 |
| 4mo/ATM/d80/14%/IV40 | +91.3% | -12.2% | 44% | $1,243,234 | $+293k | 56/19 |
| 2mo/ITM5/d80/14%/IV40 | +90.6% | -8.0% | 48% | $1,238,978 | $+289k | 62/13 |
| 3mo/ITM5/d85/14%/IV40 | +87.9% | -8.4% | 48% | $1,221,073 | $+271k | 61/14 |
| 3mo/ITM5/d80/14%/IV40 | +86.4% | -9.1% | 47% | $1,211,811 | $+262k | 64/11 |
| 2mo/ATM/d80/14%/IV60 | +84.8% | -10.3% | 41% | $1,201,327 | $+251k | 52/23 |
| 2mo/ITM5/d90/14%/IV60 | +84.4% | -7.3% | 45% | $1,198,793 | $+249k | 55/20 |
| 3mo/OTM5/d90/7%/IV40 | +84.3% | -5.5% | 47% | $1,197,741 | $+248k | 48/27 |
| 6mo/ITM5/d85/14%/IV40 | +83.7% | -5.8% | 53% | $1,194,007 | $+244k | 62/13 |
| 3mo/OTM5/d85/7%/IV40 | +82.6% | -5.5% | 47% | $1,186,593 | $+236k | 49/26 |
| 3mo/OTM5/d80/7%/IV40 | +81.7% | -5.6% | 47% | $1,180,931 | $+231k | 50/25 |
| 4mo/ITM5/d85/14%/IV40 | +81.5% | -9.5% | 47% | $1,179,673 | $+230k | 59/16 |
| 9mo/OTM5/d90/14%/IV40 | +79.9% | -3.8% | 56% | $1,169,673 | $+220k | 51/24 |
| 3mo/OTM5/d90/14%/IV60 | +78.5% | -11.5% | 41% | $1,160,337 | $+210k | 45/30 |
| 3mo/OTM5/d80/14%/IV60 | +77.8% | -11.6% | 41% | $1,155,711 | $+206k | 48/27 |
| 3mo/OTM5/d85/14%/IV60 | +77.2% | -11.9% | 41% | $1,151,948 | $+202k | 46/29 |
| 3mo/ATM/d80/14%/IV60 | +76.5% | -10.9% | 43% | $1,147,169 | $+197k | 55/20 |
| 2mo/ATM/d90/7%/IV40 | +76.0% | -4.7% | 47% | $1,144,152 | $+194k | 52/23 |
| 9mo/OTM5/d80/14%/IV40 | +75.1% | -4.8% | 52% | $1,138,185 | $+188k | 57/18 |
| 4mo/OTM5/d85/7%/IV40 | +74.8% | -5.7% | 48% | $1,136,139 | $+186k | 52/23 |
| 4mo/OTM5/d90/14%/IV60 | +74.5% | -11.7% | 45% | $1,134,143 | $+184k | 49/26 |
| 4mo/ITM5/d80/14%/IV40 | +73.9% | -9.5% | 44% | $1,130,152 | $+180k | 62/13 |
| 3mo/ATM/d85/14%/IV60 | +73.7% | -10.5% | 43% | $1,128,768 | $+179k | 53/22 |
| 4mo/OTM5/d90/7%/IV40 | +73.5% | -6.5% | 47% | $1,127,904 | $+178k | 50/25 |
| 9mo/ATM/d90/14%/IV40 | +73.5% | -4.0% | 56% | $1,127,652 | $+177k | 52/23 |
| 9mo/OTM5/d85/14%/IV40 | +72.5% | -3.8% | 51% | $1,121,379 | $+171k | 51/24 |
| 3mo/ATM/d90/14%/IV60 | +72.1% | -11.6% | 41% | $1,118,884 | $+169k | 51/24 |
| 4mo/OTM5/d80/7%/IV40 | +71.6% | -7.0% | 47% | $1,115,450 | $+165k | 53/22 |
| 4mo/OTM5/d80/14%/IV60 | +71.3% | -11.4% | 44% | $1,113,629 | $+163k | 52/23 |
| 9mo/ATM/d85/14%/IV40 | +70.5% | -4.4% | 52% | $1,107,940 | $+158k | 56/19 |
| 9mo/ITM5/d85/14%/IV40 | +70.4% | -3.8% | 53% | $1,107,315 | $+157k | 60/15 |
| 4mo/OTM5/d85/14%/IV60 | +70.3% | -11.7% | 44% | $1,106,937 | $+157k | 49/26 |
| 9mo/ATM/d80/14%/IV40 | +70.3% | -4.3% | 51% | $1,106,754 | $+157k | 60/15 |
| 3mo/ATM/d90/7%/IV40 | +68.5% | -4.9% | 48% | $1,095,466 | $+145k | 54/21 |
| 2mo/ATM/d85/7%/IV40 | +68.2% | -5.0% | 45% | $1,093,281 | $+143k | 53/22 |
| 6mo/ITM5/d80/14%/IV40 | +67.7% | -7.9% | 48% | $1,090,356 | $+140k | 63/12 |
| 2mo/ATM/d80/7%/IV40 | +67.7% | -5.1% | 45% | $1,089,994 | $+140k | 54/21 |
| 2mo/ITM5/d85/14%/IV60 | +67.7% | -9.2% | 43% | $1,089,778 | $+140k | 56/19 |
| 3mo/ITM5/d90/14%/IV60 | +66.6% | -9.4% | 48% | $1,082,779 | $+133k | 56/19 |
| 9mo/ITM5/d90/14%/IV40 | +66.5% | -4.1% | 52% | $1,082,238 | $+132k | 55/20 |
| 2mo/ITM5/d80/14%/IV60 | +66.5% | -9.6% | 43% | $1,082,039 | $+132k | 58/17 |
| 4mo/ATM/d90/7%/IV40 | +62.3% | -5.5% | 49% | $1,054,779 | $+105k | 54/21 |
| 6mo/OTM5/d90/14%/IV60 | +61.7% | -8.0% | 53% | $1,051,298 | $+101k | 51/24 |
| 4mo/ATM/d85/7%/IV40 | +61.5% | -5.5% | 49% | $1,049,604 | $+99k | 55/20 |
| 2mo/OTM5/d80/14%/IV80 | +61.1% | -11.6% | 37% | $1,046,905 | $+97k | 47/28 |
| 2mo/OTM5/d90/14%/IV80 | +60.9% | -13.2% | 36% | $1,045,569 | $+95k | 44/31 |
| 6mo/OTM5/d85/7%/IV40 | +60.8% | -2.5% | 52% | $1,045,085 | $+95k | 54/21 |
| 2mo/OTM5/d85/14%/IV80 | +60.2% | -13.2% | 36% | $1,041,382 | $+91k | 44/31 |
| 6mo/OTM5/d80/7%/IV40 | +60.2% | -2.9% | 51% | $1,041,122 | $+91k | 56/19 |
| 6mo/OTM5/d90/7%/IV40 | +59.9% | -2.7% | 51% | $1,039,547 | $+89k | 52/23 |
| 3mo/ATM/d85/7%/IV40 | +59.4% | -6.5% | 45% | $1,036,034 | $+86k | 57/18 |
| 4mo/ATM/d90/14%/IV60 | +58.6% | -10.8% | 45% | $1,031,139 | $+81k | 51/24 |
| 2mo/OTM5/d85/7%/IV60 | +58.4% | -5.5% | 41% | $1,029,450 | $+79k | 47/28 |
| 6mo/ATM/d90/7%/IV40 | +58.4% | -2.5% | 55% | $1,029,413 | $+79k | 56/19 |
| 6mo/ATM/d85/7%/IV40 | +58.3% | -2.8% | 55% | $1,028,686 | $+79k | 59/16 |
| 4mo/ATM/d85/14%/IV60 | +58.3% | -11.4% | 44% | $1,028,644 | $+78k | 52/23 |
| 3mo/ATM/d80/7%/IV40 | +57.9% | -6.1% | 45% | $1,026,627 | $+76k | 59/16 |
| 2mo/OTM5/d80/7%/IV60 | +57.9% | -5.5% | 41% | $1,026,026 | $+76k | 48/27 |
| 2mo/OTM5/d90/7%/IV60 | +57.8% | -6.6% | 39% | $1,025,757 | $+76k | 44/31 |
| 3mo/ITM5/d85/14%/IV60 | +57.7% | -9.8% | 45% | $1,024,812 | $+75k | 58/17 |
| 4mo/ATM/d80/14%/IV60 | +56.7% | -12.4% | 44% | $1,018,361 | $+68k | 55/20 |
| 4mo/ITM5/d90/7%/IV40 | +56.2% | -4.6% | 52% | $1,015,163 | $+65k | 56/19 |
| 4mo/ITM5/d90/14%/IV60 | +55.1% | -11.1% | 48% | $1,008,183 | $+58k | 53/22 |
| 4mo/ITM5/d85/14%/IV60 | +54.1% | -11.4% | 48% | $1,001,735 | $+52k | 56/19 |
| 6mo/ATM/d90/14%/IV60 | +53.1% | -7.7% | 52% | $994,979 | $+45k | 54/21 |
| 6mo/ITM5/d90/7%/IV40 | +53.0% | -2.8% | 56% | $994,822 | $+45k | 61/14 |
| 3mo/ITM5/d90/7%/IV40 | +52.7% | -5.2% | 51% | $992,467 | $+42k | 59/16 |
| 2mo/ITM5/d85/7%/IV40 | +52.3% | -4.3% | 49% | $989,904 | $+40k | 58/17 |
| 2mo/ITM5/d90/7%/IV40 | +52.3% | -4.3% | 49% | $989,904 | $+40k | 58/17 |
| 6mo/ATM/d85/14%/IV60 | +51.7% | -6.3% | 52% | $986,191 | $+36k | 55/20 |
| 6mo/OTM5/d85/14%/IV60 | +51.5% | -7.9% | 49% | $984,897 | $+35k | 51/24 |
| 6mo/ATM/d80/7%/IV40 | +51.4% | -3.9% | 49% | $984,108 | $+34k | 60/15 |
| 6mo/ITM5/d90/14%/IV60 | +50.6% | -5.4% | 53% | $978,871 | $+29k | 59/16 |
| 6mo/ATM/d80/14%/IV60 | +50.6% | -7.0% | 49% | $978,733 | $+29k | 59/16 |
| 9mo/ITM5/d80/14%/IV40 | +50.4% | -7.2% | 45% | $977,736 | $+28k | 62/13 |
| 6mo/ITM5/d85/7%/IV40 | +50.3% | -3.3% | 53% | $976,825 | $+27k | 62/13 |
| 3mo/ITM5/d80/14%/IV60 | +50.3% | -11.4% | 44% | $976,795 | $+27k | 60/15 |
| 2mo/ITM5/d85/14%/IV80 | +49.7% | -9.1% | 41% | $972,926 | $+23k | 55/20 |
| 2mo/ITM5/d90/14%/IV80 | +49.7% | -9.1% | 41% | $972,926 | $+23k | 55/20 |
| 2mo/ATM/d80/14%/IV80 | +49.6% | -10.4% | 40% | $972,318 | $+22k | 51/24 |
| 2mo/ATM/d85/14%/IV80 | +49.6% | -10.4% | 40% | $972,318 | $+22k | 51/24 |
| 2mo/ATM/d90/14%/IV80 | +49.4% | -11.7% | 39% | $971,252 | $+21k | 47/28 |
| 6mo/ITM5/d85/14%/IV60 | +48.8% | -5.6% | 51% | $966,976 | $+17k | 59/16 |
| 6mo/OTM5/d80/14%/IV60 | +48.4% | -7.5% | 48% | $964,716 | $+15k | 53/22 |
| 2mo/ATM/d85/7%/IV60 | +48.4% | -4.2% | 43% | $964,469 | $+14k | 51/24 |
| 2mo/ATM/d90/7%/IV60 | +48.4% | -4.2% | 43% | $964,469 | $+14k | 51/24 |
| 4mo/ATM/d80/7%/IV40 | +48.3% | -7.1% | 44% | $963,726 | $+14k | 56/19 |
| 6mo/ITM5/d80/14%/IV60 | +47.7% | -7.0% | 49% | $959,808 | $+10k | 62/13 |
| 9mo/OTM5/d90/7%/IV40 | +46.3% | -2.8% | 56% | $950,829 | $+1k | 51/24 |
| 2mo/ITM5/d90/7%/IV60 | +45.7% | -3.8% | 45% | $947,055 | $-3k | 55/20 |
| 4mo/ITM5/d85/7%/IV40 | +45.5% | -5.6% | 47% | $945,980 | $-4k | 59/16 |
| 9mo/ITM5/d85/7%/IV40 | +45.3% | -3.5% | 53% | $944,695 | $-5k | 60/15 |
| 3mo/ITM5/d85/7%/IV40 | +45.2% | -4.8% | 48% | $943,634 | $-7k | 61/14 |
| 3mo/ITM5/d80/7%/IV40 | +44.2% | -5.2% | 47% | $937,259 | $-13k | 64/11 |
| 2mo/ATM/d80/7%/IV60 | +43.8% | -5.6% | 41% | $934,511 | $-16k | 52/23 |
| 2mo/ITM5/d80/7%/IV40 | +43.7% | -4.6% | 48% | $933,756 | $-16k | 62/13 |
| 9mo/ITM5/d90/7%/IV40 | +43.5% | -3.3% | 52% | $932,927 | $-17k | 55/20 |
| 9mo/OTM5/d80/7%/IV40 | +43.0% | -3.9% | 52% | $929,822 | $-20k | 57/18 |
| 9mo/ATM/d90/7%/IV40 | +43.0% | -2.7% | 56% | $929,290 | $-21k | 52/23 |
| 9mo/OTM5/d85/7%/IV40 | +42.1% | -2.8% | 51% | $923,575 | $-27k | 51/24 |
| 9mo/ATM/d80/7%/IV40 | +41.6% | -3.6% | 51% | $920,080 | $-30k | 60/15 |
| 9mo/ATM/d85/7%/IV40 | +41.3% | -3.4% | 52% | $918,493 | $-32k | 56/19 |
| 3mo/ITM5/d85/14%/IV80 | +40.7% | -11.0% | 43% | $914,709 | $-35k | 56/19 |
| 9mo/ITM5/d90/14%/IV60 | +40.6% | -4.7% | 56% | $913,869 | $-36k | 53/22 |
| 6mo/ITM5/d80/7%/IV40 | +40.6% | -3.8% | 48% | $913,675 | $-36k | 63/12 |
| 3mo/OTM5/d90/7%/IV60 | +40.5% | -6.4% | 41% | $913,109 | $-37k | 45/30 |
| 3mo/ITM5/d90/14%/IV80 | +40.0% | -11.5% | 41% | $910,275 | $-40k | 54/21 |
| 3mo/ATM/d80/7%/IV60 | +39.9% | -6.2% | 43% | $909,671 | $-40k | 55/20 |
| 3mo/OTM5/d80/7%/IV60 | +39.9% | -6.5% | 41% | $909,567 | $-41k | 48/27 |
| 3mo/OTM5/d85/7%/IV60 | +39.8% | -6.6% | 41% | $908,755 | $-41k | 46/29 |
| 3mo/ATM/d85/7%/IV60 | +39.5% | -6.0% | 43% | $907,021 | $-43k | 53/22 |
| 3mo/ITM5/d90/7%/IV60 | +39.0% | -5.1% | 48% | $903,529 | $-47k | 56/19 |
| 3mo/ATM/d90/7%/IV60 | +38.8% | -6.6% | 41% | $902,451 | $-48k | 51/24 |
| 4mo/OTM5/d90/7%/IV60 | +38.7% | -6.3% | 45% | $901,524 | $-49k | 49/26 |
| 4mo/ITM5/d80/14%/IV60 | +38.6% | -12.7% | 43% | $901,166 | $-49k | 57/18 |
| 4mo/ITM5/d80/7%/IV40 | +38.4% | -5.1% | 44% | $899,522 | $-51k | 62/13 |
| 9mo/ATM/d90/14%/IV60 | +37.4% | -5.7% | 49% | $893,141 | $-57k | 51/24 |
| 4mo/OTM5/d80/7%/IV60 | +36.8% | -6.2% | 44% | $889,205 | $-61k | 52/23 |
| 4mo/OTM5/d85/7%/IV60 | +36.6% | -6.3% | 44% | $887,922 | $-62k | 49/26 |
| 4mo/ITM5/d90/7%/IV60 | +36.1% | -5.7% | 48% | $884,864 | $-65k | 53/22 |
| 2mo/ITM5/d80/14%/IV80 | +36.1% | -11.1% | 39% | $884,607 | $-66k | 56/19 |
| 3mo/ITM5/d85/7%/IV60 | +35.8% | -5.2% | 45% | $882,476 | $-68k | 58/17 |
| 4mo/ITM5/d85/7%/IV60 | +35.6% | -5.9% | 48% | $881,604 | $-69k | 56/19 |
| 2mo/ITM5/d85/7%/IV60 | +35.4% | -4.9% | 43% | $880,113 | $-70k | 56/19 |
| 6mo/OTM5/d90/7%/IV60 | +35.3% | -4.4% | 53% | $879,176 | $-71k | 51/24 |
| 4mo/ATM/d90/7%/IV60 | +35.1% | -5.9% | 45% | $878,380 | $-72k | 51/24 |
| 4mo/ATM/d85/7%/IV60 | +35.1% | -6.1% | 44% | $878,196 | $-72k | 52/23 |
| 3mo/ATM/d85/14%/IV80 | +35.1% | -12.4% | 40% | $878,126 | $-72k | 51/24 |
| 6mo/ATM/d90/7%/IV60 | +35.1% | -3.5% | 52% | $878,027 | $-72k | 54/21 |
| 3mo/ATM/d80/14%/IV80 | +34.9% | -12.6% | 40% | $876,712 | $-73k | 53/22 |
| 2mo/ITM5/d80/7%/IV60 | +34.8% | -5.0% | 43% | $876,188 | $-74k | 58/17 |
| 3mo/ITM5/d80/14%/IV80 | +34.8% | -11.5% | 40% | $875,998 | $-74k | 58/17 |
| 3mo/ATM/d90/14%/IV80 | +34.7% | -12.1% | 40% | $875,456 | $-75k | 50/25 |
| 4mo/ATM/d80/7%/IV60 | +34.4% | -6.6% | 44% | $873,669 | $-76k | 55/20 |
| 6mo/ATM/d85/7%/IV60 | +34.4% | -3.2% | 52% | $873,591 | $-77k | 55/20 |
| 9mo/ITM5/d80/14%/IV60 | +34.4% | -5.8% | 48% | $873,318 | $-77k | 60/15 |
| 6mo/ITM5/d90/7%/IV60 | +34.0% | -3.7% | 53% | $871,291 | $-79k | 59/16 |
| 4mo/ATM/d90/14%/IV80 | +34.0% | -12.8% | 47% | $871,044 | $-79k | 51/24 |
| 3mo/OTM5/d90/14%/IV80 | +33.7% | -13.2% | 39% | $868,818 | $-81k | 45/30 |
| 3mo/OTM5/d85/14%/IV80 | +33.5% | -13.2% | 39% | $867,953 | $-82k | 45/30 |
| 6mo/ITM5/d85/7%/IV60 | +33.3% | -4.4% | 51% | $866,571 | $-84k | 59/16 |
| 6mo/ATM/d80/7%/IV60 | +33.1% | -4.3% | 49% | $865,090 | $-85k | 59/16 |
| 6mo/ITM5/d80/7%/IV60 | +33.0% | -4.4% | 49% | $864,287 | $-86k | 62/13 |
| 3mo/OTM5/d80/14%/IV80 | +32.8% | -13.5% | 39% | $863,081 | $-87k | 46/29 |
| 9mo/ATM/d85/14%/IV60 | +32.6% | -5.5% | 49% | $861,743 | $-88k | 52/23 |
| 4mo/ITM5/d85/14%/IV80 | +32.5% | -10.7% | 44% | $861,282 | $-89k | 53/22 |
| 2mo/OTM5/d80/7%/IV80 | +32.5% | -6.4% | 37% | $861,235 | $-89k | 47/28 |
| 9mo/ITM5/d80/7%/IV40 | +32.5% | -4.0% | 45% | $861,142 | $-89k | 62/13 |
| 2mo/OTM5/d90/7%/IV80 | +32.5% | -7.1% | 36% | $861,109 | $-89k | 44/31 |
| 2mo/OTM5/d85/7%/IV80 | +32.2% | -7.1% | 36% | $859,155 | $-91k | 44/31 |
| 3mo/ITM5/d80/7%/IV60 | +32.0% | -6.1% | 44% | $858,166 | $-92k | 60/15 |
| 4mo/OTM5/d90/14%/IV80 | +31.7% | -13.3% | 43% | $856,266 | $-94k | 49/26 |
| 4mo/ITM5/d90/14%/IV80 | +31.6% | -10.7% | 44% | $855,449 | $-95k | 53/22 |
| 9mo/OTM5/d85/14%/IV60 | +31.5% | -5.9% | 51% | $854,439 | $-96k | 49/26 |
| 9mo/ITM5/d85/14%/IV60 | +31.3% | -6.1% | 49% | $853,569 | $-97k | 54/21 |
| 2mo/ATM/d80/7%/IV80 | +31.1% | -5.0% | 40% | $852,415 | $-98k | 51/24 |
| 2mo/ATM/d85/7%/IV80 | +31.1% | -5.0% | 40% | $852,415 | $-98k | 51/24 |
| 2mo/ATM/d90/7%/IV80 | +31.1% | -5.6% | 39% | $852,191 | $-98k | 47/28 |
| 6mo/OTM5/d85/7%/IV60 | +31.1% | -4.4% | 49% | $852,048 | $-98k | 51/24 |
| 4mo/ITM5/d80/14%/IV80 | +30.9% | -12.1% | 43% | $850,598 | $-100k | 56/19 |
| 2mo/ITM5/d85/7%/IV80 | +30.8% | -4.5% | 41% | $850,257 | $-100k | 55/20 |
| 2mo/ITM5/d90/7%/IV80 | +30.8% | -4.5% | 41% | $850,257 | $-100k | 55/20 |
| 9mo/OTM5/d90/14%/IV60 | +30.6% | -7.1% | 45% | $848,951 | $-101k | 49/26 |
| 6mo/ITM5/d90/14%/IV80 | +30.2% | -7.0% | 52% | $846,030 | $-104k | 59/16 |
| 4mo/ATM/d80/14%/IV80 | +29.9% | -12.6% | 44% | $844,137 | $-106k | 54/21 |
| 9mo/ATM/d80/14%/IV60 | +29.7% | -5.5% | 47% | $843,359 | $-107k | 53/22 |
| 6mo/ATM/d90/14%/IV80 | +29.7% | -7.8% | 47% | $842,912 | $-107k | 54/21 |
| 6mo/OTM5/d80/7%/IV60 | +29.6% | -4.1% | 48% | $842,097 | $-108k | 53/22 |
| 4mo/ATM/d85/14%/IV80 | +29.3% | -12.8% | 44% | $840,504 | $-110k | 51/24 |
| 3mo/ITM5/d85/7%/IV80 | +28.6% | -5.9% | 43% | $835,732 | $-114k | 56/19 |
| 4mo/OTM5/d85/14%/IV80 | +28.3% | -13.5% | 41% | $833,695 | $-116k | 49/26 |
| 3mo/ITM5/d90/7%/IV80 | +28.1% | -6.3% | 41% | $832,758 | $-117k | 54/21 |
| 6mo/OTM5/d85/14%/IV80 | +27.3% | -9.0% | 48% | $827,771 | $-122k | 51/24 |
| 6mo/OTM5/d90/14%/IV80 | +26.5% | -8.9% | 47% | $822,101 | $-128k | 51/24 |
| 6mo/ITM5/d80/14%/IV80 | +26.4% | -7.2% | 47% | $821,733 | $-128k | 61/14 |
| 4mo/ITM5/d80/7%/IV60 | +25.9% | -6.8% | 43% | $818,643 | $-132k | 57/18 |
| 9mo/ITM5/d90/7%/IV60 | +25.7% | -5.4% | 56% | $817,005 | $-133k | 53/22 |
| 3mo/ITM5/d80/7%/IV80 | +25.7% | -6.1% | 40% | $816,886 | $-133k | 58/17 |
| 4mo/OTM5/d80/14%/IV80 | +25.3% | -13.5% | 40% | $814,135 | $-136k | 49/26 |
| 9mo/OTM5/d80/14%/IV60 | +25.1% | -6.0% | 47% | $813,228 | $-137k | 51/24 |
| 6mo/ITM5/d85/14%/IV80 | +25.1% | -7.0% | 48% | $812,904 | $-137k | 59/16 |
| 6mo/ATM/d80/14%/IV80 | +25.0% | -7.3% | 45% | $812,191 | $-138k | 55/20 |
| 6mo/ATM/d85/14%/IV80 | +24.9% | -8.2% | 47% | $811,782 | $-138k | 54/21 |
| 4mo/ATM/d90/7%/IV80 | +24.2% | -6.5% | 47% | $807,123 | $-143k | 51/24 |
| 9mo/ITM5/d80/7%/IV60 | +24.1% | -5.7% | 48% | $806,342 | $-144k | 60/15 |
| 4mo/ITM5/d85/7%/IV80 | +23.6% | -6.6% | 44% | $803,233 | $-147k | 53/22 |
| 4mo/ITM5/d80/7%/IV80 | +23.1% | -7.0% | 43% | $800,159 | $-150k | 56/19 |
| 3mo/ATM/d85/7%/IV80 | +23.0% | -6.6% | 40% | $799,389 | $-151k | 51/24 |
| 3mo/ATM/d80/7%/IV80 | +22.9% | -6.7% | 40% | $799,011 | $-151k | 53/22 |
| 9mo/ATM/d90/7%/IV60 | +22.8% | -4.1% | 49% | $798,055 | $-152k | 51/24 |
| 3mo/ATM/d90/7%/IV80 | +22.7% | -6.6% | 40% | $797,852 | $-152k | 50/25 |
| 4mo/ITM5/d90/7%/IV80 | +22.7% | -7.6% | 44% | $797,400 | $-153k | 53/22 |
| 4mo/ATM/d80/7%/IV80 | +22.4% | -6.3% | 44% | $795,863 | $-154k | 54/21 |
| 6mo/ITM5/d90/7%/IV80 | +22.2% | -4.8% | 52% | $794,559 | $-156k | 59/16 |
| 4mo/ATM/d85/7%/IV80 | +21.9% | -6.5% | 44% | $792,154 | $-158k | 51/24 |
| 3mo/OTM5/d90/7%/IV80 | +21.4% | -6.7% | 39% | $789,338 | $-161k | 45/30 |
| 4mo/OTM5/d90/7%/IV80 | +21.4% | -6.8% | 43% | $789,175 | $-161k | 49/26 |
| 3mo/OTM5/d85/7%/IV80 | +21.4% | -6.7% | 39% | $788,905 | $-161k | 45/30 |
| 9mo/OTM5/d85/7%/IV60 | +21.3% | -4.4% | 51% | $788,701 | $-161k | 49/26 |
| 3mo/OTM5/d80/7%/IV80 | +20.9% | -6.8% | 39% | $785,766 | $-164k | 46/29 |
| 9mo/ITM5/d85/7%/IV60 | +20.9% | -6.1% | 49% | $785,643 | $-165k | 54/21 |
| 2mo/ITM5/d80/7%/IV80 | +20.7% | -5.8% | 39% | $784,497 | $-166k | 56/19 |
| 9mo/ATM/d85/7%/IV60 | +20.5% | -4.2% | 49% | $782,972 | $-167k | 52/23 |
| 6mo/OTM5/d80/14%/IV80 | +20.2% | -9.3% | 45% | $781,255 | $-169k | 51/24 |
| 9mo/OTM5/d90/7%/IV60 | +19.7% | -5.8% | 45% | $778,105 | $-172k | 49/26 |
| 4mo/OTM5/d85/7%/IV80 | +19.5% | -6.9% | 41% | $777,036 | $-173k | 49/26 |
| 6mo/ITM5/d80/7%/IV80 | +19.2% | -6.3% | 47% | $774,490 | $-176k | 61/14 |
| 6mo/ATM/d90/7%/IV80 | +18.9% | -5.4% | 47% | $772,791 | $-177k | 54/21 |
| 9mo/ATM/d80/7%/IV60 | +18.8% | -4.9% | 47% | $772,475 | $-178k | 53/22 |
| 9mo/OTM5/d80/7%/IV60 | +18.5% | -4.5% | 47% | $770,201 | $-180k | 51/24 |
| 4mo/OTM5/d80/7%/IV80 | +18.2% | -6.9% | 40% | $768,454 | $-182k | 49/26 |
| 6mo/ITM5/d85/7%/IV80 | +17.8% | -5.0% | 48% | $765,702 | $-184k | 59/16 |
| 6mo/OTM5/d85/7%/IV80 | +17.3% | -5.6% | 48% | $762,771 | $-187k | 51/24 |
| 9mo/OTM5/d90/14%/IV80 | +17.2% | -9.4% | 43% | $761,795 | $-188k | 49/26 |
| 6mo/OTM5/d90/7%/IV80 | +17.1% | -5.6% | 47% | $761,221 | $-189k | 51/24 |
| 6mo/ATM/d80/7%/IV80 | +17.1% | -4.9% | 45% | $761,182 | $-189k | 55/20 |
| 6mo/ATM/d85/7%/IV80 | +16.6% | -5.5% | 47% | $758,210 | $-192k | 54/21 |
| 6mo/OTM5/d80/7%/IV80 | +13.7% | -6.0% | 45% | $738,938 | $-211k | 51/24 |
| 9mo/OTM5/d90/7%/IV80 | +12.2% | -8.3% | 43% | $729,056 | $-221k | 49/26 |
| 9mo/ITM5/d85/14%/IV80 | +10.3% | -9.5% | 48% | $716,906 | $-233k | 53/22 |
| 9mo/ATM/d85/14%/IV80 | +10.3% | -7.3% | 47% | $716,635 | $-234k | 51/24 |
| 9mo/ITM5/d80/14%/IV80 | +9.2% | -7.9% | 45% | $709,934 | $-240k | 58/17 |
| 9mo/ITM5/d90/14%/IV80 | +8.6% | -9.4% | 44% | $705,841 | $-244k | 53/22 |
| 9mo/ITM5/d80/7%/IV80 | +8.5% | -6.8% | 45% | $705,117 | $-245k | 58/17 |
| 9mo/ATM/d85/7%/IV80 | +8.4% | -6.2% | 47% | $704,683 | $-245k | 51/24 |
| 9mo/ITM5/d85/7%/IV80 | +8.1% | -8.7% | 48% | $702,781 | $-247k | 53/22 |
| 9mo/ITM5/d90/7%/IV80 | +6.0% | -9.2% | 44% | $688,959 | $-261k | 53/22 |
| 9mo/OTM5/d85/14%/IV80 | +5.2% | -10.7% | 41% | $683,949 | $-266k | 49/26 |
| 9mo/ATM/d90/14%/IV80 | +5.2% | -10.8% | 41% | $683,876 | $-266k | 51/24 |
| 9mo/ATM/d80/7%/IV80 | +4.2% | -8.3% | 40% | $677,378 | $-273k | 52/23 |
| 9mo/ATM/d80/14%/IV80 | +4.1% | -11.5% | 40% | $676,944 | $-273k | 52/23 |
| 9mo/OTM5/d85/7%/IV80 | +3.7% | -9.2% | 41% | $673,980 | $-276k | 49/26 |
| 9mo/ATM/d90/7%/IV80 | +3.1% | -9.7% | 41% | $670,257 | $-280k | 51/24 |
| 9mo/OTM5/d80/7%/IV80 | +2.5% | -9.9% | 37% | $666,066 | $-284k | 50/25 |
| 9mo/OTM5/d80/14%/IV80 | +2.4% | -12.8% | 37% | $665,537 | $-285k | 50/25 |

- **Best cell:** `2mo/OTM5/d90/14%/IV40` at +261.6% ($+1,400k vs stock). **Worst cell:** `9mo/OTM5/d80/14%/IV80` at +2.4% ($-285k vs stock). The grid is EXPLORATORY sensitivity, not a recommendation — do not read the best cell as a chosen strategy.

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
| NVDA | 2024-05-15 | +25.0% | $+6k | $-3k | $+10k | expired-worthless |
| NVDA | 2024-05-15 | +25.0% | $+6k | $-2k | $+8k | expired-worthless |
| CLS | 2026-04-13 | -0.7% | $0k | $-6k | $+6k | expired-worthless |
| ANET | 2025-09-09 | -1.0% | $-1k | $-3k | $+2k | expired-worthless |
| DXCM | 2023-06-29 | -4.6% | $-2k | $-2k | $+1k | expired-worthless |

**NET (a) - (b) = $+192k**  (shakeout-survival wins $+218k minus theta/stall losses $+26k).

_Honesty note (not part of the named net): on WINNERS the option gave up **$+293k** to notional-cap under-participation (it owned fewer shares than the stock, so it captured less of the run — the dominant drag), and on names where BOTH lost, the option 'saved' $-63k purely by betting less (loss-mitigation, not a real edge)._

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
- **Why:** the delta trigger keeps the call in INSURANCE mode through shakeouts (it converts only once delta >= trigger), so it DOES buy some shakeout survival (decomposition (a) = $+218k). But two costs dominate: (i) THETA/STALL — names that chopped sideways bled the premium to zero ($+26k), and (ii) NOTIONAL CAP — a 7% budget buys only ~0.41x the stock notional on these high-IV names, so even the CONVERTED winners own fewer shares and under-participate (gave up $+293k on winners). Net of the two named forces: $+192k.
- **Regime read:** the option's relative case is least-bad in CHOPPY stretches (shakeouts frequent -> survival edge earns its keep) and worst in a clean BULL (2024/2025), where the stock book's full notional simply compounds and the option's capped delta + theta drag lose the race. See the per-year table.

### Hard limits (curve-fit + honesty guards, rule #1)
- **Modeled BS prices + delta, not real fills.** No bid/ask spread, no vol surface/skew, one flat IV per run, and NO per-trade earnings IV bump/crush (per-trade earnings dates are not in the ledger — disclosed, not faked). Real spreads/skew would make the option book WORSE, so this is a friendly upper bound. Validate on real historical quotes before trusting.
- **Liquid-option subset only** (55 names) — EXCLUDES the thin small-caps this system often trades, where the 'can't be shaken out' pitch is most appealing but real option liquidity is worst. The answer does NOT transfer to those names.
- **Full grid + IV sweep reported** so nothing is cherry-picked; the grid is exploratory sensitivity, not a chosen cell.
- **Small sample, bull-heavy 2023-2026 window** — cannot test a bear regime; the let-winners-run edge flatters BOTH books in a bull.
- **Exercise/delivery capital assumption:** conversion deploys the full strike dollars (a real cash draw funded from the same start capital; modeled as a cash round-trip).
