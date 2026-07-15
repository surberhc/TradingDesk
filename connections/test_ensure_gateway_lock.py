"""Tests for the NARROW launch mutex in connections/ibkr_paper.py::ensure_gateway.

Offline: no real gateway, no network. We monkeypatch gateway_running, the PID
liveness helper, time.sleep (no-op), and point the lock path at pytest tmp_path.
Popen is replaced by a spy so we can assert exactly how many launches happened.

The bug being guarded: a wedged login made a per-symbol reconnect loop call
ensure_gateway() repeatedly, and the old code Popen'd a fresh gateway every time,
stacking ~91 dead gateways. The mutex must guarantee at most ONE launch in flight.
"""
import json
import os

import pytest

from connections import ibkr_paper


class PopenSpy:
    """Records every subprocess.Popen call so tests can assert call_count."""
    def __init__(self):
        self.call_count = 0
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        self.calls.append((args, kwargs))
        return self  # a harmless stand-in "process" object


@pytest.fixture
def lockpath(tmp_path, monkeypatch):
    """Point the launch lock at a tmp dir (parent exists; makedirs is a no-op)."""
    p = tmp_path / "state" / "paperbot" / "gateway_launch.lock"
    monkeypatch.setattr(ibkr_paper, "GATEWAY_LAUNCH_LOCK", str(p))
    return p


@pytest.fixture
def spy(monkeypatch):
    s = PopenSpy()
    monkeypatch.setattr(ibkr_paper.subprocess, "Popen", s)
    return s


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Make polling instant so tiny wait_secs iterate immediately."""
    monkeypatch.setattr(ibkr_paper.time, "sleep", lambda *_: None)


def _seq(monkeypatch, values):
    """gateway_running returns each value in turn, repeating the last forever."""
    box = {"i": 0}

    def fake():
        i = box["i"]
        box["i"] = min(i + 1, len(values) - 1)
        return values[i]

    monkeypatch.setattr(ibkr_paper, "gateway_running", fake)


def _write_record(path, *, pid, started_at, attempt_done_at=None, host="testhost"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "pid": pid, "started_at": started_at,
            "attempt_done_at": attempt_done_at, "host": host,
        }))


# ---------------------------------------------------------------------------
# a. Healthy gateway: returns True, NO Popen, NO lock file created.
# ---------------------------------------------------------------------------
def test_healthy_gateway_no_launch_no_lock(monkeypatch, lockpath, spy):
    monkeypatch.setattr(ibkr_paper, "gateway_running", lambda *a, **k: True)
    assert ibkr_paper.ensure_gateway(wait_secs=1) is True
    assert spy.call_count == 0
    assert not lockpath.exists()   # fast path never touched the filesystem


# ---------------------------------------------------------------------------
# b. Down gateway, single caller: exactly ONE Popen; flips up -> True;
#    lock released afterward.
# ---------------------------------------------------------------------------
def test_single_caller_launches_once_then_up(monkeypatch, lockpath, spy):
    # down on the pre-launch check, then down once more, then up.
    _seq(monkeypatch, [False, False, True])
    assert ibkr_paper.ensure_gateway(wait_secs=30) is True
    assert spy.call_count == 1
    # Clean release on SUCCESS: the lock file is UNLINKED (gateway is healthy, so
    # subsequent callers hit the fast path; a later down-event may relaunch freely).
    assert not lockpath.exists()


# ---------------------------------------------------------------------------
# c. Concurrent-ish: a live holder with a recent started_at is present ->
#    second caller does NOT Popen, it waits.
# ---------------------------------------------------------------------------
def test_second_caller_waits_when_launch_in_flight(monkeypatch, lockpath, spy):
    _write_record(lockpath, pid=4242, started_at=ibkr_paper.time.time())  # fresh, in-flight
    monkeypatch.setattr(ibkr_paper, "_pid_alive", lambda pid: True)       # holder is live
    # gateway stays down the whole (tiny) wait -> waiter returns False, never launches.
    _seq(monkeypatch, [False])
    assert ibkr_paper.ensure_gateway(wait_secs=1) is False
    assert spy.call_count == 0
    # We did not own the lock, so we must not have deleted the holder's record.
    assert lockpath.exists()


def test_second_caller_waits_then_sees_gateway_come_up(monkeypatch, lockpath, spy):
    _write_record(lockpath, pid=4242, started_at=ibkr_paper.time.time())
    monkeypatch.setattr(ibkr_paper, "_pid_alive", lambda pid: True)
    # down on pre-check, then the other launcher's gateway comes up.
    _seq(monkeypatch, [False, True])
    assert ibkr_paper.ensure_gateway(wait_secs=30) is True
    assert spy.call_count == 0


# ---------------------------------------------------------------------------
# d. Cooldown: a record with attempt_done_at = now (fresh) and gateway still
#    down -> new caller does NOT Popen (still cooling down).
# ---------------------------------------------------------------------------
def test_cooldown_blocks_relaunch(monkeypatch, lockpath, spy):
    now = ibkr_paper.time.time()
    # started_at is old (outside wait_secs) but attempt_done_at is fresh -> cooldown.
    _write_record(lockpath, pid=4242, started_at=now - 10_000, attempt_done_at=now)
    monkeypatch.setattr(ibkr_paper, "_pid_alive", lambda pid: True)
    _seq(monkeypatch, [False])
    assert ibkr_paper.ensure_gateway(wait_secs=1) is False
    assert spy.call_count == 0


# ---------------------------------------------------------------------------
# d2. REAL-FLOW cooldown: a single caller whose gateway stays DOWN through the
#     whole wait leaves a PERSISTED cooldown marker (lock still exists,
#     attempt_done_at set, started_at unchanged from acquire). A second real
#     caller within the cooldown window then refuses to Popen — driven by the
#     persisted marker, no hand-planted file.
# ---------------------------------------------------------------------------
def test_failed_launch_persists_cooldown_marker_and_blocks_next_caller(
        monkeypatch, lockpath, spy):
    # Freeze time so we control the cooldown window deterministically.
    fixed_now = 1_000_000.0
    monkeypatch.setattr(ibkr_paper.time, "time", lambda: fixed_now)
    # Holder pid on the marker will be ours -> keep it "alive". (Patch so the
    # liveness probe doesn't shell out to tasklist, which internally uses
    # subprocess.run->Popen and would otherwise be counted by the Popen spy.)
    monkeypatch.setattr(ibkr_paper, "_pid_alive", lambda pid: True)
    # First caller: down on pre-check and never comes up during the wait.
    _seq(monkeypatch, [False])
    assert ibkr_paper.ensure_gateway(wait_secs=20) is False
    assert spy.call_count == 1
    # The failed launch must leave a persisted cooldown marker.
    assert lockpath.exists()
    rec = json.loads(lockpath.read_text(encoding="utf-8"))
    assert rec["attempt_done_at"] == fixed_now         # cooldown started
    assert rec["started_at"] == fixed_now              # unchanged from acquire
    assert rec["pid"] == os.getpid()

    # Second caller a bit later but still INSIDE the cooldown window. Drive it off
    # the SAME persisted marker (no re-planting) -> must NOT relaunch.
    monkeypatch.setattr(ibkr_paper.time, "time",
                        lambda: fixed_now + ibkr_paper.RELAUNCH_COOLDOWN_SECS - 1)
    _seq(monkeypatch, [False])
    assert ibkr_paper.ensure_gateway(wait_secs=1) is False
    assert spy.call_count == 1                          # ZERO additional Popens
    assert lockpath.exists()                            # marker still standing


# ---------------------------------------------------------------------------
# d3. REAL-FLOW drip is bounded: after a failed launch leaves a marker, once
#     patched time advances PAST both the cooldown AND wait_secs (neither
#     in_flight nor cooling_down), a NEW caller reclaims the stale marker and
#     DOES Popen exactly once. Proves the drip isn't a permanent deadlock.
# ---------------------------------------------------------------------------
def test_cooldown_marker_is_reclaimed_after_window_expires(
        monkeypatch, lockpath, spy):
    fixed_now = 2_000_000.0
    monkeypatch.setattr(ibkr_paper.time, "time", lambda: fixed_now)
    # Patch liveness so the tasklist probe (subprocess.run->Popen) isn't counted by
    # the Popen spy. Return True: even a LIVE holder must be reclaimed once the
    # record is stale-by-age (neither in_flight nor cooling_down).
    monkeypatch.setattr(ibkr_paper, "_pid_alive", lambda pid: True)
    _seq(monkeypatch, [False])
    assert ibkr_paper.ensure_gateway(wait_secs=20) is False
    assert spy.call_count == 1
    assert lockpath.exists()

    # Advance past BOTH RELAUNCH_COOLDOWN_SECS and wait_secs so the marker is
    # neither in_flight nor cooling_down -> stale-by-age reclaim path fires.
    later = fixed_now + ibkr_paper.RELAUNCH_COOLDOWN_SECS + 100
    monkeypatch.setattr(ibkr_paper.time, "time", lambda: later)
    # This caller reclaims, launches, and its gateway comes up.
    _seq(monkeypatch, [False, True])
    assert ibkr_paper.ensure_gateway(wait_secs=30) is True
    assert spy.call_count == 2                          # exactly one MORE launch


# ---------------------------------------------------------------------------
# e. Stale/dead holder: PID reported dead -> reclaimed, caller becomes the
#    launcher, exactly ONE Popen.
# ---------------------------------------------------------------------------
def test_dead_holder_reclaimed_and_launches(monkeypatch, lockpath, spy):
    _write_record(lockpath, pid=999999, started_at=ibkr_paper.time.time())
    monkeypatch.setattr(ibkr_paper, "_pid_alive", lambda pid: False)      # holder dead
    _seq(monkeypatch, [False, False, True])
    assert ibkr_paper.ensure_gateway(wait_secs=30) is True
    assert spy.call_count == 1
    assert not lockpath.exists()   # we became owner and released


def test_stale_record_by_age_reclaimed_and_launches(monkeypatch, lockpath, spy):
    now = ibkr_paper.time.time()
    # Live holder, but record older than BOTH windows (no cooldown stamp) -> stale.
    _write_record(lockpath, pid=4242, started_at=now - 10_000, attempt_done_at=None)
    monkeypatch.setattr(ibkr_paper, "_pid_alive", lambda pid: True)
    _seq(monkeypatch, [False, True])
    assert ibkr_paper.ensure_gateway(wait_secs=30) is True
    assert spy.call_count == 1


# ---------------------------------------------------------------------------
# f. Timeout: down gateway that never comes up within a tiny wait_secs ->
#    False; still only ONE Popen; cooldown marker PERSISTED (not unlinked).
# ---------------------------------------------------------------------------
def test_timeout_returns_false_single_popen_marker_persisted(monkeypatch, lockpath, spy):
    _seq(monkeypatch, [False])   # never comes up
    assert ibkr_paper.ensure_gateway(wait_secs=20) is False
    assert spy.call_count == 1
    # Failure leaves a cooldown marker so the next caller cools down (not unlinked).
    assert lockpath.exists()
    rec = json.loads(lockpath.read_text(encoding="utf-8"))
    assert rec["attempt_done_at"] is not None


# ---------------------------------------------------------------------------
# g. Fail-safe: a lock error degrades to the waiter path (never Popen, never raise).
# ---------------------------------------------------------------------------
def test_lock_error_fails_safe_to_waiter(monkeypatch, lockpath, spy):
    _seq(monkeypatch, [False])

    def boom(*a, **k):
        raise PermissionError("simulated elevated/locked state dir")

    monkeypatch.setattr(ibkr_paper.os, "open", boom)
    # Must not raise, must not launch, must return False (gateway never up).
    assert ibkr_paper.ensure_gateway(wait_secs=1) is False
    assert spy.call_count == 0
