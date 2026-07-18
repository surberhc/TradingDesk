"""test_s8_reap.py — OFFLINE tests for the PRE-LAUNCH / TEARDOWN ORPHAN REAPER.

100% offline: NO broker, NO gateway, NO network, NO real process is ever killed and NO
PowerShell is ever spawned. The pid-liveness check, the command-line lookup, the process
scan and the kill callable are all injected fakes; lock files live under a pytest tmp_path.

What is proved:
  * kills a stale MATCHING process (found via the lock record) and clears the lock;
  * finds and kills a matching orphan whose lock was already unlinked (the SCAN source);
  * SAFETY — REFUSES to kill a live holder whose command line does NOT match, and REFUSES
    an unreadable/empty command line, leaving both the process and its lock alone
    (mirrors test_s8_lock's refusal tests — same verification, same guarantee);
  * clears a stale lock whose holder is dead, killing nothing;
  * is a quiet NO-OP when nothing is running and no lock exists;
  * NEVER raises — a probe that explodes, a kill that explodes, an unusable lock path all
    return a result dict instead, because a reap failure must never prevent a launch;
  * ``main`` always exits 0.

Run (from C:\\TradingDesk\\livebot):
    powershell -Command "$env:PYTHONPATH=''; C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s8_reap.py -q"
"""

from __future__ import annotations

import json

import s8_reap


MARKER = "s8_service.py"
ME = 4242
ORPHAN = 9999
MATCHING_CMDLINE = r"C:\python.exe C:\TradingDesk\livebot\s8_service.py"


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


def _reap(path, *, alive, cmdline, kill, logs, scan=lambda m: []):
    return s8_reap.reap_one(
        "s8_service", MARKER, lock_path=path, my_pid=ME,
        is_alive=lambda p: alive, get_cmdline=lambda p: cmdline,
        find_pids=scan, kill=kill, log=logs.append,
    )


# --------------------------------------------------------------------------- #
# THE HAPPY PATH — reap the orphan, clear the lock
# --------------------------------------------------------------------------- #

def test_kills_stale_matching_holder_and_clears_lock(tmp_path):
    p = tmp_path / "state" / "s8_service.lock"
    _write_lock(p, ORPHAN)
    kill, logs = _Killer(), []
    r = _reap(p, alive=True, cmdline=MATCHING_CMDLINE, kill=kill, logs=logs)
    assert kill.calls == [ORPHAN]
    assert r["killed"] == [ORPHAN]
    assert r["refused"] == []
    assert r["lock_cleared"] is True
    assert not p.exists()
    assert any("reaped stale orphan pid=9999" in m for m in logs)


def test_scan_finds_an_orphan_with_no_lock_file(tmp_path):
    """An orphan whose lock was already unlinked is still reaped, via the scan source."""
    p = tmp_path / "state" / "s8_service.lock"          # deliberately does not exist
    kill, logs = _Killer(), []
    r = _reap(p, alive=True, cmdline=MATCHING_CMDLINE, kill=kill, logs=logs,
              scan=lambda m: [(ORPHAN, MATCHING_CMDLINE)])
    assert kill.calls == [ORPHAN]
    assert r["killed"] == [ORPHAN] and r["scanned"] is True


def test_never_kills_our_own_pid(tmp_path):
    p = tmp_path / "state" / "s8_service.lock"
    _write_lock(p, ME)
    kill, logs = _Killer(), []
    r = _reap(p, alive=True, cmdline=MATCHING_CMDLINE, kill=kill, logs=logs,
              scan=lambda m: [(ME, MATCHING_CMDLINE)])
    assert kill.calls == []
    assert r["killed"] == []


# --------------------------------------------------------------------------- #
# SAFETY — the load-bearing refusals (mirrors test_s8_lock)
# --------------------------------------------------------------------------- #

def test_refuses_to_kill_a_nonmatching_live_process(tmp_path):
    """A live holder that is NOT our script is never killed, and its lock is left alone."""
    p = tmp_path / "state" / "s8_service.lock"
    _write_lock(p, ORPHAN)
    kill, logs = _Killer(), []
    r = _reap(p, alive=True, cmdline=r"C:\Windows\System32\notepad.exe", kill=kill,
              logs=logs)
    assert kill.calls == []
    assert r["refused"] == [ORPHAN] and r["killed"] == []
    assert r["lock_cleared"] is False
    assert p.exists() and json.loads(p.read_text())["pid"] == ORPHAN
    assert any("REFUSING to kill live pid=9999" in m for m in logs)


def test_refuses_an_unreadable_cmdline(tmp_path):
    """An unidentifiable live process must NEVER be treated as ours."""
    p = tmp_path / "state" / "s8_service.lock"
    _write_lock(p, ORPHAN)
    kill, logs = _Killer(), []
    r = _reap(p, alive=True, cmdline=None, kill=kill, logs=logs)
    assert kill.calls == [] and r["refused"] == [ORPHAN]
    assert p.exists()


def test_scan_hit_is_re_verified_before_any_kill(tmp_path):
    """Even a scan hit is re-checked against the live cmdline — the scan cannot widen
    the blast radius."""
    p = tmp_path / "state" / "s8_service.lock"
    kill, logs = _Killer(), []
    r = _reap(p, alive=True, cmdline=r"C:\Windows\explorer.exe", kill=kill, logs=logs,
              scan=lambda m: [(ORPHAN, MATCHING_CMDLINE)])
    assert kill.calls == [] and r["refused"] == [ORPHAN]


# --------------------------------------------------------------------------- #
# STALE LOCK / NO-OP
# --------------------------------------------------------------------------- #

def test_dead_holder_clears_the_lock_and_kills_nothing(tmp_path):
    p = tmp_path / "state" / "s8_service.lock"
    _write_lock(p, ORPHAN)
    kill, logs = _Killer(), []
    r = _reap(p, alive=False, cmdline=MATCHING_CMDLINE, kill=kill, logs=logs)
    assert kill.calls == []
    assert r["lock_cleared"] is True and not p.exists()


def test_no_op_when_nothing_is_running(tmp_path):
    p = tmp_path / "state" / "s8_service.lock"          # no lock, no processes
    kill, logs = _Killer(), []
    r = _reap(p, alive=False, cmdline=None, kill=kill, logs=logs)
    assert kill.calls == []
    assert r == {"name": "s8_service", "killed": [], "refused": [], "lock_cleared": False,
                 "scanned": True, "error": None}


def test_failed_kill_is_reported_and_the_lock_is_left(tmp_path):
    p = tmp_path / "state" / "s8_service.lock"
    _write_lock(p, ORPHAN)
    kill, logs = _Killer(succeed=False), []
    r = _reap(p, alive=True, cmdline=MATCHING_CMDLINE, kill=kill, logs=logs)
    assert kill.calls == [ORPHAN] and r["killed"] == [] and r["refused"] == [ORPHAN]
    assert p.exists()                    # a live process we failed to kill still holds it


# --------------------------------------------------------------------------- #
# NEVER RAISES — a reap failure must never stop a launch
# --------------------------------------------------------------------------- #

def _boom(*_a, **_k):
    raise RuntimeError("probe exploded")


def test_exploding_scan_is_swallowed(tmp_path):
    p = tmp_path / "state" / "s8_service.lock"
    _write_lock(p, ORPHAN)
    kill, logs = _Killer(), []
    r = _reap(p, alive=True, cmdline=MATCHING_CMDLINE, kill=kill, logs=logs, scan=_boom)
    assert r["scanned"] is False
    assert r["killed"] == [ORPHAN]       # the lock source still worked


def test_exploding_liveness_probe_never_raises(tmp_path):
    p = tmp_path / "state" / "s8_service.lock"
    _write_lock(p, ORPHAN)
    logs = []
    r = s8_reap.reap_one("s8_service", MARKER, lock_path=p, my_pid=ME,
                         is_alive=_boom, get_cmdline=lambda x: MATCHING_CMDLINE,
                         find_pids=lambda m: [], kill=_Killer(), log=logs.append)
    assert r["error"] is not None and r["killed"] == []


def test_exploding_kill_never_raises(tmp_path):
    p = tmp_path / "state" / "s8_service.lock"
    _write_lock(p, ORPHAN)
    logs = []
    r = s8_reap.reap_one("s8_service", MARKER, lock_path=p, my_pid=ME,
                         is_alive=lambda x: True, get_cmdline=lambda x: MATCHING_CMDLINE,
                         find_pids=lambda m: [], kill=_boom, log=logs.append)
    assert r["error"] is not None


def test_unknown_target_is_reported_not_raised():
    out = s8_reap.reap(["not_a_pilot_process"])
    assert len(out) == 1 and "unknown target" in out[0]["error"]


def test_reap_defaults_to_both_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("S8_PILOT_ROOT", str(tmp_path))
    out = s8_reap.reap(is_alive=lambda p: False, get_cmdline=lambda p: None,
                       find_pids=lambda m: [], kill=_Killer(), log=lambda m: None)
    assert [r["name"] for r in out] == ["s8_service", "s8_collector"]


def test_main_always_exits_zero(tmp_path, monkeypatch, capsys):
    """A nonzero exit would be read by the wrapper as a reason to stop launching."""
    monkeypatch.setenv("S8_PILOT_ROOT", str(tmp_path))
    monkeypatch.setattr(s8_reap, "find_pids_by_marker", lambda m, **k: [])
    assert s8_reap.main(["s8_service"]) == 0
    assert s8_reap.main(["--all"]) == 0
    assert (tmp_path / "logs" / s8_reap.REAP_LOG_NAME).exists()   # its OWN log, not a day log


# --------------------------------------------------------------------------- #
# The real scan's PARSER (fed fake PowerShell output — no PowerShell is spawned)
# --------------------------------------------------------------------------- #

class _Out:
    def __init__(self, stdout):
        self.stdout = stdout


def test_scan_parses_and_filters_by_marker(monkeypatch):
    monkeypatch.setattr(s8_reap.os, "name", "nt")
    payload = json.dumps([
        {"ProcessId": 111, "CommandLine": MATCHING_CMDLINE},
        {"ProcessId": 222, "CommandLine": r"C:\python.exe s8_collector.py"},
        {"ProcessId": 333, "CommandLine": r"C:\Windows\notepad.exe"},
    ])
    hits = s8_reap.find_pids_by_marker(MARKER, run=lambda *a, **k: _Out(payload))
    assert [p for p, _c in hits] == [111]


def test_scan_returns_none_when_it_cannot_answer(monkeypatch):
    monkeypatch.setattr(s8_reap.os, "name", "nt")
    assert s8_reap.find_pids_by_marker(MARKER, run=lambda *a, **k: _Out("PROBE_FAILED")) is None
    assert s8_reap.find_pids_by_marker(MARKER, run=lambda *a, **k: _Out("")) is None
    assert s8_reap.find_pids_by_marker(MARKER, run=lambda *a, **k: _Out("not json")) is None
    assert s8_reap.find_pids_by_marker(MARKER, run=_boom) is None
