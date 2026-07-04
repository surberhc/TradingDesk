"""
s4_sizing.py — S4-specific order sizing that ALLOWS real margin leverage.

WHY A SIBLING (not an edit to the frozen sizing)
------------------------------------------------
The shared execution path deliberately blocks leverage in three places:
  * investable.compute_investable caps deployable capital at NAV*(1-buffer) < NAV;
  * reconcile/execution_engine size int(weight*investable/price) ONLY for weight>0, so a
    NEGATIVE weight (S4's borrow leg) is silently dropped;
  * risk_manager vetoes any book whose liquid reserve < cash_reserve_pct (i.e. any
    exposure > 1.0).
Those are correct for S0 (a long-only, un-levered ETF book) and are FROZEN. S4 is a
vol-control fund that, in calm markets, deliberately runs SPY exposure ABOVE 1.0x funded by
broker margin (Andrew's explicit decision: REAL margin borrow, not a clamp, not synthetic).
So S4 needs its OWN sizing that:
  * sizes the SPY (risk) leg to NAV * exposure — where exposure MAY exceed 1.0 (SPY notional
    can exceed NAV), and
  * carries the BIL (cash/borrow) leg THROUGH — a negative BIL weight is a real borrow, kept,
    never dropped.

This module is pure arithmetic over a Target + NAV + prices. No broker, no config-knob edits,
no order objects — it emits duck-typed intents the existing order_router.build can consume
(symbol/side/quantity/limit_price + the extra fields the S4 risk guard reads).

The exposure itself is decided by the shared brain (SpxVolControl) and comes in on the SPY
weight of the Target; this module does NOT re-derive exposure — it only turns weights into
share counts.
"""
from __future__ import annotations

from dataclasses import dataclass

from s4_strategy_target import s4_config

RISK_TICKER = s4_config.RISK_TICKER   # "SPY"
CASH_TICKER = s4_config.CASH_TICKER   # "BIL"


@dataclass
class S4Intent:
    """One S4 trade toward target. Duck-typed for order_router.build (symbol/side/quantity/
    limit_price) plus the fields the S4 risk guard inspects. `is_borrow_leg` marks the BIL
    leg when its target weight is negative (a real margin borrow, not a holding to buy)."""
    symbol: str
    side: str                 # BUY / SELL
    quantity: int             # whole shares (absolute)
    limit_price: float        # indicative sizing price (live quote or close)
    target_weight: float      # signed model weight (negative BIL = borrow)
    target_dollars: float     # signed notional (negative for the borrow leg)
    current_shares: float
    is_borrow_leg: bool = False
    legs: int = 1


def exposure_of(target) -> float:
    """The SPY exposure implied by the Target (its SPY weight). This IS the vol-control
    exposure the shared brain decided — read, never recomputed."""
    return float(target.weights.get(RISK_TICKER, 0.0))


def size_orders(nav: float, positions: dict, target, prices: dict | None = None) -> list[S4Intent]:
    """Diff the S4 target book against actual positions -> S4Intents, WITH leverage.

    Sizing rule (per leg):
      * RISK leg (SPY): target_dollars = weight * NAV  (weight may be > 1.0 -> notional
        exceeds NAV, funded by margin). target_shares = int(target_dollars / price), floored.
        The whole NAV is deployable for S4 — there is no (1-buffer) haircut, because a
        vol-control fund's "cash" is an explicit modelled leg (BIL), not an execution buffer.
      * CASH/BORROW leg (BIL): weight is 1 - exposure. If exposure < 1 it is a positive BIL
        holding; if exposure > 1 it is NEGATIVE — a real borrow. We size the SHARES for a
        positive BIL leg (a real holding), and for a negative BIL leg we DO NOT buy/sell BIL
        shares (you don't hold negative BIL); the borrow is realized as the margin used by
        the >100% SPY leg. The negative leg is still EMITTED (is_borrow_leg=True, quantity 0)
        so it is visible in the plan and the risk guard can account for it — never silently
        dropped.

    `prices` (symbol->price) overrides the Target close for sizing (e.g. live quotes)."""
    intents: list[S4Intent] = []
    symbols = list(target.weights.index)
    for sym in positions:
        if sym not in symbols:
            symbols.append(sym)

    for sym in sorted(symbols):
        weight = float(target.weights.get(sym, 0.0))
        px = float((prices or {}).get(sym, target.prices.get(sym, float("nan"))))
        has_price = px == px and px > 0
        current = float(positions.get(sym, 0.0))

        # The BORROW leg: a negative cash weight is financing, not a share position.
        if sym == CASH_TICKER and weight < 0:
            borrow_dollars = weight * nav   # negative
            intents.append(S4Intent(
                symbol=sym, side="BORROW", quantity=0, limit_price=(px if has_price else 0.0),
                target_weight=weight, target_dollars=borrow_dollars, current_shares=current,
                is_borrow_leg=True))
            # If we currently HOLD BIL shares while the model says borrow, close them.
            if current > 0:
                intents.append(S4Intent(
                    symbol=sym, side="SELL", quantity=int(current),
                    limit_price=(round(px, 2) if has_price else 0.0),
                    target_weight=weight, target_dollars=borrow_dollars,
                    current_shares=current, is_borrow_leg=False))
            continue

        # Normal holding leg (SPY at any exposure, or a positive BIL cash leg).
        if weight > 0 and has_price:
            target_dollars = weight * nav          # NO (1-buffer) haircut for S4
            target_shares = int(target_dollars / px)
        else:
            target_dollars = 0.0
            target_shares = 0

        delta = target_shares - current
        if abs(delta) < 1:
            continue
        side = "BUY" if delta > 0 else "SELL"
        intents.append(S4Intent(
            symbol=sym, side=side, quantity=int(abs(delta)),
            limit_price=(round(px, 2) if has_price else 0.0),
            target_weight=weight, target_dollars=target_dollars,
            current_shares=current))
    return intents
