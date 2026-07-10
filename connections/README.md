# connections

The one shared way for everything to reach outside data + the broker, so the access
logic lives in a single place and nothing collides.

Planned contents:
- `ibkr.py` — start + connect to the PAPER Gateway (reuses `C:\IBC\StartGateway.bat`)
- `tiingo.py` — pull Tiingo daily prices
- `clientids.py` — the master list of who uses which IBKR clientId (collision-proof)

clientId registry (draft): 1 = dailyreport poller · 9 = gateway health-check ·
21–24 = datacollector tests · **30 = paperbot** (reserved).

## Live-data Gateway (`ibkr_live_data.py`)

A second, independent connection module for read-only-only market-data gathering
against a separate, deliberately access-restricted personal live IBKR account
(port 4001, `LIVE_DATA_PORT`) — used by the new nightly forward-fill job.

Differs from `ibkr.py`: its own Gateway instance and install dir (`C:\IBC-Live`,
not `C:\IBC`), and no `readonly` override — every connection it makes is
hardcoded read-only, and the module never exposes an order-placement method.
Read-only is enforced twice over: by the account's own IBKR permissions, and by
this module's construction.

Paperbot execution never touches this module — it stays exclusively on the
paper Gateway (`ibkr.py`, port 4002).
