# S9 Options-Data Vendor Pricing — Closing the Tail and Staying Current

**Date:** 2026-09-05
**Scope:** published pricing only. **No vendor was contacted.** No form filled, no quote requested, no account created, no trial started, no purchase made.
**Method:** every figure below came from a page fetched on 2026-09-05, cited inline. Cboe DataShop figures were read off the live public price configurator on `datashop.cboe.com` by selecting symbols/dates/options — nothing was added to a cart and no details were entered.
**Related:** `docs/S9_SPEC.md` (§11 pull layers, §12.1 schema, §13 filters), `docs/S9_DATA_FEASIBILITY_IBKR_2026-09-05.md` (why IBKR cannot supply this).

---

## 0. Scope correction — read this first

This document was originally commissioned to price a **five-year bulk purchase** of US single-name equity options history (Sept 2021 – Sept 2026, ~30 mega-cap tickers, full field set). **That is no longer the question.**

A ThetaData-shaped full-chain EOD archive already exists on disk at `C:\TradingDesk-Local\warehouse\raw\options\`. I verified it directly:

- **141 roots** present, including every name on the S9 manifest (AAPL, MSFT, NVDA, AMZN, GOOGL, META, AVGO, TSLA, BRKB, LLY, JPM, V, XOM, UNH, MA, WMT, ORCL, NFLX, COST, HD).
- Daily parquet, one file per trading day. `AAPL/` holds **2,235 files**, first `20180101.parquet`, last **`20260724.parquet`**.
- Field coverage and LEAP depth were verified by the coordinating session, not re-verified here: bid, ask, bid_size, ask_size, delta, theta, vega, rho, gamma, implied_vol, underlying_price, open_interest, volume; LEAPS out to 660–1,005 DTE; 23–395 calls per day in the 0.70–0.98 delta band at 330–760 DTE; open interest 95–100% populated.

So the five-year history is **in hand**. What is actually missing:

| Gap | What it is | Blocks what |
|---|---|---|
| **1. The tail** | 2026-07-25 → today. ThetaData lapsed 2026-07-25 and the archive stops dead at 2026-07-24. **~30 trading days** (2026-07-27 … 2026-09-04). | Any test whose window runs to the present |
| **2. Staying current** | No live feed at all. The gap grows one trading day per day. | Everything, permanently |
| **3. Intraday** | Archive is EOD only, one row per contract per day, and its `timestamp` is each contract's own last-quote time — not a uniform snapshot. Illiquid LEAPs therefore carry stale quotes. | §11 Layer 1's 10:00 / 12:00 / 14:00 / 15:45 ET sampling — i.e. the time-of-day execution study, and **only** that |

Sections 1–4 answer gaps 1 and 2. Section 5 prices gap 3 as a separate optional line item. Section 6 keeps the full-history pricing as a fallback, in case the archive is later found wanting.

---

## 1. Recommendation

**Resume ThetaData at the Options Value tier — $40/month.** It is the cheapest option on this page by an order of magnitude, and it is the only one where the tail lands in the *same schema the archive is already written in*, against dormant client code the desk already has. ThetaData's own subscription docs put historical **Quote, Open Interest, EOD, Implied Volatility and Greeks 1st Order** inside the Value tier with history back to 2020-01-01, which covers both the ~30-day tail and everything forward. **The catch is licensing, and it is a real one:** the retail tiers are individual/personal-use, and ThetaData's separate commercial tier for Options Data is **$1,600/month billed annually, or "from $500/mo" at a startup rate** — a 12× to 40× jump. Whether an SEC/state-registered RIA doing internal research may sit on the retail tier is **not answerable from the published terms** and is flagged in §7.

**Runner-up — FirstRate Data, $499.95 one-time plus ~$79/month.** Pick this if the personal-use licence is judged unusable. It is the only vendor on this page whose published licence *explicitly* permits internal firm use ("multiple users of the data provided all users are within a single corporation or institution"), it carries the complete S9 field list including **rho and quote sizes**, and its bundle is already updated through **2026-09-04** — so a single $499.95 purchase closes the tail outright today, and $79/month keeps it closed. The cost is schema work: FirstRate's CSV layout is not ThetaData's, so the tail rows need remapping into the warehouse.

**Premium alternative worth naming — Cboe DataShop Option EOD Summary, $840 for the tail, $420/month or $4,200/year thereafter.** It is the most schema-faithful product found: it carries a **15:45 ET snapshot** (exactly S9 §11 Layer 3's default daily observation time) *and* an EOD snapshot, each with NBBO **bid, ask, bid size and ask size**, plus underlying bid/ask, open interest, volume, and — with the paid "Calcs" add-on — implied volatility and **all five greeks including rho**. It is exchange-official OPRA data. It costs roughly 10× ThetaData per month.

---

## 2. Comparison table — closing the tail and staying current

~30 mega-cap tickers. "Tail" = 2026-07-27 → 2026-09-04 (~30 trading days). All figures retrieved 2026-09-05.

| Vendor | Smallest qualifying unit | Tail price | Stay-current price | Greeks + IV historically? | OI? | Bid/ask **sizes**? | Rho? | Licence class | Source URL |
|---|---|---|---|---|---|---|---|---|---|
| **ThetaData** | Options **Value** subscription | $40 (1 month) | **$40/mo** | **Yes** — Greeks 1st Order + IV at Value, from 2020-01-01 | Yes (all tiers) | Yes (OPRA NBBO) | Yes (1st order incl. rho) | **Individual/personal**; commercial = **$1,600/mo annual**, "from $500/mo" startup | [pricing](https://www.thetadata.net/pricing) · [tier table](https://docs.thetadata.us/Articles/Getting-Started/Subscriptions.html) · [commercial](https://www.thetadata.net/commercial-use) |
| **FirstRate Data** | Full EOD options bundle, one-time | **$499.95** (bundle already runs to 2026-09-04) | ~**$79/mo** updates (1st month free) | **Yes** — "Full Greeks (delta, vega, gamma, theta, rho)", IV for bid *and* ask | Yes | **Yes** — "Bid / Ask quotes (including size of quote)" | **Yes** | **Internal firm use, multi-user within one corporation**; no external redistribution | [bundle](https://firstratedata.com/b/49/historical-options-data) · [licence](https://firstratedata.com/about/license) |
| **Cboe DataShop** — Option EOD Summary | Historical order, picked symbols + date range | **$840** with Calcs ($600 without) | **$420/mo** with Calcs ($300 without); **$4,200/yr** with Calcs ($3,000 without) | **Yes**, as paid "Calcs" add-on at the 15:45 snapshot | Yes | **Yes** — Bid/Ask Size at both 15:45 and EOD | **Yes** | Not stated on the product page; an Individual/Firm selector exists on the sibling Option Quotes product | [product](https://datashop.cboe.com/option-eod-summary) |
| **historicaldata.net** (= optiondata.org) | Daily Subscription | **$59** (1 month; 50 most-recent files downloadable, which spans the tail) | **$59/mo** | Yes — delta, gamma, theta, vega, IV | Yes | **Yes** — `bid_size`, `ask_size` | **No** | **"Internal commercial use is allowed — trading, research, and analysis inside your organization"**; no resale/sublicence/redistribution | [products](https://historicaldata.net/options.html) · [FAQ](https://historicaldata.net/faq.html) |
| **DiscountOptionData** | "All US Data (2005-2026) with Greeks" | **$295** (whole archive; 2026 end-date not verified) | Not offered as a subscription (unverified) | Yes — IV, Delta, Gamma, Vega, **Rho**, Theta | Yes | **Yes** — AskSize, BidSize | **Yes** | **Not stated** — T&C page carries only disclaimers and a final-sale clause | [site](https://www.discountoptiondata.com/) · [T&C](https://www.discountoptiondata.com/Home/TermsConditions) |
| **ORATS** | Near-EOD historical + recurring | $99 (1 month recurring) | **$99/mo** (near-EOD); $599 one-time 2007 backfill | Yes — delta, theta, vega, rho, smoothed IV | Yes (cOi/pOi) | **Not listed** for near-EOD (present in the 1-min product) | Yes | **"Individual licence"**; "Professional and enterprise licenses are priced separately" | [near-EOD](https://orats.com/near-eod-data) · [API plans](https://orats.com/data-api) |
| **IVolatility** | Pay-per-use, per ticker per day | ~**$900** (30 × 30 × $1.00) — *arithmetic on published unit rates, not a quote* | Same rate ongoing | Yes — Raw IV dataset carries IV + greeks per contract | Yes | **No** in the EOD NBBO dataset (present in the 1-minute product) | Unverified for EOD | **Retail rates only**; "For institutional, intraday or large-volume data, request a quote" | [retail pricing](https://www.ivolatility.com/data-download-intro/) · [data guide PDF](https://www.ivolatility.com/doc/IVolatility_Data_Nov17.pdf) |
| **historicaloptiondata.com** (DeltaNeutral) | L2-EODYEAR / L3-EODYEAR | $615 / $865 (one year) | Monthly subscription price not published | Yes at L2 — Delta, Gamma, Theta, Vega, IV | Yes | **No** | **No** at L2 | PRO tier = 10-seat site licence, defined for firms with **AUM ≥ $10M**, at **$4,935/yr** | [shop](https://historicaloptiondata.com/shop/) · [L2 structure](https://historicaloptiondata.com/data-structure-greeks/) · [PRO](https://historicaloptiondata.com/product/pro-eodyear/) |
| **OptionMetrics IvyDB US** | — | **No published price** | **No published price** | Yes — IV + delta, gamma, vega, theta from Jan 1996 | Yes | Not stated (closing bid/ask) | **Not listed** | Contact sales | [IvyDB US](https://optionmetrics.com/united-states/) |
| **Massive** (ex-Polygon.io) | Options Advanced | $199/mo | $199/mo | **NO — snapshot only, not historical** | Yes (daily) | Quotes at Advanced tier | n/a | "Individual use", **"Non-pros only"** | [pricing](https://massive.com/pricing) · [options](https://massive.com/options) |
| **Nasdaq Data Link** | — | **Not established** | **Not established** | Could not find any single-name equity options chain dataset with greeks | — | — | — | — | [catalogue](https://data.nasdaq.com/search?query=options) |
| **OptionsDX** | Per-ticker chain products, $0–$50 | n/a | n/a | Yes (pre-calculated greeks/IV per marketing copy) | Unverified | Unverified | Unverified | Not stated | [shop](https://www.optionsdx.com/shop/) |
| **Databento** | OPRA usage-based, $/GB | Not established | Not established | **No greeks/IV mentioned** — raw OPRA feed | n/a | Yes (raw NBBO) | No | Redistribution rights vary by dataset | [pricing](https://databento.com/pricing) |

**Vendors that publish real, self-serve pricing:** ThetaData, FirstRate Data, Cboe DataShop (via its live configurator), historicaldata.net, DiscountOptionData, ORATS, IVolatility (retail only), historicaloptiondata.com, Massive, OptionsDX, Databento (rate card only).
**Contact-sales-only, no public price:** OptionMetrics IvyDB, Nasdaq Data Link (for anything matching this requirement), ThetaData's *commercial* tier beyond the headline $1,600/$500 figures, IVolatility's institutional and intraday tiers, ORATS professional/enterprise, Cboe's Individual-vs-Firm licence terms.

---

## 3. Per-vendor detail

### 3.1 ThetaData — the resumption case

There is a direct conflict in ThetaData's own published material that I could not resolve without contacting them, and it matters:

- The **pricing page** describes Options Value ($40/mo) as "3 Request types / 4 Years of data", Options Standard ($80/mo) as "7 Request types / 8 Years of data", Options Pro ($160/mo) as "12 Request types / 12 Years of data". It does not say which request types.
- The **docs subscription table** shows VALUE unlocking **eight** historical endpoints — EOD, Quote, Open Interest, OHLC, Trade, Trade Quote, **Implied Volatility, Greeks 1st Order** — with option history from **2020-01-01**. STANDARD adds 2nd/3rd-order greeks and trade greeks, from 2016-01-01. PRO from 2012-06-01.

If the docs table is right, **Value at $40/mo is sufficient** — it carries quotes (with OPRA sizes), open interest, IV and first-order greeks (which is where delta, theta, vega and rho live), over a window that starts in 2020 and therefore fully covers both the tail and everything forward. If the pricing page's "3 request types" is right, Value is not sufficient and the answer is Standard at $80/mo. **Either way this is the cheapest line on the page.** Budget $80/mo to be safe.

Long-dated expiry coverage: ThetaData sells by contract from the OPRA record, and the existing archive demonstrably contains LEAPs out past 1,000 DTE, so long-dated coverage is established by the archive itself rather than by a marketing claim.

Delivery: REST API / Python / local terminal — the desk already has dormant client code for it.

**Licensing.** The Subscriber Agreement grants "Use of the Data for the benefit of Licensee in the ordinary course of its internal personal use/the purpose of trading on Licensee's behalf, analysis, and/or research", and prohibits disclosing or redistributing the data to third parties. The commercial page is explicit that its tier is "For commercial applications and business use cases only. Not for individual/personal use", priced at **$1,600/mo (annual billing) for Options Data**, with a **startup rate from $500/mo**. Nothing published tells you which side of that line an RIA's internal research sits on. See §7.

### 3.2 FirstRate Data — the licence-clean answer

The bundle page states, verbatim: frequency **End-of-Day**, date range **Jan 2010 – Aug 2026**, **5,860 tickers**, **"Buy Now $499.95"**, and "All tickers updated to 2026-09-04". Field list as published: "Bid / Ask quotes (including size of quote). Implied volatility for both bid and ask quotes. Last traded price of the option. Open interest. Full Greeks (delta, vega, gamma, theta, rho) for each put and call option. Daily traded volume." CSV, quarterly zip archives, download or API. "Dataset purchase includes one month of free daily updates. Thereafter, a subscription for daily updates is approx $79 per month."

This is the only vendor here whose published licence squarely covers a firm: the FAQ says "The license allows for multiple users of the data provided all users are within a single corporation or institution", and the licence agreement permits internal models and published research while prohibiting resale or external redistribution.

**Caveat:** the field list does not mention an **underlying price** column in the options file. §12.1 requires `underlying_price`. Unverified — see §7.

### 3.3 Cboe DataShop — the schema-faithful answer, priced live

Field list published on the product page: Underlying Symbol, Quote Date, Root, Expiration, Strike, Option Type, Open, High, Low, Close, Trade Volume, **Bid Size 1545, Bid 1545, Ask Size 1545, Ask 1545**, Underlying Bid 1545, Underlying Ask 1545, Active Underlying Price 1545\*, **Implied Volatility 1545\*, Delta 1545\*, Gamma 1545\*, Theta 1545\*, Vega 1545\*, Rho 1545\***, Bid Size EOD, Bid EOD, Ask Size EOD, Ask EOD, Underlying Bid EOD, Underlying Ask EOD, VWAP, Open Interest. Starred fields require the paid **Calcs** add-on. Coverage "Available from January 2012 to present". Delivery: daily files, per-day or per-month grouping.

Prices read off the live configurator on 2026-09-05, 30 mega-cap symbols picked (AAPL MSFT NVDA AMZN GOOGL GOOG META AVGO TSLA BRK.B LLY JPM V XOM UNH MA WMT ORCL NFLX COST HD PG JNJ CVX ABBV BAC KO PEP MRK ADBE):

| Configuration | Without Calcs | With Calcs |
|---|---|---|
| **Historical, 07/27/2026 – 09/04/2026 (the tail)** | **$600.00** | **$840.00** |
| Subscription, monthly | $300.00 | $420.00 |
| Subscription, annual | $3,000.00 | $4,200.00 |
| Historical, 09/01/2021 – 08/31/2026 (full 5yr — now moot) | $7,200.00 | $8,500.00 |
| Historical, 01/03/2023 – 08/31/2026 (3.75yr variant — now moot) | — | $6,000.00 |

Cboe's pricing is **not linear in symbol count**: the same 5-year range with Calcs came out at **$4,800 for 5 symbols**, **$8,500 for 20 symbols**, and **$8,500 for 30 symbols** — i.e. it tiers and then flattens. Buying all 30 costs the same as buying 20.

The academic-discount page states the standard-price basis indirectly: "50% of standard price", **$500 minimum per dataset**, accredited institutions only. Not applicable here, but it confirms Cboe treats these as standard-priced datasets rather than negotiated ones.

### 3.4 historicaldata.net (optiondata.org) — the cheapest way to close the tail

$59/month Daily Subscription, 7-day free trial, "the most recent 50 daily files available on the website, so you can download missed days during that window". The tail is ~30 trading days, so a single month's subscription reaches back far enough to cover all of it. Fields: contract, underlying, expiration, type, strike, style, **bid, bid_size, ask, ask_size**, volume, open_interest, quote_date, **delta, gamma, theta, vega, implied_volatility**. Sourced "from OPRA (Options Price Reporting Authority) feeds". New data "typically by ~5:30 pm ET". One-time archives: last 365 days $120, last 24 years (May 2002 →) $590, 2024 $99, 2022–2024 $330.

Licence, verbatim from the FAQ: "Internal commercial use is allowed — trading, research, and analysis inside your organization, including multi-user access if a multi-user license is purchased… What you may not do is resell, sublicense, or redistribute the data itself."

**The disqualifier for strict schema parity: no rho.** §12.1 lists `rho`. Everything else on the required field list is present.

### 3.5 DiscountOptionData — cheapest full field list, murkiest licence

"All US Data (2005-2026) with Greeks", **$295**, 4,552 symbols, 1.74B rows, 36 GB. Fields: Symbol, ExpirationDate, AskPrice, **AskSize**, BidPrice, **BidSize**, LastPrice, PutCall, StrikePrice, Volume, OpenInterest, **UnderlyingPrice**, DataDate, plus Implied Volatility, Delta, Gamma, Vega, **Rho**, Theta. That is the complete S9 §12.1 field list at $295 for the whole market. Delivery: CSV via browser, Google Drive, or shipped hard drive.

Two reasons it is not the recommendation: (a) the Terms & Conditions page carries only a warranty disclaimer, a liability limit and a final-sale clause — **no licence grant at all**, so what an RIA is permitted to do with it is unstated; (b) their FAQ page returned HTTP 500 today, so the EOD snapshot time and data provenance could not be established, and "2005-2026" does not tell you which day in 2026 the archive stops.

### 3.6 ORATS

API plans: Delayed Data $199/mo, Live Data $299/mo, Live Intraday $599/mo, All-In $899/mo. Historical near-EOD: **$99/mo recurring, $599 one-time backfill** to 2007, snapshot taken **14 minutes before the close** (≈15:46 ET — close to S9's 15:45 target). Fields include delta, theta, vega, rho, smoothed IV (`smoothSmvVol`), `stkPx`, `cOi`/`pOi`, `cVolu`/`pVolu`, and call/put bid/ask — but **bid and ask sizes are not listed** for the near-EOD product, which is a direct §12.1 miss. Delivery FTP (recurring) / AWS S3 with 14-day credentials (historical). "Individual license"; "Professional and enterprise licenses are priced separately."

### 3.7 IVolatility

Published **retail** rates, per ticker per day: underlying prices $0.20, option prices (NBBO) and historical volatility $0.40, Raw IV / IV Surface / IV Index $0.60. To get both quotes and greeks you buy two datasets, so ~$1.00/ticker/day. The tail (30 tickers × ~30 trading days) works out to **≈$900** — arithmetic on their published unit rates, not a quote from them. The full 5-year window at 15 concurrent tickers would have been ≈$18,900, which is why this vendor never wins on bulk.

Their own data guide confirms the EOD NBBO dataset is "bid, ask prices, daily volume, total open interest, stock price" — **no sizes**. Their **1-minute** intraday product does include "options bid/ask prices&sizes, volume and implied volatilities data & Greeks, along with underlying's prices", history from **August 2011** — but no price is published for it. US EOD history from Nov 2000, with an additional **3:45pm** snapshot since 2005. The page states "Pricing shown is for retail users only. For institutional, intraday or large-volume data, request a quote", and that retail rates are "roughly 70% below standard" — so an RIA should assume roughly 3× these numbers.

Note: the data guide PDF is dated November 2017. The rate card is current (retrieved today); the field descriptions are from a 2017 document.

### 3.8 historicaloptiondata.com (DeltaNeutral)

Yearly EOD packages: L1 $585, L2 $615, L3 $865, PRO $4,935; multi-year L2 24+ years $1,495, L3 all-history $2,035, PRO all-history $10,595. The **verified** L2 column list is: `UnderlyingSymbol, UnderlyingPrice, Exchange, OptionSymbol, Blank, Type, Expiration, DataDate, Strike, Last, Bid, Ask, Volume, OpenInterest, IV, Delta, Gamma, Theta, Vega, Alias` — **no bid size, no ask size, no rho**. L3 adds BidIV/AskIV and surface IV, not sizes. Coverage from 2002.

The PRO tier is explicitly aimed at this buyer type: "a site license to share the options data within your organization… with up to 10 people on your staff or clients staff", for "a person or entity that is using the data on behalf of others, such as a broker, investment manager or hedge fund with assets under management of 10 MILLION or more", at **$4,935/year**. That is a useful data point for §7: this vendor prices the professional/firm licence at roughly **8× the individual yearly package**.

### 3.9 OptionMetrics IvyDB US

End-of-day options from **January 1996**: closing bid and ask, volume, open interest, computed implied volatility and greeks (delta, gamma, vega, theta), plus underlying prices, interest rates, dividends and corporate actions. Rho is not listed. Bid/ask sizes are not listed. **No pricing is published anywhere on the product page** — "Contact Us for More Information". It is the academic/institutional standard and would be substantial overkill for a 30-day tail.

### 3.10 Massive (formerly Polygon.io) — disqualified on historical greeks

`polygon.io/pricing` now issues a **301 redirect to `massive.com/pricing`**. Options tiers: Basic free (2yr), Starter $29/mo (2yr), Developer $79/mo (4yr), Advanced $199/mo (5+ yr), 20% off annually; flat files / S3 on all paid tiers; NBBO quotes at Advanced.

Greeks and IV are **snapshot-only** — current state, not a historical series. Polygon's own public issue tracker records this as an outstanding feature request, with the stated workaround being "request and store values daily". For a delta-based selection rule that is fatal, for exactly the reasons already set out in `S9_DATA_FEASIBILITY_IBKR_2026-09-05.md` §5. Also: the individual tiers are marked "Non-pros only".

### 3.11 Nasdaq Data Link

I could not locate any US single-name equity options **chain** dataset with greeks, IV, open interest and quotes in the Nasdaq Data Link catalogue, and no price for one. What Nasdaq does publish under adjacent names — "Nasdaq Smart Options" (a low-bandwidth OPRA feed), "Nasdaq Greeks and Implied Volatility powered by Nasdaq Basic" (a streaming real-time feed, 60-second updates) — are **feeds, not history**. Treat this vendor as **not established** rather than as absent; see §7.

### 3.12 Also checked, and why they do not fit

- **OptionsDX** — sells per-ticker chain products at $0–$50, but only about ten underlyings exist (SPY, SPX, VIX, BTC, QQQ, TSLA, AAPL, UVXY, SLV, NVDA). Three of the thirty names. Does not cover the manifest.
- **Databento** — usage-based $/GB on the raw OPRA feed, $125 in free credits for new users, redistribution rights varying by dataset. Raw quotes only; no greeks or IV mentioned anywhere on the pricing page. Wrong shape for a delta-based study.

---

## 4. What the tail actually costs, ranked

| Option | Closes the tail | Stays current | Rho? | Sizes? | Licence covers a firm? |
|---|---|---|---|---|---|
| **ThetaData Value/Standard** | **$40–$80** | **$40–$80/mo** | Yes | Yes | **Unclear — retail is personal-use; commercial is $500–$1,600/mo** |
| **historicaldata.net** | **$59** | $59/mo | **No** | Yes | **Yes, explicitly** |
| **DiscountOptionData** | $295 (whole archive) | Not offered | Yes | Yes | **Unstated** |
| **FirstRate Data** | **$499.95** | ~$79/mo | Yes | Yes | **Yes, explicitly** |
| **Cboe DataShop EOD Summary** | **$840** | $420/mo · $4,200/yr | Yes | Yes | Not stated on this product |
| **IVolatility (retail rates)** | ≈$900 (computed) | ≈$30/trading day | Unverified | **No** | Retail only; institutional = quote |
| **ORATS near-EOD** | $99 | $99/mo | Yes | **No** | Individual; professional = quote |

---

## 5. Optional line item — intraday, for the time-of-day execution study only

This buys **one** thing: §11 Layer 1's 10:00 / 12:00 / 14:00 / 15:45 ET sampling on ~23 quarterly decision dates. Nothing else in S9 depends on it. Note that a **15-minute** interval grid is the coarsest one that lands on all four target times (a 30- or 60-minute grid misses 15:45).

**Cboe DataShop — Option Quotes**, 30 symbols, Open Interest **and** Calcs included, priced live on 2026-09-05:

| Interval | 09/01/2021 – 08/31/2026 (full window) |
|---|---|
| 1 minute | **$71,075.00** |
| **15 minutes** | **$35,538.00** |
| 30 minutes | $20,308.00 |
| 60 minutes | $11,696.00 |
| 390 minutes (EOD) | $8,500.00 |

You do not need the full window. A **single trading day** at 15-minute intervals, same 30 symbols, with OI and Calcs, priced at **$569.00**. If that scales linearly, 23 decision dates ≈ **$13,100** — **but I could not verify linearity.** The configurator only recalculates through its own "Add Date(s)" flow, and replacing one date with another left the subtotal unchanged at $569.00, which confirms the per-day rate but tells you nothing about multi-date volume discounts. Treat $13,100 as an upper-bound estimate, not a quote.

Product also carries an explicit **Individual / Firm** licence selector; toggling it did not change the subtotal in my run, which is itself surprising and may mean the selector affects the agreement rather than the price. Unverified.

**Alternatives for intraday:**
- **ORATS 1-minute historical** — **$1,500 one-time** backfill covering **August 2020 → present**, 5,000+ symbols, "bid/ask prices and sizes, open interest, and volume metrics" plus delta, gamma, theta, vega, rho, phi, driftlessTheta and IV. **Far cheaper than Cboe** — but it is full-market only (no symbol subset), the full backfill is "roughly 50TB" growing ~21GB per trading day, S3 transfer adds **"$1-2k"**, download access lasts **14 days**, and the licence is Individual with professional priced separately. Practical only if the desk can pull and filter 50TB inside a fortnight, or accepts hard-drive shipment.
- **ThetaData** — tick-level quotes and greeks let you sample any timestamp you like, at $80/mo (Standard, history to 2016-01-01) or $160/mo (Pro, to 2012-06-01). On price this dominates everything else here by two orders of magnitude, subject to the same licensing question.
- **IVolatility 1-minute** — includes bid/ask **sizes**, IV and greeks, from August 2011. **No published price**; "request a quote".

---

## 6. Fallback — full five-year history, if the archive is ever found wanting

Retained from the original brief. Tier A = intraday 4×/day on 23 decision dates plus a daily 15:45 observation; Tier B = EOD only, one observation per trading day.

| Vendor | Tier B, Sep 2021 – Sep 2026 | Tier B, Jan 2023 – Sep 2026 | Tier A |
|---|---|---|---|
| Cboe DataShop | **$8,500** with Calcs / $7,200 without | **$6,000** with Calcs | $8,500 (EOD) **+** ~$13,100 est. for 23 dates at 15-min |
| FirstRate Data | **$499.95** (2010 → 2026-09-04, whole market) | same $499.95 | not offered |
| DiscountOptionData | **$295** (2005 → 2026, whole market, with greeks) | $149 bundled 2020–2025 w/ greeks | not offered |
| historicaldata.net | **$590** (24 years) or $120 (last 365 days) | $590 | not offered |
| historicaloptiondata.com | $1,495 (L2, 24+ yrs) / $2,035 (L3 all) | $615/yr L2 | $1,815/yr intraday all equity options |
| ThetaData | $80/mo × months needed | same | same subscription |
| ORATS | $599 one-time (2007 →) | same | **$1,500** one-time (Aug 2020 →) + $1–2k transfer |
| IVolatility | ≈$18,900 computed at retail rates | ≈$14,200 computed | quote only |
| OptionMetrics | no published price | no published price | no published price |

On price alone the bulk vendors beat the metered ones by 10–60×, exactly as suspected. **DiscountOptionData at $295 is the cheapest thing found that carries the complete §12.1 field list** including rho and both quote sizes — but with no licence grant published at all, which is why it is not recommended for an RIA.

---

## 7. What I could not verify

Stated plainly. Each of these is a genuine gap, not a hedge.

1. **Whether an SEC/state-registered RIA may use ThetaData's retail tier.** The Subscriber Agreement's grant covers "internal personal use/the purpose of trading on Licensee's behalf, analysis, and/or research"; the commercial page says its tier is "For commercial applications and business use cases only. Not for individual/personal use." Nothing published maps a registered advisor's internal research onto one side or the other. **This is the single most consequential unknown on this page** — it is the difference between $40/month and $1,600/month.
2. **ThetaData's Value tier contents.** Pricing page says "3 Request types"; the docs subscription table shows eight, including Implied Volatility and Greeks 1st Order. Unreconciled. Budget Standard ($80/mo) if you need certainty.
3. **ThetaData commercial pricing detail.** $1,600/mo (Options, annual billing) and "from $500/mo" startup rate are published headline figures. What the startup rate requires, what it includes, and whether any intermediate tier exists are not published.
4. **Whether FirstRate Data's options files contain an underlying price column.** Their published field list does not mention one. §12.1 requires `underlying_price`. I did not download the sample file (downloading requires your approval).
5. **DiscountOptionData's licence.** There is none published. The Terms & Conditions page contains a warranty disclaimer, a liability limitation and a final-sale clause, and nothing granting or restricting use.
6. **DiscountOptionData's EOD snapshot time, data provenance, and exact 2026 end date.** Their FAQ page returned HTTP 500 today. "2005-2026" does not say which day in 2026.
7. **historicaldata.net's exact snapshot time.** The site says files are posted "typically by ~5:30 pm ET" and that records are sourced from OPRA feeds, but never states the instant the EOD snapshot is struck.
8. **Cboe DataShop's licence terms.** The Option EOD Summary page states none. The sibling Option Quotes product exposes an **Individual / Firm** selector, which implies a distinction exists, but toggling it left the subtotal unchanged in my run and no terms text is shown. Also unverified: whether Cboe's historical-order pricing carries volume discounts across many separately-added dates.
9. **Whether Cboe's Calcs greeks are computed at 15:45 only.** The page says calculations are "provided at the 15:45 snapshot"; whether any greeks accompany the EOD snapshot is not stated. If not, the EOD half of each row is quotes-only.
10. **Cboe's per-symbol pricing curve.** Verified points are 5 symbols = $4,800, 20 = $8,500, 30 = $8,500 (5yr, with Calcs). The shape between and beyond those points is unknown, and the "no Calcs" curve was only sampled at 30 symbols.
11. **Nasdaq Data Link.** I could not establish that a qualifying product exists, and I could not establish that one does not. Their catalogue search returned no usable page content today. Report this as unresolved, not as a negative finding.
12. **OptionMetrics, ORATS professional, IVolatility institutional/intraday, ThetaData flat-files.** All contact-sales. No number of any kind is published for these, and I did not contact them.
13. **IVolatility field detail is from a 2017 document.** The rate card is current; the column descriptions are from `IVolatility_Data_Nov17.pdf`. Whether the EOD NBBO dataset has since gained size fields is unknown.
14. **Rho at IVolatility's EOD tier.** Their marketing says "IV and Greeks"; the guide enumerates delta explicitly and does not enumerate the rest. Unverified.
15. **Data quality at the cheap bulk vendors.** Nobody's quality claims were tested. No sample file was downloaded from any vendor (downloads need your approval). Field *presence* is what I verified; field *correctness* is not.
16. **Long-dated expiry coverage was not independently confirmed at any vendor.** For the tail this is low-risk — every vendor here sells the full OPRA chain, and the existing archive already proves LEAPs out past 1,000 DTE exist in the data — but no vendor page was found that states a maximum DTE, and I did not test one.
17. **Whether ORATS' 50TB one-minute backfill is practically retrievable** inside its 14-day access window, and what the hard-drive option costs.

---

## 8. Licensing — flagged for your judgement, not advice

I am reporting what the terms say. I am not interpreting them for you and this is not legal advice.

The pattern across this market is consistent: **cheap tiers are individual/personal-use, and the firm tier costs several multiples more.** Where a vendor prices both sides, the gap is large and published:

- **ThetaData**: retail $40–$160/mo, individual; commercial Options **$1,600/mo** annual, "from $500/mo" startup. **12×–40×.**
- **historicaloptiondata.com**: L2 year $615 individual; **PRO $4,935/yr** for "a person or entity that is using the data on behalf of others, such as a broker, investment manager or hedge fund with assets under management of 10 MILLION or more", 10 seats. **8×.**
- **IVolatility**: retail rates are "roughly 70% below standard", i.e. the standard/institutional rate is roughly **3×** what is shown.
- **Massive**: individual tiers are marked "Non-pros only" outright.
- **ORATS**: "Individual license"; professional and enterprise "priced separately".

Two vendors publish a licence that reads as covering a firm's internal research without a price jump:

- **FirstRate Data** — "The license allows for multiple users of the data provided all users are within a single corporation or institution", with the licence agreement permitting internal models and published research, and prohibiting resale or external redistribution.
- **historicaldata.net** — "Internal commercial use is allowed — trading, research, and analysis inside your organization… What you may not do is resell, sublicense, or redistribute the data itself."

Two publish nothing usable either way: **DiscountOptionData** (no licence grant at all) and **Cboe DataShop** (no terms shown on the product page, though an Individual/Firm selector exists on a sibling product).

The specific question worth putting to a human before money moves: **S9 is research that may inform real client trading.** That is the fact pattern that "personal / non-commercial" licences are written to exclude. It does not make the cheap tiers wrong — it makes them a decision you should take deliberately rather than by default. If the answer is that the firm tier is required, the recommendation in §1 flips from ThetaData at $40/month to **FirstRate Data at $499.95 one-time plus $79/month**, which is still cheaper than ThetaData's own commercial tier for a single month.

---

## 9. Suggested next step

Nothing here needs a vendor conversation to act on except the ThetaData licence question, which does. Two things could be settled without spending anything:

1. **Ask ThetaData which licence tier applies to an RIA's internal research.** That is a single question and it decides between $40/month and $1,600/month. *(I did not ask it — contacting vendors was out of scope for this run.)*
2. **Pull a sample file from FirstRate Data and from historicaldata.net** and check the actual columns against §12.1 — specifically `underlying_price` at FirstRate and the absence of `rho` at historicaldata.net. Both offer free samples. *(I did not download either — downloads need your approval.)*
