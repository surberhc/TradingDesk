"""
test_rebalance_gateway_lock.py — Slice 3: the rebalance RUN and EXECUTE paths ACQUIRE the
gateway lock and WAIT-then-REFUSE when the Gateway is busy.

Proves OFFLINE (NO broker, NO real gateway, NO network, NO transmit):
  * rebalance_run.main / rebalance_execute.execute_armed, when the lock is HELD by a live,
    non-stale holder, REFUSE: they catch GatewayBusyRefuse, print the holder info
    (purpose/pid/clientId/since), and ABORT *before* any connect / order build / replaceFA —
    returning 2. No broker is touched, no order object is built, nothing is "transmitted",
    no FA config is written.
  * when the lock is FREE, both proceed PAST the guard (they reach the connect step). A
    sentinel raised at connect proves the guard was passed without a real gateway.

The broker is fully mocked/injected: ibkr_paper.connect (run) and the IB() class (execute) are
monkeypatched; a held lock is simulated by writing a live lock record to a TEMP lock path
(never the real STATE_DIR / Drive). Time/sleep are injected so the bounded refuse-wait never
sleeps for real.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_rebalance_gateway_lock.py -q
"""
from __future__ import annotations

import json
import os

import pytest

import config
import gateway_lock as gl
import rebalance_execute as rx
import rebalance_run as rr


# --- lock-on-temp-path injection (shared) --------------------------------------
def _advancing_clock():
    clock = {"t": 0.0}

    def now():
        clock["t"] += 0.05
        return clock["t"]

    return now


def _install_lock_on_tmp_path(module, monkeypatch, tmp_path, *, held: bool):
    """Make `module.gateway_lock` build a lock on a TEMP path with fast/offline timing. If
    `held`, pre-write a LIVE (our-pid, fresh-heartbeat) holder so the acquirer must
    wait-then-refuse; otherwise the path is free."""
    path = os.path.join(str(tmp_path), "gateway.lock")
    real = gl.gateway_lock

    def patched(purpose, client_id, on_busy="skip", wait_secs=None, **kw):
        kw.setdefault("lock_path", path)
        kw.setdefault("poll_interval", 0.001)
        kw.setdefault("heartbeat_interval", 1000.0)
        kw.setdefault("sleep_fn", lambda s: None)
        if held:
            kw.setdefault("pid_alive", lambda pid: True)
            kw.setdefault("now_fn", _advancing_clock())
        if wait_secs is None:
            wait_secs = 0.05
        return real(purpose, client_id, on_busy=on_busy, wait_secs=wait_secs, **kw)

    monkeypatch.setattr(module, "gateway_lock", patched)
    if held:
        _write_live_holder(path)
    return path


def _write_live_holder(path, **fields):
    rec = {
        "pid": os.getpid(), "client_id": 40, "purpose": "monitor",
        "host": "TEST", "acquired_ts": 1000.0, "acquired_at": "2026-06-30T14:00:00",
        "heartbeat_ts": 9_999_999_999.0, "heartbeat_at": "2026-06-30T14:00:00",
    }
    rec.update(fields)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec))


class _ConnectReached(Exception):
    """Sentinel raised at the connect step to prove a FREE-lock run got PAST the guard."""


def _stub_targets(monkeypatch):
    """Avoid loading real strategy data for the tier-model compute on both paths."""
    from types import SimpleNamespace
    import pandas as pd
    fake = SimpleNamespace(
        as_of=pd.Timestamp("2026-06-30"), price_date=pd.Timestamp("2026-06-30"),
        weights=pd.Series({"SPY": 1.0}), prices=pd.Series({"SPY": 100.0}))
    monkeypatch.setattr(rr, "_targets_by_version", lambda: {"Balanced": fake})


# --- rebalance_run.main --------------------------------------------------------
def test_run_refuses_when_lock_held(monkeypatch, tmp_path, capsys):
    _stub_targets(monkeypatch)
    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "DRY_RUN", True)
    _install_lock_on_tmp_path(rr, monkeypatch, tmp_path, held=True)

    # If main() tries to connect while the lock is held, blow up: refuse must abort first.
    def _boom_connect(*a, **k):
        raise AssertionError("rebalance_run must NOT connect while the gateway lock is held!")
    monkeypatch.setattr(rr.ibkr_paper, "connect", _boom_connect)

    rc = rr.main()
    assert rc == 2                                    # refused
    out = capsys.readouterr().out
    assert "REFUSING to start" in out
    assert "monitor" in out                           # named the holder
    assert "clientId 40" in out
    assert "NO orders built" in out


def test_run_passes_guard_when_lock_free(monkeypatch, tmp_path):
    _stub_targets(monkeypatch)
    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "DRY_RUN", True)
    path = _install_lock_on_tmp_path(rr, monkeypatch, tmp_path, held=False)

    held_at_connect = {"v": None}

    def _connect(*a, **k):
        held_at_connect["v"] = os.path.exists(path)   # lock must be held when we reach connect
        raise RuntimeError("stub: no real gateway")   # main() turns this into a clean return 1
    monkeypatch.setattr(rr.ibkr_paper, "connect", _connect)

    rc = rr.main()
    assert held_at_connect["v"] is True               # got PAST the guard, lock held at connect
    assert rc == 1                                     # connect was reached (its own error path)
    # released after the session unwound through the context manager's __exit__
    assert not os.path.exists(path)


# --- rebalance_execute.execute_armed -------------------------------------------
class _BoomIB:
    """An IB() that must never be constructed/connected on a refused run."""
    def __init__(self):
        raise AssertionError("execute_armed must NOT construct IB()/connect while lock held!")


def test_execute_refuses_when_lock_held(monkeypatch, tmp_path, capsys):
    _stub_targets(monkeypatch)
    _install_lock_on_tmp_path(rx, monkeypatch, tmp_path, held=True)

    # Guard every order-work seam: none may be reached on a refusal.
    monkeypatch.setattr(rx, "IB", _BoomIB)

    def _boom_fa(*a, **k):
        raise AssertionError("execute_armed must NOT write FA config on a refused run!")
    monkeypatch.setattr(rx, "set_group_contracts_or_shares", _boom_fa)
    monkeypatch.setattr(rx, "backup_fa_groups", _boom_fa)

    def _boom_place(*a, **k):
        raise AssertionError("execute_armed must NOT place orders on a refused run!")
    monkeypatch.setattr(rx.order_router, "place", _boom_place)

    rc = rx.execute_armed(armed=True)
    assert rc == 2                                    # refused
    out = capsys.readouterr().out
    assert "REFUSING to start" in out
    assert "monitor" in out                           # named the holder
    assert "clientId 40" in out
    assert "no replaceFA" in out


def test_execute_passes_guard_when_lock_free(monkeypatch, tmp_path):
    _stub_targets(monkeypatch)
    path = _install_lock_on_tmp_path(rx, monkeypatch, tmp_path, held=False)

    held_at_connect = {"v": None}

    class _SentinelIB:
        def connect(self, *a, **k):
            held_at_connect["v"] = os.path.exists(path)   # lock held when connect is reached
            raise _ConnectReached()                       # stop before a real session
    monkeypatch.setattr(rx, "IB", _SentinelIB)

    with pytest.raises(_ConnectReached):
        rx.execute_armed(armed=True)
    assert held_at_connect["v"] is True               # got PAST the guard, lock held at connect
    assert not os.path.exists(path)                   # released on the (sentinel) unwind


def test_execute_releases_lock_on_exception(monkeypatch, tmp_path):
    """The context manager must release on an exception raised inside the held session, so a
    crashed armed run never leaves a poisoned lock (a follow-on acquire then succeeds)."""
    _stub_targets(monkeypatch)
    path = _install_lock_on_tmp_path(rx, monkeypatch, tmp_path, held=False)

    class _RaisingIB:
        def connect(self, *a, **k):
            raise RuntimeError("simulated connect failure inside the held session")
    monkeypatch.setattr(rx, "IB", _RaisingIB)

    with pytest.raises(RuntimeError):
        rx.execute_armed(armed=True)
    assert not os.path.exists(path)                   # lock freed despite the in-body raise

    # A follow-on acquire on the same path succeeds (proves it was truly released).
    with gl.gateway_lock("monitor", 40, lock_path=path, poll_interval=0.001,
                         heartbeat_interval=1000.0, sleep_fn=lambda s: None):
        assert os.path.exists(path)
    assert not os.path.exists(path)
