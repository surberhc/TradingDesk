"""Tests for livebot/livedata_chained_open.py — the dependency-gated (chained behind
4003) morning bring-up for the live-DATA Gateway (port 4001).

OFFLINE + FAST: no real socket, no real gateway, no real PowerShell and no real processes —
every seam (the 4001 probe, the 4003 probe, the PROCESS probe, the reap, the launch) is
injected. Asserts the gate decision and whether the launch fired.

The gate contract:
  * 4001 already up            -> no-op, 4003 never probed, launch never called.
  * a live-data gateway PROCESS exists, young  -> "wait_login", launch never called.
  * live-data gateway PROCESSES exist, all old -> "reap_wedged", reap then ONE launch.
  * 4001 down + 4003 up        -> launch fires ONCE.
  * 4001 down + 4003 down/None -> WAIT, launch never called (no 2nd pending 2FA).

The process gate is the 2026-08-31 fix: the port is BLIND during login, so 48 cycles of
"4001 not listening + 4003 up" stacked 49 gateway windows between 08:05 and 12:00.
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
def _run(up1, up3, launch_value=True, procs=None):
    """``procs=None`` (the default) injects a process probe that reports UNDETERMINABLE, so
    the gate falls through to the pre-2026-08-31 4003 behaviour — that is what every legacy
    case below asserts. It is still INJECTED, so no real PowerShell ever runs."""
    p1 = Recorder(value=up1)
    p3 = Recorder(value=up3)
    launch = Recorder(value=launch_value)
    res = m.run_once(probe_4001=p1, probe_4003=p3, probe_procs=Recorder(value=procs),
                     launch_4001=launch, log=lambda *_a, **_k: None)
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
    res = m.run_once(probe_4001=boom, probe_4003=boom, probe_procs=Recorder(value=None),
                     launch_4001=Recorder(value=True), log=lambda *_a, **_k: None)
    assert res["action"] == "wait_4003"
    assert res["launched"] is False


def test_run_launch_raising_is_swallowed_by_default_wrapper():
    """If the injected launch raises, run_once catches it and reports error, never raises."""
    def boom_launch():
        raise RuntimeError("ensure_gateway blew up")

    res = m.run_once(probe_4001=Recorder(value=False), probe_4003=Recorder(value=True),
                     probe_procs=Recorder(value=None), launch_4001=boom_launch,
                     log=lambda *_a, **_k: None)
    assert res["action"] == "error"
    assert res["launched"] is False


def test_ports_are_4001_and_4003():
    assert m.LIVE_DATA_PORT == 4001
    assert m.LIVE_TRADE_PORT == 4003


# --------------------------------------------------------------------------- #
# THE PROCESS GATE (2026-08-31: 49 stacked gateways, one per 5-min cycle)
# --------------------------------------------------------------------------- #
LD = r"C:\IBC-Live-Data\jts.ini"          # a cmdline that IS ours
LT = r"C:\IBC-Live-Trade\jts.ini"         # the SIBLING lane — must never be touched


def _gw(pid, age, cmdline=LD):
    return {"pid": pid, "cmdline": cmdline, "age_secs": age}


def test_marker_is_full_install_dir_never_the_bare_prefix():
    """The bare "C:\\IBC" prefix matches BOTH sibling installs — that prefix match is the
    2026-07-23 cross-lane kill. The marker must be the FULL live-data directory."""
    assert m.INSTALL_MARKER == r"C:\IBC-Live-Data"
    assert m.INSTALL_MARKER != r"C:\IBC"
    assert m.LOGIN_GRACE_SECS == 1800


def test_decide_proc_ages_default_none_preserves_old_behaviour():
    """proc_ages defaults to None -> all four legacy outcomes are byte-for-byte unchanged."""
    assert m.decide_action(up_4001=True, up_4003=True) == "already_up"
    assert m.decide_action(up_4001=False, up_4003=True) == "launch"
    assert m.decide_action(up_4001=False, up_4003=False) == "wait_4003"
    assert m.decide_action(up_4001=False, up_4003=None) == "wait_4003"
    # ...and explicitly passing None is identical to omitting it.
    assert m.decide_action(up_4001=True, up_4003=True, proc_ages=None) == "already_up"
    assert m.decide_action(up_4001=False, up_4003=True, proc_ages=None) == "launch"
    assert m.decide_action(up_4001=False, up_4003=False, proc_ages=None) == "wait_4003"
    assert m.decide_action(up_4001=None, up_4003=None, proc_ages=None) == "wait_4003"


def test_decide_young_process_waits_for_login_even_when_4003_is_up():
    """A 60s-old gateway is plausibly mid-2FA — launching a rival is the pileup bug."""
    assert m.decide_action(up_4001=False, up_4003=True, proc_ages=[60.0]) == "wait_login"


def test_decide_process_past_login_window_is_wedged():
    ages = [m.LOGIN_GRACE_SECS + 1]
    assert m.decide_action(up_4001=False, up_4003=True, proc_ages=ages) == "reap_wedged"


def test_decide_empty_proc_list_falls_through_to_the_4003_gate():
    """[] is a DETERMINATE "no gateway exists" -> the old gate decides -> launch."""
    assert m.decide_action(up_4001=False, up_4003=True, proc_ages=[]) == "launch"
    assert m.decide_action(up_4001=False, up_4003=False, proc_ages=[]) == "wait_4003"


def test_decide_mixed_ages_the_young_one_protects_the_whole_set():
    assert m.decide_action(up_4001=False, up_4003=True,
                           proc_ages=[5000.0, 60.0]) == "wait_login"


def test_decide_4001_up_wins_over_any_proc_ages():
    assert m.decide_action(up_4001=True, up_4003=True, proc_ages=[60.0]) == "already_up"
    assert m.decide_action(up_4001=True, up_4003=False,
                           proc_ages=[m.LOGIN_GRACE_SECS + 1]) == "already_up"


# --------------------------------------------------------------------------- #
# run_once wiring — the process gate
# --------------------------------------------------------------------------- #
def _run_procs(up1, up3, procs, launch_value=True, reaped=1):
    p1 = Recorder(value=up1)
    p3 = Recorder(value=up3)
    pp = Recorder(value=procs)
    launch = Recorder(value=launch_value)
    reap = Recorder(value=reaped)
    res = m.run_once(probe_4001=p1, probe_4003=p3, probe_procs=pp, launch_4001=launch,
                     reap=reap, log=lambda *_a, **_k: None)
    return res, pp, launch, reap


def test_run_wait_login_never_launches_REGRESSION_49_window_pileup():
    """THE regression test for the 2026-08-31 49-window pileup.

    A live-data gateway process exists and is young (still completing login/2FA), 4001 is
    not yet bound and 4003 IS up — the exact state that fired a launch on all 48 cycles.
    The launch must NOT happen."""
    res, _, launch, reap = _run_procs(up1=False, up3=True, procs=[_gw(101, 60.0)])
    assert res["action"] == "wait_login"
    assert res["launched"] is False
    assert launch.calls == 0             # <- one of these per 5-min cycle stacked 49 windows
    assert reap.calls == 0               # an in-progress login is never killed either
    assert res["proc_count"] == 1


def test_run_reap_wedged_with_4003_up_reaps_then_launches_exactly_once():
    res, _, launch, reap = _run_procs(up1=False, up3=True,
                                      procs=[_gw(101, 9000.0), _gw(102, 8000.0)], reaped=2)
    assert res["action"] == "reap_wedged"
    assert reap.calls == 1
    assert launch.calls == 1             # at most ONE launch per cycle, ever
    assert res["launched"] is True
    assert res["reaped"] == 2
    assert res["proc_count"] == 2


def test_run_reap_wedged_with_4003_down_reaps_but_never_launches():
    res, _, launch, reap = _run_procs(up1=False, up3=False, procs=[_gw(101, 9000.0)])
    assert res["action"] == "reap_wedged"
    assert reap.calls == 1
    assert launch.calls == 0             # 4003 still gates the launch after a reap
    assert res["launched"] is False


def test_run_empty_proc_scan_falls_through_and_launches():
    res, pp, launch, reap = _run_procs(up1=False, up3=True, procs=[])
    assert res["action"] == "launch"
    assert launch.calls == 1
    assert reap.calls == 0
    assert pp.calls == 1
    assert res["proc_count"] == 0


def test_run_proc_probe_that_raises_is_undeterminable_and_fails_open():
    """FAIL-OPEN: an unusable process scan must never leave a cold morning gateway-less."""
    def boom_procs():
        raise OSError("Get-CimInstance blew up")

    launch = Recorder(value=True)
    res = m.run_once(probe_4001=Recorder(value=False), probe_4003=Recorder(value=True),
                     probe_procs=boom_procs, launch_4001=launch, reap=Recorder(value=0),
                     log=lambda *_a, **_k: None)
    assert res["action"] == "launch"     # old behaviour, unchanged
    assert launch.calls == 1
    assert res["proc_count"] is None


def test_run_4001_already_up_skips_the_proc_probe_too():
    p3 = Recorder(value=True)
    pp = Recorder(value=[_gw(101, 60.0)])
    launch = Recorder(value=True)
    res = m.run_once(probe_4001=Recorder(value=True), probe_4003=p3, probe_procs=pp,
                     launch_4001=launch, reap=Recorder(value=0),
                     log=lambda *_a, **_k: None)
    assert res["action"] == "already_up"
    assert pp.calls == 0                 # short-circuit: procs never probed when 4001 up
    assert p3.calls == 0
    assert launch.calls == 0


def test_run_reap_that_raises_is_swallowed_and_does_not_block_the_launch():
    def boom_reap(*_a, **_k):
        raise RuntimeError("taskkill blew up")

    launch = Recorder(value=True)
    res = m.run_once(probe_4001=Recorder(value=False), probe_4003=Recorder(value=True),
                     probe_procs=Recorder(value=[_gw(101, 9000.0)]), launch_4001=launch,
                     reap=boom_reap, log=lambda *_a, **_k: None)
    assert res["action"] == "reap_wedged"
    assert res["reaped"] == 0
    assert launch.calls == 1


# --------------------------------------------------------------------------- #
# reap_livedata_gateways — CROSS-LANE SAFETY
# --------------------------------------------------------------------------- #
class KillRecorder:
    def __init__(self, value=True):
        self.value = value
        self.pids = []

    def __call__(self, pid):
        self.pids.append(pid)
        return self.value


def test_reap_refuses_a_live_TRADE_cmdline_cross_lane_safety():
    """THE cross-lane safety test. A C:\\IBC-Live-Trade gateway must NEVER be killed by the
    live-DATA lane — the "C:\\IBC" prefix match is the 2026-07-23 incident."""
    kill = KillRecorder()
    killed = m.reap_livedata_gateways([_gw(999, 9000.0, cmdline=LT)],
                                      kill=kill, log=lambda *_a, **_k: None)
    assert killed == 0
    assert kill.pids == []               # the kill callable was never invoked for it


def test_reap_refuses_missing_or_empty_cmdline():
    kill = KillRecorder()
    gws = [{"pid": 1, "cmdline": None, "age_secs": 9000.0},
           {"pid": 2, "cmdline": "", "age_secs": 9000.0},
           {"pid": 3, "age_secs": 9000.0}]
    assert m.reap_livedata_gateways(gws, kill=kill, log=lambda *_a, **_k: None) == 0
    assert kill.pids == []


def test_reap_kills_only_the_matching_entries_and_counts_them():
    kill = KillRecorder()
    gws = [_gw(101, 9000.0), _gw(102, 9000.0, cmdline=LT), _gw(103, 9000.0)]
    assert m.reap_livedata_gateways(gws, kill=kill, log=lambda *_a, **_k: None) == 2
    assert kill.pids == [101, 103]       # the live-TRADE pid is absent


def test_reap_is_case_insensitive_on_the_marker():
    kill = KillRecorder()
    gws = [_gw(101, 9000.0, cmdline=r"c:\ibc-live-data\jts.ini")]
    assert m.reap_livedata_gateways(gws, kill=kill, log=lambda *_a, **_k: None) == 1
    assert kill.pids == [101]


def test_reap_counts_a_failed_kill_as_not_killed_and_keeps_going():
    class FlakyKill(KillRecorder):
        def __call__(self, pid):
            self.pids.append(pid)
            if pid == 101:
                raise OSError("access denied")
            return True

    kill = FlakyKill()
    gws = [_gw(101, 9000.0), _gw(102, 9000.0)]
    assert m.reap_livedata_gateways(gws, kill=kill, log=lambda *_a, **_k: None) == 1
    assert kill.pids == [101, 102]       # the failure did not abort the rest


def test_reap_of_none_or_empty_is_a_harmless_zero():
    kill = KillRecorder()
    assert m.reap_livedata_gateways(None, kill=kill, log=lambda *_a, **_k: None) == 0
    assert m.reap_livedata_gateways([], kill=kill, log=lambda *_a, **_k: None) == 0
    assert kill.pids == []


# --------------------------------------------------------------------------- #
# find_livedata_gateways — offline, the `run` seam injected
# --------------------------------------------------------------------------- #
class FakeRun:
    def __init__(self, stdout="", raises=None):
        self.stdout = stdout
        self.raises = raises
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        if self.raises:
            raise self.raises
        return type("P", (), {"stdout": self.stdout, "returncode": 0})()


def test_find_empty_sentinel_is_a_determinate_none_found():
    import os as _os
    if _os.name != "nt":
        return
    assert m.find_livedata_gateways(run=FakeRun(stdout="EMPTY")) == []


def test_find_parses_json_rows_and_a_single_dict():
    import os as _os
    if _os.name != "nt":
        return
    one = '{"pid":101,"cmdline":"' + LD.replace("\\", "\\\\") + '","age":12.5}'
    rows = m.find_livedata_gateways(run=FakeRun(stdout=one))
    assert rows == [{"pid": 101, "cmdline": LD, "age_secs": 12.5}]
    rows = m.find_livedata_gateways(run=FakeRun(stdout="[" + one + "]"))
    assert len(rows) == 1 and rows[0]["pid"] == 101


def test_find_unusable_scan_is_none_not_empty():
    """None (undeterminable) and [] (none found) must stay distinguishable — the whole
    fail-open contract rests on it."""
    import os as _os
    if _os.name != "nt":
        return
    assert m.find_livedata_gateways(run=FakeRun(raises=OSError("no powershell"))) is None
    assert m.find_livedata_gateways(run=FakeRun(stdout="")) is None
    assert m.find_livedata_gateways(run=FakeRun(stdout="not json at all")) is None


def test_main_returns_zero(monkeypatch):
    monkeypatch.setattr(m, "run_once", lambda: {"action": "wait_4003", "launched": False})
    assert m.main() == 0
