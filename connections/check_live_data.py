"""
check_live_data.py — first-contact smoke test for the LIVE-DATA gateway. READ-ONLY.

What it does, and ONLY this:
  1. Connects to the live-data gateway (port 4001) via connections.ibkr_live_data,
     clientId "live_data_forward". Structurally read-only -- connect() has no
     readonly override, so this can never transmit an order.
  2. Confirms exactly one account is visible, and that it is a live ('U'-prefixed)
     account, not paper (DU/DF).
  3. Confirms the market-data type: lets ib_async's implicit default go out first
     (expected to be rejected, since this account has no live-data entitlement),
     then explicitly requests delayed (type 3) and pulls one real quote.
  4. Disconnects cleanly.

It places no orders, forms no orders, and changes nothing. This does NOT cover the
order-rejection half of the smoke test (deliberately attempting/cancelling an order
to confirm IBKR itself rejects it at the account-permission level) -- that needs a
readonly=False connection and is a separate, deliberately-gated step.

Run (from anywhere):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\connections\\check_live_data.py"
"""
from __future__ import annotations

import sys

from ib_async import Stock

from connections import clientids, ibkr_live_data


def main() -> int:
    print("=" * 70)
    print("LIVE-DATA GATEWAY CHECK - read-only, no orders")
    print(f"Connecting to live-data gateway {ibkr_live_data.HOST}:{ibkr_live_data.LIVE_DATA_PORT} "
          f"(clientId={clientids.get('live_data_forward')}, structurally read-only)")
    print("=" * 70)

    try:
        # launch=False: this gateway is expected to already be up; don't auto-start it.
        ib = ibkr_live_data.connect("live_data_forward", launch=False)
    except Exception as exc:
        print("\nCOULD NOT CONNECT.")
        print(f"  reason: {exc}")
        print("\n  -> Is IBC-Live up and logged into the live account?")
        print(f"  -> Is the API enabled with socket port {ibkr_live_data.LIVE_DATA_PORT}?")
        return 1

    try:
        accounts = ib.managedAccounts()
        print(f"\nAccounts visible on this gateway: {accounts}")

        if len(accounts) != 1:
            print(f"\nSTOP: expected exactly one visible account, got {len(accounts)}: {accounts}")
            return 2

        account = accounts[0]
        is_live = account.startswith("U")
        acct_kind = "live account" if is_live else "NOT A LIVE ACCOUNT (unexpected)"
        print(f"\nUSING ACCOUNT: {account}   [{acct_kind}]")
        if not is_live:
            print("STOP: matched account is not a live ('U'-prefixed) account. Halting.")
            return 2

        # --- Market data type: confirm no implicit live entitlement leaks through ---
        spy = Stock("SPY", "SMART", "USD")
        ib.qualifyContracts(spy)

        print("\n--- Market data type check ---")
        print("  Requesting a quote WITHOUT setting market data type (implicit default)...")
        ticker = ib.reqMktData(spy, "", False, False)
        ib.sleep(2)
        implicit_last = ticker.last
        ib.cancelMktData(spy)
        print(f"  implicit request last={implicit_last!r} "
              "(NaN/None here is expected -- no live entitlement on this account)")

        print("  Explicitly requesting DELAYED (type 3) market data...")
        ib.reqMarketDataType(3)
        ticker = ib.reqMktData(spy, "", False, False)
        ib.sleep(2)
        delayed_last = ticker.last
        ib.cancelMktData(spy)
        print(f"  delayed request last={delayed_last!r}")

        if delayed_last != delayed_last or delayed_last is None:  # NaN check
            print("\nSTOP: no delayed quote came back. Market data entitlement not confirmed.")
            return 3

        print("\nDone. Nothing was transmitted. Read-only session closing.")
        print("NOTE: order-rejection half of the smoke test NOT run here -- separate step.")
        return 0
    finally:
        ib.disconnect()


if __name__ == "__main__":
    sys.exit(main())
