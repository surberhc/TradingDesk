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
import hashlib
import io
import json
import os
import subprocess
import sys
import types
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

import drive_oauth_consent as doc
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


def _cloud(state="skipped_not_configured", note=None, **over):
    """A `cloud` block shaped exactly like verify_cloud_arrival() returns one."""
    d = {"checked": state != "skipped_not_configured", "state": state,
         "folder_id": None, "file_id": None, "cloud_md5": None, "local_md5": None,
         "required": False,
         "note": note if note is not None else f"cloud state {state}"}
    d.update(over)
    return d


def _run(tmp_path, monkeypatch, **over):
    """Drive run_backup with every collaborator faked. Returns (status, fakes).

    Both the local and the 'Drive' destination are REAL temp dirs: run_backup legitimately
    mkdir()s its destinations, and a fake path like X:\\ would fail there for the wrong
    reason and mask the behaviour under test.

    cloud_fn is faked like everything else and MUST stay that way: the real one talks to
    Google. Defaulting it here (rather than letting run_backup reach for its own) is what
    keeps this suite offline even on a machine where drive_oauth.json really exists.
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
        # Default represents the world AS OF 2026-07-17: the Drive API credential is
        # configured and a normal run's bytes verify in the cloud. (Before the credential
        # existed this defaulted to "skipped_not_configured"; with CLOUD_VERIFY_REQUIRED
        # now True a skipped cloud fails closed, so the general-path tests must see the
        # verified state their subject assumes.) Tests specifically about the NO-credential
        # condition construct that state explicitly rather than leaning on this default.
        "cloud_fn": over.get("cloud_fn", Recorder(_cloud("verified"))),
        "log_fn": over.get("log_fn", Recorder(None)),
        # The `git bundle list-heads` seam. reuse_fn is deliberately NOT faked: the
        # skip DECISION is the thing under test, so the real find_reusable_bundle runs
        # against real temp dirs with only git faked out from under it. In the tests
        # that seed no bundle it short-circuits before head_fn is ever called, so this
        # suite stays as offline as it was.
        "head_fn": over.get("head_fn", Recorder((None, "no HEAD line"))),
    }
    st = rb.run_backup(now=NOW, dry_run=over.get("dry_run", False),
                       force=over.get("force", False), **fakes)
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
# THE REDUNDANT-BUNDLE SKIP — must never mask a broken backup
# --------------------------------------------------------------------------- #
# Real shas from this machine 2026-07-16: three bundles were written that day all
# carrying HEAD b50e78b (identical, ~41MB each, all three redundant), and the repo has
# since moved to e820aab. Those are the two states the skip has to tell apart.
OLD_HEAD = "b50e78b9076bc85983f565167f968b0c91cb92f7"
CUR_HEAD = "e820aab43efed21b25a380ac8d56e7ab895586f0"
EXISTING = "tradingdesk-repo-20260716-151950.bundle"    # the real newest bundle's name

# THE BIAS UNDER TEST, stated once: skip ONLY on affirmative proof, bundle on ANY
# doubt. Every "does not skip" test below is really asserting that a needless 41MB
# bundle is always the cheaper mistake. The tests are lopsided on purpose — one wrong
# skip reports a healthy backup while there is none, which is the 2026-07-16 silent
# failure rebuilt by our own hands and wired straight to the heartbeat.


def _seed(tmp_path, name=EXISTING, *, local=True, drive=True, content=b"bundle-bytes"):
    """Leave a bundle where a previous run would have left it. Mirrors _run's dirs."""
    ldir = tmp_path / "backups"
    ddir = tmp_path / "drive" / "My Drive" / "TradingDesk-Backups"
    ldir.mkdir(parents=True, exist_ok=True)
    ddir.mkdir(parents=True, exist_ok=True)
    if local:
        (ldir / name).write_bytes(content)
    if drive:
        (ddir / name).write_bytes(content)
    return ldir, ddir


def _head_fn(local_sha, drive_sha="same"):
    """Fake `git bundle list-heads`, answering per PATH.

    The local copy and the Drive copy are asked SEPARATELY and can be made to
    disagree — 'same name' is not 'same file', and the skip has to know that.
    """
    def head_fn(path):
        sha = local_sha if drive_sha == "same" or "TradingDesk-Backups" not in str(path) \
            else drive_sha
        return (sha, f"bundle records HEAD {sha}") if sha else (None, "no HEAD line")
    return head_fn


def _facts(head=CUR_HEAD):
    return Recorder({"head_sha": head, "commit_count": 298})


def test_head_unchanged_and_bundle_still_good_skips_and_creates_nothing(tmp_path, monkeypatch):
    """THE POINT OF THE WHOLE BUILD. `wrap` fires this job whenever Andrew wraps; if
    HEAD has not moved, a new bundle would be a byte-for-byte twin of the last one.
    Seven of those evict seven days of genuinely distinct history under KEEP_LAST."""
    _seed(tmp_path)
    st, f = _run(tmp_path, monkeypatch, facts_fn=_facts(), head_fn=_head_fn(CUR_HEAD))
    assert st["state"] == rb.STATE_SKIPPED_HEAD_UNCHANGED
    assert st["ok"] is True
    assert f["create_fn"].calls == 0          # no redundant 41MB twin
    assert f["copy_fn"].calls == 0
    assert st["bundle_name"] == EXISTING      # the run reports the bundle it LEANED ON
    # Nothing was inherited from the last run's status file: BOTH copies were verified
    # again, right now, before the skip was allowed.
    assert f["verify_fn"].calls == 2
    verified = [str(a[0][0]) for a in f["verify_fn"].args]
    assert all(EXISTING in p for p in verified)
    assert any("TradingDesk-Backups" in p for p in verified)


def test_a_skipped_run_refreshes_the_heartbeat(tmp_path, monkeypatch):
    """A verified, current, off-machine bundle EXISTS — which is the only thing this
    heartbeat has ever asserted. Leaving it cold would page for a backup that is
    sitting right there, and a page that is wrong is how the alarm becomes noise."""
    _seed(tmp_path)
    st, f = _run(tmp_path, monkeypatch, facts_fn=_facts(), head_fn=_head_fn(CUR_HEAD))
    assert f["heartbeat_fn"].calls == 1
    text = f["heartbeat_fn"].args[0][0][0]
    # ...but the one line a paged human reads at 2am must not imply work that never
    # happened.
    assert "no-new-bundle" in text
    assert "HEAD unchanged" in text
    assert "COMPLETE" not in text.upper()     # still must not trip assess()'s marker


def test_a_skipped_run_proves_string_says_no_new_bundle_and_names_the_covering_one(
        tmp_path, monkeypatch):
    """THE HONESTY ASSERTION. An overstated `proves` is the exact bug class this file's
    recent history is about. A skip must say (a) it created nothing and (b) WHICH
    bundle is actually carrying the backup — otherwise 'ok: true' reads as 'I backed
    you up just now', which is false."""
    _seed(tmp_path)
    st, _ = _run(tmp_path, monkeypatch, facts_fn=_facts(), head_fn=_head_fn(CUR_HEAD))
    assert "NO NEW BUNDLE WAS CREATED BY THIS RUN" in st["proves"]
    assert EXISTING in st["proves"]                       # names the covering bundle
    assert "RE-VERIFIED just now" in st["proves"]
    # The cloud clause survives the skip wrapper; with the credential now configured the
    # default run verifies in the cloud, so it is the verified clause that flows through.
    assert "confirmed present in Google's cloud" in st["proves"]
    assert st["proves"] == rb.proves_skipped(EXISTING, rb.PROVES_CLOUD_VERIFIED)


def test_head_unchanged_but_no_bundle_exists_bundles_fresh(tmp_path, monkeypatch):
    """Nothing to lean on — the only honest move is to make one."""
    st, f = _run(tmp_path, monkeypatch, facts_fn=_facts(), head_fn=_head_fn(CUR_HEAD))
    assert st["state"] == rb.STATE_VERIFIED_NEW
    assert f["create_fn"].calls == 1
    assert "no bundle this job created exists" in st["reuse_note"]
    assert "NO NEW BUNDLE" not in st["proves"]


def test_head_unchanged_but_the_prior_bundle_is_MISSING_bundles_fresh(tmp_path, monkeypatch):
    """The bundle is gone from disk. A skip here would report a backup that does not
    exist — the failure mode this whole module was written after."""
    _seed(tmp_path)
    (tmp_path / "backups" / EXISTING).unlink()
    st, f = _run(tmp_path, monkeypatch, facts_fn=_facts(), head_fn=_head_fn(CUR_HEAD))
    assert st["state"] == rb.STATE_VERIFIED_NEW
    assert f["create_fn"].calls == 1
    assert f["heartbeat_fn"].calls == 1
    assert "NO NEW BUNDLE" not in st["proves"]
    assert st["proves"] == rb.PROVES_CLOUD_VERIFIED


def test_a_local_only_bundle_does_not_license_a_skip(tmp_path, monkeypatch):
    """It is missing from DRIVE. A local-only bundle is not the backup this job
    promises — the machine is the thing we are insuring against."""
    _seed(tmp_path, drive=False)
    st, f = _run(tmp_path, monkeypatch, facts_fn=_facts(), head_fn=_head_fn(CUR_HEAD))
    assert st["state"] == rb.STATE_VERIFIED_NEW
    assert f["create_fn"].calls == 1
    assert "MISSING from the Drive destination" in st["reuse_note"]
    assert "NO NEW BUNDLE" not in st["proves"]


def test_head_unchanged_but_the_prior_bundle_FAILS_verification_bundles_fresh(
        tmp_path, monkeypatch):
    """The bundle is there and covers the right HEAD, but it is CORRUPT. Skipping on it
    would hand the heartbeat a green light backed by an unusable file."""
    _seed(tmp_path)
    st, f = _run(tmp_path, monkeypatch, facts_fn=_facts(), head_fn=_head_fn(CUR_HEAD),
                 verify_fn=Recorder(values=[
                     (False, "git bundle verify exited 1: corrupt"),   # the reuse probe
                     (True, "okay + full history"),                    # the fresh local
                     (True, "okay + full history")]))                  # the fresh drive
    assert st["state"] == rb.STATE_VERIFIED_NEW
    assert f["create_fn"].calls == 1
    assert "FAILED verification" in st["reuse_note"]
    assert "a skip here would report a backup that is not there" in st["reuse_note"]
    assert "NO NEW BUNDLE" not in st["proves"]


def test_head_moved_bundles_fresh(tmp_path, monkeypatch):
    """The ordinary case: new commits since the last bundle, so there is new history to
    back up. The existing bundle records b50e78b; the repo is at e820aab."""
    _seed(tmp_path)
    st, f = _run(tmp_path, monkeypatch, facts_fn=_facts(CUR_HEAD),
                 head_fn=_head_fn(OLD_HEAD))
    assert st["state"] == rb.STATE_VERIFIED_NEW
    assert f["create_fn"].calls == 1
    assert f["copy_fn"].calls == 1
    assert "HEAD has MOVED" in st["reuse_note"]
    assert st["bundle_name"] == "tradingdesk-repo-20260716-120000.bundle"   # a NEW one
    assert "NO NEW BUNDLE" not in st["proves"]


def test_force_bundles_even_when_head_is_unchanged(tmp_path, monkeypatch):
    _seed(tmp_path)
    st, f = _run(tmp_path, monkeypatch, facts_fn=_facts(), head_fn=_head_fn(CUR_HEAD),
                 force=True)
    assert st["state"] == rb.STATE_VERIFIED_NEW
    assert st["forced"] is True
    assert f["create_fn"].calls == 1
    assert "--force" in st["reuse_note"]
    assert "NO NEW BUNDLE" not in st["proves"]
    # --force bypasses the SKIP. It must not buy a weaker result than any other run.
    assert st["ok"] is True
    assert st["proves"] == rb.PROVES_CLOUD_VERIFIED


def test_force_cannot_turn_a_failed_backup_into_a_pass(tmp_path, monkeypatch):
    _seed(tmp_path)
    st, f = _run(tmp_path, monkeypatch, facts_fn=_facts(), head_fn=_head_fn(CUR_HEAD),
                 force=True, verify_fn=Recorder((False, "corrupt")))
    assert st["ok"] is False
    assert st["state"] == rb.STATE_FAILED
    assert f["heartbeat_fn"].calls == 0
    assert st["proves"] == rb.PROVES_FAILED_RUN


def test_a_skipped_run_still_runs_the_cloud_check_against_the_bundle_it_leans_on(
        tmp_path, monkeypatch):
    """Otherwise flipping CLOUD_VERIFY_REQUIRED to True would silently not apply to the
    majority of runs — the requirement would look enforced and mostly not be."""
    _seed(tmp_path)
    st, f = _run(tmp_path, monkeypatch, facts_fn=_facts(), head_fn=_head_fn(CUR_HEAD),
                 cloud_fn=Recorder(_cloud("verified")))
    assert f["cloud_fn"].calls == 1
    assert f["cloud_fn"].args[0][0][1] == EXISTING        # the EXISTING bundle's name
    assert st["proves"] == rb.proves_skipped(EXISTING, rb.PROVES_CLOUD_VERIFIED)
    assert "NO NEW BUNDLE WAS CREATED BY THIS RUN" in st["proves"]
    assert "confirmed present in Google's cloud" in st["proves"]


def test_a_skipped_run_fails_closed_when_the_cloud_check_is_required(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setattr(rb, "CLOUD_VERIFY_REQUIRED", True)
    st, f = _run(tmp_path, monkeypatch, facts_fn=_facts(), head_fn=_head_fn(CUR_HEAD),
                 cloud_fn=Recorder(_cloud("failed", note="MD5 MISMATCH")))
    assert st["ok"] is False
    assert st["state"] == rb.STATE_FAILED
    assert f["heartbeat_fn"].calls == 0
    assert f["prune_fn"].calls == 0
    assert st["proves"] == rb.PROVES_FAILED_RUN


# --------------------------------------------------------------------------- #
# find_reusable_bundle() — the decision matrix, at the unit seam
# --------------------------------------------------------------------------- #
def _reuse(**over):
    """Drive find_reusable_bundle with every collaborator faked. -> (info|None, why).

    Defaults are the ALL-CLEAR case, so each test below flips exactly ONE condition
    and asserts it alone is enough to force a fresh bundle.
    """
    head_sha = over.pop("head_sha", CUR_HEAD)
    kw = {
        "verify_fn": lambda p: (True, "okay + full history"),
        "head_fn": lambda p: (CUR_HEAD, f"bundle records HEAD {CUR_HEAD}"),
        "list_fn": lambda d: [EXISTING],
        "exists_fn": lambda p: True,
        "log_fn": lambda m: None,
    }
    kw.update(over)
    return rb.find_reusable_bundle(r"C:\local", r"X:\My Drive\TradingDesk-Backups",
                                   head_sha, **kw)


def test_reuse_all_clear_returns_the_bundle_and_both_verifications():
    info, why = _reuse()
    assert info["name"] == EXISTING
    assert info["verify_local"] == "okay + full history"
    assert info["verify_drive"] == "okay + full history"
    assert "UNCHANGED" in why


def test_reuse_refuses_when_the_repo_head_is_unknown():
    """repo_facts() never raises — it returns head_sha=None when git is unhappy. An
    unknown HEAD cannot be 'unchanged'; it can only be unknown."""
    info, why = _reuse(head_sha=None)
    assert info is None
    assert "could not be read" in why
    assert "rather than skipping on a guess" in why


def test_reuse_refuses_when_the_directory_cannot_be_listed():
    def boom(d):
        raise OSError("access denied")
    info, why = _reuse(list_fn=boom)
    assert info is None
    assert "bundling fresh" in why


def test_reuse_ignores_files_that_are_not_bundles_this_job_made():
    """Same strict BUNDLE_RE as retention. The hand-made rescue bundle is not ours and
    must not be leaned on — we did not make it and cannot vouch for its provenance."""
    info, why = _reuse(list_fn=lambda d: ["tradingdesk-full-20260716.bundle", "notes.txt"])
    assert info is None
    assert "no bundle this job created" in why


def test_reuse_picks_the_newest_by_filename_not_by_mtime():
    """Lexical == chronological by construction, and a Drive copy's mtime is whatever
    the filesystem felt like — the same reasoning retention uses."""
    info, why = _reuse(list_fn=lambda d: [
        "tradingdesk-repo-20260716-144429.bundle",
        "tradingdesk-repo-20260716-151950.bundle",     # newest
        "tradingdesk-repo-20260716-145240.bundle"])
    assert info["name"] == "tradingdesk-repo-20260716-151950.bundle"


def test_reuse_refuses_when_the_local_file_vanished_between_list_and_use():
    info, why = _reuse(exists_fn=lambda p: False)
    assert info is None
    assert "MISSING" in why


def test_reuse_refuses_when_the_bundles_head_cannot_be_read():
    """A bundle we cannot interrogate is a bundle we cannot vouch for."""
    info, why = _reuse(head_fn=lambda p: (None, "the bundle records no HEAD ref"))
    assert info is None
    assert "could not read the HEAD recorded inside" in why


def test_reuse_refuses_when_head_has_moved():
    info, why = _reuse(head_fn=lambda p: (OLD_HEAD, "records HEAD"))
    assert info is None
    assert "HEAD has MOVED" in why
    assert OLD_HEAD[:12] in why and CUR_HEAD[:12] in why


def test_reuse_refuses_when_the_drive_copy_records_a_different_head():
    """Same NAME is not same FILE. The Drive folder is the thing that has already lied
    to us once; a matching filename over there proves nothing on its own."""
    def head_fn(p):
        sha = OLD_HEAD if "My Drive" in str(p) else CUR_HEAD
        return sha, f"bundle records HEAD {sha}"
    info, why = _reuse(head_fn=head_fn)
    assert info is None
    assert "does not record the same HEAD" in why
    assert "same name is not the same file" in why


def test_reuse_refuses_when_the_local_bundle_fails_verification():
    info, why = _reuse(verify_fn=lambda p: (False, "git bundle verify exited 1: corrupt"))
    assert info is None
    assert "FAILED verification" in why


def test_reuse_refuses_when_only_the_drive_copy_fails_verification():
    calls = {"n": 0}

    def verify_fn(p):
        calls["n"] += 1
        return (True, "okay + full history") if calls["n"] == 1 else (False, "corrupt")
    info, why = _reuse(verify_fn=verify_fn)
    assert info is None
    assert "Drive copy" in why and "FAILED verification" in why


def test_reuse_checks_the_cheap_things_before_scanning_41MB_twice():
    """Ordering, pinned: the HEAD reads parse a header, the verifies scan the whole
    bundle. A moved HEAD must not cost two full scans on every single wrap."""
    verify = Recorder((True, "okay + full history"))
    info, _ = _reuse(head_fn=lambda p: (OLD_HEAD, "records HEAD"), verify_fn=verify)
    assert info is None
    assert verify.calls == 0


# --------------------------------------------------------------------------- #
# bundle_head_sha() — asks the BUNDLE, never a status file
# --------------------------------------------------------------------------- #
def test_bundle_head_sha_parses_real_list_heads_output():
    """Verbatim `git bundle list-heads` output from a real bundle on this machine
    (2026-07-16). A bundle made with --all carries an explicit HEAD line."""
    real = (f"{OLD_HEAD} refs/heads/main\n"
            f"06dc23337b9316cdc7373db18c0074abc6842511 refs/stash\n"
            f"{OLD_HEAD} HEAD\n")
    sha, note = rb.bundle_head_sha("x.bundle", run_fn=lambda: _proc(0, stdout=real))
    assert sha == OLD_HEAD
    assert OLD_HEAD in note


def test_bundle_head_sha_returns_None_when_git_fails():
    sha, note = rb.bundle_head_sha(
        "x.bundle", run_fn=lambda: _proc(1, stderr="error: not a bundle"))
    assert sha is None
    assert "exited 1" in note


def test_bundle_head_sha_returns_None_when_there_is_no_HEAD_line():
    """A bundle without a HEAD ref cannot tell us which HEAD it covers, so it can never
    justify a skip — no guessing from refs/heads/main."""
    sha, note = rb.bundle_head_sha(
        "x.bundle", run_fn=lambda: _proc(0, stdout=f"{OLD_HEAD} refs/heads/main\n"))
    assert sha is None
    assert "no HEAD ref" in note


def test_bundle_head_sha_survives_raising_git():
    sha, note = rb.bundle_head_sha("x.bundle", run_fn=_raise)
    assert sha is None
    assert "raised" in note


# --------------------------------------------------------------------------- #
# CLOUD ARRIVAL — the check that asks GOOGLE, not the filesystem
# --------------------------------------------------------------------------- #
# Every test here is OFFLINE: the HTTP layer is faked at urlopen (for the request
# construction / error classification tests) or at the collaborator seam (for the
# policy tests), exactly as the rest of this file fakes git and Drive.
#
# The assertion that carries this section is on the `proves` STRING, not just the
# exit code. A run can exit 0 honestly ("I did not check the cloud") or dishonestly
# ("verified!"), and only the string tells them apart — an overstated `proves` is
# the entire bug class this check was built to kill.
BUNDLE = "tradingdesk-repo-20260716-120000.bundle"
LOCAL_MD5 = "9f86d081884c7d659a2feaa0c55ad015"
CLOUD_UTC = dt.datetime(2026, 7, 16, 12, 0, tzinfo=dt.timezone.utc)

# The Drive-side coordinates. FOLDER_ID is a stand-in, never a pin: the production code
# RESOLVES the id from the destination path every run (see the folder-resolution tests
# below), because an id names one folder object and Drive recreating its own folders is
# the disease this module exists for.
FOLDER_ID = "0folderIDresolved"
DRIVE_MOUNT = r"X:\My Drive"
DRIVE_DEST = r"X:\My Drive\TradingDesk-Backups"

# obtained_utc is exactly 7 days before CLOUD_UTC — the fingerprint of the Testing-mode
# trap, so the rejection test below exercises the age the note is meant to call out.
CREDS = {"client_id": "cid", "client_secret": "SECRET-csec",
         "refresh_token": "SECRET-rtok",
         "obtained_utc": "2026-07-09T12:00:00+00:00",
         "publishing_status": "in_production"}

DRIVE_FILE = {"id": "1abcFILEID", "name": BUNDLE, "size": "41602526",
              "md5Checksum": LOCAL_MD5}


class _Resp:
    """Minimal stand-in for what urlopen() hands back (a JSON body + context mgr)."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, payload):
    """A real urllib HTTPError with a readable JSON body — what Google actually raises."""
    body = io.BytesIO(json.dumps(payload).encode("utf-8"))
    return urllib.error.HTTPError("https://oauth2.googleapis.com/token", code,
                                  "err", {}, body)


def _verify(**over):
    """Drive verify_cloud_arrival with every collaborator faked. -> the cloud dict.

    deadline_s defaults to 0 so the poll makes exactly one attempt and never sleeps —
    tests that care about polling pass their own clock.

    `find_fn` is the PARENT-SCOPED NAME QUERY: it is handed (name, folder_id, token)
    and returns whatever Drive says is in THAT FOLDER under THAT NAME. The scoping is
    the part that matters — an unscoped "does this name exist anywhere" would have
    been true throughout the wrong-folder incident.
    """
    kw = {
        "creds_fn": lambda: (dict(CREDS), "ok", "loaded"),
        "token_fn": lambda c: ("at-token", None, "access token refreshed"),
        "folder_fn": lambda t: (FOLDER_ID, f"resolved to {FOLDER_ID}"),
        "find_fn": lambda n, fid, t: ([DRIVE_FILE], "Drive returned 1 file(s)"),
        "md5_fn": lambda p: LOCAL_MD5,
        "sleep_fn": Recorder(None),
        "deadline_s": 0,
        "now": CLOUD_UTC,
    }
    kw.update(over)
    return rb.verify_cloud_arrival("C:\\fake\\backups\\" + BUNDLE, BUNDLE,
                                   DRIVE_DEST, DRIVE_MOUNT, **kw)


def test_cloud_md5_match_is_verified():
    """The happy path: Drive's md5 equals the local bundle's md5, so the bytes in
    Google's datacenter ARE the bytes on disk. That is the whole claim."""
    c = _verify()
    assert c["state"] == "verified"
    assert c["checked"] is True
    assert c["folder_id"] == FOLDER_ID
    assert c["file_id"] == "1abcFILEID"
    assert c["cloud_md5"] == LOCAL_MD5
    assert c["local_md5"] == LOCAL_MD5
    assert "CONFIRMED IN THE CLOUD" in c["note"]


def test_cloud_md5_mismatch_is_never_reported_as_success():
    """THE CORE REGRESSION FOR THIS CHECK. A file with the right NAME and SIZE but
    the wrong CONTENT is exactly what a name+size check would have blessed. md5 is
    the only version worth building, so a mismatch must never read as arrival."""
    wrong = dict(DRIVE_FILE, md5Checksum="00000000000000000000000000000000")
    c = _verify(find_fn=lambda n, fid, t: ([wrong], "1 file"))
    assert c["state"] == "failed"
    assert c["state"] != "verified"
    assert "MD5 MISMATCH" in c["note"]
    assert "NOT proven" in c["note"]
    assert c["cloud_md5"] == "00000000000000000000000000000000"
    assert c["local_md5"] == LOCAL_MD5


def test_cloud_file_absent_from_the_cloud_is_a_failure():
    """The bundle is on the Drive volume but Google does not have it in the backup
    folder — this IS the 9-day silent failure, caught. 'Not there' must never be
    shrugged off.

    Note the claim is bounded to what was asked: Drive ANSWERED for that folder and
    reported no such file. Whether that means 'never uploaded' or 'the API lag
    outlasted the deadline' is exactly what we do not get to assume."""
    c = _verify(find_fn=lambda n, fid, t: ([], "Drive returned 0 file(s)"))
    assert c["state"] == "failed"
    assert "NOT CONFIRMED IN THE CLOUD" in c["note"]
    assert "we do not get to assume which" in c["note"]
    assert "ABSENT from Drive's answer for that folder" in c["note"]


def test_cloud_file_present_without_a_checksum_is_not_success():
    """Drive can publish the file entry before the content finishes uploading. No
    md5 means nothing is proven yet — it must not be mistaken for a match.

    This is why a metadata hit ALONE is not proof and md5 remains the real one: Drive
    computes md5 server-side and only once the content lands, so its absence is the
    difference between 'a metadata row exists' and 'the bytes uploaded'."""
    c = _verify(find_fn=lambda n, fid, t: ([{"id": "x", "name": BUNDLE}], "1 file"))
    assert c["state"] == "failed"
    assert c["state"] != "verified"
    assert c["file_id"] == "x"
    assert c["cloud_md5"] is None
    assert "still in flight" in c["note"]
    assert "CONTENT HAS NOT LANDED" in c["note"]


def test_cloud_refresh_token_rejected_names_the_7_day_testing_trap():
    """THE PREDICTED FAILURE. An OAuth app left in 'Testing' issues refresh tokens
    that die after 7 days. When that lands, the note must SAY SO — a generic 'auth
    failed' would send someone hunting the wrong thing for an afternoon."""
    c = _verify(token_fn=lambda c_: (None, "invalid_grant",
                                     "HTTP 400 (invalid_grant: Token has been "
                                     "expired or revoked.)"))
    assert c["state"] == "failed"
    assert "invalid_grant" in c["note"]
    assert "EXPIRE AFTER 7 DAYS" in c["note"]
    assert "In production" in c["note"]
    assert "CLOUD ARRIVAL IS NOT BEING PROVEN" in c["note"]
    # The credential stamps its own consent date, so the note can date the failure:
    # 2026-07-09 -> 2026-07-16 is 7 days, i.e. the fingerprint of the trap.
    assert "7 day(s) ago" in c["note"]


def test_cloud_token_failure_flags_a_credential_minted_from_an_unpublished_app():
    """drive_oauth_consent.py records the answer to 'is it published?'. If it was
    not, that answer is the first thing a reader should see on a rejection."""
    creds = dict(CREDS, publishing_status="testing_or_unknown")
    c = _verify(creds_fn=lambda: (creds, "ok", "loaded"),
                token_fn=lambda c_: (None, "invalid_grant", "HTTP 400 (invalid_grant)"))
    assert "NOT confirmed published" in c["note"]


def test_cloud_network_error_is_a_failure_not_a_pass():
    c = _verify(token_fn=lambda c_: (None, None, "network error contacting "
                                     "https://oauth2.googleapis.com/token (timeout)"))
    assert c["state"] == "failed"
    assert "network error" in c["note"]
    assert "could not obtain a Drive access token" in c["note"]


def test_cloud_query_failure_is_a_failure():
    """'We could not ask Google' is not 'Google says it is not there'."""
    c = _verify(find_fn=lambda n, fid, t: (None, "HTTP 403 from the Drive API "
                                                 "(insufficient scope)"))
    assert c["state"] == "failed"
    assert "Drive API query FAILED" in c["note"]
    assert "403" in c["note"]


def test_cloud_no_credential_is_skipped_not_configured():
    """No credential is an honest 'not set up yet', NOT a failure — and NOT a pass."""
    c = _verify(creds_fn=lambda: (None, "absent", "no Drive API credential at X — run "
                                                  "drive_oauth_consent.py"))
    assert c["state"] == "skipped_not_configured"
    assert c["checked"] is False
    assert "NOT checked" in c["note"]


def test_cloud_broken_credential_is_failed_not_skipped():
    """A credential that EXISTS but is corrupt must not launder itself into 'not
    configured yet' — that would silently disable the check forever."""
    c = _verify(creds_fn=lambda: (None, "bad", "not valid JSON"))
    assert c["state"] == "failed"
    assert c["checked"] is True
    assert "UNUSABLE" in c["note"]
    assert "not an un-configured check" in c["note"]


def test_cloud_poll_waits_while_the_upload_is_still_in_flight():
    """DriveFS uploads asynchronously AND Drive's API lags the local write by ~1-5
    minutes (measured 2026-07-16, both bundles, regardless of query type), so the first
    query legitimately misses. The poll must tolerate that (or it would cry wolf
    nightly) while staying bounded. That lag is the reason this poll exists."""
    ticks = {"t": 0}

    def clock():
        ticks["t"] += 1          # 1s per call — nowhere near the 300s deadline
        return ticks["t"]

    find = Recorder(values=[([], "0 file(s)"), ([DRIVE_FILE], "1 file(s)")])
    sleep = Recorder(None)
    c = _verify(find_fn=find, sleep_fn=sleep, monotonic_fn=clock, deadline_s=300)
    assert c["state"] == "verified"
    assert find.calls == 2
    assert sleep.calls == 1
    assert "after 2 query attempt(s)" in c["note"]


def test_cloud_poll_is_bounded_and_gives_up_honestly():
    """...and the tolerance above must not become indefinite patience."""
    sleep = Recorder(None)
    c = _verify(find_fn=lambda n, fid, t: ([], "0 file(s)"), sleep_fn=sleep,
                deadline_s=0)
    assert c["state"] == "failed"
    assert sleep.calls == 0
    assert "within 0s" in c["note"]


def test_cloud_multiple_same_named_files_are_disclosed():
    """Drive permits same-named siblings inside one folder, so even a scoped query can
    return more than one hit. Checking the first is fine; hiding that fact is not."""
    c = _verify(find_fn=lambda n, fid, t: ([DRIVE_FILE, dict(DRIVE_FILE, id="dupe")],
                                           "2"))
    assert c["state"] == "verified"
    assert "2 files in the folder share this name" in c["note"]


def test_cloud_asks_for_the_bundle_scoped_to_the_RESOLVED_folder():
    """THE SHAPE OF THE LOOKUP, at the seam: the check hands the query BOTH the bundle
    name AND the folder id it resolved — never the name alone. 'A file with this name
    and md5 exists somewhere in the Drive' would have been TRUE throughout the 9-day
    wrong-folder incident, so folder identity is the thing being verified."""
    seen = []

    def find_fn(name, folder_id, token):
        seen.append((name, folder_id, token))
        return [DRIVE_FILE], "1 file(s)"

    c = _verify(find_fn=find_fn)
    assert c["state"] == "verified"
    assert seen == [(BUNDLE, FOLDER_ID, "at-token")]


def test_cloud_md5_absent_then_present_on_a_later_poll_is_verified():
    """THE CONTENT-STILL-LANDING CASE. Drive registers the metadata row before the
    bytes finish, and computes md5 server-side only once they have. So 'no md5 yet' is
    a KEEP POLLING condition — not success (nothing is proven), not failure (nothing
    is wrong). md5 is what makes the poll wait on the UPLOAD rather than on the mere
    appearance of a row."""
    ticks = {"t": 0}

    def clock():
        ticks["t"] += 1          # 1s per call — nowhere near the 300s deadline
        return ticks["t"]

    registered = {"id": "1abcFILEID", "name": BUNDLE}        # no md5Checksum yet
    find = Recorder(values=[([registered], "1 file(s)"),
                            ([registered], "1 file(s)"),
                            ([DRIVE_FILE], "1 file(s)")])    # content landed
    sleep = Recorder(None)
    c = _verify(find_fn=find, sleep_fn=sleep, monotonic_fn=clock, deadline_s=300)
    assert c["state"] == "verified"
    assert find.calls == 3
    assert sleep.calls == 2
    assert c["cloud_md5"] == LOCAL_MD5
    assert "after 3 query attempt(s)" in c["note"]


def test_cloud_md5_absent_for_the_whole_window_says_REGISTERED_BUT_NOT_LANDED():
    """...and when it never lands, the note must say WHICH failure this is. 'The row
    exists but the content never arrived' and 'the file is not there at all' point at
    different problems; collapsing them wastes the reader's night."""
    registered = {"id": "1abcFILEID", "name": BUNDLE}        # never gains an md5
    c = _verify(find_fn=lambda n, fid, t: ([registered], "1 file(s)"), deadline_s=0)
    assert c["state"] == "failed"
    assert c["state"] != "verified"
    assert c["file_id"] == "1abcFILEID"
    assert c["cloud_md5"] is None
    assert "NOT CONFIRMED IN THE CLOUD" in c["note"]
    assert "REGISTERED" in c["note"]
    assert "CONTENT HAS NOT LANDED" in c["note"]
    # ...and must NOT claim the file was missing, because it was not.
    assert "ABSENT from Drive's answer" not in c["note"]


def test_cloud_absent_and_not_landed_timeouts_do_not_wear_each_others_wording():
    """The mirror of the test above: a genuinely absent bundle must not be described
    as a registered-but-unlanded one."""
    c = _verify(find_fn=lambda n, fid, t: ([], "0 file(s)"), deadline_s=0)
    assert c["state"] == "failed"
    assert "ABSENT from Drive's answer for that folder" in c["note"]
    assert "REGISTERED" not in c["note"]
    assert c["file_id"] is None


def test_cloud_unresolvable_folder_is_a_distinct_failure_and_never_falls_back():
    """If the folder id will not resolve, the check FAILS AND SAYS SO. It must never
    quietly fall back to an UNSCOPED name query: 'a file with this name and md5 exists
    somewhere in the Drive' would have been TRUE for all 9 days of the wrong-folder
    incident, so that fallback answers a different — and worthless — question."""
    find = Recorder(([DRIVE_FILE], "1 file(s)"))
    c = _verify(folder_fn=lambda t: (None, "no folder named 'TradingDesk-Backups' "
                                           "exists under My Drive in Drive"),
                find_fn=find)
    assert c["state"] == "failed"
    assert c["folder_id"] is None
    assert "could NOT resolve the Drive folder id" in c["note"]
    assert "Refusing to fall back to an UNSCOPED name query" in c["note"]
    assert "no folder named 'TradingDesk-Backups'" in c["note"]   # the REAL reason
    # The honest distinction: this is not the bundle being missing.
    assert "not an absent bundle" in c["note"]
    assert find.calls == 0, "a query was still attempted after resolution failed"


def test_cloud_folder_resolution_happens_once_not_once_per_poll():
    """An unresolvable folder does not become resolvable by waiting, and re-walking it
    every 15s is just a slower way of reporting the same failure."""
    folder = Recorder((FOLDER_ID, "resolved"))
    ticks = {"t": 0}

    def clock():
        ticks["t"] += 1
        return ticks["t"]

    find = Recorder(values=[([], "0 file(s)"), ([DRIVE_FILE], "1 file(s)")])
    c = _verify(folder_fn=folder, find_fn=find, monotonic_fn=clock, deadline_s=300)
    assert c["state"] == "verified"
    assert find.calls == 2
    assert folder.calls == 1


# --------------------------------------------------------------------------- #
# Resolving the backup folder's Drive id — walked from the path, never pinned
# --------------------------------------------------------------------------- #
def _folder(name, fid):
    return {"id": fid, "name": name, "mimeType": rb.DRIVE_FOLDER_MIME}


def test_the_observed_folder_id_is_never_hardcoded_in_the_source():
    """The live id was measured on this machine 2026-07-16. Pinning it would be a
    landmine: an id names one folder OBJECT, and Drive moving/recreating its own
    folders is the disease this module exists for. A pinned id survives that by
    pointing at a ghost that will never receive another bundle — while the check
    reports a clean, confident miss. The PATH survives; the id does not."""
    src = Path(rb.__file__).read_text(encoding="utf-8")
    assert "1ppfS44BaR25_TnD8WT6g8vqwCAjKbTGA" not in src


def test_folder_id_is_resolved_by_listing_from_the_My_Drive_root():
    """'root' is Drive's alias for the My Drive root. The walk enumerates each level and
    matches on name AND mimeType — not because a listing is fresher than a name query
    (it is not; see _drive_find_in_folder), but because the walk has to SEE each
    candidate to reject a same-named file and to notice ambiguous siblings."""
    seen = []

    def list_fn(fid, tok):
        seen.append(fid)
        return ([_folder("TradingDesk-Backups", FOLDER_ID),
                 _folder("Some Other Folder", "zzz"),
                 {"id": "f", "name": "a-loose-file.txt"}], "listed")

    fid, note = rb._drive_resolve_folder_id(["TradingDesk-Backups"], "at-token",
                                            list_fn=list_fn)
    assert fid == FOLDER_ID
    assert seen == ["root"]
    assert "My Drive/TradingDesk-Backups" in note


def test_folder_id_resolution_walks_every_component():
    def list_fn(fid, tok):
        if fid == "root":
            return ([_folder("Nested", "n1")], "listed")
        if fid == "n1":
            return ([_folder("TradingDesk-Backups", FOLDER_ID)], "listed")
        return ([], "listed")

    fid, note = rb._drive_resolve_folder_id(["Nested", "TradingDesk-Backups"],
                                            "at-token", list_fn=list_fn)
    assert fid == FOLDER_ID
    assert "My Drive/Nested/TradingDesk-Backups" in note


def test_folder_id_resolution_refuses_a_FILE_wearing_the_folder_name():
    """mimeType is checked: a same-named FILE is not the folder, and listing a file's
    'children' would return nothing — i.e. a manufactured 'the bundle is absent'."""
    def list_fn(fid, tok):
        return ([{"id": "notafolder", "name": "TradingDesk-Backups",
                  "mimeType": "application/octet-stream"}], "listed")

    fid, note = rb._drive_resolve_folder_id(["TradingDesk-Backups"], "at-token",
                                            list_fn=list_fn)
    assert fid is None
    assert "no folder named 'TradingDesk-Backups'" in note


def test_folder_id_resolution_refuses_ambiguous_same_named_folders():
    """Drive permits same-named siblings. A coin-flip between two candidates is not a
    resolution — and picking the empty one would page for a healthy backup."""
    def list_fn(fid, tok):
        return ([_folder("TradingDesk-Backups", "one"),
                 _folder("TradingDesk-Backups", "two")], "listed")

    fid, note = rb._drive_resolve_folder_id(["TradingDesk-Backups"], "at-token",
                                            list_fn=list_fn)
    assert fid is None
    assert "2 folders named" in note
    assert "refusing to guess" in note


def test_folder_id_resolution_reports_a_listing_failure_as_a_failure():
    fid, note = rb._drive_resolve_folder_id(
        ["TradingDesk-Backups"], "at-token",
        list_fn=lambda f, t: (None, "HTTP 403 (insufficient scope)"))
    assert fid is None
    assert "could not list My Drive" in note
    assert "403" in note


def test_folder_id_resolution_rejects_a_folder_with_no_id():
    fid, note = rb._drive_resolve_folder_id(
        ["TradingDesk-Backups"], "at-token",
        list_fn=lambda f, t: ([{"name": "TradingDesk-Backups",
                                "mimeType": rb.DRIVE_FOLDER_MIME}], "listed"))
    assert fid is None
    assert "no id" in note


# --------------------------------------------------------------------------- #
# _drive_path_components() — the Drive-side path, DERIVED from what we wrote to
# --------------------------------------------------------------------------- #
def test_path_components_come_from_the_destination_resolve_already_computed():
    parts, note = rb._drive_path_components(DRIVE_DEST, DRIVE_MOUNT)
    assert parts == ["TradingDesk-Backups"]
    assert "TradingDesk-Backups" in note


def test_path_components_handle_a_nested_destination():
    parts, _ = rb._drive_path_components(r"X:\My Drive\Nested\TradingDesk-Backups",
                                         r"X:\My Drive")
    assert parts == ["Nested", "TradingDesk-Backups"]


def test_path_components_are_case_insensitive_about_the_mount_but_not_the_name():
    """Windows paths are case-insensitive; Drive folder NAMES are not. So the mount
    prefix may match loosely, but the component we go looking for must keep its
    original case or the client-side match will miss a folder that is right there."""
    parts, _ = rb._drive_path_components(r"x:\my drive\TradingDesk-Backups",
                                         r"X:\My Drive")
    assert parts == ["TradingDesk-Backups"]


def test_path_components_refuse_a_destination_outside_the_mount():
    parts, note = rb._drive_path_components(r"C:\Somewhere\Else", DRIVE_MOUNT)
    assert parts is None
    assert "not below the resolved mount root" in note


def test_path_components_refuse_when_the_mount_is_unknown():
    parts, note = rb._drive_path_components(DRIVE_DEST, None)
    assert parts is None
    assert "no Drive mount root" in note


def test_path_components_refuse_when_the_dest_is_the_mount_itself():
    parts, note = rb._drive_path_components(DRIVE_MOUNT, DRIVE_MOUNT)
    assert parts is None
    assert "no backup folder to resolve" in note


def test_path_components_refuse_when_there_is_no_destination_at_all():
    parts, note = rb._drive_path_components(None, DRIVE_MOUNT)
    assert parts is None
    assert "no Drive destination path" in note


# --------------------------------------------------------------------------- #
# md5 + credential loading + the raw HTTP layer
# --------------------------------------------------------------------------- #
def test_file_md5_streams_and_agrees_with_hashlib(tmp_path):
    """The bundle is ~41MB, so it is hashed in chunks — the chunking must not change
    the answer. Deliberately larger than one chunk."""
    blob = b"tradingdesk" * 100_000
    p = tmp_path / "b.bundle"
    p.write_bytes(blob)
    assert rb.file_md5(p, chunk=64 * 1024) == hashlib.md5(blob).hexdigest()


def test_load_creds_absent_points_at_the_consent_script():
    def missing(p):
        raise FileNotFoundError(p)
    creds, state, note = rb._load_drive_creds(r"C:\nope\drive_oauth.json",
                                              read_fn=missing)
    assert (creds, state) == (None, "absent")
    assert "drive_oauth_consent.py" in note


def test_load_creds_rejects_bad_json_and_missing_fields():
    _, state, _ = rb._load_drive_creds("x.json", read_fn=lambda p: "{not json")
    assert state == "bad"
    _, state, note = rb._load_drive_creds(
        "x.json", read_fn=lambda p: json.dumps({"client_id": "a"}))
    assert state == "bad"
    assert "refresh_token" in note and "client_secret" in note


def test_load_creds_accepts_a_complete_credential():
    creds, state, _ = rb._load_drive_creds("x.json", read_fn=lambda p: json.dumps(CREDS))
    assert state == "ok"
    assert creds["client_id"] == "cid"


def test_access_token_classifies_invalid_grant_and_never_echoes_the_secret():
    """invalid_grant is what an expired 7-day Testing token looks like on the wire.
    It must be classified, and the note must not leak the client_secret/refresh
    token that were in the request body."""
    def boom(req, timeout=None):
        raise _http_error(400, {"error": "invalid_grant",
                                "error_description": "Token has been expired or revoked."})

    tok, code, note = rb._drive_access_token(dict(CREDS), urlopen_fn=boom)
    assert tok is None
    assert code == "invalid_grant"
    assert "Token has been expired or revoked" in note
    assert "SECRET-csec" not in note and "SECRET-rtok" not in note


def test_access_token_returns_the_token_and_keeps_it_out_of_the_note():
    """The access token is in-memory only — it is never logged and never persisted."""
    tok, code, note = rb._drive_access_token(
        dict(CREDS), urlopen_fn=lambda req, timeout=None: _Resp(
            {"access_token": "ya29.SECRET-ACCESS"}))
    assert tok == "ya29.SECRET-ACCESS"
    assert code is None
    assert "ya29.SECRET-ACCESS" not in note


def test_access_token_survives_a_network_error():
    def boom(req, timeout=None):
        raise urllib.error.URLError("timed out")
    tok, code, note = rb._drive_access_token(dict(CREDS), urlopen_fn=boom)
    assert tok is None
    assert "network error" in note


def test_access_token_survives_a_non_json_error_body():
    def boom(req, timeout=None):
        raise urllib.error.HTTPError("https://oauth2.googleapis.com/token", 502,
                                     "Bad Gateway", {}, io.BytesIO(b"<html>nginx</html>"))
    tok, code, note = rb._drive_access_token(dict(CREDS), urlopen_fn=boom)
    assert tok is None
    assert code is None
    assert "HTTP 502" in note


def test_drive_find_in_folder_scopes_the_name_query_to_the_resolved_parent():
    """THE SHAPE OF THE LOOKUP, pinned at the wire. ONE query, carrying BOTH the
    bundle's name AND a parent membership term for the resolved folder id.

    THE PARENT SCOPING IS THE LOAD-BEARING PART. The 2026-07-07..16 incident was Drive
    syncing the WRONG FOLDER; a query asking only `name = '<bundle>'` would have been
    answered TRUE, happily, throughout all 9 days of it. Folder identity is the thing
    this check exists to verify, so an unscoped query is not an acceptable shape here
    even though the name half is back.

    (An earlier version listed the whole folder and matched client-side, on the claim
    that Drive's name index lagged while a parentId listing was immediately consistent.
    That mechanism was false — measured across two bundles, the API lags ~1-5 minutes
    regardless of query type, and on the second bundle the NAME query saw the file
    first. See conductor #33. The bounded poll, not the query shape, is what handles
    the lag.)

    `fields` must still name md5Checksum — the API omits it otherwise, and a
    silently-absent checksum is how a check ends up proving less than it looks."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = urllib.parse.unquote_plus(req.full_url)
        seen["auth"] = req.get_header("Authorization")
        return _Resp({"files": [DRIVE_FILE]})

    files, note = rb._drive_find_in_folder(BUNDLE, FOLDER_ID, "at-token",
                                           urlopen_fn=fake_urlopen)
    assert files == [DRIVE_FILE]
    # Both halves, in one q term: the name AND the folder it must live in.
    assert (f"name = '{BUNDLE}' and '{FOLDER_ID}' in parents and trashed = false"
            in seen["url"])
    assert f"'{FOLDER_ID}' in parents" in seen["url"], "the query is not parent-scoped"
    assert "md5Checksum" in seen["url"]
    assert "supportsAllDrives=true" in seen["url"]
    assert "includeItemsFromAllDrives=true" in seen["url"]
    assert seen["auth"] == "Bearer at-token"


def test_drive_find_in_folder_is_one_call_not_a_paginated_folder_walk():
    """The lookup is a single scoped question. The pagination that used to live here
    served the retracted 'the name index lags' claim; it bought complexity and a new
    failure mode in the repo's most safety-critical script for a benefit that does not
    exist."""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _Resp({"files": [DRIVE_FILE]})

    files, _ = rb._drive_find_in_folder(BUNDLE, FOLDER_ID, "at-token",
                                        urlopen_fn=fake_urlopen)
    assert files == [DRIVE_FILE]
    assert len(calls) == 1


def test_drive_find_in_folder_reports_a_query_failure_as_None_not_as_empty():
    """A failed query and an empty answer mean OPPOSITE things — 'we could not ask' vs
    'Google says it is not there'. Collapsing them would turn an outage into a false
    'the backup never uploaded' page (or worse, the reverse)."""
    def boom(req, timeout=None):
        raise urllib.error.HTTPError("https://www.googleapis.com/drive/v3/files", 403,
                                     "Forbidden", {}, io.BytesIO(b'{"error":"scope"}'))
    files, note = rb._drive_find_in_folder(BUNDLE, FOLDER_ID, "at-token",
                                           urlopen_fn=boom)
    assert files is None
    assert files != []
    assert "HTTP 403" in note


def test_drive_find_in_folder_treats_a_missing_files_key_as_a_failure():
    """A response we cannot read is not an answer of 'no'."""
    files, note = rb._drive_find_in_folder(
        BUNDLE, FOLDER_ID, "at-token",
        urlopen_fn=lambda req, timeout=None: _Resp({"kind": "drive#fileList"}))
    assert files is None
    assert "no `files` key" in note


def test_drive_find_in_folder_empty_answer_is_an_answer_not_a_failure():
    """Google said 'nothing by that name in that folder'. That is a real (bad) answer
    and must stay distinguishable from a query that never got through."""
    files, note = rb._drive_find_in_folder(
        BUNDLE, FOLDER_ID, "at-token",
        urlopen_fn=lambda req, timeout=None: _Resp({"files": []}))
    assert files == []
    assert files is not None
    assert "0 file(s)" in note


# --------------------------------------------------------------------------- #
# _drive_list_children() — kept, and used ONLY to resolve the backup folder's id
# --------------------------------------------------------------------------- #
# This lister is NOT part of the bundle lookup any more. It survives because
# _drive_resolve_folder_id walks 'My Drive' down to the backup folder by enumerating
# each level's children — which is what pins the check to the folder this run actually
# wrote to. A missed page in THAT walk reads as "no folder named TradingDesk-Backups
# exists", i.e. a confident, wrong resolution failure on a healthy Drive. So the
# pagination and the cap still earn their keep here, for the resolution walk.
def test_drive_list_children_follows_pagination_to_exhaustion():
    """A 'My Drive' root can hold far more than one page of folders, and the folder we
    need may be on any of them."""
    pages = [
        {"files": [_folder("Photos", "p")], "nextPageToken": "p2"},
        {"files": [_folder("Taxes", "t")], "nextPageToken": "p3"},
        {"files": [_folder("TradingDesk-Backups", FOLDER_ID)]},   # on the LAST page
    ]
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(urllib.parse.unquote_plus(req.full_url))
        return _Resp(pages[len(seen) - 1])

    files, note = rb._drive_list_children("root", "at-token", urlopen_fn=fake_urlopen)
    assert len(seen) == 3
    assert [f["id"] for f in files] == ["p", "t", FOLDER_ID]
    assert "pageToken=p2" in seen[1] and "pageToken=p3" in seen[2]
    assert "across 3 page(s)" in note
    assert "'root' in parents and trashed = false" in seen[0]


def test_drive_list_children_reports_a_query_failure_as_None_not_as_empty():
    """'We could not enumerate' must never collapse into 'the folder is not there'."""
    def boom(req, timeout=None):
        raise urllib.error.HTTPError("https://www.googleapis.com/drive/v3/files", 403,
                                     "Forbidden", {}, io.BytesIO(b'{"error":"scope"}'))
    files, note = rb._drive_list_children("root", "at-token", urlopen_fn=boom)
    assert files is None
    assert files != []
    assert "HTTP 403" in note


def test_drive_list_children_fails_a_mid_walk_page_rather_than_returning_a_short_list():
    """Page 1 lands, page 2 dies. Returning page 1 alone would be a TRUNCATED listing
    presented as the whole folder — i.e. a manufactured 'that folder does not exist'."""
    def fake_urlopen(req, timeout=None):
        if "pageToken" in req.full_url:
            raise urllib.error.URLError("connection reset")
        return _Resp({"files": [_folder("Photos", "p")], "nextPageToken": "p2"})

    files, note = rb._drive_list_children("root", "at-token", urlopen_fn=fake_urlopen)
    assert files is None
    assert "page 2" in note


def test_drive_list_children_caps_the_walk_and_calls_it_a_failure_not_an_answer():
    """A pathological/looping nextPageToken must not hang the job — and giving up must
    report a FAILURE TO ENUMERATE, never a short list that reads as an answer."""
    def fake_urlopen(req, timeout=None):
        return _Resp({"files": [_folder("Loop", "x")], "nextPageToken": "forever"})

    files, note = rb._drive_list_children("root", "at-token",
                                          urlopen_fn=fake_urlopen, max_pages=3)
    assert files is None
    assert "TRUNCATED" in note


# --------------------------------------------------------------------------- #
# THE ENABLEMENT FLAG — what the job does per outcome, and what `proves` admits
# --------------------------------------------------------------------------- #
def test_cloud_verify_required_is_enabled():
    """It shipped INERT (False) until the credential existed — wiring it fail-closed
    before GCP setup would have paged about a missing credential rather than a missing
    backup, teaching the 'that alarm is noise' reflex this job exists to defeat. As of
    2026-07-17 the credential is configured and a real --wrap run reported cloud state
    'verified', so per the flag's own instruction it is now True: the cloud-arrival
    check is enforced and a missing/mismatched cloud confirmation fails closed."""
    assert rb.CLOUD_VERIFY_REQUIRED is True


def test_the_credential_lives_outside_the_repo():
    """A refresh token in the repo would get committed; one under My Drive would get
    synced to the cloud. It belongs in the local secrets folder, beside .env."""
    p = str(rb.DRIVE_OAUTH_FILE)
    assert "TradingDesk-Local" in p and "secrets" in p
    assert "My Drive" not in p
    assert not p.lower().startswith("c:\\tradingdesk\\")


def test_consent_script_requests_the_metadata_scope_only():
    """drive.metadata.readonly is 'sensitive'; drive.readonly is 'restricted' — a
    materially higher verification bar, and it would hand a backup-checking script
    the ability to read every byte in the Drive. Metadata is all the check needs."""
    assert doc.SCOPE == "https://www.googleapis.com/auth/drive.metadata.readonly"


def test_no_credential_with_required_false_skips_and_proves_says_not_checked(
        tmp_path, monkeypatch):
    """THE SUBTLE ONE. The job still succeeds (exit 0, heartbeat fed) so tonight's
    run does not page — but the artifact must ADMIT the check did not happen. An
    un-run check that leaves no trace is how 'we have backups' becomes a belief."""
    monkeypatch.setattr(rb, "CLOUD_VERIFY_REQUIRED", False)
    # This test IS the no-credential scenario, so it constructs that state explicitly
    # (the harness default now represents a configured, verified cloud).
    st, f = _run(tmp_path, monkeypatch, cloud_fn=Recorder(_cloud("skipped_not_configured")))
    assert st["ok"] is True
    assert f["heartbeat_fn"].calls == 1
    assert st["cloud"]["state"] == "skipped_not_configured"
    assert st["proves"] == rb.PROVES_CLOUD_NOT_CHECKED
    assert "NOT checked" in st["proves"]
    assert "does NOT prove cloud arrival" in st["proves"]
    assert st["errors"] == []                    # not configured is not an error


def test_no_credential_with_required_true_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "CLOUD_VERIFY_REQUIRED", True)
    # Constructs the no-credential state explicitly; the harness default is now verified.
    st, f = _run(tmp_path, monkeypatch, cloud_fn=Recorder(_cloud("skipped_not_configured")))
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0          # cold heartbeat -> the alarm pages
    assert any("REQUIRED but no usable Drive API credential" in e for e in st["errors"])
    assert st["proves"] == rb.PROVES_FAILED_RUN


def test_cloud_verified_upgrades_the_proves_string(tmp_path, monkeypatch):
    st, f = _run(tmp_path, monkeypatch, cloud_fn=Recorder(
        _cloud("verified", note="CONFIRMED IN THE CLOUD: Drive file id 1abc has md5 ...",
               file_id="1abc", cloud_md5=LOCAL_MD5, local_md5=LOCAL_MD5)))
    assert st["ok"] is True
    assert f["heartbeat_fn"].calls == 1
    assert st["proves"] == rb.PROVES_CLOUD_VERIFIED
    assert "confirmed present in Google's cloud" in st["proves"]
    assert "md5 matching the local bundle byte-for-byte" in st["proves"]
    assert st["errors"] == []


def test_verified_cloud_state_reaches_the_heartbeat_text(tmp_path, monkeypatch):
    st, f = _run(tmp_path, monkeypatch, cloud_fn=Recorder(_cloud("verified")))
    text = f["heartbeat_fn"].args[0][0][0]
    assert "cloud=verified" in text
    assert "COMPLETE" not in text.upper()        # still must not trip assess()'s marker


def test_cloud_failure_with_required_false_downgrades_proves_but_does_not_fail(
        tmp_path, monkeypatch):
    """The grace period while Andrew confirms the setup works: a cloud failure is
    recorded LOUDLY and downgrades what the run claims, but does not fail the job —
    the local + Drive-volume bundle really did verify, and that is still a backup."""
    monkeypatch.setattr(rb, "CLOUD_VERIFY_REQUIRED", False)
    st, f = _run(tmp_path, monkeypatch, cloud_fn=Recorder(
        _cloud("failed", note="MD5 MISMATCH — Drive reports md5 0000...")))
    assert st["ok"] is True
    assert f["heartbeat_fn"].calls == 1
    assert st["proves"] == rb.PROVES_CLOUD_FAILED
    assert "RAN AND FAILED" in st["proves"]
    assert "does NOT prove cloud arrival" in st["proves"]
    # Loud in the artifact even though the job passed — never silent.
    assert any("cloud-arrival check FAILED" in e for e in st["errors"])
    assert any("MD5 MISMATCH" in e for e in st["errors"])


def test_cloud_failure_with_required_true_fails_closed_and_deletes_nothing(
        tmp_path, monkeypatch):
    """Fail-closed: non-zero exit, heartbeat cold, and retention never runs — we do
    not trade an old good bundle for a new one we cannot prove arrived."""
    monkeypatch.setattr(rb, "CLOUD_VERIFY_REQUIRED", True)
    st, f = _run(tmp_path, monkeypatch, cloud_fn=Recorder(
        _cloud("failed", note="MD5 MISMATCH — Drive reports md5 0000...")))
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert f["prune_fn"].calls == 0
    assert st["proves"] == rb.PROVES_FAILED_RUN
    assert any("cloud-arrival verification FAILED" in e for e in st["errors"])


def test_network_error_fails_closed_when_required(tmp_path, monkeypatch):
    """End-to-end through the REAL verify_cloud_arrival with only the transport
    faked: a network error must not be tolerated once the check is required."""
    monkeypatch.setattr(rb, "CLOUD_VERIFY_REQUIRED", True)

    def cloud_fn(path, name, dest=None, mount=None):
        return _verify(token_fn=lambda c: (None, None, "network error contacting "
                                           "https://oauth2.googleapis.com/token (timeout)"))

    st, f = _run(tmp_path, monkeypatch, cloud_fn=cloud_fn)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert "network error" in st["cloud"]["note"]
    assert st["proves"] == rb.PROVES_FAILED_RUN


def test_md5_mismatch_end_to_end_fails_closed_when_required(tmp_path, monkeypatch):
    """The mismatch, driven through run_backup rather than the unit seam: a bundle
    whose cloud copy differs byte-for-byte must never produce a green run."""
    monkeypatch.setattr(rb, "CLOUD_VERIFY_REQUIRED", True)

    def cloud_fn(path, name, dest=None, mount=None):
        return _verify(find_fn=lambda n, fid, t: (
            [dict(DRIVE_FILE, md5Checksum="dead" * 8)], "1 file"))

    st, f = _run(tmp_path, monkeypatch, cloud_fn=cloud_fn)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert st["cloud"]["state"] == "failed"
    assert "MD5 MISMATCH" in st["cloud"]["note"]
    assert st["proves"] == rb.PROVES_FAILED_RUN


def test_run_backup_hands_the_cloud_check_the_dest_it_actually_wrote_to(
        tmp_path, monkeypatch):
    """WIRING. The folder interrogated in the cloud must be the folder this run wrote
    to, so the destination + mount are handed DOWN from the resolve step rather than
    re-derived (let alone pinned) inside the check. resolve_drive_dest() is the one
    place allowed to decide what the destination is."""
    st, f = _run(tmp_path, monkeypatch)
    args = f["cloud_fn"].args[0][0]
    assert args[1] == st["bundle_name"]
    assert args[2] == str(tmp_path / "drive" / "My Drive" / "TradingDesk-Backups")
    assert args[3] == str(tmp_path / "drive" / "My Drive")
    # And the pair really does map onto a Drive-side path.
    parts, _ = rb._drive_path_components(args[2], args[3])
    assert parts == ["TradingDesk-Backups"]


def test_a_skipped_run_also_hands_over_the_dest(tmp_path, monkeypatch):
    """A skip runs the same cloud check, so it needs the same coordinates — otherwise
    flipping CLOUD_VERIFY_REQUIRED to True would fail every skip on a resolution
    error, which is a false page wearing a new hat."""
    _seed(tmp_path)
    st, f = _run(tmp_path, monkeypatch, facts_fn=_facts(), head_fn=_head_fn(CUR_HEAD))
    args = f["cloud_fn"].args[0][0]
    assert args[1] == EXISTING
    assert args[2] == str(tmp_path / "drive" / "My Drive" / "TradingDesk-Backups")
    assert args[3] == str(tmp_path / "drive" / "My Drive")


def test_unresolvable_folder_end_to_end_downgrades_proves_and_names_the_real_reason(
        tmp_path, monkeypatch):
    """Driven through run_backup with only the transport-level seam faked. While the
    check is not REQUIRED this stays a downgrade rather than a failure — the local +
    Drive-volume bundle really did verify, and that is still a backup — but `proves`
    must admit cloud arrival was not established, and `cloud.note` must say the reason
    was the FOLDER, not a missing bundle."""
    monkeypatch.setattr(rb, "CLOUD_VERIFY_REQUIRED", False)

    def cloud_fn(path, name, dest=None, mount=None):
        return _verify(folder_fn=lambda t: (
            None, "no folder named 'TradingDesk-Backups' exists under My Drive"))

    st, f = _run(tmp_path, monkeypatch, cloud_fn=cloud_fn)
    assert st["ok"] is True
    assert st["cloud"]["state"] == "failed"
    assert "could NOT resolve the Drive folder id" in st["cloud"]["note"]
    assert st["proves"] == rb.PROVES_CLOUD_FAILED
    assert "does NOT prove cloud arrival" in st["proves"]
    assert any("cloud-arrival check FAILED" in e for e in st["errors"])


def test_unresolvable_folder_fails_closed_once_the_check_is_required(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "CLOUD_VERIFY_REQUIRED", True)

    def cloud_fn(path, name, dest=None, mount=None):
        return _verify(folder_fn=lambda t: (None, "could not list My Drive — HTTP 403"))

    st, f = _run(tmp_path, monkeypatch, cloud_fn=cloud_fn)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert f["prune_fn"].calls == 0
    assert st["proves"] == rb.PROVES_FAILED_RUN


def test_content_not_landed_end_to_end_is_never_a_verified_run(tmp_path, monkeypatch):
    """The registered-but-unlanded timeout, through run_backup: a metadata row in the
    folder is NOT arrival, and must never reach `proves` as one."""
    monkeypatch.setattr(rb, "CLOUD_VERIFY_REQUIRED", True)

    def cloud_fn(path, name, dest=None, mount=None):
        return _verify(find_fn=lambda n, fid, t: ([{"id": "x", "name": BUNDLE}],
                                                  "1 file"))

    st, f = _run(tmp_path, monkeypatch, cloud_fn=cloud_fn)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert st["cloud"]["state"] == "failed"
    assert "CONTENT HAS NOT LANDED" in st["cloud"]["note"]
    assert st["proves"] == rb.PROVES_FAILED_RUN
    assert "confirmed present in Google's cloud" not in st["proves"]


def test_a_failed_run_never_carries_an_overstated_proves_string(tmp_path, monkeypatch):
    """`proves` starts at 'nothing' and is only ever RAISED by a check that passed.
    A run that died before bundling must not carry a string claiming it verified a
    bundle locally and on the Drive volume — which is what the old default said."""
    st, _ = _run(tmp_path, monkeypatch, resolve_fn=Recorder(_unresolved()))
    assert st["ok"] is False
    assert st["proves"] == rb.PROVES_FAILED_RUN
    assert "verified locally" not in st["proves"]
    assert st["proves"].startswith("nothing")


def test_every_proves_variant_disclaims_what_it_did_not_prove():
    """Pinned as a set: only the cloud-verified variant may claim cloud arrival, and
    every other variant must say out loud that it does not."""
    assert "cloud" in rb.PROVES_CLOUD_VERIFIED
    for s in (rb.PROVES_CLOUD_NOT_CHECKED, rb.PROVES_CLOUD_FAILED):
        assert "does NOT prove cloud arrival" in s
    assert "does NOT prove" not in rb.PROVES_CLOUD_VERIFIED
    assert rb.PROVES_FAILED_RUN.startswith("nothing")


def test_the_skipped_proves_variant_admits_it_created_nothing_and_names_the_bundle():
    """The skip's `proves` is COMPOSED (prefix + whichever cloud variant the run
    earned) rather than written out four times, so the cloud clause can never drift
    from what the cloud check actually said. Pinned across all three variants."""
    for cloud_variant in (rb.PROVES_CLOUD_VERIFIED, rb.PROVES_CLOUD_NOT_CHECKED,
                          rb.PROVES_CLOUD_FAILED):
        s = rb.proves_skipped(EXISTING, cloud_variant)
        assert s.startswith("NO NEW BUNDLE WAS CREATED BY THIS RUN")
        assert EXISTING in s                 # never anonymous about what covers HEAD
        assert cloud_variant in s            # the cloud clause survives composition
    # A skip may never UPGRADE what the cloud check said.
    assert "does NOT prove cloud arrival" in rb.proves_skipped(
        EXISTING, rb.PROVES_CLOUD_NOT_CHECKED)


# --------------------------------------------------------------------------- #
# --wrap — the interactive path CLAUDE.md's `wrap` force-word runs
# --------------------------------------------------------------------------- #
# run_backup is faked wholesale here: these tests are about main()'s OUTPUT and EXIT
# CODE, which is the entire surface --wrap adds. LOG_FILE is redirected in every one of
# them — main() calls log(), and a test that appends to the real
# C:\TradingDesk-Local\backups\repo_backup.log would be scribbling on the forensic
# record it exists to protect.
def _status(**over):
    """A status dict shaped exactly like run_backup returns one."""
    st = {"job": "repo_backup", "ok": True, "state": rb.STATE_VERIFIED_NEW,
          "bundle_name": BUNDLE, "head_sha": CUR_HEAD, "size_bytes": 41_602_526,
          "verify_local": "okay + full history", "verify_drive": "okay + full history",
          "drive_path": r"X:\My Drive\TradingDesk-Backups\\" + BUNDLE,
          "cloud": _cloud(), "errors": [], "drive_resolved": True,
          "proves": rb.PROVES_CLOUD_NOT_CHECKED}
    st.update(over)
    return st


def _main(monkeypatch, tmp_path, argv, st=None, raises=None):
    """Run main() with the given argv and a faked run_backup. -> (rc, stdout_lines)."""
    seen = {}
    monkeypatch.setattr(rb, "LOG_FILE", tmp_path / "repo_backup.log")
    monkeypatch.setattr(sys, "argv", ["repo_backup.py", *argv])

    def fake_run_backup(**k):
        seen.update(k)
        if raises:
            raise raises
        return st if st is not None else _status()

    monkeypatch.setattr(rb, "run_backup", fake_run_backup)
    return rb.main(), seen


def test_wrap_last_stdout_line_is_a_single_line_of_valid_json(tmp_path, monkeypatch, capsys):
    """THE CONTRACT WITH THE CALLING SESSION. It reports the wrap's outcome from this
    line rather than parsing the status file, so it must always be there, always be
    LAST, and always be one line."""
    rc, _ = _main(monkeypatch, tmp_path, ["--wrap"])
    out = capsys.readouterr().out.strip().splitlines()
    s = json.loads(out[-1])          # raises if the last line is not valid JSON
    assert rc == 0
    for k in ("state", "bundle_name", "head_sha", "size_bytes", "verify_local",
              "verify_drive", "drive_path"):
        assert k in s, f"--wrap summary is missing {k}"
    assert s["state"] == rb.STATE_VERIFIED_NEW
    assert s["bundle_name"] == BUNDLE
    assert s["head_sha"] == CUR_HEAD
    assert "\n" not in out[-1]


def test_wrap_summary_carries_proves_so_the_session_cannot_report_a_bare_ok(
        tmp_path, monkeypatch, capsys):
    """`ok` alone has never been the honest answer in this module. A session that says
    'backed up ✓' off a boolean has reinvented the silent green light."""
    rc, _ = _main(monkeypatch, tmp_path, ["--wrap"])
    s = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert s["proves"] == rb.PROVES_CLOUD_NOT_CHECKED
    assert "does NOT prove cloud arrival" in s["proves"]


def test_wrap_prints_human_readable_progress_before_the_json(tmp_path, monkeypatch, capsys):
    rc, _ = _main(monkeypatch, tmp_path, ["--wrap"])
    out = capsys.readouterr().out
    assert "repo backup (wrap)" in out
    assert "result :" in out and "verify :" in out and "proves :" in out


def test_wrap_exits_non_zero_on_an_unverified_result(tmp_path, monkeypatch, capsys):
    """Same fail-closed contract as the scheduled path. --wrap changes the output; it
    has no opinion about what counts as success."""
    rc, _ = _main(monkeypatch, tmp_path, ["--wrap"], st=_status(
        ok=False, state=rb.STATE_FAILED, proves=rb.PROVES_FAILED_RUN,
        errors=["LOCAL bundle failed verification — corrupt"]))
    assert rc == 1
    s = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert s["ok"] is False
    assert s["state"] == "failed"
    assert s["proves"].startswith("nothing")
    assert any("failed verification" in e for e in s["errors"])


def test_wrap_reports_a_crash_as_a_failure_rather_than_silence(tmp_path, monkeypatch, capsys):
    """If the job explodes, the session must still get a machine-readable answer.
    Guessing about backups is the entire problem this module exists to end."""
    rc, _ = _main(monkeypatch, tmp_path, ["--wrap"], raises=RuntimeError("git vanished"))
    assert rc == 2
    s = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert s["ok"] is False
    assert s["state"] == "failed"
    assert s["proves"] == rb.PROVES_FAILED_RUN
    assert any("git vanished" in e for e in s["errors"])


def test_wrap_reports_a_skipped_run_as_a_skip_not_as_a_fresh_backup(
        tmp_path, monkeypatch, capsys):
    rc, _ = _main(monkeypatch, tmp_path, ["--wrap"], st=_status(
        state=rb.STATE_SKIPPED_HEAD_UNCHANGED, bundle_name=EXISTING,
        proves=rb.proves_skipped(EXISTING, rb.PROVES_CLOUD_NOT_CHECKED)))
    out = capsys.readouterr().out
    assert rc == 0
    assert "NO NEW BUNDLE" in out
    s = json.loads(out.strip().splitlines()[-1])
    assert s["state"] == "skipped_head_unchanged"
    assert EXISTING in s["proves"]


def test_wrap_is_output_only_and_does_not_pass_force(tmp_path, monkeypatch, capsys):
    """--wrap must not quietly imply --force: a wrap that bundled a redundant twin
    every time would defeat the skip it was built alongside."""
    rc, seen = _main(monkeypatch, tmp_path, ["--wrap"])
    capsys.readouterr()
    assert seen["force"] is False
    assert seen["dry_run"] is False


def test_force_flag_reaches_run_backup(tmp_path, monkeypatch, capsys):
    rc, seen = _main(monkeypatch, tmp_path, ["--force"])
    capsys.readouterr()
    assert seen["force"] is True


def test_the_scheduled_path_stays_quiet_and_prints_no_json(tmp_path, monkeypatch, capsys):
    """Without --wrap nothing new is printed: RepoBackupDaily's stdout goes nowhere,
    and the machine-readable line is for an interactive caller that asked for it."""
    rc, _ = _main(monkeypatch, tmp_path, [])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == ""


def test_wrap_summary_of_a_dry_run_is_honest(tmp_path, monkeypatch, capsys):
    rc, _ = _main(monkeypatch, tmp_path, ["--wrap", "--dry-run"], st=_status(
        ok=False, state=rb.STATE_DRY_RUN, proves=rb.PROVES_FAILED_RUN,
        errors=["dry-run: created nothing, deleted nothing, heartbeat NOT refreshed"]))
    out = capsys.readouterr().out
    assert rc == 0                       # a dry run that resolved Drive is not a failure
    s = json.loads(out.strip().splitlines()[-1])
    assert s["ok"] is False              # ...but it is NEVER a successful backup
    assert s["state"] == "dry_run"
    assert "DRY RUN" in out


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
