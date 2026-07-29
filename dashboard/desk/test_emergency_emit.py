"""test_emergency_emit.py — locks conductor #67: emergency._emit must actually
write to the durable event store using eventlog.record_event's REAL signature.

Before the fix, _emit guessed four wrong call shapes (all raised TypeError), so
emergency Halt/Flatten actions were silently NOT logged. Test A asserts exactly
ONE event is stored, which fails hard against the old code (it stored nothing).

Self-contained: bootstraps sys.path to this file's own directory so bare
``import emergency`` / ``import eventlog`` resolve, and redirects eventlog's DB to
a throwaway tmp_path so no real audit DB is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make bare imports resolve regardless of pytest's rootdir / invocation cwd.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import emergency  # noqa: E402
import eventlog  # noqa: E402


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point eventlog at a throwaway DB. eventlog._connect() does DB_DIR.mkdir()
    then sqlite3.connect(str(DB_PATH)), so BOTH must be patched before any call."""
    db_dir = tmp_path / "desk_dashboard"
    monkeypatch.setattr(eventlog, "DB_DIR", db_dir)
    monkeypatch.setattr(eventlog, "DB_PATH", db_dir / "events.db")
    # Ensure emergency is using the live record_event (not a leftover None from a
    # prior test) for the tests that expect writes.
    monkeypatch.setattr(emergency, "record_event", eventlog.record_event)
    return db_dir


def test_A_halt_action_logs_exactly_one_event(temp_db):
    """Regression for #67: a halt-action _emit must store exactly one event, with
    the plain-English message the caller supplied and the mapped severity."""
    emergency._emit(
        "emergency_halt_action",
        which="all",
        target="MorningExecuteDaily",
        kind="task",
        ok=True,
        message="Stopped the task and turned it off.",
    )
    events = eventlog.read_events()
    assert len(events) == 1  # old code stored ZERO — this pins the bug closed.
    ev = events[0]
    assert ev["source"] == "Emergency controls"
    assert ev["category"] == "emergency_halt_action"
    assert ev["severity"] == "good"
    assert ev["message"] == "Stopped the task and turned it off."


def test_B_halt_completed_failure_severity_and_message(temp_db):
    """A not-ok halt-completed maps to severity 'bad' and a generated plain-English
    sentence naming the strategy and that it only partly completed."""
    emergency._emit("emergency_halt_completed", which="s8", ok=False, actions=3)
    events = eventlog.read_events()
    assert len(events) == 1
    ev = events[0]
    assert ev["severity"] == "bad"
    assert "Strategy 8" in ev["message"]
    assert "partly" in ev["message"]


def test_C_no_logger_never_raises_and_writes_nothing(temp_db, monkeypatch):
    """If no logger is present (record_event is None), _emit must be a silent
    no-op — no exception, nothing written."""
    monkeypatch.setattr(emergency, "record_event", None)
    # Must not raise.
    emergency._emit("emergency_halt_action", which="all", ok=True,
                    message="should not be stored")
    # Nothing was written (record_event was never called).
    assert eventlog.read_events() == []


def test_D_plain_message_halt_requested_sentence():
    """The generated halt-requested sentence counts the tasks and mentions the
    force-stop of running programs when kills_processes is set."""
    msg = emergency._plain_message(
        "emergency_halt_requested",
        {"which": "all", "tasks": ["a", "b"], "kills_processes": True},
    )
    assert "2 scheduled task" in msg
    assert "force-stop" in msg
