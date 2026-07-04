# SPX Cash Flow / 0DTE Supply-Demand Strategy Research Handoff

**Prepared for:** Andrew Surber  
**Prepared on:** 2026-06-30  
**Purpose:** Transfer the current research state to a database / AI system so it can cross-reference Brandon Wendell / SPX Cash Flow Secrets trade alerts against historical SPX market data and SPXW option-chain data, with the goal of recreating the supply-and-demand zone logic and testing the trade rules objectively.

---

## 1. Why this project exists

The working objective is to reverse-engineer, document, and test a 0DTE SPX income strategy promoted under the name **SPX Cash Flow Secrets** and associated with Brandon Wendell. The key question is not simply whether the reported returns were good. The more valuable question is:

> Can we recreate the supply and demand zones he used, map them to SPX 0DTE spread strikes, and determine whether the method has a repeatable, testable edge when applied to real historical market data?

The user has participated in the daily SPX text-message alert service and exported the full SMS thread from a Google Pixel 8. The user also uploaded several historical performance screenshots and spreadsheets that appear to be distributed by the service. The handoff system should use these materials to build a normalized, timestamped research database.

This is a research and reconstruction project, not a recommendation to trade. The source materials repeatedly state that alerts were educational/simulated and that past performance is not indicative of future results.

---

## 2. Current file inventory

### Uploaded / source files

| File | Path | Size | Short SHA256 | Purpose |
|---|---:|---:|---:|---|
| SMS XML export | `/mnt/data/sms-20260630152134.xml` | 1,108,165 bytes | `2d8ee4795b34f7cd` | Raw SMS Backup & Restore export from Android. Contains 2,413 messages. |
| September 2022 trade tracking spreadsheet | `/mnt/data/SPX Cash Flow Trade Tracking September 2022.xlsx` | 48,540 bytes | `65a576f9dbde728c` | Detailed September 2022 row-level trade tracker. |
| Yearly trade tracking spreadsheet | `/mnt/data/SPX Cash Flow Yearly Trade tracking.xlsx` | 28,223 bytes | `1064e40916e87896` | 2021/2022 monthly summary workbook; appears incomplete vs screenshot because the screenshot has full 2022. |
| January 2023 results screenshot | `/mnt/data/Jan 23 results.JPG` | 286,851 bytes | n/a | Screenshot of January 2023 trade tracking page. |
| 2022 results screenshot | `/mnt/data/2022 Results pic.JPG` | 180,451 bytes | n/a | Screenshot of full-year 2022 monthly tracker. |
| 2023 summary screenshot | `/mnt/data/image (4).png` | 68,936 bytes | n/a | Screenshot showing Jan-May 2023 monthly summary. |
| May 2023 result box screenshot | `/mnt/data/image (3).png` | 85,912 bytes | n/a | Screenshot showing May 2023 result box. |

### Files created during parsing

| File | Path | Size | Short SHA256 | Purpose |
|---|---:|---:|---:|---|
| Clean SMS log | `/mnt/data/spx_sms_clean_messages.csv` | 486,859 bytes | `ed656e81d5629d7d` | Normalized message table. |
| Extracted trade candidates | `/mnt/data/spx_trade_candidates_extracted.csv` | 111,980 bytes | `065a8c9240d40eb6` | Parsed actionable-looking SPX trade alerts. |
| Daily SPX summary | `/mnt/data/spx_daily_summary.csv` | 23,437 bytes | `cc2d658732ecb099` | Daily status table summarizing trade days, stops, profits, and no-trade days. |

---

## 3. Source-data caveats

1. **SMS file is not encrypted.** The useful content is the XML `body` field. The `subject` values beginning with `proto:` appear to be Google Messages / Android metadata and should be ignored for strategy analysis.

2. **SMS timestamps are local phone timestamps.** The phone appears to be in Central Time. Market data should be stored in Eastern Time. For cross-reference, convert SMS timestamp to ET by adding one hour, unless the XML timestamps and local system timezone prove otherwise.

3. **Alert time is not necessarily fill time.** Text alert delivery, user execution delay, bid/ask spread, broker platform, and market speed matter. A backtest must model fill assumptions separately.

4. **Excel and screenshots are not raw broker data.** The tracker explicitly describes positions as simulated. It is useful for understanding intended rules and reported performance, but not enough to prove live trade results.

5. **Multiple trade versions appear on the same day.** Some days include aggressive, conservative, safer, or alternative variants. The service may report all as wins, a subset as stopped, or note that a stopped trade would have expired worthless if held. Database modeling must preserve each candidate separately.

6. **Outcome messages are not a clean trade ledger.** “Full profit” sometimes refers to all options expiring worthless, but the subscriber may already have stopped out or closed. Do not treat every “full profit” message as the realized result for every candidate unless the trade row confirms it.

---

## 4. External/public research found so far

The public web sources below are useful for method reconstruction. LinkedIn visibility is limited because many posts are behind login, but some are indexed publicly.

### High-value public sources

1. **MoneyShow speaker/course page for Brandon Wendell: “Two-Hour Trading: 0DTE Options Income Strategies.”**  
   URL: `https://lasvegasmms.moneyshow.com/speakers/brandon-wendell/`  
   Why it matters: This page directly ties Brandon Wendell to a 0DTE SPX iron condor system called SPX Cash Flow Secrets. It states that the final two hours provide a predictable window for collecting premium, identifies **2:00 PM** as an entry sweet spot, and says the strike selection framework uses **VIX, supply and demand zones, and delta targeting**. It also lists risk controls: **2x stop-loss, minimum credit thresholds, and 5% account cap**.

2. **MoneyShow course listing: “Two-Hour Trading: 0DTE Options Income Strategies.”**  
   URL: `https://lasvegasmms.moneyshow.com/courses/`  
   Why it matters: Confirms the same details: 0DTE SPX iron condors, final two hours, 2:00 PM entry sweet spot, VIX, supply/demand zones, delta targeting, 2x stop-loss, minimum credit thresholds, 5% account cap.

3. **LinkedIn post: SPX Cash Flow Secrets low-premium/no-trade month.**  
   URL: `https://www.linkedin.com/posts/bwendellcmt_spx-0dte-zerodte-activity-7080657048983457792-6k4q`  
   Why it matters: Publicly indexed post says they could not get a trade because premium was too low and references over 90% win rate and high return on risk/investment. This supports the finding that **premium threshold** is a key trade/no-trade filter.

4. **LinkedIn post: supply and demand zones worked for SPX Cash Flow traders.**  
   URL: `https://www.linkedin.com/posts/bwendellcmt_supply-demand-odte-activity-7071954878574514176-emMF`  
   Why it matters: Publicly indexed post says supply and demand worked and references SPX Cash Flow traders using the zones for wins. This supports the thesis that supply/demand zone detection is central, not incidental.

5. **LinkedIn post: “Beginners Guide on Using Supply & Demand Zones to Trade Options & Futures.”**  
   URL: `https://www.linkedin.com/posts/bwendellcmt_beginners-guide-on-using-supply-demand-activity-7108142876802318336-7Qir`  
   Why it matters: Publicly indexed post references a free workshop on institutional order flow and what causes supply and demand. Useful for locating further educational material.

6. **MoneyShow article by Brandon Wendell: “Finding the Best Trading Zones.”**  
   URL: `https://www.moneyshow.com/articles/daytraders-26700/`  
   Why it matters: Explains his broad supply/demand philosophy: buy in demand, sell in supply, stops below demand, broken demand can become supply, broken supply can become demand, and trend context matters.

7. **MoneyShow article by Brandon Wendell: “Charting Error Can Sink Option Traders.”**  
   URL: `https://www.moneyshow.com/articles/optionsidea-28353/`  
   Why it matters: Important for options testing. He states that options are derivatives and the underlying security should be charted for decision-making. This supports using the SPX cash index chart to identify zones, not option premium charts alone.

8. **MoneyShow article by Brandon Wendell: “How to Use and Misuse the CCI Indicator.”**  
   URL: `https://www.moneyshow.com/articles/tradingidea-28058/`  
   Why it matters: Describes supply/demand zones as the decision-maker and oscillators as confirmation only. He discusses fresh demand levels and divergence near zones. Useful as an optional “odds enhancer” hypothesis.

9. **MoneyShow article by Brandon Wendell: “The Right Way to Use Oscillators for Profit.”**  
   URL: `https://www.moneyshow.com/articles/tradingidea-28911/`  
   Why it matters: Again frames RSI/CCI as confirmation at supply/demand zones, not as primary entry triggers.

10. **Options Insider / Brandon Wendell: “Base vs. Pivot.”**  
    URL: `https://theoptionsinsider.com/education/base-vs-pivot/`  
    Why it matters: Explains that the origin of imbalance usually comes near a **base**, which is a pause in price movement. This is directly relevant to algorithmic zone detection.

### YouTube / transcript research targets

Search terms / known results:

- `Brandon Wendell 0DTE Options Trading SPX Cash Flow Secrets`
- `Brandon Wendell How Time Affects Your Options Trades SPX Cashflow Secrets`
- `0DTE Options Trading with Brandon Wendell 5/02/22`
- `Brandon Wendell supply demand zones options futures`
- `Brandon Wendell CMT Finding High Quality Turning Points`
- `Working The Order Supply & Demand Zones Brandon Wendell`

If captions/transcripts are not attached to YouTube videos, use one of these approaches:

1. Check YouTube transcript panel manually.
2. Use `yt-dlp --write-auto-sub --write-sub --skip-download <url>` where permitted.
3. Download audio and run Whisper or another speech-to-text engine locally.
4. Preserve timestamped transcript lines, because the research system may need to connect specific rules to specific statements.

---

## 5. What we know from the SMS export

### Basic SMS dataset

- Total messages parsed: **2,413**
- Date range: **2022-10-10 13:02:28** to **2024-06-18 13:02:03** local phone time
- Incoming messages: **2,410**
- Outgoing messages: **3**
- Messages classified as SPX-related: **2,018**
- Other futures-related messages: **3**
- Contact name in the SMS export: **Wealth Builders**
- Phone number in export: **+1 516-272-4677**

### Trade candidate extraction

After removing obvious disclaimers, duplicates, “do not enter” messages, close-only messages, and non-SPX futures items, the extraction produced:

- Actionable-looking SPX trade candidate alerts: **534**
- Unique candidate trade days: **349**

Trade type mix:

| Trade type | Count | Share |
|---|---:|---:|
| Iron Condor | 362 | ~67.8% |
| Bear Call Spread | 91 | ~17.0% |
| Bull Put Spread | 81 | ~15.2% |

Aggression / label mix:

| Label | Count |
|---|---:|
| Standard / unspecified | 280 |
| Aggressive | 167 |
| Very aggressive | 57 |
| Conservative / safer | 28 |
| Alternative | 2 |

Credit and probability profile:

| Metric | Result |
|---|---:|
| Mean low-side credit | ~$0.479 |
| Median low-side credit | ~$0.400 |
| Mean high-side credit | ~$0.501 |
| Median high-side credit | ~$0.450 |
| Median stated probability of profit | ~62.5% |
| Stated PoP range | 40% to 94% |

By trade type:

| Trade type | Count | Median low credit | Median high credit | Median PoP |
|---|---:|---:|---:|---:|
| Iron Condor | 362 | $0.40 | $0.40 | 62% |
| Bear Call Spread | 91 | $0.50 | $0.50 | 64% |
| Bull Put Spread | 81 | $0.45 | $0.45 | 63% |

Daily outcome classification from SMS text:

| Daily status | Count |
|---|---:|
| Profit reported | 290 |
| Unknown / insufficient outcome text | 52 |
| Stop alert only | 16 |
| Mixed stop and profit | 15 |
| No trade declared | 9 |
| Stop then recovered if held | 8 |
| Skip/no-trade mentioned | 8 |

Aggregate flags:

- Days with at least one trade candidate: **349**
- Days with profit/expired-worthless language: **313**
- Days with stop-alert language: **39**
- Days explicitly noting “stopped but would have worked/expired worthless if held”: **8**
- Days with no-trade / skip language: **38**

### Time-of-day finding

The median extracted SMS trade-candidate alert time is approximately:

- **13:08 Central Time**
- **14:08 Eastern Time**

This lines up closely with the public MoneyShow description that **2:00 PM ET** is the entry sweet spot.

### Terminology frequency in the SMS dataset

Approximate counts from message text:

| Term | Count |
|---|---:|
| `supply` | 703 |
| `demand` | 743 |
| `5 min` | 378 |
| `15 min` | 191 |
| `10 min` | 2 |
| `30 min` | 3 |
| `stop` | 162 |
| `credit` | 109 |
| `premium` | 88 |
| `VIX` | 0 in SMS text |
| `delta` | 0 in SMS text |

Interpretation: The live text service emphasizes supply/demand zones and premiums directly. Public marketing/education mentions VIX and delta targeting, but the SMS alerts do not commonly expose those details.

---

## 6. What we know from the Excel / screenshot performance materials

### September 2022 detailed spreadsheet

The uploaded September 2022 workbook shows a detailed trade-tracking table with these columns:

```text
Date, Position, Strikes, Premium, Margin, Entry Time, Exit, Win or Loss,
$ Profit/Loss, ROR, Type of Exit, Reason
```

The tracker contains row-level entries such as IC, BCS, and BPS positions. It explicitly states:

- “All positions are Simulated.”
- “The trades are taken in a virtual account in Simulated mode but made as realistic as possible.”
- “Entries and Exits are based on the rules set out in the SPX Cash Flow Secrets Class.”

September 2022 row-level summary from the uploaded workbook:

- Rows with dated entries: **38**
- Winning rows: **33**
- Losing rows: **3**
- N/A / no-entry rows: **2**
- Net reported monthly P/L: **$1,966**
- Win rate excluding N/A rows: **91.67%**
- Position mix in workbook rows:
  - BCS: 22
  - IC: 10
  - BPS: 5
  - One no-trade/blank position row

This matches the 2022 screenshot’s September line: **36 trades, 33 winners, 3 losers, $1,966 total, 91.67% win rate, 98.30% ROR on $2k.**

### 2022 full-year screenshot summary

From the uploaded 2022 screenshot:

| Month | # Trades | Winners | Losers | Avg Win | Avg Loss | Win % | Total | Avg ROR | ROR on $2k |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| January | 22 | 16 | 6 | $43.08 | $113.33 | 72.73% | $15 | -9.00% | 0.75% |
| February | 24 | 23 | 1 | $62.39 | $90.00 | 95.83% | $1,345 | 13.37% | 67.25% |
| March | 24 | 19 | 5 | $59.74 | $95.20 | 79.17% | $659 | 6.32% | 32.95% |
| April | 15 | 14 | 1 | $47.14 | $140.00 | 93.33% | $520 | 7.67% | 26.00% |
| May | 24 | 18 | 6 | $52.63 | $80.00 | 75.00% | $520 | 4.87% | 26.00% |
| June | 31 | 26 | 5 | $64.04 | $136.00 | 83.87% | $985 | 7.93% | 49.25% |
| July | 27 | 24 | 3 | $42.60 | $290.00 | 88.89% | $300 | 1.48% | 15.00% |
| August | 25 | 20 | 5 | $74.50 | $90.00 | 80.00% | $1,040 | 14.13% | 52.00% |
| September | 36 | 33 | 3 | $64.74 | $100.00 | 91.67% | $1,966 | 13.00% | 98.30% |
| October | 30 | 28 | 2 | $61.25 | $85.00 | 93.33% | $1,545 | 12.15% | 77.25% |
| November | 25 | 19 | 6 | $52.16 | $119.33 | 76.00% | $275 | 2.40% | 13.75% |
| December | 27 | 21 | 6 | $58.10 | $108.00 | 77.78% | $572 | 4.66% | 28.60% |
| **2022 Average** | **25.83** | **21.75** | **4.08** | **$56.86** | **$120.57** | **83.97%** | **$811.83** | **6.58%** | **40.59%** |
| **2022 Total** |  |  |  |  |  |  | **$9,742** |  | **487.10%** |

### 2023 Jan-May screenshot summary

From the uploaded 2023 summary screenshot:

| Month | # Trades | Winners | Losers | Avg Win | Avg Loss | Win % | Total | Avg ROR | ROR on $2k |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| January | 30 | 25 | 5 | $54.78 | $117.50 | 83.33% | $470 | 2.55% | 23.50% |
| February | 29 | 24 | 5 | $60.00 | $103.00 | 82.76% | $925 | 7.32% | 46.25% |
| March | 41 | 36 | 5 | $58.06 | $92.60 | 87.80% | $1,627 | 9.21% | 81.35% |
| April | 33 | 29 | 4 | $49.48 | $217.50 | 87.88% | $565 | 3.47% | 28.25% |
| May | 34 | 29 | 5 | $54.48 | $153.00 | 85.29% | $815 | 6.07% | 40.75% |
| **Jan-May Average** | **33.4** | **28.6** | **4.8** | **$55.36** | **$136.72** | **85.41%** | **$880.40** | **5.72%** | **44.02%** |
| **Jan-May Total** |  |  |  |  |  |  | **$4,402** |  | **220.10%** |

### Interpretation of performance materials

- The reported win rate is high, typically around 80%-90%.
- The average loss is consistently much larger than the average win, often around 2x-4x the average win.
- The strategy relies on high win frequency and small defined-risk spreads.
- The reported “ROR on $2k” is very high because it appears to assume one SPX contract on a $2,000 model account. A 5-wide spread has roughly $350-$475 of margin/risk after credit, so one contract is a large risk unit relative to $2,000.
- The win/loss math is highly sensitive to whether stops are followed, whether trades are held to expiration, and whether aggressive variants are selected.

---

## 7. Reconstructed strategy thesis

The current working thesis is:

> SPX Cash Flow Secrets is a discretionary 0DTE SPXW premium-selling strategy that identifies active intraday supply and demand zones on SPX, then sells 5-point-wide SPX credit spreads or iron condors just outside those zones, primarily around 2:00 PM ET, with minimum credit filters, a premium-based stop, and either a $0.05 profit exit or expiration hold.

### Core instruments

- Underlying chart: **SPX cash index**
- Options instrument: **SPXW PM-settled weeklies / 0DTE options**
- Trade structures:
  - Bull put spread, abbreviated BPS
  - Bear call spread, abbreviated BCS
  - Iron condor, abbreviated IC
- Spread width: usually **5 SPX points**

### Common SMS examples

#### Demand-to-BPS mapping

Example structure:

```text
5 Min Demand from today @ 3606-3610.
Bull Put Spread 3600/3605 is available.
```

Interpretation:

- Demand zone lower boundary around 3606.
- Short put strike is 3605, just below demand.
- Long put strike is 3600, 5 points lower.
- Spread notation `3600/3605` means buy lower put / sell higher put.

#### Supply-to-BCS mapping

Example structure:

```text
15 Min Supply @ 3661-3669.
Bear Call Spread 3670/3675.
```

Interpretation:

- Supply zone upper boundary around 3669.
- Short call strike is 3670, just above supply.
- Long call strike is 3675, 5 points higher.
- Spread notation `3670/3675` means sell lower call / buy higher call.

#### Supply+demand-to-IC mapping

Example structure:

```text
Iron Condor, 3580/3585 & 3670/3675.
```

Interpretation:

- Put side: buy 3580 put / sell 3585 put, below demand.
- Call side: sell 3670 call / buy 3675 call, above supply.
- Total credit is collected across both sides.

---

## 8. Zone theory to model

The strategy appears rooted in Online Trading Academy-style supply and demand concepts: zones originate at the base before a strong imbalance-driven departure.

### Demand zone patterns

Potential demand-zone formations:

1. **Drop-Base-Rally (DBR)**  
   Price falls into a base, then rallies strongly away. This is a reversal demand zone.

2. **Rally-Base-Rally (RBR)**  
   Price rallies, pauses/base, then continues higher. This is continuation demand.

A demand zone should only be considered valid for a BPS if current price is above the zone and the short put can be placed at or just below the zone.

### Supply zone patterns

Potential supply-zone formations:

1. **Rally-Base-Drop (RBD)**  
   Price rallies into a base, then drops strongly away. This is a reversal supply zone.

2. **Drop-Base-Drop (DBD)**  
   Price drops, pauses/base, then continues lower. This is continuation supply.

A supply zone should only be considered valid for a BCS if current price is below the zone and the short call can be placed at or just above the zone.

### Base definition hypothesis

A base is a pause in price movement. For algorithmic testing, start with a parameterized definition:

```text
A base = 1 to 6 candles where:
- candle bodies are relatively small vs recent ATR or true range,
- candles overlap materially,
- the total base range is not too wide relative to current 0DTE option pricing,
- and price subsequently departs with momentum.
```

Initial base parameters to test:

```text
base_min_candles = 1
base_max_candles = 6
base_body_to_range_max = 0.60
base_total_range_max_atr_multiple = 1.25
base_overlap_required = true
```

### Departure definition hypothesis

A departure is the move away from the base that proves imbalance.

Initial parameters to test:

```text
departure_lookahead_candles = 1 to 3
min_departure_distance = 1.5x to 2.5x base_range
min_departure_close_beyond_base = true
strong_body_required = optional
volume_filter = optional if volume data available
```

Important: During live reconstruction at alert time, do not use future candles beyond the alert timestamp. However, historical zone creation necessarily used prior candles. A zone can be identified only after the departure has occurred.

### Zone boundaries

For testing, use two candidate boundary definitions and calibrate against the SMS zone text.

#### Boundary model A: full wick range of base

Demand:

```text
zone_low = min(low of base candles)
zone_high = max(high of base candles)
```

Supply:

```text
zone_low = min(low of base candles)
zone_high = max(high of base candles)
```

This is simple and may match many text zones.

#### Boundary model B: distal/proximal body/wick hybrid

Demand:

```text
distal_line = min(low of base candles)
proximal_line = max(open/close body of base candles) or highest body boundary nearest current price
```

Supply:

```text
distal_line = max(high of base candles)
proximal_line = min(open/close body of base candles) or lowest body boundary nearest current price
```

Because the SMS text often reports zones like `3606-3610`, not “distal/proximal” language, the system should test both definitions and choose the model with the best match rate.

### Freshness / testing logic

Public educational material emphasizes that second tests can be weaker and that broken zones can reverse roles. Model this explicitly.

For each candidate zone, compute:

```text
freshness_state:
  fresh = price has not returned to the zone after departure
  tested_once = price has touched the zone once after departure
  tested_multiple = price has touched two or more times
  broken = price closed beyond distal boundary
  role_reversal = broken demand becomes possible supply, or broken supply becomes possible demand
```

The SMS sometimes mentions “tested demand,” so the system should not exclude all tested zones. Instead, store freshness and let the optimizer learn when tested zones were used.

---

## 9. Timeframe hierarchy to test

The SMS export shows heavy usage of:

1. **5-minute zones** — most common in text examples
2. **15-minute zones** — second most common
3. **10-minute zones** — rare
4. **30-minute zones** — rare

Initial inference:

- 15-minute zones may be preferred when close enough to current price and premium is viable.
- 5-minute zones appear to be used when 15-minute zones are too far away, unavailable, already tested, or premium is too low.
- 10-minute/30-minute references are rare and may be exceptions or later rule evolution.

The backtest should create bars for:

```text
1-minute source bars
5-minute resampled bars
10-minute resampled bars
15-minute resampled bars
30-minute resampled bars
```

All bar aggregation should be session-aware using regular SPX cash session times:

```text
09:30:00 ET to 16:00:00 ET
```

---

## 10. Strike-selection rules to test

### Bull put spread

When a demand zone is valid and below spot:

```text
short_put = nearest 5-point strike at or below demand_zone_low
long_put = short_put - 5
spread = long_put / short_put
```

Example:

```text
demand zone = 3606-3610
short_put = 3605
long_put = 3600
spread text = 3600/3605
```

### Bear call spread

When a supply zone is valid and above spot:

```text
short_call = nearest 5-point strike at or above supply_zone_high
long_call = short_call + 5
spread = short_call / long_call
```

Example:

```text
supply zone = 3661-3669
short_call = 3670
long_call = 3675
spread text = 3670/3675
```

### Iron condor

When both sides are valid:

```text
put_side = bull put spread below demand
call_side = bear call spread above supply
iron_condor = put_side & call_side
```

### Aggressive / conservative variants

The text service often sends several variants:

- **Aggressive:** closer strikes, higher credit, lower stated PoP, possibly tested/weaker zone.
- **Very aggressive:** even closer strikes or higher credit, often more path-dependent.
- **Conservative / safer:** farther strikes, lower credit, higher stated PoP, likely outside cleaner zones.
- **Alternative:** a second version to compare with aggressive or conservative.

The database should preserve all variants and not collapse them into a single trade.

---

## 11. Premium, PoP, and no-trade rules

### Minimum credit filter

Strong evidence from SMS and public LinkedIn material suggests that trades are skipped when premium is too low.

Initial test thresholds:

```text
minimum_credit = 0.30
minimum_credit_alt = 0.35
```

Observed text examples:

- “Do not enter” when an IC or BCS is only $0.10-$0.15.
- “We need $0.30 to $0.35 to trade it.”
- Public LinkedIn post referenced no trade because premium was too low.

### Typical credit targets

Observed median credits:

```text
Iron Condor median credit: ~$0.40
Bear Call Spread median credit: ~$0.50
Bull Put Spread median credit: ~$0.45
```

### Probability of profit

The SMS alerts often include stated PoP but not always. Median stated PoP is about 62.5%. Public MoneyShow material says strike selection uses delta targeting, but SMS text rarely exposes delta directly.

Initial hypothesis:

```text
PoP/delta is used as a secondary filter or platform-calculated reference,
not as the primary source of the signal.
```

Test when option chain Greeks are available:

```text
short_strike_delta_abs range: roughly 0.05 to 0.30, with aggressive variants higher
stated PoP target: generally 55%-75%, with occasional very high PoP trades
```

---

## 12. Entry-time rules to test

### Public rule

MoneyShow material says **2:00 PM ET** is the sweet spot and the final two hours offer the most predictable window.

### SMS-derived behavior

Median extracted alert time:

```text
13:08 CT / 14:08 ET
```

### Test windows

Run separate backtests for:

```text
Window A: 13:45-14:15 ET
Window B: 14:00-14:30 ET
Window C: 13:30-15:00 ET
Window D: exact SMS alert time from parsed data
Window E: Excel tracker entry time where available
```

Important: The SMS service sometimes sends alerts before/after the main window. These should be tagged as exceptions, not discarded.

---

## 13. Exit and stop logic

### Profit-taking rule

Common SMS language:

```text
Can be closed for $0.05 or held to expiration.
```

Backtest variants:

```text
profit_exit_A = close when spread/condor debit <= 0.05
profit_exit_B = hold to expiration
profit_exit_C = close at 80%-90% of max credit captured
```

### Stop-loss interpretation

This is a key research issue.

Public MoneyShow material says **2x stop-loss**.

Older SMS examples sometimes say the trade stopped when the premium exceeded about 3x the original credit. Example logic:

```text
entry credit = 0.35
stop debit = 1.05
net P/L = 0.35 - 1.05 = -0.70
net loss = 2x original credit
```

So the phrase “2x stop-loss” may mean:

```text
stop when net loss equals 2x credit,
which means closing debit equals 3x original credit.
```

Test all three interpretations:

```text
Stop Model 1: closing debit >= 2.0 * entry_credit
Stop Model 2: closing debit >= 3.0 * entry_credit
Stop Model 3: net loss >= 2.0 * entry_credit, i.e. closing_debit >= 3.0 * entry_credit
```

For a 5-wide spread:

```text
entry_credit = C
margin/risk = 5.00 - C
profit if expires OTM = C * 100
stop_debit = D
realized_P/L = (C - D) * 100
```

For iron condors, the option quote system must clarify whether stop debit is total IC package premium or tested side only. SMS language usually seems to refer to the full trade premium, but this must be validated.

### Expiration / ITM loss model

If held to expiration:

For BPS:

```text
if SPX_close >= short_put: full profit
if long_put < SPX_close < short_put: partial loss/profit based on intrinsic
if SPX_close <= long_put: max loss
```

For BCS:

```text
if SPX_close <= short_call: full profit
if short_call < SPX_close < long_call: partial loss/profit based on intrinsic
if SPX_close >= long_call: max loss
```

For IC:

```text
full profit if long_put/short_put side and short_call/long_call side both expire OTM
partial/full loss if either side expires ITM
```

---

## 14. Market data needed

### Required for zone reconstruction

At minimum:

```text
timestamp_ET, open, high, low, close
```

Preferred:

```text
timestamp_ET, open, high, low, close, volume
```

Best interval:

```text
1-minute SPX cash index bars
```

The database can resample 1-minute bars to 5m, 10m, 15m, and 30m.

### Required for trade simulation

To properly test fills, stops, and closes, the system needs historical 0DTE SPXW option quotes:

```text
timestamp_ET, expiration_date, dte, option_symbol, strike, call_put,
bid, ask, mid, last, delta, gamma, theta, vega, iv, open_interest, volume
```

Minimum viable option data:

```text
timestamp_ET, expiration_date, strike, call_put, bid, ask, mid
```

### Optional but useful

```text
VIX timestamped data
ES futures 1-minute data
SPY 1-minute data
Economic calendar / Fed / CPI flags
Market holiday / early-close calendar
```

Economic event flags matter because SMS examples include no-trade days due to Fed/high volatility.

---

## 15. Proposed database schema

### Table: `source_sms_messages`

```sql
CREATE TABLE source_sms_messages (
    message_id INTEGER PRIMARY KEY,
    source_file TEXT,
    xml_date_ms BIGINT,
    timestamp_local DATETIME,
    timestamp_et DATETIME,
    trade_date DATE,
    direction_type INTEGER,
    incoming BOOLEAN,
    outgoing BOOLEAN,
    contact_name TEXT,
    address TEXT,
    raw_text TEXT,
    normalized_text TEXT,
    spx_related BOOLEAN,
    futures_other BOOLEAN,
    disclaimer BOOLEAN,
    duplicate_hash TEXT
);
```

### Table: `extracted_trade_alerts`

```sql
CREATE TABLE extracted_trade_alerts (
    alert_id INTEGER PRIMARY KEY,
    message_id INTEGER,
    timestamp_et DATETIME,
    trade_date DATE,
    trade_type TEXT,           -- IC, BCS, BPS
    aggression_label TEXT,     -- aggressive, very aggressive, conservative, safer, standard, alternative
    spread_text TEXT,
    put_long_strike REAL,
    put_short_strike REAL,
    call_short_strike REAL,
    call_long_strike REAL,
    width_points REAL,
    credit_low REAL,
    credit_high REAL,
    credit_mid REAL,
    stated_pop REAL,
    no_entry BOOLEAN,
    do_not_enter BOOLEAN,
    raw_text TEXT
);
```

### Table: `extracted_zone_mentions`

```sql
CREATE TABLE extracted_zone_mentions (
    zone_mention_id INTEGER PRIMARY KEY,
    message_id INTEGER,
    timestamp_et DATETIME,
    trade_date DATE,
    zone_type TEXT,       -- supply/demand
    timeframe_minutes INTEGER,
    zone_source_date DATE,
    zone_source_time TIME,
    zone_low REAL,
    zone_high REAL,
    tested BOOLEAN,
    raw_text TEXT
);
```

### Table: `market_bars_1m`

```sql
CREATE TABLE market_bars_1m (
    timestamp_et DATETIME PRIMARY KEY,
    symbol TEXT,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL
);
```

### Table: `market_bars_resampled`

```sql
CREATE TABLE market_bars_resampled (
    timestamp_et DATETIME,
    symbol TEXT,
    timeframe_minutes INTEGER,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    PRIMARY KEY (timestamp_et, symbol, timeframe_minutes)
);
```

### Table: `candidate_zones`

```sql
CREATE TABLE candidate_zones (
    zone_id INTEGER PRIMARY KEY,
    created_timestamp_et DATETIME,
    valid_after_timestamp_et DATETIME,
    timeframe_minutes INTEGER,
    zone_type TEXT,       -- supply/demand
    pattern_type TEXT,    -- DBR, RBR, RBD, DBD
    base_start_timestamp_et DATETIME,
    base_end_timestamp_et DATETIME,
    departure_start_timestamp_et DATETIME,
    departure_end_timestamp_et DATETIME,
    zone_low REAL,
    zone_high REAL,
    distal_line REAL,
    proximal_line REAL,
    base_candle_count INTEGER,
    base_range REAL,
    departure_distance REAL,
    departure_atr_multiple REAL,
    freshness_state TEXT,
    touch_count INTEGER,
    broken BOOLEAN,
    role_reversal BOOLEAN,
    score REAL
);
```

### Table: `zone_alert_matches`

```sql
CREATE TABLE zone_alert_matches (
    match_id INTEGER PRIMARY KEY,
    zone_mention_id INTEGER,
    zone_id INTEGER,
    timestamp_error_minutes REAL,
    low_error_points REAL,
    high_error_points REAL,
    overlap_percent REAL,
    score REAL,
    match_quality TEXT
);
```

### Table: `option_quotes_0dte`

```sql
CREATE TABLE option_quotes_0dte (
    quote_id INTEGER PRIMARY KEY,
    timestamp_et DATETIME,
    expiration_date DATE,
    dte INTEGER,
    option_symbol TEXT,
    strike REAL,
    call_put TEXT,
    bid REAL,
    ask REAL,
    mid REAL,
    delta REAL,
    gamma REAL,
    theta REAL,
    vega REAL,
    iv REAL,
    volume INTEGER,
    open_interest INTEGER
);
```

### Table: `reconstructed_trade_candidates`

```sql
CREATE TABLE reconstructed_trade_candidates (
    reconstructed_trade_id INTEGER PRIMARY KEY,
    timestamp_et DATETIME,
    trade_date DATE,
    trade_type TEXT,
    demand_zone_id INTEGER,
    supply_zone_id INTEGER,
    put_long_strike REAL,
    put_short_strike REAL,
    call_short_strike REAL,
    call_long_strike REAL,
    credit_bid REAL,
    credit_ask REAL,
    credit_mid REAL,
    short_put_delta REAL,
    short_call_delta REAL,
    estimated_pop REAL,
    qualifies_min_credit BOOLEAN,
    qualifies_time_window BOOLEAN,
    qualifies_zone_score BOOLEAN,
    strategy_variant TEXT
);
```

### Table: `backtest_results`

```sql
CREATE TABLE backtest_results (
    result_id INTEGER PRIMARY KEY,
    run_id TEXT,
    reconstructed_trade_id INTEGER,
    entry_timestamp_et DATETIME,
    exit_timestamp_et DATETIME,
    exit_type TEXT,
    entry_credit REAL,
    exit_debit REAL,
    gross_pl REAL,
    fees REAL,
    net_pl REAL,
    ror REAL,
    max_adverse_premium REAL,
    max_favorable_premium REAL,
    spx_entry REAL,
    spx_exit REAL,
    spx_close REAL,
    stopped BOOLEAN,
    expired_worthless BOOLEAN,
    notes TEXT
);
```

---

## 16. Cross-reference procedure

### Step 1: Normalize all timestamps

- Parse SMS local time.
- Convert to ET.
- Confirm conversion using close-result alerts. If close-result alerts arrive around 15:05-15:15 local, that likely corresponds to 16:05-16:15 ET after market close.

### Step 2: Parse all explicit zones from SMS

Target patterns:

```regex
(?i)(5|10|15|30)\s*min(?:ute)?\s+(supply|demand).*?@\s*([0-9]{3,5}(?:\.\d+)?)\s*(?:-|to)\s*([0-9]{3,5}(?:\.\d+)?)
```

Also handle variants:

```text
15 min demand from today @ 9:45 is 3568 - 3578
Supply is 10/7, 11:30 @ 3664-3669
5 min demand from today 12:15 @ 3620-3628
The 5 min Supply zone is from 10/5 @ 3:15 pm at 3800 - 3806
```

Fields to extract:

```text
timeframe, zone_type, source_date, source_time, zone_low, zone_high
```

### Step 3: Parse all trade candidates

Target structures:

```text
Iron Condor / IC
Bull Put Spread / BPS
Bear Call Spread / BCS
Sell 3680/3685 & 3755/3760
Buy 3855 & Sell 3860 Puts
Credit $0.35 to $0.40
PoP 64%
Aggressive / Conservative / Safer / Alternative
```

### Step 4: Build market bars

- Load 1-minute SPX data.
- Resample into 5m, 10m, 15m, 30m.
- Use no look-ahead beyond each alert timestamp.

### Step 5: Generate candidate zones

For every timeframe, every day, and every alert time:

1. Identify bases.
2. Confirm departures.
3. Create zones.
4. Track freshness, retests, broken status, and role reversal.
5. Keep active zones above and below current SPX.

### Step 6: Match computed zones to text zones

For each explicit SMS zone mention, compare against computed zones using:

```text
same timeframe
same zone type
zone source timestamp within ± one candle interval
zone low/high within ±1-3 SPX points
zone overlap >= 60%-80%
```

Store match score. Use this process to calibrate the zone-detection algorithm.

### Step 7: Match zones to strikes

For each extracted trade candidate:

- If BPS, identify demand zone that explains the put short strike.
- If BCS, identify supply zone that explains the call short strike.
- If IC, identify both.

Expected mapping:

```text
BPS short_put ≈ floor_to_5(demand_zone_low)
BCS short_call ≈ ceil_to_5(supply_zone_high)
```

Allow a tolerance:

```text
0 to 10 points outside zone boundary
```

### Step 8: Simulate option entry and exit

Using option quotes:

- Entry at alert timestamp + execution delay assumption.
- Test fill at mid, bid/ask conservative, and slippage-adjusted models.
- Monitor total spread/condor debit over time.
- Trigger stops per defined stop models.
- Trigger $0.05 close rule.
- If neither occurs, settle at expiration using SPX close.

Execution-delay test assumptions:

```text
0 seconds
15 seconds
30 seconds
60 seconds
120 seconds
```

Fill assumptions:

```text
mid
mid less 0.05 credit slippage
natural bid/ask conservative
actual alert credit if option quote validates it
```

---

## 17. Hypotheses to test

### Hypothesis 1: Zones explain strike placement

Expected result:

> Most short strikes should sit just outside a recently identified 5-minute or 15-minute supply/demand zone.

Metric:

```text
% of alerts where computed zone explains short strike within 0-10 SPX points
```

### Hypothesis 2: 2:00 PM ET is central to performance

Expected result:

> Alerts cluster around 2:00 PM ET, and trades outside that window may behave differently.

Metric:

```text
win rate / P&L / stop rate by entry-time bucket
```

### Hypothesis 3: Minimum credit filter prevents low-quality trades

Expected result:

> Trades with credit below $0.30-$0.35 underperform or have unattractive risk/reward.

Metric:

```text
P&L and stop rate by credit bucket
```

### Hypothesis 4: Aggressive variants degrade risk-adjusted performance

Expected result:

> Aggressive trades collect more credit but have higher stop rate and worse tail behavior.

Metric:

```text
profit factor, max drawdown, average loss, stop frequency by aggression label
```

### Hypothesis 5: Stops create path-dependency drag

Expected result:

> Some stopped trades would have expired worthless if held, but not stopping may expose catastrophic loss days.

Metric:

```text
compare 2x net-loss stop, 3x debit stop, $0.05 profit exit, hold-to-expiration
```

### Hypothesis 6: Fresh zones outperform tested zones

Expected result:

> Fresh zones may perform better, but tested zones are still sometimes used.

Metric:

```text
outcome by freshness_state and touch_count
```

### Hypothesis 7: 15-minute zones are better filters than 5-minute zones

Expected result:

> 15-minute zones may be cleaner but less frequent; 5-minute zones may be more practical for 0DTE premium.

Metric:

```text
match rate and trade performance by timeframe
```

### Hypothesis 8: VIX/delta layer improves strike selection

Public material says VIX and delta targeting are used; SMS does not expose this. Test whether adding VIX and short-strike delta improves the model.

Metric:

```text
performance by VIX regime and short-strike delta bucket
```

---

## 18. Initial model specification for the AI/database system

### Model version 0.1: SMS-faithful reconstruction

```yaml
strategy_name: spx_cash_flow_reconstruction_v0_1
underlying: SPX
option_product: SPXW PM-settled 0DTE
bar_source: SPX cash index
entry_window_et: 13:45-14:30
primary_timeframes: [5, 15]
rare_timeframes: [10, 30]
spread_width: 5
trade_types: [BPS, BCS, IC]
min_credit: 0.35
profit_exit_debit: 0.05
stop_model: net_loss_2x_credit   # equivalent to closing debit = 3x credit
position_sizing: 1 contract for research; later test 5% account cap
fill_model: mid with slippage sensitivity
```

### Zone scoring version 0.1

```yaml
zone_score_components:
  departure_strength: 30
  freshness: 25
  proximity_to_spot: 20
  timeframe_weight: 10
  trend_alignment: 5
  role_reversal_bonus: 5
  premium_viability: 5
```

### Trade qualification version 0.1

```yaml
qualify_bps:
  current_spx_above_demand: true
  short_put_at_or_below_demand_low: true
  spread_credit >= min_credit: true

qualify_bcs:
  current_spx_below_supply: true
  short_call_at_or_above_supply_high: true
  spread_credit >= min_credit: true

qualify_ic:
  valid_demand_below_spot: true
  valid_supply_above_spot: true
  total_credit >= min_credit: true
```

---

## 19. Validation metrics

The system should produce these outputs for every research run:

```text
1. Zone match rate against explicit SMS zones
2. Strike match rate against SMS trade alerts
3. Credit match rate against SMS stated credit
4. Outcome match rate against SMS reported outcomes
5. Outcome match rate against Excel rows where row-level data exists
6. Performance by year/month
7. Performance by trade type
8. Performance by aggression label
9. Performance by timeframe source
10. Performance by fresh/tested/broken/role-reversal zone state
11. Performance by entry-time bucket
12. Performance by VIX regime
13. Performance by short-strike delta
14. Stop vs hold-to-expiration comparison
15. Slippage sensitivity
16. Execution-delay sensitivity
17. Max drawdown and consecutive-loss statistics
18. Tail-loss days and event-day flags
```

---

## 20. Important open questions

1. **Exact zone construction:** Does he use full wick range, body range, distal/proximal hybrid, or manual drawing discretion?
2. **Exact base length:** How many candles are allowed in the base?
3. **Exact departure threshold:** How much move away from base is required?
4. **Zone freshness:** Does he prefer fresh zones, first retests, role reversals, or any zone with enough premium?
5. **Timeframe priority:** Does he prefer 15-minute over 5-minute, or simply whichever zone is nearest/usable?
6. **VIX usage:** Public material says VIX matters, but SMS does not show VIX. Does he use VIX to set expected move, minimum distance, or strike delta?
7. **Delta targeting:** Public material says delta targeting matters. Need option-chain Greeks to infer target delta ranges.
8. **Stop definition:** Does “2x stop-loss” mean debit = 2x credit or net loss = 2x credit? SMS/Excel evidence suggests the latter may be true.
9. **Iron condor stop basis:** Is the stop based on total IC debit, tested side debit, or individual side premium?
10. **Fill assumptions:** Are alerts based on mid, natural bid, or platform theoretical price?
11. **Aggressive/conservative selection:** Which version is the official trade when several are sent?
12. **No-trade rules:** Besides low premium and Fed/high volatility, what other event filters exist?
13. **Holding rule:** When does he prefer closing at $0.05 vs holding into expiration?

---

## 21. Suggested next research steps

### Phase 1: Complete source ingestion

- Import SMS XML and parsed CSVs.
- Import September 2022 xlsx row-level tracker.
- Manually key screenshot-only 2022 and 2023 monthly summaries.
- Scrape or manually capture public LinkedIn post text where accessible.
- Pull transcripts from relevant YouTube videos.

### Phase 2: Build explicit zone parser

- Extract every SMS zone mention.
- Store source timeframe, source date/time, and price range.
- Use this as the calibration set.

### Phase 3: Build market bar pipeline

- Load 1-minute SPX bars.
- Resample into 5/10/15/30-minute bars.
- Validate bars against known SPX closes in the SMS text.

### Phase 4: Build zone-generation engine

- Implement DBR/RBR/RBD/DBD detection.
- Implement freshness/touch/broken/role-reversal states.
- Score zones.
- Tune parameters to match known SMS zones.

### Phase 5: Build option simulator

- Load SPXW 0DTE quotes.
- Reprice spread/condor at entry, throughout trade, $0.05 exit, stop, and expiration.
- Run stop/hold variants.

### Phase 6: Compare official vs reconstructed

- Compare against SMS candidates.
- Compare against Excel tracker where available.
- Identify drift between 2022, 2023, 2024.

### Phase 7: Improve or redesign strategy

Once the original method is recreated, test enhancements:

- VIX regime filters
- Trend/day structure filter
- Opening range filter
- Breadth filter if available
- Avoid Fed/CPI/high event days
- Better stop logic
- Dynamic width/credit filter
- Conservative-only version
- First-touch-only zones
- 15-minute-only version
- Profit target vs hold-to-expiration

---

## 22. Practical implementation notes

### Timezone handling

Store all market timestamps in ET. Preserve original SMS local time separately.

```python
timestamp_et = timestamp_local + timedelta(hours=1)  # provisional for Central-to-Eastern
```

Validate this by matching “SPX closed at ...” SMS messages to known SPX daily closes and message times.

### Strike parsing conventions

For SPX 5-wide spreads:

```text
BPS 3600/3605 = long 3600 put + short 3605 put
BCS 3670/3675 = short 3670 call + long 3675 call
IC 3580/3585 & 3670/3675 = BPS put side + BCS call side
```

### Margin/risk calculation

For a 5-wide vertical:

```text
margin = (5.00 - credit) * 100
profit_if_win = credit * 100
ROR = profit_if_win / margin
```

Examples from tracker:

```text
$0.50 credit -> $450 margin -> $50 profit -> 11.11% ROR
$0.75 credit -> $425 margin -> $75 profit -> 17.65% ROR
```

### Duplicate handling

The SMS service sometimes sent duplicate messages seconds apart. Use normalized text + date + minute-level timestamp to de-duplicate, but preserve raw messages for audit.

### Row-level vs day-level handling

Use three layers:

1. Raw message layer
2. Trade candidate layer
3. Official/reconstructed trade layer

Do not collapse rows too early.

---

## 23. Bottom-line current conclusion

The most likely strategy is:

> A discretionary 0DTE SPXW iron-condor/credit-spread system that enters around 2:00 PM ET, identifies active SPX supply and demand zones mostly from 5-minute and 15-minute candles, places 5-wide short strikes just outside those zones, requires roughly $0.30-$0.35+ credit, closes cheap winners near $0.05 or lets them expire, and uses a premium-based stop that may correspond to a net loss of roughly 2x the original credit.

The key to recreating it is not the monthly performance tracker. The key is to use the SMS text as a calibration set because the texts often contain:

```text
- exact alert time
- exact source timeframe
- exact supply/demand zone date/time
- exact zone range
- exact trade structure
- exact strikes
- credit range
- stated PoP
- close/hold/stop notes
- end-of-day SPX close and outcome note
```

Once these are matched against real SPX 1-minute bars and SPXW option quotes, the database can determine whether the method is reproducible, whether the reported logic survives realistic execution, and which elements actually create or destroy edge.

---

## 24. Files to pass to the database system now

Recommended immediate import order:

1. `/mnt/data/sms-20260630152134.xml`
2. `/mnt/data/spx_sms_clean_messages.csv`
3. `/mnt/data/spx_trade_candidates_extracted.csv`
4. `/mnt/data/spx_daily_summary.csv`
5. `/mnt/data/SPX Cash Flow Trade Tracking September 2022.xlsx`
6. `/mnt/data/SPX Cash Flow Yearly Trade tracking.xlsx`
7. `/mnt/data/Jan 23 results.JPG`
8. `/mnt/data/2022 Results pic.JPG`
9. `/mnt/data/image (4).png`
10. `/mnt/data/image (3).png`

Recommended first database task:

```text
Create extracted_zone_mentions from the SMS messages, then match those zone mentions to historical SPX 5-minute and 15-minute candles using no-lookahead logic.
```

That task will tell us whether the zone algorithm is close. If we cannot recreate his zones, the rest of the strategy reconstruction will be much less reliable.
