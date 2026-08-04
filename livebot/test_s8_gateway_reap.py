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
