# IBKR API capabilities — canonical reference

> **FIRST STOP before sourcing any market / fundamental / scanner / news data externally.
> IBKR is already paid for, the API is live, and it drops straight into our paper-Gateway
> stack (`connections/ibkr.py`, read-only, a registered clientId). Check what IBKR provides
> HERE before reaching for an outside vendor. See memory rule `ibkr-first-data-sourcing`.**

`Last verified: 2026-07-01` (human research — see `Sources` at the bottom of the research spike)
`Last introspected (machine): 2026-07-01` (updated by `refresh_ibkr_capabilities.py`)

This doc is the durable answer to "can IBKR give us X, or do we have to pay a vendor?" It is
kept honest two ways: (1) human research (the `Last verified` line), and (2) a monthly
machine introspection run that pulls the *actual* scanner parameters off our live Gateway
(the `Last introspected` line). When the two disagree, the machine line wins for scan
codes/filters — IBKR changes those over time.

---

## VERDICT / BOUNDARY — what IBKR CAN and CANNOT do

| Need | IBKR? | Endpoint / note | If NO → vendor for |
|------|-------|-----------------|--------------------|
| **Scanner / ranking** (top %-gain, hot-by-volume, 52wk-high proximity, market-cap / price / volume banding, **+ fundamental filters: EPS/sales growth, ROE, margins, P/E, institutional %** — machine-confirmed) | **YES** | `reqScannerParameters` / `reqScannerData`; 50 rows/scan, 10 concurrent; see Scanner section for the fundamental-filter tag list | — |
| **Historical daily bars** (OHLCV, split/div-adjusted) | **YES** | `reqHistoricalData`; ~60 req / 10 min | — |
| **Historical intraday bars** (1-min … 1-hour) | **YES** | `reqHistoricalData`; sub-30s bars only ≤ 6 months back | — |
| **Real-time / snapshot quotes** | **YES** | `reqMktData` / snapshots; needs entitlement; NO delayed US-equity quotes anymore | — |
| **News** (headlines + bodies from subscribed providers) | **YES** | `reqNewsProviders` / `reqNewsBulletins` / `reqHistoricalNews` | — |
| **Account / positions / executions** | **YES** | `reqAccountSummary`, `reqPositions`, `reqExecutions` | — |
| **Event calendar** (earnings dates, dividends, splits, spinoffs) | **YES** (extra sub) | Wall Street Horizon `reqWshMetaData` / `reqWshEventData` — dates only, NOT statements | — |
| **Fundamentals: EPS / sales growth, ROE, margins, net income, book value** | **NO** | `reqFundamentalData` **REMOVED in TWS API v10.47 (2026-05-29)**; tick type 47 `FUNDAMENTAL_RATIOS` also gone | external fundamentals vendor |
| **Institutional ownership** | **NO** | was `ReportsOwnership` under `reqFundamentalData` — removed with the rest | external fundamentals vendor |
| **Point-in-time / as-reported historical fundamentals** (for honest backtests) | **NO** | even pre-removal it was a shallow rolling window (~5-6 yr annual / 6-8 q quarterly), dropped as-reported statements on restatement, no fixed-start archive | external point-in-time vendor |
| **Survivorship-free universe incl. delistings** | **NO** | IBKR only exposes its live tradable symbol set | external universe/master-list |

**Small/mid-cap coverage caveat.** Even before removal, IBKR fundamentals (Refinitiv-sourced)
had **documented gaps on small / mid / OTC names** — thinly-covered small caps frequently
returned **Error 430 ("fundamentals data … not available")**. That coverage tail is exactly
where a *lesser-known* small/mid-cap strategy needs data most. Do not assume IBKR fundamental
or scanner coverage of obscure names; verify per-ticker.

**One-line rule of thumb:** IBKR is the **execution + price/scan-ranking + news** layer. It is
**not** the fundamentals source and **not** the backtest-history source. For EPS/sales growth,
ROE, margins, institutional ownership, and point-in-time history → a paid vendor is required.

---

## Scanner (`reqScannerParameters` / `reqScannerData`)

- **In `ib_async`?** Yes — `reqScannerData` / `reqScannerDataAsync`, `reqScannerSubscription`,
  `reqScannerParameters` / `reqScannerParametersAsync`. Drops into our stack cleanly.
- **How it works.** A `ScannerSubscription` needs at minimum `instrument`, `locationCode`,
  `scanCode`. Filters go in `scannerSubscriptionFilterOptions` as a `List[TagValue]`.
  `reqScannerParameters()` returns an **XML blob** enumerating every scanCode, filter tag, and
  valid instrument/location combo for *this Gateway version* — that XML is the authoritative,
  version-current list, and it is what `refresh_ibkr_capabilities.py` snapshots and diffs.
- **Momentum / growth scan codes** (from IBKR docs; confirm exact current names via the machine
  snapshot): `TOP_PERC_GAIN`, `TOP_PERC_LOSE`, `TOP_OPEN_PERC_GAIN`, `HOT_BY_VOLUME`,
  `MOST_ACTIVE`, 52-week-high/low proximity (`HIGH_VS_52W_HL` / `LOW_VS_52W_HL` — a core
  breakout trait; tag name is version-dependent). Options-flow scans exist
  (`HIGH_OPT_IMP_VOLAT`, put/call-ratio) but aren't core to a growth screen.
- **Filters that DO exist (TagValues):** `priceAbove` / `priceBelow` (also `abovePrice` /
  `belowPrice` fields), `usdMarketCapAbove` (+ below equivalent — market-cap banding is
  **first-class**, good for isolating small/mid caps), `avgVolumeAbove`, `optVolumeAbove`,
  change-% and other technical tags.
- **Fundamental FILTER / RANK tags — MACHINE-CONFIRMED PRESENT (2026-07-01 introspection).**
  The seed research warned these might not exist via the API; the live Gateway proves otherwise.
  Our v-current params XML (495 scan codes, 1,120 filter tags) exposes O'Neil-grade fundamental
  filters and scan-codes, largely Lipper-sourced:
  - **EPS growth:** `LIPPER_EPS_GRWTH_1YR/3YR/5YR`, `AV5YREPS`, `EPS_CHANGE_TTM` +
    `epsChangeTTMAbove/Below` range filter; scan codes `SCAN_lipperEPSGrowth{1,3,5}yr_*`,
    `SCAN_epsChangeTTM_*`.
  - **Sales / revenue growth:** `LIPPER_SALES_GRWTH_1YR/3YR/5YR`, `REV_GROWTH_RATE_5Y`,
    `LIPPER_SALES_PER_SHARE_GROWTH_*`; scan codes `SCAN_lipperSalesGrowth{1,3,5}yr_*`,
    `SCAN_revGrowthRate5Y_*`.
  - **ROE:** `LIPPER_ROE_1YR/3YR`, `AVFYCURROE`. **Margins:** `NET_PROFIT_MARGIN_TTM`,
    `OPERATING_MARGIN_TTM` (+ `SCAN_netProfitMarginTTM_*`, `SCAN_operatingMarginTTM_*`).
  - **Valuation:** `AVFWD_PE`, `AVFYCURPE`, `AVFYCURPEG`, `PRICE2BK`, `LIPPER_PRICE_2_SALES`;
    scan codes `HIGH_PE_RATIO`, `LOW_PE_RATIO`, `HIGH_GROWTH_RATE`, `LOW_GROWTH_RATE`.
  - **Institutional sponsorship (CAN SLIM "I"):** `INSTITUTIONALOFFLOATPERC`,
    `NUMSHARESINSTITUTIONAL`; scan codes `SCAN_iiInstitutionalOfFloatPerc_*`,
    `SCAN_iiNumSharesInstitutional_*`.

  **BUT this does NOT resurrect fundamentals as a data SOURCE.** Scanner tags let you
  filter/rank a universe by these metrics; they do **not** hand you the underlying EPS/sales/ROE
  time series (that was `reqFundamentalData`, removed at 10.47 — see below), they carry the
  50-row-per-scan cap, and coverage on lesser-known small/mid/OTC names is unproven (verify
  per-ticker). Treat these as a *ranking/screening* lever, not the fundamentals feed and not a
  point-in-time backtest source. Exact tag names above are machine-current; re-diff via
  `refresh_ibkr_capabilities.py` after any Gateway upgrade.
- **Limits:** **50 rows per scan code, 10 API scans active at once** (broker-wide, cannot be
  raised). The 50-row cap returns the *top 50 by the scan's ranking*, not "all names in the
  band."
- **Full-universe sweep (partition strategy):** slice finely enough that no partition holds
  >50 qualifying names, using `abovePrice`/`belowPrice` × `usdMarketCapAbove/Below` ×
  `avgVolumeAbove` (× optionally sector/exchange), and iterate across the overnight window
  respecting the 10-concurrent / pacing limits. Sweeping ~4,000-6,000 US commons this way runs
  on the order of **100-300+ partitioned scans**. Feasible but blunt: **the scanner is best
  used to RANK a pre-built ticker universe, not to be the authoritative source of the universe.**
  For a clean small/mid-cap common-stock master list (incl. delistings) you still need an
  external listing.

## Historical bars (`reqHistoricalData`)

- **Pacing (broker-wide, cannot be raised):** ≤ **60 historical requests per rolling 10 min**
  (BID_ASK counts double); no 6+ requests for the same contract within 2 s; no 2 identical
  requests within 15 s; ≤ ~2,000 bars/request as the practical assembly target. A **soft
  load-balancing limit** throttles/disconnects clients that request too much too fast.
- **Throughput math:** 60 req / 10 min = **360 tickers/hour** ceiling; pace ~1 req / ~10-11 s
  to stay safe. 1-2 yr of **daily** bars = ~250-500 bars = **one request per ticker**.
  → 3,000 names ≈ 8-9 h (fits one overnight window); **6,000 does NOT fit one night** at the
  strict pace. Mitigations: two clientIds/sessions (each gets its own pacing budget, though the
  account-level data farm can still push back), weekly-full + daily-incremental top-ups, or a
  two-night split. All fine for an EOD strategy.
- **HMDS flakiness / retry:** the historical data farm (`hmds`) drops/reconnects intermittently
  ("HMDS data farm connection … broken/OK"). A nightly batch MUST have retry-with-backoff,
  farm-reconnect handling, and resumability (checkpoint which tickers are done) — a supervised
  long op per the house liveness rubric.
- **Delayed status:** historical daily bars come from IBKR's historical store, not the live
  feed, so delayed status is largely moot for the *history* pull. (IBKR no longer offers delayed
  US-equity quotes at all, but that only affects live quotes.)

## Subscriptions & fees

- **Funded live account required.** Demo/paper-only logins have **no market-data entitlements**;
  scanner + historical pulls that need US-equity data won't work unfunded. The practical setup
  is a **funded account (typically ≥ $500) with a linked paper account** — entitlements attach
  to the funded live account and the paper account uses them for data.
- **US-equity bundle:** the retail sub is roughly the **US Securities Snapshot & Futures Value
  Bundle (~$10/mo, waived with ≥ $30/mo commissions)** for consolidated US-equity data. An EOD
  screen does **not** need premium per-exchange real-time feeds — just enough to run the scanner
  and pull daily bars, which the value bundle / basic US-equity entitlement covers.
- **Scanner** runs off the same US-equity entitlement — no separate scanner fee.
- **Historical daily bars** are covered by the US-equity entitlement — no separate historical fee.
- **Snapshots:** $0.01/US-equity snapshot, 100 free/month — irrelevant to batch history, handy
  for cheap on-demand quotes.
- **Net picture:** roughly **$0-$10/month** covers scanner + historical daily bars for the full
  US universe on a funded account with paper access (often $0 if commissions waive the bundle).
  Cheap — but it does **not** include fundamentals.

## Fundamentals — REMOVED (the blocker)

`reqFundamentalData`, `cancelFundamentalData`, `reqFundamentalsDataProtoBuf`,
`cancelFundamentalsDataProtoBuf`, the `fundamentalData` callback, and tick type 47
(`FUNDAMENTAL_RATIOS`) were **REMOVED in TWS API v10.47** (2026 production release, note dated
**2026-05-29**). The API fundamental-data docs are now deprecated with **no API replacement or
migration path**. IBKR keeps raising the minimum Gateway floor (10.30 since Mar 2025), so any
pre-10.47 pin that still returns fundamentals is temporary and will be force-upgraded away.

Pre-removal it was **Refinitiv "Reuters Worldwide Fundamentals"** — ~125 statement indicators +
~20 estimate metrics (EPS variants, revenue, margins, net income, ROE/ROA, book value/share,
shares outstanding, debt/equity) plus `ReportsOwnership` for institutional holders. Enough raw
material to compute CAN SLIM C/A/I *if it still worked*. It was **point-in-time but shallow**
(~5-6 yr annual / 6-8 q quarterly rolling from pull time, no fixed-start archive; dropped the
as-reported statement when a restatement existed) and had the small/mid/OTC coverage gaps noted
above. **Not a durable foundation for live screening and never adequate for an honest backtest.**

Wall Street Horizon (`reqWshMetaData` / `reqWshEventData`) still exists but is an **event
calendar** (earnings dates, dividends, splits, spinoffs), NOT financial statements — it needs
its own research sub and does not fill the fundamentals gap.

---

## Known unknowns — confirm via live introspection or Andrew

- **Exact current `scanCode` / filter-tag names** — RESOLVED 2026-07-01 by the first machine
  snapshot (495 scan codes, 1,120 filter tags in `capabilities/ibkr_scanner_params_20260701.xml`).
  It surfaced fundamental filters the seed research doubted (see Scanner section). Re-diff after
  any Gateway upgrade with `refresh_ibkr_capabilities.py`.
- **Our Gateway's current version** — determines whether `reqFundamentalData` still returns
  anything today (< 10.47 = maybe; ≥ 10.47 = gone). Verify locally.
- **Our exact market-data entitlement SKU** — the precise US-equity data package that minimally
  enables the scanner + broad daily history on our specific funded-account config. Confirm in
  Account Management (price varies by whether commissions waive the bundle).
- **The market-data subscriptions Andrew actually pays for — CONFIRM WITH ANDREW.** This doc
  states what IBKR *can* provide; which entitlements are *live on our account right now* is not
  machine-introspectable cleanly and must be confirmed with Andrew.

---

## Changelog

- **2026-07-01** — machine truth vs research: the live Gateway's scanner DOES expose fundamental
  filter/rank tags (Lipper EPS/sales growth 1/3/5yr, ROE, net/operating margins, P/E, PEG, P/S,
  institutional-ownership %) — the seed research had doubted these existed via the API. Scanner
  section + VERDICT table corrected. Note: this is a filter/rank lever only, NOT a fundamentals
  data source or point-in-time backtest feed (`reqFundamentalData` still removed at 10.47).
- **2026-07-01** — first machine introspection — 495 scan codes, 1120 filter tags snapshotted to capabilities/ibkr_scanner_params_20260701.xml

- **2026-07-01** — initial doc created from capabilities research (`connections/refresh_ibkr_capabilities.py`
  added as the monthly machine-introspection updater; clientId 41 `capabilities_introspect` reserved).
