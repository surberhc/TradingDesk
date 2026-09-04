"""test_crm_cashflows.py — deriving cashflows.SCHEDULE entries from the CRM's
`accounts.recurring_transactions` field (no live DB).

Covers parsing of IBKR's `~`-delimited recurring-transaction format, the to_flow
conversion rules (monthly-only, not-expired, USD, positive amount), the end-to-end draft
builder, the env-var gate, and the fake-connection DB read. Nothing here opens a socket or
hits the CRM.
"""
import datetime

import crm_cashflows
from cashflows import Flow


# --- parsing ----------------------------------------------------------------------

def test_parse_withdrawal_monthly_open_ended():
    parsed = crm_cashflows.parse_recurring_transactions(
        "~WITHDRAWAL~ACH~MONTHLY~2026-09-24~~USD~3846.0")
    assert len(parsed) == 1
    p = parsed[0]
    assert p.ok is True
    assert p.type == "WITHDRAWAL"
    assert p.frequency == "MONTHLY"
    assert p.start_date == "2026-09-24"
    assert p.end_date is None
    assert p.currency == "USD"
    assert p.amount == 3846.0

    flow, reason = crm_cashflows.to_flow(p, as_of=datetime.date(2026, 9, 4))
    assert reason is None
    assert flow == Flow(kind="distribution", amount=3846.0, pct_nav=0.0, day=24, note=flow.note)


def test_parse_deposit_monthly():
    parsed = crm_cashflows.parse_recurring_transactions(
        "~DEPOSIT~ACH~MONTHLY~2024-10-15~~USD~500.0")
    assert len(parsed) == 1
    flow, reason = crm_cashflows.to_flow(parsed[0], as_of=datetime.date(2026, 9, 4))
    assert reason is None
    assert flow.kind == "contribution"
    assert flow.amount == 500.0
    assert flow.day == 15


def test_two_instructions_joined_by_semicolon():
    raw = ("~WITHDRAWAL~ACH~MONTHLY~2026-04-24~~USD~5359.57;"
           "~WITHDRAWAL~ACH~MONTHLY~2026-05-16~~USD~5359.57")
    parsed = crm_cashflows.parse_recurring_transactions(raw)
    assert len(parsed) == 2
    assert all(p.ok for p in parsed)
    days = sorted(crm_cashflows._day_of_month(p.start_date) for p in parsed)
    assert days == [16, 24]


def test_expired_end_date_withheld():
    parsed = crm_cashflows.parse_recurring_transactions(
        "~DEPOSIT~ACH~MONTHLY~2025-01-13~2026-01-13~USD~50.0")[0]
    flow, reason = crm_cashflows.to_flow(parsed, as_of=datetime.date(2026, 9, 4))
    assert flow is None
    assert "expired" in reason


def test_not_yet_expired_end_date_still_included():
    parsed = crm_cashflows.parse_recurring_transactions(
        "~DEPOSIT~ACH~MONTHLY~2025-01-13~2027-01-13~USD~50.0")[0]
    flow, reason = crm_cashflows.to_flow(parsed, as_of=datetime.date(2026, 9, 4))
    assert reason is None
    assert flow is not None
    assert flow.amount == 50.0


def test_non_monthly_frequency_withheld():
    parsed = crm_cashflows.parse_recurring_transactions(
        "~WITHDRAWAL~ACH~QUARTERLY~2026-01-01~~USD~3000.0")[0]
    flow, reason = crm_cashflows.to_flow(parsed, as_of=datetime.date(2026, 9, 4))
    assert flow is None
    assert "non-monthly" in reason or "frequency" in reason


def test_malformed_entry_reports_parse_error():
    parsed = crm_cashflows.parse_recurring_transactions("garbage-not-the-right-shape")
    assert len(parsed) == 1
    p = parsed[0]
    assert p.ok is False
    assert p.error is not None

    flow, reason = crm_cashflows.to_flow(p, as_of=datetime.date(2026, 9, 4))
    assert flow is None
    assert "parse error" in reason


def test_missing_fields_entry_reports_parse_error():
    # Too few '~'-delimited fields (missing currency/amount).
    parsed = crm_cashflows.parse_recurring_transactions("~WITHDRAWAL~ACH~MONTHLY~2026-01-01")
    assert len(parsed) == 1
    assert parsed[0].ok is False


def test_none_empty_and_whitespace_raw_yield_no_entries():
    assert crm_cashflows.parse_recurring_transactions(None) == []
    assert crm_cashflows.parse_recurring_transactions("") == []
    assert crm_cashflows.parse_recurring_transactions("   ") == []


# --- derive_schedule_draft end-to-end ----------------------------------------------

def test_derive_schedule_draft_end_to_end():
    as_of = datetime.date(2026, 9, 4)
    rows = [
        {"account_number": "U11111111",
         "recurring_transactions": "~WITHDRAWAL~ACH~MONTHLY~2026-09-24~~USD~3846.0"},
        {"account_number": "U22222222",
         "recurring_transactions": "~DEPOSIT~ACH~MONTHLY~2024-10-15~~USD~500.0"},
        {"account_number": "U13201195",
         "recurring_transactions": "~DEPOSIT~ACH~MONTHLY~2025-01-13~2026-01-13~USD~50.0"},
        {"account_number": "U33333333",
         "recurring_transactions": "garbage-not-the-right-shape"},
        {"account_number": "   ",
         "recurring_transactions": "~DEPOSIT~ACH~MONTHLY~2026-01-01~~USD~100.0"},
    ]

    draft = crm_cashflows.derive_schedule_draft(rows, as_of=as_of)

    assert set(draft.schedule.keys()) == {"U11111111", "U22222222"}
    assert len(draft.schedule["U11111111"]) == 1
    assert draft.schedule["U11111111"][0].kind == "distribution"
    assert draft.schedule["U11111111"][0].amount == 3846.0
    assert draft.schedule["U11111111"][0].day == 24
    assert len(draft.schedule["U22222222"]) == 1
    assert draft.schedule["U22222222"][0].kind == "contribution"
    assert draft.schedule["U22222222"][0].amount == 500.0
    assert draft.schedule["U22222222"][0].day == 15

    flagged_accounts = {item["account_number"] for item in draft.flagged}
    assert flagged_accounts == {"U13201195", "U33333333"}
    expired = next(i for i in draft.flagged if i["account_number"] == "U13201195")
    assert "expired" in expired["reason"]
    malformed = next(i for i in draft.flagged if i["account_number"] == "U33333333")
    assert "parse error" in malformed["reason"]

    # blank/whitespace account_number is skipped entirely — neither scheduled nor flagged
    assert "   " not in draft.schedule
    assert all(item["account_number"] != "   " for item in draft.flagged)


# --- env-var gate -------------------------------------------------------------------

def test_is_configured_reflects_env(monkeypatch):
    monkeypatch.delenv(crm_cashflows.DSN_ENV, raising=False)
    assert crm_cashflows.is_configured() is False
    monkeypatch.setenv(crm_cashflows.DSN_ENV, "postgresql://x")
    assert crm_cashflows.is_configured() is True
    monkeypatch.setenv(crm_cashflows.DSN_ENV, "   ")
    assert crm_cashflows.is_configured() is False


def test_dsn_missing_raises_unavailable(monkeypatch):
    monkeypatch.delenv(crm_cashflows.DSN_ENV, raising=False)
    import pytest
    with pytest.raises(crm_cashflows.CrmCashflowsUnavailable):
        crm_cashflows._dsn()


# --- fake-connection DB read (mirrors test_crm_roster.py's fake conn shape) --------

class _Cur:
    description = (("account_number",), ("recurring_transactions",))

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def cursor(self):
        return _Cur(self._rows)

    def close(self):
        self.closed = True


def test_fetch_accounts_with_recurring_uses_injected_conn_and_does_not_close_it():
    rows = [
        ("U11111111", "~WITHDRAWAL~ACH~MONTHLY~2026-09-24~~USD~3846.0"),
        ("U22222222", "~DEPOSIT~ACH~MONTHLY~2024-10-15~~USD~500.0"),
    ]
    conn = _Conn(rows)
    result = crm_cashflows.fetch_accounts_with_recurring(conn=conn)
    assert result == [
        {"account_number": "U11111111",
         "recurring_transactions": "~WITHDRAWAL~ACH~MONTHLY~2026-09-24~~USD~3846.0"},
        {"account_number": "U22222222",
         "recurring_transactions": "~DEPOSIT~ACH~MONTHLY~2024-10-15~~USD~500.0"},
    ]
    assert conn.closed is False


# --- format_report smoke test -------------------------------------------------------

def test_format_report_includes_schedule_and_flagged_sections():
    draft = crm_cashflows.DraftResult(
        schedule={"U11111111": [Flow(kind="distribution", amount=3846.0, pct_nav=0.0,
                                     day=24, note="test")]},
        flagged=[{"account_number": "U33333333", "raw": "garbage",
                 "reason": "parse error: bad shape"}],
    )
    report = crm_cashflows.format_report(draft)
    assert "U11111111" in report
    assert "Flagged for manual review" in report
    assert "U33333333" in report
