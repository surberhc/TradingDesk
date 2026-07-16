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
import types
import urllib.error
import urllib.parse

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
         "file_id": None, "cloud_md5": None, "local_md5": None, "required": False,
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
        "cloud_fn": over.get("cloud_fn", Recorder(_cloud())),
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
    """
    kw = {
        "creds_fn": lambda: (dict(CREDS), "ok", "loaded"),
        "token_fn": lambda c: ("at-token", None, "access token refreshed"),
        "find_fn": lambda n, t: ([DRIVE_FILE], "Drive returned 1 file(s)"),
        "md5_fn": lambda p: LOCAL_MD5,
        "sleep_fn": Recorder(None),
        "deadline_s": 0,
        "now": CLOUD_UTC,
    }
    kw.update(over)
    return rb.verify_cloud_arrival("C:\\fake\\backups\\" + BUNDLE, BUNDLE, **kw)


def test_cloud_md5_match_is_verified():
    """The happy path: Drive's md5 equals the local bundle's md5, so the bytes in
    Google's datacenter ARE the bytes on disk. That is the whole claim."""
    c = _verify()
    assert c["state"] == "verified"
    assert c["checked"] is True
    assert c["file_id"] == "1abcFILEID"
    assert c["cloud_md5"] == LOCAL_MD5
    assert c["local_md5"] == LOCAL_MD5
    assert "CONFIRMED IN THE CLOUD" in c["note"]


def test_cloud_md5_mismatch_is_never_reported_as_success():
    """THE CORE REGRESSION FOR THIS CHECK. A file with the right NAME and SIZE but
    the wrong CONTENT is exactly what a name+size check would have blessed. md5 is
    the only version worth building, so a mismatch must never read as arrival."""
    wrong = dict(DRIVE_FILE, md5Checksum="00000000000000000000000000000000")
    c = _verify(find_fn=lambda n, t: ([wrong], "1 file"))
    assert c["state"] == "failed"
    assert c["state"] != "verified"
    assert "MD5 MISMATCH" in c["note"]
    assert "NOT proven" in c["note"]
    assert c["cloud_md5"] == "00000000000000000000000000000000"
    assert c["local_md5"] == LOCAL_MD5


def test_cloud_file_absent_from_the_cloud_is_a_failure():
    """The bundle is on the Drive volume but Google does not have it — this IS the
    9-day silent failure, caught. 'Not there' must never be shrugged off."""
    c = _verify(find_fn=lambda n, t: ([], "Drive returned 0 file(s)"))
    assert c["state"] == "failed"
    assert "NOT CONFIRMED IN THE CLOUD" in c["note"]
    assert "we do not get to assume which" in c["note"]


def test_cloud_file_present_without_a_checksum_is_not_success():
    """Drive can publish the file entry before the content finishes uploading. No
    md5 means nothing is proven yet — it must not be mistaken for a match."""
    c = _verify(find_fn=lambda n, t: ([{"id": "x", "name": BUNDLE}], "1 file"))
    assert c["state"] == "failed"
    assert c["file_id"] == "x"
    assert c["cloud_md5"] is None
    assert "still in flight" in c["note"]


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
    c = _verify(find_fn=lambda n, t: (None, "HTTP 403 from the Drive API "
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
    """DriveFS uploads asynchronously, so the first query legitimately misses. The
    poll must tolerate that (or it would cry wolf nightly) while staying bounded."""
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
    c = _verify(find_fn=lambda n, t: ([], "0 file(s)"), sleep_fn=sleep, deadline_s=0)
    assert c["state"] == "failed"
    assert sleep.calls == 0
    assert "within 0s" in c["note"]


def test_cloud_multiple_same_named_files_are_disclosed():
    c = _verify(find_fn=lambda n, t: ([DRIVE_FILE, dict(DRIVE_FILE, id="dupe")], "2"))
    assert c["state"] == "verified"
    assert "2 files share this name" in c["note"]


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


def test_drive_find_asks_for_md5_explicitly_and_parses_the_answer():
    """`fields` must name md5Checksum — the Drive API omits it otherwise, and a
    silently-absent checksum is how a check ends up proving less than it looks."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = urllib.parse.unquote_plus(req.full_url)
        seen["auth"] = req.get_header("Authorization")
        return _Resp({"files": [DRIVE_FILE]})

    files, note = rb._drive_find(BUNDLE, "at-token", urlopen_fn=fake_urlopen)
    assert files == [DRIVE_FILE]
    assert "files(id,name,size,md5Checksum)" in seen["url"]
    assert f"name = '{BUNDLE}' and trashed = false" in seen["url"]
    assert "supportsAllDrives=true" in seen["url"]
    assert "includeItemsFromAllDrives=true" in seen["url"]
    assert seen["auth"] == "Bearer at-token"


def test_drive_find_reports_a_query_failure_as_None_not_as_empty():
    """A failed query and an empty result mean OPPOSITE things — 'we could not ask'
    vs 'Google says it is not there'. Collapsing them would turn an outage into a
    false 'the backup never uploaded' page (or worse, the reverse)."""
    def boom(req, timeout=None):
        raise urllib.error.HTTPError("https://www.googleapis.com/drive/v3/files", 403,
                                     "Forbidden", {}, io.BytesIO(b'{"error":"scope"}'))
    files, note = rb._drive_find(BUNDLE, "at-token", urlopen_fn=boom)
    assert files is None
    assert files != []
    assert "HTTP 403" in note


# --------------------------------------------------------------------------- #
# THE ENABLEMENT FLAG — what the job does per outcome, and what `proves` admits
# --------------------------------------------------------------------------- #
def test_cloud_verify_required_ships_disabled():
    """It ships INERT: the credential does not exist yet, so wiring it fail-closed
    would fail tonight's 20:00 run and page about a missing credential rather than a
    missing backup — teaching exactly the 'that alarm is noise' reflex this job
    exists to defeat. Flip it once a real run reports cloud state 'verified'."""
    assert rb.CLOUD_VERIFY_REQUIRED is False


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
    st, f = _run(tmp_path, monkeypatch)          # default cloud_fn = skipped
    assert st["ok"] is True
    assert f["heartbeat_fn"].calls == 1
    assert st["cloud"]["state"] == "skipped_not_configured"
    assert st["proves"] == rb.PROVES_CLOUD_NOT_CHECKED
    assert "NOT checked" in st["proves"]
    assert "does NOT prove cloud arrival" in st["proves"]
    assert st["errors"] == []                    # not configured is not an error


def test_no_credential_with_required_true_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "CLOUD_VERIFY_REQUIRED", True)
    st, f = _run(tmp_path, monkeypatch)
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

    def cloud_fn(path, name):
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

    def cloud_fn(path, name):
        return _verify(find_fn=lambda n, t: ([dict(DRIVE_FILE, md5Checksum="dead" * 8)],
                                             "1 file"))

    st, f = _run(tmp_path, monkeypatch, cloud_fn=cloud_fn)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert st["cloud"]["state"] == "failed"
    assert "MD5 MISMATCH" in st["cloud"]["note"]
    assert st["proves"] == rb.PROVES_FAILED_RUN


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
