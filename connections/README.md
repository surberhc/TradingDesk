# connections

The one shared way for everything to reach outside data + the broker, so the access
logic lives in a single place and nothing collides.

Planned contents:
- `ibkr.py` — start + connect to the PAPER Gateway (reuses `C:\IBC\StartGateway.bat`)
- `tiingo.py` — pull Tiingo daily prices
- `clientids.py` — the master list of who uses which IBKR clientId (collision-proof)

clientId registry (draft): 1 = dailyreport poller · 9 = gateway health-check ·
21–24 = datacollector tests · **30 = paperbot** (reserved).
