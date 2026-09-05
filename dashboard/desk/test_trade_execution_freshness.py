"""Tests for the Trade Execution page's prepared-trade freshness gate.

THE POINT OF THIS FILE is a real safety gap that was open until now: _prepare() reads
positions and live prices once and _send() reuses that stored plan word for word, with no
re-read and no re-plan. The only staleness check was on changing the strategy selection,
which says nothing about how much time has passed, so an operator could prepare a trade,
leave the tab open and send hours-old share counts.

These tests pin down that a fresh run can send and a stale one cannot — the pure decision,
the operator-facing sentence, and the backstop refusal inside _send() that fires BEFORE any
gateway connection is opened. Nothing here contacts a broker.

Mirrors test_control_plane_freshness.py, whose PREVIEW_FRESHNESS_SECS window this page's
own window matches. Run from dashboard/desk/:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# page_trade_execution imports the paperbot modules by bare name, relying on desk_app.py's
# sys.path bootstrap, and the page itself lives in the execution/ sub-folder. Reproduce
# both here so the module imports standalone under pytest.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
_EXECUTION = _HERE / "execution"
for _sub in ("paperbot", "backtester", "connections", "strategies", "dailyreport",
             "livebot"):
    _p = _REPO / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
for _p in (_HERE, _EXECUTION):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import page_trade_execution as pte  # noqa: E402

_NOW = 1_800_000_000.0  # a fixed epoch second, so these never depend on the wall clock
_WINDOW = pte.PREPARED_FRESHNESS_SECS


# ---------------------------------------------------------------- the pure decision ---
def test_window_matches_the_control_plane_precedent():
    """30 minutes, the same window the Control Plane uses for its reviewed preview."""
    assert _WINDOW == 1800.0


def test_just_prepared_is_fresh():
    age, fresh = pte._freshness_of(_NOW - 10, _NOW)
    assert fresh and 9 < age < 11


def test_exactly_at_the_window_is_fresh():
    age, fresh = pte._freshness_of(_NOW - _WINDOW, _NOW)
    assert fresh and age == _WINDOW


def test_one_second_past_the_window_is_stale():
    age, fresh = pte._freshness_of(_NOW - (_WINDOW + 1), _NOW)
    assert not fresh and age > _WINDOW


def test_hours_old_is_stale():
    _age, fresh = pte._freshness_of(_NOW - 4 * 3600, _NOW)
    assert not fresh


def test_missing_prepare_time_is_stale_not_waved_through():
    """Fail closed: a run that never recorded its prepare time must not be sendable."""
    age, fresh = pte._freshness_of(None, _NOW)
    assert age is None and not fresh


@pytest.mark.parametrize("bad", ["not-a-time", object(), float("nan")])
def test_unusable_prepare_time_is_stale(bad):
    age, fresh = pte._freshness_of(bad, _NOW)
    assert age is None and not fresh


def test_clock_skew_into_the_future_is_treated_as_fresh():
    _age, fresh = pte._freshness_of(_NOW + 5, _NOW)
    assert fresh


# ------------------------------------------------------- the operator-facing check ---
def test_fresh_run_passes_the_check():
    ok, text = pte._freshness_check({"prepared_at": _NOW - 60}, now=_NOW)
    assert ok
    assert "still current" in text


def test_stale_run_fails_the_check_and_says_to_prepare_again():
    ok, text = pte._freshness_check({"prepared_at": _NOW - 3 * 3600}, now=_NOW)
    assert not ok
    assert "Press Prepare again." in text
    assert "too old to send" in text
    assert "30 minutes" in text          # the window, spelled out for the operator
    assert "about 3 hours ago" in text   # how old it actually is


def test_run_with_no_prepare_time_fails_the_check():
    ok, text = pte._freshness_check({}, now=_NOW)
    assert not ok
    assert "Press Prepare again" in text


def test_check_line_has_the_same_shape_as_the_other_checks():
    """_checks() yields (ok, sentence) pairs; this line is appended to that list, so it
    has to match or it would neither render nor block the way the others do."""
    line = pte._freshness_check({"prepared_at": _NOW}, now=_NOW)
    assert isinstance(line, tuple) and len(line) == 2
    ok, text = line
    assert isinstance(ok, bool) and isinstance(text, str)
    assert text.endswith(".")            # a full sentence, like every other check line


def test_every_refusal_is_plain_spelled_out_english():
    """House rule: no shorthand and no jargon in what the operator reads."""
    for run in ({"prepared_at": _NOW - 3 * 3600}, {}):
        _ok, text = pte._freshness_check(run, now=_NOW)
        lowered = text.lower()
        for shorthand in ("secs", "mins", "hrs", "stale", "ttl", "expiry", "epoch",
                          "timestamp", "n/a"):
            assert shorthand not in lowered, f"{shorthand!r} in operator text: {text}"


# ------------------------------------------------------------- how old, in words ---
def test_age_phrase_is_spelled_out():
    assert pte._age_phrase(0) == "less than a minute ago"
    assert pte._age_phrase(60) == "about 1 minute ago"
    assert pte._age_phrase(20 * 60) == "about 20 minutes ago"
    assert pte._age_phrase(3600) == "about 1 hour ago"
    assert pte._age_phrase(2 * 3600 + 10 * 60) == "about 2 hours and 10 minutes ago"
    assert pte._age_phrase(-5) == "just now"
    assert pte._age_phrase(None) == "at a time that was not recorded"


def test_prepared_at_text_never_comes_back_blank():
    assert pte._prepared_at_text({"prepared_at": _NOW})            # a real clock time
    assert pte._prepared_at_text({}) == "a time that was not recorded"
    assert pte._prepared_at_text({"prepared_at": "nonsense"}) == \
        "a time that could not be read"


# --------------------------------------------- the backstop refusal inside _send() ---
def _tripwire_run(prepared_at):
    """A run whose _built would raise the moment anything tried to use it, so a test that
    reaches the broker path fails loudly instead of quietly doing nothing."""
    class _Tripwire(dict):
        def __getitem__(self, key):
            raise AssertionError("_send() used the prepared plan on a stale run")

    return {"prepared_at": prepared_at, "_built": _Tripwire()}


def test_send_refuses_a_stale_run_before_touching_the_gateway(monkeypatch):
    monkeypatch.setattr(pte.time, "time", lambda: _NOW)
    with pytest.raises(RuntimeError) as excinfo:
        pte._send(_tripwire_run(_NOW - 4 * 3600))
    msg = str(excinfo.value)
    assert "too old to send" in msg
    assert "nothing was sent" in msg
    assert "Press Prepare again." in msg


def test_send_refuses_a_run_with_no_prepare_time(monkeypatch):
    monkeypatch.setattr(pte.time, "time", lambda: _NOW)
    with pytest.raises(RuntimeError):
        pte._send({"_built": {}})


def test_send_lets_a_fresh_run_through_to_the_broker_path(monkeypatch):
    """A fresh run must NOT be refused by the gate. Proved by letting it past the gate and
    catching it at the very next step (reading the built plan), so this asserts the gate
    opened without ever opening a connection."""
    monkeypatch.setattr(pte.time, "time", lambda: _NOW)
    with pytest.raises(AssertionError, match="used the prepared plan"):
        pte._send(_tripwire_run(_NOW - 60))
