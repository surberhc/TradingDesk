"""Tests for connections/connections/gateway_watchdog.py.

OFFLINE + FAST: no real gateway, no network, no PowerShell. Every test drives the
PURE decision function run_once(...) with an injected clock (now=...), an in-memory
state dict, and mocked healthy / kill_fn / launch_fn / log_fn. We assert on the
returned (new_state, action) and on how many times the mocks were called.

The policy being guarded (2026-07-05 incident): a WEDGED login must be recovered by
killing the stuck gateway and bringing up exactly ONE fresh — after a grace window,
under a hard rolling-hour restart cap, and NEVER inside the IBKR nightly-reset
maintenance window. It must never become a hot loop.
"""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from connections import gateway_watchdog as gw

_NY = ZoneInfo("America/New_York")


class Counter:
    """A zero-arg callable that records call count and returns a fixed value."""
    def __init__(self, value=None):
        self.value = value
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        return self.value


def _et_epoch(y, mo, d, h, mi):
    """Epoch seconds for a wall-clock time in America/New_York (DST-correct)."""
    return dt.datetime(y, mo, d, h, mi, tzinfo=_NY).timestamp()


def _state(down_since=None, restarts=None, alerted=False):
    return {"down_since": down_since,
            "restarts": list(restarts or []),
            "alerted": alerted}


def _run(now, healthy_value, state, kill=None, launch=None):
    """Drive run_once with fresh mocks; return (new_state, action, kill, launch)."""
    kill = kill or Counter(value=[111, 222])
    launch = launch or Counter(value=True)
    healthy = Counter(value=healthy_value)
    logs = []
    new_state, action = gw.run_once(
        now=now, healthy=healthy, state=state,
        kill_fn=kill, launch_fn=launch, log_fn=logs.append,
    )
    # Stash the health probe on the returned tuple's mocks for call-count asserts.
    _run.last_healthy = healthy
    return new_state, action, kill, launch


# A safe "midday, definitely outside the maintenance window" reference time.
NOON = _et_epoch(2026, 7, 6, 12, 0)


# ---------------------------------------------------------------------------
# 1. Healthy -> down_since cleared, NO kill, NO launch.
# ---------------------------------------------------------------------------
def test_healthy_clears_down_and_does_nothing():
    st = _state(down_since=NOON - 100)
    new, action, kill, launch = _run(NOON, True, st)
    assert action == "healthy"
    assert new["down_since"] is None
    assert kill.calls == 0
    assert launch.calls == 0


# ---------------------------------------------------------------------------
# 2. Down first-seen -> down_since set, NO restart.
# ---------------------------------------------------------------------------
def test_down_first_seen_starts_grace_no_restart():
    st = _state(down_since=None)
    new, action, kill, launch = _run(NOON, False, st)
    assert action == "grace_started"
    assert new["down_since"] == NOON
    assert kill.calls == 0
    assert launch.calls == 0


# ---------------------------------------------------------------------------
# 3. Down but within grace -> NO restart.
# ---------------------------------------------------------------------------
def test_down_within_grace_no_restart():
    st = _state(down_since=NOON - (gw.GRACE_SECS - 30))   # 30s short of grace
    new, action, kill, launch = _run(NOON, False, st)
    assert action == "within_grace"
    assert new["down_since"] == NOON - (gw.GRACE_SECS - 30)   # unchanged
    assert kill.calls == 0
    assert launch.calls == 0


# ---------------------------------------------------------------------------
# 4. Down >= grace, under limit -> exactly ONE kill + ONE launch; restart
#    recorded; on launch success down_since cleared.
# ---------------------------------------------------------------------------
def test_wedged_under_limit_restarts_once_and_clears_on_success():
    st = _state(down_since=NOON - gw.GRACE_SECS, restarts=[])
    new, action, kill, launch = _run(NOON, False, st,
                                     kill=Counter([9001]), launch=Counter(True))
    assert action == "restarted"
    assert kill.calls == 1
    assert launch.calls == 1
    assert len(new["restarts"]) == 1 and new["restarts"][0] == NOON
    assert new["down_since"] is None          # came up -> cleared
    assert new["alerted"] is False


# ---------------------------------------------------------------------------
# 5. Down >= grace, launch FAILS -> down_since retained, restart still counted.
# ---------------------------------------------------------------------------
def test_wedged_launch_fails_retains_down_but_counts_restart():
    down = NOON - gw.GRACE_SECS
    st = _state(down_since=down, restarts=[])
    new, action, kill, launch = _run(NOON, False, st,
                                     kill=Counter([9001]), launch=Counter(False))
    assert action == "restart_failed"
    assert kill.calls == 1
    assert launch.calls == 1
    assert len(new["restarts"]) == 1          # still counted against the limit
    assert new["down_since"] == down          # retained so next cycle keeps counting


# ---------------------------------------------------------------------------
# 6. Rate limit: 3 restarts already in the last hour -> 4th wedged check does NOT
#    restart, sets alerted. A restart timestamp older than 1h is PRUNED so it
#    doesn't count toward the limit.
# ---------------------------------------------------------------------------
def test_rate_limit_blocks_restart_and_alerts():
    # Three restarts within the last hour + one wedged, down past grace.
    recent = [NOON - 2400, NOON - 1500, NOON - 600]   # 40m, 25m, 10m ago
    st = _state(down_since=NOON - gw.GRACE_SECS, restarts=recent, alerted=False)
    new, action, kill, launch = _run(NOON, False, st)
    assert action == "rate_limited"
    assert kill.calls == 0
    assert launch.calls == 0
    assert new["alerted"] is True
    assert len(new["restarts"]) == gw.MAX_RESTARTS_PER_HOUR   # unchanged (no new restart)


def test_rate_limit_prunes_old_restart_so_it_can_restart():
    # Two recent + ONE older than an hour: the old one is pruned, leaving 2 < 3,
    # so this wedged check IS allowed to restart.
    restarts = [NOON - 4000,                       # >1h ago -> pruned
                NOON - 1500, NOON - 600]           # recent (2)
    st = _state(down_since=NOON - gw.GRACE_SECS, restarts=restarts, alerted=False)
    new, action, kill, launch = _run(NOON, False, st,
                                     kill=Counter([1]), launch=Counter(True))
    assert action == "restarted"
    assert kill.calls == 1 and launch.calls == 1
    # Pruned to the 2 recent, plus this new one = 3.
    assert len(new["restarts"]) == 3
    assert all((NOON - t) < 3600 for t in new["restarts"])


# ---------------------------------------------------------------------------
# 7. Maintenance window: inside the window -> NO health probe, NO kill, NO launch.
#    Includes a midnight-spanning window (23:50 and 00:20 both inside 23:45->00:45).
# ---------------------------------------------------------------------------
def test_maintenance_window_before_midnight_no_action():
    now = _et_epoch(2026, 7, 6, 23, 50)          # inside 23:45 -> 00:45
    st = _state(down_since=None)
    healthy = Counter(value=False)
    kill = Counter([1]); launch = Counter(True)
    new, action = gw.run_once(now=now, healthy=healthy, state=st,
                              kill_fn=kill, launch_fn=launch, log_fn=lambda _m: None)
    assert action == "maintenance"
    assert healthy.calls == 0                     # never probed
    assert kill.calls == 0 and launch.calls == 0


def test_maintenance_window_after_midnight_no_action():
    now = _et_epoch(2026, 7, 7, 0, 20)            # inside 23:45 -> 00:45 (next day)
    st = _state(down_since=None)
    healthy = Counter(value=False)
    kill = Counter([1]); launch = Counter(True)
    new, action = gw.run_once(now=now, healthy=healthy, state=st,
                              kill_fn=kill, launch_fn=launch, log_fn=lambda _m: None)
    assert action == "maintenance"
    assert healthy.calls == 0
    assert kill.calls == 0 and launch.calls == 0


def test_just_outside_maintenance_window_probes():
    now = _et_epoch(2026, 7, 7, 0, 46)            # one minute past the 00:45 end
    st = _state(down_since=None)
    healthy = Counter(value=True)
    kill = Counter([1]); launch = Counter(True)
    new, action = gw.run_once(now=now, healthy=healthy, state=st,
                              kill_fn=kill, launch_fn=launch, log_fn=lambda _m: None)
    assert action == "healthy"
    assert healthy.calls == 1                      # DID probe outside the window


def test_in_maintenance_window_helper_spans_midnight():
    # Direct unit check of the pure helper across the midnight boundary.
    assert gw._in_maintenance_window(_et_epoch(2026, 7, 6, 23, 45)) is True   # start incl.
    assert gw._in_maintenance_window(_et_epoch(2026, 7, 6, 23, 59)) is True
    assert gw._in_maintenance_window(_et_epoch(2026, 7, 7, 0, 0)) is True
    assert gw._in_maintenance_window(_et_epoch(2026, 7, 7, 0, 44)) is True
    assert gw._in_maintenance_window(_et_epoch(2026, 7, 7, 0, 45)) is False   # end excl.
    assert gw._in_maintenance_window(_et_epoch(2026, 7, 6, 23, 44)) is False  # before start
    assert gw._in_maintenance_window(_et_epoch(2026, 7, 6, 12, 0)) is False   # midday


# ---------------------------------------------------------------------------
# 8. alerted resets once healthy again.
# ---------------------------------------------------------------------------
def test_alerted_resets_when_healthy_again():
    st = _state(down_since=NOON - 5000,
                restarts=[NOON - 100, NOON - 200, NOON - 300],
                alerted=True)
    new, action, kill, launch = _run(NOON, True, st)
    assert action == "healthy"
    assert new["alerted"] is False
    assert new["down_since"] is None
    # Restart history is preserved (still within the hour) for the rolling limit.
    assert len(new["restarts"]) == 3
    assert kill.calls == 0 and launch.calls == 0


# ---------------------------------------------------------------------------
# 9. Already-alerted wedge stays rate_limited but does NOT re-flip / re-restart.
# ---------------------------------------------------------------------------
def test_already_alerted_stays_rate_limited_no_restart():
    recent = [NOON - 2400, NOON - 1500, NOON - 600]
    st = _state(down_since=NOON - gw.GRACE_SECS, restarts=recent, alerted=True)
    new, action, kill, launch = _run(NOON, False, st)
    assert action == "rate_limited"
    assert new["alerted"] is True
    assert kill.calls == 0 and launch.calls == 0
