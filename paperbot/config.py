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
# connections package — `from connections import ibkr_paper, clientids`. They are NOT
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
    # "max_position_pct_nav": REMOVED 2026-08-25 by owner decision (Andrew). There is NO
    # per-position ceiling on this desk. It was never authorized: the 0.35 value (and the
    # 5% placeholder before it) entered in the pre-git baseline snapshot 3b29616 with no
    # decision record, and its own comment admitted the earlier 5% would have VETOED the
    # strategy itself — a number retuned to fit the book it was supposed to police is not a
    # control. The strategy's own per-asset caps (SPEC §12) are the real per-position
    # constraint. DO NOT RE-ADD. (The single-order-notional sanity check in risk_manager —
    # one order may not exceed NAV — is a different guard and is untouched.)
    # "max_daily_loss_pct_nav": REMOVED 2026-08-25 by owner decision (Andrew). There is NO
    # automated daily-loss halt on this desk. It was never authorized: the -2% value entered
    # in the pre-git baseline snapshot and no decision record for it ever existed. DO NOT
    # RE-ADD. (The MANUAL, file-based operator stop — the AUTOTRADE_DISABLED sentinel /
    # KILL_SWITCH label — is a different thing entirely and is untouched.)
    "max_legs_per_order": 1,          # S0 trades single ETFs; >1 leg = malformed for S0
    "cash_reserve_pct": 0.015,        # keep >= 1.5% of NAV in cash (no leverage + a buffer);
                                      # positions are sized against NAV*(1-this) so the
                                      # reserve is respected by construction. Slice 2 of the
                                      # account-cashflow build re-based this 5%->1.5% so risk
                                      # positions size ~3.5% of NAV closer to the model.
                                      # This is the DEFAULT for every model that does not
                                      # name its own, and it is S0's value. S0 is validated
                                      # at 1.5% and DOES NOT MOVE.
    # PER-MODEL OVERRIDE: Andrew-authored ("custom") allocations hold 1%, not 1.5%.
    # WHY A RESERVE AT ALL (operational, not cosmetic): IBKR deducts its advisory fee from
    # account CASH, and client distributions are paid from cash. A fee deducted from a
    # fully-invested account overdraws it — that is exactly the 2026-07-28 negative-balance
    # incident. WHY 1%: Andrew's call — enough fee/distribution headroom on a hand-authored
    # book while leaving less of the client's money undeployed.
    # Resolved SOURCE-based (does the label have rows in the CRM custom-allocation view),
    # NEVER by the label's spelling — see investable.buffer_pct_for /
    # custom_target.reserve_pct_by_label.
    "custom_allocation_cash_reserve_pct": 0.01,
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
# DU8922144 (Balanced) and DU8922146 (Growth) were pulled 2026-07-09 (Andrew's
# decision) to free them up for testing other strategies later; their S0 positions are
# being liquidated separately.
#
# SOURCE OF TRUTH: the live CRM roster (`public.v_tradingdesk_roster`, read via
# `paperbot/roster.py` -> `crm_roster`) is now the authoritative account -> strategy map.
# This ENROLLMENT dict is a DEGRADED-MODE FALLBACK ONLY — the live CRM roster is the source
# of truth. `roster.enrolled_roster()` falls back to it when the CRM DSN
# (`TRADINGDESK_CRM_DSN`) is unset or the CRM is unreachable, so the account wall always
# has a deterministic allow-list; several monitor/reporting paths also still read it
# directly, so it stays in place. (The old hand-maintained `conductor/ACCOUNT_ALLOCATION.md`
# map was RETIRED 2026-08-05 — superseded by the roster feed.)
VALID_VERSIONS = ("Conservative", "Balanced", "Growth")
ENROLLMENT = {
    # account number  : strategy version  (set to each client's risk profile)
    "DU8922142": "Conservative",
    "DU8922143": "Balanced",
    "DU8922145": "Growth",
}
REBALANCE_MASTER = False   # the DF...141 master is the advisor account; not traded.

# --- Corp-action / sweep whitelist (discover-then-whitelist anchor) -------------
# Symbols to EXCLUDE from the ALIEN (corp-action / manual-holding) classification even
# though the strategy's universe does not contain them — a real cash / money-market
# SWEEP fund the live account holds by design, not a corporate action to review. A
# whitelisted symbol is classified SWEEP (never ALIEN): it does not breach the band, no
# order is emitted against it, and no CORP-ACTION review alert pages on it.
#
# MECHANISM ONLY — DEFAULT EMPTY (discover-then-whitelist, reviewer decision Q4). Do NOT
# pre-hardcode any sweep symbol: the live account's exact sweep ticker(s) are named by
# Andrew once the live account is known (prereq #4), then added here. Until then this is
# empty and dormant. The cash bucket anchor (investable.CASH_SYMBOL) is ALWAYS excluded
# from ALIEN in addition to this set; this set EXTENDS that single anchor.
SWEEP_WHITELIST: set[str] = set()

# FULL EXIT SELLS THE WHOLE POSITION, FRACTION INCLUDED (owner decision, Andrew Surber,
# 2026-09-04). When the model wants NONE of a symbol the order is the entire holding rather
# than int() of it. Truncating manufactured a permanent stub: 13 of 13.8499 sold, 0.8499 left,
# and on the next run int(0.8499) == 0 classifies the line FRACTIONAL, which is never
# auto-traded. Set False to restore whole-share-only exits if IBKR refuses a fractional sell.
SELL_WHOLE_POSITION_ON_EXIT = True

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

# --- block outcome discipline (v0.53.0, after the 2026-09-04 Balanced run) ---------------
# A $354,000 BUCK sell was given the standard 90-second window, did not fill, was cancelled,
# and the run carried on into the buy phase it was supposed to fund. Twelve of twenty-five
# blocks ended up doing nothing and the desk reported "25 blocks sent" in a green box.
#
# LARGE_BLOCK_NOTIONAL — above this, a block is WORKED rather than raced: it gets
# LARGE_BLOCK_TIMEOUT_SEC instead of the standard phase window, because a block that is large
# relative to the book fills in pieces over minutes and 90 seconds only guarantees a cancel.
LARGE_BLOCK_NOTIONAL = 50_000.0
LARGE_BLOCK_TIMEOUT_SEC = 600.0
# Large blocks get IBKR's Adaptive algo on URGENT. Urgent CROSSES the spread (Patient
# does not, which is what left every buy unfilled on 2026-09-04) but works the order into
# the book instead of sweeping it in one print. The capped marketable limit remains the
# hard ceiling, so the algo can only improve the fill, never worsen it. Small blocks stay
# on a plain marketable limit -- they cross and they are done.
LARGE_BLOCK_ADAPTIVE_PRIORITY = "Urgent"

# BLOCK ORDERS ARE WHOLE SHARES. NOT a policy choice -- IBKR refuses anything else.
# Measured on the live master 2026-09-04, quoting the broker verbatim:
#
#   Error 10243: Fractional-sized order cannot be placed via API.
#                Please use desktop version to place this order.
#
# Five sell blocks carrying fractional quantities (BIL 865.3444, BUCK 6060.1915,
# GDXJ 64.5439, SIL 83.9454, XLP 102.1601) were ALL rejected with that code in one run,
# while SILJ -- the only whole-share sell in the same run, same algo, same window --
# filled 130 @ 31.74. The JAAA block rejected an hour earlier was the same thing.
#
# CONSEQUENCE, stated plainly: a full exit computed as 865.3444 places 865 and leaves
# 0.3444 behind. The engine still computes the exact exit and verify_in_sync still reports
# the remaining stub, so nothing is hidden -- but per IBKR's own message the API cannot
# clear it at all. Clearing a sub-share stub requires the desktop platform, by hand.
BLOCK_ORDERS_WHOLE_SHARES_ONLY = True

# THE GATEWAY'S OWN PRECAUTIONARY LIMITS, mirrored here so the desk refuses an oversized
# block BEFORE sending it rather than learning from a broker rejection mid-run. Owner set
# these in TWS/Gateway on 2026-09-04: Order Size Limit 100,000 shares, Order Value Limit
# $5,000,000. KEEP THESE IN STEP WITH THE GATEWAY -- if they drift apart the desk either
# refuses orders IBKR would accept, or sends orders IBKR will bounce.
#
# Found on the 2026-09-04 Growth (Custom) plan: SELL BUCK 347,419 shares / $8,098,337 broke
# both. It was also 105% of BUCK's entire daily volume against 4,200 shares at the bid, so
# the limits were the least of its problems -- but nothing should reach the wire unchecked.
GATEWAY_ORDER_SIZE_LIMIT = 100_000
GATEWAY_ORDER_VALUE_LIMIT = 5_000_000.0
# HALT_BUYS_ON_UNFILLED_SELL — the sell phase's gate is "every block reached a TERMINAL state",
# and Cancelled is terminal. That let the run proceed to buys the failed sell was funding. When
# any sell block ends short, STOP: report it and place no buys. The buys are re-sized to
# realized cash anyway, so this costs nothing except a run that would have churned.
HALT_BUYS_ON_UNFILLED_SELL = True

