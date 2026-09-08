"""The withdrawal SCHEDULE is read from the client system, and can never come back empty.

It used to be a dict typed in by hand, so a newly-onboarded withdrawal client was silently
absent from every reserve check until someone remembered to refresh it. These tests pin the
replacement: the schedule IS the client system's own records, and an unreadable or empty
answer raises instead of looking like "no client takes a withdrawal".
"""
import pytest

import cashflows
import crm_cashflows


def _draft(schedule):
    return crm_cashflows.DraftResult(schedule=schedule, flagged=[])


def test_schedule_is_built_from_the_client_systems_own_records(monkeypatch):
    """No second query and no hand-typed copy: load_schedule returns exactly what the CRM
    derivation (crm_cashflows.build_draft) produced from recurring_transactions."""
    rows = [{"account_number": "U777",
             "recurring_transactions": "~WITHDRAWAL~ACH~MONTHLY~2026-03-11~~USD~1500.0"}]
    monkeypatch.setattr(crm_cashflows, "build_draft",
                        lambda *a, **k: crm_cashflows.derive_schedule_draft(rows))

    schedule = cashflows.load_schedule()

    assert list(schedule) == ["U777"]
    flow = schedule["U777"][0]
    assert (flow.kind, flow.amount, flow.day) == ("distribution", 1500.0, 11)


def test_a_client_added_in_the_crm_shows_up_without_anyone_editing_code(monkeypatch):
    """The whole point: a newly-onboarded withdrawal client is watched immediately."""
    monkeypatch.setattr(crm_cashflows, "build_draft", lambda *a, **k: _draft(
        {"UNEW": [cashflows.Flow("distribution", amount=4000.0, pct_nav=0.0, day=9)]}))
    monkeypatch.setattr(cashflows, "SCHEDULE", cashflows._LiveSchedule())
    assert "UNEW" in cashflows.SCHEDULE
    assert cashflows.reserve_for("UNEW", 250_000.0) == cashflows.RESERVE_MONTHS * 4000.0


def test_unreadable_client_system_raises_instead_of_reading_as_no_withdrawals(monkeypatch):
    def boom(*a, **k):
        raise crm_cashflows.CrmCashflowsUnavailable("connection refused")
    monkeypatch.setattr(crm_cashflows, "build_draft", boom)

    with pytest.raises(cashflows.ScheduleUnavailable) as exc:
        cashflows.load_schedule()
    assert "could not be read from the client system" in str(exc.value)


def test_empty_result_is_a_loud_failure_not_an_empty_schedule(monkeypatch):
    monkeypatch.setattr(crm_cashflows, "build_draft", lambda *a, **k: _draft({}))
    with pytest.raises(cashflows.ScheduleUnavailable):
        cashflows.load_schedule()


def test_contributions_only_is_also_a_failed_read(monkeypatch):
    """Deposits reserve nothing. A book with no WITHDRAWAL instruction at all means the
    read went wrong, not that nine clients stopped taking money out."""
    monkeypatch.setattr(crm_cashflows, "build_draft", lambda *a, **k: _draft(
        {"UDEP": [cashflows.Flow("contribution", amount=50.0, pct_nav=0.0, day=1)]}))
    with pytest.raises(cashflows.ScheduleUnavailable):
        cashflows.load_schedule()


def test_reading_is_lazy_and_cached(monkeypatch):
    """Importing cashflows must touch no network, and one process reads the CRM once."""
    calls = []

    def counted(*a, **k):
        calls.append(1)
        return _draft({"UA": [cashflows.Flow("distribution", amount=1.0, pct_nav=0.0, day=1)]})
    monkeypatch.setattr(crm_cashflows, "build_draft", counted)

    live = cashflows._LiveSchedule()
    assert calls == []          # constructed but not yet read
    len(live), list(live), live.get("UA")
    assert calls == [1]         # read exactly once, then cached


def test_reserve_for_uses_the_live_schedule(monkeypatch):
    """The public helpers callers already use keep working against the live schedule."""
    monkeypatch.setattr(crm_cashflows, "build_draft", lambda *a, **k: _draft(
        {"UZ": [cashflows.Flow("distribution", amount=1000.0, pct_nav=0.0, day=15)]}))
    monkeypatch.setattr(cashflows, "SCHEDULE", cashflows._LiveSchedule())

    assert cashflows.reserve_for("UZ", 500_000.0) == cashflows.RESERVE_MONTHS * 1000.0
    assert cashflows.reserve_for("UNOT_SCHEDULED", 500_000.0) == 0.0


def test_reserve_for_raises_when_the_client_system_is_unreadable(monkeypatch):
    """A reserve of 0.0 sourced from a failed read would under-reserve real client money."""
    def boom(*a, **k):
        raise crm_cashflows.CrmCashflowsUnavailable("connection refused")
    monkeypatch.setattr(crm_cashflows, "build_draft", boom)
    monkeypatch.setattr(cashflows, "SCHEDULE", cashflows._LiveSchedule())

    with pytest.raises(cashflows.ScheduleUnavailable):
        cashflows.reserve_for("UZ", 500_000.0)
