"""
config.py — single source of truth for the options-data warehouse.

Design split (decided 2026-06-25):
  * CODE lives in Google Drive (this folder) so it is version-backed.
  * RAW bulk options data lives LOCAL on C: (DATA_ROOT) — it is tens of GB and
    must NOT sync into Drive (sync thrash). Only small DERIVED feature tables
    (GEX/skew dailies) get copied back to Drive for backup.

The grab model: ThetaData Options Standard is a ONE-MONTH, $80 subscription with
8 years of history and unlimited requests. We do a single bulk pull of EOD option
chains for a curated macro/allocation/vol universe, hold it locally forever, then
extend it FORWARD for free with our own IBKR collector. We never re-subscribe.
"""

from __future__ import annotations

import pathlib

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
CODE_ROOT = pathlib.Path(__file__).resolve().parent          # Drive (backed up)

# Local warehouse root — big, never synced to Drive.
DATA_ROOT = pathlib.Path(r"C:\TradingDesk-Local\warehouse")
RAW_OPTIONS = DATA_ROOT / "raw" / "options"                  # raw/options/{SYMBOL}/{YEAR}.parquet
DERIVED = DATA_ROOT / "derived"                              # computed feature tables (small)
CATALOG_DB = DATA_ROOT / "catalog.duckdb"                    # DuckDB views over the parquet
MANIFEST = RAW_OPTIONS / "_manifest.json"                    # what we've pulled, for resumability

# Secrets live OUTSIDE Drive in the consolidated local secrets file. We only ever
# READ the key by name; its value is never printed/echoed. (Repointed 2026-06-26:
# the old backtester\.env was deleted in the reorg. The key now sits alongside
# TIINGO_API_KEY under the matching *_API_KEY name.)
SECRET_ENV = pathlib.Path(r"C:\TradingDesk-Local\secrets\.env")
THETA_KEY_NAME = "THETADATA_API_KEY"

# --------------------------------------------------------------------------- #
# ThetaData local Terminal (v3) — REST gateway on localhost
# --------------------------------------------------------------------------- #
THETA_BASE_URL = "http://127.0.0.1:25503/v3"
THETA_TERMINAL_JAR = DATA_ROOT / "ThetaTerminalv3.jar"       # downloaded once, kept local
THETA_RATE_TYPE = "sofr"                                     # greeks risk-free basis (default)

# --------------------------------------------------------------------------- #
# Grab scope
# --------------------------------------------------------------------------- #
# 8-year window (Standard tier). The Terminal returns whatever it actually has.
GRAB_START = "20180101"
GRAB_END = "20260625"
GRANULARITY = "eod"          # end-of-day only — compact, sufficient for daily/swing overlays

# --------------------------------------------------------------------------- #
# IBKR forward collector — nightly EOD capture depth
# --------------------------------------------------------------------------- #
# A literal full chain via IBKR streaming is ~9.8 h/night (511k contracts) — not
# viable nightly. "Wide band" (chosen 2026-06-26) captures all actionable dealer
# gamma + every strike the condor/MSR strategies use, in ~2 h. Tunable.
FORWARD_MAX_EXPIRATIONS = 12     # nearest N expirations
FORWARD_STRIKE_BAND = 50         # ±N strikes around ATM (None = all strikes / full chain)

# Curated universe (~36 roots) — chosen to leave doors open across every strategy
# theme we've discussed, WITHOUT exploding into the full OPRA single-name universe.
# SPX/SPXW dominate storage; everything else is comparatively tiny.
UNIVERSE: dict[str, list[str]] = {
    # Market-wide gamma overlay + vol-of-vol. SPXW = the PM-settled weeklies/0DTE
    # that carry most of today's dealer gamma; SPX = AM-settled monthlies. Need both.
    # XSP = Mini-SPX (1/10 notional, cash-settled, Sec.1256) for S3 covered-call tests.
    "index_vol": ["SPX", "SPXW", "VIX", "NDX", "RUT", "XSP"],
    # Core tradeable VIX complex (vanilla long-vol ETPs with options). The futures
    # term structure isn't in this product, but these ETPs embed it.
    "vix_etps": ["VXX", "VIXY"],
    # Broad equity beta / size / breadth.
    "broad_equity": ["SPY", "QQQ", "IWM", "DIA", "RSP"],
    # 11 GICS sectors — sector-rotation fragility/skew overlays.
    "sectors": ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK",
                "XLP", "XLRE", "XLU", "XLV", "XLY"],
    # Credit — HY/IG/credit-skew. A credit-OPTIONS skew signal is a sharper
    # credit-stress read than the HYG/IEF price proxy we settled on for the backtester.
    "credit": ["HYG", "LQD", "JNK"],
    # Rates — a MOVE-like rate-vol/skew read.
    "rates": ["TLT", "IEF", "SHY"],
    # Gold / commodities (commodity-ETF option books are thinner — kept anyway,
    # storage is cheap and we don't want to re-subscribe to add them later).
    "real_assets": ["GLD", "SLV", "GDX", "USO", "UNG"],
    # Top 15 S&P names by weight — single-name options for the band-predicts-amplitude
    # (Finding 4) tests vs indices. NOTE: single-name option history starts ~2020 on
    # ThetaData (CTA tape), not 2018. Verify BRK.B root maps (may be "BRKB").
    "single_names": ["NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "AVGO",
                     "TSLA", "BRKB", "LLY", "JPM", "V", "XOM", "UNH", "MA"],
}


def all_roots() -> list[str]:
    """Flat, de-duplicated list of every root we intend to pull."""
    seen: list[str] = []
    for group in UNIVERSE.values():
        for r in group:
            if r not in seen:
                seen.append(r)
    return seen
