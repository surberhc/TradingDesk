r"""
data_backup.py — verified, off-machine rclone backup of C:\TradingDesk-Local.

WHY THIS EXISTS (the data analogue of repo_backup.py)
-----------------------------------------------------
C:\TradingDesk-Local holds ~99 GB / ~464k files of IRREPLACEABLE market data —
the options warehouse, downloaded ThetaData/Tiingo history, backtester inputs.
None of it is in git (it is deliberately OUTSIDE the repo and off Google Drive as a
working copy — Drive sync corrupts it). If this disk dies, that data is GONE unless a
verified copy already sits somewhere else. repo_backup.py insures the CODE; this file
insures the DATA.

THE LESSON WE INHERIT FROM repo_backup.py — read it, it is load-bearing here too:
a backup that can fail SILENTLY is not a backup. On 2026-07-16 Google Drive synced
the WRONG folder for 9 days and NO ERROR WAS EVER RAISED, because nothing *failed* —
Drive succeeded at syncing a folder that had stopped changing. So this job must PROVE
its result, and the alarm must fire on SILENCE rather than wait to be told about a
failure. Two rules fall out, and they are the design:

  1. The heartbeat is refreshed ONLY on a verified success — copy succeeded AND an
     independent checksum verification (`rclone check`) found NO tier-1 difference (see
     the three tiers below; tier 1 is real corruption / a real failed upload). Every
     failure path (rclone missing, copy error, any tier-1 check difference or error,
     anything unclassifiable) leaves the heartbeat UNTOUCHED and exits non-zero. A failed backup and a
     backup that never ran are therefore INDISTINGUISHABLE from the alarm's side —
     both go stale, both page. We never write "I failed" and hope someone reads it; we
     go silent, and silence is what heartbeat_alarm.py (job "data_backup") hunts.

  2. Nothing is assumed. `rclone copy` moving bytes proves only that rclone THINKS it
     wrote them; it is not proof the remote holds the same bytes. So after the copy we
     run `rclone check`, which recomputes and COMPARES checksums (md5, which Google
     Drive exposes server-side) of local vs remote. That comparison — not the copy's
     own exit code — is the integrity proof, the md5 ethos repo_backup.py established
     for the git bundle.

COPY, NOT SYNC — DELIBERATE, AND NON-NEGOTIABLE FOR IRREPLACEABLE DATA
---------------------------------------------------------------------
This job runs `rclone copy`, never `rclone sync`. `sync` makes the destination MIRROR
the source, which means it DELETES from the backup anything no longer present locally.
For irreplaceable data that is a foot-gun pointed at the only spare copy: a local
deletion, a corrupted local file, ransomware, or a bug that truncates the warehouse
would, on the next `sync`, faithfully propagate that destruction to the backup and
call it success. `copy` is purely ADDITIVE — it uploads new/changed files and NEVER
deletes on the remote. The cost is that files deleted locally linger in the backup
(cheap, and reviewable by hand); the benefit is that the backup cannot be nuked by
anything that happens to the local copy. For a backup whose entire job is to survive
the local copy being destroyed, that asymmetry is the whole point.

WHAT `rclone check` CAN AND CANNOT PROVE — be honest, the incident was caused by
believing a check that proved less than it appeared to:
  * `rclone check` recomputes the LOCAL file hashes and compares them against the
    remote's hashes (Google computes md5 server-side, only once content has landed).
    "0 differences, 0 errors, 0 missing" PROVES the files it examined are byte-for-byte
    identical in both places. That is a real proof, and it is the one worth having.
  * It proves nothing about EXCLUDED paths (venv/, backups/, secrets/) — those are
    intentionally not backed up and not examined; the `proves` string never claims
    them.
  * It proves nothing about TOMORROW, and nothing at all while it is UNRUN — which is
    exactly why a failure must leave the heartbeat cold rather than write a stale green
    light.

NOT EVERY DIFFERENCE IS A FAILURE — the three tiers (added 2026-07-18)
---------------------------------------------------------------------
The FIRST REAL RUN taught this the only way that counts. On 2026-07-18 the initial
99 GB backup completed: 499,539 files, 104.2 GiB, rclone copy exit 0. The follow-up
`rclone check --one-way --checksum` then exited 1 with 17 differences / 17 errors /
9 files missing. Every single one was benign live-file churn — ZERO corruption, and
ZERO .parquet data files among them:

  * 9 "file not in Google drive root": all of them under `s8_pilot/logs/`, a directory
    a concurrent S8 session CREATED at 15:35, roughly FOUR HOURS AFTER the backup began
    at 11:17. rclone cannot copy a file that did not exist when it walked that path.
  * 8 "sizes differ": files that legitimately changed between copy time and check time —
    conductor/conductor.db (written continuously all session), warehouse/heartbeat_alarm.log,
    warehouse/morning_execute.log, warehouse/register_forward_live.ps1,
    warehouse/run_forward_live.bat, state/paperbot/paperbot.log, state/paperbot/runs.jsonl,
    warehouse/raw/options/_manifest.json.

Those files churn EVERY day. The as-built job treated ANY difference as failure, so
DataBackupDaily would have failed its check EVERY NIGHT, left the heartbeat cold, and
paged — a false-page generator, the exact alarm-fatigue failure this quarter's work
exists to eliminate. So differences are now CLASSIFIED, never blanket-ignored:

  TIER 1 — PRECIOUS / IMMUTABLE -> HARD FAILURE, page.
      Anything under warehouse/raw/** (the write-once historical market data) except the
      manifest/index sidecars listed in tier 2. Once written these files must NEVER
      change; a size or md5 difference there means real corruption or a real failed
      upload. ALSO tier 1: any local file MISSING from the remote whose mtime is OLDER
      than this run's start (it existed when rclone walked, so it should have been
      copied and wasn't), any "hashes could not be checked" (unverifiable is not
      verified), any difference we cannot classify, and any reported problem we cannot
      account for with a per-file ERROR line. Tier 1 fails CLOSED, always.
  TIER 2 — KNOWN-VOLATILE -> recorded, does not fail the job.
      An EXPLICIT, commented pattern list (VOLATILE_PATTERNS below) of files whose
      content legitimately changes while the desk runs. Deliberately NOT a catch-all,
      and deliberately does NOT include *.parquet.
  TIER 3 — CREATED DURING THE RUN -> recorded, does not fail the job.
      Missing on the remote AND local mtime NEWER than the run's start timestamp: it did
      not exist when rclone walked past, and it gets copied on the next run.

Tier 2/3 items are recorded in the status JSON under `benign_differences` (counts plus a
capped sample of paths) so a human can audit WHY anything was called benign, and the
`proves` string NAMES them rather than implying a clean 100%.

TWO CADENCES — WHY THIS JOB NO LONGER RE-HASHES 99 GB EVERY NIGHT (added 2026-07-20)
------------------------------------------------------------------------------------
The as-built job did the full thing every single night: `rclone copy --checksum` followed
by a full, unscoped `rclone check`. BOTH of those recompute the md5 of EVERY local file —
~499k files / ~99 GB each — so one night cost roughly 200 GB of reads off the local disk.
The UPLOADS were always tiny and incremental (almost nothing changes day to day); it was
the VERIFICATION that was full-scope. That much disk churn runs for hours and competes
directly with the data collectors and a live S8 session for the same spindle. Paying it
nightly bought very little: the same bytes were re-proven over and over.

So the job now runs in one of two MODES, chosen by itself from the calendar (nothing in
the scheduled task changed — see choose_mode()):

  INCREMENTAL (most nights). `rclone copy` WITHOUT --checksum, so rclone decides what to
  upload from size+modtime against the remote's metadata listing — no local hashing at
  all. `-v` is added so rclone logs one INFO line per copied path; we parse those paths
  (parse_copied_paths) and write them to a file, then run `rclone check --files-from
  <that file>` so the checksum verification covers EXACTLY the files this run touched.
  If nothing was copied, the check is SKIPPED entirely — there is nothing new to prove,
  and the backup is still exactly as verified as it was yesterday.

  DEEP (Fridays, plus the self-heal below). Unchanged from the original job: full
  `copy --checksum` and a full unscoped `check`. Every file, both ends, re-hashed. This
  is the pass that can actually catch bit-rot in a file that was backed up long ago.

  WHY FRIDAY: the deep pass is the long one, and Friday night lets it run into a weekend
  when the desk is quiet — and lets the machine be turned off Saturday/Sunday without
  skipping it. If a Friday IS missed (machine off, job failed), DEEP_MAX_AGE_DAYS forces
  the next run to go deep once the last deep verification is older than that window, so a
  missed deep night self-heals instead of silently never happening again.

  WHAT INCREMENTAL GIVES UP — say it plainly, because the whole point of this file is
  that a backup must not claim more than it proved:
    (a) size+modtime can MISS a content change that preserves both. A file rewritten with
        identical length and a restored mtime would not be re-uploaded, and incremental
        would never notice. --checksum (deep) is what catches that.
    (b) Bit-rot / silent divergence in a file that was already backed up is OUT OF SCOPE
        of an incremental run — it verifies only what it just copied. Worst case, such a
        divergence goes unnoticed for one DEEP INTERVAL (a week, bounded by
        DEEP_MAX_AGE_DAYS) rather than one night. That is the trade we are making, with
        eyes open: a week of exposure to a rare failure, against hours of nightly disk
        contention that was itself an operational risk.
    (c) The copied-path PARSE is the one place in this design where a FAILURE could read
        as SUCCESS: if rclone's -v INFO wording ever changes, parse_copied_paths returns
        an empty list, and an empty list is indistinguishable from "nothing new to copy" —
        a verified success that refreshes the heartbeat while verifying nothing. So the
        empty case is cross-checked against an INDEPENDENT signal, rclone's own end-of-run
        stats (parse_transferred_bytes / parse_transferred_files): if rclone says it moved
        bytes or files while we could not name a single copied path, the two contradict
        each other, nothing can be scoped for verification, and the run fails CLOSED with
        PROVES_FAILED_PARSE_CONTRADICTION — a broken parser PAGES instead of silently
        under-verifying. Zero paths AND zero transferred is the only "nothing new" that
        still counts as a success.
    (d) Therefore an incremental run's `proves` string NEVER claims the whole warehouse is
        verified. It states what it actually proved — the N files it copied this run — and
        names the date of the last FULL verification for everything else. An honest,
        smaller claim is the entire design (see PROVES_VERIFIED_INCREMENTAL).

SECRETS
-------
The rclone remote token lives in the rclone config file (RCLONE_CONFIG, under the
off-Drive secrets folder). This job references that PATH on the command line so rclone
can authenticate, but it NEVER reads, prints, echoes, or logs the file's CONTENTS.
Nothing here surfaces a token, and rclone's own output does not carry it.

Run:
    <venv python> data_backup.py            # real run (the DataBackupDaily path); --mode
                                            #   defaults to auto -> deep on Friday (or if
                                            #   the last deep is stale), else incremental
    <venv python> data_backup.py --mode deep         # force the full re-hash pass
    <venv python> data_backup.py --mode incremental  # force the scoped pass
    <venv python> data_backup.py --dry-run  # rclone --dry-run: transfer nothing, do
                                            #   NOT verify, do NOT refresh the heartbeat

Exit codes: 0 = verified success (copy done AND check clean; heartbeat refreshed).
Non-zero = failure of some kind (rclone missing, copy error, check differences/errors),
heartbeat deliberately NOT refreshed, so heartbeat_alarm.py pages on the silence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths / tunables
# --------------------------------------------------------------------------- #
# The irreplaceable data root. This is the SOURCE of the backup — everything under it
# except the excluded paths below goes to the remote.
DATA_SOURCE = Path(os.environ.get("TRADINGDESK_DATA_ROOT", r"C:\TradingDesk-Local"))

# The rclone remote + path we back up INTO. `databackup` is a drive.file-scoped remote
# (config in the secrets folder); the destination folder is TradingDesk-DataBackup.
RCLONE_REMOTE = os.environ.get(
    "TRADINGDESK_DATA_REMOTE", "databackup:TradingDesk-DataBackup")

# The rclone config file — holds the OAuth token for the remote. Referenced by PATH
# only; its contents are NEVER read or printed by this job. Lives off-Drive with the
# other secrets.
RCLONE_CONFIG = Path(os.environ.get(
    "TRADINGDESK_RCLONE_CONFIG", r"C:\TradingDesk-Local\secrets\rclone.conf"))

# Small state the alarm reads. Lives beside repo_backup's own state under \backups\
# (this is the backup job's own bookkeeping, not warehouse data). NOTE: \backups\ is in
# the EXCLUDES below, so this job never tries to back up its own status/heartbeat/log.
BACKUP_DIR = Path(os.environ.get(
    "TRADINGDESK_BACKUP_DIR", r"C:\TradingDesk-Local\backups"))
STATUS_FILE = Path(os.environ.get(
    "TRADINGDESK_DATA_BACKUP_STATUS", str(BACKUP_DIR / "data_backup_status.json")))
HEARTBEAT_FILE = Path(os.environ.get(
    "TRADINGDESK_DATA_BACKUP_HEARTBEAT", str(BACKUP_DIR / "data_backup_heartbeat.txt")))
LOG_FILE = Path(os.environ.get(
    "TRADINGDESK_DATA_BACKUP_LOG", str(BACKUP_DIR / "data_backup.log")))

# The scoped file list an INCREMENTAL run hands to `rclone check --files-from`. It lives
# under BACKUP_DIR, which is in EXCLUDES below, so this file is NEVER itself backed up
# (and can never provoke a difference in the very check it scopes).
VERIFY_LIST_FILE = Path(os.environ.get(
    "TRADINGDESK_DATA_BACKUP_VERIFY_LIST",
    str(BACKUP_DIR / "data_backup_verify_list.txt")))

# Which weekday gets the full, everything-re-hashed DEEP pass. Mon=0 … Sun=6; 4 = FRIDAY,
# so the long pass runs into a quiet weekend and the machine can then be off Sat/Sun.
DEEP_WEEKDAY = int(os.environ.get("TRADINGDESK_DATA_BACKUP_DEEP_WEEKDAY", 4))

# The self-heal. If the last successful deep verification is older than this many days,
# the NEXT run goes deep regardless of weekday — so a missed Friday (machine off, job
# failed) cannot turn into "we quietly never deep-verified again".
DEEP_MAX_AGE_DAYS = float(os.environ.get("TRADINGDESK_DATA_BACKUP_DEEP_MAX_AGE_DAYS", 8))

# Paths NOT backed up. venv/ is reproducible; backups/ is this job's own bookkeeping
# (and would recurse); secrets/ must NEVER leave the machine. These MUST match the
# excludes the first full sync used, or `check` would compare against a differently
# scoped remote and report spurious differences.
EXCLUDES = ["venv/**", "backups/**", "secrets/**"]

# Same parallelism the first full sync used. rclone's own defaults are lower; 8/8 was
# chosen for the 464k-file warehouse.
TRANSFERS = "8"
CHECKERS = "8"

# rclone can run for HOURS on the full warehouse (99 GB / 464k files). This timeout is
# pure deadlock insurance, NOT an expected duration — it must sit comfortably above the
# real worst case. The scheduled task's ExecutionTimeLimit (12h) is the outer bound;
# keep this at/under it.
RCLONE_TIMEOUT = int(os.environ.get("TRADINGDESK_DATA_BACKUP_TIMEOUT_S", 12 * 3600))

# --------------------------------------------------------------------------- #
# The status file's `proves` string — the artifact's honesty, one variant per outcome.
# Each must be LITERALLY TRUE of the run that carries it. Overstating one is the exact
# bug class this whole body of work exists to kill, so they are constants pinned by
# tests, not strings improvised at each call site.
# --------------------------------------------------------------------------- #
PROVES_VERIFIED = (
    "{n} files verified byte-identical (md5) between {src} and {remote} — rclone copy "
    "completed and rclone check reported 0 differences and 0 errors")
PROVES_VERIFIED_NO_COUNT = (
    "files verified byte-identical (md5) between {src} and {remote} — rclone copy "
    "completed and rclone check reported 0 differences and 0 errors (matching-file "
    "count was not parseable from rclone's output, but the clean check is the proof)")
PROVES_DRY_RUN = (
    "nothing — this was a --dry-run (rclone --dry-run): no data was transferred and NO "
    "checksum verification was performed, so an up-to-date off-machine copy is NOT proven")
PROVES_VERIFIED_WITH_BENIGN = (
    "{n} files verified byte-identical (md5) between {src} and {remote}; {m} benign "
    "differences ({v} known-volatile live files that changed during the run, {c} files "
    "created after the run began) — 0 mismatches in the immutable warehouse/raw data. "
    "Those {m} files are NOT proven current in the backup and will be re-copied next "
    "run; everything else IS proven byte-identical")
# --- INCREMENTAL variants. These must claim LESS, and say so out loud: an incremental
# run verified only the files it copied, and it says when everything else was last
# actually proven. Never let one of these grow a sentence about the whole warehouse.
PROVES_VERIFIED_INCREMENTAL = (
    "the {n} file(s) newly copied by this run were verified byte-identical (md5) between "
    "{src} and {remote} — rclone check was scoped to exactly those files (--files-from) "
    "and reported 0 differences and 0 errors. The REST of the backup was NOT re-verified "
    "by this run; its last full byte-for-byte verification was {last_deep}")
PROVES_VERIFIED_INCREMENTAL_WITH_BENIGN = (
    "the {n} file(s) newly copied by this run were verified byte-identical (md5) between "
    "{src} and {remote}; {m} benign differences ({v} known-volatile live files that "
    "changed during the run, {c} files created after the run began) — 0 mismatches in the "
    "immutable warehouse/raw data. Those {m} files are NOT proven current in the backup "
    "and will be re-copied next run. The REST of the backup was NOT re-verified by this "
    "run; its last full byte-for-byte verification was {last_deep}")
PROVES_VERIFIED_NOTHING_NEW = (
    "rclone found nothing new to copy, so NO files needed verification this run and none "
    "was performed — the backup at {remote} is unchanged since the last run, and its last "
    "full byte-for-byte verification was {last_deep}")
PROVES_FAILED_PARSE_CONTRADICTION = (
    "nothing — rclone's own end-of-run stats say this run TRANSFERRED data ({moved}), yet "
    "not a single copied path could be parsed out of its -v output, so there was nothing "
    "to hand `rclone check --files-from` and NO file was verified. The most likely cause "
    "is that rclone's -v INFO line format changed and parse_copied_paths no longer "
    "matches it; until that parser is updated an incremental run cannot scope its "
    "verification, so this run is failed CLOSED rather than reported as 'nothing new'")
PROVES_FAILED = (
    "nothing — this run did not complete a verified data backup; read `errors` for the "
    "failure (rclone copy and rclone check did not BOTH fully succeed), so an "
    "up-to-date, checksum-verified off-machine copy is NOT proven")
PROVES_FAILED_TIER1 = (
    "nothing — the checksum verification found {k} TIER-1 failure(s) that are NOT benign "
    "churn: {detail}. A difference in write-once data (warehouse/raw/**), a file that "
    "existed before the run yet is missing from the remote, an unverifiable hash, or an "
    "unclassifiable difference all mean the remote copy is NOT proven byte-identical")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Resolving the rclone binary — robust, never version-pinned, fails loud
# --------------------------------------------------------------------------- #
# WHY THIS IS NOT A HARDCODED PATH: winget installs the binary under a versioned
# directory (…\Packages\Rclone.Rclone_*\rclone-vX.Y.Z-windows-amd64\rclone.exe). That
# path CHANGES on every rclone update, so hardcoding it is a landmine that goes off the
# next time rclone upgrades — the job would then "fail to find rclone" for no visible
# reason. We resolve, in order of preference:
#   1. TRADINGDESK_RCLONE env override (escape hatch / tests).
#   2. The version-STABLE winget shim  %LOCALAPPDATA%\Microsoft\WinGet\Links\rclone.exe
#      — winget keeps this pointer current across updates; it is the right answer when
#      it exists.
#   3. rclone on PATH (shutil.which).
#   4. LAST RESORT: glob the winget Packages dir and take the NEWEST match by mtime
#      (mtime, not name — "v1.9" sorts lexically after "v1.74", which would pick the
#      older binary). This covers this machine today, where the Links shim is absent.
# If none resolve, return (None, reason) and the caller FAILS LOUD — a backup job that
# cannot find its own tool must page, not limp on.
def resolve_rclone(*, which_fn=None, glob_fn=None) -> tuple[str | None, str]:
    which_fn = which_fn or shutil.which

    env = os.environ.get("TRADINGDESK_RCLONE")
    if env and Path(env).is_file():
        return env, f"resolved from TRADINGDESK_RCLONE ({env})"

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        shim = Path(local_appdata) / "Microsoft" / "WinGet" / "Links" / "rclone.exe"
        if shim.is_file():
            return str(shim), f"resolved via version-stable winget shim ({shim})"

    on_path = which_fn("rclone")
    if on_path:
        return on_path, f"resolved on PATH ({on_path})"

    # Fallback: search the winget Packages tree.
    if local_appdata:
        pkgs = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
        pattern = "Rclone.Rclone_*/rclone-*/rclone.exe"
        try:
            matches = list(glob_fn(pkgs, pattern) if glob_fn else pkgs.glob(pattern))
        except OSError:
            matches = []
        matches = [m for m in matches if Path(m).is_file()]
        if matches:
            # Newest by mtime — robust to version-string sort order.
            newest = max(matches, key=lambda m: Path(m).stat().st_mtime)
            return str(newest), f"resolved from winget Packages dir ({newest})"

    return None, ("rclone binary NOT FOUND — checked TRADINGDESK_RCLONE, the winget "
                  "shim (%LOCALAPPDATA%\\Microsoft\\WinGet\\Links\\rclone.exe), PATH, "
                  "and the winget Packages directory. Install rclone or set "
                  "TRADINGDESK_RCLONE.")


# --------------------------------------------------------------------------- #
# rclone argv construction
# --------------------------------------------------------------------------- #
def _base_flags() -> list[str]:
    """Flags shared by copy AND check, so the check verifies EXACTLY the scope that was
    copied. A mismatch here (e.g. a different exclude) would make check compare against
    a differently-scoped remote and report spurious differences — a false page."""
    flags: list[str] = []
    for ex in EXCLUDES:
        flags += ["--exclude", ex]
    flags += ["--transfers", TRANSFERS, "--checkers", CHECKERS, "--fast-list",
              "--config", str(RCLONE_CONFIG)]
    return flags


def copy_argv(rclone_path: str, dry_run: bool = False, deep: bool = True) -> list[str]:
    """`rclone copy SRC REMOTE` — additive, never deletes on the remote (see the
    COPY-NOT-SYNC note in the module docstring). --drive-use-trash=false so an overwrite
    frees space instead of filling Drive's trash.

    deep=True (the DEFAULT, so every pre-existing caller keeps the original behaviour):
        --checksum forces rclone to hash every local file to decide what to upload. This
        is the expensive pass — ~99 GB of local reads — and it is why it is now weekly.
    deep=False (incremental): NO --checksum, so rclone decides from size+modtime against
        the remote's metadata listing and hashes nothing locally. -v is added so rclone
        logs one INFO line per copied path; parse_copied_paths() turns those lines into
        the scoped file list the follow-up check verifies.
    """
    argv = [rclone_path, "copy", str(DATA_SOURCE), RCLONE_REMOTE]
    argv += _base_flags()
    if deep:
        argv += ["--checksum", "--drive-use-trash=false"]
    else:
        argv += ["-v", "--drive-use-trash=false"]
    if dry_run:
        argv += ["--dry-run"]
    return argv


def check_argv(rclone_path: str, files_from: str | None = None) -> list[str]:
    """`rclone check SRC REMOTE` — recompute + compare hashes (md5) local vs remote.
    This is the integrity proof. rclone check compares by hash by default when both
    ends support it (Google Drive does), so no extra flag is needed to make it a
    checksum comparison.

    files_from (incremental mode only) scopes the check to exactly the paths listed in
    that file — the ones this run actually copied. With no files_from the check is the
    original full-scope pass over everything.
    """
    argv = [rclone_path, "check", str(DATA_SOURCE), RCLONE_REMOTE]
    argv += _base_flags()
    if files_from:
        argv += ["--files-from", str(files_from)]
    return argv


def _run_copy(rclone_path: str, dry_run: bool, deep: bool = True):
    return subprocess.run(copy_argv(rclone_path, dry_run, deep=deep), capture_output=True,
                          text=True, timeout=RCLONE_TIMEOUT, check=False)


def _run_check(rclone_path: str, files_from: str | None = None):
    return subprocess.run(check_argv(rclone_path, files_from=files_from),
                          capture_output=True, text=True, timeout=RCLONE_TIMEOUT,
                          check=False)


# --------------------------------------------------------------------------- #
# Mode selection — deep (full re-hash) vs incremental (verify only what we copied)
# --------------------------------------------------------------------------- #
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
                  "Sunday")


def _coerce_dt(value) -> dt.datetime | None:
    """datetime | ISO string | None -> datetime | None. An UNPARSEABLE string comes back
    as None ON PURPOSE: None means "we cannot prove when the last deep pass was", and the
    caller then chooses DEEP. Failing toward the more thorough pass is the safe direction;
    the cost of a needless deep run is disk time, the cost of a needlessly skipped one is
    unverified data."""
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.strip())
        except (ValueError, TypeError):
            return None
    return None


def choose_mode(now, last_deep, requested: str = "auto") -> tuple[str, str]:
    """-> (mode, why). mode is "deep" or "incremental"; why is a human sentence recorded
    in the status file so a reader can always see WHY this run was scoped the way it was.

    The rules, in order:
      1. An explicit --mode wins, verbatim. The operator is allowed to override.
      2. Never deep-verified -> deep. There is no baseline to be incremental against.
      3. Last deep older than DEEP_MAX_AGE_DAYS -> deep. This is the self-heal for a
         missed deep night.
      4. It is DEEP_WEEKDAY (Friday) -> deep. The scheduled full pass.
      5. Otherwise incremental.
    """
    if requested in ("deep", "incremental"):
        return requested, (f"mode '{requested}' was explicitly requested via --mode, so "
                           f"the weekday/age rules were not consulted")

    parsed = _coerce_dt(last_deep)
    if parsed is not None and parsed.tzinfo is not None and now.tzinfo is None:
        parsed = parsed.replace(tzinfo=None)

    if parsed is None:
        return "deep", ("no usable record of a previous successful deep verification "
                        "(last_deep_verified is absent or unreadable), so this run does "
                        "the FULL pass — there is no verified baseline to be incremental "
                        "against")

    age_days = (now - parsed).total_seconds() / 86400.0
    if age_days > DEEP_MAX_AGE_DAYS:
        return "deep", (f"the last full verification was {parsed:%Y-%m-%d %H:%M:%S} "
                        f"({age_days:.1f} days ago), older than DEEP_MAX_AGE_DAYS="
                        f"{DEEP_MAX_AGE_DAYS} — this is the self-heal, so a missed deep "
                        f"night is picked up on the next run instead of never happening")
    if now.weekday() == DEEP_WEEKDAY:
        return "deep", (f"today is {_WEEKDAY_NAMES[DEEP_WEEKDAY]}, the scheduled deep "
                        f"verification night (DEEP_WEEKDAY={DEEP_WEEKDAY})")
    return "incremental", (f"not {_WEEKDAY_NAMES[DEEP_WEEKDAY]} and the last full "
                           f"verification ({parsed:%Y-%m-%d %H:%M:%S}, {age_days:.1f} "
                           f"days ago) is still within DEEP_MAX_AGE_DAYS="
                           f"{DEEP_MAX_AGE_DAYS}, so only the files copied this run are "
                           f"verified")


def read_last_deep(status_file=None) -> str | None:
    """The previous run's `last_deep_verified` out of the status JSON, or None.

    NEVER raises: a missing, unreadable, or malformed status file simply means "unknown",
    and unknown resolves to a DEEP run upstream — the safe direction.
    """
    try:
        raw = Path(status_file or STATUS_FILE).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 — any failure here means "unknown", never a crash
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("last_deep_verified")
    return value if isinstance(value, str) else None


# --------------------------------------------------------------------------- #
# Incremental mode: what did the copy actually copy?
# --------------------------------------------------------------------------- #
# rclone at -v logs one INFO line per transferred object, e.g.
#   2026/07/20 21:00:31 INFO  : warehouse/raw/options/spy/2026-07-20.parquet: Copied (new)
#   2026/07/20 21:00:31 INFO  : state/paperbot/runs.jsonl: Copied (replaced existing)
# The path itself may contain colons, so the path group is non-greedy and the reason must
# START with "Copied" — that anchors the split at the right colon. Non-"Copied" INFO lines
# (Deleted, Updated, "There was nothing to transfer", stat blocks) are deliberately not
# matched: only bytes we actually pushed this run need verifying.
_COPIED_RE = re.compile(r"INFO\s*:\s*(?P<path>.+?)\s*:\s*(?P<reason>Copied\b.*?)\s*$")


def parse_copied_paths(text: str) -> list[str]:
    """rclone -v copy output -> the de-duplicated, ORDER-PRESERVED list of copied paths.

    Forward-slashed and leading './' stripped, but CASE IS PRESERVED — deliberately NOT
    normalise_path(), which lower-cases. `rclone check --files-from` matches paths
    case-sensitively, so a lower-cased list would silently fail to match real files and
    the check would verify nothing while looking clean. That would be exactly the silent
    green light this job exists to prevent.
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        m = _COPIED_RE.search(line)
        if not m:
            continue
        p = m.group("path").strip().strip('"').replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        p = p.lstrip("/")
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def write_verify_list(paths, path=None) -> Path:
    """Write the scoped file list for `rclone check --files-from`, one path per line.

    Lands in BACKUP_DIR, which is in EXCLUDES, so this file is never itself backed up and
    can never show up as a difference in the very check it scopes.
    """
    target = Path(path or VERIFY_LIST_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        for p in paths:
            f.write(f"{p}\n")
    return target


# --------------------------------------------------------------------------- #
# Parsing rclone output
# --------------------------------------------------------------------------- #
_CHECK_PATTERNS = {
    "differences": re.compile(r"(\d+)\s+differences found", re.IGNORECASE),
    "matching": re.compile(r"(\d+)\s+matching files", re.IGNORECASE),
    "missing_dst": re.compile(r"(\d+)\s+files missing on (?:the )?destination",
                              re.IGNORECASE),
    "missing_src": re.compile(r"(\d+)\s+files missing on (?:the )?source",
                              re.IGNORECASE),
    "errors": re.compile(r"(\d+)\s+errors?\b", re.IGNORECASE),
    "hashes_unchecked": re.compile(r"(\d+)\s+hashes could not be checked",
                                   re.IGNORECASE),
}


def parse_check_output(text: str) -> dict:
    """Pull the counts out of `rclone check`'s summary lines. -> dict of int|None.

    rclone check writes a summary to stderr, e.g.:
        NOTICE: <remote>: 0 differences found
        NOTICE: <remote>: 464123 matching files
    and, when something is wrong:
        ERROR:  <remote>: 3 differences found
        ERROR:  <remote>: 2 files missing on destination
        ERROR:  <remote>: 1 hashes could not be checked

    A count that never appears comes back as None (not 0) — "we could not find the line"
    is not the same as "the line said zero", and the success decision must not launder
    one into the other. `problems` is the sum of every count that indicates trouble,
    treating a MISSING count as 0 for that sum (so absence never fabricates a problem);
    the caller ALSO gates on rclone's exit code, which is authoritative, so a parse that
    misses a bad line still fails via the return code.
    """
    text = text or ""
    out: dict = {}
    for key, pat in _CHECK_PATTERNS.items():
        m = pat.search(text)
        out[key] = int(m.group(1)) if m else None

    def _n(v):
        return v if isinstance(v, int) else 0

    out["problems"] = (_n(out["differences"]) + _n(out["missing_dst"]) +
                       _n(out["missing_src"]) + _n(out["errors"]) +
                       _n(out["hashes_unchecked"]))
    # A compact human summary for the status file / heartbeat.
    parts = []
    for key in ("matching", "differences", "missing_dst", "missing_src", "errors",
                "hashes_unchecked"):
        if out[key] is not None:
            parts.append(f"{key}={out[key]}")
    out["summary"] = ", ".join(parts) if parts else "no rclone check summary parsed"
    return out


# --------------------------------------------------------------------------- #
# Classifying differences — tier 1 (fail closed) vs tier 2/3 (benign, recorded)
#
# READ THE THREE-TIER SECTION OF THE MODULE DOCSTRING BEFORE EDITING ANY OF THIS.
# The whole point of this job is that a real corruption of irreplaceable data pages
# loudly. Every rule below is written to FAIL CLOSED: a difference is benign only if it
# matches an explicit, justified rule, and everything else is a tier-1 failure.
# --------------------------------------------------------------------------- #

# TIER 1 territory: write-once historical market data. Once a file lands here its bytes
# must never change again, so ANY size/hash difference is corruption or a failed upload.
PRECIOUS_PREFIXES = ("warehouse/raw/",)

# TIER 2: files whose content legitimately changes while the desk runs, so a difference
# between copy time and check time is expected churn rather than corruption. Matched with
# fnmatch against the forward-slashed, lower-cased path relative to DATA_SOURCE, so `*`
# DOES cross directory separators (i.e. "*.log" means "any .log anywhere").
#
# DELIBERATELY EXPLICIT, one line per reason — NOT a catch-all. Each entry is either a
# file class observed churning in the 2026-07-18 first real run or an obvious sibling of
# one. *.parquet is deliberately ABSENT: parquet is the data we are insuring, and a
# parquet difference must always page.
VOLATILE_PATTERNS = (
    # Observed: warehouse/heartbeat_alarm.log, warehouse/morning_execute.log,
    # state/paperbot/paperbot.log. Logs are appended to continuously by running jobs.
    "*.log",
    # Observed: state/paperbot/runs.jsonl. Append-only run journals.
    "*.jsonl",
    # Observed: conductor/conductor.db, written to all session long. The -wal/-shm
    # sidecars are SQLite's own journal files and churn even harder than the db itself.
    "*.db", "*.db-wal", "*.db-shm",
    "*.sqlite", "*.sqlite-wal", "*.sqlite-shm",
    # Observed: warehouse/raw/options/_manifest.json — an INDEX of the warehouse, not
    # warehouse data. It is rewritten every time the collector adds a file, so it churns
    # by design. This is the ONE documented exception to the tier-1 warehouse/raw rule,
    # and it is why the sidecar patterns are checked BEFORE the precious-prefix rule.
    "*_manifest.json", "*manifest*.json",
    # Heartbeat files exist to be re-stamped; that is their entire job.
    "*heartbeat*",
    # Progress/state bookkeeping the long-running collectors rewrite as they advance.
    "*_progress*.json", "*_state.json",
    # state/last_email_ok.txt is re-stamped every time the alarm sweep sends an email.
    # It changed mid-run during the 2026-07-20 deep-verify (copy 21:00 -> check 02:25),
    # tripping a false "sizes differ" hard-failure with the warehouse itself intact.
    # Exact path (like the .ps1/.bat below) so an unexpected change elsewhere still pages.
    "state/last_email_ok.txt",
    # Lock files appear/disappear/change for the life of a running job.
    "*.lock",
    # Observed: all 9 of the "file not in Google drive root" entries were under here.
    # S8's pilot logs are written live by a session that may run concurrently with the
    # backup. (Missing ones are caught by the tier-3 mtime rule; this pattern covers the
    # case where an s8_pilot log merely CHANGED mid-run.)
    "s8_pilot/logs/*",
    # Observed changing: two warehouse scripts that a running job rewrites in place.
    # Listed by exact path rather than by extension so a genuinely unexpected .ps1/.bat
    # change elsewhere still pages.
    "warehouse/register_forward_live.ps1",
    "warehouse/run_forward_live.bat",
)

# One ERROR line per problem file, e.g.
#   2026/07/18 15:41:02 ERROR : conductor/conductor.db: sizes differ
#   2026/07/18 15:41:02 ERROR : warehouse/raw/x.parquet: md5 differ
#   2026/07/18 15:41:02 ERROR : s8_pilot/logs/a.log: file not in Google drive root 'X'
# rclone's SUMMARY lines are ERROR lines too ("...: 17 differences found"), so lines
# whose "reason" starts with a number are skipped as summaries, not treated as files.
_CHECK_ERROR_RE = re.compile(r"ERROR\s*:\s*(?P<path>.+?)\s*:\s*(?P<reason>[^:]+?)\s*$")
_SUMMARY_REASON_RE = re.compile(r"^\d+\s")


def normalise_path(p: str) -> str:
    """rclone-relative path -> forward-slashed, lower-cased, no leading './'."""
    s = (p or "").strip().strip('"').replace("\\", "/").lower()
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def parse_check_errors(text: str) -> list[dict]:
    """Per-file problems from `rclone check` output -> [{path, reason, kind}].

    kind is "differ" (size or hash mismatch), "missing" (local file not present on the
    remote), or "unknown" — and "unknown" is deliberately a TIER-1 failure downstream,
    because a reason we do not understand is not a reason we may forgive.
    """
    found: list[dict] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        m = _CHECK_ERROR_RE.search(line)
        if not m:
            continue
        reason = m.group("reason").strip()
        if _SUMMARY_REASON_RE.match(reason):
            continue                      # rclone's own summary line, not a file
        path = normalise_path(m.group("path"))
        if not path or path in seen:
            continue
        low = reason.lower()
        if "differ" in low:
            kind = "differ"               # "sizes differ", "md5 differ", "hashes differ"
        elif low.startswith("file not in"):
            kind = "missing"
        else:
            kind = "unknown"
        seen.add(path)
        found.append({"path": path, "reason": reason, "kind": kind})
    return found


def is_volatile(path: str) -> bool:
    """True iff the path matches an EXPLICIT known-volatile pattern (tier 2)."""
    p = normalise_path(path)
    return any(fnmatch.fnmatchcase(p, pat) for pat in VOLATILE_PATTERNS)


def is_precious(path: str) -> bool:
    """True iff the path is write-once market data (tier 1 territory)."""
    p = normalise_path(path)
    return any(p.startswith(pre) for pre in PRECIOUS_PREFIXES)


def _default_mtime(rel_path: str) -> float | None:
    try:
        return (DATA_SOURCE / rel_path).stat().st_mtime
    except OSError:
        return None


def classify_difference(diff: dict, *, run_start: float, mtime_fn=None) -> dict:
    """One parsed difference -> the same dict plus `tier` (1/2/3) and `why`.

    run_start is the POSIX timestamp captured at the TOP of the run, before rclone was
    launched. Anything created after that instant could not have been copied.
    """
    mtime_fn = mtime_fn or _default_mtime
    path, kind = diff["path"], diff["kind"]
    out = dict(diff)

    if kind == "missing":
        # TIER 3 vs TIER 1 turns entirely on WHEN the local file came into existence.
        mtime = mtime_fn(path)
        if mtime is None:
            out.update(tier=1, why="missing from the remote and its local mtime could "
                                   "not be read, so we cannot prove it was created "
                                   "after the run began — failing closed")
        elif mtime >= run_start:
            out.update(tier=3, why=f"created during the run (local mtime "
                                   f"{dt.datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M:%S} "
                                   f"is after the run start "
                                   f"{dt.datetime.fromtimestamp(run_start):%Y-%m-%d %H:%M:%S}), "
                                   f"so rclone could not have copied it; next run will")
        else:
            out.update(tier=1, why=f"missing from the remote although it existed BEFORE "
                                   f"the run started (local mtime "
                                   f"{dt.datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M:%S}) "
                                   f"— it should have been backed up and was not")
        return out

    if kind == "differ":
        # Sidecars first: an index/manifest that lives INSIDE warehouse/raw is still an
        # index, not warehouse data, and it is rewritten by design.
        if is_volatile(path):
            out.update(tier=2, why="known-volatile file (matches an explicit "
                                   "VOLATILE_PATTERNS entry) — its content legitimately "
                                   "changed between the copy and the check")
            return out
        if is_precious(path):
            out.update(tier=1, why="WRITE-ONCE market data under warehouse/raw/ changed "
                                   "— this is corruption or a failed upload, never churn")
            return out
        out.update(tier=1, why="changed file that matches no known-volatile pattern — "
                               "failing closed; if this is legitimate churn, add it to "
                               "VOLATILE_PATTERNS deliberately, with a reason")
        return out

    out.update(tier=1, why=f"unrecognised rclone check reason {diff['reason']!r} — a "
                           f"reason we do not understand is not one we may forgive")
    return out


def classify_differences(diffs, *, run_start: float, mtime_fn=None) -> list[dict]:
    return [classify_difference(d, run_start=run_start, mtime_fn=mtime_fn)
            for d in diffs]


# How many problem paths the summary says there should be. rclone's counts OVERLAP
# ("17 differences" and "17 errors" describe the same 17 files, 9 of which are also the
# "9 missing"), so the requirement is the MAX of the counts, not their sum. If we parse
# fewer per-file ERROR lines than that, some reported problem is unexplained — and an
# unexplained problem is a TIER-1 failure, never a benign one.
def required_accounted(parsed: dict) -> int:
    def _n(v):
        return v if isinstance(v, int) else 0
    return max(_n(parsed.get("differences")), _n(parsed.get("errors")),
               _n(parsed.get("missing_dst")), _n(parsed.get("missing_src")))


# Cap on how many benign paths land in the status JSON. Enough to audit by hand, never
# thousands of lines of churn in a status file.
BENIGN_SAMPLE_CAP = 25


_SIZE_UNITS = {"B": 1, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3,
               "TIB": 1024**4, "PIB": 1024**5,
               "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}
_TRANSFERRED_RE = re.compile(
    r"Transferred:\s*([\d.]+)\s*([KMGTP]?i?B)\s*/", re.IGNORECASE)


def parse_transferred_bytes(text: str) -> int | None:
    """Best-effort bytes-transferred from rclone copy's end-of-run stats. -> int|None.

    rclone prints two 'Transferred:' lines — a byte total (has a size unit and a '/')
    and a file count (no unit). We match the byte one. Best-effort ONLY: this figure is
    for the status file's information, never for the success decision, so an unparseable
    value is None and nothing downstream cares.
    """
    if not text:
        return None
    m = _TRANSFERRED_RE.search(text)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2).upper()
    mult = _SIZE_UNITS.get(unit)
    if mult is None:
        return None
    return int(value * mult)


# The OTHER 'Transferred:' line — the FILE COUNT, e.g. "Transferred:  5 / 5, 100%".
# It is distinguishable from the byte line because the byte line always carries a size
# unit before the '/', so a bare integer immediately followed by '/' can only be the
# count. Best-effort in the SAFE direction: an unparseable value is None, and None never
# manufactures a failure — this figure is only ever used to CONTRADICT a claim that
# nothing was copied.
_TRANSFERRED_FILES_RE = re.compile(r"Transferred:\s*(\d+)\s*/\s*\d+", re.IGNORECASE)


def parse_transferred_files(text: str) -> int | None:
    """Best-effort file COUNT from rclone copy's end-of-run stats. -> int|None."""
    if not text:
        return None
    m = _TRANSFERRED_FILES_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Status / heartbeat — heartbeat refreshed ONLY on verified success
# --------------------------------------------------------------------------- #
def write_status(payload: dict, *, status_file=None) -> None:
    """Atomically write the status JSON. Written on success AND failure (it is the
    forensic record). The HEARTBEAT is what gates the alarm and is success-only."""
    path = Path(status_file or STATUS_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, indent=2, default=str))
        os.replace(tmp, path)
    except OSError as e:
        log(f"could not write status file ({e!r})")


def touch_heartbeat(text: str, *, heartbeat_file=None) -> None:
    """Refresh the heartbeat the alarm watches. CALL THIS ONLY ON VERIFIED SUCCESS.

    Its mtime is the "last known-good verified data backup" clock heartbeat_alarm.py
    (job "data_backup") reads. Refreshing it on a FAILED or PARTIAL backup would re-arm
    the alarm's freshness window and manufacture the exact silent green light this job
    exists to prevent. If the backup did not fully verify, this file stays untouched and
    goes cold.

    (Deliberate wording note: the text must not contain the literal word "COMPLETE" —
    heartbeat_alarm.assess() treats a "COMPLETE" marker in heartbeat text as a
    finished-job signal. Harmless here, but we keep the semantics unambiguous.)
    """
    path = Path(heartbeat_file or HEARTBEAT_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        log(f"could not write heartbeat ({e!r})")


# --------------------------------------------------------------------------- #
# The job — injectable so tests drive it offline (no real rclone, no transfers)
# --------------------------------------------------------------------------- #
def run_backup(*, now=None, dry_run: bool = False, mode: str = "auto", resolve_fn=None,
               copy_fn=None, check_fn=None, parse_fn=None, bytes_fn=None, status_fn=None,
               heartbeat_fn=None, log_fn=None, errors_fn=None, mtime_fn=None,
               run_start=None, last_deep_fn=None, copied_fn=None,
               verify_list_fn=None) -> dict:
    """One data-backup run. Returns the status dict. Never raises for policy reasons.

    THE INVARIANT, stated once so it cannot be lost in a refactor:
        ok=True  <=>  rclone is resolved,
                      AND `rclone copy` exited 0,
                      AND `rclone check` produced ZERO TIER-1 failures — every reported
                          difference was accounted for by a per-file ERROR line AND
                          classified benign (tier 2 known-volatile churn, or tier 3
                          created-after-the-run-started), with no unverifiable hashes.
        heartbeat is refreshed IF AND ONLY IF ok=True.
    Any other outcome leaves the heartbeat cold so the alarm fires on silence.

    Benign churn does NOT block a good backup (that is the 2026-07-18 false-page fix),
    but it is recorded in `benign_differences` and NAMED in `proves` — a run with benign
    differences never claims a clean 100%.

    A --dry-run passes rclone --dry-run to copy, runs NO check, and is NEVER a success
    (ok stays False, heartbeat untouched) — the same contract repo_backup.py uses.

    MODE ("auto" | "deep" | "incremental"; see choose_mode and the TWO CADENCES section of
    the module docstring). Deep behaves exactly as this job always has. Incremental scopes
    the verification to the files this run copied, and its `proves` string claims only
    those — it NEVER claims the whole warehouse. The tier classification, the accounting
    gates, and the heartbeat invariant above are IDENTICAL in both modes; only the SCOPE
    of what was verified differs, and the scope is stated honestly in `proves`.
    """
    log_fn = log_fn or log
    resolve_fn = resolve_fn or resolve_rclone
    copy_fn = copy_fn or _run_copy
    check_fn = check_fn or _run_check
    parse_fn = parse_fn or parse_check_output
    bytes_fn = bytes_fn or parse_transferred_bytes
    status_fn = status_fn or write_status
    heartbeat_fn = heartbeat_fn or touch_heartbeat
    errors_fn = errors_fn or parse_check_errors
    last_deep_fn = last_deep_fn or read_last_deep
    copied_fn = copied_fn or parse_copied_paths
    verify_list_fn = verify_list_fn or write_verify_list

    now = now or dt.datetime.now()
    # Captured BEFORE rclone is launched. Tier 3 ("created during the run") is decided
    # against this instant, so it must be the earliest possible moment of the run.
    run_start = run_start if run_start is not None else now.timestamp()

    # Resolve the mode FIRST: it decides the copy flags, so nothing may run before it.
    prev_deep = last_deep_fn()
    mode, mode_why = choose_mode(now, prev_deep, mode)
    # Normalised to a string for the status file, and carried forward on EVERY run so a
    # run that is not deep can never erase the record of when the last deep pass was.
    carried_deep = _coerce_dt(prev_deep)
    carried_deep = carried_deep.isoformat(timespec="seconds") if carried_deep else None
    last_deep_display = carried_deep or "never"

    st: dict = {
        "job": "data_backup",
        "timestamp": now.isoformat(timespec="seconds"),
        "ok": False,
        "source": str(DATA_SOURCE),
        "remote": RCLONE_REMOTE,
        "rclone_path": None,
        "rclone_note": None,
        # Which cadence this run used, and WHY — recorded so a reader of the status file
        # never has to guess how much this run actually verified.
        "mode": mode,
        "mode_why": mode_why,
        # Carried forward on every run; only a SUCCESSFUL DEEP run advances it.
        "last_deep_verified": carried_deep,
        # Incremental only: how many paths rclone reported copying (None in deep mode).
        "copied_count": None,
        "copy_returncode": None,
        "check_returncode": None,
        "files_checked": None,
        "bytes": None,
        "check_result": None,
        "differences": None,
        "run_start": dt.datetime.fromtimestamp(run_start).isoformat(timespec="seconds"),
        # Benign (tier 2/3) churn: counts + a capped, audit-able sample with the REASON
        # each path was forgiven. Stays None when there was nothing to classify.
        "benign_differences": None,
        # Tier-1 failures, verbatim, so the page says WHAT broke.
        "hard_failures": [],
        "errors": [],
        "dry_run": bool(dry_run),
        # Stated in the artifact itself so nobody reading this file later mistakes it for
        # more than it is. Starts at "nothing proven" and is only ever RAISED by a check
        # that actually passed.
        "proves": PROVES_FAILED,
    }

    def _fail(msg: str) -> dict:
        st["errors"].append(msg)
        st["ok"] = False
        st["proves"] = PROVES_FAILED
        log_fn(f"FAIL: {msg}")
        status_fn(st)
        return st

    # 1. Resolve rclone. No tool -> loud failure, cold heartbeat.
    rclone_path, rnote = resolve_fn()
    st["rclone_path"] = rclone_path
    st["rclone_note"] = rnote
    if not rclone_path:
        return _fail(rnote)
    log_fn(f"rclone {rnote}")
    log_fn(f"mode={mode} ({mode_why}); last full verification: {last_deep_display}")

    # 2. Copy (additive; never deletes on the remote). --dry-run passes through.
    #    `deep` goes by KEYWORD so the positional (rclone_path, dry_run) contract that
    #    existing callers and test doubles rely on is untouched.
    try:
        proc = copy_fn(rclone_path, dry_run, deep=(mode == "deep"))
    except subprocess.TimeoutExpired as e:
        return _fail(f"rclone copy TIMED OUT after {RCLONE_TIMEOUT}s ({e!r})")
    except Exception as e:  # noqa: BLE001
        return _fail(f"rclone copy raised {e!r}")
    st["copy_returncode"] = getattr(proc, "returncode", None)
    combined = f"{getattr(proc, 'stdout', '') or ''}\n{getattr(proc, 'stderr', '') or ''}"
    st["bytes"] = bytes_fn(combined)
    if st["copy_returncode"] != 0:
        tail = combined.strip()[-500:]
        return _fail(f"rclone copy FAILED (exit {st['copy_returncode']}): {tail}")
    log_fn(f"rclone copy exited 0 (bytes transferred this run: {st['bytes']})")

    # 3. Dry-run stops here: nothing was really transferred, so there is nothing to
    #    verify and the heartbeat must NOT move.
    if dry_run:
        st["ok"] = False
        st["proves"] = PROVES_DRY_RUN
        st["errors"].append("dry-run: transferred nothing (rclone --dry-run), did NOT "
                            "verify, heartbeat NOT refreshed")
        log_fn("dry-run: copy ran with --dry-run; skipping check and heartbeat")
        status_fn(st)
        return st

    # 4. VERIFY. The copy exiting 0 says rclone THINKS it wrote the bytes; it is not
    #    proof the remote holds the same bytes. `rclone check` recomputes and compares
    #    checksums (md5) local vs remote — THIS is the integrity proof, not step 2.
    #
    #    DEEP verifies everything (files_from stays None). INCREMENTAL verifies exactly
    #    the paths this run copied, and if it copied NOTHING there is nothing to verify:
    #    that is a legitimate verified state (the backup is unchanged and therefore still
    #    as current as it was), not a skipped check — and `proves` says so precisely.
    files_from = None
    if mode != "deep":
        copied = copied_fn(combined)
        st["copied_count"] = len(copied)
        log_fn(f"incremental: rclone reported {len(copied)} copied path(s)")
        if not copied:
            # THE PARSE-FAILURE GUARD. An empty copied-path list is read as "nothing new
            # to copy" — a legitimate verified success. But it is ALSO exactly what a
            # BROKEN PARSER produces, and a parse failure that reads as good news is the
            # silent green light this whole file exists to prevent. So cross-check it
            # against an INDEPENDENT signal: rclone's own end-of-run stats. If rclone says
            # it moved bytes (or files) while we could not name a single copied path, the
            # two disagree, nothing can be scoped for verification, and we fail CLOSED.
            moved = []
            if isinstance(st["bytes"], int) and st["bytes"] > 0:
                moved.append(f"{st['bytes']} byte(s)")
            n_files = parse_transferred_files(combined)
            if isinstance(n_files, int) and n_files > 0:
                moved.append(f"{n_files} file(s)")
            if moved:
                st["proves"] = PROVES_FAILED_PARSE_CONTRADICTION.format(
                    moved=" and ".join(moved))
                msg = (f"CONTRADICTION: rclone's copy stats report {' and '.join(moved)} "
                       f"transferred, but parse_copied_paths found ZERO copied paths in "
                       f"its -v output — so nothing could be handed to `rclone check "
                       f"--files-from` and NOTHING was verified. Most likely rclone's -v "
                       f"INFO line wording changed and parse_copied_paths no longer "
                       f"matches; the parser needs updating. Refusing to report 'nothing "
                       f"new to copy' — failing closed, heartbeat NOT refreshed.")
                st["errors"].append(msg)
                st["ok"] = False
                log_fn(f"FAIL: {msg}")
                status_fn(st)
                return st
            st["ok"] = True
            st["proves"] = PROVES_VERIFIED_NOTHING_NEW.format(
                remote=RCLONE_REMOTE, last_deep=last_deep_display)
            status_fn(st)
            heartbeat_fn(
                f"{now:%Y-%m-%d %H:%M:%S}  data backup verified  source={DATA_SOURCE} "
                f"remote={RCLONE_REMOTE} files_checked=0 bytes_this_run={st['bytes']} "
                f"check=nothing-new-to-verify mode={mode} copied=0 "
                f"benign_differences=0")
            log_fn(f"SUCCESS: nothing new to copy, so nothing needed verification "
                   f"(mode={mode}) -> {RCLONE_REMOTE}")
            return st
        try:
            files_from = str(verify_list_fn(copied))
        except Exception as e:  # noqa: BLE001 — no list means no scoped proof; fail closed
            return _fail(f"could not write the scoped verify list ({e!r}) — refusing to "
                         f"run an unscoped or unverified check")

    try:
        cproc = check_fn(rclone_path) if files_from is None else check_fn(
            rclone_path, files_from=files_from)
    except subprocess.TimeoutExpired as e:
        return _fail(f"rclone check TIMED OUT after {RCLONE_TIMEOUT}s ({e!r})")
    except Exception as e:  # noqa: BLE001
        return _fail(f"rclone check raised {e!r}")
    st["check_returncode"] = getattr(cproc, "returncode", None)
    ccombined = (f"{getattr(cproc, 'stdout', '') or ''}\n"
                 f"{getattr(cproc, 'stderr', '') or ''}")
    parsed = parse_fn(ccombined)
    st["files_checked"] = parsed.get("matching")
    st["differences"] = parsed.get("differences")
    st["check_result"] = parsed.get("summary")

    # 4a. CLASSIFY. rclone check exits non-zero on ANY difference, and the first real run
    #     proved that most nightly differences are harmless live-file churn (see the
    #     three-tier section of the module docstring). So instead of blanket-failing on a
    #     non-zero exit, we account for EVERY reported problem with a per-file ERROR line
    #     and sort each one into a tier. The bar is unchanged where it matters: ZERO
    #     tier-1 failures, and anything we cannot explain IS a tier-1 failure.
    diffs = errors_fn(ccombined)
    classified = classify_differences(diffs, run_start=run_start, mtime_fn=mtime_fn)
    tier1 = [d for d in classified if d["tier"] == 1]
    tier2 = [d for d in classified if d["tier"] == 2]
    tier3 = [d for d in classified if d["tier"] == 3]

    # Unverifiable is NOT verified: a hash rclone could not compute proves nothing, and
    # it produces no per-file path to classify, so it is its own tier-1 condition.
    unchecked = parsed.get("hashes_unchecked") or 0
    # Reported problems with no per-file ERROR line to explain them.
    unaccounted = max(0, required_accounted(parsed) - len(classified))

    st["hard_failures"] = [{"path": d["path"], "reason": d["reason"], "why": d["why"]}
                           for d in tier1]
    if classified:
        st["benign_differences"] = {
            "count": len(tier2) + len(tier3),
            "tier2_known_volatile": len(tier2),
            "tier3_created_during_run": len(tier3),
            "sample_capped_at": BENIGN_SAMPLE_CAP,
            "sample": [{"path": d["path"], "reason": d["reason"], "tier": d["tier"],
                        "why": d["why"]} for d in (tier2 + tier3)[:BENIGN_SAMPLE_CAP]],
        }

    # A single message prefix for every verification failure, so the log/alarm text is
    # greppable regardless of which tier-1 condition tripped.
    rc = st["check_returncode"]
    prefix = (f"rclone check FAILED verification (exit {rc})" if rc != 0 else
              "rclone check FAILED verification: problems reported despite exit 0")

    if tier1:
        detail = "; ".join(f"{d['path']} [{d['reason']}] — {d['why']}" for d in tier1[:10])
        st["proves"] = PROVES_FAILED_TIER1.format(k=len(tier1), detail=detail)
        st["errors"].append(f"{prefix}: {len(tier1)} TIER-1 failure(s): {detail}")
        st["ok"] = False
        log_fn(f"FAIL: {st['errors'][-1]}")
        status_fn(st)
        return st
    if unchecked:
        return _fail(f"{prefix}: {unchecked} hash(es) could not be checked — "
                     f"unverifiable is not verified; {parsed.get('summary')}")
    if unaccounted:
        return _fail(f"{prefix}: {unaccounted} reported difference(s)/error(s) could not "
                     f"be accounted for by a per-file ERROR line, so they could not be "
                     f"classified — refusing to call this verified; "
                     f"{parsed.get('summary')}; {ccombined.strip()[-400:]}")
    if rc != 0 and not classified:
        return _fail(f"{prefix}: no per-file differences parsed, so the non-zero exit is "
                     f"unexplained; {parsed.get('summary')}; {ccombined.strip()[-400:]}")

    # 5. Verified success — zero tier-1 failures — and ONLY now does the heartbeat move.
    st["ok"] = True
    n = st["files_checked"]
    benign = len(tier2) + len(tier3)
    if mode == "deep":
        # A clean deep pass is the ONLY thing that advances this clock — it is the only
        # run that actually re-proved the whole warehouse.
        st["last_deep_verified"] = now.isoformat(timespec="seconds")
        if benign:
            st["proves"] = PROVES_VERIFIED_WITH_BENIGN.format(
                n=n if isinstance(n, int) else "an unparseable number of",
                m=benign, v=len(tier2), c=len(tier3),
                src=DATA_SOURCE, remote=RCLONE_REMOTE)
        elif isinstance(n, int):
            st["proves"] = PROVES_VERIFIED.format(n=n, src=DATA_SOURCE,
                                                  remote=RCLONE_REMOTE)
        else:
            st["proves"] = PROVES_VERIFIED_NO_COUNT.format(src=DATA_SOURCE,
                                                           remote=RCLONE_REMOTE)
    else:
        # Incremental claims ONLY the files it copied, and names when everything else was
        # last actually proven.
        copied_n = st["copied_count"]
        if benign:
            st["proves"] = PROVES_VERIFIED_INCREMENTAL_WITH_BENIGN.format(
                n=copied_n, m=benign, v=len(tier2), c=len(tier3),
                src=DATA_SOURCE, remote=RCLONE_REMOTE, last_deep=last_deep_display)
        else:
            st["proves"] = PROVES_VERIFIED_INCREMENTAL.format(
                n=copied_n, src=DATA_SOURCE, remote=RCLONE_REMOTE,
                last_deep=last_deep_display)
    status_fn(st)
    heartbeat_fn(
        f"{now:%Y-%m-%d %H:%M:%S}  data backup verified  source={DATA_SOURCE} "
        f"remote={RCLONE_REMOTE} files_checked={n} bytes_this_run={st['bytes']} "
        f"check=0-tier1-failures mode={mode} copied={st['copied_count']} "
        f"benign_differences={benign}")
    for d in tier2 + tier3:
        log_fn(f"benign (tier {d['tier']}): {d['path']} [{d['reason']}] — {d['why']}")
    log_fn(f"SUCCESS: verified data backup ({st['check_result']}; "
           f"{benign} benign difference(s)) -> {RCLONE_REMOTE}")
    return st


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verified rclone backup of C:\\TradingDesk-Local to Google Drive.")
    ap.add_argument("--dry-run", action="store_true",
                    help="pass rclone --dry-run to copy: transfer nothing, run NO "
                         "checksum verification, and never refresh the heartbeat.")
    ap.add_argument("--mode", choices=("auto", "incremental", "deep"), default="auto",
                    help="auto (default): DEEP on DEEP_WEEKDAY (Friday) or if the last "
                         "deep verification is older than DEEP_MAX_AGE_DAYS, else "
                         "INCREMENTAL. deep: full re-hash of everything. incremental: "
                         "verify only the files this run copied.")
    args = ap.parse_args()

    try:
        st = run_backup(dry_run=args.dry_run, mode=args.mode)
    except Exception as e:  # noqa: BLE001 — an unexpected error is still a FAILED backup
        log(f"UNEXPECTED ERROR (reporting failure, NOT success): {e!r}")
        return 2
    if args.dry_run:
        # A dry-run that got as far as a clean copy dry-run is "fine"; it is just never a
        # real backup. Exit 0 iff rclone resolved and the copy dry-run did not error.
        return 0 if st.get("copy_returncode") == 0 else 1
    return 0 if st.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
