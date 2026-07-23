r"""
test_gateway_arm_restart_elevated.py — the collateral-damage post-condition.

REGRESSION for the 2026-07-23 incident
(docs/INCIDENT_2026-07-23_arm_restart_killed_live_gateway.md, conductor #46):
`arming.py arm` killed the S8 live-pilot Gateway on port 4003 and then wrote
`{"ok": true, "detail": "restarted and serving data"}`, because it only ever checked
that the PAPER gateway came back. The success record was actively misleading.

PAPER ONLY; nothing here touches a real Gateway, PowerShell, or socket. The kill,
the relaunch, and the listener probe are all mocked.
"""
from __future__ import annotations

import json

import pytest

import gateway_arm_restart_elevated as gare


# ---------------------------------------------------------------------------
# The pure before/after comparison.
# ---------------------------------------------------------------------------
def test_collateral_damage_none_when_nothing_lost():
    assert gare.collateral_damage(set(), set()) == []
    assert gare.collateral_damage({4003}, {4003}) == []
    assert gare.collateral_damage({4001, 4003}, {4001, 4003}) == []


def test_collateral_damage_names_the_lost_lane():
    assert gare.collateral_damage({4003}, set()) == ["live-trade (4003)"]
    assert gare.collateral_damage({4001, 4003}, {4001}) == ["live-trade (4003)"]
    assert gare.collateral_damage({4001, 4003}, set()) == [
        "live-data (4001)", "live-trade (4003)"]


def test_collateral_damage_ignores_a_lane_that_came_UP_during_the_restart():
    """A lane appearing is not damage — only a lane that was up and is now down."""
    assert gare.collateral_damage(set(), {4003}) == []


def test_other_lane_ports_do_not_include_the_paper_port():
    assert 4002 not in gare.OTHER_LANE_PORTS
    assert set(gare.OTHER_LANE_PORTS) == {4001, 4003}


# ---------------------------------------------------------------------------
# run() / main() — the loud failure.
# ---------------------------------------------------------------------------
def _wire(monkeypatch, *, listeners, came_up=True, killed=(33576,)):
    """Mock the kill, the relaunch and the listener probe. `listeners` is a list of
    the sets returned by successive _listening_ports() calls (before, after)."""
    seq = list(listeners)
    calls = {"kill": 0, "launch": 0, "probe": 0}

    def fake_probe(_ports):
        calls["probe"] += 1
        return seq.pop(0)

    def fake_kill(**kwargs):
        calls["kill"] += 1
        calls["kill_kwargs"] = kwargs
        return list(killed)

    monkeypatch.setattr(gare, "_listening_ports", fake_probe)
    monkeypatch.setattr(gare, "_kill_gateway_processes", fake_kill)
    monkeypatch.setattr(gare.ibkr_paper, "gateway_running", lambda: False)
    monkeypatch.setattr(gare.ibkr_paper, "ensure_gateway", lambda: came_up)
    monkeypatch.setattr(gare.time, "sleep", lambda _s: None)
    return calls


def test_run_reports_failure_when_a_live_lane_was_killed(monkeypatch):
    """THE INCIDENT: 4003 was LISTENING before the kill and is gone afterwards.
    Even though the paper gateway came up fine, this must NOT report success."""
    _wire(monkeypatch, listeners=[{4003}, set()], came_up=True)
    ok, detail = gare.run()
    assert ok is False
    assert "COLLATERAL DAMAGE" in detail
    assert "live-trade (4003)" in detail
    assert "restarted and serving data" not in detail


def test_run_succeeds_when_the_live_lane_survives(monkeypatch):
    _wire(monkeypatch, listeners=[{4003}, {4003}], came_up=True)
    ok, detail = gare.run()
    assert ok is True
    assert detail == "restarted and serving data"


def test_run_reports_paper_failure_when_gateway_did_not_come_back(monkeypatch):
    _wire(monkeypatch, listeners=[{4003}, {4003}], came_up=False)
    ok, detail = gare.run()
    assert ok is False
    assert "did not come back up" in detail


def test_run_snapshots_before_the_kill(monkeypatch):
    """The BEFORE snapshot must be taken before the kill, or the comparison is
    meaningless (a lane killed by us would never appear in 'before')."""
    order = []

    monkeypatch.setattr(gare, "_listening_ports",
                        lambda _p: (order.append("probe"), {4003})[1])
    monkeypatch.setattr(gare, "_kill_gateway_processes",
                        lambda **_k: (order.append("kill"), [1])[1])
    monkeypatch.setattr(gare.ibkr_paper, "gateway_running", lambda: False)
    monkeypatch.setattr(gare.ibkr_paper, "ensure_gateway", lambda: True)
    monkeypatch.setattr(gare.time, "sleep", lambda _s: None)

    gare.run()
    assert order[0] == "probe"
    assert order[1] == "kill"


def test_kill_is_called_with_explicit_paper_lane_scoping(monkeypatch):
    """No more zero-argument kill: port AND instance are stated at the call site."""
    calls = _wire(monkeypatch, listeners=[{4003}, {4003}], came_up=True)
    gare.run()
    assert calls["kill"] == 1
    assert calls["kill_kwargs"]["port"] == gare.PAPER_INSTANCE.port == 4002
    assert calls["kill_kwargs"]["instance"] is gare.PAPER_INSTANCE


def test_main_writes_the_failure_to_the_state_file_and_exits_nonzero(
        monkeypatch, tmp_path):
    """The state file is what `arming.restart_gateway()` polls. It must carry
    ok=False and the reason — never a bare success while a live lane is down."""
    state = tmp_path / "gateway_arm_restart_state.json"
    monkeypatch.setattr(gare, "STATE_FILE", str(state))
    _wire(monkeypatch, listeners=[{4003}, set()], came_up=True)

    rc = gare.main()
    assert rc == 1
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert "COLLATERAL DAMAGE" in payload["detail"]
    assert "live-trade (4003)" in payload["detail"]


def test_main_writes_success_when_clean(monkeypatch, tmp_path):
    state = tmp_path / "gateway_arm_restart_state.json"
    monkeypatch.setattr(gare, "STATE_FILE", str(state))
    _wire(monkeypatch, listeners=[{4003}, {4003}], came_up=True)

    rc = gare.main()
    assert rc == 0
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["detail"] == "restarted and serving data"
