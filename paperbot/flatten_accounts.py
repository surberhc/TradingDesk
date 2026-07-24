"""
flatten_accounts.py — sell selected leftover EQUITY positions in explicitly-named DU
paper sub-accounts back to ZERO. Approved decision `paperbot-flatten`. PAPER ONLY.

SAFETY REWORK (conductor #51). This tool used to be a load-bearing hazard: it hardcoded
all five DU sub-accounts, took no arguments, hardcoded order.transmit=True, and *running
the module WAS the sweep* — so it would liquidate the entire live S0 book on launch. It
also priced every position through a Stock() contract, so an OPTION position was priced at
the UNDERLYING and legged out naked. It is now scoped, dry-run-by-default, and refuses
anything that isn't a plain equity/ETF. The guarantees this file now rests on:

  * NOTHING is in scope unless you name it. An explicit --accounts allowlist AND an
    explicit --symbols/--conids allowlist are BOTH required, with NO default. No allowlist
    -> hard guard, non-zero exit, no connection.
  * DRY-RUN IS THE DEFAULT. Running with no --execute prints the intended orders and places
    NOTHING. --execute is the only thing that transmits.
  * INSTRUMENT-AWARE. Only STK positions are flattened, with the real (bid/ask) equity
    price. Any non-STK secType (OPT/FUT/BAG/…) is REFUSED with a loud warning and marks the
    run non-flat — never priced off the underlying, never legged out.
  * Flatten validates its OWN price (rejects NaN / non-finite / <=0) instead of trusting an
    upstream module's invariant.
  * Any still-working order is CANCELLED before disconnect; a non-flat reconcile is a HARD
    FAILURE (non-zero exit), not a printed note.

Lessons already paid for, still applied here:
  * Trade the DU sub-accounts; pin the connection to a DU account so ib_async does not
    hang on the FA master's account-update stream.
  * Do NOT call whatIfOrder (it hangs). Place marketable-limit closing orders directly.
  * The gateway hard read-only lock is OFF, so no restart is needed - just connect.
  * Serialize: one closing order at a time; watch each fill; reconcile to zero; log.

TODO (combo close, conductor #51c — OUT OF SCOPE for this fix): closing a multi-leg
position (a vertical, etc.) safely means submitting it as ONE BAG/combo order, not legging
each side out independently. That is unimplemented. Until it exists, OPTION and other
non-STK positions are REFUSED outright (above) so no naked leg can ever be created.

Run — DRY-RUN preview (default, transmits nothing):
  ...python.exe flatten_accounts.py --accounts DU8922143 --symbols SPY VTI
Run — EXECUTE (actually transmit paper closing orders):
  ...python.exe flatten_accounts.py --accounts DU8922143 --symbols SPY VTI --execute
"""
from __future__ import annotations

import argparse
import copy
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ib_async import IB, LimitOrder   # noqa: E402

import ledger          # noqa: E402
import live_quotes     # noqa: E402
from connections import clientids, ibkr_paper      # noqa: E402

# Pin the connection to a DU sub-account (avoids the FA-master account-stream hang). Any
# DU account works as the pin; it does NOT define scope — scope comes only from --accounts.
PIN = "DU8922142"

# Statuses that mean an order is still resting/working at the broker (not yet terminal).
_WORKING = {"PendingSubmit", "PreSubmitted", "Submitted", "ApiPending", "PendingCancel"}


def _valid_price(x) -> bool:
    """A usable limit price: a real, finite, strictly-positive number. This is flatten's
    OWN guard — it rejects None, NaN and non-finite/non-positive values rather than trusting
    live_quotes' upstream invariant. `if not ref:` (the old guard) let NaN through because
    NaN is truthy; math.isfinite closes that hole."""
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def _in_scope(contract, symbols: set[str], conids: set[int]) -> bool:
    """A position is in scope iff its symbol is in the --symbols allowlist OR its conId is
    in the --conids allowlist. At least one allowlist is always non-empty (enforced in
    main()), so nothing is ever swept implicitly."""
    if symbols and contract.symbol in symbols:
        return True
    if conids and getattr(contract, "conId", 0) in conids:
        return True
    return False


def flatten(ib, accounts, symbols, conids, execute: bool) -> int:
    """Flatten the in-scope STK leftovers in `accounts` to zero. `ib` must already be
    connected (main() owns connect/disconnect; tests inject a mock). Returns a shell exit
    code: 0 only if the run is fully flat (every in-scope STK position closed and NOTHING
    refused); non-zero otherwise. DRY-RUN (execute=False) places nothing and returns 0/2
    purely as a preview verdict."""
    accounts = set(accounts)
    symbols = set(symbols or [])
    conids = set(conids or [])

    mode = "EXECUTE (will transmit)" if execute else "DRY-RUN (transmits nothing)"
    print(f"FLATTEN scoped -> zero  [{mode}]", flush=True)
    print(f"  accounts={sorted(accounts)}  symbols={sorted(symbols)}  "
          f"conids={sorted(conids)}", flush=True)

    ib.reqPositions()
    ib.sleep(2.0)
    leftovers = [p for p in ib.positions()
                 if p.account in accounts and p.position != 0
                 and _in_scope(p.contract, symbols, conids)]
    print(f"found {len(leftovers)} in-scope leftover position(s):", flush=True)
    for p in leftovers:
        print(f"  {p.account} {p.contract.symbol} {p.contract.secType} "
              f"{p.position:g}", flush=True)
    if not leftovers:
        print("NOTHING IN SCOPE IS HELD. nothing to do.", flush=True)
        return 0

    # A non-flat run is a HARD FAILURE. Any refusal (non-STK, unpriceable) flips this so the
    # process exits non-zero even if every order we DID place filled.
    non_flat = False
    fills: list[dict] = []
    working: list = []   # (trade, account, symbol) for orders still resting after the wait

    for p in leftovers:
        sym = p.contract.symbol

        # (b) INSTRUMENT-AWARE: refuse anything that isn't a plain equity/ETF. Pricing a
        # non-STK position through live_quotes.fetch (a Stock contract) would return the
        # UNDERLYING's price and leg out a naked option — strictly worse than not running.
        if p.contract.secType != "STK":
            print(f"  REFUSE {p.account} {sym}: secType={p.contract.secType} is not STK "
                  f"— options/combos are not flattened (see combo-close TODO). Run marked "
                  f"NOT flat.", flush=True)
            non_flat = True
            continue

        qty = p.position
        side = "SELL" if qty > 0 else "BUY"        # close longs / cover shorts
        q = live_quotes.fetch(ib, [sym]).get(sym)
        # marketable: hit the bid to sell, lift the ask to buy; fall back to last/close.
        ref = (q.bid if side == "SELL" else q.ask) if q else None
        if not _valid_price(ref):
            ref = (q.last or q.close) if q else None
        # (c) flatten's OWN hard price guard — NaN / non-finite / <=0 never reach round().
        if not _valid_price(ref):
            print(f"  SKIP {p.account} {sym}: no usable price (got {ref!r}). Run marked "
                  f"NOT flat.", flush=True)
            non_flat = True
            continue
        limit = round(ref, 2)

        # (e) do NOT mutate the live position's contract in place — copy it first, then
        # force SMART routing on the copy. A direct route to the listing exchange is rejected
        # by IBKR precautionary settings (Error 10311); conId still pins the exact instrument.
        contract = copy.copy(p.contract)
        contract.exchange = "SMART"
        order = LimitOrder(side, abs(qty), limit)
        order.account = p.account
        order.tif = "DAY"
        # Sweep may run after the RTH close; allow extended-hours execution so these liquid
        # names actually fill instead of sitting RTH-only.
        order.outsideRth = True
        order.orderRef = f"paperbot:flatten:{p.account}:{sym}"
        order.transmit = True

        if not execute:
            print(f"  WOULD {side} {abs(qty):g} {sym} @ {limit} in {p.account} "
                  f"(dry-run — not sent)", flush=True)
            continue

        print(f"  {side} {abs(qty):g} {sym} @ {limit} in {p.account} ...", flush=True)
        trade = ib.placeOrder(contract, order)
        waited = 0
        while waited < 30 and not trade.isDone():
            ib.sleep(1.0)
            waited += 1
        st = trade.orderStatus
        print(f"    -> {st.status} filled={st.filled:g} @ {st.avgFillPrice or 0:.2f}",
              flush=True)
        if not trade.isDone():
            working.append((trade, p.account, sym))
        fills.append({"account": p.account, "symbol": sym, "side": side,
                      "qty": abs(qty), "status": st.status,
                      "filled": float(st.filled), "avg": float(st.avgFillPrice or 0)})

    if not execute:
        verdict = 0 if not non_flat else 2
        print("\nDRY-RUN complete. Nothing transmitted. "
              f"{'Some positions would be REFUSED — see above.' if non_flat else 'All in-scope positions are flattenable.'}",
              flush=True)
        return verdict

    # (d) CANCEL any still-working order BEFORE disconnecting — a mispriced/unfilled DAY
    # order must never be left resting after the tool exits.
    if working:
        print("\nCANCELLING still-working orders before disconnect:", flush=True)
        for trade, acct, sym in working:
            print(f"  cancel {acct} {sym} (orderId {getattr(trade.order, 'orderId', '?')})",
                  flush=True)
            ib.cancelOrder(trade.order)
            non_flat = True   # anything left working means we did not reach flat cleanly
        ib.sleep(1.0)

    # Reconcile: re-read and confirm zero across the in-scope accounts+symbols.
    ib.reqPositions()
    ib.sleep(2.0)
    remaining = [(p.account, p.contract.symbol, p.position) for p in ib.positions()
                 if p.account in accounts and p.position != 0
                 and _in_scope(p.contract, symbols, conids)]
    print("\nRECONCILE:", flush=True)
    if remaining:
        print("  NOT flat yet:", remaining, flush=True)
        non_flat = True
    else:
        print("  ALL IN-SCOPE POSITIONS FLAT (zero).", flush=True)

    ledger.record_run({
        "mode": "FLATTEN", "account": ",".join(sorted(accounts)), "nav": 0.0,
        "daily_pnl": 0.0, "target_as_of": "n/a", "target_weights": {}, "intents": fills,
        "n_intents": len(fills), "n_approved": len(fills),
        "n_transmitted": len(fills), "halted": non_flat,
        "halt_reason": "non-flat reconcile" if non_flat else "",
        "order_vetoes": [], "batch_vetoes": [], "remaining": remaining,
    })
    # HARD FAILURE on any non-flat outcome (refused instrument, unpriceable, cancelled
    # working order, or a residual non-zero position).
    return 0 if not non_flat else 3


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="flatten_accounts.py",
        description="Flatten scoped EQUITY positions in named DU paper accounts to zero. "
                    "Scope is explicit and required; dry-run is the default.")
    p.add_argument("--accounts", nargs="+", default=None,
                   help="REQUIRED allowlist of DU account ids to act on (e.g. DU8922143). "
                        "No default — nothing is in scope unless named.")
    p.add_argument("--symbols", nargs="+", default=None,
                   help="Symbol allowlist (e.g. SPY VTI). Supply this and/or --conids.")
    p.add_argument("--conids", nargs="+", type=int, default=None,
                   help="conId allowlist. Supply this and/or --symbols.")
    p.add_argument("--execute", action="store_true",
                   help="Actually transmit closing orders. Omit for a dry-run preview "
                        "(the default) that places nothing.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)

    # (a) HARD GUARD at the TOP of main(): refuse to do anything without an explicit account
    # allowlist AND an explicit symbol/conId allowlist. A flatten tool must never default to
    # "everything in five accounts." No connection is opened on the refusal path.
    if not args.accounts or not (args.symbols or args.conids):
        print("REFUSING TO RUN: flatten requires an explicit --accounts allowlist AND at "
              "least one of --symbols / --conids. There is no default scope.", flush=True)
        print("  e.g. flatten_accounts.py --accounts DU8922143 --symbols SPY VTI"
              "        (dry-run preview)", flush=True)
        print("       flatten_accounts.py --accounts DU8922143 --symbols SPY VTI --execute"
              " (transmit)", flush=True)
        return 2

    ib = IB()
    ib.connect(ibkr_paper.HOST, ibkr_paper.PAPER_PORT,
               clientId=clientids.get("paperbot_flatten"),
               readonly=False, timeout=15, account=PIN)
    try:
        return flatten(ib, args.accounts, args.symbols, args.conids, args.execute)
    finally:
        ib.disconnect()
        print("disconnected.", flush=True)


if __name__ == "__main__":
    sys.exit(main())
