"""
s8_risk.py — S8-specific margin/account preflight gate (Stage 4 of the 5-stage S8 build,
see the approved build plan). PURE over an account-summary mapping — never connects,
never transmits, never touches a broker; the caller (a later stage, s8_runner.py) reads
`ib.accountSummary()` and feeds it in AT DEPLOY time only.

WHOSE accountSummary THIS ACTUALLY IS
--------------------------------------
s8_runner.py routes its live cycle exclusively through `connections.ibkr_live_trade` (the
live-TRADING Gateway, port 4003 — a funded, transmit-capable test account, connected
read-only during the pilot), not the paper Gateway. That login exposes MORE THAN ONE
managed account (a trust + an individual test account), so s8_runner.py filters
`ib.accountSummary()` down to `livebot/s8_config.py`'s `ACCOUNT` BEFORE calling this
function (see `s8_runner.filter_account_summary`). The `summary` this function receives is
therefore the TARGET account's own rows only. This function is generic over any
accountSummary shape (dict or ib_async row list, see `_summary_map`) and does not itself
filter — the caller owns that. (An earlier version of this note claimed the login served a
single account so no filter was needed; that became wrong once the login carried two
accounts, and the runner now filters explicitly. `_summary_map` is last-write-wins across
rows, so feeding it an UNfiltered two-account summary would let it read the wrong
account's AccountType/BuyingPower — exactly what the filter prevents.)

WHY A SIBLING (not an edit to s4_risk.py)
------------------------------------------
s4_risk.py::margin_preflight() is built around a LONG-ONLY equity notional: NAV * exposure.
S8 trades defined-risk OPTION SPREADS (credit verticals) — there is no NAV/exposure pair;
the relevant "required capital" for one spread entry is its defined-risk max loss, not a
fraction of account NAV. So this module keeps s4_risk.py's exact shape (same tag-pulling
convention, same PreflightResult shape, same fail-closed policy spirit, same dual dict/
ib_async-list input support) but swaps the "required_notional" calculation for the option
spread's defined-risk formula instead of reinventing the margin-account-classification
logic — that logic (`account_is_margin` / `_summary_map` / `_num`) is imported straight
from s4_risk.py rather than duplicated, so account-type classification can never drift
between S4 and S8.

THE FORMULA (frozen, independently verified this session against real IBKR data)
----------------------------------------------------------------------------------
For one S8 credit-spread entry of `qty` contracts, width `width_points` (points between
short and long strike), and `realized_credit` (points, honest bid/ask net credit — see
s8_strategy.py's pick_spread_by_credit()):

    required_notional = (width_points * 100 * qty) - (realized_credit * 100 * qty)

This is the position's defined-risk max loss (multiplier 100 per SPX/SPXW point, per
standard index-option contract size). It is NOT invented for this module: it is the exact
formula documented and independently verified in `british_ic/bic_2026_execution_profile.py`
against TAT's own real `BuyingPower` field — confirmed an EXACT match (ratio 1.000000
across 4,687 real rows) — i.e. TAT's BuyingPower IS this formula, not an independent SPAN
figure. Using the same formula here as the "required_notional" a live IBKR BuyingPower
must cover is therefore a like-for-like comparison, not an approximation.

POLICY (fail closed, same spirit as s4_risk.py)
-------------------------------------------------
Unlike S4 (where only the LEVERED path needs margin), an S8 credit spread ALWAYS needs a
margin account — a defined-risk vertical cannot be held in a pure cash account at all. So,
unconditionally (not gated on any exposure threshold):
  * refuse if the account is not a confirmed MARGIN account;
  * refuse if BuyingPower < required_notional (insufficient capital for this position);
  * refuse if ExcessLiquidity <= 0 (no margin cushion left).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

# s4_risk.py stays in paperbot/ (this module was relocated to livebot/ in the
# S8-package split); add paperbot/ to sys.path so the bare `from s4_risk import ...`
# below still resolves. Path derived relative to this file, not the cwd.
_PAPERBOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "paperbot")
if _PAPERBOT not in sys.path:
    sys.path.insert(0, _PAPERBOT)

from s4_risk import _EPS, _num, _summary_map, account_is_margin  # noqa: E402

__all__ = [
    "PreflightResult",
    "required_notional",
    "margin_preflight",
]


@dataclass
class PreflightResult:
    ok: bool                       # True => this spread entry may proceed on this account
    account_type: str
    is_margin: bool
    buying_power: float
    excess_liquidity: float
    required_notional: float       # defined-risk max loss the spread entry needs covered
    reasons: list = field(default_factory=list)


def required_notional(width_points: float, realized_credit: float, qty: int) -> float:
    """Defined-risk max loss for one S8 credit-spread entry, in dollars.

    (width_points * 100 * qty) - (realized_credit * 100 * qty) — the exact formula
    independently verified against real IBKR BuyingPower this session (see module
    docstring). `qty` is contracts (positive integer expected; not itself validated here
    — the caller's order-building layer owns fat-finger checks on qty).
    """
    return (float(width_points) * 100.0 * qty) - (float(realized_credit) * 100.0 * qty)


def margin_preflight(summary, width_points: float, realized_credit: float, qty: int) -> PreflightResult:
    """DEPLOY-TIME gate for one S8 spread entry. Given the account's live accountSummary
    (AccountType, BuyingPower, ExcessLiquidity) and the spread's width/credit/qty, decide
    whether this entry may proceed.

    Policy (fail closed, unconditional — an option spread always needs margin):
      * account must be a confirmed MARGIN account (account_is_margin(AccountType)),
      * BuyingPower must be >= the position's required_notional (defined-risk max loss),
      * ExcessLiquidity must be > 0 (a positive margin cushion).
    Any failure -> refuse (ok=False), with a human-readable reason appended per failure.

    `summary` may be a dict {tag: value} (tests) or an ib_async accountSummary() list of
    rows (deploy) — same dual-shape support as s4_risk.py (see s4_risk._summary_map).
    NEVER connects; the caller reads the summary and passes it in.
    """
    m = _summary_map(summary)
    account_type = m.get("AccountType", "")
    buying_power = _num(m, "BuyingPower")
    excess_liq = _num(m, "ExcessLiquidity")
    net_liq = _num(m, "NetLiquidation")
    # account_is_margin gets the buying-power/net-liq signal too: this account's IBKR
    # AccountType is a legal-entity label ("TRUST"), not "MARGIN", so the string test
    # alone would wrongly refuse it -- BuyingPower > NetLiquidation proves it is margin.
    is_margin = account_is_margin(account_type, buying_power=buying_power, net_liq=net_liq)
    need = required_notional(width_points, realized_credit, qty)

    reasons: list[str] = []
    if not is_margin:
        reasons.append(
            f"S8 credit spreads require a MARGIN account, but AccountType={account_type!r} "
            f"is not margin — REFUSING")
    if buying_power < need - _EPS:
        reasons.append(
            f"insufficient BuyingPower {buying_power:,.0f} < required notional {need:,.0f} "
            f"(width {width_points:.0f}pt, credit {realized_credit:.2f}, qty {qty}) — REFUSING")
    if excess_liq <= 0:
        reasons.append(
            f"non-positive ExcessLiquidity {excess_liq:,.0f} (no margin cushion) — REFUSING")

    ok = not reasons
    return PreflightResult(
        ok=ok, account_type=account_type, is_margin=is_margin,
        buying_power=buying_power, excess_liquidity=excess_liq,
        required_notional=need, reasons=reasons)
