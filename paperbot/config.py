"""
config.py — paper trading engine settings (single place to change any knob).

EVERYTHING here targets the IBKR PAPER account only. There is no real-money
configuration in this project. Paper login, paper port 4002.

Account model (all paper): the gateway exposes an FA paper structure -
  DF8922141  -> FA paper MASTER; the ADVISOR / connection account (suffix "141"),
                ~$31.8k, not itself rebalanced (see REBALANCE_MASTER below).
  DU8922142..146 -> paper CLIENT sub-accounts, each funded ~$1.1M paper and visible
                    under the master. These are the accounts the multi-account
                    (Option B) engine rebalances, per the ENROLLMENT map below.
Run accounts.py for a live, read-only snapshot of the structure and funding.
"""
from __future__ import annotations

# --- Connection ----------------------------------------------------------------
# The host, paper port (4002), and clientId (30) all come from the shared
# connections package — `from connections import ibkr, clientids`. They are NOT
# duplicated here, so there is one source of truth and nothing collides.

# The paper account we intend to use ends in these digits. The engine REFUSES to
# act on any account whose number does not end with this — a hard guard against
# pointing at the wrong account.
ACCOUNT_SUFFIX = "141"

# --- Safety posture ------------------------------------------------------------
# READ-ONLY connection: while True, the API session physically cannot transmit an
# order. We stay here through the early build. Flipping to False is a deliberate,
# separate step that only happens AFTER the RiskManager + kill switch exist.
READONLY = True

# DRY-RUN: even once we use a non-read-only connection, the engine only LOGS the
# orders it would place until a human explicitly arms the session. No auto-arm.
DRY_RUN = True

# --- Risk limits (enforced by paperbot.risk_manager before any transmission) -----
RISK_LIMITS = {
    # Per-position ceiling on RISK assets only (equities, sectors, gold, commodities,
    # long/intermediate Treasuries). Cash-equivalents (T-bills, floating-rate, short
    # Treasuries) are EXEMPT - concentrating in cash-equivalents is de-risking, not risk.
    # This is a fat-finger BACKSTOP, not a strategy constraint: Adaptive All-Weather
    # enforces its own per-asset caps (SPEC §12) and legitimately holds ~26.67% in each
    # equity-core ETF, so the old 5% placeholder would have vetoed the strategy itself.
    # 0.35 sits just above S0's largest single risk position. (The tight 5% cap belongs
    # to the future single-name / options strategies, not the S0 ETF allocation.)
    "max_position_pct_nav": 0.35,
    "max_daily_loss_pct_nav": 0.02,   # KILL SWITCH: halt ALL trading at -2% on the day
    "max_legs_per_order": 1,          # S0 trades single ETFs; >1 leg = malformed for S0
    "cash_reserve_pct": 0.05,         # keep >= 5% of NAV in cash (no leverage + a buffer);
                                      # positions are sized against NAV*(1-this) so the
                                      # reserve is respected by construction
    # per_trade_max_loss: defined per-strategy later (e.g. condor max loss = wing width).
}

# --- Local runtime state (OFF Drive; kill-switch trip flag, run logs) -------------
# Like the dailyreport/warehouse, paperbot STATE lives on local C: so Drive sync can
# never corrupt a running file. Code stays in Drive; state does not.
STATE_DIR = r"C:\TradingDesk-Local\state\paperbot"

# --- Strategy selection --------------------------------------------------------
# The first strategy to run in paper: Adaptive All-Weather (S0), imported from the
# shared `strategies` package so the paperbot runs the EXACT code the backtester
# validated (`from strategies.all_weather import AdaptiveAllWeather`).
STRATEGY_NAME = "adaptive_all_weather"
STRATEGY_VERSION = "Balanced"   # matches strategies.config.ACTIVE_VERSION
                                # (single-account default; multi-account uses ENROLLMENT below)

# --- Multi-account enrollment (FA / Option B) ----------------------------------
# Each CLIENT (sub) account we rebalance is mapped to the strategy VERSION that
# matches that client's risk profile. The shared `strategies` engine produces a
# version's target weights; the multi-account engine scales them to each account's
# own investable capital and diffs against that account's own holdings.
#
# Keys are FULL account numbers (not the "141" suffix). VALID_VERSIONS gates typos.
# The FA master (DF...141) is the ADVISOR/connection account and is intentionally
# NOT enrolled here — we rebalance client sub-accounts, not the master. (Flip
# REBALANCE_MASTER if you ever want the master traded too; off by design.)
#
# EDIT per real client. Until a sub-account is actually funded + visible under the
# master (run accounts.py to check), enrolling it is a no-op the engine will skip.
VALID_VERSIONS = ("Conservative", "Balanced", "Growth")
ENROLLMENT = {
    # account number  : strategy version  (set to each client's risk profile)
    "DU8922142": "Conservative",
    "DU8922143": "Balanced",
    "DU8922144": "Balanced",
    "DU8922145": "Growth",
    "DU8922146": "Growth",
}
REBALANCE_MASTER = False   # the DF...141 master is the advisor account; not traded.

# Drift band (no-trade band): an account is LEFT ALONE until some holding drifts beyond
# this fraction of NAV from its model weight. Once any holding breaches it, the account
# is rebalanced fully back to target. Applied identically to every account so there is
# no per-client discretion to defend (the compliance point). Sized above ordinary market
# noise but tight enough that a real contribution/distribution trips it.
REBALANCE_BAND_PCT = 0.03

# --- Order style ---------------------------------------------------------------
ORDER_STYLE = "limit"   # agreed default for paper. (limit | marketable_limit | market)
