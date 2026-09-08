# Gateways — the IBKR lanes (authoritative)

**As of 2026-09-08 the desk runs ONE gateway: live-trade on port 4003.** The other two
lanes are retired but their code and installs remain on disk, so this file still maps all
three. This file is the single source of truth for that map; if code and this file ever
disagree, fix whichever is wrong so they match.

| Lane | Port | State |
|------|------|-------|
| **Live-Trade** | 4003 | **ACTIVE — the only lane.** Trading, S8/S0 pilots, and (since 2026-09-08) the nightly EOD option-chain pull. |
| Live-Data | 4001 | **RETIRED 2026-09-08.** All four scheduled tasks disabled; nothing launches it. See "Why 4001 was retired" below. |
| Paper | 4002 | **RETIRED 2026-08-24** on Andrew's call. Down on purpose — never flag it as an outage. |

## ONLY A HUMAN TAP STARTS THE GATEWAY (2026-09-08)

Starting 4003 sends an IBKR Mobile 2FA push. An unanswered push fails a login, and IBKR
counts those. So **no automated path may start the gateway**, and this is enforced in code,
not by convention:

- `ibkr_live_trade.ensure_gateway()` takes `allow_launch` and **defaults to False** — it
  reports the gateway down and returns, rather than launching. `connect(launch=True)` now
  raises an explanatory error instead of auto-starting.
- `s8_gateway_alert.handle_gateway_down()` takes `relaunch` and **defaults to False** — a
  mid-session drop is EMAILED, not relaunched. Its 5-minute alert dedup was never a cap;
  a gateway down through one 08:05–15:00 session could otherwise have fired dozens of
  pushes at someone who is travelling.
- The nightly EOD pull PROBES the port and, if down, skips and emails.
- The scheduled openers (`LiveTradeGatewayOpen_0800CT` / `_0815CT`) are disabled.

**The one way in:** `livebot/s8_desk_launch_link.py` emails one button; the tap runs
`run_live_trade_gateway_open.cmd` directly and never goes through `ensure_gateway()`, so
the gate above cannot block it. A tap means a human is holding the phone — the point.

This makes the down-alert email's premise sound again: it now says no push is coming, so
**an unexpected 2FA push should always be treated as suspicious.** Anything that reverses
one of these defaults must reckon with that.

## Why 4001 was retired (read before resurrecting it)

4001 was never an architectural separation. It was created 2026-07-10 to dodge a
market-data-subscription collision with another advisor on the PAPER gateway — and that
paper lane was itself retired 2026-08-24, so the reason expired. 4003 arrived separately
on 2026-07-15 because S8 needed real-time data that 4001's delayed-only account could not
give it; nobody revisited whether the EOD collector should follow.

Two lanes cost more than they bought: 4001 needed its OWN 2FA, was only ever launched in
an 08:05–12:00 window gated on 4003 already being up, and had to survive unattended until
17:30 — which it stopped doing. It died before the pull on 2026-09-02, 09-03 and 09-04,
the 09-05 run never fired at all, and four trading days of chain data were lost while the
GEX build kept reporting "ok" (an incremental build that finds nothing new still
succeeds). 4003 also serves the data better: an SPX/SPXW chain request returns clean,
where 4001 logged ~48,000 "not subscribed, displaying delayed" messages a night.

**What was given up, deliberately:** on 4001, read-only was STRUCTURAL — `connect()` had
no `readonly` parameter and the account had no execution capability. On 4003 it is an
explicit argument. `datacollector/` passes `readonly=True` at every call site and contains
no order call of any kind; that discipline is now the wall. Do not flip it.

| Lane | Port | Connection module | Install bat | Launch-lock env var | Launch-lock dir |
|------|------|-------------------|-------------|---------------------|-----------------|
| **Paper** | 4002 | `connections/connections/ibkr_paper.py` | `C:\IBC\StartGateway.bat` | `TRADINGDESK_PAPER_GATEWAY_LAUNCH_LOCK` | `C:\TradingDesk-Local\state\paper\` |
| **Live-Data** | 4001 | `connections/connections/ibkr_live_data.py` | `C:\IBC-Live-Data\StartGatewayLiveData.bat` | `TRADINGDESK_LIVE_DATA_GATEWAY_LAUNCH_LOCK` | `C:\TradingDesk-Local\state\live_data\` |
| **Live-Trade** | 4003 | `connections/connections/ibkr_live_trade.py` | `C:\IBC-Live-Trade\StartGatewayLiveTrade.bat` | `TRADINGDESK_LIVE_TRADE_GATEWAY_LAUNCH_LOCK` | `C:\TradingDesk-Local\state\live_trade\` |

## IBKR login per lane (distinct — never shared)

Each lane has its OWN IBKR login. They are never the same username on two ports — that is the whole reason there are three instances, and it is why all three can run at once without one booting another via `ExistingSessionDetectedAction`.

| Lane | Port | IBKR login username |
|------|------|---------------------|
| Paper | 4002 | `apsvpaper` |
| Live-Data | 4001 | `databot0001` |
| Live-Trade | 4003 | `asurber219` (advisor master `F6795549`, 356 managed accounts) |

> Passwords and 2FA are the user's and are never stored in the repo. This records only which login belongs to which lane.

> **CORRECTED 2026-09-08.** This table said the live-trade login was `apsv1816` from 2026-07-24 until today. It is `asurber219` — verified against `C:\IBC-Live-Trade\config.ini` (`IbLoginId=`) and the 2026-09-04 group-trading handoff. The wrong value was also carried in Claude's memory. Since only 4003 is still launched, this is the one login field that matters.

> **Machine-side install dirs** (`C:\IBC`, `C:\IBC-Live-Data`, `C:\IBC-Live-Trade`)
> are set up separately by the user and **must match the `GATEWAY_BAT` constant** in each
> module. Renaming a bat path here without moving the install on disk will break launch.

## Purpose and safety posture per lane

### Paper (4002) — `ibkr_paper.py` — RETIRED 2026-08-24
Down on purpose, on Andrew's call. Never report it as an outage. Historical description
follows. Simulated paper account **DU…141**. No real money, ever. The default connection is
read-only (`connect(readonly=True)`); the paperbot only flips to `readonly=False` when a
human deliberately arms order transmission through the review → arm → transmit gate. The
only login this Gateway can reach is the paper account, so live trading is not reachable
by accident. This WAS the lane essentially the whole desk used (S0, S4, datacollector,
dailyreport, canslim gap-fill, reconciliation) before the live lanes existed; that is no
longer true and this sentence is kept only to explain what the old code paths meant.

### Live-Data (4001) — `ibkr_live_data.py` — RETIRED 2026-09-08
Nothing launches this lane any more; its last consumer (the nightly EOD option-chain
pull) moved to 4003. The module and install remain on disk. Historical description
follows. A **live** connection to a deliberately access-restricted personal account that IBKR
grants visibility into with **NO execution capability at the account-permission level**.
The module is **structurally read-only**: `connect()` has no `readonly` parameter at all,
always connects read-only, and the module never exposes, wraps, or re-exports any
order-placement method. Read-only is enforced twice over — by the account's IBKR
permissions and by the module's construction. Was used for live market-data gathering only (nightly forward-fill; S8 read paths
historically). Note its account is DELAYED-DATA-ONLY, which is why S8 never stayed on it.

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

**Live-Data (4001) — retired, ids kept reserved so they are never reused**
- 48 `live_data_forward` (RETIRED — the nightly EOD collector now runs on 4003 as `live_trade_forward` 68)
- 51 `paperbot_s8_livedata` (retired S8 live-data read path; still registered)
- 53 `live_data_order_verify` (one-off account-permission order-rejection probe)

**Live-Trade (4003)**
- 52 `dashboard_s8` (dashboard S8 tab read-only display re-marking)
- 68 `live_trade_forward` (nightly EOD option-chain collector, moved here 2026-09-08; connects `readonly=True`, runs POST-CLOSE at 17:30 CT — after the 15:05 S8 teardown — so its `LINE_LIMIT=90` batches never contend with `s8_collector`/`s8_monitor` for the ~100-line account-wide budget)
- 54 `s8_live_pilot` (s8_runner live-cycle read: account summary + 0DTE chain)
- 55 `s8_monitor` (streaming exit-monitor read-only; runs concurrently with s8_live_pilot)
- 56 `s8_collector` (intraday ATM-band market collector read-only; runs concurrently with s8_live_pilot + s8_monitor; band bounded to a conservative line budget so the monitor's position-leg lines keep headroom)
- 57 `s0_live_pilot` (S0 morning-pilot read-only: reads the individual test account U5721712's NetLiq/positions/margin for morning_execute's WOULD-HAVE-TRANSMITTED reports; runs concurrently with the S8 consumers; PILOT_MODE + readonly are the zero-transmit walls)
- 58 `s0_live_exec` (RESERVED, not built: future transmit-capable S0 executor pinned to U5721712 — the gated real-money milestone)
