# IBKR API capabilities for a nightly CAN SLIM / O'Neil small/mid-cap screen

Research date: 2026-07-01. Purpose: decide whether IBKR's API can REPLACE or supplement a
paid market-data + fundamentals vendor for an EOD/weekly growth-stock screen over ~4,000-6,000
US common stocks (lesser-known small/mid caps). Slow-moving strategy → nightly batch is fine.

---

## 0. Our existing stack (read-only review)

- `connections/connections/ibkr_paper.py` — single entry point to the **PAPER** Gateway. Uses the
  `ib_async` library (`from ib_async import IB, Stock`). Host `127.0.0.1`, port **4002** (paper).
  Default connection is **read-only** (`readonly=True`) — cannot transmit orders. Gateway is
  auto-launched via `C:\IBC-Paper\StartGatewayPaper.bat` with a `java_version=17` env workaround.
  `gateway_running()` already proves a data round-trip with `reqHistoricalData` on SPY.
- `connections/connections/clientids.py` — authoritative clientId registry. Highest ID in use
  is **40** (`paperbot_monitor`). A screener would take **id 41+**. `PAPER_PORT = 4002`;
  real-money port 4001 intentionally absent.
- Implication: scanner + historical pulls slot straight into this stack (`ib_async` exposes
  both; connect via `clientids` with a new registered id, keep `readonly=True`). No new library
  needed for scanner/history. **Fundamentals is the problem area — see §2.**

---

## 1. SCANNER API (reqScannerSubscription / reqScannerData / reqScannerParameters)

**Supported in ib_async?** Yes. `ib_async`/`ib_insync` expose `reqScannerData` /
`reqScannerDataAsync`, `reqScannerSubscription`, and `reqScannerParameters` /
`reqScannerParametersAsync`. A `ScannerSubscription` needs at minimum `instrument`,
`locationCode`, `scanCode`; filters go in `scannerSubscriptionFilterOptions` as a
`List[TagValue]`. This drops into our stack cleanly.

**Scan codes relevant to momentum/growth screening** (from IBKR docs; the *authoritative,
version-current* list must be pulled at runtime via `reqScannerParameters`, which returns an XML
blob of every scanCode + filter tag + valid instrument/location combo):
- `TOP_PERC_GAIN` — top % gainers
- `TOP_PERC_LOSE` — top % losers
- `HOT_BY_VOLUME` — highest volume vs. average (momentum/accumulation proxy)
- `TOP_OPEN_PERC_GAIN` — top % gain from the open
- `MOST_ACTIVE` — most active by volume
- `HIGH_VS_52W_HL` / `LOW_VS_52W_HL` (52-week-high proximity — a core CAN SLIM breakout trait;
  exact tag name is version-dependent, confirm via `reqScannerParameters`)
- `HIGH_OPT_IMP_VOLAT`, `HIGH_OPT_VOLUME_PUT_CALL_RATIO` — options-flow scans (not core to CAN SLIM)
- Plus many `COMBO_*` (complex-order) codes — not relevant here.

**Can scans be FILTERED by fundamental / technical criteria?** Partly.
- **Technical/price/volume filters exist as TagValues:** e.g. `priceAbove`/`priceBelow`
  (also the dedicated `abovePrice`/`belowPrice` fields on the subscription), `usdMarketCapAbove`
  / (market-cap-below equivalents), `avgVolumeAbove`, `optVolumeAbove`, plus change% and other
  tags. `usdMarketCapAbove` proves **market-cap banding is a first-class filter** — good for
  isolating small/mid caps.
- **Fundamental filters (EPS growth, sales growth, P/E, ROE) are NOT reliably exposed to the
  API scanner.** The TWS desktop scanner UI historically offered some fundamental columns, but
  the API-visible filter set is whatever `reqScannerParameters` returns for that Gateway version,
  and it is dominated by price/volume/technical tags, not O'Neil-grade fundamentals. **Do not
  assume EPS-growth / sales-growth filtering works via the API — it must be verified against the
  live `reqScannerParameters` XML, and historically it does not.** CAN SLIM's C, A, and part of
  I (earnings/sales growth, institutional sponsorship) will have to be computed by us from a
  fundamentals source, not filtered inside the scan.

**Result-per-scan limit and full-universe coverage:**
- **Hard cap: 50 rows per scan code, and only 10 API scans active at once.** (IBKR docs.)
- Workaround = partition the universe so no single partition exceeds 50 qualifying names, using
  `abovePrice`/`belowPrice` + `usdMarketCapAbove/Below` + `avgVolumeAbove` bands, and iterate.
- **Coverage-completeness caveat:** the 50-row cap returns the *top 50 by the scan's ranking*,
  not "all names in the band." To truly enumerate a universe you must slice finely enough that
  each slice holds ≤50 matches — and even then you are enumerating *rankable* names the scanner
  chooses to surface, not a clean master list. Realistically, to sweep ~4,000-6,000 US commons
  you would run on the order of **100-300+ partitioned scans** (e.g. ~15-30 price bands ×
  several market-cap bands × optionally sector/exchange), spread over the overnight window to
  respect the 10-concurrent / pacing limits. This is feasible overnight but is a blunt
  instrument: **the IBKR scanner is best used to RANK a pre-built ticker universe, not to be the
  authoritative source of the universe itself.** For a clean small/mid-cap common-stock master
  list you still want an external listing (e.g. an exchange/NASDAQ symbol file), then scan/pull
  per ticker.

---

## 2. FUNDAMENTAL DATA API — **THE BLOCKER**

**Headline finding (verify against your installed Gateway build):**
`reqFundamentalData`, `cancelFundamentalData`, `reqFundamentalsDataProtoBuf`,
`cancelFundamentalsDataProtoBuf`, the `fundamentalData` callback, and the
`FUNDAMENTAL_RATIOS` tick type (47) were **REMOVED in TWS API v10.47** (2026 production release,
release note dated 2026-05-29). The API fundamental-data documentation is now marked deprecated
and points users to IBKR Campus with no API replacement/migration path.

Context that determines whether this bites us *today*:
- IBKR raised the **minimum supported TWS/Gateway to 10.30 in March 2025**, and keeps pushing the
  floor up. So the window for pinning a pre-10.47 Gateway that still exposes `reqFundamentalData`
  is **open now but shrinking**, and will eventually be force-upgraded away. Building a
  production screen on a soon-removed API is a dead end.
- **Action to confirm:** check the version of the Gateway our paper stack runs. If it is
  < 10.47, `reqFundamentalData` may still return data *for now*; if ≥ 10.47, it is gone. Either
  way, treat IBKR API fundamentals as **not a durable foundation.**

**What the API fundamentals *were* (pre-removal), for reference / if pinned to an old build:**
- **Source:** Refinitiv (formerly Thomson Reuters) "Reuters Worldwide Fundamentals."
- **Report types** (via `reportType` string): `ReportsFinSummary` (financial summary),
  `ReportSnapshot` (company overview), `ReportRatios` (ratios), `ReportsFinStatements`
  (full statements), `ReportsOwnership` (ownership/holders — the CAN SLIM "I" input), `RESC`
  (analyst estimates), `CalendarReport` (event calendar). Returns XML.
- **Fields available:** ~125 financial-statement indicators + ~20 estimate metrics — EPS
  (several variants), revenue, gross/operating margins, net income, ROE, ROA, book value/share,
  cash-flow/share, shares outstanding, debt/equity, plus `ReportsOwnership` for institutional
  holders. This is enough raw material to compute CAN SLIM C (current quarterly earnings), A
  (annual earnings), sales growth, and I (institutional sponsorship) — *if the feed still worked.*
- **Point-in-time?** The underlying Reuters Worldwide dataset **is point-in-time** (statements
  carry a `SourceDate`/filing date; estimates carry an `UpdatedDate`), and includes restatements.
  **BUT two caveats gut this for honest backtesting:**
  1. **Depth is a rolling window, not a fixed history:** ~5-6 years annual, ~6-8 quarters
     quarterly, collected from the time you pull it — no deep archive, and it does **not** go
     back to a fixed start date.
  2. When a restatement exists, "the corresponding as-reported statement is not provided" — so
     you cannot always reconstruct exactly what a screener would have seen on a past date.
  Net: even at its best it supported *shallow* point-in-time work, not a multi-year survivorship-
  and-restatement-clean backtest. And it is being removed regardless.
- **Small/mid-cap coverage:** Refinitiv coverage spans US NYSE/NASDAQ/AMEX/OTC and is broadly
  decent, but **documented gaps for small/mid/OTC names are common** — thinly-covered small caps
  frequently returned Error 430 ("fundamentals data … not available"). For a screen whose whole
  point is *lesser-known* small/mid caps, this coverage tail is exactly where IBKR was weakest.

**Alternative inside IBKR:** Wall Street Horizon (`reqWshMetaData`/`reqWshEventData`) still
exists but is an **event calendar** (earnings dates, dividends, splits, spinoffs), **not**
financial-statement fundamentals — it cannot supply EPS/sales-growth/ROE. It also needs its own
research subscription. It does not fill the CAN SLIM fundamentals gap.

**Bottom line for §2:** IBKR cannot be relied on for the fundamentals half of a CAN SLIM screen.
Live screening loses the API feed at 10.47; backtesting was never adequately deep/clean even
before removal. **A fundamentals vendor is required.**

---

## 3. HISTORICAL PRICE DATA (reqHistoricalData) — solid

**Pacing limits (IBKR "Historical Data Limitations"):**
- **≤ 60 historical requests per rolling 10 minutes** (BID_ASK counts double).
- No **6+ requests for the same contract within 2 seconds**; no **2 identical requests within
  15 seconds**.
- **≤ ~2,000 bars per request** as the practical assembly target; for bar sizes ≥ 1 min the old
  hard duration caps were lifted, but IBKR applies a **soft load-balancing limit** — request too
  much too fast and it throttles / disconnects the client. (Sub-30s bars older than 6 months are
  unavailable; irrelevant to us — we want daily bars.)
- These limits are enforced broker-wide and cannot be raised.

**Feasibility for 3,000-6,000 tickers, 1-2 years of daily bars, overnight:**
- 1-2 years of **daily** bars = ~250-500 bars per ticker = **one request per ticker** (well under
  the 2,000-bar cap). So the binding constraint is the 60-requests / 10-min rate.
- 60 req / 10 min = **360 tickers/hour** as the ceiling. Practically pace ~1 request every
  ~10-11s to stay safe.
- 6,000 tickers ÷ 360/hr ≈ **~17 hours** at the strict cap; 3,000 ≈ **~8-9 hours**.
  → 3,000 names fit a single overnight window comfortably; **6,000 does NOT fit one night** at the
  strict pace. Mitigations: (a) run across **two clientIds/sessions** to roughly double throughput
  (each API client gets its own pacing budget, though the account-level data farm can still push
  back), (b) only refresh the full universe **weekly** and do incremental daily top-ups (append
  the latest bar only), or (c) split the universe across two nights. All three fit an EOD strategy.
- **Reliability caveats:** the **historical data farm** connection (`hmds`) drops/reconnects
  intermittently ("HMDS data farm connection … broken/OK" messages); a nightly batch MUST have
  retry-with-backoff, farm-reconnect handling, and resumability (checkpoint which tickers are
  done). This matches our house liveness rubric — treat it as a supervised long op.

**Delayed / frozen / EOD data:** For daily bars we do **not** need real-time. Historical daily
bars come from IBKR's historical store, not the live feed, so delayed status is largely moot for
the *history* pull. Note IBKR **no longer offers delayed quotes on US equities** at all — but
that affects live quotes, not the historical daily-bar endpoint we'd use. Historical daily OHLCV
does require the account to be entitled/funded (see §4) but not a premium real-time tier.

---

## 4. MARKET-DATA SUBSCRIPTIONS / FEES

- **Funded live account required.** "Demo accounts cannot subscribe to data." A pure paper/demo
  login has **no market-data entitlements**, so scanner + historical pulls that need US-equity
  data won't work on an unfunded paper account. A funded account (typically ≥ $500) that also has
  paper access is the practical setup; entitlements attach to the funded live account and the
  linked paper account can then use them for data.
- **US equity real-time bundle:** the relevant retail subscription is roughly the **US Securities
  Snapshot & Futures Value Bundle (~$10/mo, waived with ≥ $30/mo commissions)** for consolidated
  US equity data; a full network (NYSE/Nasdaq/etc.) real-time entitlement costs more per exchange.
  For an **EOD** screen you do **not** need the premium real-time exchange feeds — you need enough
  entitlement to run the scanner and pull historical daily bars, which the value bundle / basic
  US equity entitlement covers.
- **Scanner:** requires the account be entitled to the underlying US equity data; runs off the
  same US-equity entitlement, no separate "scanner fee."
- **Fundamentals:** was a Refinitiv entitlement — **now removed from the API at 10.47** (§2), so
  there is nothing to subscribe to for API fundamentals going forward regardless of fee.
- **Historical daily bars:** covered by the US-equity market-data entitlement; no separate
  historical-data fee for daily equity bars.
- **Snapshots:** $0.01/US-equity snapshot request, 100 free/month — irrelevant to a batch history
  pull but worth noting if we ever want cheap on-demand quotes.
- IBKR **no longer offers delayed US-equity quotes**, so "use free delayed data" is not an option
  for live US-equity quotes; the historical daily-bar path (what we need) still works under the
  normal entitlement.

**Net cost picture:** roughly **$0-$10/month** of IBKR data fees (often $0 if commissions waive
the value bundle) covers scanner + historical daily bars for the full US universe on a funded
account with paper access. That's cheap — but it does **not** include fundamentals.

---

## 5. BOTTOM LINE

**Live nightly scan (price/technical half): YES, IBKR can carry it.**
Scanner + `reqHistoricalData` are both in `ib_async`, slot into our existing paper-Gateway stack
(new clientId 41+, keep read-only), and the pacing limits are compatible with an overnight batch.
Use the scanner to RANK/pre-filter a universe you supply (price/volume/market-cap banding, top-%
-gain, hot-by-volume, 52-wk-high proximity) and `reqHistoricalData` for the OHLCV needed to
compute relative strength, base/breakout, and moving-average logic. Full 6,000-name daily history
needs either two sessions, a weekly-full + daily-incremental scheme, or a two-night split — all
fine for an EOD strategy. Cost ≈ $0-$10/mo on a funded account with paper access.

**Fundamentals half of the live screen (CAN SLIM C, A, sales growth, I): NO.**
`reqFundamentalData` was **removed in TWS API 10.47** (2026-05-29 production notes; also killed
tick type 47 / `FUNDAMENTAL_RATIOS`). IBKR keeps raising the minimum Gateway version (floor 10.30
since Mar 2025), so any pre-10.47 workaround is temporary. Even before removal, small/mid/OTC
coverage was patchy (frequent Error 430 on exactly the lesser-known names this strategy targets).
The scanner's API filter set does not reliably expose EPS-growth / sales-growth / ROE filters
either. **You need an external fundamentals vendor for earnings, sales, margins, ROE, and
institutional-ownership.**

**Backtest: NO — needs a vendor.**
Two independent reasons: (1) fundamentals are being removed from the API, and (2) even the old
Refinitiv feed gave only a **shallow rolling window** (~5-6yr annual / 6-8q quarterly) that does
not reach a fixed start date and drops as-reported statements when restatements exist — not deep
or clean enough for an honest, survivorship- and restatement-aware CAN SLIM backtest. IBKR daily
**price** history is fine for the price/RS side of a backtest, but the fundamental point-in-time
inputs must come from a vendor with a real historical archive (and you must handle
delisted/survivorship names, which IBKR's live symbol set won't give you).

**What still needs a paid vendor, by phase:**
- **Live nightly screen:** fundamentals (EPS/sales growth, margins, ROE) + institutional
  ownership + a clean small/mid-cap common-stock master universe (incl. delistings). Price/volume
  and ranking → IBKR.
- **Backtest:** point-in-time fundamentals with deep history *and* survivorship-free universe →
  vendor. Daily price history → IBKR can contribute, but a vendor that already bundles clean,
  split/dividend-adjusted, survivorship-free daily prices with the fundamentals is usually simpler
  than stitching IBKR prices to vendor fundamentals.

**Recommendation:** Use IBKR as the **execution + price/scan-ranking layer** (it's already wired,
cheap, and paper-safe). Do **not** try to make IBKR the fundamentals or backtest-history source —
that's where a paid fundamentals vendor (with point-in-time history and a survivorship-free
small/mid-cap universe) is non-negotiable for a curve-fit-honest CAN SLIM system.

---

## Items I could NOT fully confirm from docs (flagged, not fabricated)
- The **exact** current API scanCode names / filter tags for 52-wk-high proximity and any
  fundamental filters — these are version-specific and only authoritatively knowable by calling
  `reqScannerParameters` against our live Gateway and parsing the XML. Do this before designing
  partitions.
- Whether a **second concurrent clientId** meaningfully doubles historical throughput in
  practice, or whether the account-level data-farm soft limit caps it anyway — needs an empirical
  test against our Gateway.
- The precise US-equity data entitlement SKU/price that minimally enables the scanner + daily
  history for a *broad* universe on our specific funded-account configuration — confirm in
  Account Management (fees vary by whether commissions waive the bundle).
- The exact version of the Gateway our paper stack currently runs (determines if
  `reqFundamentalData` still returns anything today). Verify locally; do not assume.

---

## Sources
- TWS API — Market Scanners: https://interactivebrokers.github.io/tws-api/market_scanners.html
- TWS API — Scanner Parameters: https://interactivebrokers.github.io/tws-api/scanner_parameters.html
- TWS API — ScannerSubscription class: https://interactivebrokers.github.io/tws-api/classIBApi_1_1ScannerSubscription.html
- IBKR Campus — TWS Python API Market Parameters and Scanners: https://www.interactivebrokers.com/campus/trading-lessons/tws-python-api-market-parameters-and-scanners/
- IBKR Campus — reqScannerParameters and reqScannerSubscription: https://www.interactivebrokers.com/campus/ibkr-quant-news/ibkr-api-scanners/
- TWS API — Fundamental Data (now deprecated): https://interactivebrokers.github.io/tws-api/fundamentals.html
- TWS 2026 API Production Release Notes (reqFundamentalData removed, v10.47, 2026-05-29): https://www.ibkrguides.com/releasenotes/prod-2026.htm
- QuantRocket — Reuters Worldwide Fundamentals Data Guide (fields, point-in-time, depth, coverage): https://docs-2-0--quantrocket.netlify.app/data/reuters/
- ib_insync API docs (reqFundamentalDataAsync, reqScannerDataAsync, report types): https://ib-insync.readthedocs.io/api.html
- ib_insync scanners notebook: https://github.com/erdewit/ib_insync/blob/master/notebooks/scanners.ipynb
- TWS API — Historical Data Limitations (pacing): https://interactivebrokers.github.io/tws-api/historical_limitations.html
- TWS API — Historical Bar Data: https://interactivebrokers.github.io/tws-api/historical_bars.html
- IBKR Campus — Market Data Subscriptions: https://www.interactivebrokers.com/campus/ibkr-api-page/market-data-subscriptions/
- IBKR — Market Data Pricing: https://www.interactivebrokers.com/en/pricing/market-data-pricing.php
- IBKR KB — US Market Data subscription considerations: https://www.ibkrguides.com/kb/subscription-consideration-us-market-data.htm
- MultiCharts — IBKR historical data pacing violations (community corroboration): https://www.multicharts.com/trading-software/index.php?title=Interactive_Brokers_Pacing_Violation
- TWS API Changelog (min version 10.30 since Mar 2025): https://www.interactivebrokers.com/campus/ibkr-api-page/tws-api-changelog-2/
