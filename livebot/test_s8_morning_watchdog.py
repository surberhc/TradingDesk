"""test_s8_morning_watchdog.py — OFFLINE tests for the MORNING "STILL DOWN" watchdog.

NO real network, NO real mail, NO real scheduler, NO real sleeps: the port probe, the
trading-day calendar, the send path and the diagnostics capture are all injected, so every
case here runs instantly against fakes.

What is proved:
  * NON-TRADING DAY -> no send, returns skipped_non_trading_day.
  * TRADING DAY + port UP (True) -> no send, up_no_alert.
  * TRADING DAY + port DOWN (False) -> exactly ONE send, alerted True, and the subject/body
    carry the key phrases ("STILL DOWN", "2FA").
  * TRADING DAY + port UNKNOWN (None) -> STILL alerts (unknown is not-confirmed-up).
  * A RAISING send is swallowed — the watchdog can never propagate into its caller.
  * main() returns 0 even when the underlying check errors.

Run (from C:\\TradingDesk\\livebot):
    powershell -Command "$env:PYTHONPATH=''; C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s8_morning_watchdog.py -q"
"""

from __future__ import annotations

import s8_morning_watchdog as mw


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeSend:
    """Captures (subject, lines) calls instead of sending anything. NO SMTP, ever."""

    def __init__(self, raises: bool = False) -> None:
        self.calls = []
        self.raises = raises

    def __call__(self, subject, lines, mailer=None, log=print):
        if self.raises:
            raise RuntimeError("SMTP is down (simulated)")
        self.calls.append((subject, list(lines)))
        return True

    @property
    def subjects(self):
        return [s for s, _ in self.calls]

    def body_text(self, i=0):
        return "\n".join(str(x) for x in self.calls[i][1])


def _kwargs(send, *, probe, trading=True, **extra):
    """Common injected seams: fixed trading-day answer + fixed port probe + a fake send.
    ``capture`` is left as the real (pure) s8_gateway_alert.capture_diagnostics, driven by
    the injected probe — it never touches the network."""
    kw = dict(
        probe_port=lambda *a, **k: probe,
        is_trading_day=lambda _d: trading,
        today="2026-07-27",              # any non-None value; the calendar seam is injected
        send=send,
        ct_date=lambda: "20260727",
        log=lambda *_a, **_k: None,
    )
    kw.update(extra)
    return kw


# --------------------------------------------------------------------------- #
# (a) non-trading day -> no email
# --------------------------------------------------------------------------- #

def test_non_trading_day_sends_nothing():
    send = FakeSend()
    res = mw.check_and_alert(**_kwargs(send, probe=False, trading=False))
    assert res == {"acted": "skipped_non_trading_day", "alerted": False}
    assert send.calls == []


# --------------------------------------------------------------------------- #
# (b) trading day + gateway UP -> no email
# --------------------------------------------------------------------------- #

def test_trading_day_gateway_up_sends_nothing():
    send = FakeSend()
    res = mw.check_and_alert(**_kwargs(send, probe=True, trading=True))
    assert res["acted"] == "up_no_alert"
    assert res["alerted"] is False
    assert send.calls == []


# --------------------------------------------------------------------------- #
# (c) trading day + gateway DOWN -> exactly one email with the key phrases
# --------------------------------------------------------------------------- #

def test_trading_day_gateway_down_sends_exactly_one_alert():
    send = FakeSend()
    res = mw.check_and_alert(**_kwargs(send, probe=False, trading=True))

    assert res["acted"] == "alerted"
    assert res["alerted"] is True
    assert res["port_listening"] is False
    assert len(send.calls) == 1

    subject = send.subjects[0]
    body = send.body_text(0)
    assert "STILL DOWN" in subject
    assert "2FA" in body
    assert "STILL NOT UP" in body
    # Reuses the shared inverse-2FA security warning verbatim.
    assert "DO NOT APPROVE IT" in body
    # Zero-transmit is reassured.
    assert "PILOT_MODE=True" in body


# --------------------------------------------------------------------------- #
# (c') trading day + gateway UNKNOWN (None) -> STILL alerts
# --------------------------------------------------------------------------- #

def test_trading_day_gateway_unknown_still_alerts():
    send = FakeSend()
    res = mw.check_and_alert(**_kwargs(send, probe=None, trading=True))
    assert res["acted"] == "alerted"
    assert res["alerted"] is True
    assert res["port_listening"] is None
    assert len(send.calls) == 1


# --------------------------------------------------------------------------- #
# Best-effort: a raising send must NEVER propagate
# --------------------------------------------------------------------------- #

def test_raising_send_is_swallowed_and_never_propagates():
    send = FakeSend(raises=True)
    res = mw.check_and_alert(**_kwargs(send, probe=False, trading=True))
    # It did not throw; it reported the error path instead.
    assert res["alerted"] is False
    assert res["acted"] == "error"
    assert "RuntimeError" in res["error"]


def test_raising_probe_is_treated_as_unknown_and_still_alerts():
    """A probe that explodes is UNKNOWN, not confirmed-up — so it must still alert."""
    send = FakeSend()

    def boom(*_a, **_k):
        raise OSError("socket layer exploded")

    res = mw.check_and_alert(**_kwargs(send, probe=False, trading=True, probe_port=boom))
    assert res["acted"] == "alerted"
    assert res["alerted"] is True
    assert res["port_listening"] is None
    assert len(send.calls) == 1


# --------------------------------------------------------------------------- #
# main() always returns 0, even when the underlying check errors
# --------------------------------------------------------------------------- #

def test_main_returns_zero_even_when_check_errors(monkeypatch):
    def boom(**_k):
        raise RuntimeError("check exploded")

    # Inject a check_and_alert that raises; main must still return 0 (it calls the real
    # check_and_alert, which itself never raises, but monkeypatching proves main is robust
    # to a check that somehow does).
    monkeypatch.setattr(mw, "check_and_alert", boom)
    try:
        rc = mw.main([])
    except Exception:  # pragma: no cover — the whole point is this must NOT happen
        rc = "raised"
    assert rc == 0


def test_main_returns_zero_on_the_normal_alert_path(monkeypatch):
    monkeypatch.setattr(mw, "check_and_alert",
                        lambda **_k: {"acted": "alerted", "alerted": True})
    assert mw.main([]) == 0
