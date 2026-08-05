"""Tests for livebot/livedata_chained_open.py — the dependency-gated (chained behind
4003) morning bring-up for the live-DATA Gateway (port 4001).

OFFLINE + FAST: no real socket, no real gateway — every seam (the 4001 probe, the 4003
probe, the launch) is injected. Asserts the gate decision and whether the launch fired.

The gate contract:
  * 4001 already up            -> no-op, 4003 never probed, launch never called.
  * 4001 down + 4003 up        -> launch fires ONCE.
  * 4001 down + 4003 down/None -> WAIT, launch never called (no 2nd pending 2FA).
"""
from __future__ import annotations

import livedata_chained_open as m


class Recorder:
    def __init__(self, value=None):
        self.value = value
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        return self.value


# --------------------------------------------------------------------------- #
# Pure gate — decide_action
# --------------------------------------------------------------------------- #
def test_decide_4001_up_is_already_up():
    assert m.decide_action(up_4001=True, up_4003=True) == "already_up"
    assert m.decide_action(up_4001=True, up_4003=False) == "already_up"


def test_decide_4001_down_4003_up_launches():
    assert m.decide_action(up_4001=False, up_4003=True) == "launch"
    assert m.decide_action(up_4001=None, up_4003=True) == "launch"


def test_decide_4001_down_4003_down_waits():
    assert m.decide_action(up_4001=False, up_4003=False) == "wait_4003"


def test_decide_4003_unknown_waits_never_guesses():
    """A None (undeterminable) 4003 must WAIT, never launch — no 2nd 2FA on a guess."""
    assert m.decide_action(up_4001=False, up_4003=None) == "wait_4003"


# --------------------------------------------------------------------------- #
# run_once wiring
# --------------------------------------------------------------------------- #
def _run(up1, up3, launch_value=True):
    p1 = Recorder(value=up1)
    p3 = Recorder(value=up3)
    launch = Recorder(value=launch_value)
    res = m.run_once(probe_4001=p1, probe_4003=p3, launch_4001=launch,
                     log=lambda *_a, **_k: None)
    return res, p1, p3, launch


def test_run_4003_down_no_launch_no_push():
    res, p1, p3, launch = _run(up1=False, up3=False)
    assert res["action"] == "wait_4003"
    assert res["launched"] is False
    assert launch.calls == 0            # nothing launched -> no 2FA push


def test_run_4003_unknown_no_launch():
    res, _, _, launch = _run(up1=False, up3=None)
    assert res["action"] == "wait_4003"
    assert launch.calls == 0


def test_run_4003_up_4001_down_launches_once():
    res, _, p3, launch = _run(up1=False, up3=True)
    assert res["action"] == "launch"
    assert res["launched"] is True
    assert launch.calls == 1
    assert p3.calls == 1                # 4003 was actually checked


def test_run_4001_already_up_is_noop_and_skips_4003_probe():
    res, p1, p3, launch = _run(up1=True, up3=True)
    assert res["action"] == "already_up"
    assert launch.calls == 0
    assert p3.calls == 0                # short-circuit: 4003 never probed when 4001 up


def test_run_4001_unknown_treated_as_not_up_and_gates_on_4003():
    # 4001 None (undeterminable) is NOT "already up"; with 4003 up it launches.
    res, _, _, launch = _run(up1=None, up3=True)
    assert res["action"] == "launch"
    assert launch.calls == 1


def test_run_launch_failure_reported_not_raised():
    res, _, _, launch = _run(up1=False, up3=True, launch_value=False)
    assert res["action"] == "launch"
    assert res["launched"] is False     # launch attempted but 4001 not up yet
    assert launch.calls == 1


def test_run_probe_that_raises_never_propagates():
    def boom():
        raise OSError("probe blew up")

    # 4001 probe raises -> treated as unknown (not up) -> gate on 4003 (also raises) ->
    # treated as not up -> wait. Must NOT raise.
    res = m.run_once(probe_4001=boom, probe_4003=boom,
                     launch_4001=Recorder(value=True), log=lambda *_a, **_k: None)
    assert res["action"] == "wait_4003"
    assert res["launched"] is False


def test_run_launch_raising_is_swallowed_by_default_wrapper():
    """If the injected launch raises, run_once catches it and reports error, never raises."""
    def boom_launch():
        raise RuntimeError("ensure_gateway blew up")

    res = m.run_once(probe_4001=Recorder(value=False), probe_4003=Recorder(value=True),
                     launch_4001=boom_launch, log=lambda *_a, **_k: None)
    assert res["action"] == "error"
    assert res["launched"] is False


def test_ports_are_4001_and_4003():
    assert m.LIVE_DATA_PORT == 4001
    assert m.LIVE_TRADE_PORT == 4003


def test_main_returns_zero(monkeypatch):
    monkeypatch.setattr(m, "run_once", lambda: {"action": "wait_4003", "launched": False})
    assert m.main() == 0
