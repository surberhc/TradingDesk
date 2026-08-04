"""
s0_live.py — the read-only S0 (adaptive_all_weather) LIVE-PILOT connection lane.

Strategy Zero's automated morning execution (paperbot/morning_execute_run.py) has always
run against the PAPER Gateway (port 4002, the DU...141 FA account family). This module
adds the read-only connection lane that lets an S0 PILOT cycle read a REAL, funded account
instead, so the "WOULD HAVE TRANSMITTED" reports Andrew reviews before ever flipping
PILOT_MODE (conductor #3/#41) reflect genuine account state — real NetLiquidation, real
positions, real margin — not the simulated paper account.

CONNECTION TARGET — the live-TRADING Gateway (connections.ibkr_live_trade, port 4003), the
SAME gateway S8's live pilot uses. Its login covers TWO live-trading accounts:
  * U14438624 — the funded TRUST account — S0's real-money execution account. On 2026-07-28
    S0 execution was retargeted to U14438624, and on 2026-07-29 it was conformed to S0
    Growth. THIS is S0_LIVE_ACCOUNT. (S8's live cycle does not trade a sub-account — its
    s8_config.ACCOUNT is informational-only — so U14438624 is not "S8's" account.)
  * U5721712  — the INDIVIDUAL account — S0's RETIRED former pilot account. It held ~$957
    and is PDT-blocked under $25k; no longer traded.
S0 reads only U14438624, so every read here is filtered to S0_LIVE_ACCOUNT (see
filter_account_summary / filter_positions), mirroring livebot/s8_runner.filter_account_summary.

ZERO-TRANSMIT, two independent walls (identical posture to the S8 pilot):
  1. PRIMARY, load-bearing: PILOT_MODE=True (hardcoded) in paperbot/morning_execute_run.py.
     Nothing transmits while it is set.
  2. Fail-safe: connect_s0_live() calls ibkr_live_trade.connect(readonly=True) and NEVER
     passes readonly=False. The gateway account is transmit-capable at the broker level, so
     readonly is a real, honored session flag — a bare connection here can never write.
The read-only default is a SECONDARY control, NOT a substitute for PILOT_MODE.

This module is connection + account-pin plumbing ONLY: it builds no orders and calls no
order-placement method. Wiring morning_execute_run.py to actually SOURCE its targets from
this live account (single-account sizing, vs the paper FA multi-account model) is a
separate, order-adjacent step that carries the paperbot/version.py bump.
"""
from __future__ import annotations

from connections import ibkr_live_trade

# The funded TRUST account, S0's real-money execution account: S0 execution was retargeted
# to U14438624 on 2026-07-28 and conformed to S0 Growth on 2026-07-29. The former individual
# pilot account U5721712 under the same 4003 login is now RETIRED (held ~$957, PDT-blocked
# under $25k). U14438624 is not "S8's" — S8's live cycle trades no sub-account.
S0_LIVE_ACCOUNT = "U14438624"


def connect_s0_live(launch: bool = False, timeout: int = 10):
    """Connect the S0 pilot READ-ONLY to the live-trading Gateway (port 4003).

    Uses the "s0_live_pilot" clientId (distinct from every S8 consumer on 4003 so the S0
    pilot and the S8 pilot/monitor/collector can all poll 4003 concurrently without a
    collision). ALWAYS read-only: this function never passes readonly=False. PILOT_MODE in
    morning_execute_run.py — not this default — is the primary zero-transmit wall.
    """
    return ibkr_live_trade.connect("s0_live_pilot", launch=launch,
                                   readonly=True, timeout=timeout)


def connect_s0_live_armed(timeout: int = 10):
    """ARMED connect for s0_live_exec's tiny-test path ONLY. Passes readonly=False so the
    session CAN transmit; the gateway's own read-only toggle + arming.probe_api_readonly
    stay the physical human wall, and s0_live_exec's caps + --arm-i-understand are the code
    wall. Uses clientId s0_live_exec (58). NEVER call this from the read-only pilot path."""
    return ibkr_live_trade.connect("s0_live_exec", launch=False, readonly=False,
                                   timeout=timeout)


def filter_account_summary(summary, account: str = S0_LIVE_ACCOUNT):
    """Keep only the accountSummary rows for `account` (default S0_LIVE_ACCOUNT).

    The 4003 live-trade login exposes MORE THAN ONE managed account (S0's execution account
    U14438624, the retired individual account U5721712, plus an aggregate 'All' scope), so an
    unfiltered ib.accountSummary() blends them. Filtering to S0's account first makes any
    downstream read (NetLiq, margin) deterministic and guarantees the S0 lane reads only its
    own execution account's numbers. Mirrors livebot/s8_runner.filter_account_summary.

    A dict {tag: value} already represents a single account and is returned unchanged.
    """
    if isinstance(summary, dict):
        return summary
    return [r for r in summary if getattr(r, "account", None) == account]


def filter_positions(positions, account: str = S0_LIVE_ACCOUNT):
    """Keep only the position/portfolio rows for `account` (default S0_LIVE_ACCOUNT).

    Same rationale as filter_account_summary: the 4003 login exposes multiple accounts, so
    ib.positions()/ib.portfolio() return rows for all of them. S0 reconciles only its own
    execution account U14438624 and never reads the retired account's holdings.
    """
    return [p for p in positions if getattr(p, "account", None) == account]
