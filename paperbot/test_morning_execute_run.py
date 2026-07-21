"""
test_morning_execute_run.py — offline unit tests for the MORNING execution runner.

NO broker, NO real gateway, NO network, NO real sleeps. Proves the guardrails that
matter most for an unattended script that could (once PILOT_MODE is flipped) transmit
PAPER orders:
  * PILOT_MODE defaults True.
  * no staged file for today -> ZERO gateway touch (ibkr_paper.connect never called), rc==0.
  * a staged file + PILOT_MODE=True -> re-validates the guard, connects, but NEVER calls
    any order-building/placing code; emails "WOULD HAVE TRANSMITTED" and archives the
    staged file.
  * a staged file whose guard RE-VALIDATION fails -> nothing transmitted, staged file
    LEFT IN PLACE (not archived), alert fires.
  * AUTOTRADE_DISABLED sentinel present -> transmission skipped, staged file LEFT IN
    PLACE, alert fires, no gateway connect attempted for the trade.
  * a busy gateway lock -> REFUSE (rc==2), staged file left in place, no work done.
  * bounded_connect gives up after CONNECT_MAX_ATTEMPTS without a real sleep.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_morning_execute_run.py -q
"""
from __future__ import annotations

import json
import os
from datetime import date

import pytest

import morning_execute_run as mer


# The staged-file filename is derived from date.today() in production (mer.main via
# _stage_path). Pin that clock to the fixtures' staging date so these tests pass every
# day instead of only on their birth day (2026-07-09). FakeDate subclasses date so
# isoformat()/strftime()/isinstance() all keep working; only today() is overridden.
FIXED_TODAY = date(2026, 7, 9)


class FakeDate(date):
    @classmethod
    def today(cls):
        return FIXED_TODAY


class _FakeIB:
    def __init__(self):
        self.disconnected = False

    def disconnect(self):
        self.disconnected = True


def _write_staged(tmp_path, today: date, routes=None) -> str:
    payload = {
        "date": today.isoformat(),
        "staged_at": f"{today.isoformat()}T21:17:03-05:00",
        "paperbot_version": "0.14.0",
        "regime": {"raw": "GOLDILOCKS", "confirmed": "GOLDILOCKS", "as_of": today.isoformat()},
        "guard": {"passed": True, "reasons": []},
        "as_of": {"Balanced": today.isoformat()},
        "accounts_needing_rebalance": ["DU8922142"],
        "routes": routes or [
            {"route": "direct", "version": "Balanced", "symbol": "SPY", "side": "BUY",
             "total_qty": 10, "fa_group": None, "fa_method": "", "account": "DU8922142",
             "per_account_split": {"DU8922142": 10}, "reason": "REBALANCE_TO_MODEL"},
        ],
        "prices_by_symbol": {"SPY": 500.0},
    }
    path = os.path.join(str(tmp_path), f"{today.isoformat()}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


def _patch_common(monkeypatch, tmp_path):
    # Pin production's date.today() to the fixtures' staging date (see FakeDate above) so
    # main()/_stage_path() look for the file the fixtures actually wrote — date-independent.
    monkeypatch.setattr(mer, "date", FakeDate)
    monkeypatch.setattr(mer, "PENDING_TRADES_DIR", str(tmp_path))
    monkeypatch.setattr(mer, "ARCHIVE_DIR", os.path.join(str(tmp_path), "archive"))
    monkeypatch.setattr(mer, "AUTOTRADE_DISABLED_SENTINEL",
                        os.path.join(str(tmp_path), "AUTOTRADE_DISABLED"))
    monkeypatch.setattr(mer, "_write_status", lambda *a, **k: None)
    monkeypatch.setattr(mer, "_alert_email", lambda *a, **k: None)


def _install_free_lock(monkeypatch, tmp_path):
    import gateway_lock as gl
    path = os.path.join(str(tmp_path), "gateway.lock")
    real = gl.gateway_lock

    def patched(purpose, client_id, on_busy="refuse", wait_secs=None, **kw):
        kw.setdefault("lock_path", path)
        kw.setdefault("poll_interval", 0.001)
        kw.setdefault("sleep_fn", lambda s: None)
        if wait_secs is None:
            wait_secs = 0.02
        return real(purpose, client_id, on_busy=on_busy, wait_secs=wait_secs, **kw)

    monkeypatch.setattr(mer, "gateway_lock", patched)


def _install_busy_lock(monkeypatch, tmp_path):
    import gateway_lock as gl
    path = os.path.join(str(tmp_path), "gateway.lock")
    rec = {"pid": os.getpid(), "client_id": 37, "purpose": "rebalance_run",
           "host": "TEST", "acquired_ts": 1000.0, "acquired_at": "2026-07-09T08:45:00",
           "heartbeat_ts": 9_999_999_999.0, "heartbeat_at": "2026-07-09T08:45:00"}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec))
    real = gl.gateway_lock

    def patched(purpose, client_id, on_busy="refuse", wait_secs=None, **kw):
        kw.setdefault("lock_path", path)
        kw.setdefault("poll_interval", 0.001)
        kw.setdefault("pid_alive", lambda pid: True)
        kw.setdefault("sleep_fn", lambda s: None)
        if wait_secs is None:
            wait_secs = 0.02
        return real(purpose, client_id, on_busy=on_busy, wait_secs=wait_secs, **kw)

    monkeypatch.setattr(mer, "gateway_lock", patched)


# --- PILOT_MODE defaults True -----------------------------------------------------
def test_pilot_mode_defaults_true():
    assert mer.PILOT_MODE is True


# --- no staged file: ZERO gateway touch --------------------------------------------
def test_no_staged_file_makes_zero_gateway_contact(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise AssertionError("must not connect when nothing is staged")

    monkeypatch.setattr(mer.ibkr_paper, "connect", _boom)
    monkeypatch.setattr(mer, "bounded_connect", _boom)

    rc = mer.main()

    assert rc == 0


# --- staged + PILOT_MODE True: no transmit, archives, emails ---------------------
def test_staged_pilot_mode_never_transmits_and_archives(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    _install_free_lock(monkeypatch, tmp_path)
    today = date(2026, 7, 9)
    stage_path = _write_staged(tmp_path, today)

    fake = _FakeIB()
    monkeypatch.setattr(mer, "bounded_connect", lambda *a, **k: fake)
    monkeypatch.setattr(mer.live_quotes, "fetch", lambda ib, universe: {})
    monkeypatch.setattr(mer.accounts, "discover", lambda ib: [])

    import rebalance_guard as rg
    monkeypatch.setattr(mer.rebalance_guard, "check",
                        lambda routes, ai, prices, claimed_regime=None:
                        rg.GuardResult(passed=True, reasons=[]))

    # Specific hard guarantee: the order-transmitting entry points are never called.
    monkeypatch.setattr(mer.order_router, "place", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("order_router.place must never be called in PILOT_MODE")))
    monkeypatch.setattr(mer.rebalance_execute, "backup_fa_groups", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("backup_fa_groups must never be called in PILOT_MODE")))

    alerts = []
    monkeypatch.setattr(mer, "_alert_email", lambda subj, lines: alerts.append((subj, lines)))

    rc = mer.main()

    assert rc == 0
    assert fake.disconnected is True
    assert not os.path.exists(stage_path)                       # archived away
    archived = os.path.join(str(tmp_path), "archive", "2026-07-09.json")
    assert os.path.exists(archived)
    assert any("PILOT" in subj for subj, _ in alerts)
    # The pilot email reports what WOULD have transmitted while sending nothing. Match
    # production's actual wording (module docstring paraphrases it as "WOULD HAVE
    # TRANSMITTED"; the emitted body says "...WOULD have been sent if PILOT_MODE were False").
    assert any("nothing was transmitted" in "\n".join(lines)
               and "WOULD have been sent" in "\n".join(lines)
               for _, lines in alerts)


# --- staged + guard re-validation FAILS: left in place, alert fires --------------
def test_revalidation_failure_leaves_staged_file_in_place(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    _install_free_lock(monkeypatch, tmp_path)
    today = date(2026, 7, 9)
    stage_path = _write_staged(tmp_path, today)

    fake = _FakeIB()
    monkeypatch.setattr(mer, "bounded_connect", lambda *a, **k: fake)
    monkeypatch.setattr(mer.live_quotes, "fetch", lambda ib, universe: {})
    monkeypatch.setattr(mer.accounts, "discover", lambda ib: [])

    import rebalance_guard as rg
    monkeypatch.setattr(mer.rebalance_guard, "check",
                        lambda routes, ai, prices, claimed_regime=None:
                        rg.GuardResult(passed=False, reasons=["regime drifted"]))

    alerts = []
    monkeypatch.setattr(mer, "_alert_email", lambda subj, lines: alerts.append(subj))

    rc = mer.main()

    assert rc == 1
    assert os.path.exists(stage_path)   # NOT archived
    assert any("re-validation FAILED" in s for s in alerts)


# --- AUTOTRADE_DISABLED sentinel: skip, leave file, alert ------------------------
def test_autotrade_disabled_sentinel_skips_and_leaves_file(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    today = date(2026, 7, 9)
    stage_path = _write_staged(tmp_path, today)
    with open(mer.AUTOTRADE_DISABLED_SENTINEL, "w", encoding="utf-8") as fh:
        fh.write("")

    def _boom(*a, **k):
        raise AssertionError("must not connect when the kill switch is tripped")

    monkeypatch.setattr(mer, "bounded_connect", _boom)

    alerts = []
    monkeypatch.setattr(mer, "_alert_email", lambda subj, lines: alerts.append(subj))

    rc = mer.main()

    assert rc == 0
    assert os.path.exists(stage_path)   # left in place for manual execution
    assert any("AUTOTRADE_DISABLED" in s for s in alerts)


# --- busy gateway lock: REFUSE, file left in place --------------------------------
def test_busy_gateway_lock_refuses_and_leaves_file(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    _install_busy_lock(monkeypatch, tmp_path)
    today = date(2026, 7, 9)
    stage_path = _write_staged(tmp_path, today)

    fake = _FakeIB()
    monkeypatch.setattr(mer, "bounded_connect", lambda *a, **k: fake)

    def _boom(*a, **k):
        raise AssertionError("must not do account work while gateway is busy")

    monkeypatch.setattr(mer.accounts, "discover", _boom)

    alerts = []
    monkeypatch.setattr(mer, "_alert_email", lambda subj, lines: alerts.append(subj))

    rc = mer.main()

    assert rc == 2
    assert os.path.exists(stage_path)
    assert any("gateway busy, refused" in s for s in alerts)
    assert fake.disconnected is True


# --- bounded_connect: never sleeps for real, gives up after N attempts -----------
def test_bounded_connect_gives_up_after_max_attempts(monkeypatch):
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise ConnectionError("no gateway")

    monkeypatch.setattr(mer.ibkr_paper, "connect", _boom)
    monkeypatch.setattr(mer.time, "sleep", lambda s: None)

    result = mer.bounded_connect("paperbot_morning_execute")

    assert result is None
    assert calls["n"] == mer.CONNECT_MAX_ATTEMPTS


def test_connect_failure_leaves_staged_file_in_place(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    today = date(2026, 7, 9)
    stage_path = _write_staged(tmp_path, today)
    monkeypatch.setattr(mer, "bounded_connect", lambda *a, **k: None)

    alerts = []
    monkeypatch.setattr(mer, "_alert_email", lambda subj, lines: alerts.append(subj))

    rc = mer.main()

    assert rc == 1
    assert os.path.exists(stage_path)
    assert any("gateway connect FAILED" in s for s in alerts)
