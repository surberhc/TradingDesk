"""crm_roster.py — the desk's READ-ONLY reader for the CRM roster (P1 out-of-spec path).

This is the seam that lets the Trading Desk build its execution roster (and the whole-book
out-of-spec read) from the CRM's blessed account list — the Supabase view
``public.v_tradingdesk_roster`` — instead of the hand-maintained ``config.ENROLLMENT`` map.

HARD BOUNDARIES (load-bearing):
  * READ-ONLY. Every query here is a plain ``SELECT`` against two postgres-owned VIEWS
    (``v_tradingdesk_roster`` and ``v_tradingdesk_holdings_latest``). It writes nothing,
    builds no order, and contacts no broker. It is meant to be run under the dedicated
    read-only Postgres role ``tradingdesk_readonly`` (SELECT-only; see the CRM migration).
  * NO HARD-CODED CREDENTIAL. The connection string is read ONLY from the environment
    variable ``TRADINGDESK_CRM_DSN`` (a libpq/psycopg connection string or postgres URL).
    Nothing here embeds a host, user, or password. If the env var (or the psycopg2 driver)
    is absent, the reader raises :class:`CrmRosterUnavailable` with an actionable message —
    it never silently invents a connection.

ACCOUNT IDENTITY MAPPING
------------------------
The desk's account identifier IS the IBKR account number (e.g. ``U20984696``). In the CRM
that value lives in ``v_tradingdesk_roster.account_number``. So the CRM->desk account map is
the identity on ``account_number`` — centralised in :func:`account_identifier` so the mapping
is explicit and has one definition, never scattered string handling.
"""
from __future__ import annotations

import os
from typing import Iterable, Mapping, Optional

# The env var that carries the read-only role's connection string. Andrew provisions this
# from the `tradingdesk_readonly` role's password + connection string (a separate, secret
# step). Until it is set, the CRM-backed path is dormant and callers fall back to config.
DSN_ENV = "TRADINGDESK_CRM_DSN"

# Whose book the desk scopes to FIRST (P1). Andrew's advisor name exactly as it appears in
# CRM ``advisors.name`` / ``v_tradingdesk_roster.advisor_name``.
DEFAULT_ADVISOR = "Andrew P Surber"


class CrmRosterUnavailable(RuntimeError):
    """Raised when the CRM roster cannot be read (no DSN configured, driver missing, or a
    connection/query error). Callers that must degrade gracefully (e.g. roster.enrolled_roster)
    catch this and fall back to the local config."""


def is_configured() -> bool:
    """True iff the CRM connection string env var is present and non-empty. Cheap, no I/O —
    lets a caller decide whether to attempt the CRM path at all before importing a driver."""
    return bool(os.environ.get(DSN_ENV, "").strip())


def _dsn() -> str:
    dsn = os.environ.get(DSN_ENV, "").strip()
    if not dsn:
        raise CrmRosterUnavailable(
            f"CRM connection is not configured: set the {DSN_ENV} environment variable to the "
            f"read-only role's connection string (the `tradingdesk_readonly` Postgres role — "
            f"Andrew provisions the password/DSN). No credential is stored in code.")
    return dsn


def _connect():
    """Open a read-only psycopg2 connection from the DSN env var. Raises CrmRosterUnavailable
    (never a bare ImportError/OperationalError) so callers have one thing to catch."""
    try:
        import psycopg2  # noqa: PLC0415 — driver imported lazily so module import is cheap
    except Exception as exc:  # noqa: BLE001
        raise CrmRosterUnavailable(
            "psycopg2 is not installed in this environment — install it "
            "(`pip install psycopg2-binary`) to read the CRM roster.") from exc
    try:
        conn = psycopg2.connect(_dsn())
        # Belt-and-suspenders: this reader must never write. A read-only session refuses any
        # write even if a future query tried one.
        conn.set_session(readonly=True, autocommit=True)
        return conn
    except CrmRosterUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CrmRosterUnavailable(f"could not connect to the CRM database: {exc}") from exc


def _rows_as_dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# --- account identity (CRM account_number == desk account id) -------------------
def account_identifier(row: Mapping) -> str:
    """The desk's account identifier for a CRM roster row: the IBKR account number verbatim
    (``v_tradingdesk_roster.account_number``). Single definition of the CRM->desk account map."""
    return str(row["account_number"])


# --- reads ----------------------------------------------------------------------
def fetch_roster(advisor_name: Optional[str] = DEFAULT_ADVISOR,
                 model: Optional[str] = None,
                 conn=None) -> list[dict]:
    """Read the blessed roster from ``v_tradingdesk_roster``.

    ``advisor_name`` scopes to one advisor's book (default: Andrew's); pass None for the whole
    book. ``model`` optionally filters to one strategy/version (e.g. "Growth"). Returns a list
    of plain dicts (one per blessed account). READ-ONLY. Raises CrmRosterUnavailable if the
    CRM is not reachable/configured."""
    where = []
    params: list = []
    if advisor_name is not None:
        where.append("advisor_name = %s")
        params.append(advisor_name)
    if model is not None:
        where.append("model = %s")
        params.append(model)
    sql = ("select account_number, master_name, ib_entity, model, advisor_name, advisor_id, "
           "entity, no_trade, keep_open, total_value, nav_as_of, custodian, account_id "
           "from v_tradingdesk_roster")
    if where:
        sql += " where " + " and ".join(where)
    sql += " order by advisor_name nulls last, total_value desc nulls last"

    own = conn is None
    conn = conn or _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return _rows_as_dicts(cur)
    except CrmRosterUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CrmRosterUnavailable(f"CRM roster query failed: {exc}") from exc
    finally:
        if own:
            conn.close()


def fetch_holdings_latest(account_ids: Iterable[str], conn=None) -> dict[str, list[dict]]:
    """Read each account's latest-date holdings from ``v_tradingdesk_holdings_latest``.

    Returns ``{account_id -> [ {symbol, quantity, mark_price, market_value, as_of_date}, ... ]}``.
    ``account_id`` here is the CRM UUID (``v_tradingdesk_roster.account_id``), the join key —
    NOT the IBKR number. READ-ONLY."""
    ids = [str(a) for a in account_ids]
    out: dict[str, list[dict]] = {}
    if not ids:
        return out
    own = conn is None
    conn = conn or _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select account_id, symbol, asset_category, quantity, mark_price, "
                "market_value, as_of_date "
                "from v_tradingdesk_holdings_latest where account_id = any(%s::uuid[])",
                (ids,))
            for r in _rows_as_dicts(cur):
                out.setdefault(str(r["account_id"]), []).append(r)
        return out
    except CrmRosterUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CrmRosterUnavailable(f"CRM holdings query failed: {exc}") from exc
    finally:
        if own:
            conn.close()


# How stale a NAV may be before an all-cash account stops counting as visible. The IBKR flex
# sync runs daily, so a week of silence means the feed — not the account — is the problem.
FUNDED_NAV_MAX_AGE_DAYS = 7

# Minimum cash before a POSITIONLESS account counts as fundable. Applies ONLY to the
# NAV-without-holdings path: an account that already holds positions is on the roster whatever
# it is worth, exactly as before. Set at $100 because the cheapest thing the desk can buy is
# one share (PDBC ~$19, XLB ~$54, up to XLK ~$183), so below this there is no order to place
# and the account would just add noise. Andrew's call 2026-08-24, after the fix swept in three
# dust accounts ($957, $3, $1) alongside the eight real ones.
FUNDED_MIN_CASH_NAV = 100.0


def funded_account_ids(account_ids: Iterable[str], conn=None) -> set[str]:
    """The subset of the given CRM account_ids the desk can actually act on — 'funded reality'.

    An account qualifies if it has a latest HOLDINGS snapshot, **or** if it has a fresh NAV
    with at least FUNDED_MIN_CASH_NAV in it. That second clause matters: this used to require holdings alone,
    which silently excluded a newly funded, never-traded account — it holds 100% cash, so it
    has no positions to snapshot. Those are exactly the accounts that most need their first
    trade (deploy the cash), and the old test dropped them from the executable roster while
    they looked perfectly healthy everywhere else. Found 2026-08-24 with a $826k rollover that
    had been assigned, repped, and adrift since April.

    The NAV must be FRESH (see FUNDED_NAV_MAX_AGE_DAYS). A stale NAV means the feed is broken,
    and acting on a stale balance is worse than not acting. READ-ONLY."""
    ids = [str(a) for a in account_ids]
    if not ids:
        return set()
    holdings = fetch_holdings_latest(ids, conn=conn)
    funded = {aid for aid, rows in holdings.items() if rows}

    sql = ("select id::text from accounts "
           "where id::text = any(%s) and archived_at is null "
           "and coalesce(total_value, 0) >= %s "
           "and nav_as_of is not null and nav_as_of >= current_date - %s")
    own = conn is None
    conn = conn or _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (ids, FUNDED_MIN_CASH_NAV, FUNDED_NAV_MAX_AGE_DAYS))
            funded |= {r[0] for r in cur.fetchall()}
    except CrmRosterUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CrmRosterUnavailable(f"CRM funded-NAV query failed: {exc}") from exc
    finally:
        if own:
            conn.close()
    return funded


# --- Andrew-authored ("custom") model allocations -------------------------------
# The CRM view that publishes Andrew's own hand-authored model portfolios (ticker +
# percentage) to the desk. READ-ONLY, granted to `tradingdesk_readonly`. Its contract, which
# the desk relies on and does NOT re-implement:
#   * one row per (model, ticker) of the CURRENT PUBLISHED version only;
#   * drafts and future-dated versions NEVER appear;
#   * a model with no published allocation returns NO ROWS at all (never a zero-weight row);
#   * weights are PERCENTAGES that sum to exactly 100 per model (DB triggers enforce it, and
#     a published version is immutable).
# Columns: strategy_name, strategy_code, ticker, weight_pct, version_number, effective_from,
#          published_at, version_id, strategy_id.
CUSTOM_ALLOCATIONS_VIEW = "v_tradingdesk_custom_allocations"

_CUSTOM_ALLOCATION_COLUMNS = ("strategy_name", "strategy_code", "ticker", "weight_pct",
                              "version_number", "effective_from", "published_at",
                              "version_id", "strategy_id")


def fetch_custom_allocations(strategy_names: Optional[Iterable[str]] = None,
                             conn=None) -> list[dict]:
    """Read the current published custom allocations from ``v_tradingdesk_custom_allocations``.

    ``strategy_names`` optionally restricts the read to specific model LABELS — the same
    strings that appear as ``v_tradingdesk_roster.model`` (e.g. "Growth (Custom)"). None reads
    every published custom model. Passing an EMPTY iterable is an explicit "nothing to ask
    for" and returns ``[]`` without touching the database.

    Returns a list of plain dicts, ordered by model then ticker. A model with no published
    allocation simply contributes no rows — callers must treat that as "no allocation", never
    as an empty allocation (an empty target would liquidate an account). READ-ONLY. Raises
    CrmRosterUnavailable if the CRM is not reachable/configured."""
    names: Optional[list[str]] = None
    if strategy_names is not None:
        names = [str(n) for n in strategy_names]
        if not names:
            return []

    sql = (f"select {', '.join(_CUSTOM_ALLOCATION_COLUMNS)} "
           f"from {CUSTOM_ALLOCATIONS_VIEW}")
    params: list = []
    if names is not None:
        sql += " where strategy_name = any(%s::text[])"
        params.append(names)
    sql += " order by strategy_name, ticker"

    own = conn is None
    conn = conn or _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return _rows_as_dicts(cur)
    except CrmRosterUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CrmRosterUnavailable(f"CRM custom-allocation query failed: {exc}") from exc
    finally:
        if own:
            conn.close()


def custom_allocation_labels(conn=None) -> set[str]:
    """The set of model labels that CURRENTLY have a published custom allocation.

    This is the desk's SOURCE-BASED test for "is this model one Andrew authored?" — the only
    safe one. A NAME-based test (does the label say "Custom"? does it end in " (Small)"?) is
    one CRM rename away from routing a hand-authored model into the S0 backtester, or into
    small_tier.collapse, and silently throwing the whole allocation away. READ-ONLY."""
    own = conn is None
    conn = conn or _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"select distinct strategy_name from {CUSTOM_ALLOCATIONS_VIEW}")
            return {str(r[0]) for r in cur.fetchall()}
    except CrmRosterUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CrmRosterUnavailable(f"CRM custom-allocation label query failed: {exc}") from exc
    finally:
        if own:
            conn.close()
