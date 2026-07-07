"""
test_arming.py — unit tests for the elevated-restart path in arming.py.

PAPER ONLY; nothing here touches a real Gateway process. `restart_gateway()` is
tested purely through mocks of `subprocess.run` (the schtasks trigger) and the
state-file poll — the real `GatewayArmRestart` scheduled task is NEVER invoked by
these tests.
"""
from __future__ import annotations

import json
import time
from unittest import mock

import pytest

import arming


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "gateway_arm_restart_state.json"
    monkeypatch.setattr(arming, "ARM_RESTART_STATE_FILE", str(path))
    monkeypatch.setattr(arming, "ARM_RESTART_POLL_TIMEOUT", 3)
    return path


def _write_state(path, ts, ok, detail="test"):
    path.write_text(json.dumps({"ts": ts, "ok": ok, "detail": detail}), encoding="utf-8")


def test_restart_gateway_success(state_file):
    """schtasks trigger succeeds; a fresh ok=True state file appears -> True."""
    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["schtasks", "/run"]:
            # Simulate the elevated task writing a fresh success state shortly after.
            _write_state(state_file, time.time() + 0.1, True, "restarted")
            return mock.Mock(returncode=0, stdout="SUCCESS", stderr="")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    with mock.patch("arming.subprocess.run", side_effect=fake_run):
        assert arming.restart_gateway() is True


def test_restart_gateway_task_reports_failure(state_file):
    """Task runs but writes ok=False -> restart_gateway() returns False."""
    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["schtasks", "/run"]:
            _write_state(state_file, time.time() + 0.1, False, "gateway did not come back up")
            return mock.Mock(returncode=0, stdout="SUCCESS", stderr="")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    with mock.patch("arming.subprocess.run", side_effect=fake_run):
        assert arming.restart_gateway() is False


def test_restart_gateway_trigger_fails(state_file):
    """schtasks /run itself fails to launch -> False, no polling needed."""
    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["schtasks", "/run"]:
            return mock.Mock(returncode=1, stdout="", stderr="ERROR: task not found")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    with mock.patch("arming.subprocess.run", side_effect=fake_run):
        assert arming.restart_gateway() is False


def test_restart_gateway_no_fresh_result_times_out(state_file):
    """Trigger succeeds but the state file never updates (or stays stale) -> False."""
    # Pre-seed a STALE state file (timestamped before the trigger) to prove staleness
    # is correctly ignored, not just absence.
    _write_state(state_file, time.time() - 999, True, "old result")

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["schtasks", "/run"]:
            return mock.Mock(returncode=0, stdout="SUCCESS", stderr="")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    with mock.patch("arming.subprocess.run", side_effect=fake_run), \
         mock.patch("arming.time.sleep"):  # don't actually block through the timeout
        assert arming.restart_gateway() is False


def test_restart_gateway_clears_stale_file_first(state_file):
    """A stale file from a previous run must not be misread as fresh before trigger."""
    _write_state(state_file, time.time() - 999, False, "stale failure")
    assert state_file.exists()

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["schtasks", "/run"]:
            assert not state_file.exists(), "stale state file should be removed before trigger"
            _write_state(state_file, time.time() + 0.1, True, "fresh success")
            return mock.Mock(returncode=0, stdout="SUCCESS", stderr="")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    with mock.patch("arming.subprocess.run", side_effect=fake_run):
        assert arming.restart_gateway() is True
