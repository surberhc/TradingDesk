"""
check_account.py — first contact with the IBKR PAPER account. READ-ONLY.

What it does, and ONLY this:
  1. Connects to the PAPER gateway (port 4002) via connections.ibkr_paper on a READ-ONLY
     session — read-only physically cannot transmit an order. Launches the Gateway
     (IBController auto-login) if it isn't already up.
  2. Confirms the account ending in '141' (config.ACCOUNT_SUFFIX). If it can't find
     it, it prints what it DID find and stops — it will not guess.
  3. Confirms the account is a PAPER account (number starts with 'DU').
  4. Prints the paper cash / net liquidation value and current positions.
  5. Disconnects.

It places no orders, forms no orders, and changes nothing.

Run (from anywhere):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\paperbot\\check_account.py"
"""
from __future__ import annotations

import sys

import config
from connections import clientids, ibkr_paper


def _tag(summary, account: str, tag: str) -> str:
    """Pull one account-summary value (e.g. NetLiquidation) for a given account."""
    for row in summary:
        if row.account == account and row.tag == tag:
            return f"{float(row.value):,.2f} {row.currency}"
    return "(not reported)"


def main() -> int:
    print("=" * 70)
    print("PAPER ACCOUNT CHECK - read-only, no orders")
    print(f"Connecting to PAPER gateway {ibkr_paper.HOST}:{ibkr_paper.PAPER_PORT} "
          f"(clientId={clientids.get('paperbot')}, readonly=True)")
    print("=" * 70)

    try:
        # launch=True -> start the Gateway (IBC auto-login) if it isn't up yet.
        ib = ibkr_paper.connect("paperbot", readonly=True, launch=True)
    except Exception as exc:
        print("\nCOULD NOT CONNECT.")
        print(f"  reason: {exc}")
        print("\n  -> Is IB Gateway able to start, logged into the PAPER account?")
        print(f"  -> Is the API enabled with socket port {ibkr_paper.PAPER_PORT}?")
        return 1

    try:
        accounts = ib.managedAccounts()
        print(f"\nAccounts visible on this gateway: {accounts}")

        # Find the account ending in our required suffix.
        matches = [a for a in accounts if a.endswith(config.ACCOUNT_SUFFIX)]
        if not matches:
            print(f"\nSTOP: no account ends in '{config.ACCOUNT_SUFFIX}'.")
            print("  The engine will not act on an account it wasn't told to use.")
            print("  Check the gateway login, or update ACCOUNT_SUFFIX in config.py.")
            return 2
        if len(matches) > 1:
            print(f"\nSTOP: more than one account ends in '{config.ACCOUNT_SUFFIX}': {matches}")
            return 2

        account = matches[0]
        # Paper accounts on the paper gateway begin with 'D': DU = paper trading
        # account, DF = FA paper master. Live accounts (out of scope) start with
        # 'U'/'F' (no leading D). DF8922141 is our main/reference paper account.
        is_paper = account.startswith(("DU", "DF"))
        acct_kind = ("FA paper master" if account.startswith("DF")
                     else "paper trading" if account.startswith("DU")
                     else "NOT A PAPER ACCOUNT")
        print(f"\nUSING ACCOUNT: {account}   [{acct_kind}]")
        if not is_paper:
            print("STOP: the matched account is not a paper (DU/DF) account. Halting.")
            return 2

        # --- Balances (read-only) ---
        summary = ib.accountSummary(account)
        print("\n--- Balances ---")
        for tag in ("NetLiquidation", "TotalCashValue", "AvailableFunds", "BuyingPower"):
            print(f"  {tag:16s}: {_tag(summary, account, tag)}")

        # --- Positions (read-only) ---
        positions = [p for p in ib.positions(account) if p.position != 0]
        print(f"\n--- Current positions ({len(positions)}) ---")
        if not positions:
            print("  (flat - no open positions)")
        else:
            for p in positions:
                c = p.contract
                print(f"  {c.symbol:8s} {c.secType:5s}  qty={p.position:>12,.2f}  "
                      f"avgCost={p.avgCost:,.4f}")

        print("\nDone. Nothing was transmitted. Read-only session closing.")
        return 0
    finally:
        ib.disconnect()


if __name__ == "__main__":
    sys.exit(main())
