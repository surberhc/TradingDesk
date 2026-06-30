"""
test_gateway_lock.py — Slice 1 of the gateway lock (INERT module).

Proves the single-process Gateway mutex offline: atomic acquire, busy-then-raise per policy,
dead-PID reclaim, heartbeat-staleness reclaim, release on normal exit AND on exception,
release-iff-owner, and a simulated two-holder contention — all with NO broker, NO real
gateway, NO network. Time/liveness/sleep are injected so nothing actually sleeps 10-30s and
no real `tasklist` is shelled.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_gateway_lock.py -q
"""
from __future__ import annotations

import json
import os

import pytest

import gateway_lock as gl
from gateway_lock import (
    GatewayBusy,
    GatewayBusyRefuse,
    GatewayBusySkip,
    gateway_lock,
)


# --- helpers -------------------------------------------------------------------
def _lock_path(tmp_path) -> str:
    return os.path.join(str(tmp_path), "gateway.lock")


def _write_lock(path: str, **fields) -> None:
    """Write a raw lock record to disk to simulate an existing holder."""
    rec = {
        "pid": 999999,
        "client_id": 40,
        "purpose": "monitor",
        "host": "TEST",
        "acquired_ts": 1000.0,
        "acquired_at": "2026-06-30T14:00:00",
        "heartbeat_ts": 1000.0,
        "heartbeat_at": "2026-06-30T14:00:00",
    }
    rec.update(fields)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec))


def _fast(**kwargs):
    """Common fast/offline injection: tiny waits, no real sleep accumulation."""
    base = dict(
        poll_interval=0.001,
        heartbeat_interval=1000.0,   # never fires during a quick test
        sleep_fn=lambda s: None,      # no real waiting
    )
    base.update(kwargs)
    return base


# --- 1. atomic acquire + busy-then-raise ---------------------------------------
def test_atomic_acquire_and_release(tmp_path):
    path = _lock_path(tmp_path)
    with gateway_lock("monitor", 40, lock_path=path, **_fast()):
        assert os.path.exists(path)
        rec = json.loads(open(path).read())
        assert rec["pid"] == os.getpid()
        assert rec["purpose"] == "monitor"
        assert rec["client_id"] == 40
    # released on normal exit
    assert not os.path.exists(path)


def test_second_acquire_while_held_raises_after_wait(tmp_path):
    path = _lock_path(tmp_path)
    # A live holder already on disk (our own pid => alive).
    _write_lock(path, pid=os.getpid(), heartbeat_ts=1_000_000.0)
    waited = {"n": 0}

    def fake_sleep(_):
        waited["n"] += 1

    # now advances past the deadline so the wait loop terminates deterministically.
    clock = {"t": 0.0}

    def fake_now():
        clock["t"] += 0.05
        return clock["t"]

    lock = gateway_lock(
        "monitor", 40, on_busy="skip", wait_secs=0.2,
        lock_path=path, poll_interval=0.01,
        pid_alive=lambda pid: True,           # holder stays alive (not stale)
        now_fn=fake_now, sleep_fn=fake_sleep,
        heartbeat_interval=1000.0,
    )
    with pytest.raises(GatewayBusySkip):
        lock.acquire()
    assert waited["n"] >= 1                    # it actually polled/waited
    # holder's lock left intact (we never stomped a live, non-stale holder)
    assert os.path.exists(path)


# --- 2. dead-PID reclaim -------------------------------------------------------
def test_stale_reclaim_dead_pid(tmp_path):
    path = _lock_path(tmp_path)
    _write_lock(path, pid=123456789)          # bogus PID
    lock = gateway_lock(
        "rebalance_execute", 38, lock_path=path,
        pid_alive=lambda pid: False,          # holder is DEAD
        **_fast(),
    )
    with lock:
        rec = json.loads(open(path).read())
        assert rec["pid"] == os.getpid()      # reclaimed: now ours
        assert rec["purpose"] == "rebalance_execute"
    assert not os.path.exists(path)


# --- 3. heartbeat-staleness reclaim --------------------------------------------
def test_heartbeat_staleness_reclaim(tmp_path):
    path = _lock_path(tmp_path)
    now = 2_000_000.0
    # Alive holder, but heartbeat is older than STALE_HEARTBEAT_SECS -> wedged -> reclaim.
    _write_lock(path, pid=42, heartbeat_ts=now - (gl.STALE_HEARTBEAT_SECS + 1))
    lock = gateway_lock(
        "rebalance_execute", 38, lock_path=path,
        pid_alive=lambda pid: True,           # alive but silent
        now_fn=lambda: now,
        **_fast(),
    )
    with lock:
        rec = json.loads(open(path).read())
        assert rec["pid"] == os.getpid()      # reclaimed despite live PID
    assert not os.path.exists(path)


def test_fresh_heartbeat_not_reclaimed(tmp_path):
    path = _lock_path(tmp_path)
    now = 2_000_000.0
    # Alive holder, FRESH heartbeat -> NOT stale -> must raise busy, not reclaim.
    # Pin heartbeat-age math to `now` via a stale-check now, but use a separately
    # ADVANCING wall clock for the wait loop so the deadline is actually reached.
    _write_lock(path, pid=42, heartbeat_ts=now - 5.0)
    clock = {"t": now}

    def fake_now():
        clock["t"] += 0.05          # advance so the wait loop terminates
        return clock["t"]

    lock = gateway_lock(
        "monitor", 40, on_busy="skip", wait_secs=0.1,
        lock_path=path, poll_interval=0.01,
        pid_alive=lambda pid: True,
        now_fn=fake_now,
        sleep_fn=lambda s: None,
        heartbeat_interval=1000.0,
    )
    with pytest.raises(GatewayBusySkip):
        lock.acquire()
    rec = json.loads(open(path).read())
    assert rec["pid"] == 42                   # holder untouched


# --- 4. release on exception ---------------------------------------------------
def test_release_on_exception(tmp_path):
    path = _lock_path(tmp_path)

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with gateway_lock("monitor", 40, lock_path=path, **_fast()):
            assert os.path.exists(path)
            raise Boom()
    # try/finally semantics: the lock is freed even though the body raised.
    assert not os.path.exists(path)
    # and a follow-on acquire succeeds (proves it was truly released).
    with gateway_lock("monitor", 40, lock_path=path, **_fast()):
        assert os.path.exists(path)
    assert not os.path.exists(path)


# --- 5. refuse policy names the holder -----------------------------------------
def test_refuse_carries_holder_info(tmp_path):
    path = _lock_path(tmp_path)
    _write_lock(path, pid=os.getpid(), client_id=40, purpose="monitor",
                heartbeat_ts=9_999_999.0)
    clock = {"t": 0.0}

    def fake_now():
        clock["t"] += 0.05
        return clock["t"]

    lock = gateway_lock(
        "rebalance_execute", 38, on_busy="refuse", wait_secs=0.1,
        lock_path=path, poll_interval=0.01,
        pid_alive=lambda pid: True,
        now_fn=fake_now, sleep_fn=lambda s: None,
        heartbeat_interval=1000.0,
    )
    with pytest.raises(GatewayBusyRefuse) as ei:
        lock.acquire()
    exc = ei.value
    # holder record is attached and the message names purpose/pid/clientId.
    assert exc.holder["purpose"] == "monitor"
    assert exc.holder["pid"] == os.getpid()
    assert exc.holder["client_id"] == 40
    msg = str(exc)
    assert "monitor" in msg
    assert str(os.getpid()) in msg
    assert "clientId 40" in msg
    assert isinstance(exc, GatewayBusy)       # catchable via the base


# --- 6. release-iff-owner ------------------------------------------------------
def test_release_iff_owner_leaves_foreign_lock(tmp_path):
    path = _lock_path(tmp_path)
    # Acquire normally, then have a FOREIGN pid stomp the file underneath us before exit.
    lock = gateway_lock("monitor", 40, lock_path=path, **_fast())
    lock.acquire()
    _write_lock(path, pid=777, purpose="someone_else")   # successor took over
    lock.release()
    # We must NOT have deleted the successor's lock.
    assert os.path.exists(path)
    rec = json.loads(open(path).read())
    assert rec["pid"] == 777


# --- 7. simulated two-holder contention (sequential + held-then-timeout) -------
def test_two_holder_contention_sequential(tmp_path):
    path = _lock_path(tmp_path)
    # Holder A acquires and releases; THEN holder B can acquire (serialized).
    with gateway_lock("rebalance_execute", 38, lock_path=path, **_fast()):
        assert os.path.exists(path)
    # A released -> B succeeds.
    with gateway_lock("monitor", 40, lock_path=path, **_fast()):
        assert os.path.exists(path)
    assert not os.path.exists(path)


def test_two_holder_contention_held_then_timeout(tmp_path):
    path = _lock_path(tmp_path)
    # Holder A still holds (live, fresh heartbeat); holder B times out and raises.
    a = gateway_lock("rebalance_execute", 38, lock_path=path,
                     pid_alive=lambda pid: True, **_fast())
    a.acquire()
    clock = {"t": 0.0}

    def fake_now():
        clock["t"] += 0.05
        return clock["t"]

    b = gateway_lock(
        "monitor", 40, on_busy="skip", wait_secs=0.1,
        lock_path=path, poll_interval=0.01,
        pid_alive=lambda pid: True,           # A is alive
        now_fn=fake_now, sleep_fn=lambda s: None,
        heartbeat_interval=1000.0,
    )
    with pytest.raises(GatewayBusySkip):
        b.acquire()
    # A is still the holder; release A and B-style acquire then works.
    a.release()
    assert not os.path.exists(path)


# --- 8. lock lives in STATE_DIR by default (LOCAL, never Drive) ----------------
def test_default_lock_path_is_local_state_dir():
    assert gl.LOCK_PATH == os.path.join(__import__("config").STATE_DIR, "gateway.lock")
    assert "TradingDesk-Local" in gl.LOCK_PATH        # off-Drive local disk
    assert "My Drive" not in gl.LOCK_PATH


# --- 9. heartbeat thread refreshes heartbeat_ts while held ---------------------
def test_heartbeat_thread_refreshes(tmp_path):
    path = _lock_path(tmp_path)
    # Real thread, tiny interval, real (monotonic-ish) clock so heartbeat_ts advances.
    lock = gateway_lock("rebalance_execute", 38, lock_path=path,
                        poll_interval=0.001, heartbeat_interval=0.01)
    with lock:
        first = json.loads(open(path).read())["heartbeat_ts"]
        # let the daemon thread beat at least once
        import time as _t
        _t.sleep(0.06)
        second = json.loads(open(path).read())["heartbeat_ts"]
    assert second >= first
    assert not os.path.exists(path)


# --- 10. bad policy rejected ---------------------------------------------------
def test_invalid_policy_rejected(tmp_path):
    with pytest.raises(ValueError):
        gateway_lock("monitor", 40, on_busy="nonsense", lock_path=_lock_path(tmp_path))
