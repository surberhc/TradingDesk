"""
universe_config.py — EXPANDED options universe + snapshot-layer settings for the
standalone `universe_download.py` bulk pull.

WHY A SEPARATE CONFIG (not edits to config.py): config.py's UNIVERSE (49 roots)
is the FROZEN scope of the existing EOD warehouse + the IBKR forward collector.
Touching it would re-scope the running collector. This file is additive and only
read by the new standalone downloader; it imports config.py for the shared paths
(DATA_ROOT, THETA_BASE_URL, etc.) and reuses that infra unchanged.

Two layers, one universe:
  * EOD layer  -> same schema/tree as the existing warehouse:
        raw/options/{SYMBOL}/{YYYYMMDD}.parquet   (greeks + open_interest join)
    A (symbol, day) already on disk is SKIPPED, so the 49 existing roots only
    grow by any missing days and the NEW roots fill from scratch.
  * SNAPSHOT layer -> NEW tree, fixed-time consistent NBBO across the near-money
    band, solving the EOD-quote timing problem (greeks/eod stamps each contract's
    quote at ITS last-activity time — verified 13:57 vs 15:58 on the same day):
        raw/options_snap/{SYMBOL}/{HHMM}/{YYYYMMDD}.parquet

Both layers pull 2018-01-01..present (single-name option history on ThetaData
generally starts ~2020; earlier days simply return empty and are marked done).
"""

# Config for the DORMANT universe_download puller (ThetaData-retired 2026-07-27,
# kept as reference).

from __future__ import annotations

import config  # shared paths / terminal URL — reused, not duplicated

# --------------------------------------------------------------------------- #
# Window (matches the warehouse's 8-year Standard-tier reach)
# --------------------------------------------------------------------------- #
GRAB_START = "20180101"
GRAB_END = None          # None -> up to yesterday (today's expiration=* is HTTP 400)

# --------------------------------------------------------------------------- #
# Snapshot layer — fixed-time consistent NBBO
# --------------------------------------------------------------------------- #
# The quote endpoint's interval=15m grid stamps at clean wall-clock boundaries
# (verified: 09:30, 09:45, 10:00 ... 15:45, 16:00), so these four target times
# are all exact grid points — we pull the 15m grid per expiration and KEEP only
# these rows. All legs at one instant (unlike greeks/eod).
SNAP_TIMES = ["12:00", "15:45"]   # ET (terminal-local session clock)
SNAP_INTERVAL = "15m"        # coarse grid that contains every SNAP_TIME above
# Near-money band + DTE window relevant to premium selling. We resolve the band
# in STRIKE-DOLLAR terms off the day's underlying_price (from greeks/eod), so we
# only ask the quote endpoint for the strikes we keep (minimizes transfer).
SNAP_BAND_PCT = 0.15         # +/-15% of spot
SNAP_DTE_MIN = 0
SNAP_DTE_MAX = 60            # 0-60 DTE — the premium-selling horizon

# --------------------------------------------------------------------------- #
# Parallelism — memory: one terminal sustains ~4 shards / ~2.85x, no gain past 4
# --------------------------------------------------------------------------- #
SHARDS = 4

# --------------------------------------------------------------------------- #
# EXPANDED UNIVERSE (~130 roots)
# --------------------------------------------------------------------------- #
# Grouping mirrors config.py so run-order can be by group. Every root here that is
# also in the existing warehouse is only TOPPED UP (missing days), never re-pulled.
UNIVERSE: dict[str, list[str]] = {
    # ---- Indices + vol (all already on disk) ----
    "index_vol": ["SPX", "SPXW", "VIX", "NDX", "RUT", "XSP"],
    "vix_etps": ["VXX", "VIXY", "UVXY", "SVXY"],
    # ---- Broad equity / size / style ----
    "broad_equity": ["SPY", "QQQ", "IWM", "DIA", "RSP", "VTI", "MDY", "EFA", "EEM"],
    # ---- 11 GICS sector SPDRs (all on disk) ----
    "sectors": ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK",
                "XLP", "XLRE", "XLU", "XLV", "XLY"],
    # ---- Industry / thematic sub-sector ETFs (liquid options) ----
    "industry_etfs": ["SMH", "SOXX", "XBI", "IBB", "KRE", "XOP", "XRT",
                      "XHB", "ITB", "JETS", "ARKK"],
    # ---- Credit / rates (mostly on disk) ----
    "credit": ["HYG", "LQD", "JNK"],
    "rates": ["TLT", "IEF", "SHY"],
    # ---- Real assets / commodity ETFs (high-IV, liquid options) ----
    "real_assets": ["GLD", "SLV", "GDX", "GDXJ", "USO", "UNG", "SLV",
                    "XLE"],   # XLE dup drops in flatten
    "commodity_hi_iv": ["USO", "UNG", "GDX", "GLD", "SLV"],
    # ---- Mega-cap tech / high-volume single names ----
    "megacap_tech": ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG",
                     "AVGO", "TSLA", "AMD", "NFLX", "ADBE", "CRM", "ORCL",
                     "INTC", "QCOM", "MU", "CSCO", "TXN", "AMAT"],
    # ---- Financials ----
    "financials": ["JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA",
                   "AXP", "SCHW", "BRKB", "COIN", "PYPL", "SQ"],
    # ---- Healthcare ----
    "healthcare": ["LLY", "UNH", "JNJ", "PFE", "MRK", "ABBV", "BMY", "AMGN",
                   "GILD", "MRNA", "CVS"],
    # ---- Consumer / retail ----
    "consumer": ["WMT", "HD", "COST", "MCD", "NKE", "SBUX", "TGT", "LOW",
                 "DIS", "BABA"],
    # ---- Energy / industrials ----
    "energy_indust": ["XOM", "CVX", "OXY", "SLB", "COP", "BA", "CAT", "GE",
                      "DE", "UPS", "FDX"],
    # ---- High-IV / high-options-volume momentum & meme names ----
    "hi_iv_momentum": ["PLTR", "SOFI", "RIVN", "LCID", "NIO", "MARA", "RIOT",
                       "SMCI", "DKNG", "SNAP", "UBER", "ABNB", "SHOP", "ROKU",
                       "GME", "AMC", "F", "T", "BABA"],
    # ---- Communication / media ----
    "comm_media": ["GOOGL", "META", "NFLX", "DIS", "CMCSA", "VZ", "TMUS"],
}


def all_roots() -> list[str]:
    """Flat, de-duplicated list (insertion order preserved)."""
    seen: list[str] = []
    for group in UNIVERSE.values():
        for r in group:
            if r not in seen:
                seen.append(r)
    return seen


# Roots already present in the frozen warehouse config (topped-up, not new).
EXISTING_ROOTS = set(config.all_roots())


def new_roots() -> list[str]:
    return [r for r in all_roots() if r not in EXISTING_ROOTS]


def existing_roots() -> list[str]:
    return [r for r in all_roots() if r in EXISTING_ROOTS]
