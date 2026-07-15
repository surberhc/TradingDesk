# Gateways — the three IBKR lanes (authoritative)

This desk runs **three separate IB Gateway instances**, one per lane. They are fully
symmetric in naming and never share an install dir, port, connection module, or
launch-lock. This file is the single source of truth for that map; if code and this
file ever disagree, fix whichever is wrong so they match.

| Lane | Port | Connection module | Install bat | Launch-lock env var | Launch-lock dir |
|------|------|-------------------|-------------|---------------------|-----------------|
| **Paper** | 4002 | `connections/connections/ibkr_paper.py` | `C:\IBC-Paper\StartGatewayPaper.bat` | `TRADINGDESK_PAPER_GATEWAY_LAUNCH_LOCK` | `C:\TradingDesk-Local\state\paper\` |
| **Live-Data** | 4001 | `connections/connections/ibkr_live_data.py` | `C:\IBC-Live-Data\StartGatewayLiveData.bat` | `TRADINGDESK_LIVE_DATA_GATEWAY_LAUNCH_LOCK` | `C:\TradingDesk-Local\state\live_data\` |
| **Live-Trade** | 4003 | `connections/connections/ibkr_live_trade.py` | `C:\IBC-Live-Trade\StartGatewayLiveTrade.bat` | `TRADINGDESK_LIVE_TRADE_GATEWAY_LAUNCH_LOCK` | `C:\TradingDesk-Local\state\live_trade\` |

> **Machine-side install dirs** (`C:\IBC-Paper`, `C:\IBC-Live-Data`, `C:\IBC-Live-Trade`)
> are set up separately by the user and **must match the `GATEWAY_BAT` constant** in each
> module. Renaming a bat path here without moving the install on disk will break launch.

## Purpose and safety posture per lane

### Paper (4002) — `ibkr_paper.py`
Simulated paper account **DU…141**. No real money, ever. The default connection is
read-only (`connect(readonly=True)`); the paperbot only flips to `readonly=False` when a
human deliberately arms order transmission through the review → arm → transmit gate. The
only login this Gateway can reach is the paper account, so live trading is not reachable
by accident. This is the lane essentially the whole desk uses (S0, S4, datacollector,
dailyreport, canslim gap-fill, reconciliation, etc.).

### Live-Data (4001) — `ibkr_live_data.py`
A **live** connection to a deliberately access-restricted personal account that IBKR
grants visibility into with **NO execution capability at the account-permission level**.
The module is **structurally read-only**: `connect()` has no `readonly` parameter at all,
always connects read-only, and the module never exposes, wraps, or re-exports any
order-placement method. Read-only is enforced twice over — by the account's IBKR
permissions and by the module's construction. Used for live market-data gathering only
(nightly forward-fill; S8 read paths historically).

### Live-Trade (4003) — `ibkr_live_trade.py`
A real, **FUNDED, transmit-CAPABLE** account — S8's zero-transmit live pilot. Unlike
Live-Data, this module is **not** structurally read-only: `connect()` exposes a real
`readonly` parameter. It **defaults to `True`** (fail-closed), and the only intended
caller that will ever pass `readonly=False` is the future S8 executor. During the pilot
the zero-transmit guarantee rests on two walls:

1. **Primary, load-bearing:** hardcoded `PILOT_MODE=True` in `livebot/s8_runner.py` —
   nothing transmits while it is set.
2. **Fail-safe backstop:** `ibkr_live_trade.connect(readonly=True)` default — the S8
   runner only ever reads (account summary + a 0DTE SPXW chain snapshot), so a bare
   connection cannot write.

The read-only default is a secondary control, **not** a substitute for `PILOT_MODE`.

## clientId assignments per lane

clientIds are the collision-proof registry in `connections/connections/clientids.py`;
this is which lane each currently belongs to (verified against the actual `connect()`
call sites, which are authoritative over any drifted registry comment).

**Paper (4002)**
- 1 `dailyreport_poller`, 9 `dailyreport_gateway_check`
- 21 `datacollector_probe`, 22 `datacollector_status`, 23 `datacollector_stream`,
  24 `datacollector_option_stream`, 25 `datacollector_forward`
- 30 `paperbot`, 31 `paperbot_accounts`, 32 `paperbot_recon`, 33 `paperbot_fa`,
  34 `paperbot_flatten`, 35 `paperbot_fa_block`, 36 `paperbot_fa_admin`,
  37 `paperbot_rebalance`, 38 `paperbot_rebalance_exec`, 39 `paperbot_arm_verify`,
  40 `paperbot_monitor`
- 41 `capabilities_introspect`, 42 `canslim_research_hist`, 43 `canslim_price_gapfill`
- 44 `paperbot_s4`, 45 `paperbot_s4_exec` (reserved)
- 46 `paperbot_nightly_monitor`, 47 `paperbot_morning_execute`
- 49 `paperbot_s8`, 50 `paperbot_s8_exec` (reserved, future paper-account transmission path)

**Live-Data (4001)**
- 48 `live_data_forward`
- 51 `paperbot_s8_livedata` (retired S8 live-data read path; still registered)
- 53 `live_data_order_verify` (one-off account-permission order-rejection probe)

**Live-Trade (4003)**
- 52 `dashboard_s8` (dashboard S8 tab read-only display re-marking)
- 54 `s8_live_pilot` (s8_runner live-cycle read: account summary + 0DTE chain)
