"""Shared fixtures for the dailyreport tests.

The Action Center poster (dashboard/desk/action_center.py) now files alerts as rows in the
CRM's ``public.tasks`` instead of a local SQLite store. No test in this suite may touch the
real CRM, so an in-memory stand-in is installed AUTOMATICALLY for every test — without it a
test run would post junk alerts into Andrew's live Action Center.

Ask for the ``crm`` fixture to inspect what a job posted: the list IS the tasks table.
"""
import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _p in (str(_HERE), str(_REPO / "connections"), str(_REPO / "paperbot"),
           str(_REPO / "dashboard" / "desk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


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


@pytest.fixture(autouse=True)
def crm(monkeypatch):
    """Stand in for the CRM's tasks table. Autouse so no test can reach the live CRM."""
    rows = []
    import action_center
    monkeypatch.setattr(action_center, "_connect", lambda: _FakeConn(rows))
    return rows
