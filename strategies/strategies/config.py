"""
config.py — Single source of truth for every tunable parameter.

Per CLAUDE.md: when a number in SPEC.md is a tunable parameter, it lives HERE
and nowhere else. Engines must import these values rather than hard-coding
magic numbers. Change a value in one place and the whole backtest follows.

Section numbers in comments refer to SPEC.md.

This file holds DATA ONLY — no logic. Nothing here computes anything; it just
declares the knobs. Engine modules (built later) read from this.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Paths & files
# ---------------------------------------------------------------------------
# Price data moved OFF Google Drive to a stable LOCAL path (2026-06-27): Drive sync
# was corrupting the parquet files mid-session. These are now absolute local paths;
# data_loader/download_data resolve absolute paths as-is (see those modules).
DATA_DIR = r"C:\TradingDesk-Local\bt_data"
OUTPUT_DIR = "output"
MANIFEST_FILE = r"C:\TradingDesk-Local\bt_data\_manifest.json"
ENV_FILE = ".env"          # holds TIINGO_API_KEY — never read/print its contents


# ---------------------------------------------------------------------------
# Backtest window & timing (SPEC §3, DATA.md)
# ---------------------------------------------------------------------------
DATA_START = "2007-01-01"      # download floor: warm-up before backtest floor
                               # (2007 = GFC window; Tiingo returns each ticker's
                               # full history from inception, pre-2010 universe is
                               # legitimately thinner — handled by inception-aware loader)
BACKTEST_START = "2015-01-01"  # full defensive universe exists by here (SPEC §2)
PRIMARY_PERIOD_START = "2017-01-01"  # period of interest for reporting (SPEC §2)
# BACKTEST_END defaults to "today" at run time when left as None.
BACKTEST_END = None

EXECUTION_LAG_DAYS = 1         # month-end signal, trade on T+1 (SPEC §3, §13)
EXECUTION_BUFFER_DAYS = 0      # optional extra one-day buffer (SPEC §13)


# ---------------------------------------------------------------------------
# Universe (SPEC §2, DATA.md)
# ---------------------------------------------------------------------------
# DE-DUPLICATED 2026-08-24 (Andrew's call). VTI was REMOVED: it is 0.996-correlated with
# SPY (tracking-difference stdev 1.61%/yr, 2015+) — the same cap-weighted bet held twice.
# Equal-weighting SPY+VTI+RSP made the sleeve effectively ~2/3 cap-weight / ~1/3 equal-weight,
# so EQUITY_CORE_WEIGHTS below PRESERVES that exact exposure with two funds instead of three.
# This is a DE-DUPLICATION, not a re-optimization: naively dropping VTI and equal-weighting
# the remaining two would silently become a 50/50 cap/equal sleeve — a real allocation change
# that tested WORSE (sleeve-only CAGR 12.87% vs 13.18%, 2015-02->2026-08). Do not "simplify"
# these weights back to equal without re-testing.
EQUITY_CORE = ["SPY", "RSP"]

# Relative weights WITHIN the equity core, normalized over whichever members pass the trend
# gate on a given date (parts/sector._core_weights). Absent/omitted ticker -> equal weight.
EQUITY_CORE_WEIGHTS = {"SPY": 2.0 / 3.0, "RSP": 1.0 / 3.0}

SECTORS = ["XLC", "XLY", "XLP", "XLE", "XLF", "XLV",
           "XLI", "XLB", "XLRE", "XLK", "XLU"]

# ---------------------------------------------------------------------------
# SECTOR NEUTRAL CORE (STUDY-ONLY, DEFAULT OFF) - prereg
# docs/PREREG_S0_sector_momentum_core_2026-08-24.md, Phase 1 "neutral-only" arm.
#
# Replaces the broad-beta equity sleeve (EQUITY_CORE) with the 11 Select Sector SPDRs held
# at a DRIFTING STRATEGIC NEUTRAL. Motivation is NOT return: Phase 0 measured the neutral at
# +15 bps/yr, i.e. free. It is held for (a) sector de-concentration vs cap weight, (b) a real
# management surface (tax-loss harvesting between sectors, rebalancing, tilts), and (c) a
# defensible client-facing book. It is NOT risk diversification: the 11 SPDRs partition the
# S&P 500, so this is the same asset-class exposure shown as 11 line items.
#
# NEUTRAL = halfway between cap-weight (SPY) and equal-weight (RSP) sector allocations.
# ANCHOR is Andrew's Aug-2026 table; weights for any other date are reconstructed by
# de-drifting / drifting that anchor with realized sector returns (parts/sector.neutral_weights).
# Reconstruction degrades going back (the Jun-2018 XLC spin-off is invisible to it), biasing
# early-period weights toward UNDERweighting eventual winners - i.e. conservative, not flattering.
SECTOR_NEUTRAL_ENABLED = False       # STUDY FLAG. True replaces EQUITY_CORE in the equity sleeve.
SECTOR_NEUTRAL_ANCHOR_DATE = "2026-08-21"
SECTOR_NEUTRAL_ANCHOR = {            # 50/50 cap/equal blend, Aug 2026 (Andrew's memo section 3)
    "XLK": 0.274, "XLF": 0.133, "XLI": 0.119, "XLV": 0.110, "XLY": 0.099,
    "XLC": 0.067, "XLP": 0.055, "XLU": 0.039, "XLE": 0.038, "XLRE": 0.037,
    "XLB": 0.029,
}
SECTOR_NEUTRAL_REBUILD = "QE"        # quarterly re-derivation of the neutral (memo section 3)


# ---------------------------------------------------------------------------
# SMALL-ACCOUNT TIER - whole-share proxy for accounts under the threshold
# DECIDED 2026-08-06 for Growth. Proposal: SmallAccount_Tier_Proposal_2026-08-05.md.
# EXTENDED 2026-08-24 (Andrew) to ALL THREE versions: Conservative, Balanced, Growth.
#
# WHY: fractional / cash-quantity orders are impossible over the TWS socket API, and the full
# models' SPY at ~$766/share cannot be whole-share-held on target in a small account. Rather
# than hold a badly-tracking multi-ETF model, small accounts hold a 2-ticker PROXY of the SAME
# engine output: the equity sleeve collapses to one cheap total-market ETF and everything else
# (defensive + real-asset) collapses to the same floating-rate fund the full models already use.
# SCHB ~$29.61/share vs SPY ~$766 is the whole point - it is the fine-grained quantum.
#
# This is NOT a separate strategy, and there is not one per version. It is a RENDERING of
# whichever version's engine output onto two tickers, so each version's own dynamic
# risk-on/risk-off behaviour - the actual edge - is preserved exactly. Conservative (Small),
# Balanced (Small) and Growth (Small) therefore differ from each other exactly as their
# full-size parents do: by the equity/defensive split the engine hands them.
SMALL_TIER_EQUITY = "SCHB"        # total US market; 0.9984 corr to VTI, 0.99%/yr tracking diff
SMALL_TIER_DEFENSIVE = "USFR"     # the same floating-rate sleeve the full models use
SMALL_TIER_SUFFIX = " (Small)"    # label convention: "Balanced" -> "Balanced (Small)"

# NAV tiering with HYSTERESIS, so an account near the line does not churn models every month.
# NOTE: the $25k boundary was derived from GROWTH's whole-share math (its ~85% equity sleeve
# in $766 SPY is the binding constraint). Per-version drift is measured in
# backtester/output/SmallAccount_AllVersions_2026-08-24.md; a version needing a different
# boundary gets an entry in SMALL_TIER_THRESHOLD_BY_VERSION rather than a global change.
SMALL_TIER_THRESHOLD = 25_000.0   # the decision boundary for a NEW/unassigned account
SMALL_TIER_PROMOTE_AT = 27_500.0  # small -> full only at/above this
SMALL_TIER_DEMOTE_AT = 22_500.0   # full -> small only below this
SMALL_TIER_THRESHOLD_BY_VERSION: dict = {}   # empty = every version uses the values above

TBILLS = ["SGOV", "BIL"]
SHORT_TREASURIES = ["SHY", "VGSH"]
FLOATING_RATE = ["USFR", "TFLO"]
INTERMEDIATE_TREASURIES = ["IEF"]
LONG_TREASURIES = ["TLT"]

GOLD = ["GLDM", "IAU"]
TIPS = ["SCHP", "STIP"]
COMMODITIES = ["PDBC", "DBC"]

# Convenience groupings
DEFENSIVE_ASSETS = (TBILLS + SHORT_TREASURIES + FLOATING_RATE
                    + INTERMEDIATE_TREASURIES + LONG_TREASURIES)
REAL_ASSETS = GOLD + TIPS + COMMODITIES

# Full download list (DATA.md). data_loader filters by inception per month.
SMALL_TIER_TICKERS = ["SCHB"]   # the small-account equity proxy; see SMALL_TIER_* below.
                                # USFR is already in DEFENSIVE_ASSETS, so only SCHB is new.
ALL_TICKERS = (EQUITY_CORE + SECTORS + DEFENSIVE_ASSETS + REAL_ASSETS + SMALL_TIER_TICKERS)

# Benchmarks for the report (SPEC §2, §14, §15)
BENCHMARK_SPY = "SPY"
BENCHMARK_6040 = ("SPY", "AGG")   # 60/40 blend: S&P 500 + total US bond market
BENCHMARK_6040_WEIGHTS = (0.60, 0.40)
# AGG (iShares Core US Aggregate Bond) replaced IEF 2026-06-24 for a realistic
# 60/40: real-world balanced portfolios hold a diversified bond index (Treasuries
# + corporates + MBS, ~6yr duration), not pure intermediate Treasuries. AGG is a
# benchmark-only ticker (downloaded to data/, not part of the tradeable universe).
BENCHMARK_TBILL = "BIL"


# ---------------------------------------------------------------------------
# Data source (DATA.md)
# ---------------------------------------------------------------------------
TIINGO_BASE_URL = "https://api.tiingo.com/tiingo/daily"
TIINGO_RATE_LIMIT_PER_HOUR = 50    # free tier unique-symbol limit
PRICE_FIELD = "adjClose"           # adjusted for splits AND dividends
STORAGE_FORMAT = "parquet"         # "parquet" (preferred) or "csv"

# Macro inputs — use labeled proxies in the first build (DATA.md)
USE_TREASURY_PARYIELD = True       # try public US Treasury par-yield first
YIELD_PROXY_TICKER = "IEF"         # fallback proxy for 10y-yield trend signal
VIX_PROXY = "realized_vol_spy"     # realized vol of SPY as vol-signal proxy
# Credit-stress proxy = HYG/IEF (high-yield vs intermediate Treasury). The ratio
# falls when HY underperforms Treasuries = credit stress + flight-to-quality.
# NOTE (2026-06-25): we TESTED HYG/LQD (HY vs investment-grade corporate, which
# cancels the rate component) per a reviewer suggestion — it was WORSE across the
# board (2008 +3.4%->+1.4%, Calmar 0.71->0.58), because the deflation filter WANTS
# the flight-to-quality/rate component HYG/LQD removes (in 2008 IG blew out too, so
# HY-vs-IG barely widened). So HYG/IEF is kept. The denominator is configurable
# (CREDIT_PROXY[1]); the real ICE BofA HY OAS isn't free past 3 yrs on FRED.
CREDIT_PROXY = ("HYG", "IEF")


# ---------------------------------------------------------------------------
# Data-quality thresholds (DATA.md)
# ---------------------------------------------------------------------------
QC_MAX_SINGLE_DAY_MOVE = 0.25      # flag > 25% one-day move (possible bad split)
QC_STALE_PRICE_RUN = 5             # flag this many identical consecutive prices
QC_MAX_GAP_DAYS = 5                # flag gaps longer than this within active life


# ---------------------------------------------------------------------------
# Regime Engine — Market Health Score (SPEC §4)
# ---------------------------------------------------------------------------
# Three equal-weight components, each up to ~33.3 of a 0-100 score.
REGIME_COMPONENT_MAX = 100.0 / 3.0

# Trend lookbacks
MA_SHORT_DAYS = 50
MA_LONG_DAYS = 200
MA_MONTHS = 10                     # 10-month moving average
TREND_RETURN_MONTHS = 6           # 6-month total return component
SLOPE_LOOKBACK_DAYS = 200         # 200-day slope

# --- 200d MA fragility study (research knobs; production-neutral) -------------
# The single MA_LONG_DAYS knob is OVERLOADED across two conceptually different
# roles, which is the suspected source of its fragility (a value tuned for one
# role silently propagates into the other). These optional overrides let us
# isolate or harden each role. Both default to None -> use MA_LONG_DAYS, so the
# production strategy is byte-identical until a value is set here.
#   TREND  role: price > its-own-MA trend gates (regime trend/breadth/RS leadership,
#                duration TLT/SPY/commodity trend & ban rules, defensive abs_trend,
#                sector core gate, real-asset gate).
#   STRESS role: a series vs its-own-MA "normal level" baselines (VIX-calm,
#                realized-vol, credit ratio / HY-OAS, the 10y-yield baseline).
TREND_MA_DAYS = None               # None -> MA_LONG_DAYS
STRESS_MA_DAYS = None              # None -> MA_LONG_DAYS

# --- MA gate MODE (Opt 1 ensemble / Opt 2 EMA-buffer) ------------------------
# "sma"      : production — single SMA(N) crossover, binary (price > SMA(N)).
# "ensemble" : vote across MA_ENSEMBLE_LOOKBACKS; a price gate returns the FRACTION
#              of member lookbacks the price clears (graded 0..1). Where a hard
#              boolean is required (duration ban rules), it uses majority (>= 0.5).
# "ema"      : single EMA(span=N) crossover (binary), optionally with a deadband.
# MA_GATE_BUFFER_PCT adds a symmetric deadband: price must clear the MA by this
# fraction to count as "above" (reduces knife-edge whipsaw). 0.0 = off.
MA_GATE_MODE = "sma"               # "sma" | "ensemble" | "ema"
MA_ENSEMBLE_LOOKBACKS = (150, 200, 250)
MA_GATE_BUFFER_PCT = 0.0           # global early-exit margin: price must clear MA by
                                   # this fraction to read 'in trend' (0.0 = off)

# Per-engine override of the early-exit margin (None -> MA_GATE_BUFFER_PCT global).
# Scopes the trend margin to specific engines. Defensive's abs_trend is a continuous
# ranking factor (no gate), so it never takes a margin.
#
# REGIME_TREND_MARGIN = 0.03 is ADOPTED (2026-06-26): the regime engine's trend gates
# (trend / breadth / RS-leadership) use a 3% one-sided early-exit margin — price must
# clear its MA by 3% to read "in trend", so the regime de-risks early and the exact
# 200d lookback stops mattering. This RESOLVES the 200d-MA fragility (VALIDATION.md
# §4.1): Calmar spread across lookbacks 150-250 falls 38% -> 8% (a plateau, also flat
# across margins 3-5%), all three versions improve, 2008 is preserved, and it holds
# out-of-sample. Localized by experiment to the regime engine ONLY: the same margin on
# duration does nothing (the proven 2008/2022 ban rules are left untouched) and on the
# real-asset/sector gates is harmful. See backtester/ma_experiment*.py.
REGIME_TREND_MARGIN = 0.03
DURATION_TREND_MARGIN = None
REALASSET_TREND_MARGIN = None
SECTOR_TREND_MARGIN = None


def trend_ma_days() -> int:
    """Resolve the trend-gate MA lookback (TREND_MA_DAYS override, else MA_LONG_DAYS).

    Pure resolver over the config globals (read at call time so sweeps that setattr
    these knobs take effect immediately). Kept here so the ~14 trend call sites stay
    DRY; config otherwise holds data only.
    """
    return TREND_MA_DAYS if TREND_MA_DAYS is not None else MA_LONG_DAYS


def stress_ma_days() -> int:
    """Resolve the stress-baseline MA lookback (STRESS_MA_DAYS override, else MA_LONG_DAYS)."""
    return STRESS_MA_DAYS if STRESS_MA_DAYS is not None else MA_LONG_DAYS


def trend_margin(scope: str) -> float:
    """Resolve the early-exit margin for an engine scope (per-engine override, else
    the global MA_GATE_BUFFER_PCT). scope in {regime, duration, realasset, sector}."""
    per = {
        "regime": REGIME_TREND_MARGIN,
        "duration": DURATION_TREND_MARGIN,
        "realasset": REALASSET_TREND_MARGIN,
        "sector": SECTOR_TREND_MARGIN,
    }.get(scope)
    return MA_GATE_BUFFER_PCT if per is None else per

# Regime thresholds -> equity band (% of client-version allowance). SPEC §4.
# (lower_score, upper_score): (band_low, band_high)
REGIME_BANDS = {
    "RiskOn":             {"score": (75, 100), "equity": (0.80, 1.00)},
    "RiskOnNarrowing":    {"score": (55, 74),  "equity": (0.60, 0.80)},
    "Caution":            {"score": (40, 54),  "equity": (0.35, 0.60)},
    "Defensive":          {"score": (25, 39),  "equity": (0.10, 0.35)},
    "CapitalPreservation":{"score": (0, 24),   "equity": (0.00, 0.15)},
}

# Hysteresis / whipsaw control (SPEC §4)
# Tuned 2026-06-24 (immediate-drop 10->20): don't panic-sell on a sub-20-point
# blip. Lifted CAGR 7.9->8.2%, Calmar 0.71->0.74, Sortino 0.89->0.91 at same maxDD
# (-11.1%) & cost (1.3%); softens the Mar-2026 whipsaw (42% not 24%) and held
# out-of-sample. Dead-zone left at 3 (raising it re-suppressed the fix). Originals: 10.
REGIME_CONFIRMATION_DAYS = 2       # hold trigger this long (range 2-4)
REGIME_MIN_THRESHOLD_CROSS = 3     # ignore crossings smaller than this many points
REGIME_IMMEDIATE_DROP_POINTS = 20  # drop > this many points -> immediate de-risk


# ---------------------------------------------------------------------------
# Equity composition / Sector satellite (SPEC §5)
# ---------------------------------------------------------------------------
SECTOR_TILT_PCT = 0.0              # default OFF; allowable 0.00-0.30 of sleeve
SECTOR_RS_LOOKBACKS_MONTHS = (3, 6)   # relative-strength lookbacks vs SPY
SECTOR_TREND_GATE_DAYS = 200          # 200-day trend gate
SECTOR_MAX_WEIGHT = 0.15              # max single sector (SPEC §5, §12)
SECTOR_COUNT_WHEN_USED = (3, 4)       # number of sectors held when tilt is on


# ---------------------------------------------------------------------------
# STUDY-ONLY equity-sleeve broadening tilt (PREREG_S0_equity_sleeve_broadening
# _2026-07-20). DEFAULT OFF and byte-neutral: with EQUITY_TILT_ENABLED = False
# the equity sleeve is assembled exactly as today (sector.select_sectors on the
# frozen SECTOR_TILT_PCT). Nothing here is a production knob; it exists only so
# the pre-registered study can be run behind a flag without touching frozen
# config. Reuses the sector engine's blessed gate/RS basis — no new indicator.
#   When ON: a static EQUITY_TILT_PCT fraction of the equity sleeve is carved out
#   for the best momentum-leading candidates among {size funds + 11 sectors},
#   gated on (above 200d trend) AND (positive RS vs SPY, score = mean(RS_3m,
#   RS_6m)); top-N by score, each weighted min(EQUITY_TILT_PCT/N, SECTOR_MAX_WEIGHT);
#   any unfilled tilt budget falls back to broad beta. The remaining
#   (1 - filled) of the sleeve stays broad beta (SPY/VTI/RSP) exactly as today.
# ---------------------------------------------------------------------------
EQUITY_TILT_ENABLED = False           # master STUDY flag; OFF => production byte-identical
EQUITY_TILT_PCT = 0.0                 # fraction of equity sleeve made available to the tilt
EQUITY_TILT_COUNT = 4                 # N: number of top candidates held (prereg sweeps 3/4/5)
EQUITY_TILT_SIZE_FUNDS = ("IJH", "IJR")     # default size pair (S&P mid/small, GICS-consistent)
EQUITY_TILT_SIZE_FUNDS_ALT = ("VO", "VB")   # pre-registered robustness swap pair (Vanguard)
EQUITY_TILT_USE_ALT_SIZE = False      # swap flag: use the VO/VB pair instead of IJH/IJR


# ---------------------------------------------------------------------------
# Duration Filter & Inflation/Deflation Engine (SPEC §6)
# ---------------------------------------------------------------------------
LONG_TSY_PERMISSION_MIN_PASSES = 4    # need ~4 of 5 permission rules
LONG_TSY_RETURN_MONTHS = 3            # TLT positive over 3 months
LONG_TSY_VS_TBILL_MONTHS = 3          # TLT outperforming T-bills over 3 months
LONG_TSY_MAX_DRAWDOWN = -0.10         # drawdown not worse than -10% from 252d high
LONG_TSY_DRAWDOWN_LOOKBACK_DAYS = 252

TBILL_VS_TSY_LOOKBACKS_MONTHS = (3, 6)  # ban check: T-bills outperform over 3/6m

# Duration caps by regime, % of TOTAL portfolio (SPEC §6, §12).
# Each value is (low, high).
DURATION_CAPS = {
    "long": {
        "RiskOn": (0.00, 0.10), "RiskOnNarrowing": (0.00, 0.10),
        "Caution": (0.00, 0.15),
        "Defensive": (0.00, 0.25), "CapitalPreservation": (0.00, 0.25),
    },
    "intermediate": {
        "RiskOn": (0.00, 0.10), "RiskOnNarrowing": (0.00, 0.10),
        "Caution": (0.00, 0.25),
        "Defensive": (0.00, 0.40), "CapitalPreservation": (0.00, 0.40),
    },
    "short": {
        "RiskOn": (0.00, 0.20), "RiskOnNarrowing": (0.00, 0.20),
        "Caution": (0.00, 0.50),
        "Defensive": (0.00, 1.00), "CapitalPreservation": (0.00, 1.00),
    },
    "tbill": {
        "RiskOn": (0.00, 0.20), "RiskOnNarrowing": (0.00, 0.20),
        "Caution": (0.20, 0.70),
        "Defensive": (0.50, 1.00), "CapitalPreservation": (0.50, 1.00),
    },
}

# Cap intermediate Treasuries low when the inflationary-bear filter is active.
INFLATIONARY_INTERMEDIATE_CAP = 0.15   # (0-15/20%); SPEC §6


# ---------------------------------------------------------------------------
# Defensive Engine ranking weights (SPEC §7) — must sum to 100
# ---------------------------------------------------------------------------
DEFENSIVE_SCORE_WEIGHTS = {
    "return_3m": 25,
    "return_6m": 20,
    "abs_trend": 20,
    "rel_vs_tbill": 15,
    "volatility_penalty": 10,
    "drawdown_penalty": 10,
}


# ---------------------------------------------------------------------------
# Volatility multiplier (SPEC §8) — subordinate trim within the regime band
# ---------------------------------------------------------------------------
VOL_LOOKBACK_DAYS = 63
# Multipliers applied to the band's mid/high point as realized vol rises.
VOL_BUCKET_MULTIPLIERS = (1.00, 0.85, 0.70)   # floor is the band bottom
# Target vol by client version (annualized). SPEC §8, §10.
TARGET_VOL_BY_VERSION = {
    "Conservative": (0.08, 0.10),
    "Balanced":     (0.10, 0.12),
    "Growth":       (0.12, 0.15),
}


# ---------------------------------------------------------------------------
# Re-entry ladder (SPEC §9)
# ---------------------------------------------------------------------------
REENTRY_STAGES = {
    1: {"equity_pct": 0.25},   # SPY > 50d MA, top sectors improving, vol stops rising
    2: {"equity_pct": 0.50},   # SPY > 200d/10m OR score back above 40
    3: {"equity_pct": 0.75},   # >=6 of 11 sectors > 200d OR breadth improves
    4: {"equity_pct": 1.00},   # score above 55-75 (by version), credit/vol normalize
}
REENTRY_STAGE3_SECTOR_COUNT = 6        # >=6 of 11 sectors above 200d
REENTRY_MAX_LAG_MONTHS = 6             # MAX-LAG override vs sharp V-recovery (tunable)
# Stage-4 ("full re-entry") score gate by client version (SPEC §9: "55-75 by
# version"). Conservative demands a healthier tape before going fully invested.
REENTRY_STAGE4_SCORE = {"Conservative": 75, "Balanced": 65, "Growth": 55}
REENTRY_BREADTH_IMPROVE = 0.05         # "materially improves" = breadth up this much M/M


# ---------------------------------------------------------------------------
# Client versions (SPEC §10) — selected via ACTIVE_VERSION
# ---------------------------------------------------------------------------
ACTIVE_VERSION = "Balanced"            # "Conservative" | "Balanced" | "Growth"

# Per-version equity allowance multiplier applied to the regime band, plus
# T-bill floors. These are starting points meant to be tuned in one place.
CLIENT_VERSIONS = {
    "Conservative": {"equity_allowance": 0.80, "tbill_floor": 0.10},
    "Balanced":     {"equity_allowance": 1.00, "tbill_floor": 0.05},
    "Growth":       {"equity_allowance": 1.00, "tbill_floor": 0.00},
}


# ---------------------------------------------------------------------------
# Real-asset sleeve (broken out 2026-06-24 — its OWN leg, not the defense budget)
# ---------------------------------------------------------------------------
# Gold/TIPS/commodities are sized as a deliberate third sleeve, carved out before
# the defensive (T-bill/Treasury) sleeve. Scaled by version as a risk asset (more
# for Growth). Trend-gated: held only when a real asset passes the §6 trend+momentum
# gate, else 0 (REAL_ASSET_STRATEGIC_FLOOR > 0 would force an always-on minimum).
# The §12 category caps (gold 25% etc.) remain hard ceilings above these targets.
REAL_ASSET_SLEEVE_TARGET = {"Conservative": 0.10, "Balanced": 0.15, "Growth": 0.20}
REAL_ASSET_STRATEGIC_FLOOR = 0.0   # 0 = pure tactical (current choice)

# The sleeve is a DIVERSIFIED basket (gold + broad commodities), not standalone gold
# (added 2026-06-24). Gold and commodities are ~uncorrelated (~0.05), so the basket's
# vol (~13.5%) is LOWER than either leg alone (~16-18%). TIPS excluded (it behaves
# like Treasuries -> defense). PDBC over DBC (no K-1 form, better for taxable accounts).
# Each leg is trend-gated independently; present legs are inverse-vol weighted.
REAL_ASSET_BASKET = {"gold": ["GLDM", "IAU"], "commodities": ["PDBC"]}
REAL_ASSET_VOL_LOOKBACK = 252      # trailing days for inverse-vol basket weighting

# DYNAMIC real-asset cap by macro regime (2026-06-24). The duration engine already
# classifies the regime each month; the sleeve TARGET is scaled by it, then clamped
# to REAL_ASSET_SLEEVE_MAX. Lean INTO real assets in inflation/stagflation (the only
# real-purchasing-power hedges), dial DOWN in deflation (Treasuries are the hedge
# there). The trend gate still decides whether to actually fill the raised ceiling.
# Tuned 2026-06-24 to the SATURATION point (L1): a sweep to 3.0/5.0 showed the
# response plateaus here (L1=L2=L3) and the base case is frozen across all levels
# (regime-gating confines the change to inflation/stagflation months). Pushing
# harder does nothing — the trend gate refuses non-trending real assets regardless.
REAL_ASSET_REGIME_SCALE = {"deflation": 0.75, "neutral": 1.0, "inflation": 2.0, "stagflation": 3.0}
REAL_ASSET_SLEEVE_MAX = 0.45       # hard ceiling on the sleeve in ANY regime
# "Stagflation" = a SUSTAINED inflationary-bear: active >= STAGFLATION_PERSISTENCE of
# the trailing STAGFLATION_LOOKBACK_DAYS (a transient spike stays "inflation").
STAGFLATION_LOOKBACK_DAYS = 126    # ~6 months
STAGFLATION_PERSISTENCE = 0.70

# EXPERIMENTAL risk-budget rotation (2026-06-24): in inflation/stagflation, real
# assets may SUBSTITUTE for some equity (rotate within the risk budget toward what's
# trending), not just draw from the defense budget. Fraction of the equity sleeve
# that may rotate to real assets, by regime. Total risk-asset exposure is preserved
# (equity down = real up); defense sleeve unchanged. OFF until validated; gated on
# real assets actually trending.
EQUITY_ROTATION_ENABLED = False
REAL_ASSET_EQUITY_ROTATION = {"inflation": 0.25, "stagflation": 0.50}


# ---------------------------------------------------------------------------
# Portfolio-level caps / floors (SPEC §12)
# ---------------------------------------------------------------------------
CAP_MAX_SECTOR = 0.15
CAP_MAX_GOLD = 0.25
CAP_MAX_COMMODITIES = 0.20
CAP_MAX_TIPS = 0.20
CAP_MAX_LONG_TREASURY = 0.25
CAP_MAX_INTERMEDIATE = 0.40


# ---------------------------------------------------------------------------
# Trading frictions (SPEC §13)
# ---------------------------------------------------------------------------
PER_TRADE_COST_BPS = 3.0           # a few basis points per trade
REBALANCE_FREQUENCY = "monthly"

# Taxable mode (SPEC §11 step 6): a no-trade band suppresses small rebalances to
# cut turnover/tax drag. When TAXABLE_MODE is on, an asset is only traded if its
# target differs from its drifted weight by more than TURNOVER_BAND.
TAXABLE_MODE = False
TURNOVER_BAND = 0.02               # 2% no-trade band per asset


# ---------------------------------------------------------------------------
# Whipsaw controls at portfolio assembly (SPEC §11)
# ---------------------------------------------------------------------------
RANK_REPLACEMENT_THRESHOLD = 10    # 10-point score gap to replace a holding
# (current holding wins ties — implemented in portfolio logic later)


# ---------------------------------------------------------------------------
# Validation switches (SPEC §16)
# ---------------------------------------------------------------------------
WALK_FORWARD_ENABLED = False
WALK_FORWARD_TRAIN_END = "2019-12-31"   # build params here, evaluate after
ASSERT_NO_LOOKAHEAD = True              # test toggle (SPEC §16)


# ---------------------------------------------------------------------------
# GEX gamma-regime OVERLAY (research; default OFF) — backtester-level post-process
# ---------------------------------------------------------------------------
# A backtester-only risk-SIZING overlay layered on top of S0's target weights. It
# does NOT touch the shared strategy brain and must leave S0 BYTE-IDENTICAL when off.
#
# Economic motivation (MSR/S1 verdict): dealer gamma's edge is SIZING + hedge-timing,
# NOT direction. When the as-of SPX gamma regime is NEGATIVE (dealers are short gamma
# and AMPLIFY moves -> fragile tape), trim RISK-asset exposure and park the trimmed
# weight in the existing cash sleeve. When Positive/Neutral, leave S0 untouched.
# This is sizing only — it never flips direction or changes which assets S0 picks.
#
# Strictly causal: the gamma_state applied at a rebalance is the most recent state
# as-of (on/before) the SIGNAL date, never the execution date — no look-ahead.
GAMMA_OVERLAY_ENABLED = False          # master flag — OFF keeps S0 byte-identical
GAMMA_OVERLAY_GEX_FILE = r"C:\TradingDesk-Local\warehouse\derived\SPX_gex_daily.parquet"
# Multiplier applied to RISK-asset weights when the as-of gamma_state is Negative.
# 1.0 = no change; 0.5 = halve risk; 0.0 = fully de-risk to cash. Trimmed weight is
# moved to the cash sleeve. Only "Negative" de-risks; Positive/Neutral/unknown pass.
GAMMA_OVERLAY_NEGATIVE_RISK_SCALE = 0.5
# Tickers treated as RISK assets by the overlay (equity beta + real assets). Anything
# NOT in this set (T-bills, Treasuries, floating-rate, TIPS) is treated as defensive
# and is the destination for trimmed weight. The cash ticker chosen for parking is the
# largest-weight cash-like holding already in the book (fallback BENCHMARK_TBILL).
GAMMA_OVERLAY_RISK_ASSETS = tuple(EQUITY_CORE + SECTORS + GOLD + COMMODITIES)
GAMMA_OVERLAY_CASH_TICKER = BENCHMARK_TBILL  # fallback park destination


# ---------------------------------------------------------------------------
# FLOW de-risk OVERLAY (research; default OFF) — backtester-level post-process
# ---------------------------------------------------------------------------
# A backtester-only risk-SIZING overlay layered on top of S0's target weights,
# the SAME pattern as the gamma overlay. It does NOT touch the shared strategy
# brain and must leave S0 BYTE-IDENTICAL when off.
#
# Economic motivation (Flow Project verdict, FLOW_VERDICT.md): the vendor flow
# signal is DROPPED for direction, but the free PRICE-ONLY de-risk gate is real.
# A Bullish/Neutral/Bearish positioning state is reconstructed from SPY price
# alone (px vs MA200 + 12m-1m momentum + 252d realized-vol percentile rank):
#   Bearish : px < MA200            OR  rvol_rank > vol_top   (downtrend or vol spike)
#   Bullish : px > MA200 AND mom>0  AND rvol_rank < vol_calm  (uptrend AND calm)
#   Neutral : otherwise
# When Bearish, trim RISK-asset exposure and park it in the existing cash sleeve.
#
# Two pre-specified variants from the verdict (no extra grid):
#   G1 "flat"  : risk x0.0 when Bearish; x1.0 Bullish/Neutral.
#   G2 "sized" : risk x1.0 / x0.5 / x0.0 for Bullish / Neutral / Bearish.
#
# Strictly causal: every feature uses only data on/before the SIGNAL date, and the
# state applied at a rebalance is the most recent state as-of the signal date.
FLOW_OVERLAY_ENABLED = False           # master flag — OFF keeps S0 byte-identical
FLOW_OVERLAY_VARIANT = "G1"            # "G1" (flat) | "G2" (1/0.5/0 sizing)
FLOW_OVERLAY_PRICE_TICKER = "SPY"      # price series the proxy is computed from
# Proxy knobs — the verdict's defaults (do NOT tune these to manufacture a win).
FLOW_OVERLAY_MA_LEN = 200
FLOW_OVERLAY_MOM_LONG = 252
FLOW_OVERLAY_MOM_SKIP = 21
FLOW_OVERLAY_RVOL_WIN = 21
FLOW_OVERLAY_VOL_RANK_WIN = 252
FLOW_OVERLAY_VOL_TOP = 0.80
FLOW_OVERLAY_VOL_CALM = 0.70
# Risk-asset set and park destination (same as the gamma overlay).
FLOW_OVERLAY_RISK_ASSETS = tuple(EQUITY_CORE + SECTORS + GOLD + COMMODITIES)
FLOW_OVERLAY_CASH_TICKER = BENCHMARK_TBILL
