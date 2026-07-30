"""roster.py — the human-blessed EXECUTION ROSTER accessor (Control Plane multi-account,
conductor #64/#66, spec docs/PRODUCTION_REBALANCE_CONTROL_PLANE.md §6/§7).

The roster is the AUTHORITATIVE execution allow-list — the set of accounts the desk is
permitted to act on. It is the account wall's source of truth (safe_execute.account_wall_ok):
any account not in the roster is refused, no matter what a planner produced. That
independence is the whole point — the wall must never "allow whatever the planner output" —
so the roster is derived from a human-blessed config, NOT from any plan.

This is a PURE, read-only accessor. It builds no order, contacts no broker, transmits
nothing.
"""
from __future__ import annotations

import config


def enrolled_roster() -> list[str]:
    """The human-blessed execution roster: the authoritative execution allow-list.

    For THIS increment it is exactly the enrolled accounts in `config.ENROLLMENT` (the
    account -> strategy-version map Andrew maintains), returned SORTED and de-duped. It is
    INDEPENDENT of any planner output on purpose — the account wall must gate on who we are
    blessed to trade, never on "whatever the planner produced".

    A later increment may INTERSECT this with live CRM sleeve assignments and
    accounts.discover()'s funded/visible reality (so an un-funded or un-assigned account
    can't be acted on even if it lingers in the config). Until then, the config map is the
    single source of truth. Pure: reads config only, contacts nothing."""
    return sorted(set(config.ENROLLMENT))
