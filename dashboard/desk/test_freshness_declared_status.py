"""Tests for the Desk Pulse freshness panel honouring each job's DECLARED status.

THE POINT OF THIS FILE is a real defect that was open until now: data_freshness() read
each nightly job's status JSON but used only its "date", so the colour tier came purely
from how old that date was. js["status"] was never referenced. A job that ran exactly on
schedule and honestly wrote {"status": "fail"} was displayed green, "Updated ... (today)".

That is not hypothetical: the live-data Gateway failed on 2026-09-02, 03 and 04, the
"forward" job wrote a failing status each of those days, and the panel stayed green.

These tests pin down that a job's own declared outcome can only ever make the tier worse,
never better, and that the operator is told so in plain words rather than by colour alone.
Run from dashboard/desk/:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import deskdata as dd  # noqa: E402


def _rows(status_value, *, include_status=True, age_days=0):
    """Run data_freshness() with every tracked job reporting the same thing."""
    today = datetime.now(tz=dd.CT_ZONE).replace(tzinfo=None)
    stamp = (today - timedelta(days=age_days)).strftime("%Y%m%d")
    js = {"job": "x", "date": stamp}
    if include_status:
        js["status"] = status_value
    dd.data_freshness.clear()
    try:
        original = dd._read_status_json
        dd._read_status_json = lambda job: dict(js)
        return dd.data_freshness()
    finally:
        dd._read_status_json = original
        dd.data_freshness.clear()


# ------------------------------------------- a failing job is never healthy ---
@pytest.mark.parametrize("bad_status", ["fail", "stale"])
def test_fresh_but_failing_job_is_not_healthy(bad_status):
    """Dated TODAY, so the age check alone would call it good. It must not be."""
    for row in _rows(bad_status):
        assert row["tier"] == "bad", row
        assert "today" in row["phrase"]          # still fresh by date...
        assert row["phrase"].startswith("This job reported")   # ...and still called out


def test_failure_is_explained_in_plain_words_not_only_colour():
    for row in _rows("fail"):
        assert "did not finish successfully" in row["phrase"]


def test_out_of_date_data_is_explained_in_plain_words():
    for row in _rows("stale"):
        assert "its data is out of date" in row["phrase"]


def test_declared_trouble_is_spelled_out_english():
    """House rule: no shorthand or jargon in what the operator reads."""
    for status in ("fail", "stale", "partial"):
        for row in _rows(status):
            lowered = row["phrase"].lower()
            for shorthand in ("fail", "stale", "partial", "err", "n/a", "status:"):
                assert shorthand not in lowered, f"{shorthand!r} in: {row['phrase']}"


# ------------------------------------------------ partial is the warning tier ---
def test_partial_reads_as_the_warning_tier():
    for row in _rows("partial"):
        assert row["tier"] == "warn", row
        assert "only partly finished" in row["phrase"]


def test_partial_never_upgrades_an_already_bad_age():
    """A declared outcome may only make the tier worse, never better."""
    for row in _rows("partial", age_days=30):
        assert row["tier"] == "bad", row


# --------------------------------------------------- healthy path is unchanged ---
def test_ok_today_is_still_good_and_reads_exactly_as_before():
    for row in _rows("ok"):
        assert row["tier"] == "good", row
        assert row["phrase"].startswith("Updated ")
        assert row["phrase"].endswith("(today)")


def test_ok_but_old_stays_degraded_as_it_always_did():
    """The age check still applies to a job that declares itself fine."""
    assert all(r["tier"] == "bad" for r in _rows("ok", age_days=30))
    assert all(r["tier"] == "warn" for r in _rows("ok", age_days=4))


def test_missing_status_field_behaves_exactly_as_before():
    for row in _rows(None, include_status=False):
        assert row["tier"] == "good", row
        assert row["phrase"].startswith("Updated ")


# ------------------------------------------------------- unrecognized outcomes ---
def test_unrecognized_status_is_not_waved_through_as_healthy():
    for row in _rows("kaput"):
        assert row["tier"] == "unknown", row
        assert "does not recognize" in row["phrase"]
