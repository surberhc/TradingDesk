"""
clientids.py — the MASTER IBKR clientId registry.

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
"""
from __future__ import annotations

PAPER_PORT = 4002
LIVE_DATA_PORT = 4001

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
