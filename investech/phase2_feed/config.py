"""
Central configuration for the Phase-2 breadth-based "Leadership Proxy" feed.

This feed computes daily breadth metrics across the S&P 500 universe from
licensed Tiingo EOD prices, then folds them into a transparent composite that
APPROXIMATES the BEHAVIOR of InvesTech Research's proprietary Negative
Leadership Composite (NLC). It is an approximation, NOT a replica -- see README.

No secrets in this file. The Tiingo key is read from the desk .env (path below)
or the environment by the fetchers, WITHOUT being printed.
"""

import os

# --- Secrets / .env ----------------------------------------------------------
# The desk keeps paid data keys here. We load KEY=value lines into os.environ
# WITHOUT echoing any value. Override with PHASE2_ENV_PATH if it moves.
DOTENV_PATH = os.environ.get(
    "PHASE2_ENV_PATH", r"C:\TradingDesk-Local\secrets\.env"
)
TIINGO_KEY_ENV = "TIINGO_API_KEY"   # name of the env var we read (never printed)

# --- Tiingo EOD --------------------------------------------------------------
# Daily end-of-day prices. Docs: https://www.tiingo.com/documentation/end-of-day
TIINGO_BASE_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
# We need ~1 year + 200 trading days of history to compute a 200-day MA and a
# 52-week high/low. ~330 calendar days is too short; use 420 to be safe.
TIINGO_LOOKBACK_DAYS = 420
# Be polite to the API: small pause between per-ticker requests (seconds).
TIINGO_REQUEST_PAUSE = 0.10
# Per-request network timeout (seconds).
HTTP_TIMEOUT = 20
USER_AGENT = "phase2-breadth-feed/1.0 (+desk overlay; licensed data only)"

# --- Universe (S&P 500 constituents) -----------------------------------------
# Source of truth for the constituent list. Primary: a committed static CSV
# (data/sp500_constituents.csv, column "Symbol") so runs are reproducible and
# do not depend on a live scrape. If that file is absent, we fall back to the
# public Wikipedia "List of S&P 500 companies" table. Documented in README.
UNIVERSE_CSV_PATH = "data/sp500_constituents.csv"
UNIVERSE_CSV_SYMBOL_COL = "Symbol"
WIKIPEDIA_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# OPERATIONAL DEFAULT: FULL UNIVERSE. UNIVERSE_LIMIT = None pulls all ~503
# constituents. A subset can still be requested (fast scaffold/test runs that are
# gentle on the Tiingo key); subset runs are CLEARLY labelled in both stdout and
# the CSV. The EOD cache (see below) makes even full-universe runs cheap to
# repeat, since only missing/stale tickers are re-fetched.
#
# Override at runtime without editing this file:
#   PowerShell:  $env:PHASE2_UNIVERSE_LIMIT = "40"   # 0 or empty => full
#   bash:        export PHASE2_UNIVERSE_LIMIT=40
UNIVERSE_LIMIT = None   # None => full S&P 500; set an int for a subset run
_env_limit = os.environ.get("PHASE2_UNIVERSE_LIMIT")
if _env_limit is not None and _env_limit != "":
    try:
        _v = int(_env_limit)
        UNIVERSE_LIMIT = None if _v <= 0 else _v
    except ValueError:
        pass

# --- EOD price cache ---------------------------------------------------------
# Each ticker's fetched daily series is written to data/cache/<TICKER>.csv
# (columns: date,close,high,low). On a run we REUSE a cache file when it is fresh
# (its newest date == the most recent expected trading day) and only fetch
# tickers that are missing or stale. This makes repeat runs (e.g. the upcoming
# calibration step) cheap and keeps us well under Tiingo rate limits.
#
# Force a full refresh (ignore + overwrite cache) without editing this file:
#   PowerShell:  $env:PHASE2_FORCE_REFRESH = "1"; python main.py
#   bash:        PHASE2_FORCE_REFRESH=1 python main.py
CACHE_DIR = "data/cache"
CACHE_ENABLED = True
FORCE_REFRESH = bool(os.environ.get("PHASE2_FORCE_REFRESH"))

# --- Breadth windows ---------------------------------------------------------
MA_SHORT = 50         # 50-day simple moving average
MA_LONG = 200         # 200-day simple moving average
HIGH_LOW_WINDOW = 252  # ~52 weeks of trading days for new highs / new lows

# --- Leadership-Proxy composite & regime thresholds --------------------------
# The composite is a 0..100 "breadth health" score (higher = more bullish /
# "Selling-Vacuum-like"; lower = more bearish / "Distribution-like"). See README
# "Composite formula" for the exact, transparent math. These are STARTER
# thresholds -- the README "Calibration TODO" explains how to tune them against
# InvesTech's published NLC values in _dataset/InvesTech_Signals.csv.
#
# Component weights (must sum to 1.0). Each component is first normalized to a
# 0..100 sub-score, then blended.
PROXY_WEIGHTS = {
    "pct_above_50dma": 0.25,   # participation (short trend)
    "pct_above_200dma": 0.25,  # participation (long trend)
    "net_highs_lows_pct": 0.30,  # leadership: new-high vs new-low dominance
    "ad_pct": 0.20,            # daily advance/decline tilt
}

# Regime cut points on the 0..100 composite.
REGIME_BULL_MIN = 60.0   # composite >= 60  -> "Selling Vacuum (bullish)"
REGIME_BEAR_MAX = 40.0   # composite <= 40  -> "Distribution (bearish)"
# Between the two -> "Neutral".

REGIME_LABELS = {
    "bull": "Selling Vacuum (bullish)",
    "neutral": "Neutral",
    "bear": "Distribution (bearish)",
}

# --- True NYSE/NASDAQ exchange breadth (OPTIONAL, additional sub-score) -------
# The four metrics above are computed over the S&P 500 LARGE-CAP universe only.
# InvesTech-style composites traditionally use FULL EXCHANGE breadth (every NYSE
# + NASDAQ issue): exchange advance/decline and new-high/new-low. When a real
# source for that data is available, we compute it as a SEPARATE, clearly-
# labelled sub-score (exchange_breadth_score, 0..100) and blend it into the
# composite with the provisional weight below.
#
# Toggle:
#   EXCHANGE_BREADTH_ENABLED = True tries the wired source each run and degrades
#   gracefully (status "unavailable") if it is down. False skips it entirely and
#   the proxy is exactly the S&P-500 breadth as before (the existing subset path
#   is therefore UNCHANGED when this is False).
EXCHANGE_BREADTH_ENABLED = True
# PROVISIONAL weight given to the exchange-breadth sub-score IF (and only if) a
# real reading is obtained. The four S&P-500 PROXY_WEIGHTS are scaled down pro
# rata to make room (see breadth.leadership_proxy). This weight is a placeholder
# and will be set in the upcoming calibration step -- see README.
EXCHANGE_BREADTH_WEIGHT = 0.25
# ThetaData Terminal local REST API (default host/port). ThetaData has NO cloud
# REST endpoint: the Java Terminal must be running locally and logged in with the
# THETADATA_API_KEY for this to respond. If it is not running we degrade.
THETADATA_BASE_URL = os.environ.get(
    "THETADATA_BASE_URL", "http://127.0.0.1:25503/v3"
)
THETADATA_KEY_ENV = "THETADATA_API_KEY"   # name only (never printed)
# Short timeout (seconds) for the Terminal health probe so a down Terminal fails
# fast (connection refused) instead of hanging the run.
THETADATA_HEALTH_TIMEOUT = 3

# --- Price data source switch ------------------------------------------------
# Which BULK EOD / history source feeds the per-ticker breadth series.
#   "thetadata" -> PREFER the local ThetaData Terminal (no free-tier hourly cap),
#                  fall back to Tiingo when the Terminal is down.
#   "tiingo"    -> use Tiingo only (the original behavior).
#   "auto"      -> use ThetaData IF the Terminal is up at run start, else Tiingo.
# In every mode the EOD cache (data/cache/) is used identically and Tiingo is the
# fallback, so a down Terminal NEVER aborts the run.
#
# Override at runtime without editing this file:
#   PowerShell:  $env:PHASE2_DATA_SOURCE = "thetadata"
#   bash:        export PHASE2_DATA_SOURCE=thetadata
DATA_SOURCE = os.environ.get("PHASE2_DATA_SOURCE", "auto").strip().lower() or "auto"

# Universe source for the per-ticker breadth metrics.
#   "sp500"     -> the existing S&P 500 constituent list (Tiingo path; default).
#   "thetadata" -> the BROAD NYSE/NASDAQ stock-roots list from the Terminal
#                  (/v2/list/roots/stock) -- a far closer approximation of the
#                  full-exchange universe InvesTech's NLC actually uses. Requires
#                  the Terminal to be up; falls back to "sp500" if it is down.
# See README "S&P-500 vs broad universe" for the tradeoff.
UNIVERSE_SOURCE = os.environ.get(
    "PHASE2_UNIVERSE_SOURCE", "sp500"
).strip().lower() or "sp500"

# Cap on the BROAD ThetaData universe (None/0 => all roots; thousands of names).
# Distinct from UNIVERSE_LIMIT (which caps the S&P-500 list). Keep a sane cap for
# first runs -- a full broad EOD pull is large even without a rate limit.
THETADATA_UNIVERSE_LIMIT = None
_td_uni_limit = os.environ.get("PHASE2_THETADATA_UNIVERSE_LIMIT")
if _td_uni_limit is not None and _td_uni_limit != "":
    try:
        _tv = int(_td_uni_limit)
        THETADATA_UNIVERSE_LIMIT = None if _tv <= 0 else _tv
    except ValueError:
        pass

# --- Calibration reference (read-only; not used by the live calc yet) --------
# InvesTech's actual published monthly NLC values/regimes. Used ONLY by the
# (future) calibration step described in the README -- never scraped, just a
# local file the desk already licenses.
# Derived from __file__ (this file lives in investech/phase2_feed/) so it survives
# the repo moving -- it left Google Drive for C:\TradingDesk on 2026-07-16.
NLC_REFERENCE_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),  # ...\investech
    "_dataset",
    "InvesTech_Signals.csv",
)

# --- Output ------------------------------------------------------------------
CSV_PATH = "data/leadership_daily.csv"

# Ordered breadth metric keys -> stable CSV columns / stdout order.
BREADTH_KEYS = [
    "universe_count",
    "pct_above_50dma",
    "pct_above_200dma",
    "new_highs_52w",
    "new_lows_52w",
    "net_highs_lows",
    "net_highs_lows_pct",
    "advances",
    "declines",
    "ad_net",
    "ad_line_cumulative",
    "exchange_breadth_score",
    "exchange_breadth_status",
    "leadership_proxy",
    "regime",
]
