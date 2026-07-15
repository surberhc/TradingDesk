"""
connections — the one shared way to reach outside data and the broker.

  * ibkr_paper.py      — start + connect to the IBKR PAPER Gateway (port 4002)
  * ibkr_live_data.py  — start + connect to the LIVE-DATA Gateway (port 4001, read-only)
  * ibkr_live_trade.py — start + connect to the LIVE-TRADING Gateway (port 4003, funded)
  * tiingo.py          — pull Tiingo daily prices
  * clientids.py       — the master registry of who uses which IBKR clientId

See GATEWAYS.md for the authoritative three-lane Gateway map.

Everything (backtester, paperbot, datacollector, dailyreport) imports from here, so
the access logic lives in one place and connections never collide.
"""
