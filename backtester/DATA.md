# DATA.md — Data Download Specification

How the backtester gets its historical price data. Build src/download_data.py and src/data_loader.py to this spec.

## Source

Tiingo end-of-day adjusted prices, free tier. The API key is stored in the project's .env file as TIINGO_API_KEY=... Read it with python-dotenv / os.environ. NEVER hard-code the key, never print it, never commit .env.

Tiingo free tier allows ~50 unique symbols per hour, which comfortably covers our universe in a single run. If a rate limit is hit, the downloader should pause and report clearly, not crash.

## What to download

Use Tiingo's daily adjusted prices. We need the adjusted close (adjusted for splits AND dividends) so total return is correct — this matters especially for the bond and dividend-paying ETFs. Store the adjusted close as the price series each engine uses.

Ticker universe (download all of these):
- Equity core / benchmarks: SPY, VTI, RSP
- Sectors (optional satellite): XLC, XLY, XLP, XLE, XLF, XLV, XLI, XLB, XLRE, XLK, XLU
- T-bills / cash-like: SGOV, BIL
- Short Treasuries: SHY, VGSH
- Floating-rate Treasuries: USFR, TFLO
- Intermediate Treasuries: IEF
- Long Treasuries: TLT
- Gold: GLDM, IAU
- TIPS: SCHP, STIP
- Commodities: PDBC, DBC

(If any single ticker is unavailable on Tiingo's free tier, skip it with a clear warning and continue — do not abort the whole download.)

Date range: from 2010-01-01 (gives ample moving-average warm-up before the 2015 backtest floor) through today. Request the full available history per ticker; Tiingo returns from each ETF's inception, which is correct and expected — younger ETFs simply return shorter series.

## Also needed: Treasury yields and (optional) credit/vol

The duration engine needs the 10-year Treasury yield. Tiingo's free tier does not reliably provide this. Handle it this way:
- Build the downloader so the yield series comes from a separate, clearly-labeled function. First choice: the U.S. Treasury's published daily par-yield data (public, no key). If that is impractical in the first build, fall back to using a Treasury ETF trend (e.g. IEF/TLT price trend) as a PROXY for the yield-trend signal, clearly labeled as a proxy in code and in the report.
- VIX and credit spreads: if a clean free source isn't wired in the first build, use the documented proxies from SPEC.md (realized volatility of SPY for the vol signal; a HY-ETF-vs-Treasury ratio for the credit signal). Label proxies.

Do not block the whole build waiting for perfect macro data. Use labeled proxies, note them in the report, and leave clean TODO markers to upgrade the data later.

## Storage

- Save each downloaded series to data/ as Parquet (preferred) or CSV.
- One file per ticker, or one combined tidy file — your call, but document it.
- Store the raw download AND a data/_manifest.json recording: each ticker, its first and last available date, row count, download timestamp, and source.
- Once written, treat data/ as READ-ONLY. Re-downloading is an explicit action the user triggers, never a silent side effect of running a backtest.

## Data-quality checks (run after download, before any backtest)

Produce a short data-quality report (printed and saved) that flags:
- Missing dates / gaps within a ticker's active life.
- Zero or negative prices.
- Stale prices (same value many days running).
- Suspicious single-day moves (e.g. > 25% for a broad ETF) that may signal an unadjusted split.
- Each ticker's inception date (first available date), so the backtest's inception-aware logic has the truth to work from.
Do NOT run a backtest from a dataset with unresolved critical errors — surface them and stop.

## First run behavior

src/download_data.py run once should: read the key from .env, pull every ticker for the date range, write to data/, write the manifest, run the quality checks, and print a clear summary ("downloaded N tickers, earliest date X, flagged Y issues"). It should be safe to re-run (overwrite cleanly with a fresh manifest).
