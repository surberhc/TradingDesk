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

Universe-aware refinement (S0 live-account corp-action guard, 2026-07-20)
------------------------------------------------------------------------
Passing `universe` (the strategy's tradeable symbols, from strategy.universe()) SPLITS
the single UNTRACKED bucket — which conflated two economically opposite cases — into
three precise ones so the engine can auto-trade a legitimate model rotation-out while
NEVER auto-liquidating an alien / corporate-action holding:
  ROTATE_OUT — held, weight 0, symbol ∈ universe, int(shares) >= 1
               (model dropped a KNOWN ticker -> should SELL: normal rebalancing)
  ALIEN      — held, weight 0, symbol ∉ universe, ∉ whitelist, ≠ cash symbol, int(shares)>=1
               (spinoff / rename / manual position -> REVIEW; the bot never auto-trades it)
  FRACTIONAL — held, weight 0, int(shares) == 0 but shares != 0
               (DRIP sub-share stub -> record, do not action; suppressed from the band)
  SWEEP      — held, weight 0, symbol is the cash symbol or in config.SWEEP_WHITELIST
               (a money-market sweep held by design -> not ALIEN, no order, no page)
When `universe is None` (backtester / paper callers that don't pass it) the classification
is IDENTICAL to before: all of the above collapse back to UNTRACKED. Behavior-preserving.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

import config
import investable as _investable
import strategy_target
from connections import clientids, ibkr_paper


# Refined statuses that a held, model-weight-0 symbol can take when a `universe` is given.
# UNTRACKED is the universe=None (behavior-preserving) collapse of all four.
ROTATE_OUT = "ROTATE_OUT"
ALIEN = "ALIEN"
FRACTIONAL = "FRACTIONAL"
SWEEP = "SWEEP"


def classify_untracked(symbol: str, actual_shares: float,
                       universe: set[str] | None,
                       whitelist: set[str] | None = None) -> str:
    """Refine a held symbol the model weights at 0 (actual_shares != 0 assumed by callers).

    Single source of truth for the split so reconcile() and alien_holdings() can never
    disagree. `universe is None` preserves the legacy single bucket exactly."""
    if universe is None:
        return "UNTRACKED"
    if symbol == _investable.CASH_SYMBOL or symbol in (whitelist or set()):
        return SWEEP
    if int(actual_shares) == 0:            # sub-1-share DRIP stub (truncation seam)
        return FRACTIONAL
    if symbol in universe:                 # a KNOWN ticker the model dropped this cycle
        return ROTATE_OUT
    return ALIEN                           # corp-action / manual holding -> human review


def alien_holdings(positions: dict, universe: set[str] | None,
                   whitelist: set[str] | None = None) -> list[tuple[str, float]]:
    """Held symbols that classify ALIEN (∉ universe, ∉ whitelist, ≠ cash, int(shares)>=1).

    A pure convenience for callers that have positions but no model Target in hand (the
    morning executor). Uses the SAME classify_untracked() predicate reconcile() does, so
    the two paths cannot drift. Returns [(symbol, shares), ...]."""
    out: list[tuple[str, float]] = []
    if universe is None:
        return out
    for sym, sh in positions.items():
        sh = float(sh)
        if sh == 0:
            continue
        if classify_untracked(sym, sh, universe, whitelist) == ALIEN:
            out.append((sym, sh))
    return out


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
              investable: float | None = None,
              universe: set[str] | None = None,
              whitelist: set[str] | None = None,
              cash_reserve_pct: float | None = None) -> list[Line]:
    """Compare the strategy's target book against actual positions. `prices` (symbol->
    price) overrides the strategy-data close for valuation (e.g. live quotes).

    `investable` overrides the capital sized against (default NAV*(1-cash_reserve)).
    The multi-account engine passes (NAV - distribution_reserve)*(1-cash_reserve) so a
    client's upcoming distribution is carved out before any buy is sized.

    `cash_reserve_pct` is THIS MODEL's cash reserve (1% for an Andrew-authored custom
    allocation, 1.5% for S0 and everything else). None -> the global default, so every
    pre-existing caller is byte-identical. It does TWO things and both matter:
      * it is the buffer used for the synthetic CASH line's TARGET weight, and
      * it is the buffer in the default `investable` when no explicit one is passed.
    Passing the sizing reserve here is not optional bookkeeping: if the plan is SIZED
    against 1% (rebalance_engine) but the CASH line is MEASURED against 1.5%, the account
    reads a permanent 0.5% phantom drift on cash, never reconciles, and churns.

    `universe` (the strategy's tradeable symbols) OPT-IN refines the single UNTRACKED
    status into ROTATE_OUT / ALIEN / FRACTIONAL / SWEEP (see module docstring). When
    None (the default — backtester and every existing caller), classification is
    byte-identical to before: UNTRACKED. `whitelist` (default config.SWEEP_WHITELIST when
    a universe is given) names sweep symbols excluded from ALIEN."""
    if universe is not None and whitelist is None:
        whitelist = set(getattr(config, "SWEEP_WHITELIST", set()))
    if investable is None:
        # Shared formula (investable module) with no distribution reserve carved out —
        # behavior-identical to the previous inline nav*(1-cash_reserve_pct) when
        # cash_reserve_pct is None.
        investable = _investable.compute_investable(nav, 0.0, cash_reserve_pct)
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
            # universe=None -> "UNTRACKED" (legacy); otherwise the refined split.
            status = classify_untracked(sym, actual_shares, universe, whitelist)
        elif abs(drift_w) <= tolerance_w:
            status = "MATCHED"
        else:
            status = "DRIFTED"

        lines.append(Line(sym, weight, target_shares, actual_shares,
                          actual_weight, drift_w, status))

    # --- Slice 3: explicit execution-side CASH bucket -------------------------
    # Each RISK line above measures drift against its TRUE model weight (no haircut),
    # exactly as before. But the model weights sum to ~100% with no cash line, while the
    # account is deliberately holding back the buffer — so without a cash bucket the book
    # does not sum to 100% and looks "light". Add a synthetic CASH line whose target is the
    # standing buffer and whose actual is the real uninvested cash fraction. A correctly
    # invested account then reads ~0 drift on CASH and the book sums to ~100%.
    #
    # This is READOUT-ONLY: no shares are sized here (target_shares=0, actual_shares=0.0),
    # and the loop above is untouched, so order quantities are exactly what Slice 2 produced.
    #
    # PER-MODEL: the CASH target is THIS model's reserve, the same number the plan sized
    # against — not the global default. Measuring cash against a buffer the account was
    # never sized to is permanent phantom drift (see the `cash_reserve_pct` note above).
    risk_value = sum(ln.actual_shares * float((prices or {}).get(ln.symbol,
                     target.prices.get(ln.symbol, 0.0)))
                     for ln in lines)
    cash_target_w, cash_actual_w = _investable.cash_line(nav, risk_value,
                                                        buffer=cash_reserve_pct)
    cash_drift_w = cash_actual_w - cash_target_w
    cash_status = "MATCHED" if abs(cash_drift_w) <= tolerance_w else "DRIFTED"
    lines.append(Line(_investable.CASH_SYMBOL, cash_target_w, 0, 0.0,
                      cash_actual_w, cash_drift_w, cash_status))
    return lines


def main() -> int:
    print("=" * 74)
    print("PAPERBOT RECONCILIATION - read-only (does the book match the target?)")
    print("=" * 74)

    target = strategy_target.current_target()
    try:
        ib = ibkr_paper.connect("paperbot", readonly=True, launch=True)
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
