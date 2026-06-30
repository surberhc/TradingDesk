"""
rebalance_engine.py — the PURE, offline multi-account block-rebalance brain.

This is the computation core of Option B (multi-account / FA). Given, per account, its
NetLiq, its current positions, its tier's target weights (from the shared `strategies`
brain, surfaced via strategy_target.Target), current prices, its distribution reserve
(from cashflows), and the no-trade band, it answers two questions with NO broker contact:

  1) Per account: the EXPLICIT integer target SHARE count per ticker, and the signed
     share DELTA vs current holdings — with an ACCOUNT-LEVEL no-trade band: the account
     is left untouched unless SOME holding breaches +/-band, in which case the whole
     account is rebalanced back to model (so ordinary market noise never trips a trade).

  2) Across a tier's accounts: same-symbol, same-direction deltas aggregated into a
     single BLOCK quantity, plus the per-account ContractsOrShares split that sums back
     to that block quantity. A block that touches only one account falls back to a
     per-account DIRECT order.

WHY explicit per-account shares (not an order-level faMethod): proven on the paper
gateway (memory: fa-block-order-allocation) — in this post-build-983 unified FA config,
allocation is governed by the GROUP's stored ContractsOrShares config; an order-level
faMethod="NetLiq" is REJECTED with Error 10226. So the engine must compute each
account's exact shares itself and the group is set to those amounts. to_fa_block_inputs()
therefore emits fa_method="" (empty) on purpose — never "NetLiq".

HARD BOUNDARY: this module is pure computation. It connects to nothing, qualifies
nothing, builds no ib_async order object, and transmits nothing. It STOPS at producing
the inputs that order_router.build_fa_block (group orders) / order_router.build (direct
orders) expect, packaged as a reviewable what-if plan. Arming/transmission stays behind
the independent READONLY + DRY_RUN + armed gates, untouched here.

Reuses, rather than duplicates: reconcile.reconcile (share math + drift/status),
cashflows.reserve_for (distribution carve-out), and the AccountPlan / BlockOrder
dataclasses defined in recon_report.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cashflows
import config
import investable as _investable
import reconcile
import strategy_target
from recon_report import AccountPlan, BlockOrder


# --- 1. reserve carve-out ------------------------------------------------------
# The reserve carve-out math now lives in the shared leaf module `investable` (Slice 1
# of the account-cashflow consolidation). This thin wrapper is kept so existing imports
# and tests (rebalance_engine.compute_investable) keep working unchanged — it is a pure
# re-export, behavior-identical to the previous inline body.
def compute_investable(net_liq: float, reserve: float,
                       cash_reserve_pct: float | None = None) -> float:
    """Capital the engine may deploy for one account. See investable.compute_investable
    for the canonical logic; this is a thin pass-through re-export."""
    return _investable.compute_investable(net_liq, reserve, cash_reserve_pct)


# --- 2. per-account target shares, deltas, band suppression --------------------
def plan_account(account: str, version: str, net_liq: float, positions: dict,
                 target: strategy_target.Target,
                 prices: dict | None = None,
                 band_pct: float | None = None) -> AccountPlan:
    """Reconcile ONE account against its tier model and emit the share deltas to fix it.

    Steps (all pure):
      * reserve  = cashflows.reserve_for(account, net_liq)        (distribution carve-out)
      * investable = compute_investable(net_liq, reserve, cash_reserve_pct)
      * lines    = reconcile.reconcile(...) — gives, per symbol, the integer target_shares
                   (= int(weight * investable / price)) and the drift weight vs the model.
      * NO-TRADE BAND (account-level, all-or-nothing): the account is left exactly as-is
        unless some holding needs a trade larger than band_pct of NetLiq (or a stray
        UNTRACKED position is held). When that happens the WHOLE account is rebalanced back
        to model — every holding with a >=1 share delta moves, in-band ones included. The
        band is measured on the required trade size, not raw weight-vs-model drift (so the
        cash-reserve gap can't trip it); applied identically to every account.

    `prices` (symbol->price) overrides the strategy-data close for sizing/valuation
    (e.g. live quotes at order time). `orders` maps symbol -> signed share delta
    (target - actual); positive = BUY, negative = SELL. Empty if the whole account is
    in-band."""
    if band_pct is None:
        band_pct = config.REBALANCE_BAND_PCT

    reserve = cashflows.reserve_for(account, net_liq)
    investable = compute_investable(net_liq, reserve)

    # tolerance_w=band_pct so reconcile classifies a holding MATCHED iff it's inside the
    # band; DRIFTED/MISSING/UNTRACKED mean it breached and is eligible for a delta.
    lines = reconcile.reconcile(target, net_liq, positions, prices=prices,
                               tolerance_w=band_pct, investable=investable)

    # NO-TRADE BAND — ACCOUNT-LEVEL, all-or-nothing (Andrew's decision 2026-06-27): leave
    # the whole account alone unless it genuinely needs work; if it does, rebalance EVERY
    # holding back to model in one pass (in-band siblings included). One band rule for all
    # accounts (compliance: no per-client discretion).
    #
    # The breach test is on the SIZE OF THE TRADE the rebalance would make
    # (|target_shares - actual_shares| valued vs NetLiq), NOT raw weight-vs-model drift.
    # Why: the cash-reserve means a fully-invested account sits ~reserve% under its raw
    # model weight by construction, so keying off raw drift (as reconcile's status does)
    # would falsely flag a correctly-invested account and, for any holding >~60% weight,
    # defeat the band entirely. A stray UNTRACKED position always breaches (it must be
    # cleared regardless of size).
    def _trade_weight(ln) -> float:
        price = float((prices or {}).get(ln.symbol,
                                         target.prices.get(ln.symbol, float("nan"))))
        if not (price == price and price > 0) or not net_liq:
            return 0.0
        return abs(ln.target_shares - int(ln.actual_shares)) * price / net_liq

    breached = (any(ln.status == "UNTRACKED" for ln in lines)
                or any(_trade_weight(ln) > band_pct for ln in lines))

    orders: dict = {}
    if breached:
        for ln in lines:
            delta = ln.target_shares - int(ln.actual_shares)
            if abs(delta) >= 1:
                orders[ln.symbol] = delta

    return AccountPlan(account, version, net_liq, reserve, investable,
                       lines, breached, orders)


def plan_accounts(account_inputs: list[dict],
                  targets: dict,
                  band_pct: float | None = None) -> list[AccountPlan]:
    """Plan many accounts at once. `account_inputs` is a list of dicts, each:
        {account, version, net_liq, positions, prices(optional)}
    `targets` maps version -> strategy_target.Target (one model per risk tier, run once).

    This is the seam the live recon_report fills from accounts.discover()+ib.positions();
    keeping it dict-driven means the engine is testable with synthetic data and never
    has to touch a broker itself."""
    plans: list[AccountPlan] = []
    for a in sorted(account_inputs, key=lambda x: x["account"]):
        plans.append(plan_account(
            a["account"], a["version"], a["net_liq"], a["positions"],
            targets[a["version"]], prices=a.get("prices"), band_pct=band_pct))
    return plans


# --- 3. block aggregation + per-account split ----------------------------------
def aggregate_blocks(plans: list[AccountPlan]) -> list[BlockOrder]:
    """Aggregate same-tier, same-symbol, same-DIRECTION deltas into block orders.

    A block executes ONE side at one average price, so BUYs and SELLs of the same symbol
    in the same tier stay separate blocks. per_account records the EXACT share split,
    fixed here at build time and never reallocated after fills (allocation-fairness /
    recordkeeping). By construction sum(per_account.values()) == total_qty."""
    blocks: dict[tuple, BlockOrder] = {}
    for p in plans:
        for sym, delta in p.orders.items():
            side = "BUY" if delta > 0 else "SELL"
            key = (p.version, sym, side)
            blk = blocks.get(key)
            if blk is None:
                blk = blocks[key] = BlockOrder(p.version, sym, side, 0)
            qty = abs(delta)
            blk.total_qty += qty
            blk.per_account[p.account] = blk.per_account.get(p.account, 0) + qty
    return sorted(blocks.values(), key=lambda b: (b.version, b.symbol, b.side))


# --- 4. order-router inputs (group block vs single-account direct) -------------
@dataclass
class RoutePlan:
    """A reviewable, transmit-free routing plan for ONE block. It carries exactly the
    inputs order_router would consume — but builds no order object and sends nothing.

      route == "fa_block"  -> order_router.build_fa_block(symbol, side, total_qty,
                                  limit_price, fa_group, fa_method, as_of, ib)
                              with fa_method="" (group's ContractsOrShares governs; an
                              order-level faMethod="NetLiq" is rejected — Error 10226).
                              per_account_split is the explicit ContractsOrShares the
                              tier GROUP must be set to before this block is placed.
      route == "direct"    -> a single-account true-up; order_router.build(...) for that
                              one account (no group; account field set on the order)."""
    route: str                         # "fa_block" | "direct"
    version: str                       # risk tier
    symbol: str
    side: str                          # BUY | SELL
    total_qty: int
    fa_group: str | None               # tier group name for a block; None for direct
    fa_method: str                     # "" for a block (ContractsOrShares group); "" for direct
    account: str | None                # the single account for a direct order; None for a block
    per_account_split: dict = field(default_factory=dict)  # account -> shares (sums to total_qty)
    reason: str = "REBALANCE_TO_MODEL"


# Per-tier FA groups already defined on the paper gateway (memory: verified state). The
# engine names the group; it never creates or edits one here (that is a serialized
# replaceFA admin step the human conductor runs).
TIER_GROUPS = {
    "Conservative": "tier_conservative",
    "Balanced": "tier_balanced",
    "Growth": "tier_growth",
}


def route_blocks(blocks: list[BlockOrder],
                 tier_groups: dict | None = None) -> list[RoutePlan]:
    """Decide, per block, whether it is a true multi-account BLOCK (FA group order) or a
    single-account true-up that falls back to a DIRECT order.

      * >= 2 accounts in the split  -> fa_block (faGroup set, faMethod="" so the group's
        explicit ContractsOrShares allocation governs).
      * exactly 1 account           -> direct (the group machinery buys nothing for a lone
        account; a plain single-account order is simpler and equally auditable).

    fa_group/per_account_split are returned for the conductor to (a) set the group's
    ContractsOrShares to per_account_split, then (b) place the block — both serialized,
    live steps OUTSIDE this module."""
    if tier_groups is None:
        tier_groups = TIER_GROUPS
    routes: list[RoutePlan] = []
    for b in blocks:
        accts = b.per_account
        if len(accts) >= 2:
            routes.append(RoutePlan(
                route="fa_block", version=b.version, symbol=b.symbol, side=b.side,
                total_qty=b.total_qty, fa_group=tier_groups.get(b.version),
                fa_method="", account=None, per_account_split=dict(accts),
                reason=b.reason))
        else:
            # exactly one account -> direct true-up
            (only_acct, qty), = accts.items()
            routes.append(RoutePlan(
                route="direct", version=b.version, symbol=b.symbol, side=b.side,
                total_qty=qty, fa_group=None, fa_method="", account=only_acct,
                per_account_split={only_acct: qty}, reason=b.reason))
    return routes


def build_plan(account_inputs: list[dict], targets: dict,
               band_pct: float | None = None,
               tier_groups: dict | None = None) -> dict:
    """End-to-end PURE pipeline: per-account plans -> blocks -> route plans.

    Returns {"plans", "blocks", "routes"} — a complete, reviewable what-if of every
    order the engine WOULD route, with nothing built and nothing transmitted. The live
    caller (recon_report / execution_engine) attaches a limit price and hands each
    RoutePlan to order_router, still behind the arming gate."""
    plans = plan_accounts(account_inputs, targets, band_pct=band_pct)
    blocks = aggregate_blocks(plans)
    routes = route_blocks(blocks, tier_groups=tier_groups)
    return {"plans": plans, "blocks": blocks, "routes": routes}
