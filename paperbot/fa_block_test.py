"""
fa_block_test.py — validate the BLOCK (FA group) order mechanism. WHAT-IF ONLY.

The core question of Option B: will the FA master accept an API-placed group order —
one order it executes as a block at a single average price and splits across a tier's
accounts? This answers it WITHOUT transmitting anything: it builds a group order and
runs a what-if (IBKR returns margin/commission if it would accept, empty if it rejects).

It places NO order. It changes NO configuration. It validates against whatever group
already exists on the gateway (discovered via fa_probe.py); creating our real per-tier
groups is a separate, deliberate step.

Connection note: like live_fill_test, it connects NON-read-only (what-if needs it) with
an explicit DU account so ib_async doesn't hang on the FA master's account-update stream.
Connecting non-read-only does not transmit anything; only a place() would, and we never
call it here.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\paperbot\\fa_block_test.py"
"""
from __future__ import annotations

import sys

from ib_async import IB

import config
import live_quotes
import order_router
from connections import clientids, ibkr_paper

# The existing group from fa_probe.py (a leftover test artifact). We only READ/what-if
# against it to prove the mechanism; we do not modify it.
TEST_GROUP = "test_group"
TEST_METHOD = "NetLiq"      # our intended method: split a block proportional to net liq
TEST_SYMBOL = "PDBC"        # cheap + liquid -> tiny notional for the what-if
TEST_SIDE = "BUY"
TEST_QTY = 3                # 1 per account in the 3-account test_group (indicative only)
# Subscribe to a DU account on connect so the master's account stream can't hang us.
SUBSCRIBE_ACCOUNT = "DU8922142"

_UNSET = 1.7976931348623157e+308   # IBKR "no value" sentinel for doubles


def _accepted(state) -> bool:
    """Accepted if IBKR returned a real margin/commission (not blank / the unset
    sentinel). A rejected or 'needs allocation' order comes back empty."""
    if state is None:
        return False
    for attr in ("initMarginChange", "maintMarginChange", "commission"):
        v = getattr(state, attr, None)
        if v in (None, ""):
            continue
        try:
            if float(v) != _UNSET:
                return True
        except (TypeError, ValueError):
            continue
    return False


def main() -> int:
    print("=" * 80)
    print("FA BLOCK (GROUP) ORDER - WHAT-IF VALIDATION ONLY (no transmission, no config change)")
    print("=" * 80)
    print(f"  group={TEST_GROUP}  method={TEST_METHOD}  probe order: {TEST_SIDE} "
          f"{TEST_QTY} {TEST_SYMBOL}")

    ib = IB()
    try:
        # Non-read-only (what-if needs it) but we ONLY what-if; nothing is transmitted.
        ib.connect(ibkr_paper.HOST, ibkr_paper.PAPER_PORT, clientId=clientids.get("paperbot_fa"),
                   readonly=False, timeout=15, account=SUBSCRIBE_ACCOUNT)
        accounts = ib.managedAccounts()
        print(f"\n  managed accounts: {accounts}")

        quote = live_quotes.fetch(ib, [TEST_SYMBOL]).get(TEST_SYMBOL)
        limit = (live_quotes.limit_price(TEST_SIDE, quote, style="marketable_limit")
                 if quote else None)
        if not limit:
            print("  ABORT: no usable live quote for the test symbol.")
            return 2
        print(f"  live quote -> limit {limit:,.2f}")

        built = order_router.build_fa_block(
            TEST_SYMBOL, TEST_SIDE, TEST_QTY, limit, TEST_GROUP, TEST_METHOD,
            as_of="fa_block_test", ib=ib)
        print(f"  built group order: faGroup={built.order.faGroup} "
              f"faMethod={built.order.faMethod} qty={built.order.totalQuantity:g} "
              f"(account field empty -> group order)  ref={built.order_ref}")

        states = order_router.what_if(ib, [built])
        ok = _accepted(states[0] if states else None)
        print("\n  RESULT:")
        if ok:
            print(f"    ACCEPTED — the FA master accepts an API-placed block order on "
                  f"'{TEST_GROUP}'.")
            print("    => Option B's block-execution mechanism works via the API. Next: "
                  "create our real per-tier groups.")
        else:
            print("    NOT ACCEPTED — the master rejected the group what-if. Needs "
                  "investigation (group/method/permissions) before block execution.")
        return 0 if ok else 3
    except Exception as exc:
        print(f"\n  ERROR during what-if: {exc}")
        return 1
    finally:
        ib.disconnect()
        print("\n  Done. WHAT-IF ONLY: nothing was transmitted, no config changed.")


if __name__ == "__main__":
    sys.exit(main())
