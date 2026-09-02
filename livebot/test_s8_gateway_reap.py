"""test_s8_gateway_reap.py — OFFLINE tests for the GATEWAY ORPHAN REAPER.

100% offline: NO gateway, NO network, NO real process killed, NO PowerShell spawned. The
port-owner probe, the gateway scan, the liveness check, the cmdline lookup and the kill
callable are all injected fakes.

Run (from C:\\TradingDesk\\livebot):
    powershell -Command "$env:PYTHONPATH=''; C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s8_gateway_reap.py -q"
"""

from __future__ import annotations

import s8_gateway_reap as gr


LT = r'java -DjtsConfigDir="C:\IBC-Live-Trade\GatewaySettings" ... "C:\IBC-Live-Trade\config.ini"'


def _gw(pid, age, cmdline=LT):
    return {"pid": pid, "cmdline": cmdline, "age_secs": age}


def test_keeps_the_port_4003_owner():
    killed = []
    res = gr.reap_orphans(
        grace_secs=180,
        get_owner=lambda: 100,
        find_gateways=lambda: [_gw(100, age=9999)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=lambda p: killed.append(p) or True,
        my_pid=1,
    )
    assert killed == []
    assert res["spared"] == [100]
    assert res["killed"] == []


def test_reaps_aged_unbound_orphan():
    killed = []
    res = gr.reap_orphans(
        grace_secs=180,
        get_owner=lambda: 100,
        find_gateways=lambda: [_gw(100, 9999), _gw(200, 9999)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=lambda p: killed.append(p) or True,
        my_pid=1,
    )
    assert killed == [200]
    assert res["killed"] == [200]
    assert 100 in res["spared"]


def test_spares_young_unbound_gateway():
    killed = []
    res = gr.reap_orphans(
        grace_secs=180,
        get_owner=lambda: None,
        find_gateways=lambda: [_gw(200, age=30)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=lambda p: killed.append(p) or True,
        my_pid=1,
    )
    assert killed == []
    assert res["spared"] == [200]


def test_reaps_aged_orphan_even_when_nobody_bound():
    killed = []
    res = gr.reap_orphans(
        grace_secs=180,
        get_owner=lambda: None,
        find_gateways=lambda: [_gw(200, age=9999)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=lambda p: killed.append(p) or True,
        my_pid=1,
    )
    assert killed == [200]


def test_refuses_all_when_owner_unknown():
    killed = []
    res = gr.reap_orphans(
        grace_secs=180,
        get_owner=lambda: gr.UNKNOWN,
        find_gateways=lambda: [_gw(200, age=9999)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=lambda p: killed.append(p) or True,
        my_pid=1,
    )
    assert killed == []
    assert res["aborted"] == "port-4003 owner undeterminable"


def test_refuses_all_when_scan_fails():
    killed = []
    res = gr.reap_orphans(
        grace_secs=180,
        get_owner=lambda: 100,
        find_gateways=lambda: None,
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=lambda p: killed.append(p) or True,
        my_pid=1,
    )
    assert killed == []
    assert res["aborted"] == "gateway scan failed"


def test_refuses_candidate_whose_cmdline_no_longer_matches():
    killed = []
    res = gr.reap_orphans(
        grace_secs=180,
        get_owner=lambda: 100,
        find_gateways=lambda: [_gw(200, age=9999)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: r'java something C:\IBC\config.ini',
        kill=lambda p: killed.append(p) or True,
        my_pid=1,
    )
    assert killed == []
    assert res["refused"] == [200]


def test_noop_when_no_gateways():
    res = gr.reap_orphans(
        get_owner=lambda: None,
        find_gateways=lambda: [],
        my_pid=1,
    )
    assert res["killed"] == [] and res["spared"] == [] and res["refused"] == []
    assert res["considered"] == 0


def test_never_raises_on_exploding_probe():
    def boom():
        raise RuntimeError("probe blew up")
    res = gr.reap_orphans(get_owner=boom, my_pid=1)
    assert res["error"] is not None
    assert res["killed"] == []


def test_captures_error_on_exploding_kill():
    def boom(pid):
        raise RuntimeError("kill blew up")
    res = gr.reap_orphans(
        grace_secs=180,
        get_owner=lambda: 100,
        find_gateways=lambda: [_gw(200, age=9999)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=boom,
        my_pid=1,
    )
    assert res["killed"] == []
    assert res["error"] is not None


def test_marker_never_matches_bare_ibc_prefix():
    assert gr._cmdline_is_live_trade(r'java "C:\IBC\config.ini"') is False
    assert gr._cmdline_is_live_trade(r'java "C:\IBC-Live-Data\config.ini"') is False
    assert gr._cmdline_is_live_trade(r'java "C:\IBC-Live-Trade\config.ini"') is True
    assert gr._cmdline_is_live_trade(None) is False
    assert gr._cmdline_is_live_trade("") is False


def test_main_returns_zero(monkeypatch):
    monkeypatch.setattr(gr, "reap_orphans", lambda **kw: {
        "owner": None, "considered": 0, "killed": [], "spared": [], "refused": [],
        "aborted": None, "error": None,
    })
    monkeypatch.setattr(gr, "default_reap_log_path", lambda: None)
    assert gr.main([]) == 0


def test_spares_login_window_gateway_when_nobody_bound():
    # The 2026-08-04 fix: nobody owns 4003 and a gateway is past the short boot grace but
    # still within the login/2FA window -> it is a login in progress, NOT an orphan. Spare
    # it. (Before the fix this pid was killed, thrashing the human's 2FA.)
    killed = []
    res = gr.reap_orphans(
        grace_secs=180,
        login_grace_secs=1800,
        get_owner=lambda: None,
        find_gateways=lambda: [_gw(200, age=600)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=lambda p: killed.append(p) or True,
        my_pid=1,
    )
    assert killed == []
    assert res["spared"] == [200]


def test_reaps_login_window_gateway_once_past_login_grace():
    # A genuinely hung unbound gateway (nobody owns 4003) is still cleaned once it ages past
    # the login window.
    killed = []
    res = gr.reap_orphans(
        grace_secs=180,
        login_grace_secs=1800,
        get_owner=lambda: None,
        find_gateways=lambda: [_gw(200, age=2000)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=lambda p: killed.append(p) or True,
        my_pid=1,
    )
    assert killed == [200]


def test_owner_present_reaps_orphan_on_short_boot_grace():
    # When a live session already OWNS 4003, a second unbound gateway past the SHORT boot
    # grace is a true port-race orphan and is reaped promptly (the long login grace does NOT
    # apply once there is a winner).
    killed = []
    res = gr.reap_orphans(
        grace_secs=180,
        login_grace_secs=1800,
        get_owner=lambda: 100,
        find_gateways=lambda: [_gw(100, age=9999), _gw(200, age=600)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=lambda p: killed.append(p) or True,
        my_pid=1,
    )
    assert killed == [200]
    assert 100 in res["spared"]


# --------------------------------------------------------------------------- #
# DEAD-AUTH detection: the login grace protects a PENDING 2FA, not a failed one
# (2026-09-02: a dead-auth gateway squatted from 08:14 through the market open)
# --------------------------------------------------------------------------- #

import datetime


def _log(*events):
    """Build a launcher log from ('YYYY-MM-DD HH:MM:SS.mmm', marker) pairs."""
    return chr(10).join(f"{ts} INFO  [JTS-AuthDispatcherS2-18] - {m}" for ts, m in events)


T0 = datetime.datetime(2026, 9, 2, 8, 30, 0)


def test_auth_outcome_reads_failure_after_start():
    text = _log(("2026-09-02 08:14:13.279", "Authorization failed: SUPPRESS_MESSAGE_BOX"))
    assert gr.auth_outcome_after(text, datetime.datetime(2026, 9, 2, 8, 0, 0)) == "FAILED"


def test_auth_outcome_ignores_failure_from_a_previous_instance():
    """A failure stamped BEFORE this process started belongs to its predecessor."""
    text = _log(("2026-09-02 08:14:13.279", "Authorization failed: SUPPRESS_MESSAGE_BOX"))
    assert gr.auth_outcome_after(text, datetime.datetime(2026, 9, 2, 8, 29, 47)) is None


def test_auth_outcome_retry_success_inside_same_process_is_not_failed():
    """Fail-then-succeed in one process is HEALTHY — the last event decides."""
    text = _log(("2026-09-02 08:14:13.279", "Authorization failed: x"),
                ("2026-09-02 08:16:02.499", "Authentication completed."))
    assert gr.auth_outcome_after(text, datetime.datetime(2026, 9, 2, 8, 0, 0)) == "OK"


def test_auth_outcome_none_on_unreadable_or_undated():
    assert gr.auth_outcome_after(None, T0) is None
    assert gr.auth_outcome_after("", T0) is None
    assert gr.auth_outcome_after("no timestamp Authorization failed", T0) is None
    assert gr.auth_outcome_after("Authorization failed", None) is None


def test_gateway_auth_failed_is_false_when_log_unreadable():
    """Absence of evidence must NEVER license a kill."""
    assert gr.gateway_auth_failed(200, 9999, read=lambda p: None) is False
    def boom(_p):
        raise OSError("nope")
    assert gr.gateway_auth_failed(200, 9999, read=boom) is False


def test_dead_auth_gateway_is_reaped_on_the_short_boot_grace():
    """The 2026-09-02 case: unbound, 20 min old, auth already failed -> reap now."""
    killed = []
    res = gr.reap_orphans(
        grace_secs=180, login_grace_secs=1800,
        get_owner=lambda: None,
        find_gateways=lambda: [_gw(13020, age=1200)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=lambda p: killed.append(p) or True,
        auth_failed=lambda pid, age: True,
        my_pid=1,
    )
    assert killed == [13020]
    assert res["killed"] == [13020]


def test_pending_2fa_is_still_spared_for_the_full_login_grace():
    """The regression that matters: a LIVE 2FA prompt must survive untouched."""
    killed = []
    res = gr.reap_orphans(
        grace_secs=180, login_grace_secs=1800,
        get_owner=lambda: None,
        find_gateways=lambda: [_gw(5824, age=1200)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=lambda p: killed.append(p) or True,
        auth_failed=lambda pid, age: False,
        my_pid=1,
    )
    assert killed == []
    assert res["spared"] == [5824]


def test_dead_auth_still_respects_the_boot_grace():
    """Even a failed login is spared while younger than the boot grace."""
    killed = []
    gr.reap_orphans(
        grace_secs=180, login_grace_secs=1800,
        get_owner=lambda: None,
        find_gateways=lambda: [_gw(13020, age=60)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=lambda p: killed.append(p) or True,
        auth_failed=lambda pid, age: True,
        my_pid=1,
    )
    assert killed == []


def test_dead_auth_probe_never_applies_to_the_port_owner():
    """The 4003 owner is spared before the probe is ever consulted."""
    killed = []
    def explode(pid, age):
        raise AssertionError("auth probe must not be consulted for the port owner")
    res = gr.reap_orphans(
        grace_secs=180,
        get_owner=lambda: 5824,
        find_gateways=lambda: [_gw(5824, age=9999)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=lambda p: killed.append(p) or True,
        auth_failed=explode,
        my_pid=1,
    )
    assert killed == []
    assert res["spared"] == [5824]


def test_exploding_auth_probe_falls_back_to_the_full_login_grace():
    killed = []
    def boom(pid, age):
        raise RuntimeError("probe died")
    gr.reap_orphans(
        grace_secs=180, login_grace_secs=1800,
        get_owner=lambda: None,
        find_gateways=lambda: [_gw(13020, age=1200)],
        is_alive=lambda p: True,
        get_cmdline=lambda p: LT,
        kill=lambda p: killed.append(p) or True,
        auth_failed=boom,
        my_pid=1,
    )
    assert killed == []


def test_auth_outcome_ignores_evidence_written_after_the_decision():
    """All instances share one launcher.log; a SUCCESSOR's success must not retroactively
    clear a failure that had already happened at decision time."""
    text = _log(("2026-09-02 08:14:13.279", "Authorization failed: x"),
                ("2026-09-02 08:30:02.499", "Authentication completed."))
    start = datetime.datetime(2026, 9, 2, 8, 0, 0)
    assert gr.auth_outcome_after(text, start, datetime.datetime(2026, 9, 2, 8, 20, 3)) == "FAILED"
    # unbounded, the successor's line wins -- which is why the bound exists
    assert gr.auth_outcome_after(text, start) == "OK"
