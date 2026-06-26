"""
ibkr_stream_test.py — prove LIVE streaming holds continuously over the connection.

Subscribes to live (type 1) streaming market data and samples it every few seconds,
counting updates and watching for disconnects / line-limit errors. Run it now on
stocks (extended-hours ticks) to prove the stream mechanism + connection stability;
re-point it at the option slice during RTH to prove option quotes/OI stream all day.

Usage:  <venv python> ibkr_stream_test.py [SECONDS] [SYM ...]
"""

from __future__ import annotations

import sys
from datetime import datetime

from ib_async import IB, Stock

secs = int(sys.argv[1]) if len(sys.argv) > 1 else 25
syms = sys.argv[2:] or ["SPY", "QQQ"]

errors: list[str] = []
ib = IB()
ib.errorEvent += lambda reqId, code, msg, c: errors.append(f"[{code}] {msg}")
ib.disconnectedEvent += lambda: errors.append("*** DISCONNECTED ***")

ib.connect("127.0.0.1", 4002, clientId=23, readonly=True, timeout=10)
ib.reqMarketDataType(1)

tickers = []
for s in syms:
    c = Stock(s, "SMART", "USD")
    ib.qualifyContracts(c)
    tickers.append(ib.reqMktData(c, "", False, False))

print(f"streaming {syms} live for ~{secs}s, sampling every 5s ...")
last = {s: None for s in syms}
updates = {s: 0 for s in syms}
for _ in range(max(1, secs // 5)):
    ib.sleep(5)
    stamp = datetime.now().strftime("%H:%M:%S")
    for t in tickers:
        s = t.contract.symbol
        snap = (t.bid, t.ask, t.last)
        if snap != last[s]:
            updates[s] += 1
        last[s] = snap
        print(f"  {stamp}  {s:4s} bid={t.bid} ask={t.ask} last={t.last}")

ib.disconnect()
print("\nupdate counts (changes seen):", updates)
print("errors/disconnects:", errors if errors else "none")
