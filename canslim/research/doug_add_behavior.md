# Doug's actual add-to-position (pyramiding) behavior

*Read-only extraction from the four APS Trading Journals (weekly holdings snapshots, 2023-06-30 → 2026-06-26, 155 weekly snapshots). No parameter tuning — this describes what he actually did.*

**Method.** A position's share Qty is tracked week-over-week. A net Qty increase (beyond a 0.5%/1-share DRIP-rounding floor) that is **not** a stock split = an ADD. First appearance = initial entry. Positions/lots are keyed by **(company name, entry date)**, not the ticker symbol, because one weekly sheet (12-15-23) has a shifted symbol column; the entry date is stable across an add (verified) so it is the reliable lot fingerprint. Splits are removed (known list + generic round-multiple-qty jump with proportional price drop). Outcomes come from the journals' own Closed-Trades tables.

## Headline numbers

- **Adds are rare.** 34 of 136 position episodes (25.0%) got at least one add. By distinct name: 32/101 (31.7%).
- **When he adds, he adds EARLY and CHEAP.** Median price-progress from initial entry at the moment of the add = **2.1%**; median days-since-entry = **14 days**. He is not a late continuation-pyramider.
- **Adds are SMALLER than the initial** (decreasing pyramid, loosely). Median add = **59.6%** of the initial position's dollars.
- **Concentration is modest.** Largest **equity** position ever built = **$127,741** market value (the $255k BIL 'position' is the T-bill cash sleeve, excluded); almost every winner is built in **one add or none**, never a stacked pyramid.
- **Added-to positions have a much higher WIN RATE but a lower MEAN return** (48% win vs 28%; mean 3.1% vs 6.1%). Adding cuts the left tail (median -0.2% vs -6.5%) but he also can't add to the fastest rockets, so mean is dragged by huge single-entry winners. Mixed, not a clean win — see §5.

## 1. Frequency

- Position episodes tracked: **136**. With ≥1 add: **34** (25.0%).
- Number-of-adds per position:

| adds | positions | share |
|---|---|---|
| 0 | 102 | 75.0% |
| 1 | 33 | 24.3% |
| 2 | 1 | 0.7% |
| 3+ | 0 | 0.0% |

Takeaway: pyramiding is the **exception**, not the rule. Only 34 of 136 positions were added to at all, and a **second** add is very rare (1 cases). There are **no** 3+-add stacked pyramids in the sample.

## 2. When he adds (price-progress from initial entry)

Distribution of % price-progress-from-initial-entry at the moment of each add:

| pctile | progress at add |
|---|---|
| min | -15.0% |
| 10th | -8.8% |
| 25th | -3.6% |
| median | 2.1% |
| 75th | 9.6% |
| 90th | 31.8% |
| max | 51.6% |
| mean | 6.3% |

Bucketed:

- Add while **underwater / flat** (< 0%): **37%** of adds
- Add in the **buy-zone (0 to +5%)**: **23%**
- Add on **early continuation (+5 to +15%)**: **26%**
- Add on **later continuation (+15%+)**: **14%**

Days-from-entry at add: median **14d**, 25th **7d**, 75th **14d**, 90th **36d**, mean **25d**. **74%** of adds land within 14 days of entry; **43%** within 7 days.

**Read:** this is **buy-zone pyramiding**, not trend continuation. The bulk of adds happen within ~2 weeks of the initial buy and within a few percent of the entry price — he is topping up a fresh position that is working (or dipping slightly), not chasing a name that has already run 20-50%. The handful of later/higher adds (IBIT +52% @84d, TSSI +42%, RKLB +39%) are the exceptions.

## 3. How big the add is (relative to the initial position)

Add size as a fraction of the **initial** position's dollar size:

| pctile | add $ as % of initial |
|---|---|
| 25th | 48.6% |
| median | 59.6% |
| 75th | 73.5% |
| mean | 62.4% |

- Add **smaller** than the initial (< 100%): **89%** of adds
- Add **roughly equal** (80-120%): **14%**
- Add **bigger** than the initial (> 100%): **11%**

**Read:** the typical add is **~60% of the original position** — a **decreasing pyramid** in the loose sense (most adds are a partial top-up, not a doubling). But it's not a strict half-size ladder: a meaningful minority of adds roughly match or exceed the initial (e.g. KD, PLTR twice), so 'add about half-to-full of the starter' is the honest characterization, not 'always smaller.'

## 4. Concentration — how big he lets a winner get

*(BIL / T-bill ETF excluded — its $255k peak is the cash-parking sleeve, not a stock bet.)*

- Largest **equity** market value ever reached (post-build): **$127,741**.
- Largest **added-to** equity position ever reached: **$100,967**.
- Typical **fresh** position size (median initial cost, closed trades): **$52,943**.
- So even a fully-built winner tops out around **~2.4× a starter position** in dollar terms — and most of that is price appreciation, not stacked adds.
- Book context: sub-total + cash ran ~\$450-520k over the window, and fresh positions are sized ~\$40-75k (≈8-13% of book each). A built winner reaching the \$85-90k area is ≈15-18% of book at its peak. He does **not** let a single name balloon to 25%+.

Top positions by peak value:

| sym | name | adds | init shares | peak shares | peak value |
|---|---|---|---|---|---|
| STRL | Sterling Infrastruct | 0 | 145 | 145 | $127,741 |
| GEV | GE Vernova | 0 | 127 | 127 | $127,726 |
| NVDA | Nvidia | 0 | 941 | 941 | $124,055 |
| VKTX | Viking Therap | 0 | 2235 | 2235 | $121,589 |
| MSTR | Microstrategy | 0 | 341 | 341 | $116,237 |
| APP | AppLovin | 0 | 148 | 148 | $101,268 |
| RKLB | Rocket Lab | 1 | 915 | 1524 | $100,967 |
| OKLO | Oklo | 0 | 682 | 682 | $100,371 |
| CRDO | Credo Tech Group | 1 | 344 | 584 | $99,100 |
| RKLB | Rocket Lab | 0 | 1524 | 1524 | $97,813 |
| ERJ | Embraer ADR | 0 | 2475 | 2475 | $96,506 |
| OKLO | Oklo | 0 | 682 | 682 | $92,234 |

## 5. Does adding help? (realized outcomes)

Linking each closed trade to whether that lot was added to:

| cohort | n | win rate | mean return | median return |
|---|---|---|---|---|
| added-to | 31 | 48% | 3.1% | -0.2% |
| single-entry | 89 | 28% | 6.1% | -6.5% |
| all closed | 120 | 33% | 5.3% | -5.6% |

**Read (mixed, be honest):** added-to lots win **much more often** (48% vs 28%) and have a far better **median** (-0.2% vs -6.5%) — adding tightens the distribution and cuts the losing tail. **But the MEAN is lower** (3.1% vs 6.1%): the biggest single-entry winners were fast, gapping names he never got the chance to top up in the buy-zone, so the huge right-tail sits mostly in the single-entry cohort. So adding looks like a **consistency / drawdown-control** behavior, not a return-maximizer.

> **Causality caveat (important, do not over-read):** this is *selection*, not proof that adding *causes* better outcomes. Doug adds to a name **because it is already working** at the add moment, so added-to lots are pre-filtered. The comparison mostly reflects his *entry timing / selection*, not the marginal value of the extra shares. N is small (31 added-to vs 89 single-entry closed lots) and the window is a **bull-heavy 3-year sample** (2023-2026), so absolute win rates are inflated versus a full cycle. Treat the *direction* (adds → higher win rate, lower variance) as the signal, not the point estimates.

## Caveats

- **Stock splits.** Known/generic splits are stripped, but a split during a week Doug also added would be hard to separate; none obviously affected the 35 adds after review.
- **Partial sells muddy net-qty.** If he sold part and re-added within one weekly gap, the net change hides it; weekly (not daily) granularity means intra-week round-trips are invisible.
- **One corrupted sheet (12-15-23)** has a shifted symbol column; handled by keying on (name, entry-date). Cosmetic name spelling variants (AppLovin/Applovin) were normalized.
- **Initial price proxy.** 'Initial price' = the position's price at its first weekly appearance. Names bought and first-seen the same week (most) are exact; a name bought days before its first snapshot has a slightly stale entry price → progress-at-add is a mild underestimate of true progress.
- **Bull-heavy sample.** 2023-2026 was mostly an uptrend; add frequency, size, and the outcome edge would likely look different through a sustained correction.
- **Small N.** 35 adds / 33 names is enough to describe tendencies, not to fit thresholds. Use these as **anchors**, not calibrated parameters (rule #1).

## Anchor for the add-rule design

1. **Rarity:** default to *not* adding; only ~25% of positions earned an add.
2. **Trigger zone:** add EARLY — within ~2 weeks of entry and within roughly -3.6%..9.6% of the entry price (buy-zone top-up), not a +20-50% continuation add.
3. **Size:** add ~half-to-full of the starter (median ~60% of initial $), not a double-down.
4. **Cap:** at most one add for almost all names (two is already the ceiling he used); let a built equity winner reach the ~\$127,741 area (~15-20% of book) at most.

5. **Expectation-setting:** in this sample adding raised the **win rate and median** and cut losers, but did **not** raise mean return (the biggest winners were un-addable fast movers). Design adds as a **consistency / risk-control** rule, not a return-booster.