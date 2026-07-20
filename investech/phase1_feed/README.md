# Phase-1 Feed -- Public Market-Risk Metrics

A small daily desk overlay. It pulls **valuation & concentration** metrics from
**public sources only**, assembles one daily snapshot row, and appends it to a
CSV time series. No InvesTech scraping -- that is prohibited and this project
contains zero InvesTech URLs.

It is built to **run with the standard library alone** and to **degrade
gracefully**: if a source needs an API key or the network is down, that one
metric reports its status and the run still completes for the others.

## Metrics & sources

| Metric                              | Source                                                              | Frequency      | Notes |
|-------------------------------------|---------------------------------------------------------------------|----------------|-------|
| S&P 500 trailing P/E                | multpl.com `/s-p-500-pe-ratio` (HTML scrape)                        | daily          | Fragile: depends on page markup |
| CAPE / Shiller P/E                  | multpl.com `/shiller-pe` (HTML scrape)                             | daily/monthly  | Default path. Optional: Shiller `ie_data.xls` (legacy .xls, needs `xlrd==1.2.0`) |
| Buffett Indicator                   | FRED `WILL5000IND` / `GDP`                                          | daily / quarterly | Needs `FRED_API_KEY` |
| US household equity allocation      | FRED Z.1 `BOGZ1LM153064105Q` / `BOGZ1FL154090005Q`                 | quarterly      | Equities held / total household financial assets. Needs `FRED_API_KEY` |
| Top-10 S&P 500 concentration        | State Street **SPY** daily holdings `.xlsx` (sum of top-10 weights); falls back to iShares **IVV** CSV | daily | Proxy for the "Gorilla Index" concentration read |

All source URLs and FRED series IDs are centralized in `config.py`.

## How to run

```bash
cd phase1_feed
# (optional) set the FRED key for the two FRED-backed metrics:
#   PowerShell:  $env:FRED_API_KEY = "your_key_here"
#   bash:        export FRED_API_KEY=your_key_here
python main.py
```

On first run it creates `data/` and `data/metrics_daily.csv`. Each run prints a
table to stdout and appends/overwrites **today's** row (de-duplicated by date,
so same-day re-runs replace rather than duplicate).

## Required env vars

| Var            | Used by                              | How to get it |
|----------------|--------------------------------------|---------------|
| `FRED_API_KEY` | Buffett Indicator, HH equity alloc.  | Free: create an account at <https://fred.stlouisfed.org/>, then request a key at <https://fred.stlouisfed.org/docs/api/api_key.html>. Keep it in your shell env; **never commit it**. |

No secrets are stored in code. If `FRED_API_KEY` is unset, the two FRED metrics
return `status="needs_api_key"` and the run continues.

## Dependencies

One package: **`openpyxl`** (reads the SPY holdings `.xlsx` for the concentration
metric). Everything else is the Python standard library (`urllib`, `csv`,
`json`, `re`). If `openpyxl` is absent, the concentration fetcher falls back to
the iShares IVV CSV and the rest of the run is unaffected. See
`requirements.txt` for optional upgrades (`requests`, and `xlrd==1.2.0` if you
choose to read Shiller's `.xls` directly).

## Status / TODO (honest)

- **Buffett Indicator** -- fully wired against FRED; just needs `FRED_API_KEY`.
- **HH equity allocation** -- fully wired against FRED; needs `FRED_API_KEY`.
  The chosen series are defensible but worth a sanity check against the Fed's
  published Z.1 "households" tables; series IDs can be swapped in `config.py`.
- **S&P 500 trailing P/E** -- works but is a **fragile HTML scrape**; a multpl
  layout change will break the regex. TODO: add an index-data fallback.
- **CAPE / Shiller P/E** -- same fragile-scrape caveat. The "proper" source is
  Shiller's `ie_data.xls`, but that is a **legacy .xls** that the stdlib and
  `openpyxl` (xlsx-only) cannot read; it requires `xlrd==1.2.0`. Wiring that
  reader in is a documented TODO; default path scrapes multpl.com.
- **Top-10 concentration** -- primary source is the public State Street **SPY**
  holdings `.xlsx` (read via openpyxl); confirmed working in-environment. Falls
  back to the iShares **IVV** CSV if SPY fails. Note: in this environment iShares
  served an HTML consent page instead of the CSV to a bare client, which is
  exactly why SPY is primary. Most likely break point is a layout change in the
  SPY sheet (header row / "Weight" column name).
- **CSV schema** -- includes a `<metric>_status` column beside each value so a
  blank value is always explainable (key missing vs. scrape error).
