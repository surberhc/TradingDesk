"""
connections — the one shared way to reach outside data and the broker.

  * ibkr.py      — start + connect to the IBKR PAPER Gateway (port 4002)
  * tiingo.py    — pull Tiingo daily prices
  * clientids.py — the master registry of who uses which IBKR clientId

Everything (backtester, paperbot, datacollector, dailyreport) imports from here, so
the access logic lives in one place and connections never collide.
"""
