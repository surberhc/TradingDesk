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
        # Faked so the suite never reads the REAL status file off disk. Returning None
        # ("never deep-verified") means auto-mode resolves to DEEP — which is also what
        # NOW (a Friday) would give, so every pre-existing test below is a DEEP run.
        "last_deep_fn": over.get("last_deep_fn", Recorder(None)),
    }
    extra = {}
    for opt in ("mtime_fn", "verify_list_fn", "copied_fn"):
        if opt in over:
            extra[opt] = over[opt]
    st = db.run_backup(now=NOW, dry_run=over.get("dry_run", False),
                       mode=over.get("mode", "auto"), **fakes, **extra)
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
    # A changed file under warehouse/raw/ is TIER 1 — `proves` names it, and never
    # implies anything was verified.
    assert "warehouse/raw/x.parquet" in st["proves"]
    assert "TIER-1" in st["proves"]
    assert st["proves"].startswith("nothing")
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
# THE THREE TIERS — the 2026-07-18 false-page fix, and the guarantee it must NOT blunt.
#
# Context (from the real first run): the initial 99 GB backup completed and verified
# 499,534 files byte-identical, then `rclone check` exited 1 with 17 differences — all of
# them benign live-file churn, ZERO .parquet among them. The job as built treated ANY
# difference as failure, so it would have false-paged EVERY night. The fix classifies
# differences into three tiers. These tests pin BOTH halves of that: benign churn no
# longer pages, AND a real corruption still does.
# --------------------------------------------------------------------------- #
BEFORE_RUN = (NOW - dt.timedelta(hours=3)).timestamp()   # existed before the run began
DURING_RUN = (NOW + dt.timedelta(hours=4)).timestamp()   # created 4h into the run


def _err(path, reason):
    return f"2026/07/17 21:05:00 ERROR : {path}: {reason}\n"


def _summary(differences, matching, missing=0, extra_errors=None):
    """rclone check's trailer. `errors` counts the same files as `differences` (they
    overlap), which is why the accounting rule uses max(), not a sum."""
    n_err = differences if extra_errors is None else extra_errors
    out = ""
    if missing:
        out += f"ERROR : Google drive root 'X': {missing} files missing\n"
    out += (f"ERROR : Google drive root 'X': {differences} differences found\n"
            f"ERROR : Google drive root 'X': {n_err} errors while checking\n"
            f"NOTICE: Google drive root 'X': {matching} matching files\n")
    return out


def test_TIER1_parquet_size_mismatch_hard_fails_and_never_moves_the_heartbeat(
        tmp_path, monkeypatch):
    """THE MOST IMPORTANT TEST IN THIS FILE. warehouse/raw/** is write-once market data.
    If one of those files differs, that is corruption or a failed upload — the classifier
    must NOT forgive it. This proves the false-page fix did not blunt real corruption
    detection."""
    out = (_err("warehouse/raw/options/spy/2024-01-02.parquet", "sizes differ") +
           _summary(differences=1, matching=499538))
    st, f = _run(tmp_path, monkeypatch, check_fn=Recorder(_proc(1, stderr=out)),
                 mtime_fn=lambda p: BEFORE_RUN)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0                  # cold heartbeat -> it pages
    assert len(st["hard_failures"]) == 1
    assert st["hard_failures"][0]["path"] == "warehouse/raw/options/spy/2024-01-02.parquet"
    assert "TIER-1" in st["proves"]
    assert "2024-01-02.parquet" in st["proves"]          # proves NAMES the failure
    assert "verified byte-identical" not in st["proves"]
    assert st["benign_differences"]["count"] == 0


def test_TIER1_parquet_hash_differ_wording_is_also_caught(tmp_path, monkeypatch):
    """rclone says 'sizes differ' or 'md5 differ' depending on what tripped. Both must
    reach tier 1 — a classifier that only understood one wording would be a silent hole."""
    out = (_err("warehouse/raw/options/x.parquet", "md5 differ") +
           _summary(differences=1, matching=10))
    st, f = _run(tmp_path, monkeypatch, check_fn=Recorder(_proc(1, stderr=out)),
                 mtime_fn=lambda p: BEFORE_RUN)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert st["hard_failures"][0]["reason"] == "md5 differ"


def test_TIER1_missing_file_older_than_run_start_hard_fails(tmp_path, monkeypatch):
    """A file that EXISTED when rclone walked past, yet is not on the remote, is a real
    'should have been backed up and wasn't'. Only the mtime distinguishes it from the
    benign tier-3 case, so this is where that rule earns its keep."""
    out = (_err("warehouse/raw/options/old.parquet", "file not in Google drive root 'X'") +
           _summary(differences=1, matching=10, missing=1))
    st, f = _run(tmp_path, monkeypatch, check_fn=Recorder(_proc(1, stderr=out)),
                 mtime_fn=lambda p: BEFORE_RUN)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert st["hard_failures"][0]["path"] == "warehouse/raw/options/old.parquet"
    assert "existed BEFORE the run" in st["hard_failures"][0]["why"]


def test_TIER1_missing_file_with_unreadable_mtime_fails_closed(tmp_path, monkeypatch):
    """If we cannot read the local mtime we cannot PROVE the file was created during the
    run, so we must not forgive it. Fail closed."""
    out = (_err("state/whatever.dat", "file not in Google drive root 'X'") +
           _summary(differences=1, matching=10, missing=1))
    st, f = _run(tmp_path, monkeypatch, check_fn=Recorder(_proc(1, stderr=out)),
                 mtime_fn=lambda p: None)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0


def test_TIER1_unknown_reason_fails_closed(tmp_path, monkeypatch):
    """A reason the classifier does not understand is not a reason it may forgive."""
    out = (_err("some/file.txt", "something rclone has never said before") +
           _summary(differences=1, matching=10))
    st, f = _run(tmp_path, monkeypatch, check_fn=Recorder(_proc(1, stderr=out)),
                 mtime_fn=lambda p: BEFORE_RUN)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert "unrecognised" in st["hard_failures"][0]["why"]


def test_TIER1_changed_file_matching_no_volatile_pattern_fails_closed(
        tmp_path, monkeypatch):
    """Outside warehouse/raw/ the default is still FAIL. Legitimate churn is added to
    VOLATILE_PATTERNS deliberately, with a reason — it is never assumed."""
    out = (_err("strategies/s0.py", "sizes differ") + _summary(differences=1, matching=10))
    st, f = _run(tmp_path, monkeypatch, check_fn=Recorder(_proc(1, stderr=out)),
                 mtime_fn=lambda p: BEFORE_RUN)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert "failing closed" in st["hard_failures"][0]["why"]


def test_TIER3_missing_file_created_during_the_run_is_benign(tmp_path, monkeypatch):
    """rclone cannot copy a file that did not exist when it walked that path. The next
    run picks it up. This is 9 of the 17 real-world differences."""
    out = (_err("s8_pilot/logs/pilot.log", "file not in Google drive root 'X'") +
           _summary(differences=1, matching=499538, missing=1))
    st, f = _run(tmp_path, monkeypatch, check_fn=Recorder(_proc(1, stderr=out)),
                 mtime_fn=lambda p: DURING_RUN)
    assert st["ok"] is True
    assert f["heartbeat_fn"].calls == 1                  # a GOOD backup is not blocked
    assert st["hard_failures"] == []
    assert st["benign_differences"]["tier3_created_during_run"] == 1
    assert "created during the run" in st["benign_differences"]["sample"][0]["why"]


@pytest.mark.parametrize("path", [
    "conductor/conductor.db",
    "warehouse/heartbeat_alarm.log",
    "state/paperbot/runs.jsonl",
    "warehouse/raw/options/_manifest.json",     # index sidecar INSIDE warehouse/raw
    "warehouse/register_forward_live.ps1",
    "warehouse/run_forward_live.bat",
])
def test_TIER2_known_volatile_files_are_benign(tmp_path, monkeypatch, path):
    """Files the desk rewrites while it runs. A difference between copy time and check
    time is expected, not corruption."""
    out = _err(path, "sizes differ") + _summary(differences=1, matching=499538)
    st, f = _run(tmp_path, monkeypatch, check_fn=Recorder(_proc(1, stderr=out)),
                 mtime_fn=lambda p: BEFORE_RUN)
    assert st["ok"] is True, f"{path} should be classified benign"
    assert f["heartbeat_fn"].calls == 1
    assert st["hard_failures"] == []
    assert st["benign_differences"]["tier2_known_volatile"] == 1


def test_TIER2_parquet_is_deliberately_NOT_volatile():
    """The one entry that must never appear in the volatile list. Parquet is the data we
    are insuring."""
    assert not db.is_volatile("warehouse/raw/options/spy/2024-01-02.parquet")
    assert db.is_precious("warehouse/raw/options/spy/2024-01-02.parquet")
    assert not any("parquet" in p for p in db.VOLATILE_PATTERNS)


def test_MIXED_benign_churn_never_masks_a_real_tier1_failure(tmp_path, monkeypatch):
    """The failure mode a lazy fix would introduce: forgive the noise and lose the signal
    with it. One corrupt parquet among a pile of legitimate churn must STILL page."""
    out = (_err("conductor/conductor.db", "sizes differ") +
           _err("warehouse/heartbeat_alarm.log", "sizes differ") +
           _err("s8_pilot/logs/a.log", "file not in Google drive root 'X'") +
           _err("warehouse/raw/options/spy/2024-01-02.parquet", "sizes differ") +
           _summary(differences=4, matching=499535, missing=1))
    st, f = _run(tmp_path, monkeypatch, check_fn=Recorder(_proc(1, stderr=out)),
                 mtime_fn=lambda p: DURING_RUN if "s8_pilot" in p else BEFORE_RUN)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0                  # the signal survives the noise
    assert len(st["hard_failures"]) == 1
    assert "2024-01-02.parquet" in st["proves"]
    assert st["benign_differences"]["count"] == 3        # the churn is still RECORDED


def test_unaccounted_differences_fail_closed(tmp_path, monkeypatch):
    """The summary says 5 problems but only one per-file ERROR line explains one of them.
    The four we cannot see we cannot classify — and an unclassified problem is a failure,
    never a benign one. This is what stops the fix from becoming a blanket amnesty."""
    out = (_err("conductor/conductor.db", "sizes differ") +
           _summary(differences=5, matching=10))
    st, f = _run(tmp_path, monkeypatch, check_fn=Recorder(_proc(1, stderr=out)),
                 mtime_fn=lambda p: BEFORE_RUN)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert any("could not be accounted for" in e for e in st["errors"])
    assert st["proves"] == db.PROVES_FAILED


def test_unverifiable_hashes_fail_closed(tmp_path, monkeypatch):
    """'hashes could not be checked' produces no path to classify. Unverifiable is not
    verified."""
    out = ("ERROR : Google drive root 'X': 2 hashes could not be checked\n"
           "NOTICE: Google drive root 'X': 10 matching files\n")
    st, f = _run(tmp_path, monkeypatch, check_fn=Recorder(_proc(1, stderr=out)),
                 mtime_fn=lambda p: BEFORE_RUN)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert any("could not be checked" in e for e in st["errors"])


# --------------------------------------------------------------------------- #
# THE REGRESSION TEST: replay the EXACT 17 differences from the real 2026-07-18 run.
# --------------------------------------------------------------------------- #
REAL_S8_LOGS = [f"s8_pilot/logs/s8_pilot_{i}.log" for i in range(9)]
REAL_CHANGED = [
    "conductor/conductor.db",
    "warehouse/heartbeat_alarm.log",
    "warehouse/morning_execute.log",
    "warehouse/register_forward_live.ps1",
    "warehouse/run_forward_live.bat",
    "state/paperbot/paperbot.log",
    "state/paperbot/runs.jsonl",
    "warehouse/raw/options/_manifest.json",
]
REAL_CHECK_OUTPUT = (
    "".join(_err(p, "file not in Google drive root 'TradingDesk-DataBackup'")
            for p in REAL_S8_LOGS) +
    "".join(_err(p, "sizes differ") for p in REAL_CHANGED) +
    _summary(differences=17, matching=499534, missing=9)
)


def test_replay_the_real_17_differences_is_fully_benign_and_does_not_page(
        tmp_path, monkeypatch):
    """THE FALSE-PAGE REGRESSION TEST. This is verbatim the result of the first real
    99 GB backup: 499,534 files byte-identical, then 17 differences — 9 s8_pilot logs a
    concurrent session created ~4h into the run, and 8 live files that changed between
    copy and check. Zero corruption, zero .parquet. The as-built job failed on this and
    would have paged EVERY night."""
    st, f = _run(tmp_path, monkeypatch,
                 check_fn=Recorder(_proc(1, stderr=REAL_CHECK_OUTPUT)),
                 mtime_fn=lambda p: DURING_RUN if p.startswith("s8_pilot/") else BEFORE_RUN)
    assert st["ok"] is True
    assert f["heartbeat_fn"].calls == 1                  # NO PAGE. That is the whole fix.
    assert st["hard_failures"] == []
    assert st["benign_differences"]["count"] == 17
    assert st["benign_differences"]["tier3_created_during_run"] == 9
    assert st["benign_differences"]["tier2_known_volatile"] == 8
    assert st["files_checked"] == 499534


def test_replay_the_real_17_proves_string_is_honest_about_them(tmp_path, monkeypatch):
    """It must NOT read as a clean 100%. The 17 are named, and the sentence says outright
    that those files are not proven current."""
    st, _ = _run(tmp_path, monkeypatch,
                 check_fn=Recorder(_proc(1, stderr=REAL_CHECK_OUTPUT)),
                 mtime_fn=lambda p: DURING_RUN if p.startswith("s8_pilot/") else BEFORE_RUN)
    assert st["proves"] == db.PROVES_VERIFIED_WITH_BENIGN.format(
        n=499534, m=17, v=8, c=9, src=db.DATA_SOURCE, remote=db.RCLONE_REMOTE)
    assert "499534 files verified byte-identical (md5)" in st["proves"]
    assert "17 benign differences" in st["proves"]
    assert "NOT proven current" in st["proves"]
    assert "0 mismatches in the immutable warehouse/raw data" in st["proves"]
    # It must not claim the clean-run wording.
    assert "0 differences and 0 errors" not in st["proves"]


def test_benign_sample_in_the_status_file_is_capped(tmp_path, monkeypatch):
    """Audit-able, not a dump: thousands of churning paths must never land in the status
    JSON."""
    n = db.BENIGN_SAMPLE_CAP + 20
    paths = [f"warehouse/job{i}.log" for i in range(n)]
    out = ("".join(_err(p, "sizes differ") for p in paths) +
           _summary(differences=n, matching=10))
    st, f = _run(tmp_path, monkeypatch, check_fn=Recorder(_proc(1, stderr=out)),
                 mtime_fn=lambda p: BEFORE_RUN)
    assert st["ok"] is True
    assert st["benign_differences"]["count"] == n
    assert len(st["benign_differences"]["sample"]) == db.BENIGN_SAMPLE_CAP


# --------------------------------------------------------------------------- #
# parse_check_errors — the per-file ERROR lines, and NOT rclone's summary lines
# --------------------------------------------------------------------------- #
def test_parse_check_errors_extracts_path_reason_and_kind():
    d = db.parse_check_errors(
        _err("conductor/conductor.db", "sizes differ") +
        _err("warehouse/raw/x.parquet", "md5 differ") +
        _err("s8_pilot/logs/a.log", "file not in Google drive root 'X'"))
    assert [x["kind"] for x in d] == ["differ", "differ", "missing"]
    assert d[0]["path"] == "conductor/conductor.db"


def test_parse_check_errors_ignores_rclones_own_summary_lines():
    """The summary trailer is written at ERROR level too. Mistaking it for a file would
    invent a phantom path and, worse, make the accounting look satisfied."""
    assert db.parse_check_errors(_summary(differences=17, matching=499534, missing=9)) == []


def test_parse_check_errors_normalises_windows_separators():
    d = db.parse_check_errors(_err(r"warehouse\raw\x.parquet", "sizes differ"))
    assert d[0]["path"] == "warehouse/raw/x.parquet"


def test_required_accounted_uses_max_not_sum():
    """rclone's counts OVERLAP — the same 17 files are '17 differences' AND '17 errors',
    9 of which are also '9 missing'. Summing them would demand 43 ERROR lines and
    false-fail every run."""
    parsed = db.parse_check_output(_summary(differences=17, matching=499534, missing=9))
    assert db.required_accounted(parsed) == 17


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


# --------------------------------------------------------------------------- #
# TWO CADENCES (2026-07-20) — deep (full re-hash) vs incremental (verify what we copied)
#
# The old job re-hashed ~99 GB EVERY night (copy --checksum + a full unscoped check =
# ~200 GB of local reads) while the uploads were already incremental. These tests pin the
# split AND, more importantly, pin the honesty of the smaller claim: an incremental run
# must never say the whole warehouse is verified, because it isn't.
# --------------------------------------------------------------------------- #
def test_NOW_is_a_friday_so_the_existing_suite_runs_in_deep_mode():
    """Load-bearing for every test above: NOW is DEEP_WEEKDAY, so auto-mode picks deep and
    the pre-existing expectations (full check, PROVES_VERIFIED*) still hold unchanged."""
    assert NOW.weekday() == db.DEEP_WEEKDAY == 4        # Friday
    st, _ = _run(None, None)
    assert st["mode"] == "deep"


# --- choose_mode: all five rules --------------------------------------------------- #
FRIDAY = dt.datetime(2026, 7, 17, 21, 0, 0)
MONDAY = dt.datetime(2026, 7, 20, 21, 0, 0)


def test_choose_mode_explicit_request_wins_over_every_other_rule():
    """Rule 1. An operator override is honoured verbatim — including asking for the CHEAP
    pass on a Friday, and the EXPENSIVE pass on a Monday."""
    mode, why = db.choose_mode(FRIDAY, "2026-07-17T21:00:00", requested="incremental")
    assert mode == "incremental"
    assert "explicitly requested" in why
    mode, why = db.choose_mode(MONDAY, "2026-07-20T21:00:00", requested="deep")
    assert mode == "deep"
    assert "explicitly requested" in why


def test_choose_mode_never_deep_verified_goes_deep():
    """Rule 2. With no verified baseline there is nothing to be incremental against."""
    mode, why = db.choose_mode(MONDAY, None)
    assert mode == "deep"
    assert "no usable record" in why


def test_choose_mode_stale_last_deep_self_heals_to_deep():
    """Rule 3. A missed Friday must not become 'we quietly never deep-verified again'."""
    stale = (MONDAY - dt.timedelta(days=db.DEEP_MAX_AGE_DAYS + 2)).isoformat()
    mode, why = db.choose_mode(MONDAY, stale)
    assert mode == "deep"
    assert "self-heal" in why
    assert "days ago" in why


def test_choose_mode_on_the_deep_weekday_goes_deep():
    """Rule 4. The scheduled full pass."""
    mode, why = db.choose_mode(FRIDAY, (FRIDAY - dt.timedelta(days=7)).isoformat())
    assert mode == "deep"
    assert "Friday" in why


def test_choose_mode_otherwise_is_incremental_and_names_the_last_deep():
    """Rule 5. The normal weeknight."""
    mode, why = db.choose_mode(MONDAY, "2026-07-17T21:00:00")
    assert mode == "incremental"
    assert "2026-07-17" in why


def test_choose_mode_accepts_a_datetime_as_well_as_a_string():
    mode, _ = db.choose_mode(MONDAY, dt.datetime(2026, 7, 17, 21, 0, 0))
    assert mode == "incremental"


def test_choose_mode_unparseable_last_deep_fails_TOWARD_deep():
    """An unreadable timestamp means we cannot prove when the last full verification was.
    Unknown must resolve to the MORE thorough pass — the cost of a needless deep run is
    disk time; the cost of a wrongly-skipped one is unverified data."""
    mode, why = db.choose_mode(MONDAY, "not a timestamp at all")
    assert mode == "deep"
    assert "no usable record" in why


# --- argv --------------------------------------------------------------------------- #
def test_incremental_copy_argv_drops_checksum_and_adds_verbose():
    """The whole saving: no --checksum means rclone hashes NOTHING locally and decides
    from size+modtime. -v is what makes the copied paths visible to parse."""
    argv = db.copy_argv(r"C:\fake\rclone.exe", deep=False)
    assert "--checksum" not in argv
    assert "-v" in argv
    assert "--drive-use-trash=false" in argv
    assert argv[1] == "copy"


def test_deep_copy_argv_is_unchanged_and_is_the_DEFAULT():
    argv = db.copy_argv(r"C:\fake\rclone.exe")
    assert "--checksum" in argv
    assert "-v" not in argv
    assert db.copy_argv(r"C:\fake\rclone.exe", deep=True) == argv


def test_check_argv_scopes_to_files_from_when_given_one():
    argv = db.check_argv(r"C:\fake\rclone.exe", files_from=r"C:\tmp\list.txt")
    assert "--files-from" in argv
    assert argv[argv.index("--files-from") + 1] == r"C:\tmp\list.txt"


def test_check_argv_without_files_from_is_the_full_unscoped_pass():
    assert "--files-from" not in db.check_argv(r"C:\fake\rclone.exe")


# --- parse_copied_paths ------------------------------------------------------------- #
COPY_INCREMENTAL = (
    "2026/07/20 21:00:31 INFO  : warehouse/raw/options/SPY/2026-07-20.parquet: "
    "Copied (new)\n"
    "2026/07/20 21:00:31 INFO  : state/paperbot/runs.jsonl: Copied (replaced existing)\n"
    "Transferred:   \t   1.234 GiB / 1.234 GiB, 100%, 10.5 MiB/s, ETA 0s\n"
    "Transferred:            2 / 2, 100%\n")
COPY_NOTHING_NEW = (
    "2026/07/20 21:00:31 INFO  : There was nothing to transfer\n"
    "Transferred:   \t   0 B / 0 B, -, 0 B/s, ETA -\n"
    "Checks:              499539 / 499539, 100%\n"
    "Elapsed time:         4m1.2s\n")


def test_parse_copied_paths_handles_both_copied_wordings():
    got = db.parse_copied_paths(COPY_INCREMENTAL)
    assert got == ["warehouse/raw/options/SPY/2026-07-20.parquet",
                   "state/paperbot/runs.jsonl"]


def test_parse_copied_paths_PRESERVES_CASE():
    """rclone check --files-from matches case-SENSITIVELY. Lower-casing the list (as
    normalise_path does) would silently match nothing, and a check that verifies nothing
    while exiting clean is precisely the silent green light this job exists to prevent."""
    got = db.parse_copied_paths(
        "2026/07/20 21:00:31 INFO  : Warehouse/RAW/SPY/Xyz.PARQUET: Copied (new)\n")
    assert got == ["Warehouse/RAW/SPY/Xyz.PARQUET"]


def test_parse_copied_paths_dedups_but_preserves_order():
    txt = ("INFO  : b/second.txt: Copied (new)\n"
           "INFO  : a/first.txt: Copied (new)\n"
           "INFO  : b/second.txt: Copied (replaced existing)\n")
    assert db.parse_copied_paths(txt) == ["b/second.txt", "a/first.txt"]


def test_parse_copied_paths_ignores_everything_that_is_not_a_copy():
    txt = ("INFO  : old/thing.txt: Deleted\n"
           "INFO  : other/thing.txt: Updated modification time\n"
           "INFO  : There was nothing to transfer\n"
           "Transferred:   \t   0 B / 0 B, -, 0 B/s, ETA -\n"
           "NOTICE: Google drive root 'X': 0 differences found\n")
    assert db.parse_copied_paths(txt) == []
    assert db.parse_copied_paths(COPY_NOTHING_NEW) == []


def test_parse_copied_paths_normalises_windows_separators():
    got = db.parse_copied_paths(r"INFO  : .\warehouse\raw\x.parquet: Copied (new)" + "\n")
    assert got == ["warehouse/raw/x.parquet"]


def test_parse_copied_paths_empty_input():
    assert db.parse_copied_paths("") == []
    assert db.parse_copied_paths(None) == []


# --- write_verify_list -------------------------------------------------------------- #
def test_write_verify_list_writes_one_path_per_line(tmp_path):
    target = tmp_path / "list.txt"
    out = db.write_verify_list(["a/b.parquet", "C/D.jsonl"], path=target)
    assert Path(out) == target
    assert target.read_text(encoding="utf-8") == "a/b.parquet\nC/D.jsonl\n"


def test_verify_list_lives_under_an_EXCLUDED_dir_so_it_is_never_backed_up():
    """If the scoping list were itself inside the backup scope it would show up as a
    difference in the very check it scopes."""
    assert str(db.BACKUP_DIR).lower() in str(db.VERIFY_LIST_FILE).lower()
    assert "backups/**" in db.EXCLUDES


# --- read_last_deep ----------------------------------------------------------------- #
def test_read_last_deep_reads_the_field(tmp_path):
    p = tmp_path / "status.json"
    p.write_text('{"last_deep_verified": "2026-07-17T21:00:00"}', encoding="utf-8")
    assert db.read_last_deep(status_file=p) == "2026-07-17T21:00:00"


@pytest.mark.parametrize("body", [None, "{ not json at all", "[]", '{"ok": true}'])
def test_read_last_deep_never_raises_and_returns_none_when_unknown(tmp_path, body):
    """Missing file, malformed file, wrong shape, or missing key — all mean 'unknown',
    which resolves to a DEEP run upstream. It must never crash the backup job."""
    p = tmp_path / "status.json"
    if body is not None:
        p.write_text(body, encoding="utf-8")
    assert db.read_last_deep(status_file=p) is None


# --- incremental runs --------------------------------------------------------------- #
def _incremental(tmp_path, *, copy_out=COPY_INCREMENTAL, check_fn=None, last_deep=None,
                 mtime_fn=None):
    over = {
        "mode": "incremental",
        "copy_fn": Recorder(_proc(0, stderr=copy_out)),
        "verify_list_fn": Recorder(tmp_path / "verify_list.txt"),
        "last_deep_fn": Recorder(last_deep),
    }
    if check_fn is not None:
        over["check_fn"] = check_fn
    if mtime_fn is not None:
        over["mtime_fn"] = mtime_fn
    return _run(tmp_path, None, **over)


def test_incremental_run_scopes_the_check_to_the_copied_files(tmp_path):
    st, f = _incremental(tmp_path, last_deep="2026-07-10T21:00:00")
    assert st["mode"] == "incremental"
    assert st["copied_count"] == 2
    assert f["check_fn"].calls == 1
    assert f["check_fn"].args[0][1]["files_from"] == str(tmp_path / "verify_list.txt")
    assert f["heartbeat_fn"].calls == 1
    assert st["ok"] is True
    # The copy ran WITHOUT --checksum: deep=False threaded through as a keyword.
    assert f["copy_fn"].args[0][1]["deep"] is False


def test_incremental_proves_string_never_claims_the_whole_warehouse(tmp_path):
    """THE HONESTY ASSERTION for the cheap pass. It proved 2 files. It must say 2 files,
    and it must name when everything else was last actually proven."""
    st, _ = _incremental(tmp_path, last_deep="2026-07-10T21:00:00")
    assert st["proves"] == db.PROVES_VERIFIED_INCREMENTAL.format(
        n=2, src=db.DATA_SOURCE, remote=db.RCLONE_REMOTE, last_deep="2026-07-10T21:00:00")
    assert "everything else IS proven byte-identical" not in st["proves"]
    assert "NOT re-verified by this run" in st["proves"]
    assert "2026-07-10T21:00:00" in st["proves"]


def test_incremental_with_no_prior_deep_renders_never_not_None(tmp_path):
    st, _ = _incremental(tmp_path, last_deep=None)
    assert "never" in st["proves"]
    assert "None" not in st["proves"]


def test_incremental_with_benign_churn_uses_the_benign_incremental_wording(tmp_path):
    out = (_err("conductor/conductor.db", "sizes differ") +
           _summary(differences=1, matching=2))
    st, f = _incremental(tmp_path, check_fn=Recorder(_proc(1, stderr=out)),
                         last_deep="2026-07-10T21:00:00",
                         mtime_fn=lambda p: BEFORE_RUN)
    assert st["ok"] is True
    assert f["heartbeat_fn"].calls == 1
    assert st["proves"] == db.PROVES_VERIFIED_INCREMENTAL_WITH_BENIGN.format(
        n=2, m=1, v=1, c=0, src=db.DATA_SOURCE, remote=db.RCLONE_REMOTE,
        last_deep="2026-07-10T21:00:00")
    assert "everything else IS proven byte-identical" not in st["proves"]


def test_incremental_with_nothing_copied_skips_the_check_entirely(tmp_path):
    """Nothing changed, so nothing needs verifying — and that IS a verified state: the
    backup is exactly as current as it was. Running a check here would burn hours to
    re-prove bytes nobody touched."""
    st, f = _incremental(tmp_path, copy_out=COPY_NOTHING_NEW,
                         last_deep="2026-07-10T21:00:00")
    assert st["copied_count"] == 0
    assert f["check_fn"].calls == 0              # the whole point
    assert st["ok"] is True
    assert f["heartbeat_fn"].calls == 1
    assert st["proves"] == db.PROVES_VERIFIED_NOTHING_NEW.format(
        remote=db.RCLONE_REMOTE, last_deep="2026-07-10T21:00:00")
    assert "nothing new to copy" in st["proves"]
    assert "verified byte-identical" not in st["proves"]   # nothing was checked; say so


def test_incremental_heartbeat_text_names_the_mode(tmp_path):
    st, f = _incremental(tmp_path, last_deep="2026-07-10T21:00:00")
    text = f["heartbeat_fn"].args[0][0][0]
    assert "mode=incremental" in text
    assert "copied=2" in text
    assert "COMPLETE" not in text.upper()


def test_incremental_TIER1_failure_still_fails_closed(tmp_path):
    """The cheap pass must not become a cheap EXCUSE. A corrupt parquet inside the scoped
    check pages exactly as it does in a deep run."""
    out = (_err("warehouse/raw/options/spy/2024-01-02.parquet", "md5 differ") +
           _summary(differences=1, matching=1))
    st, f = _incremental(tmp_path, check_fn=Recorder(_proc(1, stderr=out)),
                         last_deep="2026-07-10T21:00:00",
                         mtime_fn=lambda p: BEFORE_RUN)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0          # cold heartbeat -> it pages
    assert "TIER-1" in st["proves"]
    assert "2024-01-02.parquet" in st["proves"]


# --- the last_deep_verified clock --------------------------------------------------- #
def test_only_a_successful_deep_run_advances_last_deep_verified(tmp_path, monkeypatch):
    st, _ = _run(tmp_path, monkeypatch, last_deep_fn=Recorder("2026-07-10T21:00:00"))
    assert st["mode"] == "deep"
    assert st["last_deep_verified"] == NOW.isoformat(timespec="seconds")


def test_a_successful_incremental_run_carries_last_deep_verified_FORWARD(tmp_path):
    """It must not be clobbered to None by the cheap pass — the whole self-heal and the
    honesty of `proves` both hang off this value surviving."""
    st, _ = _incremental(tmp_path, last_deep="2026-07-10T21:00:00")
    assert st["ok"] is True
    assert st["last_deep_verified"] == "2026-07-10T21:00:00"


def test_a_FAILED_deep_run_does_not_advance_last_deep_verified(tmp_path, monkeypatch):
    st, f = _run(tmp_path, monkeypatch, last_deep_fn=Recorder("2026-07-10T21:00:00"),
                 check_fn=Recorder(_proc(1, stderr=CHECK_DIFF)),
                 mtime_fn=lambda p: BEFORE_RUN)
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert st["last_deep_verified"] == "2026-07-10T21:00:00"


# --- the parse-failure guard -------------------------------------------------------- #
# An empty copied-path list is read as "nothing new to copy" — a verified success that
# refreshes the heartbeat. It is ALSO what a BROKEN parse_copied_paths produces. These
# pin the cross-check against rclone's own transfer stats: bytes/files moved + zero
# parsed paths = a contradiction, and a contradiction fails CLOSED.
COPY_MOVED_BUT_UNPARSEABLE = (
    # Same run as COPY_INCREMENTAL, but with the INFO lines in a wording our regex does
    # not match — i.e. exactly what a future rclone -v format change looks like here.
    "2026/07/20 21:00:31 INFO  : warehouse/raw/options/SPY/2026-07-20.parquet "
    "-> transferred (new)\n"
    "Transferred:   \t   1.234 GiB / 1.234 GiB, 100%, 10.5 MiB/s, ETA 0s\n"
    "Transferred:            2 / 2, 100%\n")


def test_incremental_zero_parsed_paths_but_bytes_moved_FAILS_CLOSED(tmp_path):
    st, f = _incremental(tmp_path, copy_out=COPY_MOVED_BUT_UNPARSEABLE,
                         last_deep="2026-07-10T21:00:00")
    assert st["copied_count"] == 0
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0          # cold heartbeat -> it pages
    assert f["check_fn"].calls == 0              # nothing could be scoped to verify
    blob = " ".join(st["errors"])
    assert "CONTRADICTION" in blob
    assert "parse_copied_paths" in blob
    assert "-v" in blob                          # names the likely cause
    assert st["proves"] == db.PROVES_FAILED_PARSE_CONTRADICTION.format(
        moved=f"{st['bytes']} byte(s) and 2 file(s)")
    assert st["bytes"] > 0


def test_the_contradiction_proves_string_claims_nothing_was_verified(tmp_path):
    st, _ = _incremental(tmp_path, copy_out=COPY_MOVED_BUT_UNPARSEABLE,
                         last_deep="2026-07-10T21:00:00")
    assert st["proves"].startswith("nothing")
    assert "verified byte-identical" not in st["proves"]
    assert "nothing new to copy" not in st["proves"]
    assert "parse_copied_paths" in st["proves"]


def test_incremental_zero_parsed_paths_and_nothing_transferred_is_still_a_success(
        tmp_path):
    """The symmetric half: rclone's stats AGREE that nothing moved, so the existing
    'nothing new' success path is untouched."""
    st, f = _incremental(tmp_path, copy_out=COPY_NOTHING_NEW,
                         last_deep="2026-07-10T21:00:00")
    assert st["copied_count"] == 0
    assert st["ok"] is True
    assert f["heartbeat_fn"].calls == 1
    assert f["check_fn"].calls == 0
    assert st["proves"] == db.PROVES_VERIFIED_NOTHING_NEW.format(
        remote=db.RCLONE_REMOTE, last_deep="2026-07-10T21:00:00")


def test_contradiction_also_trips_on_a_transferred_FILE_COUNT_alone(tmp_path):
    """Bytes unparseable (or absent) but the file count says 2 — still a contradiction."""
    out = ("INFO  : some/path.parquet -> transferred (new)\n"
           "Transferred:            2 / 2, 100%\n")
    st, f = _incremental(tmp_path, copy_out=out, last_deep="2026-07-10T21:00:00")
    assert st["bytes"] is None
    assert st["ok"] is False
    assert f["heartbeat_fn"].calls == 0
    assert st["proves"] == db.PROVES_FAILED_PARSE_CONTRADICTION.format(moved="2 file(s)")


def test_parse_transferred_files_reads_the_count_line_not_the_byte_line():
    assert db.parse_transferred_files(COPY_OK) == 5
    assert db.parse_transferred_files(COPY_INCREMENTAL) == 2
    # "0 B / 0 B" carries a size unit, so it is the BYTE line and is correctly not read
    # as a count. Unparseable -> None, which never manufactures a contradiction.
    assert db.parse_transferred_files(COPY_NOTHING_NEW) is None
    assert db.parse_transferred_files("Transferred:            0 / 0, 100%\n") == 0
    assert db.parse_transferred_files("") is None
    assert db.parse_transferred_files("Elapsed time: 1s\n") is None


def test_status_records_the_mode_and_why(tmp_path):
    st, _ = _incremental(tmp_path, last_deep="2026-07-10T21:00:00")
    assert st["mode"] == "incremental"
    assert "explicitly requested" in st["mode_why"]
