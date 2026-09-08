"""Tests for the LAUNCH AUTHORIZATION GATE in ibkr_live_trade.ensure_gateway.

Offline: no real gateway, no network. subprocess.Popen is replaced by a spy so a
launch is directly observable.

WHAT THIS GUARDS (conductor #82/#83, 2026-09-08). Starting the live-trading gateway
sends an IBKR Mobile 2FA push to Andrew's phone, and every unanswered push fails a
login IBKR counts. Before this gate, every automated caller could launch: the S8
collector/service relaunched on mid-session disconnect with a 5-minute alert dedup but
NO hard cap, so a gateway down through one 08:05-15:00 session could ding him dozens of
times, and three dailyreport checks each launched on their own schedule. Andrew travels.

The contract: nothing launches unless a human authorized it. The tap-to-launch path
(s8_desk_launch_link) runs the .cmd directly and never comes through here, so it is
unaffected by this gate.
"""
import pytest

from connections import ibkr_live_trade


class PopenSpy:
    def __init__(self):
        self.call_count = 0

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        return self


@pytest.fixture
def spy(monkeypatch, tmp_path):
    s = PopenSpy()
    monkeypatch.setattr(ibkr_live_trade.subprocess, "Popen", s)
    monkeypatch.setattr(ibkr_live_trade, "GATEWAY_LAUNCH_LOCK",
                        str(tmp_path / "state" / "live_trade" / "gateway_launch.lock"))
    monkeypatch.setattr(ibkr_live_trade.time, "sleep", lambda *_a, **_k: None)
    return s


def test_down_gateway_is_not_launched_by_default(spy, monkeypatch):
    monkeypatch.setattr(ibkr_live_trade, "gateway_running", lambda *a, **k: False)

    assert ibkr_live_trade.ensure_gateway() is False
    assert spy.call_count == 0, "an unauthorized caller must NEVER start the gateway"


def test_repeated_calls_still_never_launch(spy, monkeypatch):
    """The shape that actually hurt: a retry loop calling this over and over."""
    monkeypatch.setattr(ibkr_live_trade, "gateway_running", lambda *a, **k: False)

    for _ in range(50):
        assert ibkr_live_trade.ensure_gateway() is False
    assert spy.call_count == 0, "50 retries must still produce zero 2FA pushes"


def test_healthy_gateway_short_circuits_without_launching(spy, monkeypatch):
    monkeypatch.setattr(ibkr_live_trade, "gateway_running", lambda *a, **k: True)

    assert ibkr_live_trade.ensure_gateway() is True
    assert spy.call_count == 0


def test_explicit_authorization_still_launches(spy, monkeypatch):
    """A human-driven path must keep working — the gate is not a wall."""
    monkeypatch.setattr(ibkr_live_trade, "gateway_running", lambda *a, **k: False)
    monkeypatch.setattr(ibkr_live_trade, "_poll_until_up", lambda *a, **k: True)
    # No listener on 4003, or the orphan-prevention re-check correctly refuses to spawn
    # a second gateway. (A real gateway is often up on this machine while tests run.)
    monkeypatch.setattr(ibkr_live_trade, "port_listening", lambda *a, **k: False)

    assert ibkr_live_trade.ensure_gateway(allow_launch=True) is True
    assert spy.call_count == 1, "an authorized launch must still happen, exactly once"


def test_connect_launch_true_raises_instead_of_launching(spy, monkeypatch):
    """`launch=True` used to auto-start. It must now fail honestly instead."""
    monkeypatch.setattr(ibkr_live_trade, "gateway_running", lambda *a, **k: False)

    with pytest.raises(RuntimeError, match="not up"):
        ibkr_live_trade.connect("s8_live_pilot", launch=True)
    assert spy.call_count == 0
