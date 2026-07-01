# CAN SLIM / IBD System — Replication-Grade Specification

**Purpose:** Enough concrete, quantitative detail that a quant could rebuild William J. O'Neil's CAN SLIM / Investor's Business Daily (IBD) rules in code, for evaluating a real trader who runs this system.

**Primary sources:** O'Neil, *How to Make Money in Stocks* (4th ed.); Investors.com / IBD education; William O'Neil + Co / MarketSmith rating definitions; AAII's codified "CAN SLIM Revised 3rd Edition" screen; TraderLion / MarketSmith pattern write-ups; Lutey academic OPBM interpretation. See **Sources**.

**Source-disagreement note up front:** O'Neil's book states many rules as *ranges* or discretionary judgments ("20% to 50%", "sound base", "healthy market"). Downstream codifiers (AAII, screeners) sharpen those into single thresholds. Where they differ I give O'Neil's original wording **and** the common codified threshold, and label which is which. Anything I could not source I mark **[NOT IN SOURCES]** rather than inventing a number.

---

## 1. The CAN SLIM acronym — letter by letter

### C — Current Quarterly Earnings (and Sales)
- **EPS growth, most recent quarter vs. same quarter a year ago:** O'Neil's book range is **+20% to +50% minimum**; his stated preference is the biggest you can find. The widely codified screen threshold is **≥ 25% YoY** (some codifiers use the two most recent quarters both ≥ 25%). *Book = range; 25% = codified.*
- **Acceleration:** O'Neil wants earnings growth *accelerating*, not just high — recent quarters' growth rate rising vs. prior quarters. Codified as: current-quarter growth ≥ prior-quarter growth.
- **Sales growth:** current quarter **≥ 25% YoY**, OR sales growth accelerating over the last several quarters. (AAII screen: sales growth > 25% or > trailing-twelve-month sales growth.)
- **Margins:** current-quarter pretax/net margin ≥ trailing-twelve-month margin (accelerating profitability).

### A — Annual Earnings Growth
- **Annual EPS growth:** **≥ 25% per year** in each of the last **3 years** (O'Neil: "annual compounded growth rate of 25% to 50%, or more"). Codified: 3-year EPS CAGR ≥ 25%, each year up.
- **ROE:** **≥ 17%** (O'Neil's stated floor; "the great growth companies" run 20–50%).
- Some codifications add: positive annual cash flow ≥ reported EPS (quality check). *This is a screener addition, not verbatim O'Neil.*

### N — New (products, management, price highs)
- Qualitative "N": a new product, service, management, or industry condition driving the earnings surge.
- Quantitative "N": stock buying **as it makes a new price high emerging from a proper base** — O'Neil's paradox: "what seems too high and risky usually goes higher." Buy near new highs, not near lows.
- Screen proxy: price within a small % of its 52-week high (closer to 52-wk high than 52-wk low); relative price change in the top ~15%.

### S — Supply and Demand
- **Small float preferred:** O'Neil's book example favors companies with **≤ 25 million shares** in the (then-current) float as more explosive; the general principle is *smaller float = more volatile/upside*, large-cap = more supply to absorb. *The "25 million" is era-specific book wording; treat the principle (prefer smaller float) as the durable rule.*
- **Demand signals:** management/company **buying back stock** (reducing supply) is a plus; low debt-to-equity is a plus.
- **Demand at breakout:** volume surge on up days (see Buy Rules).
- Minimum price often applied in screens: **≥ $15/share** (avoid cheap/thin names). *Screener convention, not a core O'Neil number.*

### L — Leader or Laggard (Relative Strength)
- Buy the **#1 or #2 leader in a strong industry group**; avoid "sympathy" laggards.
- **RS Rating ≥ 80** minimum; O'Neil's research: winners averaged an RS Rating of **~87 before** their big advance. Preferred **≥ 90** when combined with a chart breakout.
- Rule of thumb: avoid stocks with RS Rating below 70; never buy the cheap laggard because it "hasn't moved yet."

### I — Institutional Sponsorship
- Want **increasing** numbers of institutional owners (funds), quarter over quarter — but not over-owned.
- Screen proxies: **≥ 20 institutional holders** (minimum), holder count rising YoY. O'Neil warns *against* the most widely over-owned names ("avoid the top-50 most-institutionally-held").
- Quality of sponsorship matters (a few top-performing funds owning it > many mediocre ones) — **discretionary**.

### M — Market Direction ("the M is the most important")
- ~**3 of 4 stocks follow the general market**, so trade only when the general market is in a confirmed uptrend. Full mechanics in §7.

**Cross-source disagreement:** Portfolio123's summary of the AAII article says O'Neil "dismisses M as unreliable" — that reflects the *screen's inability to encode M*, not O'Neil's view. O'Neil himself treats M as decisive. For replication, encode M via the Market Pulse rules in §7, not as an afterthought.

---

## 2. IBD / MarketSmith proprietary ratings

| Rating | Scale | What it measures | CAN SLIM buyer wants |
|---|---|---|---|
| **RS Rating** | 1–99 | Price performance vs. all stocks over ~12 months, **recent quarter weighted more heavily** | **≥ 80** (ideally ≥ 90) |
| **EPS Rating** | 1–99 | Earnings growth + stability, past 3 yrs, **two most recent quarters weighted extra** | **≥ 80** (ideally ≥ 90) |
| **Composite Rating** | 1–99 | Blend of EPS Rank, RS Rating, Industry Group RS, SMR, Acc/Dist, and % off 52-wk high | **≥ 90–95** |
| **SMR Rating** | A–E | Sales growth + profit Margin + Return on equity | **A or B** |
| **Acc/Dist Rating** | A–E | Institutional buying vs. selling (price+volume) over ~13 weeks | **A or B** (avoid D/E) |
| **Group RS (Industry Group Rank)** | A–E, and 1–197 group rank | Relative strength of the stock's industry group | Top groups; A/B, top ~40 of 197 groups |

**RS Rating computation (as published):** ranks each stock's trailing price change against the whole database and converts to a **percentile (1–99)**; the **most recent 3 months are weighted more** than the prior 9. **The exact weighting coefficients are proprietary and NOT disclosed** by IBD. A common public approximation: `weighted return = 0.4·(3-mo %chg) + 0.2·(6-mo) + 0.2·(9-mo) + 0.2·(12-mo)`, then percentile-rank across the universe. *This 40/20/20/20 form is a widely-used community approximation, NOT O'Neil's confirmed formula.* An RS of 90 = outperformed 90% of all stocks.

**EPS Rating computation (as published):** combines (1) most-recent-quarter EPS %chg YoY, (2) prior-quarter EPS %chg YoY, (3) 3-to-5-year EPS growth rate, and (4) an earnings-stability factor; each ranked, weighted (recent quarters extra), then percentile-ranked 1–99. Exact weights proprietary.

**Composite Rating (as published):** weights the five sub-ratings above plus % off 52-week high; recent versions weight RS and EPS most. Exact weights proprietary.

**RS Line and the "RS line at new high before breakout" concept:**
- **RS Line = stock price ÷ S&P 500 index**, plotted over time (a ratio line, not the 1–99 rating). Rising line = outperforming the S&P.
- **Bullish signal:** the RS line makes a **new high while the stock's price is still in/finishing its base** (before the price breakout). MarketSmith flags this as a **blue dot** ("RS line new high"). O'Neil/IBD treat an RS line at or near new highs going into a breakout as strong confirmation; an RS line *lagging* (not confirming) is a red flag on an otherwise-valid breakout.

---

## 3. Chart base patterns (geometry)

General rules that apply to all bases:
- A **proper base needs time to build** — minimum **~5 weeks** (flat base) to **7+ weeks** (cup); one-to-three-week "bases" are not valid.
- The base should form **above the rising 200-day (40-week) line**; the 10-week/50-day line ideally rising.
- **Pivot / buy point** for any pattern = the highest price of the most recent resistance in the pattern (handle high, mid-peak, etc.) **+ $0.10** (ten cents), where sources give a precise trigger. IBD often expresses the pivot as "10 cents above" that prior high.
- **Breakout volume** should be **≥ 40–50% above the 50-day average daily volume** (see §4).
- **Buy zone** = pivot up to **+5%** above pivot.

### Cup-with-handle
- **Duration:** minimum **7 weeks**; commonly **7–65 weeks** (typical 3–6 months).
- **Cup depth:** **12–33%** from left rim to bottom (O'Neil: "shouldn't decline more than ~1/3 from the high"); in violent bears up to ~40–50% can still work but is riskier.
- **Shape:** rounded "U" (not a sharp "V"); left and right rims roughly equal height.
- **Handle:** forms in the **upper half of the cup**, within **~15% of the old high**, **above the stock's 10-week line**. Handle depth **8–12%** (up to ~15% in choppy markets) measured from handle peak to handle low. Handle should **drift/slope slightly DOWN** (a handle that wedges *up* along its lows is a defect). **Volume dries up** in the handle (contraction), then expands on breakout.
- **Pivot / buy point:** **handle high + $0.10**.
- **Volume signature:** breakout day volume ≥ +40–50% vs. 50-day average.

### Cup-without-handle
- Same cup geometry and duration; **no handle** — riskier, higher failure/undercut rate.
- **Pivot:** **$0.10 above the left-side (prior) high** of the cup.

### Saucer-with-handle
- Same family as cup-with-handle but **shallower and longer** — a broad, shallow, saucer-shaped base often running **many months** (longer than a typical cup). Depth is smaller; time is longer. Handle rules as above. Pivot = handle high + $0.10.

### Flat base
- **Duration:** minimum **~5 weeks**.
- **Depth:** shallow — corrects **no more than ~15%** top-to-bottom; tight sideways action.
- Often a **second-stage base** that forms right after a prior breakout (stock consolidates a gain).
- **Pivot:** **$0.10 above the base's high** (the top of the sideways range). Breakout on volume ≥ +40–50%.

### Double-bottom ("W")
- **Duration:** minimum **~7 weeks**; prior uptrend ideally ≥ ~30%.
- **Shape:** a "W" where the **second low undercuts the first low** (shakeout of weak holders), then recovers.
- **Depth:** up to ~**40%** peak-to-second-low in strong markets (deeper than a flat base; typically 20–30%).
- **Pivot / buy point:** **middle peak of the "W" + $0.10** (the pivot is the mid-"W" high, NOT the pattern's outer high).

### Ascending base
- **Duration:** ~**9–16 weeks**.
- **Shape:** three separate pullbacks, each making **higher highs and higher lows** (staircase up), pullbacks typically **10–20%** each; forms during an ongoing uptrend, often mid-run.
- **Pivot:** **$0.10 above the high of the third (last) leg** / most recent peak.

### High, tight flag (rare, most powerful)
- **Flagpole:** a run-up of **~100–120% in ~4–8 weeks**.
- **Flag:** **3–5 weeks** of very tight sideways consolidation, pulling back **only ~10–25%** (tight; a deeper pullback disqualifies it).
- **Pivot:** **$0.10 above the flag's high.** Highest-risk / highest-reward; least common; heavy discretion.

### IPO base
- Forms **shortly after** a recent IPO (can begin as few as ~7 trading days post-IPO).
- **Duration:** can be short, often < 5 weeks, up to ~12–16 weeks (optimal 12–16 per some write-ups).
- **Depth:** usually **≤ 20%** in normal markets; up to **~50%** in volatile markets (IPO bases tolerate deeper/looser action than mature bases).
- **Pivot:** **$0.10 above the base's prior high.** Want heavy volume + close near session high on breakout.

### Base-on-base
- A base that forms **on top of a prior base** without the stock making much net progress — usually because the **general market pulled back** during what would have been the advance. Two stacked bases. Treated as a **single continuous base-building period**; buy the breakout of the upper base. *IBD guidance: don't start counting "late-stage" base numbers until earnings/sales are running ≥ 25%.*

### Square / consolidation
- Generic tight rectangular consolidation (box). Not a named O'Neil primary pattern in the book; treated as a flat-base-like sideways range. Pivot = top of the range + $0.10. *Least formally specified — largely discretionary.*

**Base-stage caution (all patterns):** first- and second-stage bases (early) are higher-probability; **late-stage (3rd/4th) bases are failure-prone** ("everyone can see it"). Base counting is partly mechanical (count breakouts) but the "is this stage-3?" judgment is discretionary.

---

## 4. Buy rules

- **Trigger:** price crosses the **pivot** (pattern high + $0.10) intraday, ideally closing in the upper part of the day's range.
- **Buy zone:** from the pivot up to **+5% above the pivot**. Do **not** chase beyond +5% (extended = wait for next base or pullback).
- **Breakout volume requirement:** day's volume **≥ +40–50% above the 50-day average** (IBD standard; ideal breakouts run +100%+). Below-average-volume breakouts are suspect and fail roughly twice as often per IBD.
- **Add-on / pyramiding:** O'Neil pyramids **UP into strength**, never averages down.
  - Classic staging: initial position at the pivot; **add a smaller tranche after ~+2.0–2.5%**, and a final smaller tranche near **+5%**, keeping the whole cost basis inside the +5% zone. A common textbook split is roughly **50% / 30% / 20%** of the intended position. *The exact fractions are practitioner convention; O'Neil's hard rule is "don't add more than ~5% past the pivot."*
  - Never add to a losing position.
- **Buying pullbacks / add points after the initial run:**
  - **10-week (50-day) line pullback:** once a leader is extended, a **pullback to the rising 10-week / 50-day line on light volume, then a bounce on rising volume**, is a valid add-on (secondary buy). Requires the line to be **rising** and the pullback orderly.
  - **21-day EMA** is used similarly for faster/stronger leaders as a shorter-term support-buy reference.

---

## 5. Sell rules

### Defensive (loss-cutting) — the non-negotiable
- **7–8% hard stop:** "**Cut every loss when it is 7% or 8% below your purchase price, with no exceptions.**" Applies to *every* buy, measured from *your* entry (not from the pivot). Rationale: keep losses small so a modest win rate is still profitable; pairs with the 20–25% profit target for a favorable asymmetry (lose 7–8%, win 20–25% ≈ ~3:1). Strong leaders often stop *before* hitting 8% — many practitioners tighten to ~3–5% in choppy tape (discretionary).

### Offensive (profit-taking)
- **20–25% profit rule:** take most gains at **+20% to +25%** from the pivot, because many winners advance ~20–25% then correct/rebuild a base.
- **The 8-week hold rule (exception to the 20–25% rule):** if a stock **gains ≥ 20% within 1–3 weeks** of breaking out of a proper base (in a healthy market), **hold it for at least 8 weeks** — such explosive strength flags a potential huge winner; only then reassess. This is the rule that lets the biggest winners run.

### Sell-signal catalog (technical / late-stage tops)
Any of these can trigger a sell even before +20–25%:
- **Climax / blow-off top:** after a long advance, the stock's move **accelerates**, often gaining **25–50% in 1–3 weeks** — the largest, fastest gains of the whole run. Sell into strength.
- **Exhaustion gap:** a **gap up** after the stock is already well-extended (late in the move) on heavy volume — often the final gasp.
- **Largest one-day / one-week price gain** of the entire advance — frequently marks the top.
- **Break of the 50-day / 10-week line on heavy (above-average) volume**, especially if it fails to reclaim it — major sell/reduce signal. (A break on *light* volume is less severe.)
- **Break of the 200-day line** — deeper structural damage.
- **Upper channel line:** stock pierces **above** the upper line of a properly drawn trend channel (line drawn along the highs) — overextended; sell into it.
- **New price high on LOW/declining volume** — rally lacks institutional support; distribution risk.
- **Stalling / churning:** heavy volume but **little/no price progress** (or a close in the lower range) at new highs — institutions distributing.
- **Largest daily volume of the move with no upside progress** — short-term exhaustion.
- **Living below the 50-day line / repeated distribution** at the general-market level (see §7) — reduce exposure.
- **Late-stage base failure / undercutting the base low after breakout.**

Most defensive market-level selling is driven by **distribution-day counts** (§7).

---

## 6. Moving averages and how they're used

| MA | Primary use |
|---|---|
| **21-day EMA** | Short-term support for *strong* leaders; add-on / hold reference; break below (esp. on volume) = caution for fast movers |
| **50-day SMA (≈10-week SMA)** | The key institutional trend line. Support in an uptrend; **pullback-to-50-day on light volume then bounce** = add point; **break on heavy volume** = major sell signal. IBD uses the **10-week SMA on weekly charts** as the same line. |
| **200-day SMA (≈40-week SMA)** | Long-term trend filter. Proper bases should form **above a rising 200-day**; break below = structural weakness. |

Note: 10-week SMA and 50-day SMA are the **same line** (weekly vs. daily rendering). 40-week ≈ 200-day likewise.

---

## 7. Market timing — "The Big Picture" / Market Pulse

Three states (IBD "Market Pulse"), like a traffic light:

| State | Meaning | Typical exposure guidance |
|---|---|---|
| **Confirmed Uptrend** | A valid **Follow-Through Day** has occurred; indices trending up | Near **fully invested** (leaders only) |
| **Uptrend Under Pressure** | Distribution days accumulating; trend intact but weakening | **Reduce** exposure; no/few new buys; protect gains |
| **Market in Correction** | Uptrend has failed / indices breaking down | **Raise cash toward 0%**; stop buying |

**Follow-Through Day (FTD) — the exact definition (marks a new uptrend):**
1. First, an attempted rally must begin: the market makes a low, then has an up day = "Day 1" of the attempt.
2. Watch for the FTD on the **4th through 7th day** of the attempted rally (occasionally later; before day 4 is too early / suspect).
3. On the FTD, a major index (**NASDAQ Composite or S&P 500**) closes **up a decisive amount — commonly cited as ≥ +1.25% (originally +1%; IBD raised the practical bar to ~1.2–2%)** for the day,
4. **on volume HIGHER than the prior day's** volume.
5. Only **one** of the major indices needs to follow through. A rally attempt can fail if the index undercuts its rally-attempt low before an FTD confirms.
   - *Source disagreement on the % magnitude:* O'Neil's original text says ~+1%; modern IBD practice cites **+1.25% or more** as the working threshold to filter noise. Both appear in sources — the +1.25% figure is the more commonly cited modern number.

**Distribution Day (DD) — counting rules (warns of a top):**
- A **distribution day** = a major index closes **down ≥ 0.2%** (some sources: any meaningful down close) on **volume HIGHER than the prior day** = institutional selling. (The 0.2% floor is IBD's modern refinement to exclude flat days.)
- A DD is also recorded on a **stall day** (index up small but heavy volume, little progress, poor close).
- **Counting window:** count DDs over a **rolling 25 trading sessions (~5 weeks)**.
- **DD expiration:** a DD drops off the count after **25 trading sessions**, OR sooner if the index **rises ≥ 5% above that DD's closing price** (intraday).
- **Thresholds:**
  - **3–4 DDs** in a few weeks = caution building.
  - **5 DDs** within ~25 sessions is the classic trigger to shift toward **Uptrend Under Pressure**.
  - **6+ DDs** (often "**6 in 4–5 weeks**") commonly flips the read toward **Market in Correction** (combined with price breaking key support). Sources vary: some cite 5, some 6, some "5–6." *There is no single hard-coded number O'Neil publishes; ~5–6 is the consensus band, and it's confirmed by price action, not the count alone.*

**Exposure-band mapping (source variation):**
- The **simple ETF-timing version** IBD publishes: **Confirmed Uptrend = 100%**, **Under Pressure = ~50%**, **Correction = 0%**.
- IBD's **"Market Exposure" gauge** for individual-stock investors is more granular, expressed in **~20-percentage-point bands (0–20%, 20–40%, 40–60%, 60–80%, 80–100%)** that IBD moves up/down as conditions evolve. The specific band on any day is an **editorial/discretionary** call within the state, not a formula. *The clean 100/50/0 is the mechanically replicable version; the 20% bands are IBD's judgment overlay.*

---

## 8. Position sizing / portfolio concentration

- **Concentration over diversification** — O'Neil calls broad diversification "a hedge against ignorance." Own a **small number of your best ideas**.
- **Number of names by account size (O'Neil's guidance):**
  - **$5,000–$20,000:** ~**2–3 stocks** max.
  - **$20,000–$200,000:** ~**4–5 stocks**.
  - Larger accounts: still concentrated — roughly **5–6, up to ~10** at the extreme; O'Neil discourages many small positions.
- **Max position size:** not a single published hard %; implied by name-count (e.g., 4–5 names ⇒ ~20–25% each at full investment). O'Neil emphasizes *equal-ish* initial positions then pyramiding the winners.
- **Pyramiding:** add to winners within +5% of pivot (see §4); never to losers.
- **Margin:** O'Neil used margin, but only in confirmed uptrends and only on proven leaders — highly discretionary and risk-amplifying; not a mechanical rule.

---

## 9. Mechanically replicable vs. discretionary

### Mechanically replicable (automatable now)
- **Fundamental screens:** current-quarter EPS ≥ 25% YoY; sales ≥ 25%; 3-yr annual EPS ≥ 25%; ROE ≥ 17%; margin acceleration; institutional-holder count ≥ 20 and rising.
- **Ratings gates (if you have the data feed):** RS Rating ≥ 80/90; EPS Rating ≥ 80; Composite ≥ 90; SMR A/B; Acc/Dist A/B; Group RS top-tier. (RS/EPS *values* are proprietary but purchasable; you can also recompute an RS-percentile proxy yourself.)
- **RS line new-high vs. price** — computable as `price/SPX` and testing for a rolling new high while price < prior high.
- **Buy trigger & zone:** pivot + $0.10; buy-zone cap +5%; breakout volume ≥ +40–50% vs 50-day avg — *once the pivot price is given.*
- **Stops/targets:** 7–8% stop from entry; 20–25% profit; 8-week-hold trigger (≥20% gain within 3 weeks of breakout).
- **MA rules:** price vs. 21-EMA / 50-day / 200-day; "break of 50-day on above-avg volume"; "pullback to rising 50-day on below-avg volume."
- **Market timing:** Follow-Through Day (index up ≥ ~1.25% on higher volume, days 4–7 of a rally attempt); distribution-day count with the 25-session / +5% expiration; state machine (Uptrend / Under Pressure / Correction); the clean 100/50/0 exposure mapping.

### Discretionary / pattern-recognition (hard to fully automate)
- **Identifying and validating the base itself** — is it a *proper* cup/flat/double-bottom, is the shape sound (rounded not V), is the handle drifting down and in the upper half, is volume drying up correctly. (Pattern-recognition tools like MarketSmith approximate this, but base *quality* is judgment.)
- **Where exactly the pivot is** — depends on correctly identifying the pattern's resistance point; the "+$0.10" is mechanical only after the human/model picks the point.
- **Base stage / lateness** (stage 1–2 good, stage 3–4 fragile) and base-on-base interpretation.
- **"N"** — the qualitative new product/management/story judgment.
- **Quality of institutional sponsorship** (which funds, not just how many).
- **Reading tops** — climax vs. normal strength, stalling vs. healthy volume, exhaustion gaps — requires context/judgment even though each signal has a rough numeric form.
- **IBD's granular 20% exposure bands** and the final "is the market really rolling over" call — editorial.
- **Position sizing/margin in practice** — O'Neil scales conviction by market health and leader quality.

**Bottom line for automation:** the *fundamental filter*, the *ratings gates*, the *risk rules (7-8% / 20-25% / 8-week)*, the *MA-break rules*, and the *market-timing engine (FTD + distribution days + state → exposure)* are cleanly codeable. The *base/pivot recognition* is the irreducibly discretionary core — it can be assisted (MarketSmith-style pattern detection) but not fully mechanized without accepting quality tradeoffs.

---

## Numbers I could NOT source (flagged, not invented)
- **Exact proprietary weighting coefficients** for RS Rating, EPS Rating, and Composite Rating — IBD does not publish them; the 40/20/20/20 RS approximation is a community estimate only.
- A **single canonical distribution-day count** that flips Uptrend→Correction — sources give a band (~5–6 in ~4–5 weeks) plus price confirmation, not one fixed integer.
- A **single published max-position-size %** — inferred from name-count, not stated as a rule.
- The **precise FTD % gain** is disputed across sources (original ~+1% vs. modern ~+1.25%+); both are documented above.
- **"Square/consolidation"** as a formally-specified O'Neil pattern — it's treated as a generic box/flat-base analog; not rigorously defined in the primary sources found.

---

## Sources
- O'Neil CAN SLIM thresholds & overview (StockAlarm): https://pro.stockalarm.io/blog/canslim-growth-stocks-system
- Portfolio123 — Stock-Picker's Guide to CAN SLIM (codified screen criteria): https://blog.portfolio123.com/a-stock-pickers-guide-to-william-oneils-can-slim-system/
- AAII — CAN SLIM approach article: https://www.aaii.com/journal/article/william-oneil-can-slim-approach-to-selecting-growth-stocks
- AAII — O'Neil CAN SLIM Revised 3rd Edition screen: https://www.aaii.com/stocks/screens/78
- AAII — Cup-with-Handle article: https://www.aaii.com/journal/article/predicting-short-term-trends-the-cup-with-handle-pattern
- William O'Neil + Co — Proprietary Ratings & Rankings: https://www.williamoneil.com/proprietary-ratings-and-rankings/
- IBD SmartSelect ratings (Quizlet flashcards, verbatim IBD definitions): https://quizlet.com/83588114/the-ibd-smartselect-corporate-ratings-flash-cards/
- IBD SmartSelect user manual (PDF): http://gimonline.net/Module_5_User.pdf
- Macro Ops — CAN SLIM explained: https://macro-ops.com/william-oneils-can-slim-trading-strategy-explained/
- TraderLion — Flat Base: https://traderlion.com/technical-analysis/the-flat-base-pattern/
- TraderLion — Double Bottom: https://traderlion.com/technical-analysis/the-double-bottom-pattern/
- TraderLion — Cup and Handle: https://traderlion.com/technical-analysis/cup-and-handle-pattern/
- TraderLion — High Tight Flag: https://traderlion.com/technical-analysis/high-tight-flag-pattern/
- TraderLion — Follow Through Day: https://traderlion.com/trading-strategies/follow-through-day/
- Deepvue — Relative Strength (RS Rating/RS line): https://deepvue.com/indicators/relative-strength-stocks/
- MarketSmith HK — "RS Line Breaks To New Highs" idea list (blue dot): https://www.marketsmith.hk/v2/blog/william-j-oneil-idea-listrs-line-breaks-to-new-highs?lang=en
- IBD Market Pulse timing guide: https://ibdstock.com/use-ibd-market-pulse-timing/
- IBD stock-market-correction guide: https://ibdstock.com/read-stock-market-correction/
- CrystalBull — IBD Market Pulse review (exposure %): https://www.crystalbull.com/IBD-market-pulse-review/
- MarketSmith India (Medium) — "Selling Right: How O'Neil Mastered Selling": https://medium.com/@socialmedia_96459/selling-right-how-oneil-mastered-selling-4d5b7770119e
- Nasdaq — 20%-25% profit-taking: https://www.nasdaq.com/articles/how-build-long-term-profits-stocks-take-many-gains-20-25-2017-10-25
- Trading With Rayner — 23 O'Neil trading rules: https://www.tradingwithrayner.com/23-trading-rules-by-william-j-oneil/
- Lutey, M. — "OPBM: An Interpretation of the CAN SLIM Investment Strategy" (academic, quantitative): http://www.na-businesspress.com/JAF/LuteyM_LWeb14_5_.pdf
- Grokipedia — CAN SLIM: https://grokipedia.com/page/CAN_SLIM
- Chris Perruna — How to calculate a stock's pivot point: https://www.chrisperruna.com/2007/01/22/how-to-calculate-a-stocks-pivot-point/
