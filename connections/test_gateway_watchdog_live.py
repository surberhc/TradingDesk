"""Tests for connections/connections/gateway_watchdog_live.py — the LIVE-DATA (port
4001) gateway death-watchdog.

OFFLINE + FAST: no real gateway, no network, no PowerShell. Every test drives the PURE
decision function run_once(...) with an injected clock, an in-memory state dict, and
mocked healthy / kill_fn / launch_fn / gateway_age / log_fn, asserting on the returned
(new_state, action) and mock call counts.

These cover the two 2026-08-05 fixes for keeping 4001 up 24/7 unattended:

  (A) LOGIN/2FA GRACE (ported from livebot/s8_gateway_reap.py commit a173ca2). Once a
      gateway is judged wedged, a still-PRESENT gateway process that is younger than
      LOGIN_GRACE_SECS (or a process-scan that is indeterminate) is SPARED — never
      killed/relaunched — so the watchdog can never reap a gateway that is mid-login/2FA
      and start the reaper-vs-2FA thrash. Only "no process at all" launches fresh, and
      only a process aged past the login window is killed as genuinely hung.

  (B) MAINTENANCE_WINDOW_ET brackets the real self-restart. The 4001 Gateway
      auto-restarts at 01:05 CT (= 02:05 ET); the watchdog must do nothing in that
      window so the expected nightly bounce is never mistaken for a wedge.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from connections import gateway_watchdog_live as gwl

_NY = ZoneInfo("America/New_York")
_CT = ZoneInfo("America/Chicago")


class Counter:
    """A callable that records call count and returns a fixed value."""

    def __init__(self, value=None):
        self.value = value
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        return self.value


def _et_epoch(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=_NY).timestamp()


def _state(down_since=None, restarts=None, alerted=False):
    return {"down_since": down_since, "restarts": list(restarts or []), "alerted": alerted}


# Midday Wednesday 2026-08-05 ET — well outside the maintenance window.
NOON = _et_epoch(2026, 8, 5, 12, 0)
# A gateway that has been DOWN long enough to be wedged (past GRACE_SECS).
WEDGED_STATE = lambda: _state(down_since=NOON - (gwl.GRACE_SECS + 120))  # noqa: E731


def _run(*, now=NOON, healthy_value=False, state=None, gateway_age, kill=None, launch=None):
    kill = kill if kill is not None else Counter(value=[111])
    launch = launch if launch is not None else Counter(value=True)
    logs = []
    new_state, action = gwl.run_once(
        now=now, healthy=Counter(value=healthy_value),
        state=state if state is not None else WEDGED_STATE(),
        kill_fn=kill, launch_fn=launch, log_fn=logs.append,
        gateway_age=gateway_age,
    )
    return new_state, action, kill, launch, logs


# --------------------------------------------------------------------------- #
# (A) LOGIN / 2FA GRACE
# --------------------------------------------------------------------------- #
def test_login_grace_spares_young_present_gateway():
    """Wedged, but a gateway PROCESS is present and young (mid-login/2FA) -> SPARE:
    no kill, no relaunch, no 2FA re-push."""
    _, action, kill, launch, _ = _run(gateway_age=Counter(value=120.0))
    assert action == "login_grace"
    assert kill.calls == 0
    assert launch.calls == 0


def test_login_grace_spares_indeterminate_scan():
    """Wedged, but the process-scan is UNKNOWN -> SPARE (never risk reaping a mid-2FA
    gateway on a scan we couldn't perform)."""
    _, action, kill, launch, _ = _run(gateway_age=Counter(value=gwl.UNKNOWN_AGE))
    assert action == "login_grace"
    assert kill.calls == 0
    assert launch.calls == 0


def test_login_grace_spares_when_probe_raises():
    """A gateway_age probe that RAISES must be treated as indeterminate -> SPARE, never
    kill (a probe hiccup can never license a kill)."""
    def boom():
        raise RuntimeError("powershell exploded")

    _, action, kill, launch, _ = _run(gateway_age=boom)
    assert action == "login_grace"
    assert kill.calls == 0
    assert launch.calls == 0


def test_no_process_launches_fresh():
    """Wedged and NO gateway process exists (age None) -> safe to launch a fresh one."""
    _, action, kill, launch, _ = _run(gateway_age=Counter(value=None))
    assert action == "restarted"
    assert kill.calls == 1
    assert launch.calls == 1


def test_aged_hung_process_is_killed_and_relaunched():
    """A process that has sat unbound PAST the login window (age >= LOGIN_GRACE_SECS) is
    genuinely hung -> kill + relaunch."""
    _, action, kill, launch, _ = _run(
        gateway_age=Counter(value=float(gwl.LOGIN_GRACE_SECS + 60)))
    assert action == "restarted"
    assert kill.calls == 1
    assert launch.calls == 1


def test_login_grace_not_consulted_within_boot_grace():
    """Before GRACE_SECS elapses the gateway is only 'within_grace' — the login/2FA
    machinery (and gateway_age) is never reached, so a raising probe is harmless."""
    def boom():
        raise AssertionError("gateway_age must NOT be called within boot grace")

    st = _state(down_since=NOON - 30)  # down only 30s, < GRACE_SECS
    _, action, kill, launch, _ = _run(state=st, gateway_age=boom)
    assert action == "within_grace"
    assert kill.calls == 0
    assert launch.calls == 0


def test_login_grace_boundary_is_exclusive():
    """age exactly == LOGIN_GRACE_SECS is NOT younger than the window -> treated as hung
    (kill+relaunch), matching the '< login_grace_secs spares' boundary."""
    _, action, kill, launch, _ = _run(
        gateway_age=Counter(value=float(gwl.LOGIN_GRACE_SECS)))
    assert action == "restarted"
    assert kill.calls == 1


# --------------------------------------------------------------------------- #
# (B) MAINTENANCE WINDOW brackets the 01:05 CT (= 02:05 ET) self-restart
# --------------------------------------------------------------------------- #
def test_maintenance_window_value():
    assert gwl.MAINTENANCE_WINDOW_ET == ("01:55", "02:20")


def test_ct_restart_equals_0205_et():
    """Sanity: 01:05 in the machine's Central tz is the same instant as 02:05 ET."""
    assert (dt.datetime(2026, 8, 5, 1, 5, tzinfo=_CT).timestamp()
            == dt.datetime(2026, 8, 5, 2, 5, tzinfo=_NY).timestamp())


def test_maintenance_window_covers_the_restart_instant():
    """02:05 ET (the AutoRestartTime) is INSIDE the window -> watchdog does nothing."""
    ts = _et_epoch(2026, 8, 5, 2, 5)
    assert gwl._in_maintenance_window(ts, window=gwl.MAINTENANCE_WINDOW_ET) is True


def test_maintenance_window_excludes_just_outside():
    """Just before the open (01:50 ET) and just after the close (02:25 ET) are OUTSIDE."""
    before = _et_epoch(2026, 8, 5, 1, 50)
    after = _et_epoch(2026, 8, 5, 2, 25)
    assert gwl._in_maintenance_window(before, window=gwl.MAINTENANCE_WINDOW_ET) is False
    assert gwl._in_maintenance_window(after, window=gwl.MAINTENANCE_WINDOW_ET) is False


def test_run_once_skips_all_work_in_maintenance_window():
    """Inside the window run_once probes NOTHING (no health, no gateway_age, no kill)."""
    ts = _et_epoch(2026, 8, 5, 2, 5)

    def boom_health():
        raise AssertionError("healthy must NOT be called in the maintenance window")

    def boom_age():
        raise AssertionError("gateway_age must NOT be called in the maintenance window")

    kill = Counter(value=[1])
    launch = Counter(value=True)
    logs = []
    new_state, action = gwl.run_once(
        now=ts, healthy=boom_health, state=WEDGED_STATE(),
        kill_fn=kill, launch_fn=launch, log_fn=logs.append, gateway_age=boom_age,
    )
    assert action == "maintenance"
    assert kill.calls == 0 and launch.calls == 0
