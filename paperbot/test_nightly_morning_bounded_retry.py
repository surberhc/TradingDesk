"""
test_nightly_morning_bounded_retry.py — offline tests for the bounded-retry connect
policy shared (in spirit) by nightly_monitor_run.py and morning_execute_run.py, plus
morning_execute_run's zero-touch no-op when nothing is staged.

NO broker, NO gateway, NO real network/sleep: ibkr_paper.connect and time.sleep are
monkeypatched so a "gateway never comes up" scenario runs in milliseconds, not ~10
minutes, and never shells out to the real IBC launcher.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_nightly_morning_bounded_retry.py -q
"""
from __future__ import annotations

import json
import os

import pytest

import morning_execute_run as mer
import nightly_monitor_run as nmr


# --- nightly_monitor_run.bounded_connect ------------------------------------------
def test_bounded_connect_gives_up_after_cap(monkeypatch):
    calls = {"n": 0}

    def always_fails(consumer, readonly=True, launch=True, timeout=10):
        calls["n"] += 1
        raise RuntimeError("gateway did not come up")

    monkeypatch.setattr(nmr.ibkr_paper, "connect", always_fails)
    monkeypatch.setattr(nmr.time, "sleep", lambda s: None)   # no real backoff wait

    result = nmr.bounded_connect("paperbot_nightly_monitor")
    assert result is None
    assert calls["n"] == nmr.CONNECT_MAX_ATTEMPTS   # never retries past the cap


def test_bounded_connect_succeeds_on_a_later_attempt(monkeypatch):
    calls = {"n": 0}
    sentinel = object()

    def fails_then_succeeds(consumer, readonly=True, launch=True, timeout=10):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("not up yet")
        return sentinel

    monkeypatch.setattr(nmr.ibkr_paper, "connect", fails_then_succeeds)
    monkeypatch.setattr(nmr.time, "sleep", lambda s: None)

    result = nmr.bounded_connect("paperbot_nightly_monitor")
    assert result is sentinel
    assert calls["n"] == 2
    assert calls["n"] <= nmr.CONNECT_MAX_ATTEMPTS


def test_main_alerts_and_exits_cleanly_on_connect_failure(monkeypatch):
    """When every connect attempt fails, main() must alert + report failure + return
    a nonzero code WITHOUT raising — and never leave anything to disconnect (bounded_connect
    itself returned None, so there is no IB object to leak)."""
    monkeypatch.setattr(nmr, "bounded_connect", lambda *a, **k: None)
    alerts = []
    monkeypatch.setattr(nmr, "_alert_email", lambda subject, lines: alerts.append(subject))
    monkeypatch.setattr(nmr, "_write_status", lambda *a, **k: None)

    rc = nmr.main()
    assert rc == 1
    assert len(alerts) == 1
    assert "gateway connect FAILED" in alerts[0]


# --- morning_execute_run: zero-touch no-op when nothing staged --------------------
def test_morning_execute_zero_touch_when_nothing_staged(monkeypatch, tmp_path):
    """The common-case day: no staged file for today -> ZERO gateway contact. Assert
    bounded_connect is never even called."""
    monkeypatch.setattr(mer, "PENDING_TRADES_DIR", str(tmp_path))
    monkeypatch.setattr(mer, "_stage_path", lambda today: os.path.join(str(tmp_path),
                                                                       f"{today.isoformat()}.json"))
    connect_calls = {"n": 0}

    def should_not_be_called(*a, **k):
        connect_calls["n"] += 1
        raise AssertionError("bounded_connect must not be called when nothing is staged")

    monkeypatch.setattr(mer, "bounded_connect", should_not_be_called)

    rc = mer.main()
    assert rc == 0
    assert connect_calls["n"] == 0


def test_morning_execute_kill_switch_skips_without_archiving(monkeypatch, tmp_path):
    """AUTOTRADE_DISABLED sentinel present -> skip transmission, alert, and leave the
    staged file IN PLACE (not archived) so a human can act on it manually."""
    from datetime import date

    monkeypatch.setattr(mer, "PENDING_TRADES_DIR", str(tmp_path))
    stage_file = os.path.join(str(tmp_path), f"{date.today().isoformat()}.json")
    with open(stage_file, "w", encoding="utf-8") as fh:
        json.dump({"date": date.today().isoformat(), "routes": [],
                   "regime": {"confirmed": "GOLDILOCKS"},
                   "prices_by_symbol": {}}, fh)
    monkeypatch.setattr(mer, "_stage_path", lambda today: stage_file)

    sentinel_path = os.path.join(str(tmp_path), "AUTOTRADE_DISABLED")
    with open(sentinel_path, "w", encoding="utf-8") as fh:
        fh.write("")
    monkeypatch.setattr(mer, "AUTOTRADE_DISABLED_SENTINEL", sentinel_path)

    alerts = []
    monkeypatch.setattr(mer, "_alert_email", lambda subject, lines: alerts.append(subject))
    monkeypatch.setattr(mer, "_write_status", lambda *a, **k: None)
    connect_calls = {"n": 0}
    monkeypatch.setattr(mer, "bounded_connect",
                        lambda *a, **k: connect_calls.__setitem__("n", connect_calls["n"] + 1))

    rc = mer.main()
    assert rc == 0
    assert os.path.exists(stage_file)          # left in place, not archived
    assert connect_calls["n"] == 0              # never even tries to connect
    assert any("AUTOTRADE_DISABLED" in a for a in alerts)


def test_morning_execute_pilot_mode_defaults_true():
    """PILOT_MODE must default to True — the whole point of the pilot build. This is a
    tripwire: if anyone flips it in a future edit without deliberately updating this
    test, CI-equivalent (pytest) fails loudly."""
    assert mer.PILOT_MODE is True


def test_morning_execute_bounded_connect_gives_up_after_cap(monkeypatch):
    calls = {"n": 0}

    def always_fails(consumer, readonly=True, launch=True, timeout=10):
        calls["n"] += 1
        raise RuntimeError("gateway did not come up")

    monkeypatch.setattr(mer.ibkr_paper, "connect", always_fails)
    monkeypatch.setattr(mer.time, "sleep", lambda s: None)

    result = mer.bounded_connect("paperbot_morning_execute")
    assert result is None
    assert calls["n"] == mer.CONNECT_MAX_ATTEMPTS
