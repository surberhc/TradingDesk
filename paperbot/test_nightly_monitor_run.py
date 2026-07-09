"""
test_nightly_monitor_run.py — offline unit tests for the NIGHTLY bounded-retry
monitor + stage runner.

NO broker, NO real gateway, NO network, NO real price-history reads, NO real sleeps:
ibkr.connect, gateway_lock, rebalance_guard.compute_regime_now, and time.sleep are all
monkeypatched. Proves:
  * bounded_connect gives up after CONNECT_MAX_ATTEMPTS and never sleeps for real
    past that (a fake sleep is injected via monkeypatch on the module's `time`).
  * an in-band fleet stages nothing.
  * a drifted fleet + a passing guard stages a schema-correct JSON file.
  * a drifted fleet + a FAILING guard stages nothing and fires an alert email.
  * a busy gateway lock -> clean skip (rc==0), no broker touched past connect.
  * stage_trade_list's on-disk JSON round-trips the documented schema fields.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_nightly_monitor_run.py -q
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

import nightly_monitor_run as nmr
import rebalance_guard as rg


# --- shared fakes ----------------------------------------------------------------
class _FakeIB:
    def __init__(self, managed=None):
        self._managed = managed or []
        self.disconnected = False

    def managedAccounts(self):
        return self._managed

    def accountValues(self, account):
        return []

    def positions(self, account):
        return []

    def reqExecutions(self, flt):
        return []

    def disconnect(self):
        self.disconnected = True


def _fake_target():
    import pandas as pd
    return SimpleNamespace(
        as_of=pd.Timestamp("2026-07-09"), weights=pd.Series({"SPY": 1.0}),
        prices=pd.Series({"SPY": 500.0}))


def _patch_common(monkeypatch, tmp_path):
    """Stub tier models, baselines/earmarks, run_cycle, and the pending-trades dir."""
    monkeypatch.setattr(nmr.amr, "_targets_by_version", lambda: {"Balanced": _fake_target()})
    monkeypatch.setattr(nmr.amr, "load_baselines", lambda: {})
    monkeypatch.setattr(nmr.amr, "load_earmarks", lambda: {})
    monkeypatch.setattr(nmr.amr, "read_account_cycle",
                        lambda ib, acct, today: {"account": acct})
    monkeypatch.setattr(nmr.amr, "run_cycle", lambda *a, **k: [])
    monkeypatch.setattr(nmr, "PENDING_TRADES_DIR", str(tmp_path))
    monkeypatch.setattr(nmr, "_write_status", lambda *a, **k: None)
    monkeypatch.setattr(nmr, "_alert_email", lambda *a, **k: None)


def _install_free_lock(monkeypatch, tmp_path):
    """Route nightly_monitor_run.gateway_lock at a free TEMP-path lock so no real
    STATE_DIR file is touched and no real sleep happens."""
    import gateway_lock as gl
    path = os.path.join(str(tmp_path), "gateway.lock")
    real = gl.gateway_lock

    def patched(purpose, client_id, on_busy="skip", wait_secs=None, **kw):
        kw.setdefault("lock_path", path)
        kw.setdefault("poll_interval", 0.001)
        kw.setdefault("sleep_fn", lambda s: None)
        if wait_secs is None:
            wait_secs = 0.02
        return real(purpose, client_id, on_busy=on_busy, wait_secs=wait_secs, **kw)

    monkeypatch.setattr(nmr, "gateway_lock", patched)


def _install_busy_lock(monkeypatch, tmp_path):
    import gateway_lock as gl
    path = os.path.join(str(tmp_path), "gateway.lock")
    rec = {"pid": os.getpid(), "client_id": 38, "purpose": "rebalance_execute",
           "host": "TEST", "acquired_ts": 1000.0, "acquired_at": "2026-07-09T21:00:00",
           "heartbeat_ts": 9_999_999_999.0, "heartbeat_at": "2026-07-09T21:00:00"}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec))
    real = gl.gateway_lock

    def patched(purpose, client_id, on_busy="skip", wait_secs=None, **kw):
        kw.setdefault("lock_path", path)
        kw.setdefault("poll_interval", 0.001)
        kw.setdefault("pid_alive", lambda pid: True)
        kw.setdefault("sleep_fn", lambda s: None)
        if wait_secs is None:
            wait_secs = 0.02
        return real(purpose, client_id, on_busy=on_busy, wait_secs=wait_secs, **kw)

    monkeypatch.setattr(nmr, "gateway_lock", patched)
    return path


# --- bounded_connect: never sleeps for real, gives up after N attempts -----------
def test_bounded_connect_gives_up_after_max_attempts(monkeypatch):
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise ConnectionError("no gateway")

    monkeypatch.setattr(nmr.ibkr, "connect", _boom)
    monkeypatch.setattr(nmr.time, "sleep", lambda s: None)  # no real backoff wait

    result = nmr.bounded_connect("paperbot_nightly_monitor")

    assert result is None
    assert calls["n"] == nmr.CONNECT_MAX_ATTEMPTS


def test_bounded_connect_returns_ib_on_first_success(monkeypatch):
    fake = _FakeIB()
    monkeypatch.setattr(nmr.ibkr, "connect", lambda *a, **k: fake)
    result = nmr.bounded_connect("paperbot_nightly_monitor")
    assert result is fake


def test_main_alerts_and_returns_1_when_connect_never_succeeds(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(nmr, "bounded_connect", lambda *a, **k: None)
    alerts = []
    monkeypatch.setattr(nmr, "_alert_email", lambda subj, lines: alerts.append(subj))

    rc = nmr.main()

    assert rc == 1
    assert any("gateway connect FAILED" in s for s in alerts)


# --- in-band fleet: nothing staged ------------------------------------------------
def test_in_band_fleet_stages_nothing(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    _install_free_lock(monkeypatch, tmp_path)
    fake = _FakeIB(managed=["DU8922142"])
    monkeypatch.setattr(nmr, "bounded_connect", lambda *a, **k: fake)

    monkeypatch.setattr(nmr.accounts, "discover", lambda ib: [
        SimpleNamespace(number="DU8922142", version="Balanced", net_liq=1_000_000.0,
                        enrolled=True, funded=True, is_master=False)])
    monkeypatch.setattr(nmr.live_quotes, "fetch", lambda ib, universe: {})

    # build_plan says nothing needs rebalancing.
    plan = SimpleNamespace(account="DU8922142", needs_rebalance=False)
    monkeypatch.setattr(nmr, "build_plan", lambda *a, **k: {"plans": [plan], "routes": []})

    rc = nmr.main()

    assert rc == 0
    assert not os.listdir(tmp_path) or all(
        not f.endswith(".json") for f in os.listdir(tmp_path))
    assert fake.disconnected is True


# --- drifted fleet + passing guard: stages a schema-correct file -----------------
def test_drifted_fleet_with_passing_guard_stages_file(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    _install_free_lock(monkeypatch, tmp_path)
    fake = _FakeIB(managed=["DU8922142"])
    monkeypatch.setattr(nmr, "bounded_connect", lambda *a, **k: fake)

    monkeypatch.setattr(nmr.accounts, "discover", lambda ib: [
        SimpleNamespace(number="DU8922142", version="Balanced", net_liq=1_000_000.0,
                        enrolled=True, funded=True, is_master=False)])
    monkeypatch.setattr(nmr.live_quotes, "fetch", lambda ib, universe: {})

    plan = SimpleNamespace(account="DU8922142", needs_rebalance=True)
    route = SimpleNamespace(route="direct", version="Balanced", symbol="SPY", side="BUY",
                            total_qty=10, fa_group=None, fa_method="", account="DU8922142",
                            per_account_split={"DU8922142": 10}, reason="REBALANCE_TO_MODEL")

    call_n = {"n": 0}

    def _fake_build_plan(*a, **k):
        call_n["n"] += 1
        if call_n["n"] == 1:
            return {"plans": [plan], "routes": []}
        return {"plans": [plan], "routes": [route]}

    monkeypatch.setattr(nmr, "build_plan", _fake_build_plan)

    import rebalance_run
    monkeypatch.setattr(rebalance_run, "resolve_tier_groups",
                        lambda ib, versions: {"Balanced": "tier_balanced"})
    monkeypatch.setattr(nmr.rebalance_guard, "compute_regime_now",
                        lambda: ("GOLDILOCKS", "GOLDILOCKS", "2026-07-09"))

    rc = nmr.main()

    assert rc == 0
    files = [f for f in os.listdir(tmp_path) if f.endswith(".json")]
    assert len(files) == 1
    with open(os.path.join(tmp_path, files[0]), encoding="utf-8") as fh:
        payload = json.load(fh)
    for key in ("date", "staged_at", "paperbot_version", "regime", "guard", "as_of",
               "accounts_needing_rebalance", "routes", "prices_by_symbol"):
        assert key in payload
    assert payload["guard"]["passed"] is True
    assert payload["accounts_needing_rebalance"] == ["DU8922142"]
    assert payload["routes"][0]["symbol"] == "SPY"
    assert payload["routes"][0]["per_account_split"] == {"DU8922142": 10}


# --- drifted fleet + FAILING guard: nothing staged, alert fires ------------------
def test_drifted_fleet_with_failing_guard_stages_nothing(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    _install_free_lock(monkeypatch, tmp_path)
    fake = _FakeIB(managed=["DU8922142"])
    monkeypatch.setattr(nmr, "bounded_connect", lambda *a, **k: fake)

    monkeypatch.setattr(nmr.accounts, "discover", lambda ib: [
        SimpleNamespace(number="DU8922142", version="Balanced", net_liq=1_000_000.0,
                        enrolled=True, funded=True, is_master=False)])
    monkeypatch.setattr(nmr.live_quotes, "fetch", lambda ib, universe: {})

    plan = SimpleNamespace(account="DU8922142", needs_rebalance=True)
    route = SimpleNamespace(route="direct", version="Balanced", symbol="ZZZBOGUS", side="BUY",
                            total_qty=10, fa_group=None, fa_method="", account="DU8922142",
                            per_account_split={"DU8922142": 10}, reason="REBALANCE_TO_MODEL")

    call_n = {"n": 0}

    def _fake_build_plan(*a, **k):
        call_n["n"] += 1
        if call_n["n"] == 1:
            return {"plans": [plan], "routes": []}
        return {"plans": [plan], "routes": [route]}

    monkeypatch.setattr(nmr, "build_plan", _fake_build_plan)

    import rebalance_run
    monkeypatch.setattr(rebalance_run, "resolve_tier_groups",
                        lambda ib, versions: {"Balanced": "tier_balanced"})
    # Unrecognized symbol -> ticker allow-list check fails closed (real rebalance_guard.check).
    monkeypatch.setattr(nmr.rebalance_guard, "compute_regime_now",
                        lambda: ("GOLDILOCKS", "GOLDILOCKS", "2026-07-09"))

    alerts = []
    monkeypatch.setattr(nmr, "_alert_email", lambda subj, lines: alerts.append(subj))

    rc = nmr.main()

    assert rc == 1
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".json")]
    assert any("GUARD FAILED" in s for s in alerts)


# --- busy gateway lock: clean skip -------------------------------------------------
def test_busy_gateway_lock_skips_cleanly(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    _install_busy_lock(monkeypatch, tmp_path)
    fake = _FakeIB(managed=["DU8922142"])
    monkeypatch.setattr(nmr, "bounded_connect", lambda *a, **k: fake)

    def _boom(*a, **k):
        raise AssertionError("must not do account work while gateway is busy")

    monkeypatch.setattr(nmr.accounts, "discover", _boom)

    rc = nmr.main()

    assert rc == 0
    assert fake.disconnected is True   # connect still happened + was cleanly closed


# --- staging JSON round-trip -------------------------------------------------------
def test_stage_trade_list_writes_atomically_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(nmr, "PENDING_TRADES_DIR", str(tmp_path))
    import datetime as dt
    route = SimpleNamespace(route="fa_block", version="Balanced", symbol="SPY", side="BUY",
                            total_qty=12, fa_group="tier_balanced", fa_method="",
                            account=None, per_account_split={"DU8922143": 5, "DU8922144": 7},
                            reason="REBALANCE_TO_MODEL")
    guard = rg.GuardResult(passed=True, reasons=[])
    target = _fake_target()

    path = nmr.stage_trade_list(
        dt.date(2026, 7, 9), [route],
        {"raw": "GOLDILOCKS", "confirmed": "GOLDILOCKS", "as_of": "2026-07-09"},
        guard, {"Balanced": target}, ["DU8922143", "DU8922144"], {"SPY": 512.34})

    assert path == os.path.join(str(tmp_path), "2026-07-09.json")
    assert os.path.exists(path)
    assert not os.path.exists(path + ".tmp")   # temp file was renamed away
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["date"] == "2026-07-09"
    assert payload["routes"][0]["per_account_split"] == {"DU8922143": 5, "DU8922144": 7}
    assert payload["prices_by_symbol"] == {"SPY": 512.34}
