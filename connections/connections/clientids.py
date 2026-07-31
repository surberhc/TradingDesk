"""
clientids.py — the MASTER IBKR clientId registry.

See connections/GATEWAYS.md for the authoritative three-lane Gateway map
(Paper 4002 / Live-Data 4001 / Live-Trade 4003: ports, modules, install bats,
launch-lock env vars, and which clientIds below belong to which lane).

Every component that connects to the Gateway takes its clientId from here, so two
things can never silently grab the same id and collide. To add a consumer, give it
the next free id below and import it (don't hard-code a number elsewhere).

PAPER ONLY: port 4002. Port 4001 (live) is used only by a separate, deliberately
access-restricted read-only market-data login (visibility into exactly one personal
account, no execution capability); paperbot execution remains exclusively on port 4002.

NOTE (2026-07-13): a paperbot consumer may now READ from port 4001 too -- s8_runner.py's
live-cycle connection ("paperbot_s8_livedata", 51) queries the live-data Gateway for its
account-summary margin gate and chain snapshot, never the paper Gateway. This does not
change the rule above: order TRANSMISSION (place/placeOrder) remains exclusively a port
4002 (paper) capability everywhere in this codebase; connections.ibkr_live_data has no
order-placement method at all, by construction.

NOTE (2026-07-15): port 4003 (LIVE_TRADE_PORT) now also exists -- a SEPARATE live-TRADING
Gateway instance for S8's zero-transmit live pilot (connections.ibkr_live_trade). Unlike the
port 4001 live-data login, this account is transmit-CAPABLE at the account-permission
level. Nothing transmits on it during the pilot: the primary wall is hardcoded
PILOT_MODE=True in livebot/s8_runner.py, backstopped by ibkr_live_trade.connect()'s
readonly=True default. Ports 4001 (live read-only data) and 4002 (paper) are unchanged.

NOTE (2026-07-27): S0 (adaptive_all_weather) now also READS on port 4003 -- "s0_live_pilot"
(57) is morning_execute_run.py's read-only live-pilot connection to the individual test
account U5721712 (S8 uses the trust account U14438624 under the same login). Order
TRANSMISSION on 4003 stays zero during both pilots: PILOT_MODE (hardcoded) in each runner
is the primary wall, ibkr_live_trade.connect(readonly=True) the fail-safe. "s0_live_exec"
(58) is reserved for the future, gated S0 transmit path -- not built.
"""
from __future__ import annotations

PAPER_PORT = 4002
LIVE_DATA_PORT = 4001
LIVE_TRADE_PORT = 4003

CLIENT_IDS = {
    "dailyreport_poller": 1,        # dailyreport: pull SPY+sector bars (rrg_poller)
    "dailyreport_gateway_check": 9,  # dailyreport: "is the Gateway up?" probe
    "datacollector_probe": 21,      # datacollector: option-data test
    "datacollector_status": 22,     # datacollector: data-entitlement check
    "datacollector_stream": 23,     # datacollector: equity stream test
    "datacollector_option_stream": 24,  # datacollector: option stream test
    "datacollector_forward": 25,    # datacollector: PRODUCTION forward EOD collector
    "paperbot": 30,                 # paperbot: the paper execution engine
    "paperbot_accounts": 31,        # paperbot: read-only multi-account discovery probe
    "paperbot_recon": 32,           # paperbot: read-only multi-account reconciliation report
    "paperbot_fa": 33,              # paperbot: read-only FA allocation-config probe (requestFA)
    "paperbot_flatten": 34,         # paperbot: one-shot flatten-to-zero sweep (own id so it never collides with the engine on 30)
    "paperbot_fa_block": 35,        # paperbot: one-shot FA block-order proof (test_group/NetLiq) — own id, never collides with 30
    "paperbot_fa_admin": 36,        # paperbot: FA config write (replaceFA group create/edit) — own id, serialize this shared write
    "paperbot_rebalance": 37,       # paperbot: multi-account rebalance RUNNER (read-only connect, build-only, arm-gated) — own id so it never collides with the engine on 30 or any read-only probe
    "paperbot_rebalance_exec": 38,  # paperbot: multi-account rebalance EXECUTOR (transmit-capable; connects readonly=False, pinned to a DU sub, writes ContractsOrShares via replaceFA, places blocks armed) — own id so it never collides with the engine on 30 or the read-only runner on 37
    "paperbot_arm_verify": 39,      # paperbot: arm/disarm VERIFICATION probe — connects readonly=False and inspects the gateway's API read-only state via the "Read-Only mode" rejection signal; NEVER transmits (orders it probes with are rejected at the API boundary or never sent). Own id so it never collides with the engine on 30 or the executor on 38
    "paperbot_monitor": 40,         # paperbot: account-cashflow MONITOR shell (account_monitor_run.py) — connects READ-ONLY, reads SettledCashByDate/TotalCashValue + today's fills (reqExecutions), persists per-account baselines, runs the pure decide() and PRINTS propose-only verdicts. Transmits nothing; no whatIfOrder. Own id so it never collides with any other consumer
    "capabilities_introspect": 41,  # connections: IBKR CAPABILITIES auto-updater (refresh_ibkr_capabilities.py) — connects READ-ONLY, calls reqScannerParameters() to snapshot the live scan-code/filter tag set, diffs against the prior snapshot, and keeps IBKR_CAPABILITIES.md honest. Transmits nothing; monthly schedule. Own id so it never collides with any other consumer
    "canslim_research_hist": 42,    # canslim research: one-off READ-ONLY historical daily-bar puller for the hard-stop counterfactual (reqHistoricalData TRADES, ~90d pre-entry through sell for ~120 tickers). Transmits nothing. Own id so it never collides with any desk task
    "canslim_price_gapfill": 43,    # canslim full-market selection Phase 1: READ-ONLY IBKR gap-fill puller for the survivorship-free daily price/volume warehouse (reqHistoricalData TRADES, 2010-2026, ONLY for symbols Tiingo/Stooq missed). Transmits nothing; yields to the Gateway mutex + AccountMonitorDaily. Own id so it never collides with any desk task
    "paperbot_s4": 44,              # paperbot S4 (SPX vol-control fund): single-account REVIEW-ONLY daily runner (s4_rebalance_run.py / s4_daily_run.py) — connects READ-ONLY, reads NAV/positions/account-summary, sizes the {SPY,BIL} vol-control book (real margin leverage allowed up to the profile cap), runs the S4 risk guard + margin preflight, builds orders and place(armed=False). Transmits nothing. Own id so it never collides with the S0 engine (30) or the multi-account rebalance runner (37)
    "paperbot_s4_exec": 45,         # paperbot S4 EXECUTOR (RESERVED, not yet built): the future transmit-capable S4 single-account executor (connects readonly=False, pinned to the S4 DU sub, places the vol-control orders armed). Own id reserved now so it never collides with anything when it is built
    "paperbot_nightly_monitor": 46,  # paperbot: nightly bounded-retry monitor+stage runner (nightly_monitor_run.py) — connects READ-ONLY (~9:15 PM CT, after EodReport), runs the same drift/cashflow check as account_monitor_run.py, computes S0's current target, and on a rebalance/signal day builds+guard-checks the trade list and STAGES it to C:\TradingDesk-Local\pending_trades for the morning executor to pick up. Transmits nothing; own id so it never collides with the read-only monitor (40) or any rebalance path
    "paperbot_morning_execute": 47,  # paperbot: morning execution runner (morning_execute_run.py, ~8:50 AM CT) — no-ops with ZERO gateway touch on a day with no staged file; otherwise bounded-retry connects and either (PILOT_MODE=True, default) logs/emails "WOULD HAVE TRANSMITTED" without transmitting, or (PILOT_MODE=False, future) arms + executes the staged, guard-approved trade list via the existing laddered router. Own id so it never collides with the manual rebalance_execute path (38) or anything else
    "live_data_forward": 48,        # datacollector: read-only nightly forward-fill against the separate, restricted live-side Gateway (port 4001, distinct instance from paper 4002); never transmits; never touched by paperbot
    "paperbot_s8": 49,               # paperbot S8 (British IC + B2 long-leg auto-close): scheduled entry-point runner (s8_runner.py) -- zero-gateway-contact due-check fast path; when a template's entry grid slot is due, connects READ-ONLY, snapshots the live 0DTE SPXW chain, picks the spread, runs the S8 margin preflight, builds the entry+stop-parent+B2-child order group, and (PILOT_MODE=True, hardcoded) logs/emails "WOULD HAVE TRANSMITTED" without transmitting. Own id so it never collides with the S4 runner (44) or anything else
    "paperbot_s8_exec": 50,          # paperbot S8 EXECUTOR (RESERVED, not yet built): the future transmit-capable S8 executor (connects readonly=False, pinned to the S8 DU sub once s8_config.ACCOUNT is decided, transmits the entry/stop/B2 order group armed). Own id reserved now so it never collides with anything when it is built, mirroring the S4 44/45 pattern. NOTE (2026-07-13): s8_runner.py's live-cycle connection no longer uses "paperbot_s8" (49) at all -- see "paperbot_s8_livedata" (51) below -- but 49/50 stay registered/reserved for a future real-transmission path against the paper account
    "paperbot_s8_livedata": 51,      # paperbot S8 (British IC + B2): s8_runner.py's ACTUAL live-cycle connection, decided 2026-07-13 -- connects READ-ONLY (structurally, no override possible) to the separate live-side data Gateway (connections.ibkr_live_data, port 4001, NOT the paper Gateway on port 4002) for both the account-summary margin-preflight read and the 0DTE SPXW chain snapshot. Distinct from live_data_forward (48, datacollector's own live-data consumer) so the two never collide on the same live-data Gateway connection. PILOT_MODE (paperbot/s8_runner.py) remains the primary non-transmission control; this connection's hardcoded read-only-ness is a second, independent backstop
    "dashboard_s8": 52,              # dashboard/app.py's S8 monitor tab -- READ-ONLY (connects readonly=True) to the live-TRADING Gateway (port 4003, connections.ibkr_live_trade), launch=False (never boots the Gateway from a passive auto-refreshing web page), short timeout. Targeted per-pick quote pulls (2 legs at a time) + accountSummary() read, purely for DISPLAY re-marking of today's logged picks -- never builds or transmits an order. Own id so it never collides with the runner's live-cycle connection s8_live_pilot (54) if both are polling port 4003 at the same moment
    "live_data_order_verify": 53,    # connections: read-only-gateway ORDER-REJECTION verification probe (check_live_data_order_reject.py) -- connects readonly=False directly (bypassing ibkr_live_data.connect()'s hardcoded readonly), with Andrew's explicit informed authorization, specifically to test whether IBKR itself rejects order transmission at the ACCOUNT-permission level on this live-data-only account. Zero-transmission: only ever calls raw client.cancelOrder() on a fabricated, never-placed orderId (mirrors paperbot/arming.py's probe_api_readonly pattern) -- no order is ever sent. Own id so it never collides with any read-only live-data consumer (48/51/52)
    "s8_live_pilot": 54,             # livebot/s8_runner.py's live-cycle connection to the NEW live-TRADING Gateway (connections.ibkr_live_trade, port 4003, transmit-capable account) -- connects readonly=True during the PILOT (reads accountSummary + 0DTE SPXW chain only, never transmits; PILOT_MODE in s8_runner is the primary wall). Distinct id from the retired live-DATA path paperbot_s8_livedata (51, port 4001) and from the reserved future executor paperbot_s8_exec (50)
    "s8_monitor": 55,                # livebot/s8_monitor.py's live exit-monitor SERVICE connection to the live-TRADING Gateway (connections.ibkr_live_trade, port 4003) -- the streaming exit-monitor's READ-ONLY consumer (connects readonly=True; only ever reqMktData/cancelMktData/reads, no order path anywhere). Distinct from s8_live_pilot (54, the entry runner) so the monitor and the entry runner can both poll port 4003 concurrently without a clientId collision. Zero-transmit like the rest of the pilot: PILOT_MODE upstream + this connection's readonly are the walls
    "s8_collector": 56,              # livebot/s8_collector.py's intraday ATM-BAND MARKET COLLECTOR connection to the live-TRADING Gateway (connections.ibkr_live_trade, port 4003) -- the periodic context feed's READ-ONLY consumer (connects readonly=True; only ever reqMktData/cancelMktData/reads, no order path anywhere). Streams a bounded ATM band of SPXW 0DTE strikes + SPX + VIX with model greeks and harvests a market-context snapshot at a configurable cadence. Distinct from s8_live_pilot (54) and s8_monitor (55) so the entry runner, exit monitor, and collector can all poll port 4003 concurrently without a clientId collision. Band size is bounded to a conservative market-data-line budget (Risk #1) so the exit monitor's position-leg lines always have headroom; on an IBKR line-limit error the band shrinks rather than crashing. Zero-transmit: PILOT_MODE upstream + this connection's readonly are the walls
    "s0_live_pilot": 57,             # paperbot S0 (adaptive_all_weather): morning-pilot READ-ONLY connection to the live-TRADING Gateway (connections.ibkr_live_trade, port 4003) to read the REAL individual test account U5721712's NetLiq/positions/margin, so morning_execute_run.py's "WOULD HAVE TRANSMITTED" pilot reports (conductor #3/#41) reflect genuine account state. Connects readonly=True (see paperbot/s0_live.py); PILOT_MODE in morning_execute_run.py is the primary zero-transmit wall. Distinct from every S8 consumer on 4003 (s8_live_pilot 54 / s8_monitor 55 / s8_collector 56) so the S0 pilot and the S8 pilot/monitor/collector never collide on the shared gateway
    "s0_live_exec": 58,              # paperbot S0 EXECUTOR (RESERVED, not yet built): the future transmit-capable S0 live executor (would connect readonly=False, pinned to U5721712, to transmit the staged S0 rebalance armed). Own id reserved now so it never collides when built, mirroring the S4 44/45 and S8 49/50 reserved-exec pattern. Transmitting real money on this account remains the deliberate, gated milestone behind PILOT_MODE — nothing transmits today
    "cp_arm_probe": 59,              # dashboard Control Plane (dashboard/desk/gateway_arm_probe.py): READ-ONLY, ZERO-TRANSMISSION probe of the port-4003 live-trade Gateway's Read-Only-API (armed) state. Connects readonly=True and reuses s0_live_deploy._probe_gateway_readonly's fabricated-order-cancel technique (cancels a never-placed orderId, reads code 321/"read-only mode" => not armed vs 10147/10148/"not found" => armed) — never places or transmits an order. Shelled out as a subprocess by Control Plane Step 2 to SHOW armed/not-armed/unreachable. Own id, distinct from every S8 consumer on 4003 (s8_live_pilot 54 / s8_monitor 55 / s8_collector 56) and from the S0 pilot (s0_live_pilot 57) / reserved executor (s0_live_exec 58), so the arm-state check can run concurrently with any of them without a clientId collision
    "s0_month_end_snapshot": 60,     # dailyreport S0 month-end CLOSE-TIME HOLDINGS SNAPSHOT (dailyreport/s0_month_end_snapshot.py, ~2:50pm CT on the month-end signal day, before the ~3:05pm gateway teardown): connects READ-ONLY (ibkr_live_trade.connect readonly=True) to the live-TRADING Gateway (port 4003), reads the S0 month-end account's positions + NetLiquidation, and writes them to an off-repo JSON so the evening verdict job (s0_month_end_notice.py, after the 7pm Tiingo pull) can produce an EXACT TRADE/NO-trade heads-up. INFORMATIONAL + READ-ONLY: builds no order, calls no order-placement method, transmits nothing — the readonly=True session flag is the wall. Own id, distinct from every other 4003 consumer (s8_live_pilot 54 / s8_monitor 55 / s8_collector 56 / s0_live_pilot 57 / s0_live_exec 58 / cp_arm_probe 59) so the snapshot can run concurrently without a clientId collision
    "s0_cash_deploy_check": 61,      # dailyreport S0 IDLE-CASH DEPLOY CHECK (dailyreport/s0_cash_deploy_check.py): connects READ-ONLY (ibkr_live_trade.connect readonly=True) to the live-TRADING Gateway (port 4003), reads the S0 account's NetLiquidation + TotalCashValue, and — when free cash held ABOVE the standing cash buffer exceeds an operational fraction of NAV — posts a propose-and-arm "idle cash, consider deploying" notice to the in-app Action Center (dashboard/desk/action_center.py). INFORMATIONAL + READ-ONLY: builds no order, calls no order-placement method, transmits nothing — the readonly=True session flag is the wall; the notice only points the operator at the Control Plane, it never trades. Own id, distinct from every other 4003 consumer (… s0_month_end_snapshot 60) so it can run concurrently without a clientId collision
}

# Ids seen in old stray scripts — DO NOT reuse without checking; left here as history.
RETIRED = {2: "old rrg_backfill.py", 7: "old delayed_test.py"}


def get(name: str) -> int:
    """Return the registered clientId for a consumer, or raise if it's unknown."""
    try:
        return CLIENT_IDS[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown clientId consumer {name!r}; add it to connections.clientids.CLIENT_IDS"
        ) from exc
