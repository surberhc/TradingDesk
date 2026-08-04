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
                "from v_tradingdesk_holdings_latest where account_id = any(%s)",
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


def funded_account_ids(account_ids: Iterable[str], conn=None) -> set[str]:
    """The subset of the given CRM account_ids that have a latest holdings snapshot — the
    read-only stand-in for 'funded / visible reality' (an account with no holdings snapshot
    is not something the desk can act on). READ-ONLY."""
    holdings = fetch_holdings_latest(account_ids, conn=conn)
    return {aid for aid, rows in holdings.items() if rows}
