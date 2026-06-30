"""
execution_engine.py — DRY-RUN paper execution engine.

Build-order steps 2-4 (docs/HANDOFF.md §5), all in DRY RUN: connect to the PAPER
account, read its NAV and positions, ask the shared strategy what it wants to hold,
diff that against what the account actually holds, run every RISK guard over the
result, and have the OrderRouter CONSTRUCT (but not send) the limit orders. It
transmits NOTHING.

Pipeline:
  [1] strategy_target  -> the book the validated strategy wants now (read-only compute)
  [2] connect (read-only) + confirm the paper account, read NAV / positions / daily P&L
  [3] diff target vs actual -> intended orders (sized against NAV*(1-cash_reserve))
  [4] risk_manager     -> kill switch + per-order + cash-reserve guards (vetoes)
  [5] order_router     -> build the exact IBKR limit orders, log them, transmit nothing

Safety posture (enforced here): READONLY + DRY_RUN required; account must end in
config.ACCOUNT_SUFFIX and be a paper (DU/DF) account. Transmission is impossible until
the guards exist (they now do), READONLY/DRY_RUN are deliberately turned off, AND a
human arms the session. None of that happens in this file.

Run (gateway auto-starts if down):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\paperbot\\execution_engine.py"
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

import config
import investable as _investable
import ledger
import live_quotes
import order_router
import risk_manager
import strategy_target
from connections import clientids, ibkr


@dataclass
class IntendedOrder:
    """One trade the engine WOULD place to move the book toward target. Not sent."""
    symbol: str
    side: str            # BUY / SELL
    quantity: int        # whole shares
    limit_price: float   # indicative (last close); the real limit comes from a live
                         # quote in the (future) live-quote step
    target_weight: float
    target_dollars: float
    current_shares: float
    price_source: str = "CLOSE"   # LIVE (market quote) or CLOSE (strategy-data fallback)
    legs: int = 1        # ETF orders are single-leg (RiskManager checks this)


def _net_liq(summary, account: str) -> float | None:
    for row in summary:
        if row.account == account and row.tag == "NetLiquidation":
            return float(row.value)
    return None


def _daily_pnl(ib, account: str) -> float:
    """Best-effort today's P&L for the kill switch. 0.0 if unavailable (e.g. flat)."""
    try:
        pnl = ib.reqPnL(account)
        ib.sleep(1.5)
        value = float(pnl.dailyPnL)
        ib.cancelPnL(account)
        return value if value == value else 0.0   # nan -> 0.0
    except Exception:
        return 0.0


def _run_record(account, nav, daily_pnl, target, orders, report, transmitted) -> dict:
    """Assemble the audit record for one engine run (written to the ledger)."""
    return {
        "mode": "DRY_RUN",
        "account": account,
        "nav": round(nav, 2),
        "daily_pnl": round(daily_pnl, 2),
        "target_as_of": str(target.as_of.date()),
        "target_weights": {k: round(float(v), 4) for k, v in target.weights.items()},
        "intents": [{"side": o.side, "sym": o.symbol, "qty": o.quantity,
                     "limit": o.limit_price, "src": o.price_source} for o in orders],
        "n_intents": len(orders),
        "n_approved": len(report.approved),
        "n_transmitted": transmitted,
        "halted": report.halted,
        "halt_reason": report.halt_reason,
        "order_vetoes": [{"sym": v.symbol, "reasons": v.reasons}
                         for v in report.order_verdicts if not v.ok],
        "batch_vetoes": report.batch_reasons,
    }


def compute_intended_orders(nav: float, positions: dict, target: strategy_target.Target,
                            quotes: dict | None = None) -> list[IntendedOrder]:
    """Diff the strategy's target book against actual paper positions -> orders.

    Positions are sized against INVESTABLE capital = NAV*(1-cash_reserve_pct) and
    floored to whole shares, so the cash reserve is respected by construction and the
    book never levers up. The relative weights still match the validated strategy.

    Sizing + limit prices use LIVE quotes when available (read-only market data) and
    fall back to the strategy-data close per symbol if a quote is missing.
    """
    # Shared formula (investable module), no distribution reserve carved out here —
    # behavior-identical to the previous inline nav*(1-cash_reserve_pct).
    investable = _investable.compute_investable(nav, 0.0)
    orders: list[IntendedOrder] = []
    symbols = set(target.weights.index) | set(positions)
    for sym in sorted(symbols):
        weight = float(target.weights.get(sym, 0.0))
        data_close = float(target.prices.get(sym, float("nan")))
        q = quotes.get(sym) if quotes else None
        live_ref = live_quotes.reference_price(q) if q else None

        if live_ref and live_ref > 0:
            size_price, source = live_ref, "LIVE"
        else:
            size_price, source = data_close, "CLOSE"
        current = float(positions.get(sym, 0.0))

        if weight > 0 and size_price == size_price and size_price > 0:
            target_dollars = weight * investable
            target_shares = int(target_dollars / size_price)   # floor: never over-allocate
        else:
            target_dollars = 0.0
            target_shares = 0  # not in target (or no price) -> close the position

        delta = target_shares - current
        if abs(delta) < 1:
            continue

        side = "BUY" if delta > 0 else "SELL"
        limit = live_quotes.limit_price(side, q) if q else None
        if limit is None:
            limit = round(size_price, 2) if size_price == size_price else 0.0
            source = "CLOSE"

        orders.append(IntendedOrder(
            symbol=sym,
            side=side,
            quantity=int(abs(delta)),
            limit_price=limit,
            target_weight=weight,
            target_dollars=target_dollars,
            current_shares=current,
            price_source=source,
        ))
    return orders


def main() -> int:
    print("=" * 78)
    print("PAPERBOT EXECUTION ENGINE - DRY RUN (logs intended orders, transmits nothing)")
    print("=" * 78)

    if not (config.READONLY and config.DRY_RUN):
        print("SAFETY STOP: this engine requires READONLY=True and DRY_RUN=True in config.")
        return 2

    # [1] Strategy target — read-only compute, identical code path to the backtest.
    print("\n[1] Computing the strategy's current target book (running the validated engine)...")
    target = strategy_target.current_target()
    print(f"    version={target.version}   rebalance as_of={target.as_of.date()}   "
          f"price_date={target.price_date.date()}")
    print("    target book:")
    for sym, wt in target.weights.sort_values(ascending=False).items():
        print(f"      {sym:6s} {wt * 100:6.2f}%   last={float(target.prices.get(sym, float('nan'))):>10,.2f}")

    # [2] Connect to the PAPER account (read-only) and confirm it.
    print(f"\n[2] Connecting to PAPER gateway {ibkr.HOST}:{ibkr.PAPER_PORT} "
          f"(clientId={clientids.get('paperbot')}, readonly=True)...")
    try:
        ib = ibkr.connect("paperbot", readonly=True, launch=True)
    except Exception as exc:
        print(f"    COULD NOT CONNECT: {exc}")
        return 1

    try:
        accounts = ib.managedAccounts()
        matches = [a for a in accounts if a.endswith(config.ACCOUNT_SUFFIX)]
        if len(matches) != 1:
            print(f"    SAFETY STOP: need exactly one account ending in "
                  f"'{config.ACCOUNT_SUFFIX}', found {matches}.")
            return 2
        account = matches[0]
        if not account.startswith(("DU", "DF")):
            print(f"    SAFETY STOP: {account} is not a paper (DU/DF) account. Halting.")
            return 2

        summary = ib.accountSummary(account)
        nav = _net_liq(summary, account)
        if not nav or nav <= 0:
            print(f"    SAFETY STOP: could not read a positive NetLiquidation for {account}.")
            return 2
        positions = {p.contract.symbol: p.position
                     for p in ib.positions(account) if p.position != 0}
        daily_pnl = _daily_pnl(ib, account)
        print(f"    account={account}   NetLiq={nav:,.2f} USD   open_positions={len(positions)}"
              f"   dailyPnL={daily_pnl:,.2f}")

        # [2b] Live quotes (read-only market data) for sizing + limit prices.
        print("\n[2b] Fetching live quotes (read-only market data)...")
        universe = sorted(set(target.weights.index) | set(positions))
        quotes = live_quotes.fetch(ib, universe)
        n_live = sum(1 for q in quotes.values() if q.md_type == 1)
        print(f"    {len(quotes)} symbols quoted; {n_live} live (mdType=1), "
              f"{len(quotes) - n_live} delayed/unavailable.")

        # [3] Diff target vs actual -> intended orders (sized to investable capital).
        print("\n[3] Diffing target book against actual paper positions "
              f"(sized vs investable = NAV*(1-{config.RISK_LIMITS['cash_reserve_pct']*100:.0f}%))...")
        orders = compute_intended_orders(nav, positions, target, quotes)
        if not orders:
            print("    none - the account already matches the target book.")
        else:
            print(f"    {'SIDE':4s} {'SYM':6s} {'QTY':>7s}  {'LIMIT':>10s} {'SRC':>5s}  "
                  f"{'TGT_W':>7s}  {'TGT_$':>13s}  {'CUR_SH':>8s}")
            print("    " + "-" * 72)
            for o in orders:
                print(f"    {o.side:4s} {o.symbol:6s} {o.quantity:>7d}  "
                      f"{o.limit_price:>10,.2f} {o.price_source:>5s}  {o.target_weight * 100:>6.2f}%  "
                      f"{o.target_dollars:>13,.2f}  {o.current_shares:>8,.0f}")
            print("    " + "-" * 72)

        # [4] RISK GUARDS — kill switch + per-order + cash reserve (vetoes).
        print("\n[4] Risk checks (kill switch / per-order caps / cash reserve)...")
        report = risk_manager.evaluate(nav, daily_pnl, positions, orders, target)
        if report.halted:
            print(f"    KILL SWITCH ENGAGED -> HALT. {report.halt_reason}")
            print("    No order may be routed while halted. (DRY RUN: nothing was sent.)")
            ledger.record_run(_run_record(account, nav, daily_pnl, target, orders, report, 0))
            return 0
        for v in report.order_verdicts:
            if v.ok:
                print(f"    OK    {v.symbol}")
            else:
                print(f"    VETO  {v.symbol}: {'; '.join(v.reasons)}")
        if report.batch_reasons:
            for r in report.batch_reasons:
                print(f"    BATCH VETO: {r}")
        print(f"    -> {len(report.approved)} of {len(orders)} order(s) approved"
              f"{' (all clear)' if report.all_clear else ''}.")

        # [5] ORDER ROUTER — construct the exact IBKR limit orders, log, transmit nothing.
        print("\n[5] Routing approved orders (build-only; transmission arm-gated)...")
        built = order_router.build(report.approved, account, target.as_of, ib=ib)
        result = order_router.place(ib, built, armed=False)

        path = ledger.record_run(_run_record(account, nav, daily_pnl, target, orders, report,
                                             result.get("transmitted", 0)))
        print(f"\n    Run recorded to the audit ledger: {path}")
        print("    NOTE: transmission stays impossible until READONLY and DRY_RUN are")
        print("    deliberately turned off AND a human arms the session. DRY RUN complete.")
        return 0
    finally:
        ib.disconnect()
        print("\nRead-only session closed. Nothing was transmitted.")


if __name__ == "__main__":
    sys.exit(main())
