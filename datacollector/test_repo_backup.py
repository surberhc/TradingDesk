r"""test_repo_backup.py — tests for the verified repo-backup job + its staleness alarm.

OFFLINE + FAST: no git subprocess, no Drive, no network, no email. Every test drives
the injectable seams of repo_backup.run_backup(...) / bundle_verify(...) /
resolve_drive_dest(...) / prune_old_bundles(...) with fakes, and asserts on the
returned status dict. Mirrors test_theta_terminal_watchdog.py's style (pure function +
injected collaborators) and test_heartbeat_alarm.py's tmp_path/back-dated-mtime trick.

WHAT THESE PIN — the 2026-07-16 incident, encoded as regressions:
Google Drive silently synced the WRONG folder for 9 days and NO ERROR WAS RAISED,
because nothing failed. So the tests here are overwhelmingly about the job REFUSING to
report success, and about the heartbeat NOT moving when anything is off. The single
most important assertion in this file, repeated deliberately across every failure
path, is `heartbeat_fn.calls == 0` — because the heartbeat is the only thing standing
between a silent failure and a page.

Run from datacollector/ so `import config` (which heartbeat_alarm imports) resolves:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest test_repo_backup.py -q
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import types

import pytest

import heartbeat_alarm as hba
import repo_backup as rb


NOW = dt.datetime(2026, 7, 16, 12, 0, 0)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class Recorder:
    """Callable that records every call and returns a fixed (or per-call) value."""

    def __init__(self, value=None, values=None):
        self.value = value
        self.values = list(values) if values else None
        self.calls = 0
        self.args = []

    def __call__(self, *a, **k):
        self.calls += 1
        self.args.append((a, k))
        if self.values:
            return self.values.pop(0)
        return self.value


def _proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _resolved(dest=r"X:\My Drive\TradingDesk-Backups", mount=r"X:\My Drive"):
    return {"resolved": True, "dest": dest, "mount": mount, "source": "probe",
            "drive_managed": True, "note": "accepted"}


def _unresolved():
    return {"resolved": False, "dest": None, "mount": None, "source": "none",
            "drive_managed": False, "note": "NOTHING RESOLVED — no Drive-managed mount found"}


def _run(tmp_path, monkeypatch, **over):
    """Drive run_backup with every collaborator faked. Returns (status, fakes).

    Both the local and the 'Drive' destination are REAL temp dirs: run_backup legitimately
    mkdir()s its destinations, and a fake path like X:\\ would fail there for the wrong
    reason and mask the behaviour under test.
    """
    monkeypatch.setattr(rb, "LOCAL_BACKUP_DIR", tmp_path / "backups")
    fake_drive = tmp_path / "drive" / "My Drive"
    fakes = {
        "resolve_fn": over.get("resolve_fn", Recorder(_resolved(
            dest=str(fake_drive / "TradingDesk-Backups"), mount=str(fake_drive)))),
        "paused_fn": over.get("paused_fn", Recorder((False, "syncing is ON"))),
        "create_fn": over.get("create_fn", Recorder(_proc(0))),
        "verify_fn": over.get("verify_fn", Recorder((True, "okay + full history"))),
        "copy_fn": over.get("copy_fn", Recorder(None)),
        "prune_fn": over.get("prune_fn", Recorder([])),
        "facts_fn": over.get("facts_fn", Recorder({"head_sha": "abc123", "commit_count": 294})),
        "size_fn": over.get("size_fn", Recorder(41_573_813)),
        "status_fn": over.get("status_fn", Recorder(None)),
        "heartbeat_fn": over.get("heartbeat_fn", Recorder(None)),
        "log_fn": over.get("log_fn", Recorder(None)),
    }
    st = rb.run_backup(now=NOW, dry_run=over.get("dry_run", False), **fakes)
    return st, fakes


# --------------------------------------------------------------------------- #
# THE HAPPY PATH — success must still work (don't over-correct into always-fail)
# --------------------------------------------------------------------------- #
def test_verified_success_refreshes_heartbeat(tmp_path, monkeypatch):
    st, f = _run(tmp_path, monkeypatch)
    assert st["ok"] is True
    assert f["heartbeat_fn"].calls == 1          # the ONLY path that may move it
    assert st["head_sha"] == "abc123"
    assert st["commit_count"] == 294
    assert st["size_bytes"] == 41_573_813
    assert st["errors"] == []
    # Verified in BOTH places: locally and again at the Drive destination.
    assert f["verify_fn"].calls == 2
    assert st["bundle_name"] == "tradingdesk-repo-20260716-120000.bundle"


def test_success_heartbeat_text_avoids_the_COMPLETE_marker(tmp_path, monkeypatch):
    """heartbeat_alarm.assess() treats a literal 'COMPLETE' in the heartbeat text as a
    finished-job marker. Our text must not trip that branch by accident (e.g. by
    quoting git's 'records a complete history')."""
    st, f = _run(tmp_path, monkeypatch)
    text = f["heartbeat_fn"].args[0][0][0]
    assert "COMPLETE" not in text.upper()


# --------------------------------------------------------------------------- #
# FAILURE MODE: bundle verify failure is caught
# --------------------------------------------------------------------------- #
def test_local_bundle_verify_failure_is_caught_and_never_reports_success(tmp_path, monkeypatch):
    st, f = _run(tmp_path, monkeypatch,
                 verify_fn=Recorder((False, "git bundle verify exited 1: corrupt")))
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0          # THE point: no silent green light
    assert any("LOCAL bundle failed verification" in e for e in st["errors"])
    assert f["copy_fn"].calls == 0               # never ship a bundle that didn't verify


def test_drive_copy_verify_failure_is_caught(tmp_path, monkeypatch):
    """Local verify passes, the copy lands, but the bundle at the DRIVE destination
    does not verify (truncated/corrupt on arrival). That is a FAILED backup."""
    st, f = _run(tmp_path, monkeypatch,
                 verify_fn=Recorder(values=[(True, "okay + full history"),
                                            (False, "git bundle verify exited 1: corrupt")]))
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert any("DRIVE destination failed verification" in e for e in st["errors"])


def test_bundle_create_failure_is_caught(tmp_path, monkeypatch):
    st, f = _run(tmp_path, monkeypatch, create_fn=Recorder(_proc(128, stderr="fatal: bad")))
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert any("git bundle create failed" in e for e in st["errors"])


# --------------------------------------------------------------------------- #
# bundle_verify() itself — the exact strings git actually emits
# --------------------------------------------------------------------------- #
def test_bundle_verify_accepts_real_git_output():
    """Verbatim shape of real `git bundle verify` output (captured 2026-07-16). git
    writes this report to STDERR, which is why bundle_verify joins both streams."""
    real = ("probe.bundle is okay\n"
            "The bundle contains these 3 refs:\n"
            "0a73cec... refs/heads/main\n"
            "The bundle records a complete history.\n"
            "The bundle uses this hash algorithm: sha1\n")
    ok, detail = rb.bundle_verify("x.bundle", run_fn=lambda: _proc(0, stderr=real))
    assert ok is True
    assert detail == "okay + full history"


def test_bundle_verify_rejects_nonzero_exit():
    ok, detail = rb.bundle_verify(
        "x.bundle", run_fn=lambda: _proc(1, stderr="error: not a bundle"))
    assert ok is False
    assert "exited 1" in detail


def test_bundle_verify_rejects_missing_is_okay():
    ok, detail = rb.bundle_verify("x.bundle", run_fn=lambda: _proc(0, stderr="weird output"))
    assert ok is False
    assert "is okay" in detail


def test_bundle_verify_rejects_incomplete_history():
    """A thin bundle can verify 'okay' while recording only a PARTIAL history — it
    needs a base repo we may not have, so it is NOT a standalone backup. Hard fail."""
    thin = ("x.bundle is okay\n"
            "The bundle requires these 1 ref:\n"
            "0a73cec... refs/heads/main\n")
    ok, detail = rb.bundle_verify("x.bundle", run_fn=lambda: _proc(0, stderr=thin))
    assert ok is False
    assert "complete history" in detail


def test_bundle_verify_survives_raising_git():
    ok, detail = rb.bundle_verify("x.bundle", run_fn=_raise)
    assert ok is False
    assert "raised" in detail


def _raise():
    raise subprocess.TimeoutExpired(cmd="git", timeout=1)


# --------------------------------------------------------------------------- #
# FAILURE MODE: Drive destination unresolvable -> LOUD failure, not a silent skip
# --------------------------------------------------------------------------- #
def test_unresolvable_drive_fails_loudly_and_never_skips_silently(tmp_path, monkeypatch):
    """THE CORE REGRESSION. A backup that cannot find Drive must NOT quietly fall back
    to 'local only' and report success — that is the silent failure, restated."""
    st, f = _run(tmp_path, monkeypatch, resolve_fn=Recorder(_unresolved()))
    assert st["ok"] is False
    assert st["drive_resolved"] is False
    assert f["heartbeat_fn"].calls == 0
    assert any("could NOT be resolved" in e for e in st["errors"])
    # It must not even bundle: no success is possible, and we say so rather than
    # producing a local-only artifact that could be mistaken for a good backup.
    assert f["create_fn"].calls == 0
    # The failure is RECORDED (forensics) even though the heartbeat stays cold.
    assert f["status_fn"].calls >= 1


def test_resolve_rejects_a_non_drive_managed_candidate(tmp_path):
    """A path that EXISTS but is an ordinary local folder is the wrong-folder mode.
    It must be rejected, loudly, not accepted because it happens to be writable."""
    info = rb.resolve_drive_dest(
        db_fn=lambda: (None, "db empty"),
        candidates=[r"C:\Users\andre\Not Really Drive\My Drive"],
        exists_fn=lambda p: True,
        drive_managed_fn=lambda p: (False, "destination resolves to the SYSTEM volume"))
    assert info["resolved"] is False
    assert info["dest"] is None
    assert "REJECTED" in info["note"]
    assert "NOTHING RESOLVED" in info["note"]


def test_resolve_rejects_db_supplied_path_that_is_not_drive_managed():
    """Even a path the DriveFS DB itself names must pass the volume check. The live
    `media` table names 'C:\\' — trusting the DB blindly would back up into the system
    volume and call it Drive."""
    info = rb.resolve_drive_dest(
        db_fn=lambda: ("C:\\", "resolved from DriveFS DB"),
        candidates=[],
        exists_fn=lambda p: True,
        drive_managed_fn=lambda p: (False, "destination resolves to the SYSTEM volume (C:\\)"))
    assert info["resolved"] is False
    assert "REJECTED" in info["note"]


def test_resolve_prefers_db_then_falls_back_to_probe():
    info = rb.resolve_drive_dest(
        db_fn=lambda: (None, "roots empty (streaming mode)"),
        candidates=[r"X:\My Drive"],
        exists_fn=lambda p: True,
        drive_managed_fn=lambda p: (True, "on the DriveFS volume"))
    assert info["resolved"] is True
    assert info["source"] == "probe"
    assert info["dest"] == os.path.join(r"X:\My Drive", "TradingDesk-Backups")


def test_resolve_skips_candidate_that_does_not_exist():
    info = rb.resolve_drive_dest(
        db_fn=lambda: (None, "db empty"),
        candidates=[r"X:\Gone", r"Y:\My Drive"],
        exists_fn=lambda p: str(p) != r"X:\Gone",
        drive_managed_fn=lambda p: (True, "on the DriveFS volume"))
    assert info["resolved"] is True
    assert info["mount"] == r"Y:\My Drive"
    assert "does not exist" in info["note"]


# --------------------------------------------------------------------------- #
# is_drive_managed() — the wrong-folder check
# --------------------------------------------------------------------------- #
def test_drive_managed_rejects_system_volume():
    ok, why = rb.is_drive_managed(
        r"C:\Users\andre\My Drive\TradingDesk-Backups",
        mount_root_fn=lambda p: "C:\\",
        label_fn=lambda r: "Windows")
    assert ok is False
    assert "SYSTEM volume" in why


def test_drive_managed_rejects_wrong_label():
    ok, why = rb.is_drive_managed(
        r"D:\Backups",
        mount_root_fn=lambda p: "D:\\" if "D:" in str(p) else "C:\\",
        label_fn=lambda r: "Backup HDD")
    assert ok is False
    assert "expected" in why and "Google Drive" in why


def test_drive_managed_accepts_the_real_drivefs_shape():
    """The live shape measured on this machine 2026-07-16: DriveFS mounts a separate
    volume (junction to a Volume{GUID}) labelled 'Google Drive'."""
    drive_root = "C:\\Users\\andre\\Google Drive Sync Surber HC\\"

    def mount_root_fn(p):
        return "C:\\" if str(p).lower().startswith("c:\\windows") else drive_root

    ok, why = rb.is_drive_managed(
        drive_root + "My Drive", mount_root_fn=mount_root_fn,
        label_fn=lambda r: "Google Drive")
    assert ok is True
    assert "DriveFS volume" in why


def test_drive_managed_fails_when_volume_unknown():
    ok, why = rb.is_drive_managed("whatever", mount_root_fn=lambda p: None,
                                  label_fn=lambda r: None)
    assert ok is False
    assert "could not determine" in why


# --------------------------------------------------------------------------- #
# FAILURE MODE: paused sync
# --------------------------------------------------------------------------- #
def test_paused_sync_detected_from_real_log_line():
    """Verbatim line from drive_fs.txt on 2026-07-16 (the ~2.5h silent-pause window)."""
    log = ("2026-07-16T16:38:29.028ZI [6232:NonCelloThread] "
           "presence_tracker.cc:579:NotifyPauseSyncing Syncing is paused\n"
           "2026-07-16T16:38:29.028ZI [15508:core_1128] "
           "platform.cc:196:SetIsBackgroundSyncingEnabled Background syncing has been disabled\n")
    paused, why = rb.is_sync_paused(read_fn=lambda p: log)
    assert paused is True
    assert "PAUSED" in why


def test_resumed_sync_reads_as_not_paused():
    """Drive logs pause AND resume through the SAME call site, so only the LAST such
    line describes current state — a resume after a pause must read as ON."""
    log = ("2026-07-16T16:38:29.028ZI presence_tracker.cc:579:NotifyPauseSyncing Syncing is paused\n"
           "2026-07-16T18:59:07.465ZI presence_tracker.cc:579:NotifyPauseSyncing Syncing is on\n")
    paused, why = rb.is_sync_paused(read_fn=lambda p: log)
    assert paused is False


def test_pause_state_unknown_when_no_marker_line():
    paused, why = rb.is_sync_paused(read_fn=lambda p: "boring startup noise\n")
    assert paused is None
    assert "cannot tell" in why


def test_pause_state_unknown_when_log_unreadable():
    def boom(p):
        raise OSError("file locked")
    paused, why = rb.is_sync_paused(read_fn=boom)
    assert paused is None


def test_paused_sync_blocks_the_backup(tmp_path, monkeypatch):
    """Paused sync is an ALARM condition, not a pass: the write would be accepted
    locally and never uploaded."""
    st, f = _run(tmp_path, monkeypatch,
                 paused_fn=Recorder((True, "Drive log's last pause-state line says PAUSED")))
    assert st["ok"] is False
    assert st["sync_paused"] is True
    assert f["heartbeat_fn"].calls == 0
    assert f["create_fn"].calls == 0
    assert any("PAUSED" in e for e in st["errors"])


def test_unknown_pause_state_does_not_block_but_is_recorded(tmp_path, monkeypatch):
    """Unknown != paused (the log may simply have rotated). We proceed, but we record
    that we could not tell rather than laundering it into a clean pass."""
    st, f = _run(tmp_path, monkeypatch,
                 paused_fn=Recorder((None, "no marker line — cannot tell")))
    assert st["ok"] is True
    assert st["sync_paused"] is None
    assert "cannot tell" in st["sync_note"]


# --------------------------------------------------------------------------- #
# RETENTION — deletes ONLY its own bundles
# --------------------------------------------------------------------------- #
def _mk(d, name, content=b"x"):
    p = d / name
    p.write_bytes(content)
    return p


def test_retention_deletes_only_its_own_bundles(tmp_path):
    """THE SAFETY TEST. The Drive folder really does contain a hand-made rescue bundle
    (`tradingdesk-full-20260716.bundle`) plus unrelated files. Retention must delete
    ONLY files matching its own strict pattern and leave every bystander alone."""
    d = tmp_path / "backups"
    d.mkdir()
    # 9 of ours (keep 7 -> delete the 2 oldest).
    ours = [f"tradingdesk-repo-2026070{i}-120000.bundle" for i in range(1, 10)]
    for n in ours:
        _mk(d, n)
    # Bystanders that MUST survive — including the real rescue bundle's exact name.
    bystanders = [
        "tradingdesk-full-20260716.bundle",     # the real hand-made rescue copy
        "tradingdesk-repo-backup.bundle",       # no timestamp -> not ours
        "tradingdesk-repo-20260701.bundle",     # date only, no time -> not ours
        "tradingdesk-repo-20260701-120000.bundle.bak",  # suffixed -> not ours
        "notes.txt",
        "repo_backup_status.json",
    ]
    for n in bystanders:
        _mk(d, n)

    deleted = prune = rb.prune_old_bundles(d, keep=7, allowed_dirs=[str(d)],
                                           log_fn=lambda m: None)
    assert deleted == ["tradingdesk-repo-20260701-120000.bundle",
                       "tradingdesk-repo-20260702-120000.bundle"]
    for n in bystanders:
        assert (d / n).exists(), f"retention deleted a bystander: {n}"
    assert len([p for p in d.iterdir() if rb.BUNDLE_RE.match(p.name)]) == 7


def test_retention_refuses_a_directory_outside_the_allow_list(tmp_path):
    """Guard 1: even a folder full of perfectly-matching names is untouched if it is
    not an allow-listed backup dir."""
    d = tmp_path / "somewhere_else"
    d.mkdir()
    for i in range(1, 10):
        _mk(d, f"tradingdesk-repo-2026070{i}-120000.bundle")
    deleted = rb.prune_old_bundles(d, keep=1, allowed_dirs=[str(tmp_path / "backups")],
                                   log_fn=lambda m: None)
    assert deleted == []
    assert len(list(d.iterdir())) == 9


def test_retention_keeps_everything_when_under_the_limit(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    for i in range(1, 4):
        _mk(d, f"tradingdesk-repo-2026070{i}-120000.bundle")
    deleted = rb.prune_old_bundles(d, keep=7, allowed_dirs=[str(d)], log_fn=lambda m: None)
    assert deleted == []
    assert len(list(d.iterdir())) == 3


def test_retention_never_deletes_directories(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    (d / "tradingdesk-repo-20260701-120000.bundle").mkdir()   # a DIR wearing our name
    for i in range(2, 10):
        _mk(d, f"tradingdesk-repo-2026070{i}-120000.bundle")
    deleted = rb.prune_old_bundles(d, keep=7, allowed_dirs=[str(d)], log_fn=lambda m: None)
    assert "tradingdesk-repo-20260701-120000.bundle" not in deleted
    assert (d / "tradingdesk-repo-20260701-120000.bundle").is_dir()


def test_bundle_regex_does_not_match_the_real_rescue_bundle():
    """Pinned separately because this one filename is the whole reason the pattern is
    strict. If someone loosens BUNDLE_RE, this fails before it eats Andrew's copy."""
    assert not rb.BUNDLE_RE.match("tradingdesk-full-20260716.bundle")
    assert rb.BUNDLE_RE.match("tradingdesk-repo-20260716-143000.bundle")


def test_retention_is_not_reached_on_a_failed_backup(tmp_path, monkeypatch):
    """No deletion may happen on a run that failed — we never trade an old good
    bundle for a new bad one."""
    st, f = _run(tmp_path, monkeypatch, verify_fn=Recorder((False, "corrupt")))
    assert st["ok"] is False
    assert f["prune_fn"].calls == 0


# --------------------------------------------------------------------------- #
# DRY-RUN — creates nothing, deletes nothing, never moves the heartbeat
# --------------------------------------------------------------------------- #
def test_dry_run_creates_nothing_and_leaves_heartbeat_alone(tmp_path, monkeypatch):
    st, f = _run(tmp_path, monkeypatch, dry_run=True)
    assert f["create_fn"].calls == 0
    assert f["copy_fn"].calls == 0
    assert f["prune_fn"].calls == 0
    assert f["heartbeat_fn"].calls == 0
    assert st["ok"] is False          # a dry run is NEVER a successful backup


# --------------------------------------------------------------------------- #
# THE ALARM — staleness triggers it
# --------------------------------------------------------------------------- #
def _backup_job(tmp_path):
    return {"name": "repo_backup",
            "label": "TradingDesk repo backup (git bundle -> Drive)",
            "heartbeat": tmp_path / "repo_backup_heartbeat.txt",
            "progress": None,
            "threshold_s": hba.REPO_BACKUP_THRESHOLD_S,
            "task_name": "RepoBackupDaily"}


def _hb(job, now, age_s, text="2026-07-16 12:00:00  repo backup verified ..."):
    p = job["heartbeat"]
    p.write_text(text)
    os.utime(p, (now - age_s, now - age_s))
    return p


def test_alarm_fires_when_no_verified_backup_within_threshold(tmp_path):
    """The heartbeat only ever moves on a VERIFIED success, so a cold one means the
    backup failed OR never ran — indistinguishable, and both must page."""
    now = dt.datetime.now().timestamp()
    job = _backup_job(tmp_path)
    _hb(job, now, age_s=30 * 3600)          # 30h > 26h threshold
    a = hba.assess(job, now)
    assert a["status"] == "stale"
    assert a["alert"] is True


def test_alarm_quiet_when_backup_is_fresh(tmp_path):
    now = dt.datetime.now().timestamp()
    job = _backup_job(tmp_path)
    _hb(job, now, age_s=2 * 3600)           # 2h — a normal daily cadence
    a = hba.assess(job, now)
    assert a["status"] == "fresh"
    assert a["alert"] is False


def test_alarm_fires_when_heartbeat_never_existed(tmp_path):
    """No heartbeat at all = no verified backup has EVER landed. Must alert, not
    shrug — 'never ran' is the failure mode that hid for 9 days."""
    now = dt.datetime.now().timestamp()
    job = _backup_job(tmp_path)             # file deliberately not created
    a = hba.assess(job, now)
    assert a["status"] == "missing"
    assert a["alert"] is True


def test_alarm_boundary_just_inside_threshold_is_quiet(tmp_path):
    now = dt.datetime.now().timestamp()
    job = _backup_job(tmp_path)
    _hb(job, now, age_s=hba.REPO_BACKUP_THRESHOLD_S - 600)
    assert hba.assess(job, now)["alert"] is False


def test_alarm_boundary_just_past_threshold_fires(tmp_path):
    now = dt.datetime.now().timestamp()
    job = _backup_job(tmp_path)
    _hb(job, now, age_s=hba.REPO_BACKUP_THRESHOLD_S + 600)
    assert hba.assess(job, now)["alert"] is True


def test_repo_backup_job_is_actually_registered_in_JOBS():
    """Wiring test: the job must be in the alarm's watch list, pointed at the SAME
    heartbeat the backup job writes. A perfect backup job nobody watches is how you
    get another silent 9 days."""
    jobs = [j for j in hba.JOBS if j["name"] == "repo_backup"]
    assert len(jobs) == 1, "repo_backup is not registered in heartbeat_alarm.JOBS"
    job = jobs[0]
    assert str(job["heartbeat"]) == str(rb.HEARTBEAT_FILE)
    assert job["threshold_s"] == hba.REPO_BACKUP_THRESHOLD_S


def test_alert_body_uses_the_backup_specific_wording(tmp_path):
    """The default alert text says 'the supervisor writes every ~30s', which is false
    for a once-daily backup. The override must be used."""
    now = dt.datetime.now().timestamp()
    job = dict(_backup_job(tmp_path))
    job["cause_stale"] = "No VERIFIED repo backup has landed in {age}. Check <b>{task_name}</b>."
    _hb(job, now, age_s=30 * 3600)
    a = hba.assess(job, now)
    subject, html = hba._build_alert(job, a)
    assert "No VERIFIED repo backup has landed" in html
    assert "supervisor writes every ~30s" not in html
    assert "RepoBackupDaily" in html


def test_registered_job_alert_renders_with_real_wording(tmp_path):
    """End-to-end on the REAL JOBS entry (not a fixture): render its stale alert."""
    now = dt.datetime.now().timestamp()
    job = dict(next(j for j in hba.JOBS if j["name"] == "repo_backup"))
    job["heartbeat"] = tmp_path / "hb.txt"
    _hb(job, now, age_s=40 * 3600)
    a = hba.assess(job, now)
    subject, html = hba._build_alert(job, a)
    assert "TradingDesk ALARM" in subject
    assert "silent-sync failure" in html
    assert "supervisor writes every ~30s" not in html
