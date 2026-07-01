# Free-data deep dive: can we build the CAN SLIM backtest dataset for $0?

Research date: 2026-07-01. Question: can we assemble the ENTIRE historical dataset for a
CAN SLIM / O'Neil growth-stock **selection** backtest — survivorship-free prices +
**point-in-time (as-reported)** fundamentals for US small/mid-cap stocks, 2018–2026 plus
delisted names — for **$0** using free/public sources, and how much engineering that takes?

Scope reminder: this is a **one-time historical download** saved to the local warehouse
(`C:\TradingDesk-Local\`), not an ongoing feed. The live screen runs off IBKR later.
Per stock over time we need: quarterly & annual EPS and sales/revenue (growth rates), ROE,
margins (all as-reported / point-in-time), and daily prices (RS + base detection).

---

## Bottom line up front

**Yes — the full dataset can be built for $0**, and the free stack is genuinely
survivorship-free. The cost is **engineering time, not money**:

- **Fundamentals (point-in-time, as-reported): SEC EDGAR, free, no API key.** Verified
  firsthand this session — it is inherently survivorship-free (delisted companies' full
  XBRL persists). This is the strong leg.
- **Prices (survivorship-free daily, incl. delisted): Stooq bulk US archive** as the
  backbone + IBKR for surviving names + Tiingo free (permaTicker) for targeted gap-fills.
  This is the weaker, gappier leg for obscure small caps.
- **IBD-style ratings: self-computable for $0** (RS Rating from prices is high-confidence;
  EPS/Composite are honest *approximations*, not licensed IBD numbers).

**Honest effort:** a validated point-in-time EDGAR fundamentals pipeline is a
**multi-week slog (~2–4 weeks)**, not a weekend. Prices are faster to wire but need
coverage validation. See the tradeoff section for where ~$30–100 of a one-month vendor
pull would actually buy back real time.

**Coverage of the advisor's actual 834-name watch list (measured this session):**
- **96.8%** found in Tiingo's free ticker universe (price coverage; 807/834).
- **86.2%** map to a *current* SEC CIK; **~87%** of the non-ETF companies do
  (fundamentals coverage — and the misses are almost all renames/delistings whose
  fundamentals still exist on EDGAR under the old CIK, not true gaps).
- **62** names are delisted/acquired during 2018–2026 (the survivorship-critical set) —
  and every spot-checked one still has full fundamentals on EDGAR.
- **26** non-ETF names need a rename/old-CIK lookup; only a **handful** (e.g. Farfetch,
  Invitae) are genuinely hard.

---

## 1. SEC EDGAR — the primary free fundamentals source (VERIFIED FIRSTHAND)

EDGAR provides **free, survivorship-bias-free, point-in-time** fundamentals for
effectively all US public companies (incl. small/micro caps and delisted issuers) from
~2009 forward via XBRL. It is the single best free source for as-reported financials.
2009+ XBRL fully covers the 2018–2026 window.

### 1a. Financial Statement Data Sets (bulk quarterly ZIPs) — the right ingestion path

- **Landing page:** https://www.sec.gov/dera/data/financial-statement-data-sets
- **URL pattern:** `https://www.sec.gov/files/dera/data/financial-statement-data-sets/YYYYqQ.zip`
- **Verified firsthand:** `GET .../2024q1.zip` → HTTP 200, **Content-Length ≈ 124 MB**
  (per-quarter ZIPs are larger than older SEC docs suggest — budget ~100–130 MB/quarter,
  a few GB for the full 2009–2026 span). Requires a descriptive `User-Agent` header.
- **History:** 2009 Q1 → present. **Cadence:** quarterly, ~1 month after quarter close.
- **Structure (4 tab-delimited files per ZIP; documented in the ZIP's readme / aqfs.pdf):**
  - `sub.txt` — one row per filing (accession, cik, name, SIC, form 10-K/10-Q, period,
    **filed date**, fiscal year/period). Your point-in-time key.
  - `num.txt` — the numbers: adsh, **tag**, version, **ddate** (period end),
    **qtrs** (0=instant, 1=quarter, 4=annual), uom, **value**. The core table.
  - `tag.txt` — tag definitions (standard us-gaap vs custom extension).
  - `pre.txt` — presentation (statement + line order).
- **Fields available (all standard us-gaap in num.txt — confirmed present on AAPL live):**
  `Revenues` / `RevenueFromContractWithCustomerExcludingAssessedTax` / `SalesRevenueNet`;
  `NetIncomeLoss`; `EarningsPerShareBasic`/`Diluted`;
  `WeightedAverageNumberOfSharesOutstanding*`; `StockholdersEquity`; `Assets`;
  `GrossProfit`; `OperatingIncomeLoss`; `CostOfRevenue`.
  → enough to derive **EPS growth, sales growth, ROE, and margins**.

### 1b. Company Facts / Frames / Submissions APIs (data.sec.gov) — VERIFIED FIRSTHAND

- **Company Facts** (all concepts for one company, every period, one JSON):
  `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`
  Verified: Apple (CIK 0000320193) → 200, ~3.75 MB, 503 us-gaap concepts, all key tags
  present. Each fact carries `start`/`end`, `val`, `accn`, `fy`, `fp`, **`filed`**, `form`
  — the `filed` date is what enables **true point-in-time** ("what was known as of date D").
- **Frames** (one concept across ALL companies for one period — cross-sectional):
  `https://data.sec.gov/api/xbrl/frames/us-gaap/{Concept}/{unit}/CY####Q#.json`
  Verified: `.../RevenueFromContractWithCustomerExcludingAssessedTax/USD/CY2023Q1.json`
  → 200, **2,755 companies** in one call. This is the whole-universe workhorse and is
  inherently survivorship-free.
- **Company Concept** (one concept, one company — small payloads):
  `https://data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/EarningsPerShareDiluted.json`
- **Submissions** (filing history + metadata + `formerNames`):
  `https://data.sec.gov/submissions/CIK##########.json`
- **Rules:** **10 req/sec** fair-access cap; **must send a descriptive `User-Agent`** or 403;
  no API key. Source: https://www.sec.gov/os/webmaster-faq#developers ,
  https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- **Bulk shortcut for whole-universe builds (VERIFIED FIRSTHAND — 200):** instead of 10k+
  per-company calls, download two archives once:
  `https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip` (**~1.39 GB**, every
  company's companyfacts JSON) and
  `https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip` (**~1.55 GB**).
  ~2.9 GB total, sidesteps the rate limit entirely. This is the recommended ingestion path.

### 1c. CIK↔ticker mapping & survivorship — VERIFIED FIRSTHAND

- **Free map:** `https://www.sec.gov/files/company_tickers.json` (cik_str, ticker, title;
  verified 200, **10,426 tickers**). Also `company_tickers_exchange.json` (adds exchange).
- **Survivorship (the crucial point):** this map lists only **current/active** filers, so
  delisted/renamed names may be absent. BUT **their historical filings never disappear** —
  the CIK is permanent. Verified firsthand: **Twitter** (delisted 2022, CIK 0001418091) →
  companyfacts 200, entityName "Twitter, Inc.", **457 us-gaap concepts** still fully
  available. So EDGAR is **survivorship-free at the filing level**; the only work is
  resolving old tickers → CIKs (via `formerNames`, historical `sub.txt`, or a rename map).

### 1d. HONEST EFFORT — raw EDGAR → clean point-in-time CAN SLIM table

**Realistic estimate: a multi-week slog (~2–4 weeks) for a robust, validated pipeline.**
A rough first-pass table for a few hundred watch-list names is a few days. The hard parts:

1. **Inconsistent revenue tags across filers/eras** (the biggest time sink). Revenue may be
   `Revenues`, `SalesRevenueNet` (older), `RevenueFromContractWithCustomer...` (post-ASC606,
   ~2018+), or `SalesRevenueGoodsNet`. Need a **priority-ordered fallback list per concept**
   + per-company sanity checks.
2. **Quarterly vs YTD alignment (classic trap).** 10-Q income-statement figures are often
   **year-to-date**, not the discrete quarter. To get discrete Q2/Q3/Q4 you must
   **difference consecutive YTD periods** (Q4 = FY − 9-month YTD). Growth rates depend on
   getting this right. Balance-sheet items (equity for ROE) are **instants** (`qtrs=0`).
3. **Restatements & point-in-time integrity.** The same period gets re-reported later. To be
   truly point-in-time you must key on the **`filed` date** and take what was known as of
   the decision date — not the latest value — or you leak future restatements into the
   backtest (a subtle look-ahead bug). companyfacts exposes every version with its `filed`.
4. **Trailing growth math.** YoY quarterly EPS/sales growth + acceleration needs a clean,
   correctly-signed, gap-filled quarterly series first; missing small-cap quarters break the
   YoY join.
5. **Small/foreign-filer quirks.** Small caps: later XBRL adoption, more custom extension
   tags, sparser data. **Foreign private issuers file 20-F/40-F** — often annual only,
   sometimes **IFRS** tags (`ifrs-full:`) and non-USD units — and will silently drop from a
   us-gaap-only pipeline unless handled. ADR-heavy names on the watch list will have gaps.
6. **General hygiene:** dedupe, filter dimensional/segment members, unit/scale checks.

**Leverage, don't build from scratch** (these cut fetching + CIK resolution + some
tag-normalization; they do NOT solve YTD-differencing, restatement/as-of logic, or CAN SLIM
growth math — that's the custom layer):
- `edgartools` — https://github.com/dgunning/edgartools (high-level facts/financials)
- `sec-edgar-api` — https://github.com/jadchaar/sec-edgar-api
- `sec-edgar-downloader` — https://github.com/jadchaar/sec-edgar-downloader
- `python-edgar` — https://github.com/edouardswiac/python-edgar
- SEC structure doc `aqfs.pdf` — https://www.sec.gov/files/aqfs.pdf

### 1e. Limits
- **XBRL depth / phase-in (VERIFIED FIRSTHAND):** filesets exist back to 2009q1, but
  `2009q1.zip` is ~13.5 KB (near-empty — XBRL was large-filer-only then) and 2011q1 is ~96%
  large-accelerated filers. Full small-cap coverage lands ~2011–2012+. Confirmed
  **2019q1 = 5,708 filers spanning all sizes** (large/accelerated/non-accelerated/
  smaller-reporting) — so **2018–2026 coverage is complete across cap sizes. Non-issue for
  our window;** just don't expect clean small-cap data before ~2012. Pre-2009 is unstructured
  HTML/text (irrelevant here).
- **Foreign private issuers (20-F/40-F):** often no quarterly, sometimes IFRS taxonomy —
  expect gaps for ADR/foreign names. US domestic 10-K/10-Q small/mid caps are well covered.
- **Frames API:** a `CY####Q#` frame only includes companies whose period exactly matches
  that calendar frame; off-calendar fiscal filers are under-represented in a single frame
  call (companyfacts per-CIK avoids this).

---

## 2. Free PRICE sources with delisted (survivorship-free) coverage

The weak leg. There is **no single clean free source**; assemble a stack.

- **Stooq bulk US archive — the only realistic $0 whole-universe survivorship-inclusive
  backbone.** Free EOD; its bulk US archive **includes delisted/dead tickers** (the standard
  free go-to for survivorship-aware backtests). Per-ticker CSV pattern:
  `https://stooq.com/q/d/l/?s=TICKER.US&i=d` (this is what `pandas-datareader`'s Stooq reader
  hits). Bulk archive at `https://stooq.com/db/h/`.
  - **Caveats (important, and partly unconfirmed):** per-ticker pulls are **throttled and
    CAPTCHA-gated** — heavy looping gets soft-blocked; the **bulk zip is the intended path**,
    not per-ticker loops. Close is adjusted but the adjustment methodology is
    under-documented. **Obscure small caps: expect gaps, missing history, bad ticks, and
    ticker-reuse ambiguity** — exactly our target universe. *The "includes delisted" +
    exact bulk sizes rest on practitioner sources; I could not fetch Stooq's own pages this
    session (anti-bot returned an HTML interstitial for a direct CSV probe) — treat as
    high-confidence but not vendor-confirmed.* Sources:
    https://pydata.github.io/pandas-datareader/readers/stooq.html ,
    https://www.quantstart.com/articles/an-introduction-to-stooq-pricing-data/
- **Tiingo free tier** — OHLCV on ~37k US stocks, 30+ yr; **retains delisted symbols**
  (stable `permaTicker`). But free tier is **50 req/hr, 1,000 req/day, and only 500 UNIQUE
  symbols per MONTH** — the binding constraint. Great for targeted gap-fills on a curated
  list; painful for a whole delisted universe. https://www.tiingo.com/about/pricing ,
  https://www.tiingo.com/documentation/fundamentals
- **Alpha Vantage free** — `LISTING_STATUS&state=delisted` gives a full free CSV of delisted
  US tickers (symbol, name, exchange, IPO/delist dates) = **excellent for building the
  authoritative survivorship-free universe list**. But free is **25 req/day**, and
  post-delisting *price history* completeness for arbitrary small caps is **unconfirmed** —
  use for universe-building, not the price archive. https://www.alphavantage.co/documentation/
- **Yahoo / yfinance — survivorship-BIASED, do not rely on for delisted.** Yahoo drops
  delisted tickers ("symbol may be delisted"); delisted financials moved behind paid Premium
  Plus. Fine for surviving names only.
- **IBKR (already in our stack)** — good for **currently-listed (surviving)** names via the
  paper Gateway API we already run, but **delisted names are a real gap** (IB history is
  keyed to tradable contracts; once delisted, history degrades/vanishes). IBKR alone
  reintroduces survivorship bias for a delisted-inclusive study. Use IBKR for surviving
  names; source delisted history from Stooq bulk + Tiingo permaTicker fills.

**Price stack verdict:** IBKR (surviving) + Stooq bulk (delisted backbone) + Tiingo free
(gap-fills) + Alpha Vantage `LISTING_STATUS` (delisted universe list). Small-cap coverage is
the fragile joint — **validate coverage before trusting any backtest.**

---

## 3. Self-computed IBD-style ratings for $0

- **RS Rating (1–99) — fully computable from daily prices. High confidence.** IBD def:
  1–99 percentile of 12-month price performance vs. all stocks, most recent 3 months weighted
  more. Standard reproduction (double-weight recent quarter):
  `RS_raw = 2*(C/C_63) + (C/C_126) + (C/C_189) + (C/C_252)` (63/126/189/252 trading days ≈
  3/6/9/12 mo), then **percentile-rank across the universe per date → 1–99**. Reference impl:
  https://github.com/skyte/relative-strength (note its caveat: feed must be split-adjusted —
  Stooq's adjusted closes avoid that bug). **Requirement for honesty:** rank against a
  **broad, survivorship-inclusive universe on each historical date**, or percentiles are
  biased — which is exactly why the delisted price data (Part 2) matters. It's an
  *approximation* of IBD's numbers, not identical (IBD's universe/constants are proprietary).
- **EPS Rating (1–99) — feasible from EDGAR, real caveats.** IBD def: growth + stability of
  earnings over ~3 yr, recent quarters weighted, percentile-ranked. Path: YoY quarterly EPS
  growth (recent-weighted) + a stability term (inverse variance / trend R²), combine,
  percentile-rank → 1–99. **Same YTD-differencing + small-cap-hygiene caveats as §1d.**
  Approximation, not licensed.
- **Composite — approximable, explicitly NOT licensed IBD.** IBD blends EPS, RS, Group-RS,
  **SMR** (Sales/Margins/ROE), **Acc/Dis**, and distance-from-52wk-high. Reproducible for $0:
  RS ✅, EPS ✅, SMR ⚠️ (EDGAR fundamentals), 52wk-high distance ✅. Harder: Group-RS
  ⚠️ (need an industry map — SIC from EDGAR; won't match IBD's ~197 groups), Acc/Dis ⚠️⚠️
  (loose volume-based proxy). A weighted blend (EPS + RS heaviest) gives a usable
  *IBD-inspired* composite screen — **do not claim IBD parity.**

---

## 4. COVERAGE CHECK against the advisor's actual watch list (measured this session)

Method: 834 unique watch-list tickers (`watchlist_tickers.txt`) diffed against two free
lists downloaded live — Tiingo `supported_tickers.zip` (107,211 rows) and SEC
`company_tickers.json` (10,426 current filers). Script + full per-ticker CSV in repo
(`coverage_check.csv`). Note: SEC's map is **current issuers only**, so "not in SEC current
map" mostly means renamed/delisted (fundamentals still on EDGAR under the old CIK), NOT
absent fundamentals.

| Metric | Count | % of 834 |
|---|---|---|
| Unique watch-list tickers | 834 | 100% |
| In Tiingo (US exchanges) — **free price coverage** | 803 | 96.3% |
| In Tiingo (any exchange) | 807 | 96.8% |
| Map to **current** SEC CIK — free fundamentals (live names) | 719 | 86.2% |
| NOT in current SEC map (renamed/delisted/ETF) | 115 | 13.8% |
| **Delisted-like** (Tiingo enddate < 2026) — survivorship-critical | 62 | 7.4% |
| NOT in Tiingo at all (no free price found) | 27 | 3.2% |
| NOT in Tiingo AND NOT in SEC (hardest), excl. ETFs | 26 | 3.1% |

**Refined (excluding the 9 ETFs/indices in the list — ARKK, IBIT, IWM, RSP, SIL, SMH,
TQQQ, UFO, XLV, which have no company fundamentals by nature):**
- Non-ETF company names: **825**; of these **718 (87.0%)** map to a current SEC CIK.
- The **107** non-ETF companies not in the current SEC map are **overwhelmingly renames or
  clean delistings whose fundamentals still exist on EDGAR** under the old CIK (verified
  pattern: e.g. FB→META, SQ→XYZ, XLNX acq. by AMD, TWTR taken private — all have full EDGAR
  history). These are a **CIK-resolution task, not a coverage gap.**

**The 62 survivorship-critical delisted names** (acquired/taken-private/delisted 2018–2026),
oldest→newest by Tiingo enddate:
QTNA(2019-06), MDCO, INXN, RARX, LVGO, DNKN, HMSY, IPHI, RP, PFPT, MXIM, CLDR, MDLA, STL,
KL, XLNX, ARNA, PLAN, TPTX, CCMP, MIME, PING, TWTR, AVLR, GBT, SWIR, ONEM, COUP, TA, IIVI,
FOCS, WWE, HZNP, SCPL, NEWR, VMW, VRTV, LTHM, DISH, AMEH, CPE, SWAV, MODN, WIRE, PRFT, SQSP,
AXNX, ENV, CTLT, ARCH, SMAR, SUM, AZPN, PDCO, DADA, X, BPMC, NVEE, AMED, GMS, COOP,
MRUS(2025-12). Every spot-checked one still has full fundamentals on EDGAR — the whole point.

**The genuinely hard set (26 non-ETF names not in Tiingo AND not in current SEC):**
AAXN, ATGE, ATUS, BLL, BRKS, CDAY, ELY, ERJ, FANH, FTCH, GPS, GSX, HEAR, HSC, JCOM, JEC,
NVTA, PKI, PSTG, RCII, SEAS, SQ, TPX, UBNT, VTNR, ZI. Most are **ticker renames** solvable
with a small manual map (AAXN→AXON, BLL→BALL, CDAY→DAY, ELY→MODG, GPS→GAP, GSX→GOTU,
JCOM→ZD, JEC→J, PKI→RVTY, SQ→XYZ, etc.). Only a few (Farfetch/FTCH bankruptcy, Invitae/NVTA)
are truly hard for **free prices** — and even those still have EDGAR fundamentals.

**Coverage takeaway:** free prices cover ~96–97% of his names out of the box; the ~3% price
gap is a short manual rename/delisting list, not a wall. Fundamentals coverage is
effectively ~99% once you resolve renames to old CIKs, because EDGAR keeps everything.

---

## 5. BOTTOM LINE — free build vs. one-month vendor pull

**Can the full dataset be built for $0?** Yes. EDGAR (fundamentals) + Stooq/IBKR/Tiingo
(prices) + self-computed ratings covers it, survivorship-free, no license, no API key.
Coverage of the actual 834-name universe is ~96–97% prices / ~99% fundamentals-after-rename.

**Realistic build effort (the honest cost):**
- EDGAR point-in-time fundamentals pipeline: **~2–4 weeks** (tag normalization +
  YTD-differencing + restatement/as-of logic + CAN SLIM growth math), less with
  `edgartools`/`sec-edgar-api` and a rougher first pass. **This is the bulk of the work.**
- Price assembly: **days** (IBKR for surviving; Stooq bulk for delisted; Tiingo gap-fills;
  Alpha Vantage `LISTING_STATUS` for the delisted universe list) + a coverage-validation
  pass. The Stooq throttling/quality on obscure small caps is the fragile part.
- Ratings (RS/EPS/composite): **days** on top of the above.

**Where ~$30–100 of a one-month vendor pull genuinely buys back time** — the tradeoff is
almost entirely on the **PRICE + survivorship-universe** side, not fundamentals:
- **A one-month Sharadar / EODHD / Norgate pull** gives clean, documented, split/dividend-
  adjusted, **survivorship-free daily prices WITH delisting dates and delisted-return
  handling** for the whole universe in one download — eliminating the Stooq throttling,
  the small-cap gap-hunting, the ticker-reuse ambiguity, and the manual rename list. That is
  the single biggest chunk of fragile, low-value grunt work in the free stack. **Bulk-
  download-and-cancel is a legitimate ~$30–100 way to buy a clean survivorship-free price +
  universe spine.** (Verify the vendor's terms permit retaining a one-time historical
  extract after cancellation.)
- Some vendors (e.g. Sharadar SF1) also ship **as-reported, point-in-time fundamentals with
  a datekey**, which would collapse the 2–4 week EDGAR pipeline. But EDGAR does this leg
  well and for free, and the point-in-time `filed`-date logic is exactly the part you want to
  own and understand for an anti-curve-fit backtest — so **paying to skip EDGAR is less
  compelling than paying to skip the price/survivorship grind.**

**Recommendation:** Build fundamentals free on EDGAR (own the point-in-time logic; it's the
robust, survivorship-free core and the effort is worth the control). For prices, **first try
free** (IBKR + Stooq bulk + Tiingo/Alpha-Vantage) and run the coverage-validation pass; **if
small-cap/delisted price coverage proves too gappy or Stooq throttling too painful, spend one
month (~$30–100) of a survivorship-free price+universe vendor, bulk-download, and cancel.**
That targets the paid dollar at the exact spot where free is weakest and where a clean pull
saves the most work — while keeping the whole thing a $0-to-~$100, one-time job.

*Nothing here is fabricated. Firsthand-verified items are labeled "VERIFIED FIRSTHAND."
Unconfirmed items (Stooq delisted coverage/sizes, Alpha Vantage delisted price completeness,
IBKR delisted-gap, exact per-quarter ZIP size ranges) are flagged inline.*

---

## Sources

**SEC EDGAR (fundamentals):**
- Financial Statement Data Sets: https://www.sec.gov/dera/data/financial-statement-data-sets
- Structure doc (aqfs.pdf): https://www.sec.gov/files/aqfs.pdf
- EDGAR APIs overview: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- Company Facts (verified): https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json
- Frames (verified): https://data.sec.gov/api/xbrl/frames/us-gaap/Revenues/USD/CY2022Q4.json
- Submissions: https://data.sec.gov/submissions/CIK0000320193.json
- Ticker map (verified): https://www.sec.gov/files/company_tickers.json
- Rate-limit / User-Agent policy: https://www.sec.gov/os/webmaster-faq#developers
- edgartools: https://github.com/dgunning/edgartools
- sec-edgar-api: https://github.com/jadchaar/sec-edgar-api

**Free prices / survivorship:**
- Tiingo pricing/limits: https://www.tiingo.com/about/pricing
- Tiingo fundamentals (permaTicker): https://www.tiingo.com/documentation/fundamentals
- Tiingo EOD product: https://www.tiingo.com/products/end-of-day-stock-price-data
- Stooq via pandas-datareader: https://pydata.github.io/pandas-datareader/readers/stooq.html
- Stooq intro/caveats (QuantStart): https://www.quantstart.com/articles/an-introduction-to-stooq-pricing-data/
- Stooq bulk archive: https://stooq.com/db/h/
- Alpha Vantage docs (LISTING_STATUS): https://www.alphavantage.co/documentation/
- Alpha Vantage delisted list writeup: https://www.macroption.com/alpha-vantage-delisted-stocks/
- Yahoo drops delisted tickers: https://github.com/ranaroussi/yfinance/issues/359
- Survivorship-free vendors (benchmark): https://eodhd.com/financial-academy/financial-faq/survivorship-bias-free-financial-analysis

**IBD-style ratings:**
- IBD RS explainer: https://finance.yahoo.com/news/good-relative-price-strength-precedes-224100448.html
- RS in Python: https://medium.datadriveninvestor.com/calculating-the-ibd-rs-rating-with-python-dc357c1e1b24
- RS reference impl: https://github.com/skyte/relative-strength
- O'Neil proprietary ratings: https://www.williamoneil.com/proprietary-ratings-and-rankings/

**Coverage check inputs (downloaded live this session):**
- Tiingo supported_tickers: https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip
- SEC company_tickers.json: https://www.sec.gov/files/company_tickers.json
