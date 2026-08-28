"""
risk_manager.py — pre-trade guards. VETOES before any transmission.

Pure and side-effect-free: it reads account state + the intended orders and returns
verdicts. It sends nothing and persists nothing.

NO AUTOMATED DAILY-LOSS HALT (owner decision, Andrew, 2026-08-25). The persisted -2%
"kill switch" that used to live here was REMOVED: it was never authorized — it entered in
the pre-git baseline snapshot and no decision record for it ever existed. Nothing in this
module halts on the day's P&L any more, and it must not be re-added. (The MANUAL,
file-based operator stop — the AUTOTRADE_DISABLED sentinel / KILL_SWITCH label used by
safe_execute and the live-deploy rails — is a separate, deliberate control and is
untouched by this.)

NO PER-POSITION CAP (owner decision, Andrew, 2026-08-25). The 35%-of-NAV ceiling on a
single risk position (config.RISK_LIMITS["max_position_pct_nav"]) that used to live here
was REMOVED: like the daily-loss breaker it was never authorized — it entered in the
pre-git baseline snapshot with no decision record — and its own comment admitted that its
earlier 5% value would have VETOED the strategy itself, so it had already been retuned
once to fit the book it was supposed to police. The strategy's own per-asset caps
(SPEC §12) are the real per-position constraint. It must not be re-added.

Guards (all from config.RISK_LIMITS):
  * CASH RESERVE / no leverage: the resulting book must leave >= cash_reserve_pct of
    NAV liquid (uninvested cash + cash-equivalent holdings). Backstop — the engine
    already sizes against NAV*(1-reserve), so this should normally pass. Cash-equivalents
    (T-bills / floating-rate / short Treasuries) count toward the liquid reserve — that is
    what CASH_EQUIVALENTS below is for, and it is the ONLY thing it is for now.
  * MAX LEGS per order: ETF orders are 1 leg; >max_legs_per_order is malformed for S0.
  * ORDER SANITY: positive quantity, and single-order notional not exceeding NAV.

Reconciliation of INTENDED vs ACTUAL broker positions is a separate, post-fill concern
(the fills/reconciliation build step), not a pre-trade veto here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import config
import investable as _investable
from strategies import config as strat_config

# Cash-equivalents COUNT TOWARD the liquid reserve in the cash-reserve / no-leverage batch
# guard below (holding them de-risks). That batch guard is now their only consumer — the
# per-position cap they used to be exempt from was removed 2026-08-25 by owner decision.
CASH_EQUIVALENTS = set(
    strat_config.TBILLS + strat_config.FLOATING_RATE + strat_config.SHORT_TREASURIES
)


# --- Verdicts -------------------------------------------------------------------
@dataclass
class OrderVerdict:
    symbol: str
    ok: bool
    reasons: list = field(default_factory=list)   # veto reasons (empty if ok)


@dataclass
class RiskReport:
    # halted / halt_reason are KEPT because consumers (the executors, the ledger record,
    # the dashboard readouts) read them. From THIS module they are now ALWAYS False / ""
    # — the automated daily-loss halt was removed 2026-08-25 by owner decision and no
    # guard here sets them any more. A caller may still construct a RiskReport of its own
    # with halted=True; nothing in evaluate() will.
    halted: bool
    halt_reason: str
    order_verdicts: list         # list[OrderVerdict], 1:1 with the input orders
    batch_reasons: list          # batch-level vetoes (e.g. cash reserve)
    approved: list               # orders that passed everything (empty if halted/batch veto)

    @property
    def all_clear(self) -> bool:
        return (not self.halted and not self.batch_reasons
                and all(v.ok for v in self.order_verdicts))


def evaluate(nav, daily_pnl, positions, orders, target, limits=None,
             cash_reserve_pct=None) -> RiskReport:
    """Run every guard over the intended orders and return a RiskReport.

    positions : {symbol: shares} currently held.
    daily_pnl : ACCEPTED FOR CALL-COMPATIBILITY ONLY and otherwise UNUSED here. Callers and
                tests pass it positionally, and the executors still read the day's figure so
                it lands in the audit ledger — but as of 2026-08-25 it GATES NOTHING. The
                automated daily-loss halt was removed by owner decision (see the module
                docstring); no value of this parameter, however catastrophic, can halt a run.
    orders    : list of execution_engine.IntendedOrder (duck-typed: symbol, side,
                quantity, limit_price).
    target    : strategy_target.Target (for prices + the per-position weights).
    cash_reserve_pct : THIS model's standing reserve, the floor the liquid-reserve /
                no-leverage batch guard enforces. None -> the existing resolution order
                (an explicit `limits["cash_reserve_pct"]`, else the global buffer), so
                every pre-existing caller is byte-identical. A book SIZED against a 1%
                reserve must be CHECKED against 1%: check it against 1.5% and the guard
                vetoes a correctly-sized custom account for being 0.5% over-invested.
    """
    limits = limits or config.RISK_LIMITS
    # NO AUTOMATED DAILY-LOSS HALT (owner decision, Andrew, 2026-08-25). This used to be
    # `check_kill_switch(nav, daily_pnl, limits)`, a persisted -2%-of-NAV breaker that
    # halted the whole book until a human deleted a file. It was never authorized and has
    # been removed; `daily_pnl` is no longer consulted by any guard. The fields stay on
    # RiskReport for its consumers and are now constant.
    halted, halt_reason = False, ""

    # (The unconditional "resulting share book" that used to be built here existed ONLY to
    # feed the per-position cap, which was removed 2026-08-25 by owner decision. The batch
    # guard builds its own `resulting_ok` below — deliberately from the orders that PASSED
    # their own guards — and that is the only resulting book anything reads now.)

    # --- PRICE RESOLUTION: A GUARD THAT CANNOT SEE MUST REFUSE (v0.42.0) -------------
    # BEFORE: `price_of` turned a missing price into 0.0 and returned a NaN price as NaN.
    # Both are permissive to the point of being switches that turn the guards OFF:
    #   * 0.0  -> the position contributes NOTHING to risk_value, so a plainly levered book
    #            reads as having a full liquid reserve and the no-leverage veto never fires.
    #   * NaN  -> every comparison against NaN is False, so NO threshold can trip. One
    #            NaN-priced leg silently disabled the per-position cap, the notional cap AND
    #            the reserve guard for the whole account (proven: an oversized order that is
    #            vetoed twice with good prices was APPROVED with one NaN price).
    # NOW: an unresolvable price yields None and the guard REFUSES, naming the symbol.
    # `resolve` also consults the ORDERS' own limit prices, so a symbol we are actively
    # trading at a known limit is priceable and does not needlessly bench the account.
    order_limits: dict = {}
    for o in orders:
        px = _investable.usable_price(getattr(o, "limit_price", None))
        if px is not None:
            order_limits.setdefault(o.symbol, px)

    def resolve(sym, fallback=None):
        """The usable price for `sym`, or None. NEVER 0.0, NEVER NaN (see above)."""
        stored = target.prices.get(sym, None) if hasattr(target, "prices") else None
        return (_investable.usable_price(stored)
                or order_limits.get(sym)
                or _investable.usable_price(fallback))

    # Per-order guards.
    order_verdicts: list[OrderVerdict] = []
    for o in orders:
        reasons: list[str] = []
        if o.quantity <= 0:
            reasons.append("non-positive quantity")
        legs = getattr(o, "legs", 1)
        if legs > limits["max_legs_per_order"]:
            reasons.append(f"{legs} legs > max {limits['max_legs_per_order']}")
        order_px = resolve(o.symbol, getattr(o, "limit_price", None))
        if order_px is None:
            # FAIL CLOSED at the ORDER level: isolated to this leg, so a single unpriceable
            # symbol does not veto its priceable siblings.
            reasons.append(
                f"no usable price for {o.symbol} (no live quote, no limit price) — the "
                f"order-notional guard cannot be evaluated, so this order is REFUSED "
                f"rather than passed unchecked")
        else:
            notional = o.quantity * order_px
            if nav and notional > nav + 1e-6:
                reasons.append(f"order notional {notional:,.0f} exceeds NAV {nav:,.0f}")
            # NO per-position cap here (removed 2026-08-25, owner decision — see the
            # module docstring). Do not re-add one.
        order_verdicts.append(OrderVerdict(symbol=o.symbol, ok=not reasons, reasons=reasons))

    # The book that would ACTUALLY result: currently-held positions plus only the orders
    # that passed their own guards. An order already vetoed above can never fill, so
    # refusing the whole batch because THAT symbol has no price would be benching the
    # account over a trade that is not going to happen (do not bench unnecessarily).
    resulting_ok = dict(positions)
    for o, v in zip(orders, order_verdicts):
        if v.ok:
            resulting_ok[o.symbol] = resulting_ok.get(o.symbol, 0.0) + (
                o.quantity if o.side == "BUY" else -o.quantity)

    # Positions the batch guard must value but cannot. Zero-quantity entries are excluded:
    # they contribute nothing to any total whatever their price.
    unpriceable_positions = sorted(
        sym for sym, sh in resulting_ok.items()
        if float(sh or 0.0) != 0.0 and resolve(sym) is None)

    # Batch guard: cash reserve / no leverage. Liquid reserve = uninvested cash +
    # cash-equivalent positions must be >= reserve.
    batch_reasons: list[str] = []
    # FAIL CLOSED, BATCH LEVEL: this guard sums EVERY resulting position, so one holding we
    # cannot value makes the whole leverage answer a guess. Unlike the per-order guards it
    # genuinely cannot be isolated — an unpriced holding could be worth anything, and the
    # pre-0.42.0 behavior (treat it as $0) is precisely the assumption that reads a levered
    # book as fully reserved. Most such names are legacy/off-model holdings, so the reason
    # names them and tells a human exactly what to supply.
    if unpriceable_positions and nav and nav > 0:
        batch_reasons.append(
            f"no usable price for held position(s) {', '.join(unpriceable_positions)} — the "
            f"liquid-reserve / no-leverage guard sums every holding, so it cannot be "
            f"evaluated at all. REFUSING the batch rather than reporting it clear")
    elif nav and nav > 0:
        def price_of(sym, fallback=0.0):
            # Only reached when every non-zero holding resolved above, so this is never a
            # silent zero for a real position.
            return resolve(sym, fallback) or 0.0

        risk_value = sum(sh * price_of(sym, 0.0)
                         for sym, sh in resulting_ok.items() if sym not in CASH_EQUIVALENTS)
        cash_equiv_value = sum(sh * price_of(sym, 0.0)
                               for sym, sh in resulting_ok.items() if sym in CASH_EQUIVALENTS)
        uninvested = nav - (risk_value + cash_equiv_value)
        liquid_reserve_pct = (uninvested + cash_equiv_value) / nav
        # Threshold source is the shared buffer accessor; an explicit caller-supplied
        # `limits` override still wins (preserves the existing override seam). When
        # `limits` is the default config, limits["cash_reserve_pct"] == buffer_pct() — so
        # this is behavior-identical to the previous limits["cash_reserve_pct"].
        # An explicit per-model reserve wins over both (it IS what the book was sized to).
        reserve = (float(cash_reserve_pct) if cash_reserve_pct is not None
                   else limits.get("cash_reserve_pct", _investable.buffer_pct()))
        if liquid_reserve_pct < reserve - 1e-9:
            batch_reasons.append(
                # .2f, not .0f: the reserves in play are now 1.00% and 1.50%, and .0f
                # rendered 1.5% as "2%" — a veto message that misstates its own threshold.
                f"liquid reserve {liquid_reserve_pct * 100:.2f}% < required {reserve * 100:.2f}% "
                f"(book over-invested / leveraged)")

    approved = ([] if (halted or batch_reasons)
                else [o for o, v in zip(orders, order_verdicts) if v.ok])
    return RiskReport(halted=halted, halt_reason=halt_reason,
                      order_verdicts=order_verdicts, batch_reasons=batch_reasons,
                      approved=approved)
