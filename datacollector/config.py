"""
config.py — single source of truth for the options-data warehouse.

Design split (decided 2026-06-25):
  * CODE lives at C:\\TradingDesk (this folder) — a plain local folder, deliberately
    OUTSIDE Google Drive; git is what version-backs it. Drive is a backup destination
    for git bundles only, never the working copy.
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
CODE_ROOT = pathlib.Path(__file__).resolve().parent          # C:\TradingDesk (git-backed)

# Local warehouse root — big, never synced to Drive.
DATA_ROOT = pathlib.Path(r"C:\TradingDesk-Local\warehouse")
RAW_OPTIONS = DATA_ROOT / "raw" / "options"                  # raw/options/{SYMBOL}/{YEAR}.parquet
DERIVED = DATA_ROOT / "derived"                              # computed feature tables (small)
CATALOG_DB = DATA_ROOT / "catalog.duckdb"                    # DuckDB views over the parquet
MANIFEST = RAW_OPTIONS / "_manifest.json"                    # what we've pulled, for resumability

# Parallel IBKR-sourced namespace, used ONLY during the ThetaData A/B validation
# window so IBKR (port-4001) and ThetaData days can coexist for daily comparison
# (they otherwise collide in raw/options via storage.have_day). Kept OUT of the
# main catalog.duckdb; the A/B check reads these parquet files directly. Same
# column schema as the ThetaData parquet, so a future cutover can union cleanly.
RAW_OPTIONS_IBKR = DATA_ROOT / "raw" / "options_ibkr"
MANIFEST_IBKR = RAW_OPTIONS_IBKR / "_manifest.json"

# Secrets live OUTSIDE Drive in the consolidated local secrets file. We only ever
# READ the key by name; its value is never printed/echoed. (Repointed 2026-06-26:
# the old backtester\.env was deleted in the reorg. The key now sits alongside
# TIINGO_API_KEY under the matching *_API_KEY name.)
SECRET_ENV = pathlib.Path(r"C:\TradingDesk-Local\secrets\.env")
THETA_KEY_NAME = "THETADATA_API_KEY"

# --------------------------------------------------------------------------- #
# ThetaData local Terminal (v3) — REST gateway on localhost
# --------------------------------------------------------------------------- #
# NOTE: the ThetaData subscription lapsed 2026-07-25 and the nightly EOD feed cut
# over to the IBKR forward collector (raw/options, key "forward"). These keys are
# retained ONLY because still-present (non-nightly) modules import them —
# universe_download.py / download.py / thetadata_client.py / collect_spx_1m.py /
# canslim\pull_equity_options.py — so removing them would break those imports. The
# old THETA_TERMINAL_JAR key was removed in the cutover (its only users,
# start_terminal.py + theta_terminal_watchdog.py, were deleted).
THETA_BASE_URL = "http://127.0.0.1:25503/v3"
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
# viable nightly. Widened 2026-07-07 after measuring actual downstream footprint:
# datacollector/features/gex.py (the GEX/dealer-gamma engine behind the validated
# MSR signal, consumed via run_gex.py -> derived/{SYM}_gex_daily.parquet -> MSR
# key-levels) does NOT filter by strike or expiration — it uses every contract
# with open_interest > 0 in whatever chain it's handed, gamma-weighted. Measured
# against a real SPXW EOD day (2026-07-02, 19,124-row full chain): 95% of |GEX|
# needs ~40 expirations (of 40 total — gamma has real mass out to ~182-363 DTE
# monthlies/LEAPS, not just the front week) and +/-131 strikes from ATM; SPY/AAPL
# similarly needed ~20-24 of their available expirations and +/-14 to +/-90
# strikes. The prior band=50/exp=12 (~2h/night, ~98k contracts) covered well
# under half of measured 95%-gamma mass for SPX/SPXW. New split gives SPX/SPXW
# (the condor+MSR-critical roots) full headroom over the measured footprint;
# everything else gets a smaller-but-still-~2x-wider band (thin roots are capped
# by their own available strikes/expirations anyway, so this costs little).
# Measured against the live 2026-07-02 warehouse day: ~209k contracts total,
# batches of LINE_LIMIT=90 @ (6s settle + 0.2s cooldown) => ~4.0h/night estimate
# (up from ~1.9h measured-equivalent under the old band). Well under an overnight
# window. NOT yet cut over — see conductor/STATUS.md for remaining validation
# steps (side-by-side greeks vs ThetaData, full overnight timing run) before the
# scheduled task is re-enabled.
FORWARD_MAX_EXPIRATIONS = 20     # nearest N expirations (default; SPX/SPXW override below)
FORWARD_STRIKE_BAND = 75         # +/-N strikes around ATM (default; SPX/SPXW override below)

# SPX/SPXW carry the dealer gamma that actually drives GEX/MSR and the condor
# research — widened further than the rest of the universe (measured: 95% of
# |GEX| needs ~40 expirations and +/-131 strikes on SPXW; SPX is comparable).
FORWARD_DEEP_ROOTS = {"SPX", "SPXW"}
FORWARD_DEEP_MAX_EXPIRATIONS = 40
FORWARD_DEEP_STRIKE_BAND = 150


def forward_depth(sym: str) -> tuple[int, int]:
    """(strike_band, max_expirations) for a root — deep for SPX/SPXW, default elsewhere."""
    if sym in FORWARD_DEEP_ROOTS:
        return FORWARD_DEEP_STRIKE_BAND, FORWARD_DEEP_MAX_EXPIRATIONS
    return FORWARD_STRIKE_BAND, FORWARD_MAX_EXPIRATIONS


# --------------------------------------------------------------------------- #
# IBKR forward collector — unattended-run safety bounds (conductor #59)
# --------------------------------------------------------------------------- #
# The nightly forward pull is UNATTENDED; one bad root must never wedge the whole
# run. A root with no options market-data entitlement (e.g. QQQ -> every option
# returns IBKR Error 10091, greeks/OI never populate) otherwise walks all its
# batches at SETTLE_SECS each — 10+ minutes of silent no-progress that reads as a
# hang — and a non-returning underlying can block the synchronous spot lookup
# indefinitely. These three bounds (all applied in ibkr_forward_live.py) each
# degrade to skip / partial-and-continue, never abort the run.

# 1) PER-ROOT WALL-CLOCK DEADLINE — the definitive backstop. GENEROUS on purpose
#    so it never aborts a legitimately-large root: SPX/SPXW at the current deep
#    depth legitimately take ~11-13 min and could take longer at wider depth, so
#    30 min leaves ample headroom. Only a genuinely stuck root (dead gateway, a
#    non-returning line) ever reaches it; when it does, the collector stops,
#    cancels open data lines, writes whatever it harvested (partial), and moves on.
FORWARD_PER_ROOT_TIMEOUT_SECS = 1800     # 30 min hard ceiling per root

# 2) FAST EARLY-OUT on a no-entitlement / no-data root, so QQQ costs ~seconds not
#    the full deadline. A real liquid root populates greeks/OI within its FIRST
#    expiration, so ZERO populated rows after a meaningful sample means there is no
#    data to wait for. CONSERVATIVE thresholds — we bail only once we have processed
#    at least FORWARD_EARLY_OUT_MIN_BATCHES batches OR FORWARD_EARLY_OUT_MIN_CONTRACTS
#    contracts (whichever comes first) with the populated count still EXACTLY zero,
#    so a slow-but-real root is never falsely bailed and a few empty far-OTM strikes
#    never trigger it.
FORWARD_EARLY_OUT_MIN_BATCHES = 6        # sample >= this many batches, or ...
FORWARD_EARLY_OUT_MIN_CONTRACTS = 300    # ... >= this many contracts, before bailing

# 3) SPOT PRE-CHAIN TIMEOUT — the synchronous reqTickers for the underlying spot
#    can block indefinitely on a non-returning contract (IB.RequestTimeout defaults
#    to 0 = wait forever). Bound it; spot=None is already tolerated downstream
#    (model-greeks undPrice backfills per row), so on timeout we log and continue.
FORWARD_SPOT_TIMEOUT_SECS = 15           # seconds to wait for the underlying spot

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
