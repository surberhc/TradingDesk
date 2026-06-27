"""
clientids.py — the MASTER IBKR clientId registry.

Every component that connects to the Gateway takes its clientId from here, so two
things can never silently grab the same id and collide. To add a consumer, give it
the next free id below and import it (don't hard-code a number elsewhere).

PAPER ONLY: port 4002. The real-money port (4001) is intentionally absent — this
project does not connect to a real-money account.
"""
from __future__ import annotations

PAPER_PORT = 4002

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
