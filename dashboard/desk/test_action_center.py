"""Tests for the Action Center poster.

It no longer keeps a SQLite notice store — it files each alert as a row in the CRM's
``public.tasks``. These tests replace the old store tests (post/read/dedup/dismiss/unread/
snooze/migration), which exercised a database that no longer exists. Coverage is kept on the
behaviour that still matters: the column mapping, the ONE de-duplication rule, the
``is_snoozed`` call site the four nightly jobs still use, and the promise that a CRM outage
never takes a nightly job down with it.

The CRM is faked here so the suite stays hermetic — no network, no credential, no live DB.
"""
import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import action_center  # noqa: E402


class _FakeCursor:
    """Understands exactly the two statements action_center issues."""

    def __init__(self, rows):
        self.rows = rows
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("SELECT"):
            source, dedup_key = params
            self._result = (1,) if any(
                r["source"] == source and r["dedup_key"] == dedup_key
                and r["status"] == "open" for r in self.rows) else None
        else:
            title, description, severity, category, dedup_key, source = params
            row = {"id": str(uuid.uuid4()), "title": title, "description": description,
                   "severity": severity, "category": category, "dedup_key": dedup_key,
                   "source": source, "status": "open"}
            self.rows.append(row)
            self._result = (row["id"],)

    def fetchone(self):
        return self._result


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _FakeCursor(self.rows)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def crm(monkeypatch):
    """A stand-in CRM: the list IS the tasks table."""
    rows = []
    monkeypatch.setattr(action_center, "_connect", lambda: _FakeConn(rows))
    return rows


# --------------------------------------------------------------------------- #
# column mapping                                                               #
# --------------------------------------------------------------------------- #
def test_post_notice_maps_columns_onto_a_crm_task(crm):
    task_id = action_center.post_notice(
        "cash_deploy", "Idle cash is sitting uninvested", "There is $5,000 in free cash.",
        severity="warn", action_hint="Open the Trade Execution page.",
        dedup_key="s0_cash_deploy_open")

    assert task_id and len(crm) == 1
    row = crm[0]
    assert row["id"] == task_id
    assert row["title"] == "Idle cash is sitting uninvested"
    assert row["category"] == "cash_deploy"          # kind -> category
    assert row["dedup_key"] == "s0_cash_deploy_open"
    assert row["status"] == "open"
    # the RLS policy refuses any other source, so this must be exact
    assert row["source"] == "trading_desk"
    # the CRM only accepts error/warning/info; the desk's jobs all say "warn"
    assert row["severity"] == "warning"
    # the CRM has no action_hint column, so the hint is folded into the description
    assert "There is $5,000 in free cash." in row["description"]
    assert "Open the Trade Execution page." in row["description"]


def test_severity_falls_back_to_info_when_unrecognised(crm):
    action_center.post_notice("x", "T", "B", severity="whatever")
    assert crm[0]["severity"] == "info"


def test_detail_json_and_ts_are_accepted_and_ignored(crm):
    """The four nightly jobs still pass these; the CRM has no column for either. The call must
    keep working rather than raising a TypeError on an unexpected keyword."""
    task_id = action_center.post_notice(
        "outofspec", "T", "B", dedup_key="oos",
        detail_json=[{"account": "U1", "model": "Growth"}], ts="2026-09-05 10:00:00")
    assert task_id and len(crm) == 1


# --------------------------------------------------------------------------- #
# the ONE de-duplication rule                                                  #
# --------------------------------------------------------------------------- #
def test_open_alert_with_same_dedup_key_posts_nothing(crm):
    first = action_center.post_notice("cash_deploy", "T1", "B1", dedup_key="d")
    assert first and len(crm) == 1

    # the desk has no UPDATE permission: the original is left standing, not refreshed
    second = action_center.post_notice("cash_deploy", "T2", "B2", dedup_key="d")
    assert second is None
    assert len(crm) == 1
    assert crm[0]["title"] == "T1"


def test_closing_the_task_lets_a_fresh_alert_through(crm):
    action_center.post_notice("cash_deploy", "T1", "B1", dedup_key="d")
    assert action_center.has_open("d")

    crm[0]["status"] = "done"                   # Andrew closes it in the CRM
    assert not action_center.has_open("d")

    assert action_center.post_notice("cash_deploy", "T2", "B2", dedup_key="d")
    assert len(crm) == 2


def test_no_dedup_key_always_inserts(crm):
    a = action_center.post_notice("x", "A", "a")
    b = action_center.post_notice("x", "B", "b")
    assert a and b and a != b
    assert len(crm) == 2


def test_is_snoozed_is_now_the_same_already_open_check(crm):
    """Snooze is gone as a concept. The four jobs still call is_snoozed() before posting, and
    it must now mean 'an open alert is already waiting in the CRM'."""
    assert action_center.is_snoozed("d") is False
    action_center.post_notice("cash_deploy", "T", "B", dedup_key="d")
    assert action_center.is_snoozed("d") is True
    assert action_center.is_snoozed("d") == action_center.has_open("d")
    assert action_center.is_snoozed("") is False


# --------------------------------------------------------------------------- #
# a CRM outage must never take a nightly job down                              #
# --------------------------------------------------------------------------- #
def test_crm_unreachable_degrades_quietly(monkeypatch, capsys):
    def _boom():
        raise RuntimeError("could not connect to the CRM database")

    monkeypatch.setattr(action_center, "_connect", _boom)

    assert action_center.post_notice("cash_deploy", "T", "B", dedup_key="d") is None
    assert action_center.has_open("d") is False      # fail-open
    assert action_center.is_snoozed("d") is False

    # and it says so in plain English rather than dying silently
    err = capsys.readouterr().err.lower()
    assert "could not file this alert" in err
