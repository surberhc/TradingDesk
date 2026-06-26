"""
ibkr_status.py — verify the connection's DATA ENTITLEMENT and account type.

The critical "no surprises" check: port 4002 is the PAPER gateway. Paper accounts
only get LIVE data if market data is explicitly shared from the live account;
otherwise they get DELAYED data — which would be useless for an intraday collector.
This tells us which we have, before we build anything on top of it.

marketDataType returned: 1=live(realtime)  2=frozen  3=delayed  4=delayed-frozen
"""

from __future__ import annotations

from datetime import datetime

from ib_async import IB, Stock

ib = IB()
print("now:", datetime.now().strftime("%Y-%m-%d %H:%M:%S %A"))
ib.connect("127.0.0.1", 4002, clientId=22, readonly=True, timeout=10)
accts = ib.managedAccounts()
print(f"accounts: {accts}  ({'PAPER' if any(a.startswith('DU') for a in accts) else 'LIVE/other'})")

spy = Stock("SPY", "SMART", "USD")
ib.qualifyContracts(spy)
for req in (1, 2):                       # 1=ask for live, 2=ask for frozen
    ib.reqMarketDataType(req)
    [t] = ib.reqTickers(spy)
    got = {1: "LIVE", 2: "frozen", 3: "DELAYED", 4: "delayed-frozen"}.get(t.marketDataType, t.marketDataType)
    print(f"  requested type {req} -> received {got:14s} "
          f"bid={t.bid} ask={t.ask} last={t.last} close={t.close}")
ib.disconnect()
print("done.")
