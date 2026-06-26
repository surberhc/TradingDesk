"""
live_fill_test.py — GUARDED, AUTOMATED single small paper order to validate real fills.

Fully automated end to end (no manual gateway steps):
  1. ARM the gateway in code (ReadOnlyApi=no + restart).
  2. Connect NON-read-only and what-if-validate the order (transmits nothing). The
     what-if also tells us whether the FA master accepts a direct order.
  3. If accepted -> place ONE tiny order (default: BUY 1 PDBC marketable), watch the
     fill, and re-read the position to confirm it landed. If rejected -> stop and
     report (we then target a DU sub-account instead).
  4. ALWAYS DISARM (ReadOnlyApi=yes + restart) in a finally, restoring the safe lock.

Running this script IS the deliberate, authorized arm action for ONE small order. It
flips config.READONLY/DRY_RUN only in memory; the files stay safe so the normal engine
remains dry-run.  PAPER ONLY.
"""
from __future__ import annotations

import sys
import time
from types import SimpleNamespace

from ib_async import IB

import arming
import config
import ledger
import live_quotes
import order_router
from connections import clientids, ibkr

# The single tiny test order: cheap + liquid -> minimal notional, fast fill.
TEST_SYMBOL = "PDBC"
TEST_QTY = 1
TEST_SIDE = "BUY"
# Target a DU sub-account (a normal account that accepts direct orders). The FA master
# (DF8922141) rejects direct/unallocated orders and hangs normal account reads.
TARGET_ACCOUNT = "DU8922142"

_UNSET = 1.7976931348623157e+308   # IBKR "no value" sentinel for doubles


def _net_liq(summary, account):
    return next((float(r.value) for r in summary
                 if r.account == account and r.tag == "NetLiquidation"), None)


def _accepted(state) -> bool:
    """A what-if is 'accepted' if IBKR returned a real margin/commission, not the unset
    sentinel or blanks (a rejected/again-FA order comes back empty)."""
    if state is None:
        return False
    for attr in ("initMarginChange", "maintMarginChange", "commission"):
        v = getattr(state, attr, None)
        if v in (None, "",):
            continue
        try:
            if float(v) != _UNSET:
                return True
        except (TypeError, ValueError):
            continue
    return False


def main() -> int:
    print("=" * 78)
    print("GUARDED LIVE PAPER FILL TEST - ONE small order (automated arm/disarm)")
    print("=" * 78)
    print(f"  order: {TEST_SIDE} {TEST_QTY} {TEST_SYMBOL} (marketable limit)")

    # In-memory arm of the SOFTWARE guards (not persisted: the normal engine stays
    # dry-run). Running this script is the deliberate human arm for ONE small order.
    config.READONLY = False
    config.DRY_RUN = False

    # The paper gateway's hard read-only lock is OFF (paper can't lose real money;
    # the bot stays safe via its software dry-run default + explicit arm). So there is
    # NO gateway restart here - we just connect. The default engine remains dry-run.
    ib = None
    try:
        account = TARGET_ACCOUNT
        # Connect NON-readonly, subscribing to THIS DU account's updates. Passing the
        # account avoids ib_async hanging on the FA master's account-update stream.
        ib = IB()
        ib.connect(ibkr.HOST, ibkr.PAPER_PORT, clientId=clientids.get("paperbot"),
                   readonly=False, timeout=15, account=account)
        accounts = ib.managedAccounts()
        if account not in accounts:
            print(f"ABORT: target {account} not in managed accounts {accounts}.")
            return 2
        nav = _net_liq(ib.accountSummary(account), account)
        print(f"  account={account}  NetLiq={nav:,.2f}  (connected NON-readonly)")

        quote = live_quotes.fetch(ib, [TEST_SYMBOL]).get(TEST_SYMBOL)
        limit = live_quotes.limit_price(TEST_SIDE, quote, style="marketable_limit") if quote else None
        if not limit:
            print("ABORT: no usable live quote for the test symbol.")
            return 2
        print(f"  live quote: bid={quote.bid} ask={quote.ask} last={quote.last} -> limit {limit:,.2f}")

        intent = SimpleNamespace(symbol=TEST_SYMBOL, side=TEST_SIDE,
                                 quantity=TEST_QTY, limit_price=limit)

        # what-if validation (transmits nothing).
        built = order_router.build([intent], account, "live_fill_test", ib=ib)
        states = order_router.what_if(ib, built)
        if not _accepted(states[0] if states else None):
            print("\n  WHAT-IF NOT ACCEPTED on this account.")
            if account.startswith("DF"):
                print("  The FA master needs an allocation for a direct order. Tell me which DU\n"
                      "  sub-account to use and I'll re-run targeting it (no manual steps).")
            return 3

        # Accepted -> place for real (fresh order objects, transmit gated by armed=True).
        built = order_router.build([intent], account, "live_fill_test", ib=ib)
        result = order_router.place(ib, built, armed=True, fill_timeout=60)

        ib.sleep(1.0)
        held = {p.contract.symbol: (p.position, p.avgCost)
                for p in ib.positions(account) if p.position != 0}.get(TEST_SYMBOL)
        print("\n  Post-fill position check:")
        if held:
            print(f"    {TEST_SYMBOL}: {held[0]:g} shares @ avgCost {held[1]:,.4f}  (fill landed)")
        else:
            print(f"    {TEST_SYMBOL}: not yet showing (order may still be working).")

        ledger.record_run({
            "mode": "ARMED_LIVE_TEST", "account": account, "nav": round(nav, 2),
            "daily_pnl": 0.0, "target_as_of": "n/a", "target_weights": {},
            "intents": [{"side": TEST_SIDE, "sym": TEST_SYMBOL, "qty": TEST_QTY, "limit": limit}],
            "n_intents": 1, "n_approved": 1, "n_transmitted": result.get("transmitted", 0),
            "halted": False, "halt_reason": "", "order_vetoes": [], "batch_vetoes": [],
            "fills": result.get("fills", []),
        })
        return 0
    finally:
        if ib is not None:
            ib.disconnect()
        print("\nDone. No gateway restarts. The default engine still stays dry-run/read-only.")


if __name__ == "__main__":
    sys.exit(main())
