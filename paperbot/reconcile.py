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

"UNPRICED" IS NOT "ZERO" (v0.42.0)
----------------------------------
Until v0.42.0 this module answered "the model wants none of this" and "I could not price
this" with the SAME number — target_shares = 0 — because the sizing expression fell
through to 0 whenever `has_price` was false. Downstream, `delta = target_shares -
actual_shares`, so a model holding we merely failed to quote was sized as a FULL
LIQUIDATION of a position the model actually wanted to keep; and the far more common
mirror (a model symbol the account does not hold yet, with no quote) contributed a 0-share
delta and 0.0 of trade weight, so the account read "in-spec, nothing to trade" while
holding none of that sleeve.

The two cases are now told apart explicitly:
  * weight == 0 with a USABLE price  -> target_shares 0, i.e. SELL. Legitimate rotation
    out of a holding the model no longer wants. UNCHANGED.
  * weight  > 0 with NO usable price -> status UNPRICED, `priced=False`, and
    target_shares pinned to int(actual_shares) so the delta is exactly 0. It can never
    become a sell leg, and it is never silently counted as conforming.

`usable_price()` below is the single definition of "we have a price". None, NaN, a
non-numeric, and a non-positive number all mean the SAME thing — unpriced — so a NaN
written into a prices dict (s0_live_deploy / s0_live_exec / live_fa_block_execute do
exactly that) and an absent key (batch_rebalance_execute drops the key) take one path.
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

# A symbol the model WANTS (weight > 0) that we could not price. Distinct from every
# status above: it is not a drift, not a rotation, not an alien holding — it is a DATA
# GAP, and the desk refuses to size or trade a symbol it cannot see. Never auto-traded.
UNPRICED = "UNPRICED"

# Plain-English reasons the two consumers (rebalance_engine / recon_report) render. Both
# name the symbol, because "some price was missing" is not something a human can act on.
UNPRICED_HELD_REASON = (
    "model holding {symbol} could not be priced and IS HELD ({shares:,.0f} share(s)) — its "
    "value is inside NetLiq but unknown, so the rest of the book cannot be sized against a "
    "base we cannot break down. NO orders are emitted for this account (fail-closed; "
    "human review)")
UNPRICED_WANTED_REASON = (
    "model holding {symbol} (target weight {weight:.2%}) could not be priced, so that "
    "sleeve was left in cash and NOT traded. The account is NOT in spec — it is missing "
    "{symbol} — and it cannot conform until a price is available (fail-closed)")


# THE definition of "we have a price for this" — one function, in the leaf module, so
# reconcile / rebalance_engine / recon_report / risk_manager can never disagree about it
# again (they each had their own inline version, and two of them were wrong). Re-exported
# here because this is where every sizing caller already looks.
usable_price = _investable.usable_price


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
    # Was this line's symbol PRICED? Defaults True so every pre-existing 7-arg positional
    # construction (tests, dashboards) is unchanged. False means target_shares /
    # actual_weight / drift_weight were computed WITHOUT a price and must not be read as
    # economic facts — the whole point is that downstream can now tell, which it could not
    # before v0.42.0.
    priced: bool = True

    @property
    def unpriced_and_wanted(self) -> bool:
        """The dangerous case: the model wants this symbol and we could not price it."""
        return self.status == UNPRICED


def reconcile(target: strategy_target.Target, nav: float, positions: dict,
              prices: dict | None = None, tolerance_w: float = 0.01,
              investable: float | None = None,
              universe: set[str] | None = None,
              whitelist: set[str] | None = None,
              cash_reserve_pct: float | None = None,
              strict_prices: bool = False) -> list[Line]:
    """Compare the strategy's target book against actual positions. `prices` (symbol->
    price) overrides the strategy-data close for valuation (e.g. live quotes).

    `strict_prices` — THE EXECUTION-PATH SWITCH (owner decision, v0.42.0)
    --------------------------------------------------------------------
    False (default, and every offline caller): `prices` merely OVERRIDES the model's own
    close, and a symbol absent from it falls back to `target.prices`. That is correct for
    the backtester and for offline readouts, where the strategy's price HISTORY is the
    legitimate source — you cannot compute a regime or a momentum weight from one live tick.

    True (every rail that can size or transmit a real order): the broker's live quotes in
    `prices` are the ONLY price source. The model's stored close is NOT consulted, because
    a stale daily close is not a price you can trade at, and quietly substituting one turns
    "IBKR would not quote this" into an order sized off yesterday. A symbol IBKR will not
    quote simply does not trade, and says so — see UNPRICED below.

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
        # ONE price gate for the whole module. None means UNPRICED — never 0.0, never NaN.
        # Under strict_prices the stored close is never consulted (execution rails).
        raw = (prices or {}).get(sym, None)
        if raw is None and not strict_prices:
            raw = target.prices.get(sym, None)
        price = usable_price(raw)
        has_price = price is not None
        actual_shares = float(positions.get(sym, 0.0))

        if weight > 0 and not has_price:
            # THE FIX (v0.42.0). We cannot size a symbol we cannot price. Pinning the
            # target to what is ALREADY held makes the downstream delta
            # (target_shares - int(actual_shares)) exactly 0, so even a consumer that has
            # never heard of the UNPRICED status cannot turn this into a liquidation.
            # Sizing it to 0 — the pre-0.42.0 behavior — meant "sell every share".
            target_shares = int(actual_shares)
        elif weight > 0:
            target_shares = int(weight * investable / price)
        else:
            # weight == 0 with a usable price: the model genuinely wants none of this.
            # target 0 => a SELL. Legitimate rotation-out; deliberately UNCHANGED.
            target_shares = 0

        actual_weight = (actual_shares * price / nav) if (has_price and nav) else 0.0
        drift_w = actual_weight - weight

        if weight > 0 and not has_price:
            # Ranked FIRST so it can never be reported as MISSING (which reads "we just
            # have not bought it yet") or MATCHED (which reads "conforming"). Neither is
            # true: we do not know.
            status = UNPRICED
        elif weight > 0 and actual_shares == 0:
            status = "MISSING"
        elif weight == 0 and actual_shares != 0:
            # universe=None -> "UNTRACKED" (legacy); otherwise the refined split.
            status = classify_untracked(sym, actual_shares, universe, whitelist)
        elif abs(drift_w) <= tolerance_w:
            status = "MATCHED"
        else:
            status = "DRIFTED"

        lines.append(Line(sym, weight, target_shares, actual_shares,
                          actual_weight, drift_w, status, priced=has_price))

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
    # usable_price(), not float(...), so a NaN quote cannot poison the sum into NaN and
    # turn the CASH line's drift comparison into a silent False (requirement: never let a
    # NaN reach a comparison). An unpriced line contributes nothing here — which is why the
    # engine BLOCKS an account that holds an unpriced model symbol rather than trusting it.
    def _valuation_price(sym):
        raw = (prices or {}).get(sym, None)
        if raw is None and not strict_prices:
            raw = target.prices.get(sym, None)
        return usable_price(raw) or 0.0

    risk_value = sum(ln.actual_shares * _valuation_price(ln.symbol) for ln in lines)
    cash_target_w, cash_actual_w = _investable.cash_line(nav, risk_value,
                                                        buffer=cash_reserve_pct)
    cash_drift_w = cash_actual_w - cash_target_w
    cash_status = "MATCHED" if abs(cash_drift_w) <= tolerance_w else "DRIFTED"
    lines.append(Line(_investable.CASH_SYMBOL, cash_target_w, 0, 0.0,
                      cash_actual_w, cash_drift_w, cash_status))
    return lines


# --- the one place that decides what an UNPRICED line COSTS the account ----------
# Two different consequences, and getting the split wrong turns one defect into another:
#
#   HELD + unpriced  -> BLOCK the account. Its value sits inside the broker's NetLiq but
#       we cannot break NetLiq down, so every sibling's target (weight * investable) is
#       sized against a base we cannot account for. Buy against that and the book can go
#       levered — and the leverage guard is exactly the guard that cannot see this symbol
#       either. This is the SAME failure the held-aside carve-out already fails closed on
#       (holding_class.UNPRICED_BLOCK_REASON), so it takes the SAME road: blocked_reasons.
#
#   NOT HELD + unpriced -> ISOLATE, do not block. Nothing of ours is tied up in it, NetLiq
#       is fully accounted for, and skipping it just leaves that sleeve in cash: the
#       account ends UNDER-invested, never levered. Benching the whole account for a symbol
#       we own none of would trade this defect for a different one (an account that cannot
#       trade at all). It IS reported, and it makes the account read NOT-in-spec.
def split_unpriced(lines) -> tuple[list, list]:
    """(held_unpriced, wanted_unpriced) — UNPRICED lines split by whether we hold any.

    ONE definition, imported by both rebalance_engine.plan_account and
    recon_report.plan_account so the two sizing paths can never disagree about which
    unpriced symbol benches an account and which one is merely parked."""
    held, wanted = [], []
    for ln in lines:
        if ln.status != UNPRICED:
            continue
        (held if int(ln.actual_shares) != 0 else wanted).append(ln)
    return held, wanted


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
