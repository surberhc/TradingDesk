"""
fa_probe.py — read-only dump of the gateway's FA allocation configuration.

Before we can place a BLOCK order (one order the FA master splits across a tier's
accounts at a single average price), the matching FA ALLOCATION GROUP has to exist on
the gateway. This probe asks IBKR — via requestFA — what allocation Groups / Profiles
/ Aliases are currently defined, so we know whether we must create them and what the
exact config XML looks like.

requestFA only READS configuration; it transmits no orders and changes nothing. (On
TWS/Gateway build 983+ the PROFILES type returns an error because profiles were merged
into groups — we catch that and move on.)

Run (gateway auto-starts if down):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\paperbot\\fa_probe.py"
"""
from __future__ import annotations

import sys

import config
from connections import clientids, ibkr_paper
from gateway_lock import GatewayBusySkip, gateway_lock

# TWS API FA data types. 1=GROUPS, 2=PROFILES (de-supported on 983+), 3=ALIASES.
FA_TYPES = {1: "GROUPS", 2: "PROFILES", 3: "ALIASES"}


def main() -> int:
    print("=" * 78)
    print("FA ALLOCATION-CONFIG PROBE - read-only (requestFA), no orders")
    print("=" * 78)

    try:
        with gateway_lock(purpose="fa_probe",
                          client_id=clientids.get("paperbot_fa"), on_busy="skip"):
            try:
                ib = ibkr_paper.connect("paperbot_fa", readonly=True, launch=True)
            except Exception as exc:
                print(f"\nCOULD NOT CONNECT: {exc}")
                return 1

            try:
                accounts = ib.managedAccounts()
                print(f"\nmanaged accounts: {accounts}")
                masters = [a for a in accounts if a.startswith("DF")]
                print(f"FA master(s): {masters or '(none - requestFA needs an advisor login)'}")

                for code, name in FA_TYPES.items():
                    print(f"\n--- FA {name} (type {code}) ---")
                    try:
                        xml = ib.requestFA(code)
                    except Exception as exc:
                        print(f"  requestFA error (expected for PROFILES on build 983+): {exc}")
                        continue
                    if not xml or not str(xml).strip():
                        print("  (empty - nothing defined)")
                    else:
                        print(str(xml).strip())

                print("\nDone. Read-only: nothing was transmitted, no config was changed.")
                return 0
            finally:
                ib.disconnect()
                print("Read-only session closed.")
    except GatewayBusySkip as busy:
        holder = busy.holder or {}
        print(f"\ngateway busy — held by {holder.get('purpose')} pid {holder.get('pid')} "
              f"clientId {holder.get('client_id')} since "
              f"{holder.get('acquired_at') or holder.get('acquired_ts')}; skipping this "
              f"probe. (Read-only; nothing read or transmitted.)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
