"""test_s8_lock.py — OFFLINE tests for the SINGLE-INSTANCE / ORPHAN GUARD and the
TRADING-DAY GUARD (both go-live dry-run findings).

100% offline: NO broker, NO gateway, NO network, NO real processes killed. The pid-liveness
check, the command-line lookup and the kill callable are all injected fakes; the market
calendar is a fake callable. The lock files live under a pytest tmp_path.

What is proved:
  LOCK
    * acquires when free;
    * RECLAIMS a stale lock whose holder PID is dead;
    * TERMINATES and takes over a LIVE holder whose command line matches our script;
    * SAFETY — REFUSES to kill (and refuses to start against) a LIVE holder whose command
      line does NOT match our script (PID reuse / unrelated process / unreadable cmdline);
    * releases on exit, and only if the record is still ours.
  TRADING DAY
    * a non-trading day skips entries and logs EXACTLY once (not per cycle);
    * a trading day runs entries normally;
    * a calendar EXCEPTION FAILS OPEN (entries proceed) with a logged fallback.

Run (from C:\\TradingDesk\\livebot):
    powershell -Command "$env:PYTHONPATH=''; C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s8_lock.py -q"
"""

from __future__ import annotations

import datetime as dt
import json
import os

import pytest

import s8_lock
import s8_service
from s8_lock import ACQUIRE, RECLAIM, REFUSE, TAKE_OVER, SingleInstanceLock


MARKER = "s8_service.py"
MY_PID = 4242
HOLDER = 9999


def _write_lock(path, pid):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "started_at": 1.0, "host": "h"}))


class _Killer:
    """Records every kill request instead of touching a real process."""

    def __init__(self, succeed=True):
        self.calls = []
        self.succeed = succeed

    def __call__(self, pid):
        self.calls.append(int(pid))
        return self.succeed


def _lock(path, *, alive, cmdline, kill, logs):
    return SingleInstanceLock(
        "s8_service", MARKER, path=path, my_pid=MY_PID,
        is_alive=lambda p: alive, get_cmdline=lambda p: cmdline, kill=kill,
        log=logs.append, settle_secs=0.0, sleep=lambda s: None,
    )


# --------------------------------------------------------------------------- #
# PURE decision seam
# --------------------------------------------------------------------------- #

def test_decide_no_record_is_reclaim():
    action, _ = s8_lock.decide_lock_action(
        None, MARKER, my_pid=MY_PID, is_alive=lambda p: True,
        get_cmdline=lambda p: MARKER)
    assert action == RECLAIM


def test_decide_dead_holder_is_reclaim():
    action, reason = s8_lock.decide_lock_action(
        {"pid": HOLDER}, MARKER, my_pid=MY_PID, is_alive=lambda p: False,
        get_cmdline=lambda p: MARKER)
    assert action == RECLAIM and "not running" in reason


def test_decide_live_matching_holder_is_take_over():
    action, reason = s8_lock.decide_lock_action(
        {"pid": HOLDER}, MARKER, my_pid=MY_PID, is_alive=lambda p: True,
        get_cmdline=lambda p: r"C:\py.exe C:\TradingDesk\livebot\S8_SERVICE.PY")
    assert action == TAKE_OVER
    assert "terminating stale orphan and taking over" in reason


def test_decide_live_nonmatching_holder_is_refuse():
    action, reason = s8_lock.decide_lock_action(
        {"pid": HOLDER}, MARKER, my_pid=MY_PID, is_alive=lambda p: True,
        get_cmdline=lambda p: r"C:\Windows\explorer.exe")
    assert action == REFUSE and "refusing to kill" in reason


def test_decide_unreadable_cmdline_is_refuse_never_kill():
    """An unidentifiable live process must NEVER be treated as ours."""
    action, _ = s8_lock.decide_lock_action(
        {"pid": HOLDER}, MARKER, my_pid=MY_PID, is_alive=lambda p: True,
        get_cmdline=lambda p: None)
    assert action == REFUSE


def test_decide_our_own_pid_is_acquire():
    action, _ = s8_lock.decide_lock_action(
        {"pid": MY_PID}, MARKER, my_pid=MY_PID, is_alive=lambda p: True,
        get_cmdline=lambda p: MARKER)
    assert action == ACQUIRE


# --------------------------------------------------------------------------- #
# LOCK acquire / reclaim / take-over / refuse / release
# --------------------------------------------------------------------------- #

def test_acquire_when_free(tmp_path):
    p = tmp_path / "state" / "s8_service.lock"
    kill, logs = _Killer(), []
    lk = _lock(p, alive=False, cmdline=None, kill=kill, logs=logs)
    assert lk.acquire() is True
    assert lk.held is True
    assert json.loads(p.read_text())["pid"] == MY_PID
    assert kill.calls == []          # nothing was killed
    lk.release()
    assert not p.exists()


def test_reclaims_stale_dead_holder(tmp_path):
    p = tmp_path / "state" / "s8_service.lock"
    _write_lock(p, HOLDER)
    kill, logs = _Killer(), []
    lk = _lock(p, alive=False, cmdline=MARKER, kill=kill, logs=logs)
    assert lk.acquire() is True
    assert json.loads(p.read_text())["pid"] == MY_PID
    assert kill.calls == []          # a DEAD pid is never "killed"
    assert any("stale lock found" in m for m in logs)


def test_terminates_and_takes_over_live_matching_holder(tmp_path):
    p = tmp_path / "state" / "s8_service.lock"
    _write_lock(p, HOLDER)
    kill, logs = _Killer(succeed=True), []
    lk = _lock(p, alive=True,
               cmdline=r"C:\python.exe C:\TradingDesk\livebot\s8_service.py",
               kill=kill, logs=logs)
    assert lk.acquire() is True
    assert kill.calls == [HOLDER]
    assert json.loads(p.read_text())["pid"] == MY_PID
    assert any("found live prior instance pid=9999" in m for m in logs)


def test_refuses_to_kill_live_nonmatching_pid(tmp_path):
    """SAFETY: a live holder that is NOT our script is never killed, and we do not start."""
    p = tmp_path / "state" / "s8_service.lock"
    _write_lock(p, HOLDER)
    kill, logs = _Killer(), []
    lk = _lock(p, alive=True, cmdline=r"C:\Windows\System32\notepad.exe", kill=kill,
               logs=logs)
    assert lk.acquire() is False
    assert kill.calls == []                                   # nothing killed
    assert lk.held is False
    assert json.loads(p.read_text())["pid"] == HOLDER         # holder's lock untouched
    assert any("refusing to kill an unrelated process" in m for m in logs)


def test_take_over_aborts_if_the_kill_fails(tmp_path):
    p = tmp_path / "state" / "s8_service.lock"
    _write_lock(p, HOLDER)
    kill, logs = _Killer(succeed=False), []
    lk = _lock(p, alive=True, cmdline=MARKER, kill=kill, logs=logs)
    assert lk.acquire() is False
    assert kill.calls == [HOLDER]
    assert lk.held is False


def test_release_does_not_remove_someone_elses_lock(tmp_path):
    p = tmp_path / "state" / "s8_service.lock"
    kill, logs = _Killer(), []
    lk = _lock(p, alive=False, cmdline=None, kill=kill, logs=logs)
    assert lk.acquire() is True
    _write_lock(p, HOLDER)           # someone else took it over while we ran
    lk.release()
    assert p.exists() and json.loads(p.read_text())["pid"] == HOLDER


def test_default_lock_paths_are_separate_and_off_drive(tmp_path, monkeypatch):
    monkeypatch.setenv("S8_PILOT_ROOT", str(tmp_path))
    svc = s8_lock.default_lock_path("s8_service")
    col = s8_lock.default_lock_path("s8_collector")
    assert svc != col
    assert svc.parent == tmp_path / "state"
    assert "My Drive" not in str(svc)


def test_cmdline_matches_is_conservative():
    assert s8_lock.cmdline_matches("python S8_Service.PY", "s8_service.py") is True
    assert s8_lock.cmdline_matches(None, "s8_service.py") is False
    assert s8_lock.cmdline_matches("", "s8_service.py") is False
    assert s8_lock.cmdline_matches("python s8_collector.py", "s8_service.py") is False


# --------------------------------------------------------------------------- #
# TRADING-DAY GUARD
# --------------------------------------------------------------------------- #

HOLIDAY = dt.date(2026, 7, 3)      # Independence Day (observed) — real NYSE closure
OPEN_DAY = dt.date(2026, 7, 2)


def test_resolve_trading_day_uses_the_real_desk_calendar():
    """Sanity-check against the desk's actual market_calendar (no fake injected)."""
    assert s8_service.resolve_trading_day(HOLIDAY) is False
    assert s8_service.resolve_trading_day(OPEN_DAY) is True


def test_resolve_trading_day_fails_open_on_calendar_error():
    logs = []

    def boom(_d):
        raise RuntimeError("calendar table missing")

    assert s8_service.resolve_trading_day(HOLIDAY, is_trading_day=boom,
                                          log=logs.append) is True
    assert any("FAILING OPEN" in m for m in logs)


def _service():
    svc = s8_service.S8Service.__new__(s8_service.S8Service)   # no IB, no monitor needed
    svc._trading_day_checked = False
    svc._entries_enabled = True
    return svc


def test_non_trading_day_skips_entries_and_logs_once(capsys):
    svc = _service()
    for _ in range(5):   # five cycles, as the live loop would
        assert svc._ensure_trading_day_checked(HOLIDAY, lambda d: False) is False
    out = capsys.readouterr().out
    assert out.count("is NOT a trading day") == 1     # ONCE, not per cycle
    # And entry_cycle short-circuits before touching the gateway at all.
    exploding_ib = object()   # any attribute access would AttributeError
    svc.account = "DU1"
    assert svc.entry_cycle(exploding_ib) == []


def test_trading_day_allows_entries():
    svc = _service()
    for _ in range(3):
        assert svc._ensure_trading_day_checked(OPEN_DAY, lambda d: True) is True
    assert svc._entries_enabled is True


def test_calendar_exception_fails_open_and_entries_proceed(capsys):
    svc = _service()

    def boom(_d):
        raise RuntimeError("no table for this year")

    assert svc._ensure_trading_day_checked(HOLIDAY, boom) is True
    out = capsys.readouterr().out
    assert "FAILING OPEN" in out
    assert "is NOT a trading day" not in out
