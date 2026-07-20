"""
Central configuration for the Phase-1 public market-risk metrics feed.

All source URLs and FRED series IDs live here so they can be reviewed/adjusted
in one place. No secrets in this file -- the FRED API key is read from the
FRED_API_KEY environment variable by the fetchers.
"""

# --- FRED (Federal Reserve Economic Data) -----------------------------------
# Free API key: https://fred.stlouisfed.org/docs/api/api_key.html
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# Buffett Indicator = total US corporate equity market value / GDP.
# The Wilshire 5000 FRED series (WILL5000IND etc.) are discontinued, so we use
# the Z.1 Financial Accounts market-value-of-equities series instead.
#   NCBEILQ027S = Nonfinancial Corp Business; Corporate Equities; Liability
#                 (market value, MILLIONS of $, QUARTERLY)
#   GDP         = Nominal GDP (BILLIONS of $, quarterly)
# Formula: 100 * (NCBEILQ027S / 1000) / GDP  -> percent of GDP (~218%).
FRED_BUFFETT_NUM_SERIES = "NCBEILQ027S"   # equities mkt value, $MM, quarterly
FRED_GDP_SERIES = "GDP"                   # Nominal GDP, $B, quarterly

# Household equity allocation (Z.1 Financial Accounts of the United States).
# Public proxy for "% of household financial assets held in equities":
#   BOGZ1LM193064005Q = Households; Corporate Equities and Mutual Fund Shares;
#                       Asset, Market Value ($MM, quarterly)  [numerator]
#   TFAABSHNO         = Households & Nonprofit Orgs; Total Financial Assets,
#                       Level ($MM, quarterly)                [denominator]
# Formula: 100 * BOGZ1LM193064005Q / TFAABSHNO  -> percent (~38.9%).
FRED_HH_EQUITIES_SERIES = "BOGZ1LM193064005Q"
FRED_HH_FIN_ASSETS_SERIES = "TFAABSHNO"

# --- S&P 500 trailing P/E ----------------------------------------------------
# multpl.com publishes the S&P 500 trailing-twelve-month P/E. Scraped from HTML.
MULTPL_SP500_PE_URL = "https://www.multpl.com/s-p-500-pe-ratio"

# --- CAPE / Shiller P/E ------------------------------------------------------
# Primary: Robert Shiller's legacy spreadsheet (ie_data). It is a .xls file,
# which stdlib + openpyxl cannot read (openpyxl is .xlsx only; .xls needs xlrd).
# So by default we scrape the current CAPE value from multpl.com instead, and
# document the .xls path as an optional upgrade.
SHILLER_XLS_URL = "https://img1.wsimg.com/blobby/go/e5e77e0b-59d1-44d9-ab25-4763ac982e53/downloads/ie_data.xls"
MULTPL_CAPE_URL = "https://www.multpl.com/shiller-pe"

# --- Top-10 concentration (Gorilla Index proxy) ------------------------------
# Primary: State Street publishes SPY daily holdings as a public XLSX. It is a
# real .xlsx (openpyxl reads it) with a clean "Name/Ticker/.../Weight" table.
SPY_HOLDINGS_XLSX_URL = (
    "https://www.ssga.com/us/en/intermediary/library-content/products/"
    "fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
)
# Fallback: iShares Core S&P 500 ETF (IVV) holdings CSV. NOTE: in some
# environments iShares returns an HTML consent/interstitial page to bare
# clients instead of the CSV -- the fetcher detects that and reports it.
IVV_HOLDINGS_CSV_URL = (
    "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund"
)

# --- HTTP -------------------------------------------------------------------
HTTP_TIMEOUT = 20  # seconds
USER_AGENT = "phase1-feed/1.0 (+desk overlay; public data only)"

# --- Output -----------------------------------------------------------------
CSV_PATH = "data/metrics_daily.csv"

# Ordered list of metric keys -> CSV column names. Keeps main.py output stable.
METRIC_KEYS = [
    "sp500_trailing_pe",
    "cape_shiller_pe",
    "buffett_indicator",
    "hh_equity_allocation_pct",
    "top10_concentration_pct",
]
