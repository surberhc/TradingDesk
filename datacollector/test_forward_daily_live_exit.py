"""test_forward_daily_live_exit.py — offline tests for the process EXIT-CODE contract
of forward_daily_live.main() (the nightly EOD option collector).

Regression guard for the exit-0 masking bug (fixed 2026-08-05): when
ensure_gateway() failed, main() did a bare `return`, so the process exited 0 and
Windows Task Scheduler showed the aborted nightly run as SUCCESS. main() now
returns an int exit code that the wrapper propagates via `exit /b %ERRORLEVEL%`.

Contract asserted here (fully offline — no gateway, no network):
 - gateway DOWN on a TRADING DAY            -> exit 1, jobstatus "fail", alert emailed,
                                              and NO launch attempted (2026-09-08)
 - weekend                                  -> exit 0, jobstatus "ok"
 - full-day market holiday                  -> exit 0, jobstatus "ok"
 - all roots collect on a trading day       -> exit 0, jobstatus "ok"
 - every root fails on a trading day        -> exit 1 ("fail")

Run from datacollector/:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest test_forward_daily_live_exit.py -q
"""

from __future__ import annotations

import datetime

import forward_daily_live as fdl


class _FakeDate:
    """Stand-in for the module's `date` symbol so we can pin main()'s notion of today."""

    def __init__(self, d: datetime.date):
        self._d = d

    def today(self) -> datetime.date:
        return self._d


# A real, ordinary trading day (Wed 2026-08-05) and a weekend (Sat 2026-08-08).
TRADING_DAY = datetime.date(2026, 8, 5)
WEEKEND = datetime.date(2026, 8, 8)


def _capture_status(monkeypatch):
    """Silence file logging and capture jobstatus.write() calls."""
    writes: list[tuple] = []
    monkeypatch.setattr(fdl, "log", lambda *a, **k: None)
    monkeypatch.setattr(
        fdl.jobstatus, "write",
        lambda key, state, *a, **k: writes.append((key, state, a, k)))
    return writes


def test_gateway_down_on_trading_day_exits_nonzero(monkeypatch):
    monkeypatch.setattr(fdl, "date", _FakeDate(TRADING_DAY))
    monkeypatch.setattr(fdl, "gateway_up", lambda *a, **k: False)
    monkeypatch.setattr(fdl, "alert_gateway_down", lambda *a, **k: True)
    writes = _capture_status(monkeypatch)

    rc = fdl.main()

    assert rc == 1, "gateway-down on a trading day must exit non-zero"
    # The status file — the second, independent detection path — must still fire.
    assert writes, "jobstatus.write must still be called on the skip path"
    key, state, _, _ = writes[-1]
    assert (key, state) == ("forward", "fail")


def test_gateway_down_emails_and_never_launches(monkeypatch):
    """The whole point of the 2026-09-08 change (conductor #82).

    A 17:30 launch fires an IBKR Mobile 2FA push nobody may be there to answer, which
    fails the login and burns one of IBKR's counted failed logins. So a down gateway
    must produce an EMAIL and a skip — never a launch attempt.
    """
    monkeypatch.setattr(fdl, "date", _FakeDate(TRADING_DAY))
    monkeypatch.setattr(fdl, "gateway_up", lambda *a, **k: False)
    _capture_status(monkeypatch)

    emailed: list[str] = []
    monkeypatch.setattr(fdl, "alert_gateway_down",
                        lambda daystr: emailed.append(daystr) or True)
    # Any launch path at all is a hard failure of this contract.
    monkeypatch.setattr(
        fdl.gw, "ensure_gateway",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("ensure_gateway must NEVER be called — see conductor #82")))

    rc = fdl.main()

    assert rc == 1
    assert emailed, "a down gateway must send the skip alert"


def test_gateway_up_probe_is_a_plain_tcp_probe(monkeypatch):
    """gateway_up() must not open an API session or launch anything — just a socket."""
    calls: list[tuple] = []

    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(fdl.socket, "create_connection",
                        lambda addr, timeout=None: calls.append((addr, timeout)) or _Sock())
    assert fdl.gateway_up() is True
    assert calls and calls[0][0] == (fdl.gw.HOST, fdl.gw.LIVE_TRADE_PORT)

    def _refused(addr, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(fdl.socket, "create_connection", _refused)
    assert fdl.gateway_up() is False, "a refused connection means down, not an exception"


def test_weekend_exits_zero(monkeypatch):
    monkeypatch.setattr(fdl, "date", _FakeDate(WEEKEND))
    # the gateway must never even be probed on a weekend.
    monkeypatch.setattr(fdl, "gateway_up",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached")))
    writes = _capture_status(monkeypatch)

    rc = fdl.main()

    assert rc == 0
    assert writes[-1][:2] == ("forward", "ok")


def test_market_holiday_exits_zero(monkeypatch):
    # New Year's Day 2026-01-01 (Thu) is a full-day closure per the shared calendar.
    holiday = datetime.date(2026, 1, 1)
    monkeypatch.setattr(fdl, "date", _FakeDate(holiday))
    monkeypatch.setattr(fdl, "gateway_up",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached")))
    writes = _capture_status(monkeypatch)

    rc = fdl.main()

    assert rc == 0
    assert writes[-1][:2] == ("forward", "ok")


class _FakeIB:
    def isConnected(self):
        return True

    def disconnect(self):
        pass


def _run_trading_day(monkeypatch, tmp_path, per_root):
    """Drive main() on a trading day with a fake gateway/connect and a stubbed
    collect_day whose (status, n) result is looked up per root from `per_root`."""
    monkeypatch.setattr(fdl, "date", _FakeDate(TRADING_DAY))
    monkeypatch.setattr(fdl, "gateway_up", lambda *a, **k: True)
    monkeypatch.setattr(fdl, "_connect", lambda real_errors: _FakeIB())
    # Keep the heartbeat write off the real warehouse file.
    monkeypatch.setattr(fdl, "HEARTBEAT", tmp_path / "forward_heartbeat_live.txt")
    writes = _capture_status(monkeypatch)

    def fake_collect_day(ib, sym, daystr, band=None, max_exps=None):
        st = per_root[sym]
        if st == "raise":
            raise RuntimeError("boom")
        return st, (10 if st == "ok" else 0)

    monkeypatch.setattr(fdl.fwd, "collect_day", fake_collect_day)
    # Pin the universe to the roots we defined.
    import sys as _sys
    monkeypatch.setattr(_sys, "argv", ["forward_daily_live.py", *per_root.keys()])
    return fdl.main(), writes


def test_all_roots_ok_exits_zero(monkeypatch, tmp_path):
    rc, writes = _run_trading_day(monkeypatch, tmp_path, {"SPX": "ok", "SPXW": "ok"})
    assert rc == 0
    assert writes[-1][:2] == ("forward", "ok")


def test_every_root_fails_exits_nonzero(monkeypatch, tmp_path):
    # Both roots raise on both attempts -> ok=0, fail=2 -> overall "fail" -> exit 1.
    rc, writes = _run_trading_day(monkeypatch, tmp_path, {"SPX": "raise", "SPXW": "raise"})
    assert rc == 1
    assert writes[-1][:2] == ("forward", "fail")
