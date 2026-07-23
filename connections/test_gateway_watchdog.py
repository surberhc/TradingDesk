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
import json
import re
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


# ===========================================================================
# 10. THE KILL DISCRIMINATOR — regression suite for the 2026-07-23 incident.
#
#     docs/INCIDENT_2026-07-23_arm_restart_killed_live_gateway.md
#
#     A zero-argument (paper) `_kill_gateway_processes()` killed the S8 LIVE-pilot
#     Gateway on port 4003, because the secondary discriminator was the bare
#     substring `C:\IBC` — a string PREFIX of C:\IBC-Live-Data / C:\IBC-Live-Trade.
#
#     These tests exercise the REAL match semantics (gw.should_kill /
#     gw.matches_instance / gw.foreign_instance_marker) against command lines
#     CAPTURED FROM Win32_Process on this box on 2026-07-23 — including the shared
#     `C:\IBC\IBC.jar` classpath entry that defeats the naive trailing-separator
#     fix. The old tests asserted only on generated PowerShell TEXT, which is
#     exactly why the prefix collision was invisible to the suite.
# ===========================================================================

# ---- FIXTURES: real command lines --------------------------------------------
# Captured 2026-07-23 via `Get-CimInstance Win32_Process`. Truncated in the middle
# of the -cp classpath (elided with "...") but PRESERVING the two things that
# matter: the shared `C:\IBC\IBC.jar` classpath entry and the per-instance
# -DjtsConfigDir / trailing config.ini argument.

# pid 3496 — the java JVM that was LISTENING on 4003 (the live pilot) at capture time.
LIVE_TRADE_JVM_CL = (
    r'"c:\users\andre\appdata\local\programs\common\i4j_jres\oda-jk0qgtemvssflllp'
    r'\17.0.16.0.101-zulu_64\bin\java.exe"  --add-opens=java.base/java.util=ALL-UNNAMED '
    r'-cp  "C:\Jts\ibgateway\1045\jars\batik-all-1.16.jar;...;'
    r'C:\Jts\ibgateway\1045\.install4j\i4jruntime.jar;C:\IBC\IBC.jar"  -Xmx768m '
    r'-DvmOptionsPath=C:\Jts\ibgateway\1045\ibgateway.vmoptions '
    r'-Dinstall4jType=standalone -DjtsConfigDir="C:\IBC-Live-Trade\GatewaySettings" '
    r'-Dibcsessionid="2269714722"  ibcalpha.ibc.IbcGateway '
    r'"C:\IBC-Live-Trade\config.ini" live'
)

# pid 7004 — the cmd.exe launcher shell that started pid 3496.
LIVE_TRADE_CMD_CL = r'"cmd.exe" /c "C:\IBC-Live-Trade\StartGatewayLiveTrade.bat"'

# pid 29236 — the ORPHAN live-trade JVM from the 09:55 self-heal race. Bound to NO
# port, so the port discriminator cannot see it: only the lane markers can.
LIVE_TRADE_ORPHAN_JVM_CL = LIVE_TRADE_JVM_CL.replace("2269714722", "2217120932")

# pid 30728 — the orphan's cmd.exe launcher.
LIVE_TRADE_ORPHAN_CMD_CL = r"cmd /c C:\IBC-Live-Trade\StartGatewayLiveTrade.bat"

# Live-data lane. No live-data gateway was running at capture time; this is built
# from the SAME captured JVM shape with the lane's own config/settings paths, which
# C:\IBC-Live-Data\StartGatewayLiveData.bat sets (CONFIG=C:\IBC-Live-Data\config.ini,
# TWS_SETTINGS_PATH=C:\IBC-Live-Data\GatewaySettings — read from the .bat).
LIVE_DATA_JVM_CL = (LIVE_TRADE_JVM_CL
                    .replace(r"C:\IBC-Live-Trade", r"C:\IBC-Live-Data"))
LIVE_DATA_CMD_CL = r'"cmd.exe" /c "C:\IBC-Live-Data\StartGatewayLiveData.bat"'

# PAPER lane — DERIVED, NOT OBSERVED. No paper gateway was running at capture time,
# so no paper JVM command line could be captured. Derived by reading
# C:\IBC\StartGateway.bat (CONFIG=%SYSTEMDRIVE%\IBC\config.ini, TRADING_MODE=paper,
# TWS_SETTINGS_PATH empty) and C:\IBC\scripts\StartIBC.bat, which does
# `if not defined TWS_SETTINGS_PATH set TWS_SETTINGS_PATH=%TWS_PATH%` (-> C:\Jts) and
# invokes: java ... -DjtsConfigDir="%TWS_SETTINGS_PATH%" %ENTRY_POINT% "%CONFIG%" %MODE%
PAPER_JVM_CL = (
    r'"c:\users\andre\appdata\local\programs\common\i4j_jres\oda-jk0qgtemvssflllp'
    r'\17.0.16.0.101-zulu_64\bin\java.exe"  --add-opens=java.base/java.util=ALL-UNNAMED '
    r'-cp  "C:\Jts\ibgateway\1045\jars\batik-all-1.16.jar;...;'
    r'C:\Jts\ibgateway\1045\.install4j\i4jruntime.jar;C:\IBC\IBC.jar"  -Xmx768m '
    r'-DvmOptionsPath=C:\Jts\ibgateway\1045\ibgateway.vmoptions '
    r'-Dinstall4jType=standalone -DjtsConfigDir="C:\Jts" '
    r'-Dibcsessionid="1234567890"  ibcalpha.ibc.IbcGateway '
    r'"C:\IBC\config.ini" paper'
)
PAPER_CMD_CL = r"cmd /c C:\IBC\StartGateway.bat"

_LIVE_TRADE_ALL = (LIVE_TRADE_JVM_CL, LIVE_TRADE_CMD_CL,
                   LIVE_TRADE_ORPHAN_JVM_CL, LIVE_TRADE_ORPHAN_CMD_CL)
_LIVE_DATA_ALL = (LIVE_DATA_JVM_CL, LIVE_DATA_CMD_CL)
_PAPER_ALL = (PAPER_JVM_CL, PAPER_CMD_CL)


# ---- The exact bug: the shared classpath defeats the naive fixes -------------
def test_fixtures_actually_reproduce_the_bug_conditions():
    """Guard the fixtures themselves: if these stop holding, the tests below stop
    testing the incident. Both naive discriminators MUST match the live-trade JVM."""
    # The original bug: bare 'C:\IBC' matches every live-trade process.
    for cl in _LIVE_TRADE_ALL:
        assert r"C:\IBC" in cl
    # The "obvious" trailing-separator fix STILL matches the live JVMs, because all
    # three lanes load IBC from the shared classpath entry C:\IBC\IBC.jar.
    assert r"C:\IBC\IBC.jar" in LIVE_TRADE_JVM_CL
    assert r"C:\IBC\\" .replace("\\\\", "\\") in LIVE_TRADE_JVM_CL   # i.e. 'C:\IBC\'
    assert r"C:\IBC\IBC.jar" in LIVE_DATA_JVM_CL
    # ...but it does spare the cmd.exe launchers, which is why it looked plausible.
    assert r"C:\IBC" + "\\" not in LIVE_TRADE_CMD_CL


# ---- matches_instance(): the positive discriminator --------------------------
def test_paper_discriminator_does_not_match_live_lanes():
    """THE REGRESSION. The paper instance must not match ANY live-lane command
    line — including the JVMs carrying C:\\IBC\\IBC.jar on their classpath."""
    for cl in _LIVE_TRADE_ALL + _LIVE_DATA_ALL:
        assert gw.matches_instance(cl, gw.PAPER_INSTANCE) is False, cl


def test_paper_discriminator_matches_the_paper_instance():
    for cl in _PAPER_ALL:
        assert gw.matches_instance(cl, gw.PAPER_INSTANCE) is True, cl


def test_live_lane_discriminators_match_only_their_own_lane():
    for cl in _LIVE_TRADE_ALL:
        assert gw.matches_instance(cl, gw.LIVE_TRADE_INSTANCE) is True, cl
        assert gw.matches_instance(cl, gw.LIVE_DATA_INSTANCE) is False, cl
    for cl in _LIVE_DATA_ALL:
        assert gw.matches_instance(cl, gw.LIVE_DATA_INSTANCE) is True, cl
        assert gw.matches_instance(cl, gw.LIVE_TRADE_INSTANCE) is False, cl
    # And the live lanes never match paper (already covered) nor paper the lives.
    for cl in _PAPER_ALL:
        assert gw.matches_instance(cl, gw.LIVE_TRADE_INSTANCE) is False, cl
        assert gw.matches_instance(cl, gw.LIVE_DATA_INSTANCE) is False, cl


# ---- foreign_instance_marker(): the HARD never-kill guard --------------------
def test_never_kill_guard_flags_every_live_process_for_a_paper_kill():
    for cl in _LIVE_TRADE_ALL:
        found = gw.foreign_instance_marker(cl, gw.PAPER_INSTANCE)
        assert found is not None and found[0] == "live-trade", cl
    for cl in _LIVE_DATA_ALL:
        found = gw.foreign_instance_marker(cl, gw.PAPER_INSTANCE)
        assert found is not None and found[0] == "live-data", cl


def test_never_kill_guard_does_not_flag_a_lane_against_itself():
    """The shared C:\\IBC\\IBC.jar classpath entry must NOT make the live lanes look
    foreign to themselves — that would make their own watchdogs unable to recover."""
    for cl in _LIVE_TRADE_ALL:
        assert gw.foreign_instance_marker(cl, gw.LIVE_TRADE_INSTANCE) is None, cl
    for cl in _LIVE_DATA_ALL:
        assert gw.foreign_instance_marker(cl, gw.LIVE_DATA_INSTANCE) is None, cl
    for cl in _PAPER_ALL:
        assert gw.foreign_instance_marker(cl, gw.PAPER_INSTANCE) is None, cl


# ---- should_kill(): the whole predicate -------------------------------------
def _kill(cl, name, instance, pid=3496, gw_pids=(), theta_pids=()):
    return gw.should_kill(name=name, command_line=cl, pid=pid,
                          gw_pids=gw_pids, theta_pids=theta_pids,
                          instance=instance)


def test_paper_kill_spares_the_live_trade_gateway_that_owns_4003():
    """THE INCIDENT, end to end: a PAPER kill running while pid 3496 serves 4003."""
    assert _kill(LIVE_TRADE_JVM_CL, "java.exe", gw.PAPER_INSTANCE, pid=3496) is False
    assert _kill(LIVE_TRADE_CMD_CL, "cmd.exe", gw.PAPER_INSTANCE, pid=7004) is False
    # The unbound orphan too — the port discriminator can't see it, markers must.
    assert _kill(LIVE_TRADE_ORPHAN_JVM_CL, "java.exe", gw.PAPER_INSTANCE,
                 pid=29236) is False
    assert _kill(LIVE_TRADE_ORPHAN_CMD_CL, "cmd.exe", gw.PAPER_INSTANCE,
                 pid=30728) is False
    for cl, nm in ((LIVE_DATA_JVM_CL, "java.exe"), (LIVE_DATA_CMD_CL, "cmd.exe")):
        assert _kill(cl, nm, gw.PAPER_INSTANCE, pid=555) is False


def test_paper_kill_does_kill_the_paper_gateway():
    assert _kill(PAPER_JVM_CL, "java.exe", gw.PAPER_INSTANCE, pid=33576) is True
    assert _kill(PAPER_CMD_CL, "cmd.exe", gw.PAPER_INSTANCE, pid=33500) is True
    # Also via the PRIMARY discriminator alone (owns port 4002).
    assert _kill(PAPER_JVM_CL, "java.exe", gw.PAPER_INSTANCE,
                 pid=33576, gw_pids=[33576]) is True


def test_never_kill_guard_overrides_even_the_port_discriminator():
    """Belt and braces: if the port lookup ever returned a live lane's pid (a wrong
    port, a race, a reused pid), the lane markers must still veto the kill."""
    assert _kill(LIVE_TRADE_JVM_CL, "java.exe", gw.PAPER_INSTANCE,
                 pid=3496, gw_pids=[3496]) is False


def test_live_trade_lane_can_still_kill_its_own_gateway():
    """The guard must not make the live lanes unrecoverable by their own tooling."""
    assert _kill(LIVE_TRADE_JVM_CL, "java.exe", gw.LIVE_TRADE_INSTANCE,
                 pid=3496) is True
    assert _kill(LIVE_DATA_JVM_CL, "java.exe", gw.LIVE_DATA_INSTANCE,
                 pid=555) is True


def test_should_kill_preserves_the_original_spares():
    """ThetaData terminal, python, and non-gateway processes: unchanged behavior."""
    # ThetaData terminal (owns 25503) — spared even though it is a java.exe.
    theta_cl = r'"C:\ThetaTerminal\java.exe" -jar ThetaTerminal.jar IbcGateway'
    assert _kill(theta_cl, "java.exe", gw.PAPER_INSTANCE,
                 pid=15608, theta_pids=[15608]) is False
    # python / pythonw — never.
    assert _kill(PAPER_JVM_CL, "python.exe", gw.PAPER_INSTANCE, pid=1) is False
    assert _kill(PAPER_JVM_CL, "pythonw.exe", gw.PAPER_INSTANCE, pid=1) is False
    # A java that is not an IbcGateway, and a cmd that is not a StartGateway.
    assert _kill(r'java.exe -jar something.jar C:\IBC\config.ini', "java.exe",
                 gw.PAPER_INSTANCE, pid=2) is False
    assert _kill(r'cmd /c C:\IBC\EnableAPI.bat', "cmd.exe",
                 gw.PAPER_INSTANCE, pid=3) is False
    # A null CommandLine (Win32_Process returns None for protected processes).
    assert _kill(None, "java.exe", gw.PAPER_INSTANCE, pid=4) is False


def test_instance_registry_matches_the_clientids_port_topology():
    from connections import clientids
    assert gw.PAPER_INSTANCE.port == clientids.PAPER_PORT == 4002
    assert gw.LIVE_DATA_INSTANCE.port == clientids.LIVE_DATA_PORT == 4001
    assert gw.LIVE_TRADE_INSTANCE.port == clientids.LIVE_TRADE_PORT == 4003
    # No lane's identity marker may be a substring of another's — that property is
    # the whole fix, and it is what `C:\IBC` violated.
    for a in gw.KNOWN_INSTANCES:
        for b in gw.KNOWN_INSTANCES:
            if a.name == b.name:
                continue
            for ma in a.identity_markers():
                for mb in b.identity_markers():
                    assert ma.lower() not in mb.lower(), (ma, mb)


# ---------------------------------------------------------------------------
# 11. _kill_gateway_processes() wiring — enumerate (PowerShell) -> decide (Python)
#     -> kill. No real PowerShell/gateway: subprocess.run is monkeypatched.
# ---------------------------------------------------------------------------
class _FakeCompletedProcess:
    def __init__(self, stdout=""):
        self.stdout = stdout
        self.stderr = ""


def _fake_ps(monkeypatch, enum_payload):
    """Stub subprocess.run: 1st call returns the enumeration JSON, 2nd returns the
    killed-pid JSON echoing whatever pids the kill script was asked to stop.
    Returns the list of captured scripts."""
    scripts = []

    def fake_run(cmd, **_kw):
        script = cmd[-1]
        scripts.append(script)
        if "Win32_Process" in script:
            return _FakeCompletedProcess(stdout=json.dumps(enum_payload))
        # Kill phase: echo back the pids embedded in the generated script.
        pids = [int(x) for x in re.findall(r"\d+", script.split("foreach")[1]
                                           .split("Stop-Process")[0])]
        return _FakeCompletedProcess(stdout=json.dumps(pids))

    monkeypatch.setattr(gw.subprocess, "run", fake_run)
    return scripts


_REAL_BOX_2026_07_23 = {
    "theta": [15608],
    "gw": [],                       # nothing was listening on 4002
    "procs": [
        {"pid": 30728, "name": "cmd.exe", "cl": LIVE_TRADE_ORPHAN_CMD_CL},
        {"pid": 29236, "name": "java.exe", "cl": LIVE_TRADE_ORPHAN_JVM_CL},
        {"pid": 7004, "name": "cmd.exe", "cl": LIVE_TRADE_CMD_CL},
        {"pid": 3496, "name": "java.exe", "cl": LIVE_TRADE_JVM_CL},
    ],
}


def test_paper_kill_against_the_real_box_kills_nothing(monkeypatch):
    """Replay of the incident's actual process table: a PAPER kill must kill ZERO
    processes. Before the fix this killed all four, including pid 3496 on 4003."""
    _fake_ps(monkeypatch, _REAL_BOX_2026_07_23)
    assert gw._kill_gateway_processes(port=gw.PAPER_INSTANCE.port,
                                      instance=gw.PAPER_INSTANCE) == []


def test_paper_kill_with_defaults_also_kills_nothing(monkeypatch):
    """Defaults are now safe too — the call sites pass explicitly, but a defaults-
    only call (which is what caused the incident) must no longer be destructive."""
    _fake_ps(monkeypatch, _REAL_BOX_2026_07_23)
    assert gw._kill_gateway_processes() == []


def test_paper_kill_kills_the_paper_processes_and_only_those(monkeypatch):
    payload = dict(_REAL_BOX_2026_07_23)
    payload = {
        "theta": [15608],
        "gw": [33576],
        "procs": list(_REAL_BOX_2026_07_23["procs"]) + [
            {"pid": 33576, "name": "java.exe", "cl": PAPER_JVM_CL},
            {"pid": 33500, "name": "cmd.exe", "cl": PAPER_CMD_CL},
        ],
    }
    _fake_ps(monkeypatch, payload)
    killed = gw._kill_gateway_processes(port=gw.PAPER_INSTANCE.port,
                                        instance=gw.PAPER_INSTANCE)
    assert sorted(killed) == [33500, 33576]


def test_live_trade_kill_targets_only_the_live_trade_lane(monkeypatch):
    payload = {
        "theta": [15608],
        "gw": [3496],
        "procs": list(_REAL_BOX_2026_07_23["procs"]) + [
            {"pid": 33576, "name": "java.exe", "cl": PAPER_JVM_CL},
        ],
    }
    _fake_ps(monkeypatch, payload)
    killed = gw._kill_gateway_processes(port=gw.LIVE_TRADE_INSTANCE.port,
                                        instance=gw.LIVE_TRADE_INSTANCE)
    assert sorted(killed) == [3496, 7004, 29236, 30728]
    assert 33576 not in killed


def test_kill_refuses_when_port_and_instance_disagree(monkeypatch):
    """A port/instance mismatch is a wiring bug — refuse rather than guess. A
    refused kill leaves a wedged gateway (loud, recoverable); a wrong kill doesn't."""
    scripts = _fake_ps(monkeypatch, _REAL_BOX_2026_07_23)
    assert gw._kill_gateway_processes(port=4003,
                                      instance=gw.PAPER_INSTANCE) == []
    assert scripts == []          # never even enumerated, let alone killed


def test_enumeration_script_keeps_the_thetadata_and_port_carve_outs(monkeypatch):
    """The PowerShell half only ENUMERATES now, but must still supply the two pid
    sets the Python predicate needs: ThetaData (25503) and this instance's port."""
    scripts = _fake_ps(monkeypatch, {"theta": [], "gw": [], "procs": []})
    gw._kill_gateway_processes(port=gw.PAPER_INSTANCE.port,
                               instance=gw.PAPER_INSTANCE)
    enum = scripts[0]
    assert "-LocalPort 25503" in enum
    assert f"-LocalPort {gw.ibkr_paper.PAPER_PORT}" in enum
    assert gw.ibkr_paper.PAPER_PORT == 4002
    assert "IbcGateway" in enum and "StartGateway" in enum
    # The old, over-broad substring parameter is GONE from the generated script.
    assert "dirSubstring" not in enum
