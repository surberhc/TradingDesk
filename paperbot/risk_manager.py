"""
risk_manager.py — pre-trade guards + kill switch. VETOES before any transmission.

Pure and side-effect-light: it reads account state + the intended orders and returns
verdicts. It sends nothing and changes nothing except the kill-switch trip flag, which
it persists to local state so a tripped halt survives a restart (you must clear it by
hand to resume — a safety property, not a bug).

Guards (all from config.RISK_LIMITS):
  * KILL SWITCH — daily loss: if the account is down >= max_daily_loss_pct_nav on the
    day, trip and HALT (no order may go). A previously-tripped flag also halts.
  * CASH RESERVE / no leverage: the resulting book must leave >= cash_reserve_pct of
    NAV liquid (uninvested cash + cash-equivalent holdings). Backstop — the engine
    already sizes against NAV*(1-reserve), so this should normally pass.
  * PER-POSITION CAP: no single RISK position over max_position_pct_nav of NAV. Cash-
    equivalents (T-bills / floating-rate / short Treasuries) are EXEMPT — concentrating
    in cash-equivalents is de-risking. The strategy's own SPEC §12 caps are the primary
    per-asset control; this is a fat-finger backstop above them.
  * MAX LEGS per order: ETF orders are 1 leg; >max_legs_per_order is malformed for S0.
  * ORDER SANITY: positive quantity, and single-order notional not exceeding NAV.

Reconciliation of INTENDED vs ACTUAL broker positions is a separate, post-fill concern
(the fills/reconciliation build step), not a pre-trade veto here.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import config
import investable as _investable
from strategies import config as strat_config

# Cash-equivalents are EXEMPT from the per-position risk cap (holding them de-risks).
CASH_EQUIVALENTS = set(
    strat_config.TBILLS + strat_config.FLOATING_RATE + strat_config.SHORT_TREASURIES
)

_KILL_FILE = os.path.join(config.STATE_DIR, "killswitch.json")


# --- Kill switch (persisted) ----------------------------------------------------
def killswitch_state() -> dict:
    """Current kill-switch record ({} if never tripped)."""
    if os.path.exists(_KILL_FILE):
        try:
            with open(_KILL_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}


def trip_killswitch(reason: str) -> None:
    """Persist a tripped kill switch. Stays tripped until cleared by hand."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(_KILL_FILE, "w", encoding="utf-8") as fh:
        json.dump({"tripped": True, "reason": reason}, fh)


def clear_killswitch() -> bool:
    """Manually clear a tripped kill switch. Returns True if one was cleared."""
    if os.path.exists(_KILL_FILE):
        os.remove(_KILL_FILE)
        return True
    return False


def check_kill_switch(nav: float, daily_pnl: float | None, limits: dict) -> tuple[bool, str]:
    """(halted, reason). Trips + persists if the daily loss breaches the limit."""
    state = killswitch_state()
    if state.get("tripped"):
        return True, (f"kill switch ALREADY tripped: {state.get('reason', '(no reason)')} "
                      f"- clear it by hand (risk_manager.clear_killswitch) to resume")
    if nav and nav > 0 and daily_pnl is not None:
        loss_pct = daily_pnl / nav
        limit = abs(limits["max_daily_loss_pct_nav"])
        if loss_pct <= -limit:
            reason = (f"daily loss {loss_pct * 100:.2f}% breached -{limit * 100:.2f}% "
                      f"(dailyPnL={daily_pnl:,.2f} on NAV {nav:,.2f})")
            trip_killswitch(reason)
            return True, reason
    return False, ""


# --- Verdicts -------------------------------------------------------------------
@dataclass
class OrderVerdict:
    symbol: str
    ok: bool
    reasons: list = field(default_factory=list)   # veto reasons (empty if ok)


@dataclass
class RiskReport:
    halted: bool                 # kill switch -> nothing may transmit
    halt_reason: str
    order_verdicts: list         # list[OrderVerdict], 1:1 with the input orders
    batch_reasons: list          # batch-level vetoes (e.g. cash reserve)
    approved: list               # orders that passed everything (empty if halted/batch veto)

    @property
    def all_clear(self) -> bool:
        return (not self.halted and not self.batch_reasons
                and all(v.ok for v in self.order_verdicts))


def evaluate(nav, daily_pnl, positions, orders, target, limits=None) -> RiskReport:
    """Run every guard over the intended orders and return a RiskReport.

    positions : {symbol: shares} currently held.
    orders    : list of execution_engine.IntendedOrder (duck-typed: symbol, side,
                quantity, limit_price).
    target    : strategy_target.Target (for prices + the per-position weights).
    """
    limits = limits or config.RISK_LIMITS
    halted, halt_reason = check_kill_switch(nav, daily_pnl, limits)

    # Resulting share book after applying the orders (for position-level checks).
    resulting = dict(positions)
    for o in orders:
        resulting[o.symbol] = resulting.get(o.symbol, 0.0) + (
            o.quantity if o.side == "BUY" else -o.quantity)

    def price_of(sym, fallback=0.0):
        px = float(target.prices.get(sym, 0.0)) if hasattr(target, "prices") else 0.0
        return px or fallback

    # Per-order guards.
    order_verdicts: list[OrderVerdict] = []
    for o in orders:
        reasons: list[str] = []
        if o.quantity <= 0:
            reasons.append("non-positive quantity")
        legs = getattr(o, "legs", 1)
        if legs > limits["max_legs_per_order"]:
            reasons.append(f"{legs} legs > max {limits['max_legs_per_order']}")
        notional = o.quantity * (o.limit_price or price_of(o.symbol))
        if nav and notional > nav + 1e-6:
            reasons.append(f"order notional {notional:,.0f} exceeds NAV {nav:,.0f}")
        # per-position cap on risk assets (cash-equivalents exempt)
        if o.symbol not in CASH_EQUIVALENTS and nav:
            res_w = resulting.get(o.symbol, 0.0) * price_of(o.symbol, o.limit_price) / nav
            cap = limits["max_position_pct_nav"]
            if res_w > cap + 1e-9:
                reasons.append(f"resulting {res_w * 100:.1f}% > per-position cap {cap * 100:.0f}%")
        order_verdicts.append(OrderVerdict(symbol=o.symbol, ok=not reasons, reasons=reasons))

    # Batch guard: cash reserve / no leverage. Liquid reserve = uninvested cash +
    # cash-equivalent positions must be >= reserve.
    batch_reasons: list[str] = []
    if nav and nav > 0:
        risk_value = sum(sh * price_of(sym, 0.0)
                         for sym, sh in resulting.items() if sym not in CASH_EQUIVALENTS)
        cash_equiv_value = sum(sh * price_of(sym, 0.0)
                               for sym, sh in resulting.items() if sym in CASH_EQUIVALENTS)
        uninvested = nav - (risk_value + cash_equiv_value)
        liquid_reserve_pct = (uninvested + cash_equiv_value) / nav
        # Threshold source is the shared buffer accessor; an explicit caller-supplied
        # `limits` override still wins (preserves the existing override seam). When
        # `limits` is the default config, limits["cash_reserve_pct"] == buffer_pct() — so
        # this is behavior-identical to the previous limits["cash_reserve_pct"].
        reserve = limits.get("cash_reserve_pct", _investable.buffer_pct())
        if liquid_reserve_pct < reserve - 1e-9:
            batch_reasons.append(
                f"liquid reserve {liquid_reserve_pct * 100:.1f}% < required {reserve * 100:.0f}% "
                f"(book over-invested / leveraged)")

    approved = ([] if (halted or batch_reasons)
                else [o for o, v in zip(orders, order_verdicts) if v.ok])
    return RiskReport(halted=halted, halt_reason=halt_reason,
                      order_verdicts=order_verdicts, batch_reasons=batch_reasons,
                      approved=approved)
