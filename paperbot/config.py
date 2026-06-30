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
    "cash_reserve_pct": 0.015,        # keep >= 1.5% of NAV in cash (no leverage + a buffer);
                                      # positions are sized against NAV*(1-this) so the
                                      # reserve is respected by construction. Slice 2 of the
                                      # account-cashflow build re-based this 5%->1.5% so risk
                                      # positions size ~3.5% of NAV closer to the model.
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

# --- Laddered order-execution router (see docs/IBKR_ORDER_TYPES_RESEARCH.md) ------
# The 2026-06-29 rebalance sent a STATIC limit at the neutral reference price with no
# escalation, so the wide-spread Treasury/cash legs (TFLO all accounts, VGSH on DU142)
# never crossed and died at session disconnect. The router below replaces the single
# scalar ORDER_STYLE with an instrument-aware RECIPE: a per-class ordered ladder of
# rungs that escalates from passive (best price) toward marketable (fill certainty),
# and ALWAYS terminates at a marketable-cap rung so a leg can never hang open again.
#
# This is policy data only — no orders are placed by importing it, and the existing
# review->arm->transmit gate (order_router.transmit_guard, fails closed) is unchanged.

# Instrument classes. The classifier (order_router.classify_instrument) is data-driven:
# it consults these seed sets first, then falls back to a live spread-width heuristic.
INSTRUMENT_CLASS_LIQUID_ETF = "LIQUID_ETF"
INSTRUMENT_CLASS_ILLIQUID_ETF = "ILLIQUID_ETF"
INSTRUMENT_CLASS_INDEX_OPTION = "INDEX_OPTION"

# Seed symbol sets (extend as the universe grows). Known liquid ~1-tick-spread ETFs
# cross immediately; known thin Treasury/cash ETFs are the ones that hung on 06-29.
LIQUID_ETF_SYMBOLS = {"SPY", "VTI", "RSP", "PDBC"}
ILLIQUID_ETF_SYMBOLS = {"TFLO", "VGSH", "SHV", "BIL", "GBIL", "SGOV", "ICSH", "JPST"}

# Spread-width heuristic for symbols NOT in a seed set: an ETF whose live relative
# spread (ask-bid)/mid exceeds this is treated as ILLIQUID (gets the full ladder);
# at or under it, LIQUID (cross now). Index options never hit the heuristic — they are
# classified by security type (sec_type="OPT") and get the options-only recipe.
ILLIQUID_SPREAD_THRESHOLD = 0.0015   # 15 bps of mid

# Cap budget `k`: the marketable cap is BUY ask*(1+k) / SELL bid*(1-k). The cap is a
# worst-case price the engine will pay to GET DONE on a rung; the peg/algo usually
# fills better. Validated through the HARD PRICE GUARD on every rung.
ORDER_CAP_K = 0.003               # 30 bps over/under the touch as the marketable cap

# Per-rung watch window (seconds) and per-rung poll cadence. Tight per the
# supervise-long-ops rule: place, watch with flushed progress, then cancel+escalate.
LADDER_RUNG_SECONDS = 15          # how long to watch each rung before escalating
LADDER_POLL_SECONDS = 1.0         # fill-watch poll cadence within a rung

# The RECIPE: instrument class -> ordered list of ladder rungs. Each rung is a dict
# {"order_type": <builder kind>, ...}. The router walks rungs in order, re-placing only
# the UNFILLED remainder, until filled or the terminal (marketable-cap) rung. The final
# rung of every ladder is a marketable limit, so the ladder always terminates.
#   order_type values:
#     "marketable_limit" -> capped LMT that crosses the spread now
#     "midprice"         -> Order(orderType="MIDPRICE", lmtPrice=cap)   [stocks/ETFs ONLY]
#     "adaptive"         -> LMT + algoStrategy="Adaptive", priority Patient|Normal|Urgent
#     "rel"              -> Order(orderType="REL", auxPrice=cap)        [options-safe peg]
ORDER_LADDER = {
    INSTRUMENT_CLASS_LIQUID_ETF: [
        {"order_type": "marketable_limit"},
        {"order_type": "adaptive", "priority": "Urgent"},
    ],
    INSTRUMENT_CLASS_ILLIQUID_ETF: [
        {"order_type": "midprice"},
        {"order_type": "adaptive", "priority": "Patient"},
        {"order_type": "adaptive", "priority": "Urgent"},
        {"order_type": "marketable_limit"},
    ],
    # NEVER MIDPRICE on options (unsupported) and NO scheduler algos. Capped LMT, then a
    # REL peg toward marketable (capped via auxPrice), then a hard marketable LMT.
    INSTRUMENT_CLASS_INDEX_OPTION: [
        {"order_type": "marketable_limit"},
        {"order_type": "rel"},
        {"order_type": "marketable_limit"},
    ],
}

# GTC-REMAINDER LAYER ("ladder while connected, rest when gone"). See
# docs/IBKR_RESTING_CONDITIONAL_ORDERS.md §6. After the terminal (marketable-cap) rung's
# watch window, if quantity is STILL unfilled, do not give up: convert the remainder to a
# RESTING plain LMT at the cap with tif="GTC" and leave it at IB so the leg cannot die at
# session disconnect (the exact failure that killed the 2026-06-29 TFLO/VGSH DAY legs).
# The rest MUST be a plain LMT — Adaptive forbids GTC and MIDPRICE is DAY-only, so neither
# can carry the resting tif. Reported as "RESTING (GTC)", never "failed".
LADDER_REST_REMAINDER = True

# FA-block compatibility with MIDPRICE/Adaptive is UNCONFIRMED (docs are single-account;
# needs a live PAPER probe — see research §4/§6). Until proven, FA *block* (group) legs
# stay on the existing safe capped-limit path; only DIRECT (lone-account) legs get the
# full ladder. Flip this ONLY after a probe confirms algos ride an FA block.
LADDER_FA_BLOCKS = False

# FA-block MARKETABLE pricing (approach b). The armed run on 2026-06-29 placed FA blocks
# fine and the liquid equity legs (PDBC/RSP/SPY/VTI) filled, but the illiquid TFLO block
# legs (DU143-146) did NOT fill because the block limit was priced at the neutral
# reference/mid and never crossed the thin book. Until the MIDPRICE/Adaptive-on-FA-block
# probe is done (LADDER_FA_BLOCKS above), price the single FA-block limit MARKETABLE — the
# same cap the direct ladder uses (BUY ask*(1+k) / SELL bid*(1-k), via
# live_quotes.marketable_cap, ORDER_CAP_K) — so a thin-book block leg actually crosses.
# Liquid block legs get ~touch (harmless on a 1-tick spread). If no usable quote is
# available, the block falls back to the neutral reference price (then the HARD PRICE GUARD
# still rejects NaN/<=0). This is NOT an algo on the block — it is a plain marketable LMT.
FA_BLOCK_MARKETABLE = True

