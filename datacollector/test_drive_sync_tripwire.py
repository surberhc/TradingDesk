r"""test_drive_sync_tripwire.py — tests for the Google-Drive-management tripwire.

WHAT THIS GUARDS: C:\TradingDesk-Local (99 GB of irreplaceable market data + secrets)
is deliberately OUTSIDE Google Drive. The tripwire pages the moment a human re-adds it
to the Drive Desktop sync/backup client — the exact wrong-folder corruption risk that
silently ran for 9 days in 2026-07-16 (see repo_backup.py). These tests pin, as
regressions:
  * the CURRENT HEALTHY STATE reads GREEN (no page) — if it paged today it'd be noise;
  * a simulated DriveFS mount at/above the tree PAGES (THREAT 1);
  * a simulated Drive-backup registry root overlapping the tree PAGES (THREAT 2);
  * an on-disk Drive-sync artifact PAGES (the positive-only proxy);
  * an UNREADABLE registry FAILS CLOSED (pages "could not evaluate") — a guard that
    silently can't look is itself the silent failure we forbid;
  * the page names the folder AND the remediation.

OFFLINE + FAST: no DriveFS, no email, no network. Every check's collaborators are
injected. Mirrors test_repo_backup.py's injected-seam style.

Run from datacollector/ so `import config` (pulled in via heartbeat_alarm) resolves:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest test_drive_sync_tripwire.py -q
"""

from __future__ import annotations

import datetime as dt
import os
import types
from pathlib import Path

import drive_sync_tripwire as tw
import heartbeat_alarm as hba


PROT = r"C:\TradingDesk-Local"


# --------------------------------------------------------------------------- #
# _paths_overlap — the core THREAT-2 predicate (equal / ancestor / descendant)
# --------------------------------------------------------------------------- #
def test_overlap_equal():
    assert tw._paths_overlap(PROT, PROT) is True


def test_overlap_ancestor_backed_up_sweeps_tree_in():
    # Backing up C:\ (an ancestor) would sweep TradingDesk-Local in.
    assert tw._paths_overlap(r"C:\\", PROT) is True


def test_overlap_descendant_single_subfolder_added():
    # Adding just the warehouse subfolder still overlaps the protected tree.
    assert tw._paths_overlap(PROT, r"C:\TradingDesk-Local\warehouse") is True


def test_overlap_case_and_separator_insensitive():
    assert tw._paths_overlap("c:/tradingdesk-local", PROT) is True


def test_no_overlap_sibling():
    assert tw._paths_overlap(r"C:\TradingDesk", PROT) is False


def test_no_overlap_different_drive():
    assert tw._paths_overlap(r"D:\TradingDesk-Local", PROT) is False


# --------------------------------------------------------------------------- #
# THREAT 1 — path on the DriveFS virtual volume
# --------------------------------------------------------------------------- #
def test_threat1_clean_when_all_on_system_volume():
    # Every path resolves to a mount root, none is Drive-managed.
    r = tw.check_threat1_volume(
        [Path(PROT)],
        managed_fn=lambda p: (False, "system volume"),
        mount_root_fn=lambda p: "C:\\")
    assert r["tripped"] is False and r["unevaluable"] is False


def test_threat1_trips_when_path_on_drive_volume():
    r = tw.check_threat1_volume(
        [Path(PROT)],
        managed_fn=lambda p: (True, "on the DriveFS volume (label 'Google Drive')"),
        mount_root_fn=lambda p: "G:\\My Drive")
    assert r["tripped"] is True
    assert any("DRIVE VOLUME" in h for h in r["hits"])


def test_threat1_unevaluable_when_mount_root_unknown_fails_closed():
    # GetVolumePathNameW failing must NOT read as "clean" — it is unevaluable.
    r = tw.check_threat1_volume(
        [Path(PROT)],
        managed_fn=lambda p: (False, "system volume"),
        mount_root_fn=lambda p: None)
    assert r["tripped"] is False and r["unevaluable"] is True


# --------------------------------------------------------------------------- #
# THREAT 2 — the Drive backup/mirror registry
# --------------------------------------------------------------------------- #
def test_threat2_clean_when_registry_empty():
    r = tw.check_threat2_registry(
        [PROT],
        drivefs_present_fn=lambda: True,
        roots_fn=lambda: ([], "read 0 rows"),
        mirror_fn=lambda: ([], "none"))
    assert r["tripped"] is False and r["unevaluable"] is False


def test_threat2_trips_on_overlapping_registered_root():
    row = {"root_path": r"C:\TradingDesk-Local", "last_seen_absolute_path": None,
           "sync_type": 2, "is_my_drive": 0, "state": 1}
    r = tw.check_threat2_registry(
        [PROT],
        drivefs_present_fn=lambda: True,
        roots_fn=lambda: ([row], "read 1 row"),
        mirror_fn=lambda: ([], "none"))
    assert r["tripped"] is True
    assert any("overlaps protected path" in h for h in r["hits"])


def test_threat2_trips_on_ancestor_backup_via_last_seen():
    # Registry can carry the live location in last_seen_absolute_path; an ancestor trips.
    row = {"root_path": None, "last_seen_absolute_path": r"C:\\",
           "sync_type": 3, "is_my_drive": 0, "state": 1}
    r = tw.check_threat2_registry(
        [PROT],
        drivefs_present_fn=lambda: True,
        roots_fn=lambda: ([row], "read 1 row"),
        mirror_fn=lambda: ([], "none"))
    assert r["tripped"] is True


def test_threat2_ignores_unrelated_backup_root():
    # A backup of some OTHER folder must NOT false-page.
    row = {"root_path": r"C:\Users\andre\Documents", "last_seen_absolute_path": None,
           "sync_type": 2, "is_my_drive": 0, "state": 1}
    r = tw.check_threat2_registry(
        [PROT],
        drivefs_present_fn=lambda: True,
        roots_fn=lambda: ([row], "read 1 row"),
        mirror_fn=lambda: ([], "none"))
    assert r["tripped"] is False and r["unevaluable"] is False


def test_threat2_trips_via_mirror_db_corroboration():
    r = tw.check_threat2_registry(
        [PROT],
        drivefs_present_fn=lambda: True,
        roots_fn=lambda: ([], "read 0 rows"),
        mirror_fn=lambda: ([r"C:\TradingDesk-Local\warehouse"], "1 mirror path"))
    assert r["tripped"] is True


def test_threat2_unreadable_registry_fails_closed():
    # DriveFS installed but the roots DB can't be read -> unevaluable (NOT clean).
    r = tw.check_threat2_registry(
        [PROT],
        drivefs_present_fn=lambda: True,
        roots_fn=lambda: (None, "could not open root_preference DB read-only"),
        mirror_fn=lambda: ([], "none"))
    assert r["tripped"] is False and r["unevaluable"] is True


def test_threat2_clean_when_drivefs_not_installed():
    # No DriveFS at all -> trivially clean, NOT unevaluable (nothing could manage it).
    r = tw.check_threat2_registry(
        [PROT],
        drivefs_present_fn=lambda: False,
        roots_fn=lambda: (None, "should not be called"),
        mirror_fn=lambda: ([], "none"))
    assert r["tripped"] is False and r["unevaluable"] is False


# --------------------------------------------------------------------------- #
# PROXY — positive-only on-disk fingerprints
# --------------------------------------------------------------------------- #
def _entry(name, path, is_dir=False):
    return types.SimpleNamespace(
        name=name, path=path, is_dir=lambda follow_symlinks=True: is_dir)


def test_proxy_clean_when_no_artifacts():
    r = tw.check_proxy_artifacts(
        PROT,
        scandir_fn=lambda d: [_entry("data.parquet", os.path.join(str(d), "data.parquet"))],
        reparse_fn=lambda p: False)
    assert r["tripped"] is False and r["unevaluable"] is False


def test_proxy_trips_on_reparse_point():
    r = tw.check_proxy_artifacts(
        PROT,
        scandir_fn=lambda d: [_entry("x", os.path.join(str(d), "x"))],
        reparse_fn=lambda p: True)
    assert r["tripped"] is True
    assert any("reparse-point" in h for h in r["hits"])


def test_proxy_trips_on_drive_transfer_dir():
    r = tw.check_proxy_artifacts(
        PROT,
        scandir_fn=lambda d: [_entry(".tmp.drivedownload",
                                     os.path.join(str(d), ".tmp.drivedownload"),
                                     is_dir=True)],
        reparse_fn=lambda p: False)
    assert r["tripped"] is True
    assert any("transfer temp dir" in h for h in r["hits"])


# --------------------------------------------------------------------------- #
# evaluate() — combining the checks
# --------------------------------------------------------------------------- #
def _clean(name):
    return {"tripped": False, "unevaluable": False, "hits": [], "note": f"{name} clean"}


def _tripped(name, hit):
    return {"tripped": True, "unevaluable": False, "hits": [hit], "note": f"{name} tripped"}


def _uneval(name):
    return {"tripped": False, "unevaluable": True, "hits": [], "note": f"{name} uneval"}


def test_evaluate_green_when_all_clean():
    v = tw.evaluate(
        threat1_fn=lambda paths: _clean("t1"),
        threat2_fn=lambda pstr: _clean("t2"),
        proxy_fn=lambda root: _clean("px"))
    assert v["ok"] is True and v["should_page"] is False
    assert v["reasons"] == []


def test_evaluate_pages_when_any_tripped_and_names_folder_and_remediation():
    v = tw.evaluate(
        threat1_fn=lambda paths: _tripped("t1", "C:\\TradingDesk-Local is ON THE DRIVE VOLUME"),
        threat2_fn=lambda pstr: _clean("t2"),
        proxy_fn=lambda root: _clean("px"))
    assert v["tripped"] is True and v["should_page"] is True and v["ok"] is False
    assert "TradingDesk-Local" in v["remediation"]
    assert "Google Drive Desktop" in v["remediation"]
    assert any("DRIVE VOLUME" in r for r in v["reasons"])


def test_evaluate_pages_when_unevaluable_fails_closed():
    v = tw.evaluate(
        threat1_fn=lambda paths: _clean("t1"),
        threat2_fn=lambda pstr: _uneval("t2"),
        proxy_fn=lambda root: _clean("px"))
    assert v["unevaluable"] is True and v["should_page"] is True and v["ok"] is False
    assert any("COULD NOT EVALUATE" in r for r in v["reasons"])


# --------------------------------------------------------------------------- #
# LIVE — the current healthy machine state must read GREEN
# --------------------------------------------------------------------------- #
def test_live_current_state_is_green():
    """The whole point: in the CORRECT state (TradingDesk-Local NOT synced) the
    tripwire must PASS. A tripwire that pages while healthy is worthless. Runs the real
    checks against the live machine — no injection."""
    v = tw.evaluate()
    assert isinstance(v, dict) and "ok" in v
    assert v["ok"] is True, f"tripwire is NOT green on the healthy machine: {v['reasons']}"
    assert v["should_page"] is False


# --------------------------------------------------------------------------- #
# heartbeat_alarm integration — the OUTSTANDING-problem decision
# --------------------------------------------------------------------------- #
# UPDATED 2026-08-05: handle_tripwire no longer sends email directly. Per the owner's
# one-email-a-day rule, it now RETURNS (line, problem|None) and the Drive-sync guard is
# folded into the single consolidated morning digest (heartbeat_alarm.maybe_send_digest).
# So these tests pin the new contract: GREEN -> no problem; TRIPPED/UNEVALUABLE -> a
# problem descriptor carrying the folder + remediation wording; the handler itself never
# calls _send (the digest owns delivery); and there is no more per-handler cooldown
# (once-a-day de-dupe lives in the digest, covered in test_heartbeat_alarm.py).
def _now():
    return dt.datetime.now().timestamp()


def test_handle_tripwire_green_returns_no_problem_and_never_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(hba, "_send", lambda s, h: sent.append((s, h)) or True)
    monkeypatch.setattr(hba, "_tripwire", types.SimpleNamespace(
        evaluate=lambda: {"ok": True, "tripped": False, "unevaluable": False,
                          "should_page": False, "reasons": [], "remediation": "",
                          "protected": [PROT]}))
    state = {}
    line, problem = hba.handle_tripwire(state, _now())
    assert "GREEN" in line
    assert problem is None
    assert sent == []


def test_handle_tripwire_tripped_returns_problem_naming_folder_and_remediation(monkeypatch):
    sent = []
    monkeypatch.setattr(hba, "_send", lambda s, h: sent.append((s, h)) or True)
    remediation = (r"C:\TradingDesk-Local appears to be under Google Drive sync/backup "
                   r"management — this is the wrong-folder corruption risk; disconnect "
                   r"it in Google Drive Desktop immediately.")
    monkeypatch.setattr(hba, "_tripwire", types.SimpleNamespace(
        evaluate=lambda: {"ok": False, "tripped": True, "unevaluable": False,
                          "should_page": True,
                          "reasons": ["[threat1_volume] C:\\TradingDesk-Local is ON THE DRIVE VOLUME"],
                          "remediation": remediation, "protected": [PROT]}))
    state = {}
    line, problem = hba.handle_tripwire(state, _now())
    assert "OUTSTANDING" in line
    assert sent == [], "the handler must NOT send — the digest owns delivery"
    assert problem is not None
    assert problem["status"] == "TRIPPED"
    assert "TradingDesk-Local" in problem["cause"]
    assert "Google Drive Desktop" in problem["cause"]           # remediation present
    # protected folder surfaced in the detail rows
    assert any("TradingDesk-Local" in str(v) for _, v in problem["rows"])


def test_handle_tripwire_unevaluable_returns_problem_when_module_missing(monkeypatch):
    # If the tripwire module itself failed to import, handle_tripwire must FAIL CLOSED —
    # now by returning an OUTSTANDING problem (folded into the digest), not by paging.
    sent = []
    monkeypatch.setattr(hba, "_send", lambda s, h: sent.append((s, h)) or True)
    monkeypatch.setattr(hba, "_tripwire", None)
    state = {}
    line, problem = hba.handle_tripwire(state, _now())
    assert "UNEVALUABLE" in line and "OUTSTANDING" in line
    assert sent == []
    assert problem is not None
    assert problem["status"] == "UNEVALUABLE"
    assert "could not evaluate" in problem["cause"].lower()
    assert "TradingDesk-Local" in problem["cause"] or any(
        "TradingDesk-Local" in str(v) for _, v in problem["rows"])


def test_handle_tripwire_never_sends_directly(monkeypatch):
    # The handler is send-free by construction now: even a TRIPPED evaluation only
    # RETURNS a problem; nothing is emailed until the digest decides.
    monkeypatch.setattr(hba, "_send",
                        lambda s, h: (_ for _ in ()).throw(AssertionError("must not send")))
    monkeypatch.setattr(hba, "_tripwire", types.SimpleNamespace(
        evaluate=lambda: {"ok": False, "tripped": True, "unevaluable": False,
                          "should_page": True, "reasons": ["x"],
                          "remediation": "r", "protected": [PROT]}))
    state = {}
    line, problem = hba.handle_tripwire(state, _now())  # would raise if it sent
    assert problem is not None
    assert "OUTSTANDING" in line


def test_handle_tripwire_no_handler_cooldown_every_sweep_reports_problem(monkeypatch):
    # There is no longer a per-handler cooldown: two consecutive sweeps both return the
    # problem (de-dupe to once-a-day is the digest's job, not the handler's).
    calls = []
    monkeypatch.setattr(hba, "_send", lambda s, h: calls.append(1) or True)
    monkeypatch.setattr(hba, "_tripwire", types.SimpleNamespace(
        evaluate=lambda: {"ok": False, "tripped": True, "unevaluable": False,
                          "should_page": True, "reasons": ["x"],
                          "remediation": "r", "protected": [PROT]}))
    state = {}
    now = _now()
    _, p1 = hba.handle_tripwire(state, now)
    _, p2 = hba.handle_tripwire(state, now + 60)
    assert p1 is not None and p2 is not None
    assert calls == [], "handler never sends; both sweeps just report the problem"


def test_handle_tripwire_recovery_returns_no_problem_and_clears_legacy_cooldown(monkeypatch):
    monkeypatch.setattr(hba, "_send", lambda s, h: True)
    tripped = {"ok": False, "tripped": True, "unevaluable": False, "should_page": True,
               "reasons": ["x"], "remediation": "r", "protected": [PROT]}
    green = {"ok": True, "tripped": False, "unevaluable": False, "should_page": False,
             "reasons": [], "remediation": "", "protected": [PROT]}
    mod = types.SimpleNamespace(evaluate=lambda: tripped)
    monkeypatch.setattr(hba, "_tripwire", mod)
    # Seed a legacy cooldown key to prove recovery still clears it (log accuracy).
    now = _now()
    state = {hba._TRIPWIRE_NAME: {"last_alert_ts": now}}
    _, p_tripped = hba.handle_tripwire(state, now)
    assert p_tripped is not None
    mod.evaluate = lambda: green
    line, p_green = hba.handle_tripwire(state, now + 60)        # recovered
    assert p_green is None
    assert "GREEN" in line
    assert "last_alert_ts" not in state[hba._TRIPWIRE_NAME]
