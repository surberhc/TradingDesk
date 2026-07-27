"""
s0_live.py — the read-only S0 (adaptive_all_weather) LIVE-PILOT connection lane.

Strategy Zero's automated morning execution (paperbot/morning_execute_run.py) has always
run against the PAPER Gateway (port 4002, the DU...141 FA account family). This module
adds the read-only connection lane that lets an S0 PILOT cycle read a REAL, funded account
instead, so the "WOULD HAVE TRANSMITTED" reports Andrew reviews before ever flipping
PILOT_MODE (conductor #3/#41) reflect genuine account state — real NetLiquidation, real
positions, real margin — not the simulated paper account.

CONNECTION TARGET — the live-TRADING Gateway (connections.ibkr_live_trade, port 4003), the
SAME gateway S8's live pilot uses. Its login covers TWO individual live-trading TEST
accounts:
  * U14438624 — the TRUST account — S8's pilot account (livebot/s8_config.py). NOT ours.
  * U5721712  — the INDIVIDUAL account — S0's pilot account. THIS is S0_LIVE_ACCOUNT.
S0 must only ever read U5721712 and must never touch the trust account, so every read here
is filtered to S0_LIVE_ACCOUNT (see filter_account_summary / filter_positions), mirroring
livebot/s8_runner.filter_account_summary.

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

# The individual live-trading TEST account, in Andrew's name, chosen 2026-07-27 as S0's
# live-pilot account. The trust account U14438624 under the same 4003 login is S8's —
# never read or touch it from the S0 lane.
S0_LIVE_ACCOUNT = "U5721712"


def connect_s0_live(launch: bool = False, timeout: int = 10):
    """Connect the S0 pilot READ-ONLY to the live-trading Gateway (port 4003).

    Uses the "s0_live_pilot" clientId (distinct from every S8 consumer on 4003 so the S0
    pilot and the S8 pilot/monitor/collector can all poll 4003 concurrently without a
    collision). ALWAYS read-only: this function never passes readonly=False. PILOT_MODE in
    morning_execute_run.py — not this default — is the primary zero-transmit wall.
    """
    return ibkr_live_trade.connect("s0_live_pilot", launch=launch,
                                   readonly=True, timeout=timeout)


def filter_account_summary(summary, account: str = S0_LIVE_ACCOUNT):
    """Keep only the accountSummary rows for `account` (default S0_LIVE_ACCOUNT).

    The 4003 live-trade login exposes MORE THAN ONE managed account (S0's individual
    account U5721712, S8's trust account U14438624, plus an aggregate 'All' scope), so an
    unfiltered ib.accountSummary() blends them. Filtering to S0's account first makes any
    downstream read (NetLiq, margin) deterministic and guarantees the S0 lane never reads
    the trust account's numbers. Mirrors livebot/s8_runner.filter_account_summary.

    A dict {tag: value} already represents a single account and is returned unchanged.
    """
    if isinstance(summary, dict):
        return summary
    return [r for r in summary if getattr(r, "account", None) == account]


def filter_positions(positions, account: str = S0_LIVE_ACCOUNT):
    """Keep only the position/portfolio rows for `account` (default S0_LIVE_ACCOUNT).

    Same rationale as filter_account_summary: the 4003 login exposes multiple accounts, so
    ib.positions()/ib.portfolio() return rows for all of them. S0 reconciles only its own
    individual account U5721712 and must never see the trust account's holdings.
    """
    return [p for p in positions if getattr(p, "account", None) == account]
