"""Tests for datacollector/theta_terminal_watchdog.py.

OFFLINE + FAST: no real terminal, no network, no PowerShell. Every test drives the
PURE decision function run_once(...) with an injected clock (now=...), an in-memory
state dict, and mocked healthy / already_running / launch_fn / log_fn. We assert on
the returned (new_state, action) and on how many times the mocks were called.

Mirrors connections/test_gateway_watchdog.py's style — same policy shape (grace
window, rolling-hour restart cap, alert-once) applied to the ThetaData terminal
instead of the IB gateway.
"""
import datetime as dt

import theta_terminal_watchdog as tw


class Counter:
    """A zero-arg callable that records call count and returns a fixed value."""
    def __init__(self, value=None):
        self.value = value
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        return self.value


def _epoch(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi).timestamp()


def _state(down_since=None, restarts=None, alerted=False):
    return {"down_since": down_since,
            "restarts": list(restarts or []),
            "alerted": alerted}


def _run(now, healthy_value, state, already_running_value=False, launch=None):
    launch = launch or Counter(value=True)
    healthy = Counter(value=healthy_value)
    already_running = Counter(value=already_running_value)
    logs = []
    new_state, action = tw.run_once(
        now=now, healthy=healthy, already_running=already_running,
        state=state, launch_fn=launch, log_fn=logs.append,
    )
    return new_state, action, healthy, already_running, launch


NOON = _epoch(2026, 7, 10, 12, 0)


def test_healthy_clears_down_and_does_nothing():
    st = _state(down_since=NOON - 100)
    new, action, healthy, already_running, launch = _run(NOON, True, st)
    assert action == "healthy"
    assert new["down_since"] is None
    assert launch.calls == 0


def test_down_first_seen_starts_grace_no_restart():
    st = _state(down_since=None)
    new, action, healthy, already_running, launch = _run(NOON, False, st)
    assert action == "grace_started"
    assert new["down_since"] == NOON
    assert launch.calls == 0


def test_down_within_grace_no_restart():
    st = _state(down_since=NOON - (tw.GRACE_SECS - 30))   # 30s short of grace
    new, action, healthy, already_running, launch = _run(NOON, False, st)
    assert action == "within_grace"
    assert new["down_since"] == NOON - (tw.GRACE_SECS - 30)   # unchanged
    assert launch.calls == 0


def test_wedged_under_limit_restarts_once_and_records():
    st = _state(down_since=NOON - tw.GRACE_SECS, restarts=[])
    new, action, healthy, already_running, launch = _run(
        NOON, False, st, already_running_value=False, launch=Counter(True))
    assert action == "restarted"
    assert launch.calls == 1
    assert len(new["restarts"]) == 1 and new["restarts"][0] == NOON
    assert new["alerted"] is False


def test_wedged_launch_fails_still_counts_restart():
    st = _state(down_since=NOON - tw.GRACE_SECS, restarts=[])
    new, action, healthy, already_running, launch = _run(
        NOON, False, st, already_running_value=False, launch=Counter(False))
    assert action == "restart_failed"
    assert launch.calls == 1
    assert len(new["restarts"]) == 1   # still counted against the limit


def test_grace_expired_but_already_booting_does_not_double_launch():
    st = _state(down_since=NOON - tw.GRACE_SECS, restarts=[])
    new, action, healthy, already_running, launch = _run(
        NOON, False, st, already_running_value=True, launch=Counter(True))
    assert action == "booting"
    assert launch.calls == 0   # never launched a second terminal
    assert already_running.calls == 1


def test_rate_limit_blocks_restart_and_alerts():
    recent = [NOON - 2400, NOON - 1500, NOON - 600]   # 40m, 25m, 10m ago
    st = _state(down_since=NOON - tw.GRACE_SECS, restarts=recent, alerted=False)
    new, action, healthy, already_running, launch = _run(NOON, False, st)
    assert action == "rate_limited"
    assert launch.calls == 0
    assert new["alerted"] is True
    assert len(new["restarts"]) == tw.MAX_RESTARTS_PER_HOUR   # unchanged


def test_rate_limit_prunes_old_restart_so_it_can_restart():
    restarts = [NOON - 4000,                     # >1h ago -> pruned
                NOON - 1500, NOON - 600]          # recent (2)
    st = _state(down_since=NOON - tw.GRACE_SECS, restarts=restarts, alerted=False)
    new, action, healthy, already_running, launch = _run(
        NOON, False, st, already_running_value=False, launch=Counter(True))
    assert action == "restarted"
    assert launch.calls == 1
    assert len(new["restarts"]) == 3
    assert all((NOON - t) < 3600 for t in new["restarts"])


def test_alerted_resets_when_healthy_again():
    st = _state(down_since=NOON - 5000,
                restarts=[NOON - 100, NOON - 200, NOON - 300],
                alerted=True)
    new, action, healthy, already_running, launch = _run(NOON, True, st)
    assert action == "healthy"
    assert new["alerted"] is False
    assert new["down_since"] is None
    assert len(new["restarts"]) == 3   # restart history preserved for the rolling limit
    assert launch.calls == 0


def test_already_alerted_stays_rate_limited_no_restart():
    recent = [NOON - 2400, NOON - 1500, NOON - 600]
    st = _state(down_since=NOON - tw.GRACE_SECS, restarts=recent, alerted=True)
    new, action, healthy, already_running, launch = _run(NOON, False, st)
    assert action == "rate_limited"
    assert new["alerted"] is True
    assert launch.calls == 0
