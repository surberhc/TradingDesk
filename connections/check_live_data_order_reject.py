"""
check_live_data_order_reject.py — ORDER-REJECTION half of the live-data gateway
smoke test. Deliberately connects WRITE-CAPABLE (readonly=False) but ZERO-
TRANSMISSION, with Andrew's explicit informed authorization to bypass the
hardcoded-readonly backstop for this verification only.

Context: connections.ibkr_live_data.connect() hardcodes readonly=True and has no
order-placement method at all -- that's backstop #2 for normal use. This script
exists ONLY to verify backstop #1 independently: that the live account behind port
4001 itself has no execution capability at the IBKR account-permission level,
regardless of what the API connection allows. It deliberately bypasses
ibkr_live_data.connect() and opens its own raw ib_async.IB() session with
readonly=False, directly against port 4001, using its own registered clientId
("live_data_order_verify", 53) so it never collides with any other live-data
consumer.

ZERO-TRANSMISSION GUARANTEE (mirrors paperbot/arming.py:probe_api_readonly):
  This never places, modifies, or rests an order. It calls the RAW
  `ib.client.cancelOrder(oid, "")` on a fabricated orderId that was never placed by
  anyone. There is no such order, so nothing can be cancelled and nothing was ever
  transmitted -- the server can only ever reply with a rejection of one kind or
  another. Every errorEvent callback received in the response window is captured and
  printed verbatim so a human can classify the specific rejection reason (this
  account's exact restriction wording has never been observed before).

Run (from anywhere):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\connections\\check_live_data_order_reject.py"
"""
from __future__ import annotations

import sys
import time

from ib_async import IB

from connections import clientids, ibkr_live_data

_CLIENT_ID = clientids.get("live_data_order_verify")


def main() -> int:
    print("=" * 70)
    print("LIVE-DATA GATEWAY ORDER-REJECTION CHECK")
    print("Connects WRITE-CAPABLE (readonly=False) but transmits NOTHING --")
    print("only cancels a fabricated, never-placed orderId (zero-transmission).")
    print(f"Connecting to {ibkr_live_data.HOST}:{ibkr_live_data.LIVE_DATA_PORT} "
          f"(clientId={_CLIENT_ID})")
    print("=" * 70)

    ib = IB()
    events = []

    def on_error(reqId, errorCode, errorString, *_):
        events.append((reqId, errorCode, errorString))
        print(f"  errorEvent: reqId={reqId} code={errorCode} msg={errorString!r}")

    try:
        ib.connect(ibkr_live_data.HOST, ibkr_live_data.LIVE_DATA_PORT,
                   clientId=_CLIENT_ID, readonly=False, timeout=15)
    except Exception as exc:
        print("\nCOULD NOT CONNECT.")
        print(f"  reason: {exc}")
        return 1

    try:
        accounts = ib.managedAccounts()
        print(f"\nAccounts visible: {accounts}")

        ib.errorEvent += on_error
        oid = ib.client.getReqId()
        print(f"\nCalling raw client.cancelOrder({oid}, '') on a never-placed orderId...")
        ib.client.cancelOrder(oid, "")

        deadline = time.time() + 15
        while time.time() < deadline:
            ib.sleep(0.2)
            if events:
                break

        print(f"\n{len(events)} errorEvent callback(s) captured within the window.")
        if not events:
            print("STOP: no decisive response within timeout. UNVERIFIED -- do not "
                  "conclude anything about order-rejection from this run.")
            return 2

        print("\nDone. No order was ever placed, modified, or transmitted.")
        print("Review the errorEvent(s) above to classify the account's rejection reason.")
        return 0
    finally:
        try:
            ib.errorEvent -= on_error
        except Exception:
            pass
        ib.disconnect()


if __name__ == "__main__":
    sys.exit(main())
