"""test_s8_startup.py — OFFLINE tests for the bounded STARTUP CONNECT-RETRY seam.

NO broker, NO gateway, NO network, NO real sleeps: the connect callable, the clock and the
sleep are all injected, so every case here runs instantly against a fake clock.

What is proved:
  * SUCCESS FIRST TRY — returns the connection promptly, no sleeping.
  * SUCCEEDS AFTER REFUSALS — a gateway that refuses N times then accepts is connected to,
    with a "waiting for IB Gateway on port 4003" line logged per failed attempt.
  * BOUNDED CLEAN GIVE-UP — a gateway that never comes up ends in StartupConnectTimeout
    (a caught, handled condition) after the window, not a hang and not an uncaught crash.
  * NON-CONNECTION ERRORS ARE NOT SWALLOWED — a genuine bug propagates immediately instead
    of being retried forever.
  * BOTH live modules exit CLEANLY (SystemExit, nonzero rc) — never a raw traceback — when
    the gateway never becomes available.

Run (from C:\\TradingDesk\\livebot):
    powershell -Command "$env:PYTHONPATH=''; C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s8_startup.py -q"
"""

from __future__ import annotations

import pytest

import s8_startup
from s8_startup import StartupConnectTimeout, connect_with_retry


class _FakeClock:
    """Monotonic fake clock: only injected sleeps advance it (no real time passes)."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.t += float(secs)


def _harness():
    clock = _FakeClock()
    logs: list[str] = []
    return clock, logs, dict(clock=clock.now, sleep=clock.sleep, log=logs.append)


def test_returns_immediately_when_connect_succeeds():
    clock, logs, kw = _harness()
    sentinel = object()
    ib = connect_with_retry(lambda: sentinel, timeout_secs=1200, poll_secs=20, **kw)
    assert ib is sentinel
    assert clock.t == 0.0          # no sleeping at all
    assert logs == []              # a clean first-try connect logs nothing extra


def test_retries_without_raising_then_succeeds():
    clock, logs, kw = _harness()
    sentinel = object()
    calls = {"n": 0}

    def _connect():
        calls["n"] += 1
        if calls["n"] < 4:
            raise ConnectionRefusedError(
                "[WinError 1225] The remote computer refused the network connection")
        return sentinel

    ib = connect_with_retry(_connect, timeout_secs=1200, poll_secs=20, **kw)
    assert ib is sentinel
    assert calls["n"] == 4
    assert clock.t == pytest.approx(60.0)                     # 3 polls of 20s
    waiting = [m for m in logs if "waiting for IB Gateway on port 4003" in m]
    assert len(waiting) == 3
    assert any("accepted the connection after 4 attempt(s)" in m for m in logs)


def test_gives_up_cleanly_after_the_bounded_window():
    clock, logs, kw = _harness()

    def _always_refused():
        raise ConnectionRefusedError("[WinError 1225] refused")

    with pytest.raises(StartupConnectTimeout) as excinfo:
        connect_with_retry(_always_refused, timeout_secs=1200, poll_secs=20, **kw)

    # Bounded: it stopped (no hang) at/after the window, and said so legibly.
    assert clock.t >= 1200.0
    assert "gateway never became available on port 4003" in str(excinfo.value)
    assert "20 minutes" in str(excinfo.value)
    assert len(logs) == 61          # one waiting line per attempt across the window


def test_timeout_error_is_also_treated_as_not_ready_yet():
    _clock, logs, kw = _harness()
    calls = {"n": 0}

    def _connect():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("API handshake timed out")
        return "ib"

    assert connect_with_retry(_connect, timeout_secs=100, poll_secs=20, **kw) == "ib"
    assert any("waiting for IB Gateway" in m for m in logs)


def test_non_connection_exception_is_not_swallowed():
    _clock, logs, kw = _harness()
    calls = {"n": 0}

    def _connect():
        calls["n"] += 1
        raise ValueError("unknown consumer 's8_typo' — clientId registry lookup failed")

    with pytest.raises(ValueError):
        connect_with_retry(_connect, timeout_secs=1200, poll_secs=20, **kw)
    assert calls["n"] == 1          # a genuine bug is NOT retried for 20 minutes
    assert logs == []


# --------------------------------------------------------------------------- #
# Wiring: both live modules turn the give-up into a CLEAN nonzero exit
# --------------------------------------------------------------------------- #
def _force_refused(monkeypatch):
    """Shrink the window to nothing and make every connect attempt refuse."""
    monkeypatch.setattr(s8_startup, "STARTUP_CONNECT_WAIT_SECS", 0.0)
    monkeypatch.setattr(s8_startup, "STARTUP_CONNECT_POLL_SECS", 0.0)

    from connections import ibkr_live_trade

    def _refuse(*_a, **_kw):
        raise ConnectionRefusedError(
            "[WinError 1225] The remote computer refused the network connection")

    monkeypatch.setattr(ibkr_live_trade, "connect", _refuse)


@pytest.mark.parametrize("module_name, cls_name", [
    ("s8_service", "S8Service"),
    ("s8_collector", "S8Collector"),
])
def test_module_exits_cleanly_when_gateway_never_comes_up(
        module_name, cls_name, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("S8_PILOT_ROOT", str(tmp_path))
    _force_refused(monkeypatch)

    import importlib

    mod = importlib.import_module(module_name)
    with pytest.raises(SystemExit) as excinfo:
        getattr(mod, cls_name)().run()

    assert excinfo.value.code != 0          # nonzero rc for the launcher/operator
    out = capsys.readouterr().out
    assert "waiting for IB Gateway on port 4003" in out
    assert "gateway never became available on port 4003" in out
    assert "Traceback" not in out           # legible line, never a raw traceback
