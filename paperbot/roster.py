"""roster.py — the human-blessed EXECUTION ROSTER accessor (Control Plane multi-account,
conductor #64/#66, spec docs/PRODUCTION_REBALANCE_CONTROL_PLANE.md §6/§7).

The roster is the AUTHORITATIVE execution allow-list — the set of accounts the desk is
permitted to act on. It is the account wall's source of truth (safe_execute.account_wall_ok):
any account not in the roster is refused, no matter what a planner produced. That
independence is the whole point — the wall must never "allow whatever the planner output" —
so the roster is derived from a human-blessed source, NOT from any plan.

This is a PURE, read-only accessor. It builds no order, contacts no broker, transmits
nothing.

SOURCE OF TRUTH — CRM ROSTER, with a config FALLBACK (P1 read-seam)
------------------------------------------------------------------
The blessed roster now comes FIRST from the CRM's read-only view ``v_tradingdesk_roster``
(via crm_roster), scoped to Andrew's book and intersected with FUNDED reality (accounts that
actually have a latest holdings snapshot). This replaces the hand-maintained
``config.ENROLLMENT`` as the authoritative source. ``config.ENROLLMENT`` is LEFT IN PLACE as
a fallback (and to be retired later): if the CRM connection is not wired yet (the read-only
role's connection string in the ``TRADINGDESK_CRM_DSN`` env var — Andrew's provisioning step)
or unreachable, ``enrolled_roster`` degrades to the local config so the wall always has a
deterministic allow-list and nothing here can break by a transient DB outage.
"""
from __future__ import annotations

import config
import crm_roster


def crm_enrolled_roster(advisor_name: str | None = crm_roster.DEFAULT_ADVISOR,
                        model: str | None = None) -> list[str]:
    """The blessed roster built from the CRM view ``v_tradingdesk_roster``, scoped to one
    advisor's book (default: Andrew's) and INTERSECTED with funded reality — only accounts
    that have a latest holdings snapshot survive (an un-funded / un-visible account cannot be
    acted on even if it is blessed). Returns the desk account identifiers (IBKR numbers)
    SORTED and de-duped. Raises crm_roster.CrmRosterUnavailable if the CRM is not configured
    or reachable. Pure/read-only: SELECTs from the read-only role, contacts no broker."""
    with_conn = crm_roster._connect()
    try:
        rows = crm_roster.fetch_roster(advisor_name=advisor_name, model=model, conn=with_conn)
        funded = crm_roster.funded_account_ids(
            [r["account_id"] for r in rows], conn=with_conn)
        return sorted({
            crm_roster.account_identifier(r)
            for r in rows if str(r["account_id"]) in funded
        })
    finally:
        with_conn.close()


def enrolled_roster() -> list[str]:
    """The human-blessed execution roster: the authoritative execution allow-list.

    Built FIRST from the CRM roster view (``crm_enrolled_roster`` — Andrew's book, funded
    reality). If the CRM path is not wired (no ``TRADINGDESK_CRM_DSN`` env var / read-only
    role connection string set — Andrew's provisioning step) or is unreachable, it degrades
    to the local ``config.ENROLLMENT`` map (kept in place for exactly this fallback and to be
    retired later), so the account wall always has a deterministic allow-list.

    It is INDEPENDENT of any planner output on purpose — the account wall must gate on who we
    are blessed to trade, never on 'whatever the planner produced'. Pure: reads the CRM view
    (read-only role) or config; contacts no broker."""
    if crm_roster.is_configured():
        try:
            roster = crm_enrolled_roster()
            if roster:
                return roster
        except crm_roster.CrmRosterUnavailable:
            pass  # CRM not reachable -> fall back to the local config allow-list
    return sorted(set(config.ENROLLMENT))
