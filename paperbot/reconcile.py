"""
reconcile.py — read-only reconciliation: does the paper book match the strategy target?

The other half of "fills + reconciliation": after orders fill (or any time), confirm the
ACTUAL broker positions match what the strategy INTENDS. Pure read-only — it places no
order. Importable (the engine / monitors call reconcile()) and runnable standalone:

  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\paperbot\\reconcile.py"

Per-ticker status:
  MATCHED   — held weight within tolerance of target
  DRIFTED   — held, but off target by more than tolerance (a rebalance would correct it)
  MISSING   — wanted by the strategy but not held (e.g. before the first fill)
  UNTRACKED — held but NOT in the target (manual position / leftover -> investigate)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

import config
import investable as _investable
import strategy_target
from connections import clientids, ibkr


@dataclass
class Line:
    symbol: str
    target_weight: float
    target_shares: int
    actual_shares: float
    actual_weight: float
    drift_weight: float
    status: str


def reconcile(target: strategy_target.Target, nav: float, positions: dict,
              prices: dict | None = None, tolerance_w: float = 0.01,
              investable: float | None = None) -> list[Line]:
    """Compare the strategy's target book against actual positions. `prices` (symbol->
    price) overrides the strategy-data close for valuation (e.g. live quotes).

    `investable` overrides the capital sized against (default NAV*(1-cash_reserve)).
    The multi-account engine passes (NAV - distribution_reserve)*(1-cash_reserve) so a
    client's upcoming distribution is carved out before any buy is sized."""
    if investable is None:
        # Shared formula (investable module) with no distribution reserve carved out —
        # behavior-identical to the previous inline nav*(1-cash_reserve_pct).
        investable = _investable.compute_investable(nav, 0.0)
    lines: list[Line] = []
    for sym in sorted(set(target.weights.index) | set(positions)):
        weight = float(target.weights.get(sym, 0.0))
        price = float((prices or {}).get(sym, target.prices.get(sym, float("nan"))))
        has_price = price == price and price > 0

        target_shares = int(weight * investable / price) if (weight > 0 and has_price) else 0
        actual_shares = float(positions.get(sym, 0.0))
        actual_weight = (actual_shares * price / nav) if (has_price and nav) else 0.0
        drift_w = actual_weight - weight

        if weight > 0 and actual_shares == 0:
            status = "MISSING"
        elif weight == 0 and actual_shares != 0:
            status = "UNTRACKED"
        elif abs(drift_w) <= tolerance_w:
            status = "MATCHED"
        else:
            status = "DRIFTED"

        lines.append(Line(sym, weight, target_shares, actual_shares,
                          actual_weight, drift_w, status))
    return lines


def main() -> int:
    print("=" * 74)
    print("PAPERBOT RECONCILIATION - read-only (does the book match the target?)")
    print("=" * 74)

    target = strategy_target.current_target()
    try:
        ib = ibkr.connect("paperbot", readonly=True, launch=True)
    except Exception as exc:
        print(f"COULD NOT CONNECT: {exc}")
        return 1
    try:
        accounts = ib.managedAccounts()
        matches = [a for a in accounts if a.endswith(config.ACCOUNT_SUFFIX)]
        if len(matches) != 1 or not matches[0].startswith(("DU", "DF")):
            print(f"SAFETY STOP: need one paper account ending in '{config.ACCOUNT_SUFFIX}', "
                  f"found {matches}.")
            return 2
        account = matches[0]
        nav = next((float(r.value) for r in ib.accountSummary(account)
                    if r.tag == "NetLiquidation"), None)
        positions = {p.contract.symbol: p.position
                     for p in ib.positions(account) if p.position != 0}
        print(f"account={account}   NetLiq={nav:,.2f}   target as_of={target.as_of.date()}\n")

        lines = reconcile(target, nav, positions)
        print(f"  {'STATUS':9s} {'SYM':6s} {'TGT_W':>7s} {'ACT_W':>7s} {'DRIFT':>7s} "
              f"{'TGT_SH':>7s} {'ACT_SH':>7s}")
        print("  " + "-" * 60)
        for ln in lines:
            print(f"  {ln.status:9s} {ln.symbol:6s} {ln.target_weight * 100:>6.2f}% "
                  f"{ln.actual_weight * 100:>6.2f}% {ln.drift_weight * 100:>+6.2f}% "
                  f"{ln.target_shares:>7d} {ln.actual_shares:>7,.0f}")
        print("  " + "-" * 60)
        n_match = sum(1 for ln in lines if ln.status == "MATCHED")
        aligned = all(ln.status == "MATCHED" for ln in lines)
        print(f"  {n_match}/{len(lines)} matched. "
              + ("BOOK ALIGNED with target." if aligned
                 else "BOOK NOT aligned - a rebalance would move it toward target."))
        return 0
    finally:
        ib.disconnect()
        print("\nRead-only session closed.")


if __name__ == "__main__":
    sys.exit(main())
