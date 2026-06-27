"""
recon_report.py — daily multi-account reconciliation & drift report. READ-ONLY.

The compliance readout for the Option B (multi-account) program. For every enrolled,
funded client sub-account it shows, with ZERO transmission:

  * the account's risk tier (model version) and scheduled cash flows
  * its distribution RESERVE (held liquid) and the investable capital after it
  * per-holding drift vs the model, and whether any holding breaches the no-trade band
  * the orders it WOULD take to return to model — aggregated into per-tier BLOCK
    orders (one average price for everyone) with the exact per-account share split

This is the piece that proves the design before any order is armed: rules-based
targets (NetLiq − reserve) × model weights, one documented band applied identically
to every account, cash earmarked for distributions never invested, and a per-account
allocation set at order-build time (not after fills). It places no orders.

Run (gateway auto-starts if down):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\paperbot\\recon_report.py"
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

import accounts
import cashflows
import config
import reconcile
import strategy_target
import version
from connections import clientids, ibkr


@dataclass
class AccountPlan:
    """One account's reconciliation result + the per-symbol share deltas to fix it."""
    account: str
    version: str
    net_liq: float
    reserve: float
    investable: float
    lines: list            # reconcile.Line per symbol
    needs_rebalance: bool
    orders: dict           # symbol -> signed share delta (target - actual); empty if in-band


@dataclass
class BlockOrder:
    """Same-tier, same-symbol, same-side orders aggregated for one average-price fill."""
    version: str
    symbol: str
    side: str              # BUY | SELL
    total_qty: int
    per_account: dict = field(default_factory=dict)   # account -> shares
    reason: str = "REBALANCE_TO_MODEL"


def _targets_by_version() -> dict:
    """Run the validated engine once per DISTINCT enrolled version (compliance: the
    model per risk tier, not a per-client guess)."""
    out = {}
    for version in sorted(set(config.ENROLLMENT.values())):
        out[version] = strategy_target.current_target(version=version)
    return out


def plan_account(account: str, version: str, net_liq: float, positions: dict,
                 target: strategy_target.Target) -> AccountPlan:
    """Reconcile one account against its tier model, reserving its distribution cash."""
    reserve = cashflows.reserve_for(account, net_liq)
    investable = (net_liq - reserve) * (1.0 - config.RISK_LIMITS["cash_reserve_pct"])
    lines = reconcile.reconcile(target, net_liq, positions,
                                tolerance_w=config.REBALANCE_BAND_PCT,
                                investable=investable)
    # NO-TRADE BAND — ACCOUNT-LEVEL, all-or-nothing. Mirrors rebalance_engine.plan_account
    # exactly so this readout's REBALANCE/in-band labels match what the engine actually does.
    # The breach test keys on the SIZE OF THE TRADE the rebalance would make
    # (|target_shares - actual_shares| valued vs NetLiq), NOT raw weight-vs-model drift:
    # the cash reserve means a fully-invested account sits ~reserve% under its raw model
    # weight by construction, so the old status-based test falsely flagged correctly-invested
    # accounts and defeated the band on any holding over ~60% weight. A stray UNTRACKED
    # position always breaches (it must be cleared regardless of size). (Replicated here, not
    # imported: rebalance_engine imports AccountPlan/BlockOrder FROM this module.)
    band_pct = config.REBALANCE_BAND_PCT

    def _trade_weight(ln) -> float:
        # No live `prices` override in this readout's signature; size off the strategy close.
        price = float(target.prices.get(ln.symbol, float("nan")))
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


def aggregate_blocks(plans: list[AccountPlan]) -> list[BlockOrder]:
    """Aggregate same-tier, same-symbol, same-side deltas into block orders. Buys and
    sells stay separate blocks (a block executes one side); the per-account map records
    the exact allocation, fixed here at build time — never reallocated after fills."""
    blocks: dict[tuple, BlockOrder] = {}
    for p in plans:
        for sym, delta in p.orders.items():
            side = "BUY" if delta > 0 else "SELL"
            key = (p.version, sym, side)
            blk = blocks.get(key)
            if blk is None:
                blk = blocks[key] = BlockOrder(p.version, sym, side, 0)
            blk.total_qty += abs(delta)
            blk.per_account[p.account] = abs(delta)
    return sorted(blocks.values(), key=lambda b: (b.version, b.symbol, b.side))


def main() -> int:
    print("=" * 92)
    print(f"MULTI-ACCOUNT RECONCILIATION & DRIFT REPORT - read-only, no orders   "
          f"[{version.banner()}]")
    print(f"band=+/-{config.REBALANCE_BAND_PCT*100:.0f}% of NAV   "
          f"cash_reserve={config.RISK_LIMITS['cash_reserve_pct']*100:.0f}%   "
          f"distribution_reserve={cashflows.RESERVE_MONTHS} month(s)")
    print("=" * 92)

    # Models per tier (read-only compute) BEFORE connecting — fails fast if data is stale.
    print("\nComputing tier models (Conservative/Balanced/Growth as enrolled)...")
    targets = _targets_by_version()
    for v, t in targets.items():
        print(f"  {v:13s} as_of={t.as_of.date()}  ({len(t.weights)} holdings)")

    try:
        ib = ibkr.connect("paperbot_recon", readonly=True, launch=True)
    except Exception as exc:
        print(f"\nCOULD NOT CONNECT: {exc}")
        return 1

    try:
        infos = accounts.discover(ib)
        clients = [i for i in infos if i.enrolled and i.funded and not i.is_master]
        if not clients:
            print("\nNo enrolled + funded client accounts to reconcile.")
            return 0

        plans: list[AccountPlan] = []
        for info in sorted(clients, key=lambda x: x.number):
            positions = {p.contract.symbol: p.position
                         for p in ib.positions(info.number) if p.position != 0}
            plans.append(plan_account(info.number, info.version, info.net_liq,
                                      positions, targets[info.version]))

        # --- Section A: per-account reconciliation ---
        print(f"\n{'ACCOUNT':12s} {'TIER':13s} {'NETLIQ':>14s} {'RESERVE':>10s} "
              f"{'INVESTABLE':>14s} {'DRIFTED':>7s}  {'ACTION':9s}  CASH FLOWS")
        print("-" * 92)
        for p in plans:
            n_drift = sum(1 for ln in p.lines
                          if ln.status in ("DRIFTED", "MISSING", "UNTRACKED"))
            action = "REBALANCE" if p.needs_rebalance else "in-band"
            print(f"{p.account:12s} {p.version:13s} {p.net_liq:>14,.0f} {p.reserve:>10,.0f} "
                  f"{p.investable:>14,.0f} {n_drift:>7d}  {action:9s}  "
                  f"{cashflows.describe(p.account, p.net_liq)}")
        print("-" * 92)

        # --- Section B: aggregated block orders (fair single-price execution) ---
        blocks = aggregate_blocks(plans)
        print("\nAGGREGATED BLOCK ORDERS (one average price per block; per-account split fixed "
              "at build time)")
        if not blocks:
            print("  none - every enrolled account is within the drift band. No trades.")
        else:
            cur_tier = None
            for b in blocks:
                if b.version != cur_tier:
                    cur_tier = b.version
                    print(f"\n  [{cur_tier}]")
                split = "  ".join(f"{a[-4:]}:{q}" for a, q in sorted(b.per_account.items()))
                print(f"    {b.side:4s} {b.symbol:6s} x{b.total_qty:<7d} {b.reason:18s} -> {split}")
            print(f"\n  {len(blocks)} block order(s) across "
                  f"{sum(1 for p in plans if p.needs_rebalance)} account(s) needing rebalance.")

        print("\nDone. READ-ONLY: nothing was transmitted, no orders were built. "
              "Review-only readout.")
        return 0
    finally:
        ib.disconnect()
        print("Read-only session closed.")


if __name__ == "__main__":
    sys.exit(main())
