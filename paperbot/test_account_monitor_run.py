"""
test_account_monitor_run.py — Slice 2: the monitor's live shell ACQUIRES the gateway lock
and SKIPS cleanly when the Gateway is busy.

Proves OFFLINE (NO broker, NO real gateway, NO network):
  * BUSY  -> when the lock is already held by a live, non-stale holder, main() raises NOTHING
            to the caller: it catches GatewayBusySkip, logs a clear "gateway busy — held by
            <holder> ... skipping this cycle" line, makes ZERO broker/connect calls, and
            returns 0 (a non-event; the next cycle catches up).
  * FREE  -> when the lock is free, main() proceeds exactly as before: it connects (mocked),
            runs the read-only cycle over the mocked accounts, and returns 0.

The broker is fully mocked/injected — ibkr_paper.connect is monkeypatched to a fake IB; a held
lock is simulated by writing a live lock record to a TEMP lock path (never the real
STATE_DIR / Drive). Time/sleep are injected so the brief monitor wait never sleeps for real.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_account_monitor_run.py -q
"""
from __future__ import annotations

import json
import os

import pytest

import account_monitor_run as amr
import gateway_lock as gl


# --- a mock broker (read-only endpoints only) ----------------------------------
class _FakeIB:
    """Minimal read-only IB stand-in. Records that it was used so a BUSY skip can assert it
    was NEVER touched. Returns empty/benign data so a FREE cycle completes without a broker."""

    def __init__(self):
        self.used = False
        self.disconnected = False

    def managedAccounts(self):
        self.used = True
        return []          # no visible accounts -> the cycle ends cleanly after [3]

    def accountValues(self, account):
        self.used = True
        return []

    def positions(self, account):
        self.used = True
        return []

    def reqExecutions(self, flt):
        self.used = True
        return []

    def disconnect(self):
        self.disconnected = True


def _patch_targets(monkeypatch):
    """Avoid loading real strategy data: stub the tier-model compute with a tiny fake."""
    from types import SimpleNamespace
    import pandas as pd

    fake = SimpleNamespace(
        as_of=pd.Timestamp("2026-06-30"), weights=pd.Series({"SPY": 1.0}),
        prices=pd.Series({"SPY": 100.0}))
    monkeypatch.setattr(amr, "_targets_by_version", lambda: {"Balanced": fake})


def _patch_state_files(monkeypatch, tmp_path):
    """Point the baseline/earmark loads at empty fakes so no STATE_DIR file is read/written."""
    monkeypatch.setattr(amr, "load_baselines", lambda: {})
    monkeypatch.setattr(amr, "load_earmarks", lambda: {})


def _install_lock_on_tmp_path(monkeypatch, tmp_path, **lock_kwargs):
    """Make account_monitor_run.gateway_lock build a lock on a TEMP path with fast/offline
    timing, so the test never touches the real STATE_DIR lock and never sleeps for real."""
    path = os.path.join(str(tmp_path), "gateway.lock")
    real = gl.gateway_lock

    def patched(purpose, client_id, on_busy="skip", wait_secs=None, **kw):
        kw.setdefault("lock_path", path)
        kw.setdefault("poll_interval", 0.001)
        kw.setdefault("heartbeat_interval", 1000.0)
        kw.setdefault("sleep_fn", lambda s: None)
        kw.update(lock_kwargs)
        if wait_secs is None:
            wait_secs = 0.05
        return real(purpose, client_id, on_busy=on_busy, wait_secs=wait_secs, **kw)

    monkeypatch.setattr(amr, "gateway_lock", patched)
    return path


def _write_live_holder(path, **fields):
    """Write a lock record for a LIVE (our-own-pid), fresh-heartbeat holder so it is NOT
    treated as stale and the acquirer must wait-then-skip."""
    rec = {
        "pid": os.getpid(), "client_id": 38, "purpose": "rebalance_execute",
        "host": "TEST", "acquired_ts": 1000.0, "acquired_at": "2026-06-30T14:07:32",
        "heartbeat_ts": 9_999_999_999.0, "heartbeat_at": "2026-06-30T14:07:32",
    }
    rec.update(fields)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec))


# --- BUSY: held lock -> clean skip, NO broker touched --------------------------
def test_busy_lock_skips_cycle_without_touching_broker(monkeypatch, tmp_path, capsys):
    _patch_targets(monkeypatch)
    _patch_state_files(monkeypatch, tmp_path)
    # The holder is alive (our pid) with a fresh heartbeat -> not stale -> acquirer must skip.
    path = _install_lock_on_tmp_path(monkeypatch, tmp_path, pid_alive=lambda pid: True,
                                     now_fn=_advancing_clock())
    _write_live_holder(path)

    # If main() ever tries to connect while the lock is held, this blows the test up:
    # the BUSY skip must abort BEFORE any broker/connect call (no account reads attempted).
    def _boom_connect(*a, **k):
        raise AssertionError("monitor must NOT connect while the gateway lock is held!")

    monkeypatch.setattr(amr.ibkr_paper, "connect", _boom_connect)

    rc, verdict_summary = main_no_exception(monkeypatch)

    assert rc == 0                                   # a clean SKIP, not an error
    assert verdict_summary == {}                      # no cycle ran -> no verdicts
    out = capsys.readouterr().out
    assert "gateway busy" in out                     # logged the busy line
    assert "rebalance_execute" in out                # named the holder purpose
    assert "skipping this monitor cycle" in out
    # holder's lock left intact — the monitor never stomped a live holder.
    assert os.path.exists(path)


# --- FREE: free lock -> the cycle proceeds (mocked broker) ---------------------
def test_free_lock_runs_cycle(monkeypatch, tmp_path, capsys):
    _patch_targets(monkeypatch)
    _patch_state_files(monkeypatch, tmp_path)
    path = _install_lock_on_tmp_path(monkeypatch, tmp_path)
    assert not os.path.exists(path)                  # lock is free

    fake = _FakeIB()
    monkeypatch.setattr(amr.ibkr_paper, "connect", lambda *a, **k: fake)

    rc, verdict_summary = amr.main()

    assert rc == 0
    assert verdict_summary == {}                      # no managed accounts -> no verdicts
    assert fake.used is True                          # it actually connected + read
    assert fake.disconnected is True                  # session closed (disconnect ran)
    # lock released after the session (held through connect->work->disconnect, then freed).
    assert not os.path.exists(path)
    out = capsys.readouterr().out
    assert "gateway busy" not in out                  # no skip on a free lock


def test_free_lock_held_through_whole_session(monkeypatch, tmp_path):
    """Prove the lock spans the ENTIRE session: it must exist DURING the connect+read work
    and be gone only AFTER disconnect — not just around the connect call."""
    _patch_targets(monkeypatch)
    _patch_state_files(monkeypatch, tmp_path)
    path = _install_lock_on_tmp_path(monkeypatch, tmp_path)

    seen = {"during_connect": None, "during_read": None}

    class _ProbingIB(_FakeIB):
        def managedAccounts(self):
            # This runs INSIDE the gateway session (after connect, before disconnect).
            seen["during_read"] = os.path.exists(path)
            return super().managedAccounts()

    fake = _ProbingIB()

    def _connect(*a, **k):
        seen["during_connect"] = os.path.exists(path)   # lock must already be held at connect
        return fake

    monkeypatch.setattr(amr.ibkr_paper, "connect", _connect)

    rc, verdict_summary = amr.main()
    assert rc == 0
    assert verdict_summary == {}                      # no managed accounts -> no verdicts
    assert seen["during_connect"] is True            # held BEFORE/AT connect
    assert seen["during_read"] is True               # still held DURING the read work
    assert not os.path.exists(path)                  # released AFTER disconnect


# --- status severity reflects real per-account drift (not just cycle health) ---
# These prove _run_with_status() reads main()'s verdict_summary, not just rc. Slice: the
# 2026-07-01 -> 2026-07-07 incident where a week of live ALERT verdicts on every account
# still wrote status "ok" because the status write only ever looked at rc==0/non-zero.
def test_alert_verdict_writes_fail_status_with_detail(monkeypatch):
    summary = {"accounts": {"DU8922142": {"action": "ALERT", "reason": "UNTRACKED_POSITION"}},
               "n_hold": 0, "n_rebalance": 0, "n_alert": 1}
    monkeypatch.setattr(amr, "main", lambda: (0, summary))
    written = {}

    def fake_write(st, metrics=None, message=""):
        written["status"] = st
        written["metrics"] = metrics
        written["message"] = message

    monkeypatch.setattr(amr, "_write_monitor_status", fake_write)

    rc = amr._run_with_status()

    assert rc == 0
    assert written["status"] == "fail"                # ALERT overrides a clean rc
    assert written["metrics"]["n_alert"] == 1
    assert "ALERT" in written["message"]
    assert "DU8922142" in written["message"]


def test_all_hold_verdict_keeps_ok_status(monkeypatch):
    summary = {"accounts": {"DU8922142": {"action": "HOLD", "reason": "IN_BAND"}},
               "n_hold": 1, "n_rebalance": 0, "n_alert": 0}
    monkeypatch.setattr(amr, "main", lambda: (0, summary))
    written = {}
    monkeypatch.setattr(amr, "_write_monitor_status",
                        lambda st, metrics=None, message="": written.update(
                            status=st, metrics=metrics, message=message))

    rc = amr._run_with_status()

    assert rc == 0
    assert written["status"] == "ok"
    assert written["metrics"]["n_hold"] == 1


def test_rebalance_only_verdict_keeps_ok_but_carries_summary(monkeypatch):
    summary = {"accounts": {"DU8922142": {"action": "REBALANCE", "reason": "DRIFT_BAND_BREACH"}},
               "n_hold": 0, "n_rebalance": 1, "n_alert": 0}
    monkeypatch.setattr(amr, "main", lambda: (0, summary))
    written = {}
    monkeypatch.setattr(amr, "_write_monitor_status",
                        lambda st, metrics=None, message="": written.update(
                            status=st, metrics=metrics, message=message))

    rc = amr._run_with_status()

    assert rc == 0
    assert written["status"] == "ok"                  # REBALANCE alone doesn't fail the day
    assert written["metrics"]["n_rebalance"] == 1
    assert "1" in written["message"]


# --- helpers -------------------------------------------------------------------
def _advancing_clock():
    """A now_fn that advances each call so the bounded wait loop reaches its deadline fast."""
    clock = {"t": 0.0}

    def now():
        clock["t"] += 0.05
        return clock["t"]

    return now


def main_no_exception(monkeypatch):
    """Run amr.main() and assert no exception escapes (the BUSY path must be swallowed)."""
    try:
        return amr.main()
    except Exception as exc:                          # pragma: no cover - failure path
        pytest.fail(f"main() let an exception escape on a busy lock: {exc!r}")
