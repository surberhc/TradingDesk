# connections

The one shared way for everything to reach outside data + the broker, so the access
logic lives in a single place and nothing collides.

> **Gateways:** see [`GATEWAYS.md`](GATEWAYS.md) for the authoritative three-lane
> map (Paper 4002 / Live-Data 4001 / Live-Trade 4003 — ports, modules, install
> bats, launch-lock env vars, safety posture, and clientId assignments).

Planned contents:
- `ibkr_paper.py` — start + connect to the PAPER Gateway (reuses `C:\IBC-Paper\StartGatewayPaper.bat`)
- `tiingo.py` — pull Tiingo daily prices
- `clientids.py` — the master list of who uses which IBKR clientId (collision-proof)

clientId registry (draft): 1 = dailyreport poller · 9 = gateway health-check ·
21–24 = datacollector tests · **30 = paperbot** (reserved).

## Live-data Gateway (`ibkr_live_data.py`)

A second, independent connection module for read-only-only market-data gathering
against a separate, deliberately access-restricted personal live IBKR account
(port 4001, `LIVE_DATA_PORT`) — used by the new nightly forward-fill job.

Differs from `ibkr_paper.py`: its own Gateway instance and install dir
(`C:\IBC-Live-Data`, not `C:\IBC-Paper`), and no `readonly` override — every
connection it makes is hardcoded read-only, and the module never exposes an
order-placement method. Read-only is enforced twice over: by the account's own
IBKR permissions, and by this module's construction.

Paperbot execution never touches this module — it stays exclusively on the
paper Gateway (`ibkr_paper.py`, port 4002).

## Live-trade Gateway (`ibkr_live_trade.py`)

A third, independent connection module for the real, FUNDED, transmit-CAPABLE
live-trading account (port 4003, `LIVE_TRADE_PORT`) — S8's zero-transmit live
pilot. Unlike `ibkr_live_data.py`, `connect()` DOES expose a real `readonly`
parameter (it defaults to `True`, fail-closed): this account can transmit at the
account-permission level, so structural read-only is not available here. The
zero-transmit guarantee during the pilot rests on hardcoded `PILOT_MODE=True` in
`livebot/s8_runner.py` (primary wall) plus the read-only default (backstop). Own
Gateway instance and install dir (`C:\IBC-Live-Trade`).
