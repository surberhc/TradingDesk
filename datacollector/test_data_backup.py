r"""test_data_backup.py — tests for the verified data-backup job + its staleness alarm.

OFFLINE + FAST: no rclone subprocess, no network, no Google Drive, no real transfers.
Every test drives the injectable seams of data_backup.run_backup(...) with fakes and
asserts on the returned status dict, exactly as test_repo_backup.py does for the git
bundle job. `resolve_fn`, `copy_fn`, and `check_fn` are ALWAYS faked — the real ones run
rclone, which would touch the live warehouse and the live remote. Defaulting them here
keeps the suite offline even on a machine where rclone really exists.

WHAT THESE PIN — the same silent-failure regressions repo_backup encodes, restated for
the DATA path. The single most important assertion, repeated deliberately across every
failure path, is `heartbeat_fn.calls == 0` — the heartbeat is the only thing standing
between a silent data-backup failure and a page. The second is that `proves` is HONEST
in every case: an overstated `proves` (claiming a verified backup that did not happen) is
the exact bug class this whole body of work exists to kill.

Run from datacollector/ so `import config` (which heartbeat_alarm imports) resolves:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest test_data_backup.py -q
"""

from __future__ import annotations

import datetime as dt
import types
from pathlib import Path

import pytest

import data_backup as db
import heartbeat_alarm as hba


NOW = dt.datetime(2026, 7, 17, 21, 0, 0)

# Realistic rclone output shapes.
COPY_OK = ("Transferred:   \t   1.234 GiB / 1.234 GiB, 100%, 10.5 MiB/s, ETA 0s\n"
           "Checks:                 0 / 0, -\n"
           "Transferred:            5 / 5, 100%\n"
           "Elapsed time:         2m3.4s\n")
CHECK_OK = ("2026/07/17 21:05:00 NOTICE: Google drive root 'TradingDesk-DataBackup': "
            "0 differences found\n"
            "2026/07/17 21:05:00 NOTICE: Google drive root 'TradingDesk-DataBackup': "
            "464123 matching files\n")
CHECK_DIFF = ("2026/07/17 21:05:00 ERROR : warehouse/raw/x.parquet: md5 differ\n"
              "2026/07/17 21:05:00 ERROR : Google drive root 'TradingDesk-DataBackup': "
              "3 differences found\n"
              "2026/07/17 21:05:00 NOTICE: Google drive root 'TradingDesk-DataBackup': "
              "464120 matching files\n")


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


def _run(tmp_path, monkeypatch, **over):
    """Drive run_backup with every collaborator faked. Returns (status, fakes).

    status_fn/heartbeat_fn/log_fn are faked so NOTHING is written to disk. copy_fn and
    check_fn return canned rclone procs; the REAL parse_check_output / parse_transferred_bytes
    run against them, so the parsing is exercised end-to-end rather than mocked away.
    """
    fakes = {
        "resolve_fn": over.get("resolve_fn", Recorder(
            (r"C:\fake\rclone.exe", "resolved from TRADINGDESK_RCLONE"))),
        "copy_fn": over.get("copy_fn", Recorder(_proc(0, stderr=COPY_OK))),
        "check_fn": over.get("check_fn", Recorder(_proc(0, stderr=CHECK_OK))),
        "status_fn": over.get("status_fn", Recorder(None)),
        "heartbeat_fn": over.get("heartbeat_fn", Recorder(None)),
        "log_fn": over.get("log_fn", Recorder(None)),
    }
    st = db.run_backup(now=NOW, dry_run=over.get("dry_run", False), **fakes)
    return st, fakes


# --------------------------------------------------------------------------- #
# THE HAPPY PATH — success must still work (don't over-correct into always-fail)
# --------------------------------------------------------------------------- #
def test_verified_success_refreshes_heartbeat(tmp_path, monkeypatch):
    st, f = _run(tmp_path, monkeypatch)
    assert st["ok"] is True
    assert f["heartbeat_fn"].calls == 1          # the ONLY path that may move it
    assert f["copy_fn"].calls == 1
    assert f["check_fn"].calls == 1              # copy is NOT the proof; check is
    assert st["copy_returncode"] == 0
    assert st["check_returncode"] == 0
    assert st["files_checked"] == 464123
    assert st["differences"] == 0
    assert st["bytes"] == int(1.234 * 1024 ** 3)  # parsed from copy stats
    assert st["errors"] == []


def test_verified_success_proves_string_is_honest_and_names_the_count(tmp_path, monkeypatch):
    """THE HONESTY ASSERTION for the success path. `proves` must state EXACTLY what was
    proven — N files byte-identical by md5 between the two named endpoints — and nothing
    more (no claim about excluded paths, no claim about tomorrow)."""
    st, _ = _run(tmp_path, monkeypatch)
    assert st["proves"] == db.PROVES_VERIFIED.format(
        n=464123, src=db.DATA_SOURCE, remote=db.RCLONE_REMOTE)
    assert "464123 files verified byte-identical (md5)" in st["proves"]
    assert str(db.RCLONE_REMOTE) in st["proves"]


def test_success_heartbeat_text_avoids_the_COMPLETE_marker(tmp_path, monkeypatch):
    """heartbeat_alarm.assess() treats a literal 'COMPLETE' in the heartbeat text as a
    finished-job marker. Our text must not trip that branch by accident."""
    st, f = _run(tmp_path, monkeypatch)
    text = f["heartbeat_fn"].args[0][0][0]
    assert "COMPLETE" not in text.upper()
    assert "data backup verified" in text


def test_success_with_unparseable_count_still_verifies_but_says_so(tmp_path, monkeypatch):
    """If rclone check exits 0 but its matching-file count can't be parsed, the run is
    still a verified success (the clean check IS the proof) — but `proves` must say the
    count was unavailable rather than fabricate a number."""
    st, f = _run(tmp_path, monkeypatch,
                 check_fn=Recorder(_proc(0, stderr="NOTICE: root: 0 differences found\n")))
    assert st["ok"] is True
    assert f["heartbeat_fn"].calls == 1
    assert st["files_checked"] is None
    assert st["proves"] == db.PROVES_VERIFIED_NO_COUNT.format(
        src=db.DATA_SOURCE, remote=db.RCLONE_REMOTE)
    assert "count was not parseable" in st["proves"]


# --------------------------------------------------------------------------- #
# FAILURE MODE: rclone check reports a DIFFERENCE — the integrity proof failed
# --------------------------------------------------------------------------- #
def test_check_difference_is_caught_and_never_reports_success(tmp_path, monkeypatch):
    """THE CORE REGRESSION. rclone copy 'succeeded' but the checksum verification found
    the remote copy is NOT byte-identical. That is a FAILED backup — the copy exiting 0
    is not proof, the check is."""
    st, f = _run(tmp_path, monkeypatch,
                 check_fn=Recorder(_proc(1, stderr=CHECK_DIFF)))
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0          # THE point: no silent green light
    assert st["check_returncode"] == 1
    assert st["differences"] == 3
    assert any("rclone check FAILED verification" in e for e in st["errors"])
    assert st["proves"] == db.PROVES_FAILED
    assert "verified byte-identical" not in st["proves"]   # must not overstate


def test_check_difference_with_exit_zero_still_fails(tmp_path, monkeypatch):
    """Belt-and-suspenders: even if rclone check somehow exits 0 while its own summary
    reports differences, the parse catches it and the run fails. Neither signal alone is
    allowed to bless a bad backup."""
    weird = ("NOTICE: root: 2 differences found\n"
             "NOTICE: root: 100 matching files\n")
    st, f = _run(tmp_path, monkeypatch, check_fn=Recorder(_proc(0, stderr=weird)))
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert any("despite exit 0" in e for e in st["errors"])
    assert st["proves"] == db.PROVES_FAILED


# --------------------------------------------------------------------------- #
# FAILURE MODE: rclone copy failed — never verify or crow over a failed copy
# --------------------------------------------------------------------------- #
def test_copy_failure_is_caught(tmp_path, monkeypatch):
    st, f = _run(tmp_path, monkeypatch,
                 copy_fn=Recorder(_proc(1, stderr="ERROR: permission denied")))
    assert st["ok"] is False
    assert st["copy_returncode"] == 1
    assert f["check_fn"].calls == 0              # never verify a copy that failed
    assert f["heartbeat_fn"].calls == 0
    assert any("rclone copy FAILED" in e for e in st["errors"])
    assert st["proves"] == db.PROVES_FAILED


# --------------------------------------------------------------------------- #
# FAILURE MODE: rclone binary missing — clean, honest failure
# --------------------------------------------------------------------------- #
def test_rclone_missing_is_a_clean_honest_failure(tmp_path, monkeypatch):
    st, f = _run(tmp_path, monkeypatch,
                 resolve_fn=Recorder((None, "rclone binary NOT FOUND — checked ...")))
    assert st["ok"] is False
    assert st["rclone_path"] is None
    assert f["copy_fn"].calls == 0              # nothing to run without the tool
    assert f["check_fn"].calls == 0
    assert f["heartbeat_fn"].calls == 0
    assert any("NOT FOUND" in e for e in st["errors"])
    assert st["proves"] == db.PROVES_FAILED
    # The failure is RECORDED (forensics) even though the heartbeat stays cold.
    assert f["status_fn"].calls >= 1


# --------------------------------------------------------------------------- #
# DRY-RUN — transfers nothing, verifies nothing, never moves the heartbeat
# --------------------------------------------------------------------------- #
def test_dry_run_transfers_nothing_verifies_nothing_and_leaves_heartbeat_alone(
        tmp_path, monkeypatch):
    st, f = _run(tmp_path, monkeypatch, dry_run=True)
    assert f["copy_fn"].calls == 1             # copy runs, but with --dry-run
    assert f["copy_fn"].args[0][0][1] is True  # dry_run flag threaded through
    assert f["check_fn"].calls == 0            # no verification on a dry run
    assert f["heartbeat_fn"].calls == 0        # heartbeat NEVER moves on dry-run
    assert st["ok"] is False                   # a dry run is NEVER a successful backup
    assert st["proves"] == db.PROVES_DRY_RUN
    assert "NOT proven" in st["proves"]


# --------------------------------------------------------------------------- #
# copy_argv / check_argv — copy-not-sync is the load-bearing safety choice
# --------------------------------------------------------------------------- #
def test_copy_uses_copy_not_sync_and_is_additive():
    """`copy` (additive) NOT `sync` (mirror) — a local deletion must never propagate to
    and nuke the irreplaceable backup copy. If someone 'optimises' this to sync, this
    fails first."""
    argv = db.copy_argv(r"C:\fake\rclone.exe")
    assert argv[1] == "copy"
    assert "sync" not in argv
    assert argv[2] == str(db.DATA_SOURCE)
    assert argv[3] == db.RCLONE_REMOTE
    assert "--checksum" in argv
    assert "--drive-use-trash=false" in argv
    for ex in ("venv/**", "backups/**", "secrets/**"):
        assert ex in argv
    # References the config by PATH (so rclone can auth) — never reads/prints its bytes.
    assert str(db.RCLONE_CONFIG) in argv
    assert "--dry-run" not in argv


def test_copy_dry_run_appends_the_flag():
    assert "--dry-run" in db.copy_argv(r"C:\fake\rclone.exe", dry_run=True)


def test_check_uses_the_check_subcommand_with_the_same_scope():
    """check must verify the SAME excludes/scope that copy wrote, or it compares against
    a differently-scoped remote and false-pages."""
    argv = db.check_argv(r"C:\fake\rclone.exe")
    assert argv[1] == "check"
    assert argv[2] == str(db.DATA_SOURCE)
    assert argv[3] == db.RCLONE_REMOTE
    for ex in ("venv/**", "backups/**", "secrets/**"):
        assert ex in argv
    assert str(db.RCLONE_CONFIG) in argv


# --------------------------------------------------------------------------- #
# parse_check_output — the summary counts, absence != zero
# --------------------------------------------------------------------------- #
def test_parse_check_clean_output():
    p = db.parse_check_output(CHECK_OK)
    assert p["differences"] == 0
    assert p["matching"] == 464123
    assert p["problems"] == 0


def test_parse_check_differences():
    p = db.parse_check_output(CHECK_DIFF)
    assert p["differences"] == 3
    assert p["matching"] == 464120
    assert p["problems"] >= 3


def test_parse_check_missing_and_uncheckable_hashes_count_as_problems():
    txt = ("ERROR : root: 2 files missing on destination\n"
           "ERROR : root: 1 hashes could not be checked\n"
           "NOTICE: root: 0 differences found\n")
    p = db.parse_check_output(txt)
    assert p["missing_dst"] == 2
    assert p["hashes_unchecked"] == 1
    assert p["problems"] >= 3


def test_parse_check_absent_counts_are_none_not_zero():
    """'we could not find the line' must not launder into 'the line said zero'."""
    p = db.parse_check_output("some unrelated startup log line\n")
    assert p["differences"] is None
    assert p["matching"] is None
    assert p["problems"] == 0        # absence never fabricates a problem for the sum


# --------------------------------------------------------------------------- #
# parse_transferred_bytes — best-effort, None on failure
# --------------------------------------------------------------------------- #
def test_parse_transferred_bytes_reads_the_byte_line():
    b = db.parse_transferred_bytes("Transferred:   \t   1.5 GiB / 1.5 GiB, 100%\n")
    assert b == int(1.5 * 1024 ** 3)


def test_parse_transferred_bytes_none_when_absent():
    assert db.parse_transferred_bytes("no stats in here") is None


# --------------------------------------------------------------------------- #
# resolve_rclone — robust, version-stable, fails loud
# --------------------------------------------------------------------------- #
def test_resolve_rclone_honours_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "rclone.exe"
    fake.write_text("x")
    monkeypatch.setenv("TRADINGDESK_RCLONE", str(fake))
    path, note = db.resolve_rclone()
    assert path == str(fake)
    assert "TRADINGDESK_RCLONE" in note


def test_resolve_rclone_falls_back_to_PATH(tmp_path, monkeypatch):
    """No env override, no winget shim (LOCALAPPDATA points at an empty temp dir) — so
    it must find rclone on PATH."""
    monkeypatch.delenv("TRADINGDESK_RCLONE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path, note = db.resolve_rclone(which_fn=lambda n: r"C:\somewhere\rclone.exe")
    assert path == r"C:\somewhere\rclone.exe"
    assert "PATH" in note


def test_resolve_rclone_not_found_is_loud(tmp_path, monkeypatch):
    """Nothing resolves -> (None, loud reason). A backup that can't find its tool must
    page, not limp on silently."""
    monkeypatch.delenv("TRADINGDESK_RCLONE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))   # no shim, no Packages tree
    path, note = db.resolve_rclone(which_fn=lambda n: None)
    assert path is None
    assert "NOT FOUND" in note


# --------------------------------------------------------------------------- #
# THE ALARM — the data_backup job is wired with the documented threshold
# --------------------------------------------------------------------------- #
def test_data_backup_job_is_actually_registered_in_JOBS():
    """Wiring test: the job must be in the alarm's watch list, pointed at the SAME
    heartbeat the backup job writes, with the documented threshold. A perfect backup job
    nobody watches is how you get a silent data loss."""
    jobs = [j for j in hba.JOBS if j["name"] == "data_backup"]
    assert len(jobs) == 1, "data_backup is not registered in heartbeat_alarm.JOBS"
    job = jobs[0]
    assert str(job["heartbeat"]) == str(db.HEARTBEAT_FILE)
    assert job["threshold_s"] == hba.DATA_BACKUP_THRESHOLD_S
    assert job["task_name"] == "DataBackupDaily"


def test_data_backup_threshold_is_the_documented_30h():
    """Pinned so a silent change to the cadence<->threshold coupling can't slip through:
    30h = 24h daily cadence + ~6h grace for a long (hours-scale) run."""
    assert hba.DATA_BACKUP_THRESHOLD_S == 30 * 3600


def _data_job(tmp_path):
    return {"name": "data_backup",
            "label": "TradingDesk data backup (rclone -> Drive)",
            "heartbeat": tmp_path / "data_backup_heartbeat.txt",
            "progress": None,
            "threshold_s": hba.DATA_BACKUP_THRESHOLD_S,
            "task_name": "DataBackupDaily"}


def _hb(job, now, age_s):
    import os
    p = job["heartbeat"]
    p.write_text("2026-07-17 21:00:00  data backup verified ...")
    os.utime(p, (now - age_s, now - age_s))
    return p


def test_data_backup_alarm_fires_when_stale(tmp_path):
    now = dt.datetime.now().timestamp()
    job = _data_job(tmp_path)
    _hb(job, now, age_s=31 * 3600)              # 31h > 30h threshold
    a = hba.assess(job, now)
    assert a["status"] == "stale"
    assert a["alert"] is True


def test_data_backup_alarm_quiet_when_fresh(tmp_path):
    now = dt.datetime.now().timestamp()
    job = _data_job(tmp_path)
    _hb(job, now, age_s=2 * 3600)               # a normal same-day run
    a = hba.assess(job, now)
    assert a["status"] == "fresh"
    assert a["alert"] is False


def test_data_backup_alarm_fires_when_heartbeat_never_existed(tmp_path):
    now = dt.datetime.now().timestamp()
    job = _data_job(tmp_path)                    # file deliberately not created
    a = hba.assess(job, now)
    assert a["status"] == "missing"
    assert a["alert"] is True
