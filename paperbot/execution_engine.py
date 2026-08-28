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
  [4] risk_manager     -> per-order + cash-reserve guards (vetoes). NO daily-loss halt:
                          removed 2026-08-25 by owner decision; the day's P&L is read for
                          the audit ledger only and gates nothing.
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
import reconcile
import risk_manager
import strategy_target
from connections import clientids, ibkr_paper


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
    """Best-effort today's realized+unrealized P&L for `account` (IBKR reqPnL).

    AN AUDIT FIGURE, NOT A BREAKER INPUT. Nothing on this desk halts on the day's P&L —
    the automated daily-loss halt was removed 2026-08-25 by owner decision — so this
    number's ONLY job is to be printed and recorded in the ledger's audit record for the
    run. It is deliberately best-effort: any broker/API failure, or a NaN, returns 0.0,
    because an unreadable audit figure is not a reason to refuse to trade.

    (The strict reader that used to sit behind this — read_daily_pnl / DailyPnlUnavailable
    — existed only to feed that breaker and was removed with it.)"""
    try:
        pnl = ib.reqPnL(account)
        ib.sleep(1.5)
        value = float(pnl.dailyPnL)
        ib.cancelPnL(account)
    except Exception:   # noqa: BLE001 — any broker/API shape failure just means "no figure"
        return 0.0
    if value != value:  # NaN — the subscription answered with no number
        return 0.0
    return value


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
                            quotes: dict | None = None,
                            cash_reserve_pct: float | None = None) -> list[IntendedOrder]:
    """Diff the strategy's target book against actual paper positions -> orders.

    Positions are sized against INVESTABLE capital = NAV*(1-cash_reserve_pct) and
    floored to whole shares, so the cash reserve is respected by construction and the
    book never levers up. The relative weights still match the validated strategy.

    `cash_reserve_pct` is THIS model's standing reserve (1% for an Andrew-authored custom
    allocation, 1.5% otherwise). None -> the global default, so this single-account S0 path
    is byte-identical to before. A caller that passes it here MUST pass the same value to
    risk_manager.evaluate, or the reserve guard will veto the very book this sized.

    PRICING — LIVE IBKR QUOTE ONLY ON THE EXECUTION PATH (owner decision, v0.42.0)
    ------------------------------------------------------------------------------
    When a `quotes` dict is supplied this is a real execution path: the live quote is the
    ONLY price source and the model's stored daily close is NEVER substituted for a quote
    IBKR would not give. With `quotes=None` (the offline what-if / unit-test shape) there is
    no broker in the picture and no execution to protect, so the strategy-data close is still
    used — that is the model's own price series doing its legitimate job.

    "UNPRICED" IS NOT "TARGET ZERO". This function used to compute `target_shares = 0` for
    "not in target (or no price)" — one number for two opposite meanings — and the delta that
    followed (0 - held) was a FULL LIQUIDATION of a position the model wanted to KEEP. A
    symbol we cannot price now produces NO order at all and is named in a printed warning.
    A symbol with weight == 0 AND a usable price still sells: legitimate rotation, unchanged.
    The limit price likewise can no longer fall through to 0.0 — every emitted order carries
    a real, positive limit.
    """
    # Shared formula (investable module), no distribution reserve carved out here —
    # behavior-identical to the previous inline nav*(1-cash_reserve_pct) when
    # cash_reserve_pct is None.
    investable = _investable.compute_investable(nav, 0.0, cash_reserve_pct)
    orders: list[IntendedOrder] = []
    unpriced: list[str] = []
    live_only = quotes is not None      # a quotes dict means "we are executing"
    symbols = set(target.weights.index) | set(positions)
    for sym in sorted(symbols):
        weight = float(target.weights.get(sym, 0.0))
        q = quotes.get(sym) if quotes else None
        live_ref = live_quotes.reference_price(q) if q is not None else None

        size_price = reconcile.usable_price(live_ref)
        source = "LIVE"
        if size_price is None and not live_only:
            size_price = reconcile.usable_price(target.prices.get(sym, None))
            source = "CLOSE"
        current = float(positions.get(sym, 0.0))

        if size_price is None:
            # No price at all. Refuse to act on it in EITHER direction: we cannot size a buy,
            # and we cannot responsibly price a sell. Report it; never silently size it to 0.
            if weight > 0 or current != 0:
                unpriced.append(sym)
            continue

        if weight > 0:
            target_dollars = weight * investable
            target_shares = int(target_dollars / size_price)   # floor: never over-allocate
        else:
            target_dollars = 0.0
            target_shares = 0   # model wants none of it AND we have a price -> SELL (rotation)

        delta = target_shares - current
        if abs(delta) < 1:
            continue

        side = "BUY" if delta > 0 else "SELL"
        limit = live_quotes.limit_price(side, q) if q is not None else None
        if limit is None:
            # size_price is guaranteed real and positive here, so this can never be 0.0.
            limit = round(size_price, 2)
            source = source if source == "LIVE" else "CLOSE"

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
    if unpriced:
        # Counted and surfaced, never a silent omission.
        print(f"    !! NO USABLE PRICE for {len(unpriced)} symbol(s): {', '.join(unpriced)}. "
              f"NO order was generated for them in either direction. The book is NOT in "
              f"spec — it is missing/holding a sleeve that cannot be sized.")
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
    print(f"\n[2] Connecting to PAPER gateway {ibkr_paper.HOST}:{ibkr_paper.PAPER_PORT} "
          f"(clientId={clientids.get('paperbot')}, readonly=True)...")
    try:
        ib = ibkr_paper.connect("paperbot", readonly=True, launch=True)
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

        # [4] RISK GUARDS — per-order caps + cash reserve (vetoes). There is NO automated
        # daily-loss halt: removed 2026-08-25 by owner decision. daily_pnl is passed for
        # the ledger's audit record only and gates nothing.
        print("\n[4] Risk checks (per-order caps / cash reserve)...")
        report = risk_manager.evaluate(nav, daily_pnl, positions, orders, target)
        if report.halted:
            # risk_manager never sets this any more; kept because RiskReport.halted is a
            # public field a caller could still set.
            print(f"    RISK REPORT HALTED -> no orders routed. {report.halt_reason}")
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
