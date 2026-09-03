"""group_rebalance.py — PURE planner: per-account plans -> one FA block group PER TICKER.

WHAT THIS IS
------------
The planning half of the group rail. It takes the AccountPlans the frozen engine already
produces (rebalance_engine.plan_account, exactly as batch_rebalance_execute builds them at
its step [6]) and turns them into a reviewable list of TICKER GROUPS: for each symbol and
side, ONE group holding every account that needs to trade it, each with its own share count.

It builds no order, opens no connection, writes no XML and touches no gateway. Everything
here is a pure function of its inputs so the whole plan can be reviewed, diffed and tested
offline before anything is armed.

WHY PER TICKER, NOT PER MODEL (owner decision, Andrew Surber, 2026-09-03)
------------------------------------------------------------------------
The model decides HOW MANY SHARES each account needs. It has no business reaching the order
layer. Two facts settle the shape:

  1. An IBKR API order is always for ONE contract.
  2. IBKR exposes NO verb that rebalances a group to target percentages. Verified twice:
     our own docs/MODEL_PORTFOLIO_RESEARCH.md ("the API can route to and read models, but
     cannot create or rebalance them ... rebalance is UI-only"), and IBKR's TWS API FA page
     (allocation methods are EqualQuantity / NetLiq / AvailableEquity / PctChange /
     ContractsOrShares - there is no rebalance call). The one-click rebalance exists only in
     the TWS/Advisor Portal GUI, which a human drives and nothing can schedule or log.

So a rebalance IS N block orders whichever way it is sliced. Slicing per MODEL would mean six
separate XLE orders at six different average prices for the same ETF on the same day. Slicing
per TICKER means ONE XLE order, and every account in the run - across every model in it -
fills at the SAME average price. On the 2026-09-03 book that is ~17 orders instead of ~96.

THE RUN SCOPE IS THE MODEL SELECTION, NOT THE GROUP SHAPE
---------------------------------------------------------
Which accounts are in a run is decided upstream by the model scope the operator picks
(roster.enrolled_roster_scan(models=...), the existing --models token). This module never
selects accounts; it only pivots whatever plans it is handed. Pick one model and the groups
hold that model's accounts; tick two and the same XLE group simply holds both models'
accounts and everybody still fills at one price. Widening the scope widens the groups - it
never multiplies the orders.

That is what makes a staged rollout possible: run Conservative (Custom) with its 1 account
first, then Balanced (Small, Custom) with 2, and leave Growth (Custom) with its 107 accounts
and $27.8M until the rail has worked several times.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import fa_group_sync
import rebalance_engine


@dataclass(frozen=True)
class TickerGroupPlan:
    """ONE ticker, ONE side, ONE FA group, and the exact per-account share split.

    `group_name` is the throwaway run group this block will be traded through - created
    fresh, traded, and left behind as the audit record of exactly who was in this trade.
    `per_account` is fixed here at build time and is never reallocated after fills.
    """
    symbol: str
    side: str                       # BUY | SELL
    total_qty: int
    group_name: str
    per_account: dict = field(default_factory=dict)   # account -> shares
    est_notional: float | None = None                 # None when no price was supplied

    @property
    def accounts(self) -> tuple:
        return tuple(sorted(self.per_account))

    @property
    def n_accounts(self) -> int:
        return len(self.per_account)


def plan_ticker_groups(plans, *, run_stamp: str, prices=None) -> list[TickerGroupPlan]:
    """PURE: pivot per-account AccountPlans into one TickerGroupPlan per (symbol, side).

    `run_stamp` identifies the run and is what makes each group an audit record of ONE run
    (fa_group_sync.run_group_name). Two runs on the same day MUST pass different stamps or the
    second one collides with the first group and creation refuses - which is the correct
    outcome, not something to work around.

    `prices` (symbol -> price) is optional and only decorates the plan with an estimated
    notional for review. It never affects the split.

    RAISES ValueError if any invariant fails: a split that does not sum to its total, an empty
    group, or two groups landing on the same name. Every one of those would be a silent
    mis-trade, so this refuses to hand back a plan rather than let a caller act on it.
    """
    stamp = str(run_stamp or "").strip()
    if not stamp:
        raise ValueError(
            "plan_ticker_groups: no run stamp given. A run group MUST be identifiable to its "
            "run or it is not an audit record. FAILING LOUD.")

    blocks = rebalance_engine.aggregate_blocks_by_ticker(plans)
    px = dict(prices or {})
    out: list[TickerGroupPlan] = []
    seen_names: dict[str, str] = {}

    for b in blocks:
        split = {a: int(q) for a, q in b.per_account.items() if int(q)}
        if not split:
            continue
        total = sum(split.values())
        if total != int(b.total_qty):
            raise ValueError(
                f"plan_ticker_groups: {b.side} {b.symbol} split sums to {total} but the block "
                f"total is {b.total_qty}. Refusing to hand back a split that does not add up.")
        name = fa_group_sync.run_group_name(f"{b.symbol} {b.side}", stamp)
        if name in seen_names:
            raise ValueError(
                f"plan_ticker_groups: two blocks resolved to the same group name {name!r} "
                f"({seen_names[name]} and {b.symbol} {b.side}). A group name must identify "
                f"exactly one block. FAILING LOUD.")
        seen_names[name] = f"{b.symbol} {b.side}"
        p = px.get(b.symbol)
        out.append(TickerGroupPlan(
            symbol=b.symbol, side=b.side, total_qty=int(b.total_qty), group_name=name,
            per_account=split,
            est_notional=(float(p) * total) if p else None))
    return out


def summarize(group_plans) -> str:
    """A plain-English, reviewable summary of a whole run. Pure; returns a string."""
    if not group_plans:
        return "No ticker groups: nothing in this scope needs to trade."
    buys = [g for g in group_plans if g.side == "BUY"]
    sells = [g for g in group_plans if g.side == "SELL"]
    accounts = sorted({a for g in group_plans for a in g.per_account})
    known = [g.est_notional for g in group_plans if g.est_notional is not None]
    lines = [
        f"{len(group_plans)} block order(s) across {len(accounts)} account(s): "
        f"{len(buys)} buy, {len(sells)} sell.",
        "Every account in a group fills that ticker at ONE average price.",
    ]
    if known:
        buy_n = sum(g.est_notional or 0.0 for g in buys)
        sell_n = sum(g.est_notional or 0.0 for g in sells)
        lines.append(f"Estimated notional: {buy_n:,.0f} of buys, {sell_n:,.0f} of sells.")
    for g in group_plans:
        n = "" if g.est_notional is None else f"  ~{g.est_notional:,.0f}"
        lines.append(f"  {g.side:<4} {g.symbol:<6} {g.total_qty:>8,} sh across "
                     f"{g.n_accounts:>3} account(s){n}   group: {g.group_name}")
    return "\n".join(lines)
