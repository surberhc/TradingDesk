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
The blessed roster comes FIRST from the CRM's read-only view ``v_tradingdesk_roster`` (via
crm_roster), scoped to Andrew's book and intersected with FUNDED reality.
``config.ENROLLMENT`` is LEFT IN PLACE as a DEGRADED-MODE FALLBACK ONLY — the live CRM
roster is the source of truth: if the CRM connection is not wired (the read-only role's
connection string in the ``TRADINGDESK_CRM_DSN`` env var) or unreachable, ``enrolled_roster``
degrades to the local config so the wall always has a deterministic allow-list.

TWO THINGS THIS MODULE NOW ENFORCES THAT IT PREVIOUSLY DID NOT
--------------------------------------------------------------
1. THE NO-TRADE HOLD. ``v_tradingdesk_roster`` has always carried ``no_trade`` and
   ``crm_roster.fetch_roster`` has always SELECTed it, but nothing in the execution path ever
   read it — the flag was a record with no effect, so a hold set in the CRM did not stop the
   desk trading that account. It is applied HERE, in the allow-list itself, so a held account
   is missing from BOTH walls: never read or sized, AND independently refused by
   ``allowed_accounts`` in safe_execute.account_wall_ok.
2. MODEL SCOPE. A run can be narrowed to one or more model labels, so the first live
   deployment of a subset of the book need not be an all-or-nothing whole-book run. The
   SQL-side single-``model`` filter already existed in ``crm_roster.fetch_roster``; the plural
   ``models`` scope is applied in Python over the fetched rows so that function's SQL contract
   stays untouched for its other callers.

Both are reported, never silent: the ``*_scan`` functions return what was excluded and why.
"""
from __future__ import annotations

from typing import Iterable, Optional

import config
import crm_roster


class RosterScopeUnavailable(RuntimeError):
    """Raised when a caller asked for a MODEL-SCOPED roster and that scope cannot be honoured.

    The degraded-mode ``config.ENROLLMENT`` fallback carries no model labels, so it cannot
    answer "only the accounts on these models". Silently falling back would WIDEN a
    deliberately narrowed run to whatever the fallback happens to contain — the exact
    opposite of what the caller asked for, and on this rail that means trading accounts the
    operator explicitly excluded. Refuse instead."""


# ========================================================================================
# THE NO-TRADE HOLD — the CRM's per-account "do not trade this account" flag.
#
# FAIL CLOSED. Only an EXPLICITLY false flag clears the hold. A missing key or a NULL means
# "the record does not say this account is safe to trade", which is not the same thing as
# "it is", so it is held. Measured against the live view on 2026-09-01: all 302 roster rows
# carry an explicit false, so this costs nothing today and only bites if the column stops
# being populated — which is exactly when refusing is the right answer.
# ========================================================================================
def has_no_trade_hold(row) -> bool:
    """True if this CRM roster row carries a NO-TRADE HOLD. PURE. Fail closed: anything other
    than an explicitly false ``no_trade`` — including a missing key and NULL — is a hold."""
    value = row.get("no_trade") if hasattr(row, "get") else None
    if value is None:
        return True
    return bool(value)


def matches_models(row, models: Optional[Iterable[str]]) -> bool:
    """True if this roster row's model is within ``models``. PURE.

    ``models`` None or empty means NO scope filter — every row matches. An empty selection is
    deliberately "the whole book", never "nothing", because the unscoped whole-book run is the
    pre-existing behaviour and an accidental empty list must not silently become a
    zero-account run that looks like a clean pass.

    Compares the CRM's STORED label. The desk re-tiers that label at plan time (custom_tier /
    small_tier), but re-tiering only ever moves an account WITHIN its own model family, so
    scoping on the stored label selects the same set either way."""
    if not models:
        return True
    wanted = {str(m).strip() for m in models if str(m).strip()}
    if not wanted:
        return True
    return str(row.get("model") or "").strip() in wanted


def partition_roster_rows(rows, models: Optional[Iterable[str]] = None) -> tuple[list, list]:
    """PURE: ``(tradeable_rows, held_rows)`` for the rows inside ``models``.

    Rows outside the model scope are dropped entirely — they were never asked for. Rows inside
    it are split by :func:`has_no_trade_hold`, so a caller can act on the tradeable set and
    REPORT the held set by name rather than quietly shrinking the roster."""
    scoped = [r for r in rows if matches_models(r, models)]
    held = [r for r in scoped if has_no_trade_hold(r)]
    tradeable = [r for r in scoped if not has_no_trade_hold(r)]
    return tradeable, held


def crm_enrolled_roster_scan(advisor_name: str | None = crm_roster.DEFAULT_ADVISOR,
                             model: str | None = None,
                             models: Optional[Iterable[str]] = None) -> dict:
    """The blessed roster read from the CRM, plus everything a caller must be able to SAY about
    it. ONE database round-trip.

    Returns ``{"accounts", "held", "unfunded", "models", "scope"}``:
      * ``accounts`` — the execution allow-list: in scope, no hold, funded reality.
      * ``held``     — in scope but carrying a NO-TRADE HOLD. Excluded from ``accounts``,
                       returned so the run can name them.
      * ``unfunded`` — in scope and unheld, but with no funded reality to act on.
      * ``models``   — every distinct model label in the advisor's book BEFORE scoping, so a
                       UI can offer the real choices instead of a hardcoded list.
      * ``scope``    — the model scope actually applied (sorted; empty means whole book).

    ``model`` (singular, SQL-side) is the pre-existing one-label filter, unchanged. ``models``
    (plural) is applied in Python over the fetched rows. Raises
    crm_roster.CrmRosterUnavailable if the CRM is not configured or reachable. Read-only:
    SELECTs from the read-only role, contacts no broker."""
    with_conn = crm_roster._connect()
    try:
        rows = crm_roster.fetch_roster(advisor_name=advisor_name, model=model, conn=with_conn)
        all_models = sorted({str(r.get("model") or "").strip() for r in rows
                             if str(r.get("model") or "").strip()})
        tradeable_rows, held_rows = partition_roster_rows(rows, models)
        # Funded reality is asked ONLY about rows that survived the hold + scope filters — a
        # held account's funded state is irrelevant and must not widen the query.
        funded = crm_roster.funded_account_ids(
            [r["account_id"] for r in tradeable_rows], conn=with_conn)
        return {
            "accounts": sorted({crm_roster.account_identifier(r) for r in tradeable_rows
                                if str(r["account_id"]) in funded}),
            "held": sorted({crm_roster.account_identifier(r) for r in held_rows}),
            "unfunded": sorted({crm_roster.account_identifier(r) for r in tradeable_rows
                                if str(r["account_id"]) not in funded}),
            "models": all_models,
            "scope": sorted({str(m).strip() for m in (models or ()) if str(m).strip()}),
        }
    finally:
        with_conn.close()


def crm_enrolled_roster(advisor_name: str | None = crm_roster.DEFAULT_ADVISOR,
                        model: str | None = None,
                        models: Optional[Iterable[str]] = None) -> list[str]:
    """The blessed roster built from the CRM view ``v_tradingdesk_roster``, scoped to one
    advisor's book (default: Andrew's), optionally narrowed to ``models``, with NO-TRADE HOLD
    accounts removed, and INTERSECTED with funded reality — an un-funded / un-visible account
    cannot be acted on even if it is blessed. Returns the desk account identifiers (IBKR
    numbers) SORTED and de-duped.

    Thin wrapper over :func:`crm_enrolled_roster_scan`; use that one when you need to report
    what was held back. Raises crm_roster.CrmRosterUnavailable if the CRM is not configured or
    reachable. Pure / read-only: SELECTs from the read-only role, contacts no broker."""
    return crm_enrolled_roster_scan(
        advisor_name=advisor_name, model=model, models=models)["accounts"]


def enrolled_roster_scan(models: Optional[Iterable[str]] = None) -> dict:
    """The human-blessed execution roster PLUS what was excluded and why. Same shape as
    :func:`crm_enrolled_roster_scan`, with an added ``source`` key of ``"crm"`` or
    ``"config"``.

    Built FIRST from the CRM roster view (Andrew's book, no-trade holds honoured, funded
    reality). If the CRM path is not wired or is unreachable, it degrades to the local
    ``config.ENROLLMENT`` map so the account wall always has a deterministic allow-list.

    ONE EXCEPTION TO THE FALLBACK: if a MODEL SCOPE was requested, the degraded path RAISES
    :class:`RosterScopeUnavailable` rather than falling back. ``config.ENROLLMENT`` carries no
    model labels, so it cannot honour "only these models" — and a scoped run that silently
    widened to the whole fallback allow-list is the worst outcome available here."""
    scope = sorted({str(m).strip() for m in (models or ()) if str(m).strip()})
    if crm_roster.is_configured():
        try:
            scan = crm_enrolled_roster_scan(models=scope or None)
            # A scoped read is authoritative even when it selects nothing: "no account is on
            # that model" is a real answer and must not fall through to the config list.
            if scan["accounts"] or scan["held"] or scope:
                scan["source"] = "crm"
                return scan
        except crm_roster.CrmRosterUnavailable:
            if scope:
                raise RosterScopeUnavailable(
                    f"a model-scoped run was requested ({', '.join(scope)}) but the CRM "
                    f"roster is unreachable, and the degraded config.ENROLLMENT fallback "
                    f"carries no model labels — refusing rather than widening the run to the "
                    f"whole fallback allow-list") from None
            # No scope asked for -> the pre-existing degraded behaviour is correct.
    if scope:
        raise RosterScopeUnavailable(
            f"a model-scoped run was requested ({', '.join(scope)}) but the CRM roster is not "
            f"configured, and the degraded config.ENROLLMENT fallback carries no model labels "
            f"— refusing rather than widening the run to the whole fallback allow-list")
    return {"accounts": sorted(set(config.ENROLLMENT)), "held": [], "unfunded": [],
            "models": [], "scope": [], "source": "config"}


def enrolled_roster(models: Optional[Iterable[str]] = None) -> list[str]:
    """The human-blessed execution roster: the authoritative execution allow-list.

    It is INDEPENDENT of any planner output on purpose — the account wall must gate on who we
    are blessed to trade, never on 'whatever the planner produced'. Accounts carrying the
    CRM's NO-TRADE HOLD are excluded; pass ``models`` to narrow the run to one or more model
    labels. Thin wrapper over :func:`enrolled_roster_scan` — use that one when you need to
    report what was held back. Pure: reads the CRM view (read-only role) or config; contacts
    no broker."""
    return enrolled_roster_scan(models=models)["accounts"]
