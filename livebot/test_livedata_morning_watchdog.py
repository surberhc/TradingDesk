"""Tests for livebot/livedata_morning_watchdog.py — the LIVE-DATA (port 4001) weekly
"still down" 2FA-tap nudge.

OFFLINE + FAST: no real socket, no real mail, no calendar dependency — every seam
(port probe, trading-day guard, send, today) is injected. Asserts on the returned
decision dict and on whether/how the mailer send was called.
"""
from __future__ import annotations

import datetime as dt

import livedata_morning_watchdog as m


class Recorder:
    """Records send() calls; returns a fixed value."""

    def __init__(self, value=True):
        self.value = value
        self.calls = []

    def __call__(self, subject, lines, mailer=None, log=None):
        self.calls.append({"subject": subject, "lines": lines})
        return self.value


TRADING_DAY = dt.date(2026, 8, 5)   # Wednesday


def _run(*, up, trading=True, send=None, today=TRADING_DAY):
    send = send if send is not None else Recorder()
    res = m.check_and_alert(
        probe_port=lambda port: up,
        is_trading_day=lambda d: trading,
        today=today,
        send=send,
        log=lambda *_a, **_k: None,
    )
    return res, send


def test_up_no_alert():
    res, send = _run(up=True)
    assert res["acted"] == "up_no_alert"
    assert res["alerted"] is False
    assert send.calls == []


def test_down_sends_one_alert():
    res, send = _run(up=False)
    assert res["acted"] == "alerted"
    assert res["alerted"] is True
    assert len(send.calls) == 1
    # LIVE-DATA-specific subject (never confused with an S8 live-trade alert), 4001 named.
    assert "LIVE-DATA" in send.calls[0]["subject"]
    assert any("4001" in str(x) for x in send.calls[0]["lines"])


def test_unknown_probe_alerts():
    """Probe None (undeterminable) is NOT confirmed-up -> alert."""
    res, send = _run(up=None)
    assert res["acted"] == "alerted"
    assert len(send.calls) == 1


def test_probe_that_raises_is_treated_as_unknown_and_alerts():
    send = Recorder()
    res = m.check_and_alert(
        probe_port=lambda port: (_ for _ in ()).throw(OSError("boom")),
        is_trading_day=lambda d: True,
        today=TRADING_DAY, send=send, log=lambda *_a, **_k: None,
    )
    assert res["acted"] == "alerted"
    assert len(send.calls) == 1


def test_non_trading_day_skips_probe_and_send():
    send = Recorder()
    called = {"probe": 0}

    def probe(port):
        called["probe"] += 1
        return False

    res = m.check_and_alert(
        probe_port=probe, is_trading_day=lambda d: False,
        today=dt.date(2026, 8, 8), send=send, log=lambda *_a, **_k: None,
    )
    assert res["acted"] == "skipped_non_trading_day"
    assert res["alerted"] is False
    assert called["probe"] == 0          # never even probed
    assert send.calls == []


def test_calendar_error_fails_open_and_still_alerts_when_down():
    """A raising trading-day guard must FAIL OPEN (assume trading day) so a calendar glitch
    never suppresses a real down alert."""
    send = Recorder()
    res = m.check_and_alert(
        probe_port=lambda port: False,
        is_trading_day=lambda d: (_ for _ in ()).throw(RuntimeError("calendar down")),
        today=TRADING_DAY, send=send, log=lambda *_a, **_k: None,
    )
    assert res["acted"] == "alerted"
    assert len(send.calls) == 1


def test_send_failure_never_raises():
    """A mailer that raises must not propagate — check_and_alert always returns a dict."""
    def boom_send(subject, lines, mailer=None, log=None):
        raise RuntimeError("smtp down")

    res, _ = _run(up=False, send=boom_send)
    # The send exception is caught by the outer guard -> classified as error, never raised.
    assert res["acted"] in ("alerted", "error")
    assert "alerted" in res


def test_port_constant_is_4001():
    assert m.LIVE_DATA_PORT == 4001


def test_main_returns_zero(monkeypatch):
    monkeypatch.setattr(m, "check_and_alert", lambda: {"acted": "up_no_alert", "alerted": False})
    assert m.main() == 0
