"""crm_cashflows.py — derive paperbot cashflows.SCHEDULE entries from the CRM's
IBKR-configured recurring transactions.

The CRM's `ibkr-flex-sync` edge function (Supabase, runs nightly) already writes IBKR's
own configured recurring deposit/withdrawal instructions into `accounts.recurring_transactions`
(pulled from IBKR Flex `AccountInformation.recurringTransactions` — see SYSTEM_MAP.md,
2026-09-04). This module reads that field and turns it into `cashflows.Flow` entries so
`cashflows.SCHEDULE` (and therefore `cashflows.reserve_for` / every real rebalance) can be
populated from IBKR's own data instead of hand-typed or inferred from history.

FORMAT (observed on all 44 populated accounts, 2026-09-04): one or more `;`-separated
instructions per account, each `~`-delimited with a leading `~`:
    ~TYPE~METHOD~FREQUENCY~STARTDATE~ENDDATE~CURRENCY~AMOUNT
e.g. `~WITHDRAWAL~ACH~MONTHLY~2026-09-24~~USD~3846.0` (ENDDATE blank = open-ended) or
`~DEPOSIT~ACH~MONTHLY~2025-01-13~2026-01-13~USD~50.0` (ENDDATE populated — already expired
as of 2026-09-04 in this example; an expired instruction must NOT be auto-included).

HARD BOUNDARIES (mirrors crm_roster.py): READ-ONLY (a plain SELECT against `accounts`, the
`tradingdesk_readonly` Postgres role already has SELECT on that table — confirmed
2026-09-04). NO HARD-CODED CREDENTIAL — the connection string comes only from the
`TRADINGDESK_CRM_DSN` environment variable, exactly like crm_roster.py.

WHY ONLY MONTHLY AUTO-CONVERTS: `cashflows.reserve_for` sums every scheduled Flow as if it
recurs monthly (see cashflows.py's `monthly_dist` sum). IBKR's format allows MONTHLY,
QUARTERLY, or ANNUAL. Folding a quarterly/annual amount straight into a monthly Flow would
silently over- or under-reserve real client money, so anything not MONTHLY is flagged for a
human decision instead of guessed at — same posture as an unparseable entry.

This module ONLY produces a draft (parses + reports). It does NOT write into cashflows.py's
SCHEDULE dict — that stays a deliberate, reviewed, separate step (see crm_cashflows_report.py
/ the docstring on build_draft below). Never called from any live rebalance path.
"""
from __future__ import annotations

import datetime
import os
from dataclasses import dataclass
from typing import Optional

from cashflows import Flow

DSN_ENV = "TRADINGDESK_CRM_DSN"

_KIND_BY_TYPE = {"WITHDRAWAL": "distribution", "DEPOSIT": "contribution"}


class CrmCashflowsUnavailable(RuntimeError):
    """Raised when the CRM cannot be read (no DSN configured, driver missing, or a
    connection/query error). Mirrors crm_roster.CrmRosterUnavailable exactly — one thing
    for a caller to catch, never a bare driver exception."""


def is_configured() -> bool:
    """True iff the CRM connection string env var is present and non-empty. Cheap, no I/O."""
    return bool(os.environ.get(DSN_ENV, "").strip())


def _dsn() -> str:
    dsn = os.environ.get(DSN_ENV, "").strip()
    if not dsn:
        raise CrmCashflowsUnavailable(
            f"CRM connection is not configured: set the {DSN_ENV} environment variable to "
            f"the read-only role's connection string (Andrew provisions the password/DSN). "
            f"No credential is stored in code.")
    return dsn


def _connect():
    """Open a read-only psycopg2 connection from the DSN env var. Raises
    CrmCashflowsUnavailable (never a bare ImportError/OperationalError)."""
    try:
        import psycopg2  # noqa: PLC0415 — imported lazily so module import stays cheap
    except Exception as exc:  # noqa: BLE001
        raise CrmCashflowsUnavailable(
            "psycopg2 is not installed in this environment — install it "
            "(`pip install psycopg2-binary`) to read the CRM.") from exc
    try:
        conn = psycopg2.connect(_dsn())
        conn.set_session(readonly=True, autocommit=True)
        return conn
    except CrmCashflowsUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CrmCashflowsUnavailable(f"could not connect to the CRM database: {exc}") from exc


# --- parsing (pure — no I/O, safe to unit test without a DB) --------------------

@dataclass(frozen=True)
class ParsedRecurringTransaction:
    """One `~`-delimited instruction, parsed. `.error` is set (and every other field is
    best-effort / possibly None) when the entry could not be fully parsed — this dataclass
    never raises on construction, so one malformed instruction can be reported without
    aborting the rest of a whole-book scan."""
    raw: str
    type: Optional[str]
    method: Optional[str]
    frequency: Optional[str]
    start_date: Optional[str]
    end_date: Optional[str]
    currency: Optional[str]
    amount: Optional[float]
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _parse_one(entry: str) -> ParsedRecurringTransaction:
    entry = entry.strip()
    if not entry:
        return ParsedRecurringTransaction(entry, None, None, None, None, None, None, None,
                                          error="empty entry")
    parts = entry.split("~")
    # Expected shape: '', TYPE, METHOD, FREQUENCY, START, END, CURRENCY, AMOUNT (8 parts —
    # the leading '' comes from the entry's own leading '~').
    if len(parts) != 8 or parts[0] != "":
        return ParsedRecurringTransaction(entry, None, None, None, None, None, None, None,
                                          error=f"unexpected field shape ({len(parts)} parts)")
    _, tx_type, method, frequency, start_date, end_date, currency, amount_s = parts
    try:
        amount = float(amount_s)
    except (TypeError, ValueError):
        return ParsedRecurringTransaction(
            entry, tx_type or None, method or None, frequency or None, start_date or None,
            end_date or None, currency or None, None,
            error=f"unparseable amount {amount_s!r}")
    return ParsedRecurringTransaction(
        entry, tx_type or None, method or None, frequency or None, start_date or None,
        end_date or None, currency or None, amount, error=None)


def parse_recurring_transactions(raw: Optional[str]) -> list[ParsedRecurringTransaction]:
    """Parse one account's `accounts.recurring_transactions` field into its individual
    instructions (IBKR joins multiple with ';'). Never raises. Empty/None -> []."""
    if not raw or not raw.strip():
        return []
    return [_parse_one(e) for e in raw.split(";") if e.strip()]


def _day_of_month(iso_date: str) -> Optional[int]:
    try:
        return int(iso_date.split("-")[2])
    except (IndexError, ValueError, AttributeError):
        return None


def _iso_to_date(iso_date: str) -> Optional[datetime.date]:
    try:
        y, m, d = (int(p) for p in iso_date.split("-"))
        return datetime.date(y, m, d)
    except (ValueError, AttributeError, TypeError):
        return None


def to_flow(parsed: ParsedRecurringTransaction,
           as_of: Optional[datetime.date] = None) -> tuple[Optional[Flow], Optional[str]]:
    """Convert one parsed instruction to a `cashflows.Flow`, or (None, reason) when it
    cannot be auto-included. `as_of` defaults to today (UTC) — pass it explicitly in tests
    for determinism. Reasons a Flow is withheld (never guessed past):
      * parse error on the entry itself
      * an unrecognized TYPE (only WITHDRAWAL/DEPOSIT are known)
      * frequency other than MONTHLY (see module docstring — reserve_for assumes monthly)
      * an END DATE that has already passed as of `as_of` (instruction no longer active)
      * an unparseable start date, non-positive/missing amount, or non-USD currency
    """
    if as_of is None:
        as_of = datetime.date.today()
    if not parsed.ok:
        return None, f"parse error: {parsed.error}"
    kind = _KIND_BY_TYPE.get((parsed.type or "").upper())
    if kind is None:
        return None, f"unrecognized type {parsed.type!r}"
    if (parsed.frequency or "").upper() != "MONTHLY":
        return None, (f"non-monthly frequency {parsed.frequency!r} — needs a manual "
                      f"monthly-equivalent decision, not an automatic conversion")
    end_date = (parsed.end_date or "").strip()
    if end_date:
        end = _iso_to_date(end_date)
        if end is None:
            return None, f"unparseable end date {parsed.end_date!r}"
        if end <= as_of:
            return None, f"instruction expired {end_date} (as of {as_of.isoformat()})"
    day = _day_of_month(parsed.start_date or "")
    if day is None:
        return None, f"unparseable start date {parsed.start_date!r}"
    if parsed.amount is None or parsed.amount <= 0:
        return None, f"non-positive/missing amount {parsed.amount!r}"
    if (parsed.currency or "USD").upper() != "USD":
        return None, f"non-USD currency {parsed.currency!r} — needs review"
    note = f"CRM recurring_transactions ({parsed.method or 'unknown method'}, since {parsed.start_date})"
    return Flow(kind=kind, amount=parsed.amount, pct_nav=0.0, day=day, note=note), None


@dataclass(frozen=True)
class DraftResult:
    """`schedule` is ready to review-then-merge into `cashflows.SCHEDULE`. `flagged` lists
    every instruction withheld from auto-inclusion, with why, for a human to resolve."""
    schedule: dict  # dict[str, list[Flow]]
    flagged: list   # list[dict] — {"account_number", "raw", "reason"}


def derive_schedule_draft(rows: list, as_of: Optional[datetime.date] = None) -> DraftResult:
    """`rows`: [{"account_number": ..., "recurring_transactions": ...}, ...] as read from
    the CRM `accounts` table. Pure — no I/O, fully unit-testable without a DB."""
    schedule: dict = {}
    flagged: list = []
    for row in rows:
        account = str(row.get("account_number") or "").strip()
        if not account:
            continue
        for parsed in parse_recurring_transactions(row.get("recurring_transactions")):
            flow, reason = to_flow(parsed, as_of=as_of)
            if flow is not None:
                schedule.setdefault(account, []).append(flow)
            else:
                flagged.append({"account_number": account, "raw": parsed.raw, "reason": reason})
    return DraftResult(schedule=schedule, flagged=flagged)


# --- DB read (mirrors crm_roster.py's connection pattern exactly) ---------------

def fetch_accounts_with_recurring(conn=None) -> list:
    """Read every account with a non-empty `recurring_transactions` field. Read-only. Pass
    `conn` in tests to inject a fake connection/cursor (see test_crm_cashflows.py)."""
    owns_conn = conn is None
    if owns_conn:
        conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select account_number, recurring_transactions from accounts "
                "where recurring_transactions is not null and recurring_transactions <> ''"
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        if owns_conn:
            conn.close()


def build_draft(conn=None, as_of: Optional[datetime.date] = None) -> DraftResult:
    """Fetch from the CRM + parse, in one call. This is a REPORT step only — it returns a
    DraftResult for a human to review; nothing here writes to cashflows.py or any broker."""
    rows = fetch_accounts_with_recurring(conn=conn)
    return derive_schedule_draft(rows, as_of=as_of)


# --- plain-text report -----------------------------------------------------------

def format_report(draft: DraftResult) -> str:
    lines = ["=== Draft cashflows.SCHEDULE from CRM accounts.recurring_transactions ===", ""]
    if not draft.schedule:
        lines.append("(no MONTHLY, currently-active entries ready to add)")
    for account in sorted(draft.schedule):
        lines.append(f"{account}:")
        for f in draft.schedule[account]:
            lines.append(f"    Flow({f.kind!r}, amount={f.amount}, pct_nav=0.0, day={f.day})  # {f.note}")
    lines.append("")
    lines.append(f"=== Flagged for manual review ({len(draft.flagged)}) ===")
    if not draft.flagged:
        lines.append("(none)")
    for item in draft.flagged:
        lines.append(f"{item['account_number']}: {item['raw']}  -- {item['reason']}")
    return "\n".join(lines)


def main() -> int:
    if not is_configured():
        print(f"CRM connection not configured — set {DSN_ENV}.")
        return 1
    try:
        draft = build_draft()
    except CrmCashflowsUnavailable as exc:
        print(f"Could not read the CRM: {exc}")
        return 1
    print(format_report(draft))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
