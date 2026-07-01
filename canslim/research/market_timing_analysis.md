# APS CAN SLIM — Market-Timing / Allocation Analysis, 2018 → 2026

**Source:** the two "APS - Weekly review and trading plan" workbooks (weekly sheets, week-ending
dates). No external market data was pulled — every index level, weekly return, and exposure figure
is read out of the sheets themselves.

- Old workbook: 210 weekly sheets, **Nov 2018 → Dec 2022** (evolving format).
- New workbook: 130 weekly sheets, **Dec 2023 → Jun 2026** (newer format).
- **2023 is intentionally missing** — the advisor mostly did not trade between the two books. It is
  represented as a gap, never interpolated. The weekly-return chain is broken across any link longer
  than 21 days, so the dead-zone can't manufacture a phantom return.

Combined machine-readable output: **`market_timing_2018_2026.csv`** (340 weekly rows).

---

## 1. What was parsed, and how (by label, not cell position)

Cell positions drift across the format evolution, so every field is located by its text label:

| Field | Label anchored on | Coverage |
|---|---|---|
| Week-ending date | sheet name (canonical, unique) + `Week of:` | 340/340 |
| Recommended alloc band | `Recommended alloc:` → e.g. `60-80%` | 129/340 (new book only) |
| Market direction/state | `Market Direction:` → e.g. "Market in correction 12/17" | 211/340 (old book) |
| Cash weight | `Cash/other` or `Cash` → adjacent % | 319/340 |
| Positions held | weighted rows under cash, else numbered `Current Positions:` tickers | 340/340 |
| NASDAQ / S&P close, weekly %, YTD % | `NASDAQ:` / `S&P:` rows; Friday = last non-"closed" daily; weekly = Fri ÷ prior-Fri − 1; YTD from the prior-year-end base cell where present | 340 / 258 |

The two formats are complementary: the **old book** describes exposure qualitatively (`Market
Direction:` text) plus, from 2019 on, a `Cash`/`Cash/other` field; the **new book** adds an explicit
`Recommended alloc:` band and a per-position weighted table.

### Exposure proxy (`invested_pct`)
- **When a cash field exists (319/340 weeks):** `invested_pct = 1 − cash`. Authoritative.
- **When it doesn't (21 early weeks, Nov 2018 – Oct 2019):** inferred from market-direction text +
  position count. Zero positions ⇒ 0% invested; otherwise `min(1, n_positions × 0.096)`, where 0.096
  is the median invested-per-position calibrated from the 189 later weeks that have both fields.
  Flagged in the CSV as `invested_source = inferred_dir+pos`.

### Data quality by era
| Period | Quality | Note |
|---|---|---|
| Nov 2018 – Oct 2019 | **Sparse** | 21 weeks inferred; no explicit cash field; DJI shown, not S&P |
| 2020 – 2022 | **Rich** | explicit cash every week; S&P present |
| 2023 | **Missing** | advisor idle; gap preserved |
| 2024 – Jun 2026 | **Richest** | explicit alloc band + cash + weighted book every week |

S&P weekly returns only exist for 258 weeks (early 2018-2019 sheets tracked DJI instead of S&P), so
the S&P decomposition covers ~5 years vs ~6.5 for NASDAQ.

---

## 2. Timing decomposition — the key analysis

**Method.** This isolates the *allocation dial* from *stock selection*. Take the invested fraction he
set this weekend and apply it to **next** week's index return (exposure set now governs next week).
Compound over the full span. Compare to a 100%-always-invested benchmark. NASDAQ is used as the asset
proxy — **so this measures his timing, not the CAN SLIM stocks he actually held.**

### Overall (full span)

| | Total return | Annualized | **Max drawdown** | End equity |
|---|---|---|---|---|
| **NASDAQ buy & hold** | +146.4% | +14.9% | **−36.9%** | 2.46× |
| **NASDAQ timed (his exposure)** | +96.6% | +11.0% | **−11.8%** | 1.97× |
| **S&P buy & hold** | +95.0% | +14.4% | **−24.8%** | 1.95× |
| **S&P timed** | +49.7% | +8.5% | **−6.3%** | 1.50× |

Average exposure over the whole span was **~48%** (median 64%; 80 of 340 weeks at ~0% invested, 88 at
≥80%).

**Read:** the dial gave up roughly a third of the raw NASDAQ return but **cut max drawdown by ~68%**
(−36.9% → −11.8%). On the S&P the drawdown cut is even sharper (−24.8% → −6.3%). Holding ~half the
market's exposure captured **two-thirds of its return** — a clean risk-reduction trade, not a
return-enhancement one. On a risk-adjusted basis (return per unit of drawdown) the timed path is far
better: NASDAQ 8.2 vs 4.0, S&P 7.9 vs 3.8.

### Per calendar year — NASDAQ (BH = buy & hold, TM = timed)

| Year | Weeks | BH return | BH maxDD | TM return | TM maxDD | Avg exposure |
|---|---|---|---|---|---|---|
| 2018 (Nov–Dec) | 4 | −10.2% | −13.6% | −2.9% | −2.9% | 19% |
| 2019 | 53 | +36.2% | −8.7% | +16.0% | −6.4% | 67% |
| 2020 | 49 | +42.7% | −29.3% | +31.0% | −9.8% | 66% |
| 2021 | 51 | +23.6% | −8.3% | **−6.0%** | −9.4% | 41% |
| **2022** | 52 | **−33.1%** | **−34.0%** | **+0.0%** | **0.0%** | **0%** |
| 2024 | 52 | +31.4% | −9.3% | +17.6% | −9.4% | 76% |
| 2025 | 52 | +19.6% | −22.2% | **+16.8%** | **−5.0%** | 55% |
| 2026 (thru Jun) | 25 | +8.6% | −10.4% | +3.1% | −5.4% | 62% |

Two years earn their keep spectacularly:
- **2022:** flat (0.0%) vs NASDAQ −33% — he sat in 100% cash the entire year.
- **2020 & 2025:** roughly matched or nearly matched the index return while taking a fraction of the
  drawdown (2020: −9.8% vs −29.3%; 2025: −5.0% vs −22.2%).

One year the dial hurt materially: **2021**, where he averaged only 41% invested through a +24% NASDAQ
year and the timed path went slightly negative (−6%). That is the cost of a defensive stance in a
grinding bull — the same caution that saved 2022 clipped 2021.

---

## 3. The four bear episodes — did cash-raising lead, coincide, or lag?

| Episode | Exposure start → min → end | Cash raised | NASDAQ over window (peak-to-trough) | Timing |
|---|---|---|---|---|
| **Q4 2018** (11/30/18–1/11/19) | 38% → 0% → 10% | +38 pts | −4.9% (−13.6%) | **Coincident** |
| **2022 full year** | 0% → 0% → 0% | already flat | −29.9% (−34.0%) | **Led** (already out) |
| **Feb–Apr 2025** | 64% → 5% → 23% | +59 pts | −8.4% (−22.2%) | **Led/coincident** |
| **Mar–Apr 2026** | 36% → 28% → 60% | +8 pts | +9.0% (−16.6%) | **Reacted, then re-entered well** |

- **Q4 2018:** cut from 38% to 0% over three weeks *as* the decline unfolded (was already 0% before
  the worst week, −8.4% on 12/21). Coincident-to-slightly-leading.
- **2022:** the cleanest win — he entered the year already 100% cash (having exited in late 2021) and
  never re-engaged through a −34% peak-to-trough. Every one of the year's 52 weeks reads 0% invested.
- **Feb–Apr 2025:** the strongest *leading* behavior. Exposure fell 64% → 12% over four weeks
  (late-Feb) **before** the −10% crash week of 4/4/25. He then rode the bottom near 5% and began
  re-adding into the April rebound (+7.3%, +6.7% weeks).
- **Mar–Apr 2026:** more reactive. He trimmed only modestly (36% → 28%) into the drawdown, then
  ramped aggressively 28% → 82% right as the market ripped +6.8%/+4.7% off the low — good re-entry,
  weak exit.

Pattern: **the exit is usually coincident, the re-entry is where the skill (and the whipsaw risk)
shows.** 2025 is his best-timed episode; 2018 and 2026 show the reactive tendency.

---

## 4. Reactivity test — anticipation vs whipsaw

Correlation of `invested_pct` with forward and trailing NASDAQ returns (n ≈ 314–332 weeks):

| | 4-week | 13-week |
|---|---|---|
| **Forward** (does exposure predict what's coming?) | **+0.058** | **+0.127** |
| **Trailing** (does exposure just echo what just happened?) | **+0.471** | **+0.644** |

**Interpretation — the honest read.** Exposure correlates **weakly-positively with the future**
(+0.06 / +0.13) but **strongly with the recent past** (+0.47 / +0.64). This is the signature of a
**reactive, trend-following dial, not a predictive one.** He raises cash after weakness and adds after
strength. The small positive forward correlation (particularly the +0.13 at 13 weeks) says the
reactivity isn't pure noise — trend persistence gives it a slight genuine edge — but the effect is
modest. The dial is not anticipating tops and bottoms; it is following the trend and getting out of
the way of the biggest declines. That is exactly why it cuts drawdown far more than it adds return.

---

## 5. Stated results, pulled from the sheets (verbatim)

Quoted from the `Market Comments:` cells (bracketed `< >` = a loss in his notation):

- **2018:** "I am now +.64% with the NASDAQ <8.26%> for the year." (100% cash into year-end.)
- **2019:** exited the year ~35% cash; "accounts that were fully invested at the start of the quarter
  are up between 5-6% with the S&P up 8.21%." (Note: strong participation year.)
- **2020:** consistently near index week-to-week (e.g. "+.19% this week vs S&P <.17%>, NASDAQ +.38%").
- **2022:** "The NASDAQ closed the year with a loss of <33.10%>. The S&P closed with a loss of
  <19.44%>." — his group ended the year **100% cash**, essentially flat.
- **2024:** "My group was <1.44%> this week, now **+25%ytd**." (vs NASDAQ +31.4%.)
- **2025 (Apr bottom):** "My group was <.47%> this week, now **<7.7%>ytd** … high cash level protected
  us from the carnage" — while NASDAQ was −19.3% YTD.
- **2025 (year-end):** "Our group was <.66%> this week, now **+23.97%ytd**." (vs NASDAQ +22.2%.)

These stated group figures track the timing decomposition's story: near-index in up years, sharply
protected in down phases.

---

## 6. Caveats — read before trusting any number

1. **This measures TIMING, not his book.** NASDAQ/S&P are the asset proxy. His actual CAN SLIM
   holdings behaved differently (usually higher-beta growth names). The decomposition answers "what
   did the exposure dial add on top of the index," not "how did his stocks do." His stated group
   results (Section 5) are the only window onto selection, and they're self-reported.
2. **Very few bear episodes.** Four drawdowns (2018, 2022, 2025, 2026) — and 2022 dominates the entire
   drawdown-reduction result. This is a **small sample**; do not over-fit a "he times bears well"
   conclusion to essentially one-and-a-half clean examples (2022 + 2025).
3. **Exposure timing convention** (this weekend → next week) is a modeling choice. Same-week
   application would flatter the numbers (he'd "avoid" the crash week he's reacting to); next-week is
   the honest, causally-clean choice and is what's reported.
4. **21 early weeks are inferred**, not read from a cash field. They sit in the low-weight tail
   (avg 19% exposure in the Q4-2018 window) so they don't drive the headline, but treat 2018–mid-2019
   exposure as approximate.
5. **Survivorship / self-report.** The sheets are the advisor's own record; group-performance figures
   are stated, not independently reconstructed from trades.
6. **2021 is the counter-example.** The same defensiveness that won 2022 lost ~6% of a +24% year.
   The dial is a drawdown-control tool with a real opportunity cost in melt-ups — not free alpha.

---

## Verdict

The allocation dial, on its own and separated from stock picking, is a **risk-reduction engine, not a
return engine.** Over 2018–2026 it captured about two-thirds of the NASDAQ's return while cutting max
drawdown by roughly two-thirds (−37% → −12%; S&P −25% → −6%). Its value is overwhelmingly concentrated
in getting out of the way of the two worst regimes (100% cash through all of 2022; 12% invested into
the April-2025 crash). The reactivity test confirms it works by **following the trend, not
forecasting it** — exposure echoes the recent past far more than it predicts the future — so the edge
is drawdown avoidance plus mild trend-persistence, not market-timing clairvoyance. The honest bull
case is "sleeps better, gives up some upside in melt-ups (2021)"; the honest bear case is that with
only ~1.5 clean bear examples, the drawdown result leans hard on 2022 and shouldn't be treated as a
robust, repeatable timing alpha.
