"""
ibkr_probe.py — one-off capability test for the IBKR forward collector.

Reuses the RRG project's connection pattern (ib_async, IB Gateway 127.0.0.1:4002,
readonly, distinct clientId). Proves IBKR can hand us the SAME fields ThetaData
does for an option: bid/ask, model greeks (gamma/delta/IV), underlying price, and
open interest. If this works, the forward collector is feasible.

Run:  <venv python> options_warehouse/ibkr_probe.py
"""

from __future__ import annotations

import sys

try:
    from ib_async import IB, Stock, Option
except ImportError:
    sys.exit("ib_async not installed in this venv. Install:  pip install ib_async")


def main() -> None:
    ib = IB()
    try:
        ib.connect("127.0.0.1", 4002, clientId=21, readonly=True, timeout=10)
    except Exception as e:
        sys.exit(f"Could not reach IB Gateway on 127.0.0.1:4002 ({e}). "
                 "Is the Gateway running and logged in?")
    print(f"Connected: {ib.isConnected()}  | server {ib.client.serverVersion()}")

    ib.reqMarketDataType(2)  # frozen: last-available snapshot, works after hours

    spy = Stock("SPY", "SMART", "USD")
    ib.qualifyContracts(spy)
    [st] = ib.reqTickers(spy)
    spot = st.marketPrice() or st.close or st.last
    print(f"SPY spot ~ {spot}")

    chains = ib.reqSecDefOptParams(spy.symbol, "", spy.secType, spy.conId)
    chain = next((c for c in chains if c.exchange == "SMART"), chains[0])
    exp = sorted(chains and chain.expirations)[0]
    atm = min(chain.strikes, key=lambda s: abs(s - (spot or 0)))
    print(f"nearest expiry {exp} | {len(chain.strikes)} strikes | ATM ~ {atm}")

    opt = Option("SPY", exp, atm, "C", "SMART")
    ib.qualifyContracts(opt)
    # genericTickList 100=opt volume, 101=open interest; greeks arrive on the stream
    t = ib.reqMktData(opt, genericTickList="100,101", snapshot=False)
    ib.sleep(3)
    mg = t.modelGreeks
    print("--- OPTION SNAPSHOT (the fields the collector would store) ---")
    print(f"  {exp} {atm}C   bid={t.bid} ask={t.ask}")
    print(f"  IV={getattr(mg,'impliedVol',None)}  gamma={getattr(mg,'gamma',None)} "
          f"delta={getattr(mg,'delta',None)}  undPrice={getattr(mg,'undPrice',None)}")
    print(f"  open_interest(call)={t.callOpenInterest}  volume={t.volume}")
    ib.cancelMktData(opt)
    ib.disconnect()
    print("OK — disconnected.")


if __name__ == "__main__":
    main()
