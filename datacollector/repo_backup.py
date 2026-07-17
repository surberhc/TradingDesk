r"""
repo_backup.py — verified, off-machine git-bundle backup of the C:\TradingDesk repo.

WHY THIS EXISTS (2026-07-16 incident — read this before changing anything)
-------------------------------------------------------------------------
Google Drive silently synced the WRONG FOLDER for 9 days (2026-07-07 .. 2026-07-16).
85 commits never left this machine. NO ERROR WAS EVER RAISED — and that is the whole
point: nothing *failed*. Drive succeeded, perfectly, at syncing a folder that had
stopped changing. Separately, Drive sync was found PAUSED for ~2.5h on 2026-07-16;
writes into the Drive mount were accepted locally during that window and silently
never uploaded.

THE LESSON THIS FILE ENCODES: a backup that can fail SILENTLY is not a backup.
Writing a file into a Drive folder proves NOTHING about whether it reached the cloud.
So this job must PROVE its result, and the alarm must fire on SILENCE rather than
wait to be told about a failure.

Two rules fall out of that, and they are the load-bearing design of this module:

  1. The heartbeat/status is refreshed ONLY on a fully verified success. Every
     failure path — bundle verify failed, Drive unresolvable, Drive copy unreadable,
     sync paused — leaves the heartbeat UNTOUCHED and exits non-zero. A failure is
     therefore indistinguishable from the job never running at all, and BOTH go
     stale and page. We never write "I failed" and hope someone reads it; we go
     silent, and silence is what the alarm hunts.

  2. Nothing is assumed. The Drive destination is RESOLVED at runtime (Drive moves
     its own folders — that is the disease), confirmed to actually live on the
     DriveFS volume, and the bundle is re-verified AFTER it lands there.

TWO TRIGGERS, AND WHY BOTH STAY
-------------------------------
  1. `wrap` (CLAUDE.md's force-word) runs this job with --wrap, after the session's
     conductor render/commit. This is the PRIMARY trigger: it backs the work up while
     it is fresh. A wrap at 15:00 followed by a dead disk at 19:00 used to lose the
     whole day, because the 20:00 task was the only thing that ever fired.
  2. The RepoBackupDaily scheduled task (20:00 + AtLogon) STAYS as the safety net, and
     is deliberately NOT reduced to a backstop-in-name. Andrew does not always wrap;
     and — the load-bearing reason — the DAILY CADENCE is what feeds
     heartbeat_alarm.py's 26h staleness check. A wrap-only trigger would go silent
     every quiet weekend and page for a backup that was never actually missing, which
     is precisely how an alarm gets trained into noise. The alarm only works because
     something reliably feeds it whether or not a human showed up.

WHAT IT DOES
------------
  * `git bundle create <dest> --all`  — a full, self-contained clone of every ref.
  * `git bundle verify` — must report BOTH "is okay" AND "records a complete
    history". Anything else is a hard failure; we do not report success.
  * SKIPS creating a bundle when HEAD has not moved AND the most recent bundle is
    still PROVEN good in both locations — see find_reusable_bundle. Trigger (1) can
    fire this job many times a day; without the skip, seven identical 41MB bundles of
    one HEAD would evict seven days of genuinely distinct history under KEEP_LAST.
    The skip is proof-gated and errs toward bundling — see the note below.
  * Writes the bundle to the resolved Google Drive folder (TradingDesk-Backups)
    AND keeps a local copy under C:\TradingDesk-Local\backups\ (Drive is not
    trusted alone — it is the thing that just failed us).
  * Re-verifies the bundle at the Drive destination after the copy.
  * ASKS GOOGLE whether the bundle actually arrived: it resolves the backup folder's
    Drive id from the destination path, asks Drive for a file of the bundle's name
    IN THAT FOLDER, and compares the Drive API's md5Checksum against the local
    bundle's md5 (verify_cloud_arrival). The query is deliberately SCOPED TO THE
    RESOLVED FOLDER rather than asking whether the name exists anywhere in the Drive —
    the 9-day incident was Drive syncing the WRONG FOLDER, so folder identity is
    exactly what must be proven; see the block comment above _drive_find_in_folder.
    Inert until a credential exists — see CLOUD_VERIFY_REQUIRED.
  * Records a status JSON + heartbeat the alarm reads (heartbeat_alarm.py, job
    key "repo_backup").
  * Retention: keeps the last KEEP_LAST bundles per location.

WHAT THE CHECKS CAN AND CANNOT PROVE — be honest here, the incident was caused by
believing a check that proved less than it appeared to:
  * `git bundle verify` PROVES the bundle file is a structurally intact bundle with
    a complete history, readable by git at that path. That is a real proof.
  * The DriveFS-volume check PROVES the destination is on the Google Drive virtual
    volume rather than an ordinary local folder wearing a Drive-looking name (that
    is the wrong-folder mode). It does NOT prove the file uploaded.
  * The paused-sync check PROVES only what Drive's own log last SAID about pause
    state. It does NOT prove upload, and it can be stale/racy (see is_sync_paused).
  * THE CLOUD CHECK (verify_cloud_arrival) is the ONLY check in this file that is
    not a local filesystem/log check — it is the only one Google itself answers.
    It PROVES the bytes are in Google's datacenter and are the SAME bytes as the
    local bundle, because it compares md5, not name+size. That distinction is the
    whole point: name+size proves a file wearing the right label exists; md5
    proves the bundle in the cloud IS the bundle on disk. What it does NOT prove:
    anything about tomorrow, and anything at all while it is UNCONFIGURED.
  * THE HEAD-UNCHANGED SKIP, when it fires, proves that the pre-existing bundle
    STILL verifies in BOTH places AND records the repo's CURRENT HEAD — i.e. it
    re-earns the same guarantee a fresh bundle would, on the same evidence, rather
    than inheriting a claim from a previous run's status file. It proves NOTHING
    about UNCOMMITTED work: a bundle covers committed history only. That is exactly
    why `wrap` commits FIRST and backs up second, and why this job must never commit
    on its own (see the git helpers section).
  * EVERY OTHER CHECK HERE IS LOCAL AND PROVES NOTHING ABOUT CLOUD ARRIVAL.
    Confirming bytes are in the Drive mount is NOT confirming they are in Google's
    datacenter. With the cloud check unconfigured, the honest guarantee is exactly
    what it was before it existed: "a verified-complete bundle exists locally AND
    has been placed on the real Drive volume with sync not visibly paused." The
    LOCAL copy is what makes that acceptable — it is a real backup on its own.
    The status file's `proves` string states which of these two guarantees a given
    run actually earned. It is the artifact's honesty; keep every variant of it
    literally true.

RETENTION SAFETY
----------------
The ONLY deletion this job performs is of OLD BUNDLES IT CREATED ITSELF. That is
enforced three ways: (a) the filename must match BUNDLE_RE exactly — a strict
`tradingdesk-repo-YYYYMMDD-HHMMSS.bundle` pattern; (b) the directory must be one of
the two allow-listed backup dirs; (c) only plain files are ever unlinked. NOTE: a
hand-made bundle already lives in the Drive folder
(`tradingdesk-full-20260716.bundle`, the 2026-07-16 rescue copy). BUNDLE_RE
deliberately does NOT match it — the prefix is `tradingdesk-repo-`, not
`tradingdesk-full-`. Do not loosen that pattern.

Run:
    <venv python> repo_backup.py            # real run (the RepoBackupDaily path)
    <venv python> repo_backup.py --wrap     # same job, interactive: progress on stdout
                                            #   + a compact-JSON summary as the LAST line
    <venv python> repo_backup.py --force    # bundle even if HEAD has not moved
    <venv python> repo_backup.py --dry-run  # resolve + report, create nothing, delete nothing

Exit codes: 0 = verified success (heartbeat refreshed). Non-zero = failure of some
kind (heartbeat deliberately NOT refreshed, so the alarm goes off on schedule).
--wrap changes the OUTPUT, never the contract: same checks, same fail-closed codes.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths / tunables
# --------------------------------------------------------------------------- #
REPO = Path(os.environ.get("TRADINGDESK_REPO", r"C:\TradingDesk"))

# Local backup dir — the un-trusted-Drive insurance copy. Never synced.
LOCAL_BACKUP_DIR = Path(os.environ.get(
    "TRADINGDESK_BACKUP_DIR", r"C:\TradingDesk-Local\backups"))

# Small state the alarm reads. Lives beside the bundles (this is the backup job's
# own state; it is NOT options-warehouse data, so it does not go under DATA_ROOT).
STATUS_FILE = Path(os.environ.get(
    "TRADINGDESK_BACKUP_STATUS", str(LOCAL_BACKUP_DIR / "repo_backup_status.json")))
HEARTBEAT_FILE = Path(os.environ.get(
    "TRADINGDESK_BACKUP_HEARTBEAT", str(LOCAL_BACKUP_DIR / "repo_backup_heartbeat.txt")))
LOG_FILE = Path(os.environ.get(
    "TRADINGDESK_BACKUP_LOG", str(LOCAL_BACKUP_DIR / "repo_backup.log")))

# The Drive folder name we back up into, relative to the resolved "My Drive" root.
DRIVE_BACKUP_FOLDER = "TradingDesk-Backups"

# Known-good streaming mount(s), probed in order ONLY if the DriveFS DB can't tell
# us (which, as of 2026-07-16, it can't — see resolve_drive_root). Ordered
# most-recently-known-good first. These are FALLBACKS, not the source of truth.
KNOWN_MOUNT_CANDIDATES = [
    r"C:\Users\andre\Google Drive Sync Surber HC\My Drive",
]

# Retention: how many bundles to keep per location.
KEEP_LAST = 7

# Strict naming for bundles THIS JOB creates. Retention will not touch anything else.
BUNDLE_PREFIX = "tradingdesk-repo-"
BUNDLE_RE = re.compile(r"^tradingdesk-repo-\d{8}-\d{6}\.bundle$")

# Drive's own log — read-only. Logs rotate (drive_fs_N.txt); only the LIVE
# drive_fs.txt reflects CURRENT pause state, so we deliberately read only that one.
DRIVE_LOG = Path(r"C:\Users\andre\AppData\Local\Google\DriveFS\Logs\drive_fs.txt")
DRIVE_DB = Path(r"C:\Users\andre\AppData\Local\Google\DriveFS\root_preference_sqlite.db")

GIT_TIMEOUT = 600  # seconds; a 40MB bundle takes ~5s, so this is pure deadlock insurance

# --------------------------------------------------------------------------- #
# Cloud-arrival verification (the Drive API check)
# --------------------------------------------------------------------------- #
# The credential minted by drive_oauth_consent.py. Lives with the other secrets,
# outside Drive, NEVER in the repo. Its values are never logged or printed.
DRIVE_OAUTH_FILE = Path(os.environ.get(
    "TRADINGDESK_DRIVE_OAUTH", r"C:\TradingDesk-Local\secrets\drive_oauth.json"))

# THE ENABLEMENT FLAG. Flip to True once drive_oauth.json exists AND a real run has
# reported cloud state "verified" at least once.
#
# WHY IT SHIPS FALSE: the credential does not exist yet. Wiring this fail-closed
# before the GCP setup is done would fail tonight's 20:00 run and page for a
# missing credential rather than a missing backup — training the very "that alarm
# is just noise" reflex this job exists to defeat. So the check ships INERT.
#
# WHAT FALSE DOES NOT MEAN: silence. With the check unconfigured the job still
# succeeds, but the status file's `proves` string says out loud that cloud arrival
# was NOT checked. The absence of the check is VISIBLE IN THE ARTIFACT — never
# assumed, never quietly implied to have passed. That is the whole difference
# between "not checked" and the 9-day silent failure.
#
#   False + no credential  -> state "skipped_not_configured", job exits 0, `proves`
#                             says cloud arrival was NOT checked.
#   False + credential     -> the check RUNS; a failure downgrades `proves` and is
#                             recorded loudly, but does not fail the job. This is
#                             the grace period for confirming the setup works.
#   True  + missing/failed -> FAIL CLOSED: non-zero exit, heartbeat left cold, the
#                             alarm pages. Same contract as every other check here.
CLOUD_VERIFY_REQUIRED = False

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"

# Drive's folder mimeType — used to match a FOLDER while walking the path down to the
# backup dir, so a same-named FILE can never be mistaken for the folder.
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"

# Paging for _drive_list_children, which exists to enumerate a folder's children while
# RESOLVING the backup folder's id (see _drive_resolve_folder_id). A 'My Drive' root can
# hold far more than one page of folders, and a MISSED PAGE there reads as "no folder
# named TradingDesk-Backups exists" — a resolution failure on a perfectly healthy Drive.
# So we follow nextPageToken to exhaustion, and cap the walk so a pathological/looping
# token cannot hang the job. Hitting the cap is a QUERY FAILURE (None), never an empty
# result: "we could not finish enumerating" must never collapse into "it is not there".
DRIVE_LIST_PAGE_SIZE = 100
DRIVE_LIST_MAX_PAGES = 50

CLOUD_HTTP_TIMEOUT = 30      # per HTTP call — this job must never hang on the network

# DriveFS uploads ASYNCHRONOUSLY: the copy into the mount returns long before the
# bytes reach Google, so querying once, immediately, would report "not in the cloud"
# on a perfectly healthy run and cry wolf nightly. MEASURED 2026-07-16, across two
# bundles: Drive's API lagged the local write by roughly 1-5 MINUTES before it would
# answer for the file at all — see the block comment above _drive_find_in_folder. That
# lag is why this poll is essential rather than optional. We poll on a BOUNDED deadline
# instead. ~41MB is seconds-to-a-minute on any sane link, so 5 minutes is generous
# without putting the job at risk of running forever.
CLOUD_POLL_DEADLINE_S = 300
CLOUD_POLL_INTERVAL_S = 15

MD5_CHUNK = 1024 * 1024      # the bundle is ~41MB — hash it streaming, never in RAM

# The status file's `state` — what this run ACTUALLY DID, as a distinct value rather
# than an ok=True that blurs "made you a new bundle" into "re-verified an old one".
# A reader must never have to infer which happened.
STATE_VERIFIED_NEW = "verified_new_bundle"
STATE_SKIPPED_HEAD_UNCHANGED = "skipped_head_unchanged"
STATE_DRY_RUN = "dry_run"
STATE_FAILED = "failed"

# The status file's `proves` string, one variant per outcome. These are the artifact's
# honesty: each must be LITERALLY TRUE of the run that carries it. Overstating one is
# the exact bug class this job exists to kill, so they are constants — pinned by
# tests — rather than strings improvised at each call site.
PROVES_CLOUD_VERIFIED = (
    "bundle verified locally and at the Drive-volume destination, AND confirmed "
    "present in Google's cloud with an md5 matching the local bundle byte-for-byte")
PROVES_CLOUD_NOT_CHECKED = (
    "bundle verified locally and at the Drive-volume destination; cloud arrival was "
    "NOT checked (no Drive API credential configured) — does NOT prove cloud arrival")
PROVES_CLOUD_FAILED = (
    "bundle verified locally and at the Drive-volume destination; the cloud-arrival "
    "check RAN AND FAILED — does NOT prove cloud arrival, see `cloud` for why")
PROVES_FAILED_RUN = (
    "nothing — this run did not complete a verified backup; read `errors` for the "
    "failure, and `verify_local` / `verify_drive` / `cloud` for how far it got")

# The skip's `proves` is a PREFIX composed onto whichever cloud variant above the run
# earned, because a skip changes only ONE thing about the claim: who made the bundle.
# It must never read as "backed up" full stop — the whole point is that this run
# created NOTHING, and the reader is owed the name of the bundle carrying the weight.
PROVES_SKIPPED_PREFIX = (
    "NO NEW BUNDLE WAS CREATED BY THIS RUN — HEAD is unchanged and is already covered "
    "by the pre-existing bundle {bundle}, which records this exact HEAD and was "
    "RE-VERIFIED just now rather than assumed good; that bundle ")


def proves_skipped(bundle_name: str, cloud_proves: str) -> str:
    """The `proves` string for a skipped run: the prefix + the cloud variant earned.

    Composed rather than written out four times, so the cloud clause can never drift
    away from what the cloud check actually said on a skip.
    """
    return PROVES_SKIPPED_PREFIX.format(bundle=bundle_name) + cloud_proves


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
# Volume identity — the "is this REALLY Drive?" check
# --------------------------------------------------------------------------- #
def _volume_mount_root(path: str) -> str | None:
    """The volume mount root for `path` (e.g. 'C:\\' or the DriveFS junction root).

    Wraps GetVolumePathNameW. Returns None on any failure or off-Windows.
    """
    if os.name != "nt":
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        ok = ctypes.windll.kernel32.GetVolumePathNameW(str(path), buf, 1024)
        return buf.value if ok else None
    except Exception:  # noqa: BLE001 — identity check must never crash the job
        return None


def _volume_label(mount_root: str) -> str | None:
    """Volume label for a mount root ('Windows' for C:, 'Google Drive' for DriveFS)."""
    if os.name != "nt" or not mount_root:
        return None
    try:
        name = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        ser = ctypes.c_ulong(); mcl = ctypes.c_ulong(); flags = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            str(mount_root), name, 261, ctypes.byref(ser), ctypes.byref(mcl),
            ctypes.byref(flags), fs, 261)
        return name.value if ok else None
    except Exception:  # noqa: BLE001
        return None


def is_drive_managed(path, *, mount_root_fn=None, label_fn=None) -> tuple[bool, str]:
    """Is `path` actually on the Google Drive virtual volume? -> (ok, human_reason)

    THE WRONG-FOLDER MODE THIS CATCHES: on 2026-07-16 the destination looked like a
    Drive folder and accepted writes, but was not the folder Drive was syncing. A
    path that is a PLAIN LOCAL DIRECTORY wearing a Drive-ish name is the classic
    shape of that failure (it happens whenever DriveFS is unmounted — the mount
    point degrades into an ordinary empty folder on C: and writes silently land on
    the local disk forever).

    HOW: DriveFS mounts a SEPARATE virtual volume (a junction to a Volume{GUID},
    labelled 'Google Drive', FAT32-emulating). An ordinary folder on C: resolves to
    mount root 'C:\\' with label 'Windows'. So: the path's volume mount root must
    differ from the system volume's, and the volume label must be 'Google Drive'.
    Verified against the live machine 2026-07-16:
        C:\\TradingDesk                        -> root 'C:\\'  label 'Windows'
        ...\\Google Drive Sync Surber HC\\My Drive -> root '...Surber HC\\' label 'Google Drive'

    WHAT THIS PROVES: the bytes are being written to the DriveFS volume and not to a
    plain local decoy directory. WHAT IT DOES NOT PROVE: that DriveFS ever uploads
    them. A mounted, correctly-identified volume with sync paused is still a silent
    black hole — which is why is_sync_paused() exists as a separate check.
    """
    mount_root_fn = mount_root_fn or _volume_mount_root
    label_fn = label_fn or _volume_label

    root = mount_root_fn(str(path))
    if not root:
        return False, "could not determine the volume mount root for the destination"

    system_root = mount_root_fn(os.environ.get("SystemRoot", r"C:\Windows")) or "C:\\"
    if os.path.normcase(root) == os.path.normcase(system_root):
        return False, (f"destination resolves to the SYSTEM volume ({root}) — it is an "
                       f"ordinary local folder, NOT a Drive-managed mount")

    label = label_fn(root)
    if label != "Google Drive":
        return False, (f"destination volume {root!r} has label {label!r}, expected "
                       f"'Google Drive' — not a confirmed DriveFS mount")

    return True, f"on the DriveFS volume (mount root {root!r}, label {label!r})"


# --------------------------------------------------------------------------- #
# Paused-sync detection
# --------------------------------------------------------------------------- #
# Drive logs BOTH pause and resume through the SAME call site, so the LAST such
# line is the current state:
#   ...presence_tracker.cc:579:NotifyPauseSyncing Syncing is paused   <- paused
#   ...presence_tracker.cc:579:NotifyPauseSyncing Syncing is on       <- resumed
# (Both observed verbatim in drive_fs.txt on 2026-07-16: paused 16:38:29Z,
# resumed 18:59:07Z — the ~2.5h window during which writes silently didn't upload.)
_PAUSE_MARKER = "NotifyPauseSyncing"


def is_sync_paused(log_path=None, *, read_fn=None) -> tuple[bool | None, str]:
    """Did Drive's own log LAST say sync is paused? -> (paused|None, human_reason)

    Returns (True, ...) paused, (False, ...) on, (None, ...) unknown — unknown when
    the log is missing/unreadable or carries no pause line at all.

    WHAT THIS PROVES: only what DriveFS last WROTE about its own pause state. That
    is a genuine signal — it is exactly how we found the 2026-07-16 pause — but it
    is deliberately weak, and pretending otherwise is how you get another silent
    9-day gap:
      * It is a LOG TAIL, not an API query. Drive does not log a heartbeat of
        "still paused"; it logs the TRANSITION. So state is inferred from the last
        transition, which may be hours old and may have rotated out of the live file.
      * NOT-paused does NOT mean uploading. Sync can be on and still be stuck,
        throttled, quota-blocked, signed-out, or syncing the WRONG FOLDER — which is
        the exact 9-day failure that motivated this whole module and which this
        check would have happily called healthy.
      * UNKNOWN is genuinely unknown. We report it; we do not launder it into a pass.
    A `None` here is NOT treated as failure (the log may simply have rotated), but it
    IS recorded in the status file so a human can see we couldn't tell.
    """
    path = Path(log_path) if log_path else DRIVE_LOG
    if read_fn is not None:
        try:
            text = read_fn(path)
        except Exception as e:  # noqa: BLE001
            return None, f"could not read Drive log ({e!r})"
    else:
        try:
            # Read-only, tolerant of Drive writing concurrently.
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return None, f"could not read Drive log {path} ({e!r})"

    last = None
    for line in text.splitlines():
        if _PAUSE_MARKER in line:
            last = line
    if last is None:
        return None, (f"no {_PAUSE_MARKER} line in the live Drive log — cannot tell "
                      f"current pause state (it may have rotated out)")

    low = last.lower()
    if "syncing is paused" in low:
        return True, f"Drive log's last pause-state line says PAUSED: {last.strip()[:160]}"
    if "syncing is on" in low:
        return False, "Drive log's last pause-state line says syncing is ON"
    return None, f"unrecognised pause-state line: {last.strip()[:160]}"


# --------------------------------------------------------------------------- #
# Resolving the Drive destination — never hardcoded, always fails loud
# --------------------------------------------------------------------------- #
def _drive_root_from_db(db_path=None, *, connect_fn=None) -> tuple[str | None, str]:
    """Try to learn the Drive mount from DriveFS's own sqlite DB. -> (path|None, note)

    Opened READ-ONLY (uri=True, mode=ro). We deliberately do NOT use immutable=1:
    on a LIVE database immutable=1 can serve a stale cached read, and that bit us on
    2026-07-16 — an immutable read returned old mount data that sent us at the wrong
    folder. mode=ro is honest about the DB being live.

    REALITY CHECK (measured on this machine 2026-07-16): `roots` is EMPTY (it is only
    populated in MIRROR mode; this install is STREAMING), and `media` holds exactly
    one row — ('Windows', 'C:\\'), which is the local NTFS disk, not Drive. Its
    media_id even matches C:'s volume GUID. So today the DB yields NOTHING usable and
    this function returns None by design; the mount-probe fallback is what actually
    resolves. We still try the DB first because it is the only *authoritative* source
    when it IS populated (mirror mode), and a hardcoded path is what we are trying to
    escape. Any row we do get is treated as a CANDIDATE and must still pass
    is_drive_managed() — we never trust a path just because the DB named it (that is
    how you resolve to 'C:\\' and cheerfully back up into the system volume).
    """
    path = Path(db_path) if db_path else DRIVE_DB
    if not path.exists():
        return None, f"DriveFS DB not present at {path}"
    try:
        if connect_fn is not None:
            con = connect_fn(path)
        else:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception as e:  # noqa: BLE001
        return None, f"could not open DriveFS DB read-only ({e!r})"

    try:
        cur = con.cursor()
        # Mirror-mode installs record the synced root here.
        try:
            rows = list(cur.execute(
                "SELECT last_seen_absolute_path, root_path FROM roots"))
        except sqlite3.Error:
            rows = []
        for last_seen, root_path in rows:
            for cand in (last_seen, root_path):
                if cand and str(cand).strip():
                    return str(cand), "resolved from DriveFS DB `roots`"
        return None, ("DriveFS DB `roots` is empty (streaming-mode install) — no mount "
                      "recorded; falling back to probing known mounts")
    except Exception as e:  # noqa: BLE001
        return None, f"error reading DriveFS DB ({e!r})"
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


def resolve_drive_dest(*, db_fn=None, candidates=None, exists_fn=None,
                       drive_managed_fn=None) -> dict:
    """Resolve the Drive backup folder at runtime. NEVER hardcoded, NEVER silent.

    Order: DriveFS DB (authoritative when populated) -> probe known mounts.
    Every candidate — including one the DB named — must exist AND pass
    is_drive_managed() before we accept it.

    Returns a dict:
      resolved      : bool
      dest          : str | None   the TradingDesk-Backups folder to write into
      mount         : str | None   the "My Drive" root we resolved
      source        : str          'drivefs_db' | 'probe' | 'none'
      drive_managed : bool
      note          : str          human explanation (goes in the status file)

    If NOTHING resolves, resolved=False — the caller MUST record that and exit
    non-zero. We never silently skip the Drive copy; a skipped Drive copy that
    reports success is precisely the 9-day silent failure this module exists to
    prevent.
    """
    db_fn = db_fn or _drive_root_from_db
    exists_fn = exists_fn or (lambda p: Path(p).is_dir())
    drive_managed_fn = drive_managed_fn or is_drive_managed
    cands = list(candidates) if candidates is not None else list(KNOWN_MOUNT_CANDIDATES)

    notes: list[str] = []

    db_path, db_note = db_fn()
    notes.append(f"db: {db_note}")
    ordered: list[tuple[str, str]] = []
    if db_path:
        ordered.append((db_path, "drivefs_db"))
    ordered.extend((c, "probe") for c in cands)

    for mount, source in ordered:
        if not exists_fn(mount):
            notes.append(f"{source} candidate {mount!r}: does not exist")
            continue
        dest = str(Path(mount) / DRIVE_BACKUP_FOLDER)
        managed, why = drive_managed_fn(mount)
        if not managed:
            # LOUD: a candidate that exists but is not Drive-managed is the
            # wrong-folder mode wearing a convincing costume. Skip it.
            notes.append(f"{source} candidate {mount!r}: REJECTED — {why}")
            continue
        notes.append(f"{source} candidate {mount!r}: accepted — {why}")
        return {"resolved": True, "dest": dest, "mount": mount, "source": source,
                "drive_managed": True, "note": " | ".join(notes)}

    notes.append("NOTHING RESOLVED — no Drive-managed mount found")
    return {"resolved": False, "dest": None, "mount": None, "source": "none",
            "drive_managed": False, "note": " | ".join(notes)}


# --------------------------------------------------------------------------- #
# git helpers — READ-ONLY git only (bundle create/verify/list-heads, rev-parse,
# rev-list). This job must never mutate the repo: no add/commit/reset/checkout/clean/gc.
#
# COMMITTING IS THE SESSION'S JOB, NOT THE BACKUP'S, and that boundary is deliberate
# rather than incidental. `wrap` commits and THEN calls this job; if this job also
# committed, a backup would silently change the thing it is supposed to be observing,
# and "HEAD is unchanged" — the skip's entire premise — would become a statement about
# the backup's own side effects instead of about Andrew's work. A read-only observer
# can be trusted to report; a writer cannot be trusted to report on itself.
# --------------------------------------------------------------------------- #
def _git(args: list[str], *, cwd=None, timeout: int = GIT_TIMEOUT):
    return subprocess.run(
        ["git", *args], cwd=str(cwd or REPO), capture_output=True, text=True,
        timeout=timeout, check=False)


def bundle_verify(path, *, run_fn=None) -> tuple[bool, str]:
    """Verify a bundle. -> (ok, detail)

    Demands BOTH conditions, per the spec and per common sense:
      * git exits 0 and says "<path> is okay"
      * the output contains "records a complete history"
    A bundle that verifies but records only a PARTIAL history is a thin bundle that
    cannot stand alone as a backup — it needs a base repo we may not have. So a
    missing "complete history" line is a HARD FAIL, not a warning.

    git bundle verify writes its human report to STDERR, so we join both streams
    before matching (verified against real output 2026-07-16).
    """
    run_fn = run_fn or (lambda: _git(["bundle", "verify", str(path)]))
    try:
        proc = run_fn()
    except Exception as e:  # noqa: BLE001
        return False, f"git bundle verify raised {e!r}"

    out = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
    if proc.returncode != 0:
        return False, f"git bundle verify exited {proc.returncode}: {out[:400]}"
    if "is okay" not in out:
        return False, f"git bundle verify did not report 'is okay': {out[:400]}"
    if "records a complete history" not in out:
        return False, (f"git bundle verify did NOT report a complete history "
                       f"(thin/partial bundle — unusable as a standalone backup): "
                       f"{out[:400]}")
    return True, "okay + full history"


def bundle_head_sha(path, *, run_fn=None) -> tuple[str | None, str]:
    """The HEAD sha recorded INSIDE a bundle. -> (sha|None, detail)

    Asked of THE BUNDLE ITSELF, never of a status file or a filename. That is the
    point: the status JSON records what a previous RUN believed, which is exactly the
    kind of second-hand claim this module exists to stop trusting. `git bundle
    list-heads` reads the bundle's own header and prints '<sha> <refname>' per ref; a
    bundle made with --all carries an explicit HEAD line (verified against a real
    bundle on this machine 2026-07-16):
        b50e78b9076bc85983f565167f968b0c91cb92f7 refs/heads/main
        06dc23337b9316cdc7373db18c0074abc6842511 refs/stash
        b50e78b9076bc85983f565167f968b0c91cb92f7 HEAD

    READ-ONLY: list-heads only reads the bundle file; it does not touch the repo.

    Never raises. Any doubt whatsoever — git failed, the file is not a bundle, there
    is no HEAD line — returns None, and None means the caller BUNDLES rather than
    skips. This function is only ever allowed to enable a skip by affirmatively
    naming a sha.
    """
    run_fn = run_fn or (lambda: _git(["bundle", "list-heads", str(path)]))
    try:
        proc = run_fn()
    except Exception as e:  # noqa: BLE001
        return None, f"git bundle list-heads raised {e!r}"
    if getattr(proc, "returncode", 1) != 0:
        return None, (f"git bundle list-heads exited {proc.returncode}: "
                      f"{(getattr(proc, 'stderr', '') or '')[:200]}")
    for line in (getattr(proc, "stdout", "") or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "HEAD":
            return parts[0], f"bundle records HEAD {parts[0]}"
    return None, ("the bundle records no HEAD ref — cannot tell which HEAD it covers, "
                  "so it cannot be reused")


def repo_facts(*, run_fn=None) -> dict:
    """HEAD sha + commit count across all refs. Read-only git. Never raises."""
    run_fn = run_fn or _git
    facts = {"head_sha": None, "commit_count": None}
    try:
        p = run_fn(["rev-parse", "HEAD"])
        if p.returncode == 0:
            facts["head_sha"] = (p.stdout or "").strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        p = run_fn(["rev-list", "--count", "--all"])
        if p.returncode == 0:
            facts["commit_count"] = int((p.stdout or "0").strip() or 0)
    except Exception:  # noqa: BLE001
        pass
    return facts


# --------------------------------------------------------------------------- #
# The redundant-bundle skip — proof-gated, and biased toward bundling
# --------------------------------------------------------------------------- #
def find_reusable_bundle(local_dir, drive_dir, head_sha, *, verify_fn=None, head_fn=None,
                         list_fn=None, exists_fn=None, log_fn=None) -> tuple[dict | None, str]:
    """Is the most recent existing bundle ALREADY a proven-good backup of THIS HEAD?

    -> (reuse_info | None, human_reason). None means CREATE A FRESH BUNDLE.

    WHY THIS EXISTS: `wrap` can fire the backup several times a day. On 2026-07-16
    three bundles were written carrying the identical head_sha (b50e78b) — ~41MB each,
    all three redundant. Left alone, that churn evicts genuinely distinct history from
    under KEEP_LAST=7 and replaces a week of recoverable states with seven copies of
    one afternoon. The skip is a RETENTION-INTEGRITY measure first and a disk/time
    saving second.

    THE DANGER, and it is the only thing that matters here: a skip that fires on a bad
    or absent bundle would report a healthy backup while there is NO good backup — the
    2026-07-16 silent failure, rebuilt with our own hands and wired to the heartbeat.
    So the bias is absolute and one-directional:

        SKIP ONLY ON AFFIRMATIVE PROOF. BUNDLE ON ANY DOUBT.

    Every condition below must AFFIRMATIVELY pass. Missing file, unreadable HEAD, a
    failed verify, an unlistable directory, an unknown repo HEAD, an exception — every
    one of them returns None and we bundle. The costs are wildly asymmetric: a
    needless bundle costs ~5 seconds and 41MB; a wrong skip costs the backup. There is
    no close call to make.

    The conditions, cheapest-first (the HEAD reads parse a header; the verifies scan
    ~41MB twice, so they go last):
      1. the repo's current HEAD sha is known at all
      2. a bundle THIS JOB created (BUNDLE_RE) exists in the local dir
      3. that same bundle is present at the Drive destination too — a local-only
         bundle is not the backup this job promises, so it does not license a skip
      4. the LOCAL bundle's own recorded HEAD == the repo's current HEAD
      5. the DRIVE copy's recorded HEAD matches as well — same name is not same file
      6. the LOCAL bundle still verifies (okay + complete history)
      7. the DRIVE copy still verifies
    Only then may the caller skip, and only while saying whose bundle it is leaning on.
    """
    log_fn = log_fn or log
    verify_fn = verify_fn or bundle_verify
    head_fn = head_fn or bundle_head_sha
    exists_fn = exists_fn or (lambda p: Path(p).is_file())
    list_fn = list_fn or (lambda d: sorted(p.name for p in Path(d).iterdir()
                                           if p.is_file()))

    if not head_sha:
        return None, ("the repo's current HEAD sha could not be read — bundling fresh "
                      "rather than skipping on a guess")

    try:
        names = sorted(n for n in list_fn(local_dir) if BUNDLE_RE.match(n))
    except OSError as e:
        return None, f"could not list {local_dir} ({e!r}) — bundling fresh"
    if not names:
        return None, (f"no bundle this job created exists in {local_dir} yet — "
                      f"bundling fresh")

    # Newest by FILENAME (the timestamp is in the name and zero-padded, so lexical ==
    # chronological), for the same reason retention does it that way: a Drive copy's
    # mtime is whatever the filesystem felt like and must not decide anything.
    name = names[-1]
    local_path = Path(local_dir) / name
    drive_path = Path(drive_dir) / name

    if not exists_fn(local_path):
        return None, (f"the most recent bundle {name} is MISSING from {local_dir} — "
                      f"bundling fresh")
    if not exists_fn(drive_path):
        return None, (f"the most recent bundle {name} is MISSING from the Drive "
                      f"destination {drive_dir} — bundling fresh")

    local_head, lnote = head_fn(local_path)
    if not local_head:
        return None, (f"could not read the HEAD recorded inside {name} ({lnote}) — "
                      f"bundling fresh")
    if local_head != head_sha:
        return None, (f"HEAD has MOVED since {name} (that bundle records "
                      f"{local_head[:12]}, the repo is at {head_sha[:12]}) — bundling "
                      f"fresh")

    drive_head, dnote = head_fn(drive_path)
    if drive_head != local_head:
        return None, (f"the Drive copy of {name} does not record the same HEAD as the "
                      f"local one ({dnote}) — same name is not the same file; bundling "
                      f"fresh")

    ok, verify_local = verify_fn(local_path)
    if not ok:
        return None, (f"the existing bundle {name} FAILED verification — {verify_local}; "
                      f"bundling fresh (a skip here would report a backup that is not "
                      f"there)")
    ok, verify_drive = verify_fn(drive_path)
    if not ok:
        return None, (f"the Drive copy of {name} FAILED verification — {verify_drive}; "
                      f"bundling fresh")

    why = (f"HEAD {head_sha[:12]} is UNCHANGED since {name}, and that bundle was "
           f"re-verified just now in both places (local: {verify_local}; drive: "
           f"{verify_drive}) and records this exact HEAD — a new bundle would be a "
           f"byte-for-byte redundant copy, so this run creates nothing")
    log_fn(f"skip check: {why}")
    return {"name": name, "local_path": str(local_path), "drive_path": str(drive_path),
            "verify_local": verify_local, "verify_drive": verify_drive}, why


# --------------------------------------------------------------------------- #
# Cloud arrival — the one question only Google can answer
# --------------------------------------------------------------------------- #
# STDLIB ONLY, DELIBERATELY. This is raw Drive REST v3 over urllib rather than
# google-api-python-client because this is the most safety-critical script in the
# repo and CLAUDE.md forbids new heavy dependencies. (Service-account auth was
# rejected for the same reason: RS256 JWT signing would drag a crypto dependency in
# here.) An installed-app refresh token is a form POST; a metadata query is a GET.
# That is the entire surface — it does not justify a dependency tree.
def file_md5(path, *, chunk: int = MD5_CHUNK, open_fn=None) -> str:
    """md5 of a file, read STREAMING. The bundle is ~41MB — never slurp it into RAM.

    (md5 is not a security choice here and is not being used as one: it is simply
    the checksum Google exposes for a Drive file, so it is the only value that can
    be compared against the cloud at all.)
    """
    opener = open_fn or (lambda p: open(p, "rb"))
    h = hashlib.md5()
    with opener(path) as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _load_drive_creds(path=None, *, read_fn=None) -> tuple[dict | None, str, str]:
    """Load drive_oauth.json. -> (creds|None, state, note); state: ok|absent|bad.

    ABSENT and BAD are deliberately DIFFERENT states. Absent means "never set up"
    (an honest, expected condition before the GCP work is done). Bad means the
    credential exists but is broken — that is a real failure and must not be
    laundered into "not configured yet", which would let a corrupted credential
    quietly disable the check forever.

    Never logs a value out of this file.
    """
    path = Path(path or DRIVE_OAUTH_FILE)
    if read_fn is None:
        def read_fn(p):
            return Path(p).read_text(encoding="utf-8")
    try:
        raw = read_fn(path)
    except FileNotFoundError:
        return None, "absent", (f"no Drive API credential at {path} — run "
                                f"drive_oauth_consent.py to configure the check")
    except OSError as e:
        return None, "bad", f"could not read Drive API credential {path} ({e!r})"
    try:
        creds = json.loads(raw)
    except ValueError as e:
        return None, "bad", f"Drive API credential {path} is not valid JSON ({e!r})"
    if not isinstance(creds, dict):
        return None, "bad", f"Drive API credential {path} is not a JSON object"
    missing = [k for k in ("client_id", "client_secret", "refresh_token")
               if not creds.get(k)]
    if missing:
        return None, "bad", (f"Drive API credential {path} is missing: "
                             f"{', '.join(missing)}")
    return creds, "ok", f"loaded Drive API credential from {path}"


def _post_form(url, fields: dict, *, timeout: int = CLOUD_HTTP_TIMEOUT,
               urlopen_fn=None) -> tuple[dict | None, str | None, str]:
    """POST a form, expect JSON. -> (payload|None, google_error_code|None, note).

    Never raises, and never puts `fields` in the note — they carry the client_secret
    and the refresh token. Google's ERROR bodies are quoted (they are diagnostic and
    carry no secret); the request never is.
    """
    body = urllib.parse.urlencode(fields).encode("ascii")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with (urlopen_fn or urllib.request.urlopen)(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), None, "ok"
    except urllib.error.HTTPError as e:
        code, detail = None, ""
        try:
            payload = json.loads(e.read().decode("utf-8", errors="replace"))
            code = payload.get("error")
            detail = f"{code}: {payload.get('error_description', '')}".strip(": ")
        except Exception:  # noqa: BLE001 — a non-JSON error body is still a failure
            pass
        return None, code, f"HTTP {e.code} from {url} ({detail or 'no detail'})"
    except urllib.error.URLError as e:
        return None, None, f"network error contacting {url} ({e.reason!r})"
    except Exception as e:  # noqa: BLE001 — a hung/odd transport is still a failure
        return None, None, f"unexpected error contacting {url} ({e!r})"


def _get_json(url, *, headers=None, timeout: int = CLOUD_HTTP_TIMEOUT,
              urlopen_fn=None) -> tuple[dict | None, str]:
    """GET JSON. -> (payload|None, note). Never raises; never logs the bearer token."""
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    try:
        with (urlopen_fn or urllib.request.urlopen)(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), "ok"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return None, f"HTTP {e.code} from the Drive API ({detail or 'no detail'})"
    except urllib.error.URLError as e:
        return None, f"network error contacting the Drive API ({e.reason!r})"
    except Exception as e:  # noqa: BLE001
        return None, f"unexpected error contacting the Drive API ({e!r})"


def _drive_access_token(creds: dict, *, urlopen_fn=None,
                        timeout: int = CLOUD_HTTP_TIMEOUT
                        ) -> tuple[str | None, str | None, str]:
    """refresh_token -> (access_token|None, error_code|None, note).

    The access token is returned for use IN MEMORY ONLY — it is never logged and
    never written to the status file. It lives ~1h; we hold it for the length of one
    poll and drop it.
    """
    payload, code, note = _post_form(GOOGLE_TOKEN_URL, {
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=timeout, urlopen_fn=urlopen_fn)
    if payload is None:
        return None, code, note
    token = payload.get("access_token")
    if not token:
        return None, None, "the token endpoint returned no access_token"
    return token, None, "access token refreshed"


def _drive_list_children(folder_id: str, token: str, *, urlopen_fn=None,
                         timeout: int = CLOUD_HTTP_TIMEOUT,
                         page_size: int = DRIVE_LIST_PAGE_SIZE,
                         max_pages: int = DRIVE_LIST_MAX_PAGES
                         ) -> tuple[list | None, str]:
    """List EVERY child of a Drive folder. -> (files|None, note); None == query failed.

    USED FOR ONE THING: walking 'My Drive' down to the backup folder to resolve its id
    (_drive_resolve_folder_id). The bundle lookup itself does NOT go through here — it
    is a single parent-scoped name query; see _drive_find_in_folder.

    PAGINATED TO EXHAUSTION, deliberately. The resolution walk matches each path
    component against a folder's children, so a MISSED PAGE reads as "no folder named
    TradingDesk-Backups exists" — a confident, wrong resolution failure on a healthy
    Drive. Every page is followed; running out of cap returns None (a failure to
    enumerate), never a short list masquerading as the whole folder.

    `fields` names md5Checksum explicitly — the Drive API omits it otherwise, and a
    silently-absent checksum is precisely the kind of "check that proved less than it
    appeared to" this repo has already been bitten by. supportsAllDrives /
    includeItemsFromAllDrives are set so a shared drive is enumerated too rather than
    returning an empty list that reads identically to "it is not there".
    """
    # Folder ids come from Drive itself, but escape anyway rather than rely on the
    # upstream staying well-behaved forever.
    safe = str(folder_id).replace("\\", "\\\\").replace("'", "\\'")
    files: list = []
    page_token: str | None = None
    for page in range(1, max_pages + 1):
        params = {
            "q": f"'{safe}' in parents and trashed = false",
            "fields": "nextPageToken,files(id,name,size,md5Checksum,mimeType)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "spaces": "drive",
            "pageSize": str(page_size),
        }
        if page_token:
            params["pageToken"] = page_token
        payload, note = _get_json(
            DRIVE_FILES_URL + "?" + urllib.parse.urlencode(params),
            headers={"Authorization": f"Bearer {token}"}, timeout=timeout,
            urlopen_fn=urlopen_fn)
        if payload is None:
            return None, f"listing folder {folder_id} failed on page {page} — {note}"
        batch = payload.get("files")
        if batch is None:
            return None, (f"the Drive API response for folder {folder_id} page {page} "
                          f"had no `files` key")
        files.extend(batch)
        page_token = payload.get("nextPageToken")
        if not page_token:
            return files, (f"Drive listed {len(files)} file(s) in folder {folder_id} "
                           f"across {page} page(s)")
    return None, (f"folder {folder_id} still had more pages after {max_pages} — refusing "
                  f"to report a TRUNCATED listing, because a short list here is "
                  f"indistinguishable from the folder we are looking for not existing")


# HOW WE LOOK THE BUNDLE UP, AND WHAT THE 2026-07-16 MEASUREMENTS ACTUALLY SHOW.
# One parent-scoped name query:
#     q=name='<bundle>' and '<folder_id>' in parents and trashed = false
#
# THE DATA, both bundles, local write time then what each query shape answered:
#   tradingdesk-repo-20260716-162242.bundle, written 16:22:42:
#       ~16:23   name query      -> EMPTY
#       ~16:25   name query      -> EMPTY
#       ~16:27   parent listing  -> FOUND
#       ~16:27   name query      -> FOUND   (moments after the parent listing hit)
#   tradingdesk-repo-20260716-163838.bundle, written 16:38:38:
#       ~16:40   parent listing  -> NOT FOUND  (it still showed 162242 as the newest)
#       ~16:41   name query      -> FOUND      <- the NAME query saw it FIRST here
#       ~16:42   parent listing  -> FOUND
#
# THE ONLY CLAIM THAT DATA SUPPORTS, and the only one anything here may assert: DRIVE'S
# API LAGS THE LOCAL WRITE BY ROUGHLY 1-5 MINUTES, REGARDLESS OF QUERY TYPE. On the
# second bundle the name query found the file BEFORE the parent listing did — the two
# shapes are not measurably different, and one observation runs against the idea that
# listing is faster. That lag is real, it was measured, and it is exactly what makes
# CLOUD_POLL_DEADLINE_S essential rather than optional.
#
# WHAT THIS CORRECTS: an earlier version of this file (commit 8a07993, conductor #32)
# asserted that Drive's NAME INDEX was eventually consistent while a parentId listing
# was IMMEDIATELY consistent, and replaced this query with a paginated full-folder
# listing plus a client-side filename match on that basis. THAT MECHANISM WAS FALSE. It
# was read off the first bundle alone, where ~5 minutes had already elapsed before the
# parent listing was first tried; "parentId is immediate" was elapsed time mistaken for
# a property of the query. The second bundle then cut the other way. The retraction is
# conductor entry #33; 8a07993 stays in the history unamended. Do not reintroduce a
# mechanism claim here that the measurements do not carry.
#
# HONEST LIMIT ON EVEN THE TRUE PART: the lag was measured through an MCP Drive
# connector, not through the raw endpoint this module calls. Its `title` operator
# probably maps onto `name` in files.list, but that is not proven. n=2 bundles.
#
# WHY THE QUERY IS PARENT-SCOPED — NOT part of that retraction, and load-bearing:
# the 2026-07-07..16 incident was Drive syncing the WRONG FOLDER. A check that asked
# only "does a file with this name and md5 exist SOMEWHERE in the Drive" would have
# passed happily throughout all 9 days of it. FOLDER IDENTITY is precisely what has to
# be verified, so the query is scoped to the folder id RESOLVED (never hardcoded) from
# the path this run actually wrote to.
def _drive_find_in_folder(name: str, folder_id: str, token: str, *, urlopen_fn=None,
                          timeout: int = CLOUD_HTTP_TIMEOUT
                          ) -> tuple[list | None, str]:
    """Find files named `name` INSIDE folder `folder_id`. -> (files|None, note).

    None == the query FAILED (we could not ask Google). [] == Google ANSWERED and says
    no such file is in that folder. Those mean opposite things and must never blur.

    `fields` asks for md5Checksum explicitly — the Drive API omits it otherwise, and a
    silently-absent checksum is precisely the kind of "check that proved less than it
    appeared to" this repo has already been bitten by. supportsAllDrives /
    includeItemsFromAllDrives are set so a shared drive is searched too rather than
    returning an empty list that reads identically to "the upload never happened".
    """
    # Bundle names come from BUNDLE_RE and folder ids come from Drive itself (no quotes
    # possible in either), but escape anyway rather than rely on an upstream staying
    # well-behaved forever.
    safe_name = str(name).replace("\\", "\\\\").replace("'", "\\'")
    safe_folder = str(folder_id).replace("\\", "\\\\").replace("'", "\\'")
    url = DRIVE_FILES_URL + "?" + urllib.parse.urlencode({
        "q": (f"name = '{safe_name}' and '{safe_folder}' in parents and "
              f"trashed = false"),
        "fields": "files(id,name,size,md5Checksum)",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
        "spaces": "drive",
        "pageSize": "10",
    })
    payload, note = _get_json(url, headers={"Authorization": f"Bearer {token}"},
                              timeout=timeout, urlopen_fn=urlopen_fn)
    if payload is None:
        return None, note
    files = payload.get("files")
    if files is None:
        return None, "the Drive API response had no `files` key"
    return files, (f"Drive returned {len(files)} file(s) named {name} in folder "
                   f"{folder_id}")


def _drive_path_components(dest, mount) -> tuple[list[str] | None, str]:
    """The folder names between the 'My Drive' root and the backup folder.

    -> (components|None, note). Purely lexical — no filesystem, no network.

    DERIVED, NEVER HARDCODED. The live folder id was observed on this machine, and
    pinning it would be a landmine: an id names one particular folder OBJECT, and
    Drive moving/recreating its own folders is the exact disease this module exists
    for. A pinned id survives that by pointing at a ghost which will never receive
    another bundle — while the check reports a clean, confident miss. The PATH (which
    resolve_drive_dest already computes at runtime) survives it; the id does not.
    """
    if not dest:
        return None, "no Drive destination path was supplied to the cloud check"
    if not mount:
        return None, (f"no Drive mount root was supplied alongside {dest} — cannot tell "
                      f"which part of that path sits below the 'My Drive' root")
    dparts = Path(dest).parts
    mparts = Path(mount).parts
    # Case-insensitive on the MOUNT PREFIX (Windows paths are), but the components we
    # carry away keep their ORIGINAL CASE — Drive folder names are case-sensitive, and
    # a lowercased name would miss a folder that is sitting right there.
    same = (len(dparts) >= len(mparts) and
            [os.path.normcase(p) for p in dparts[:len(mparts)]] ==
            [os.path.normcase(p) for p in mparts])
    if not same:
        return None, (f"the Drive destination {dest!r} is not below the resolved mount "
                      f"root {mount!r} — cannot map it onto a Drive folder path")
    parts = [p for p in dparts[len(mparts):] if p not in ("", ".")]
    if not parts:
        return None, (f"the Drive destination {dest!r} IS the mount root — there is no "
                      f"backup folder to resolve")
    return parts, f"backup folder path below 'My Drive': {'/'.join(parts)}"


def _drive_resolve_folder_id(components, token, *, list_fn=None, urlopen_fn=None,
                             timeout: int = CLOUD_HTTP_TIMEOUT
                             ) -> tuple[str | None, str]:
    """Walk 'My Drive' down `components` to the backup folder's id. -> (id|None, note).

    Each step LISTS the parent and matches the next component CLIENT-SIDE against the
    children's name AND mimeType. ('root' is Drive's alias for the My Drive root in an
    `in parents` term.) Enumerating is not a claim that a listing is faster or fresher
    than a name query — no such claim survives the measurements; see
    _drive_find_in_folder. It is simply that this walk has to SEE each candidate to
    reject a same-named FILE and to notice ambiguous same-named siblings, and it needs
    the children of each level anyway.

    Fails honestly and distinctly on anything ambiguous — Drive permits same-named
    siblings, and a coin-flip between two candidate folders is not a resolution.

    THIS RESOLUTION IS NOT OPTIONAL AND HAS NO FALLBACK. It is what pins the check to
    the folder this run actually wrote to, which is the whole question the 2026-07-07..16
    wrong-folder incident turned on.
    """
    if list_fn is None:
        def list_fn(fid, tok):
            return _drive_list_children(fid, tok, urlopen_fn=urlopen_fn, timeout=timeout)

    parent = "root"
    trail = ["My Drive"]
    for comp in components:
        children, note = list_fn(parent, token)
        if children is None:
            return None, (f"could not list {'/'.join(trail)} while resolving the backup "
                          f"folder — {note}")
        hits = [c for c in children
                if c.get("name") == comp and c.get("mimeType") == DRIVE_FOLDER_MIME]
        if not hits:
            return None, (f"no folder named {comp!r} exists under {'/'.join(trail)} in "
                          f"Drive ({note})")
        if len(hits) > 1:
            return None, (f"{len(hits)} folders named {comp!r} exist under "
                          f"{'/'.join(trail)} — ambiguous; refusing to guess which one "
                          f"the bundles are supposed to be in")
        parent = hits[0].get("id")
        if not parent:
            return None, (f"Drive returned a folder named {comp!r} under "
                          f"{'/'.join(trail)} with no id")
        trail.append(comp)
    return parent, f"resolved {'/'.join(trail)} to Drive folder id {parent}"


def _creds_age_days(creds: dict | None, *, now=None) -> int | None:
    """Days since consent, from the credential's own stamp. -> int | None.

    Exists for ONE reason: when a refresh is rejected, an age of ~7 days is the
    fingerprint of the Testing-publishing-status trap (see drive_oauth_consent.py).
    Naming that at the moment of failure is the only detection available — Google
    exposes no API for an app's publishing status.
    """
    stamp = (creds or {}).get("obtained_utc")
    if not stamp:
        return None
    try:
        then = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    ref = now or dt.datetime.now(dt.timezone.utc)
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=dt.timezone.utc)
    return max(0, (ref - then).days)


def _token_failure_note(err_code: str | None, note: str, creds: dict | None,
                        *, now=None) -> str:
    """The honest, specific note for a token refresh that did not produce a token."""
    age = _creds_age_days(creds, now=now)
    age_txt = f" The credential was minted {age} day(s) ago." if age is not None else ""
    status = (creds or {}).get("publishing_status")
    status_txt = ("" if status in (None, "in_production") else
                  f" The credential records publishing_status={status!r}, i.e. the app "
                  f"was NOT confirmed published when it was minted.")
    if err_code == "invalid_grant":
        return (f"Drive REJECTED the refresh token (invalid_grant) — {note}. THIS IS "
                f"THE PREDICTED FAILURE: an OAuth app left in 'Testing' publishing "
                f"status issues refresh tokens that EXPIRE AFTER 7 DAYS.{age_txt}"
                f"{status_txt} If that age is around 7 days, this is almost certainly "
                f"it: set the app's publishing status to 'In production' and re-run "
                f"drive_oauth_consent.py --force. (Other causes: consent revoked, the "
                f"client secret rotated, the account password changed.) Until it is "
                f"fixed, CLOUD ARRIVAL IS NOT BEING PROVEN.")
    return f"could not obtain a Drive access token — {note}.{age_txt}{status_txt}"


def verify_cloud_arrival(bundle_path, bundle_name=None, drive_dest=None,
                         drive_mount=None, *, required=None,
                         creds_fn=None, token_fn=None, folder_fn=None, find_fn=None,
                         md5_fn=None, sleep_fn=None, monotonic_fn=None, now=None,
                         deadline_s: int = CLOUD_POLL_DEADLINE_S,
                         interval_s: int = CLOUD_POLL_INTERVAL_S,
                         log_fn=None) -> dict:
    """Did the bundle actually reach Google? -> the status file's `cloud` block.

    HOW IT LOOKS: it RESOLVES THE BACKUP FOLDER'S ID, then asks Drive ONE parent-scoped
    question — is there a file of this name IN THAT FOLDER (see _drive_find_in_folder).
    The scoping is the point and is not negotiable: the 2026-07-07..16 incident was
    Drive syncing the WRONG FOLDER, and a check that asked only "does this name exist
    somewhere in the Drive" would have passed straight through it. If the folder id
    cannot be resolved we FAIL WITH A DISTINCT NOTE — we never quietly fall back to an
    unscoped query, because an unscoped answer is not an answer to this question.

    WHY IT POLLS: Drive's API lags the local write by roughly 1-5 minutes (measured
    2026-07-16, both bundles, regardless of query type — the numbers are in the block
    comment above _drive_find_in_folder). So a single immediate query would report "not
    in the cloud" on a perfectly healthy backup. The deadline is what keeps that
    tolerance from becoming indefinite patience.

    THE COMPARISON IS md5, AND md5 IS THE REAL PROOF. name+size proves a file wearing
    the right label and roughly the right shape exists in the cloud; md5 proves the
    bytes in the cloud ARE the bytes on disk. Only the second one is worth building:
    the 2026-07-16 incident was a folder full of files with plausible names. md5 does a
    second job too: Drive computes md5Checksum SERVER-SIDE, and only ONCE THE CONTENT
    HAS LANDED. So md5 is what separates "a metadata row exists in that folder" from
    "the bytes actually uploaded" — which is why a metadata hit ALONE is not proof, and
    why an absent md5Checksum is a KEEP POLLING condition rather than either answer.

    Returns:
      checked   : bool   did we actually query Google?
      state     : 'verified' | 'failed' | 'skipped_not_configured'
      folder_id : str | None
      file_id   : str | None
      cloud_md5 : str | None
      local_md5 : str | None
      required  : bool   was CLOUD_VERIFY_REQUIRED in force for this run?
      note      : str    the honest human explanation — distinct per failure mode

    This function NEVER raises for policy reasons and never decides the job's fate;
    it reports. run_backup() owns the fail-closed decision.
    """
    log_fn = log_fn or log
    sleep_fn = sleep_fn or time.sleep
    monotonic_fn = monotonic_fn or time.monotonic
    creds_fn = creds_fn or _load_drive_creds
    token_fn = token_fn or _drive_access_token
    find_fn = find_fn or _drive_find_in_folder
    md5_fn = md5_fn or file_md5
    name = bundle_name or Path(bundle_path).name
    required = CLOUD_VERIFY_REQUIRED if required is None else required

    if folder_fn is None:
        def folder_fn(tok):
            comps, cnote = _drive_path_components(drive_dest, drive_mount)
            if comps is None:
                return None, cnote
            return _drive_resolve_folder_id(comps, tok)

    res = {"checked": False, "state": "skipped_not_configured", "folder_id": None,
           "file_id": None, "cloud_md5": None, "local_md5": None,
           "required": bool(required), "note": ""}

    creds, cstate, cnote = creds_fn()
    if cstate == "absent":
        # NOT an error — the check is simply not set up yet. But it is not silence
        # either: the caller turns this into a `proves` string that says so.
        res["note"] = f"cloud arrival was NOT checked: {cnote}"
        return res
    if cstate != "ok":
        res["checked"] = True
        res["state"] = "failed"
        res["note"] = (f"the Drive API credential exists but is UNUSABLE — {cnote}. "
                       f"This is a real failure, not an un-configured check.")
        return res

    res["checked"] = True
    try:
        res["local_md5"] = md5_fn(bundle_path)
    except OSError as e:
        res["state"] = "failed"
        res["note"] = f"could not md5 the local bundle {bundle_path} ({e!r})"
        return res

    token, err_code, tnote = token_fn(creds)
    if not token:
        res["state"] = "failed"
        res["note"] = _token_failure_note(err_code, tnote, creds, now=now)
        return res

    # Resolve the FOLDER we are about to query. Outside the poll: an unresolvable folder
    # is not a thing that becomes resolvable by waiting, and re-walking it every 15s
    # would just be a slower way of reporting the same failure.
    folder_id, folnote = folder_fn(token)
    if not folder_id:
        res["state"] = "failed"
        res["note"] = (
            f"could NOT resolve the Drive folder id for the backup destination — "
            f"{folnote}. Refusing to fall back to an UNSCOPED name query: 'a file with "
            f"this name and md5 exists somewhere in the Drive' would have been TRUE "
            f"throughout the 9-day wrong-folder incident this check exists to catch, so "
            f"it is not an answer to the question being asked. Cloud arrival is NOT "
            f"proven, and the reason is THIS — not an absent bundle.")
        return res
    res["folder_id"] = folder_id

    # Poll: the copy into the DriveFS mount returns before the upload finishes, AND
    # Drive's API lags the local write by ~1-5 minutes regardless of how it is asked
    # (measured 2026-07-16 — see the block comment above _drive_find_in_folder). So
    # "not there yet" is expected for a while and is NOT evidence of failure. The
    # deadline is what stops that tolerance from becoming indefinite patience, and md5
    # is what makes the eventual answer mean "the bytes landed" rather than "a row
    # appeared".
    end = monotonic_fn() + max(0, deadline_s)
    attempts = 0
    last = "no query was performed"
    while True:
        attempts += 1
        files, fnote = find_fn(name, folder_id, token)
        if files is None:
            # "We could not ask" and "Google says it is not there" mean OPPOSITE
            # things. Never blur them.
            res["state"] = "failed"
            res["note"] = f"the Drive API query FAILED — {fnote}"
            return res
        hits = list(files)
        if hits:
            hit = hits[0]
            res["file_id"] = hit.get("id")
            cloud_md5 = hit.get("md5Checksum")
            if cloud_md5:
                res["cloud_md5"] = cloud_md5
                extra = (f" NOTE: {len(hits)} files in the folder share this name; "
                         f"checked the first." if len(hits) > 1 else "")
                if cloud_md5.lower() == (res["local_md5"] or "").lower():
                    res["state"] = "verified"
                    res["note"] = (
                        f"CONFIRMED IN THE CLOUD: Drive file id {res['file_id']} in "
                        f"folder {folder_id} has md5 {cloud_md5}, matching the local "
                        f"bundle byte-for-byte (after {attempts} query attempt(s))."
                        f"{extra}")
                    return res
                # A concrete, differing checksum is not an in-flight upload — Drive
                # publishes md5Checksum for completed content. Fail now, do not wait.
                res["state"] = "failed"
                res["note"] = (
                    f"MD5 MISMATCH — Drive file id {res['file_id']} reports md5 "
                    f"{cloud_md5} but the local bundle is {res['local_md5']}. The bytes "
                    f"in the cloud are NOT the bytes on disk; cloud arrival of THIS "
                    f"bundle is NOT proven.{extra}")
                return res
            # REGISTERED, NOT LANDED. Drive computes md5Checksum server-side and only
            # once the content is there, so its absence means the bytes are still in
            # flight. That is neither success nor failure — it is the one honest reason
            # to keep polling, and it is why a metadata hit alone proves nothing.
            last = (f"Drive HAS a file named {name} (id {res['file_id']}) in the backup "
                    f"folder {folder_id}, but reports NO md5Checksum for it — the "
                    f"metadata row is REGISTERED and the CONTENT HAS NOT LANDED "
                    f"(Drive computes md5 server-side only after content lands), so "
                    f"the upload is still in flight")
        else:
            last = (f"Drive reports NO file named {name} in the backup folder "
                    f"{folder_id} — the bundle is ABSENT from Drive's answer for that "
                    f"folder ({fnote})")
        if monotonic_fn() >= end:
            break
        sleep_fn(min(interval_s, max(0, end - monotonic_fn())))

    res["state"] = "failed"
    res["note"] = (
        f"NOT CONFIRMED IN THE CLOUD within {deadline_s}s ({attempts} query "
        f"attempt(s)): {last}. The bundle is on the Drive volume, but Google has not "
        f"acknowledged its content. That is either an unusually slow upload, the "
        f"API lag outlasting the deadline, or the silent non-upload this check exists "
        f"to catch — and we do not get to assume which. Cloud arrival is NOT proven.")
    return res


# --------------------------------------------------------------------------- #
# Retention — the ONLY deletion anywhere in this job
# --------------------------------------------------------------------------- #
def prune_old_bundles(directory, keep: int = KEEP_LAST, *, allowed_dirs=None,
                      list_fn=None, delete_fn=None, log_fn=None) -> list[str]:
    """Delete bundles THIS JOB created, keeping the newest `keep`. -> deleted names.

    THREE INDEPENDENT GUARDS, because a retention bug that eats the wrong file is
    worse than no retention at all:
      1. ALLOW-LIST: `directory` must be one of the two backup dirs (local backups /
         the Drive TradingDesk-Backups folder). Anything else -> delete nothing.
      2. NAME: the filename must match BUNDLE_RE EXACTLY
         (tradingdesk-repo-YYYYMMDD-HHMMSS.bundle). This is why the pattern is strict
         rather than 'tradingdesk*.bundle': the Drive folder already holds a
         hand-made `tradingdesk-full-20260716.bundle` rescue copy that we must NEVER
         touch. A loose glob would have eaten it.
      3. FILES ONLY: directories and anything non-file are skipped.
    Newest-first is decided by filename (the timestamp is IN the name and is
    zero-padded, so lexical == chronological), NOT by mtime — a Drive copy's mtime
    is whatever the filesystem felt like and must not decide what gets deleted.
    """
    log_fn = log_fn or log
    directory = Path(directory)

    if allowed_dirs is None:
        allowed_dirs = _allowed_prune_dirs()
    if not any(os.path.normcase(str(directory)) == os.path.normcase(str(a))
               for a in allowed_dirs):
        log_fn(f"retention REFUSED for {directory} — not an allow-listed backup dir")
        return []

    list_fn = list_fn or (lambda d: sorted(p.name for p in Path(d).iterdir()
                                           if p.is_file()))
    delete_fn = delete_fn or (lambda p: Path(p).unlink())

    try:
        names = [n for n in list_fn(directory) if BUNDLE_RE.match(n)]
    except OSError as e:
        log_fn(f"retention: could not list {directory} ({e!r})")
        return []

    names.sort()                       # lexical == chronological by construction
    doomed = names[:-keep] if keep > 0 and len(names) > keep else []
    deleted: list[str] = []
    for n in doomed:
        target = Path(directory) / n
        try:
            delete_fn(target)
            deleted.append(n)
            log_fn(f"retention: deleted old bundle {target}")
        except OSError as e:
            log_fn(f"retention: could not delete {target} ({e!r})")
    return deleted


def _allowed_prune_dirs() -> list[str]:
    """The only two dirs retention may ever touch (Drive dir added when resolved)."""
    dirs = [str(LOCAL_BACKUP_DIR)]
    info = resolve_drive_dest()
    if info["resolved"]:
        dirs.append(info["dest"])
    return dirs


# --------------------------------------------------------------------------- #
# Status / heartbeat — refreshed ONLY on verified success
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

    This single line is the entire contract with heartbeat_alarm.py: its mtime is the
    "last known-good verified backup" clock. Refreshing it on a FAILED or PARTIAL
    backup would re-arm the alarm's freshness window and manufacture exactly the
    silent green light that let 85 commits rot for 9 days. If the backup did not
    fully verify, this file must stay untouched and go cold.

    (Deliberate wording note: the text must not contain the literal word "COMPLETE" —
    heartbeat_alarm.assess() treats a "COMPLETE" marker in the heartbeat text as a
    finished-job signal. Harmless here, but we keep the semantics unambiguous.)
    """
    path = Path(heartbeat_file or HEARTBEAT_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        log(f"could not write heartbeat ({e!r})")


# --------------------------------------------------------------------------- #
# The job — injectable so tests drive it offline
# --------------------------------------------------------------------------- #
def run_backup(*, now=None, dry_run: bool = False, force: bool = False, resolve_fn=None,
               paused_fn=None, create_fn=None, verify_fn=None, head_fn=None,
               reuse_fn=None, copy_fn=None, prune_fn=None, facts_fn=None, size_fn=None,
               status_fn=None, heartbeat_fn=None, cloud_fn=None, log_fn=None) -> dict:
    """One backup run. Returns the status dict. Never raises for policy reasons.

    THE INVARIANT, stated once so it cannot be lost in a refactor:
        ok=True  <=>  a bundle verified okay-with-complete-history locally,
                      AND is present on a confirmed Drive-managed destination,
                      AND verified there too,
                      AND records the repo's CURRENT HEAD,
                      AND Drive sync was not visibly paused,
                      AND (if CLOUD_VERIFY_REQUIRED) Google confirmed the bundle's
                          md5 in the cloud.
        heartbeat is refreshed IF AND ONLY IF ok=True.
    Any other outcome leaves the heartbeat cold so the alarm fires on silence.

    NOTE WHAT THE INVARIANT DOES *NOT* SAY: "this run created a bundle". A run that
    proves the above about a bundle an EARLIER run created has established exactly the
    same fact about the world — a verified, current, off-machine backup exists — and
    that fact, not the act of writing 41MB, is what the heartbeat has always meant. So
    a skip legitimately feeds it. `state` is what tells the two apart, and `proves`
    names the bundle being leaned on; neither is allowed to blur them. Every clause is
    RE-PROVEN on a skip (see find_reusable_bundle) — nothing is inherited from the
    previous run's status file.

    `force` bypasses the skip only. It cannot weaken a check: a forced run still has to
    earn ok=True the same way.

    THE ONE ASYMMETRY, and it is deliberate: while CLOUD_VERIFY_REQUIRED is False a
    cloud-check failure does NOT set ok=False — it downgrades `proves` and is
    recorded in `errors`. That is the grace period for confirming the credential
    works, and it is why `proves` (not ok) is the field that tells you what a run
    actually proved. Every OTHER check remains hard-fail. Flip the flag and the
    asymmetry disappears.
    """
    log_fn = log_fn or log
    resolve_fn = resolve_fn or resolve_drive_dest
    paused_fn = paused_fn or is_sync_paused
    verify_fn = verify_fn or bundle_verify
    head_fn = head_fn or bundle_head_sha
    reuse_fn = reuse_fn or find_reusable_bundle
    facts_fn = facts_fn or repo_facts
    prune_fn = prune_fn or prune_old_bundles
    status_fn = status_fn or write_status
    heartbeat_fn = heartbeat_fn or touch_heartbeat
    cloud_fn = cloud_fn or verify_cloud_arrival
    size_fn = size_fn or (lambda p: Path(p).stat().st_size)
    copy_fn = copy_fn or (lambda s, d: shutil.copy2(str(s), str(d)))

    now = now or dt.datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    name = f"{BUNDLE_PREFIX}{stamp}.bundle"

    st: dict = {
        "job": "repo_backup",
        "timestamp": now.isoformat(timespec="seconds"),
        "ok": False,
        # What this run DID — never inferred from ok. Like `proves`, it starts at the
        # pessimistic value and is only ever RAISED by something that actually happened.
        "state": STATE_FAILED,
        "bundle_name": name,
        "local_path": None,
        "drive_path": None,
        "size_bytes": None,
        "head_sha": None,
        "commit_count": None,
        "verify_local": None,
        "verify_drive": None,
        "drive_resolved": False,
        "drive_managed": False,
        "drive_source": None,
        "drive_note": None,
        "sync_paused": None,
        "sync_note": None,
        "forced": bool(force),
        "reuse_note": None,
        "pruned_local": [],
        "pruned_drive": [],
        "cloud": {"checked": False, "state": "skipped_not_configured",
                  "folder_id": None, "file_id": None, "cloud_md5": None,
                  "local_md5": None, "required": bool(CLOUD_VERIFY_REQUIRED),
                  "note": ("cloud arrival was NOT checked — the run did not get far "
                           "enough to have a bundle to look for")},
        "errors": [],
        "dry_run": bool(dry_run),
        # Stated in the artifact itself so nobody reading this file later mistakes it
        # for more than it is. Starts at "nothing proven" and is only ever RAISED by a
        # check that actually passed — a run that dies early must not carry a string
        # describing verification it never did.
        "proves": PROVES_FAILED_RUN,
    }

    def _fail(msg: str) -> dict:
        st["errors"].append(msg)
        st["ok"] = False
        st["state"] = STATE_FAILED
        st["proves"] = PROVES_FAILED_RUN
        log_fn(f"FAIL: {msg}")
        status_fn(st)
        return st

    facts = facts_fn()
    st["head_sha"] = facts.get("head_sha")
    st["commit_count"] = facts.get("commit_count")

    # 1. Resolve Drive FIRST. If we can't, there is no point bundling — and we must
    #    NOT quietly fall back to "local only" and call it a success.
    info = resolve_fn()
    st["drive_resolved"] = bool(info.get("resolved"))
    st["drive_managed"] = bool(info.get("drive_managed"))
    st["drive_source"] = info.get("source")
    st["drive_note"] = info.get("note")
    if not info.get("resolved"):
        return _fail("Drive destination could NOT be resolved — refusing to report "
                     "success. The Drive copy is NOT optional; a silent skip is the "
                     f"exact failure this job exists to prevent. Detail: {info.get('note')}")
    log_fn(f"Drive resolved via {info['source']}: {info['dest']}")

    # 2. Pause state. Paused == an ALARM condition, not a pass: writes into the mount
    #    are accepted locally and silently never uploaded (observed 2026-07-16).
    paused, why = paused_fn()
    st["sync_paused"] = paused
    st["sync_note"] = why
    if paused is True:
        return _fail(f"Drive sync is PAUSED — a write into the mount would be accepted "
                     f"locally and never uploaded. Refusing to report success. {why}")
    if paused is None:
        log_fn(f"WARN: could not determine Drive pause state — {why}")

    if dry_run:
        st["ok"] = False
        st["state"] = STATE_DRY_RUN
        st["errors"].append("dry-run: created nothing, deleted nothing, "
                            "heartbeat NOT refreshed")
        log_fn(f"dry-run: would write {name} to {info['dest']} and {LOCAL_BACKUP_DIR}")
        status_fn(st)
        return st

    local_dir = LOCAL_BACKUP_DIR
    try:
        local_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _fail(f"could not create local backup dir {local_dir} ({e!r})")
    dest_dir = Path(info["dest"])

    # 3. SKIP CHECK. Is the newest existing bundle already a PROVEN-good backup of the
    #    current HEAD, in both places? Only affirmative proof buys a skip; anything
    #    else — including "we couldn't tell" — falls through and bundles. See
    #    find_reusable_bundle for why the asymmetry is absolute.
    reused = None
    if force:
        st["reuse_note"] = ("--force: the HEAD-unchanged check was not consulted; "
                            "bundling unconditionally")
        log_fn(st["reuse_note"])
    else:
        reused, why = reuse_fn(local_dir, dest_dir, st["head_sha"],
                               verify_fn=verify_fn, head_fn=head_fn, log_fn=log_fn)
        st["reuse_note"] = why
        if not reused:
            log_fn(f"bundling fresh: {why}")

    if reused:
        # 3a. Reuse. Note what did NOT happen: no create, no copy, no claim carried
        #     over from the previous run. Every field below was PROVEN moments ago by
        #     find_reusable_bundle against the files as they exist right now.
        name = reused["name"]
        local_path = Path(reused["local_path"])
        drive_path = Path(reused["drive_path"])
        st["bundle_name"] = name
        st["local_path"] = reused["local_path"]
        st["drive_path"] = reused["drive_path"]
        st["verify_local"] = reused["verify_local"]
        st["verify_drive"] = reused["verify_drive"]
        st["state"] = STATE_SKIPPED_HEAD_UNCHANGED
        try:
            st["size_bytes"] = size_fn(local_path)
        except OSError:
            pass
        log_fn(f"SKIP: no new bundle — {name} already covers HEAD {st['head_sha']}")
    else:
        # 3b. Bundle locally, then verify locally.
        local_path = local_dir / name
        st["local_path"] = str(local_path)

        create = create_fn or (lambda dest: _git(["bundle", "create", str(dest), "--all"]))
        try:
            proc = create(local_path)
        except Exception as e:  # noqa: BLE001
            return _fail(f"git bundle create raised {e!r}")
        if getattr(proc, "returncode", 1) != 0:
            return _fail(f"git bundle create failed: "
                         f"{(getattr(proc, 'stderr', '') or '')[:400]}")

        ok, detail = verify_fn(local_path)
        st["verify_local"] = detail
        if not ok:
            return _fail(f"LOCAL bundle failed verification — {detail}")
        try:
            st["size_bytes"] = size_fn(local_path)
        except OSError:
            pass
        log_fn(f"local bundle verified: {local_path} ({st['size_bytes']} bytes)")

        # 4. Copy to Drive, then RE-VERIFY at the destination. Verifying the copy is
        #    what turns "we called copy()" into "git can read a complete bundle there".
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return _fail(f"could not create Drive backup dir {dest_dir} ({e!r})")
        drive_path = dest_dir / name
        st["drive_path"] = str(drive_path)
        try:
            copy_fn(local_path, drive_path)
        except Exception as e:  # noqa: BLE001
            return _fail(f"copy to Drive destination failed ({e!r})")

        ok, detail = verify_fn(drive_path)
        st["verify_drive"] = detail
        if not ok:
            return _fail(f"bundle at the DRIVE destination failed verification — {detail}")
        log_fn(f"drive bundle verified: {drive_path}")
        st["state"] = STATE_VERIFIED_NEW

    # 5. CLOUD ARRIVAL. Everything above this line is a local filesystem check and
    #    proves nothing about Google having the bytes — which is the failure that ran
    #    silent for 9 days. We md5 the LOCAL bundle (the canonical artifact) and ask
    #    Drive for its own md5: matching md5s prove the cloud copy IS this bundle.
    #    Runs BEFORE retention, so a fail-closed cloud failure deletes nothing.
    #
    #    A SKIPPED run runs this too, against the bundle it is leaning on. It must:
    #    otherwise flipping CLOUD_VERIFY_REQUIRED to True would leave a hole where a
    #    skip quietly feeds the heartbeat with no cloud proof — the requirement
    #    silently not applying to the majority of runs. The check is the same, the
    #    fail-closed decision is the same; only the bundle's author differs.
    #
    #    The destination + mount are handed DOWN from the resolve step rather than
    #    re-derived (let alone hardcoded) inside the check: the folder the check
    #    interrogates in the cloud must be the same folder this run actually wrote to,
    #    and resolve_drive_dest() is the one place that decides what that is.
    cloud = cloud_fn(local_path, name, str(dest_dir), info.get("mount"))
    st["cloud"] = cloud
    state = cloud.get("state")
    if state == "verified":
        cloud_proves = PROVES_CLOUD_VERIFIED
        log_fn(f"cloud arrival VERIFIED: {cloud.get('note')}")
    elif state == "skipped_not_configured":
        # Not configured is not the same as passed, and the artifact must not blur
        # the two. The job still succeeds; `proves` says plainly it wasn't checked.
        cloud_proves = PROVES_CLOUD_NOT_CHECKED
        log_fn(f"cloud arrival NOT CHECKED: {cloud.get('note')}")
        if CLOUD_VERIFY_REQUIRED:
            return _fail(f"cloud-arrival verification is REQUIRED but no usable Drive "
                         f"API credential is configured — {cloud.get('note')}")
    else:
        cloud_proves = PROVES_CLOUD_FAILED
        log_fn(f"CLOUD ARRIVAL CHECK FAILED: {cloud.get('note')}")
        if CLOUD_VERIFY_REQUIRED:
            return _fail(f"cloud-arrival verification FAILED — {cloud.get('note')}")
        st["errors"].append(
            f"cloud-arrival check FAILED (not fatal while CLOUD_VERIFY_REQUIRED is "
            f"False — the local + Drive-volume bundle still verified) — "
            f"{cloud.get('note')}")

    # A skip earns the SAME cloud clause, wrapped in the admission that this run
    # created nothing and the name of the bundle actually carrying the backup.
    st["proves"] = proves_skipped(name, cloud_proves) if reused else cloud_proves

    # 6. Retention — only our own bundles, only in the two allow-listed dirs.
    allowed = [str(local_dir), str(dest_dir)]
    st["pruned_local"] = prune_fn(local_dir, KEEP_LAST, allowed_dirs=allowed)
    st["pruned_drive"] = prune_fn(dest_dir, KEEP_LAST, allowed_dirs=allowed)

    # 7. Success — and ONLY now does the heartbeat move. A skip reaches here honestly:
    #    a verified bundle covering the current HEAD exists in both places, which is
    #    the only thing this heartbeat has ever asserted. It still SAYS which it was,
    #    so the one line a paged human reads at 2am cannot imply work that never
    #    happened.
    st["ok"] = True
    status_fn(st)
    made = "no-new-bundle(HEAD unchanged, re-verified)" if reused else "new-bundle"
    heartbeat_fn(
        f"{now:%Y-%m-%d %H:%M:%S}  repo backup verified  head={st['head_sha']} "
        f"commits={st['commit_count']} size={st['size_bytes']} "
        f"verify=okay+full-history {made} cloud={state} drive={drive_path}")
    log_fn(f"SUCCESS: verified backup {name} -> local + Drive "
           f"(state={st['state']} cloud={state})")
    return st


# --------------------------------------------------------------------------- #
# --wrap — the interactive path (CLAUDE.md's `wrap` force-word runs this)
# --------------------------------------------------------------------------- #
# Output only. --wrap adds a banner, a human summary, and a machine-readable last
# line; it changes NO check and NO exit code. The scheduled path stays as quiet as it
# is today (log() already prints, and under Task Scheduler that goes nowhere anyway).
def wrap_summary(st: dict | None, *, error: str | None = None) -> dict:
    """The compact JSON printed as the LAST line of a --wrap run.

    This is what the calling session reports from — so it carries `proves` and
    `errors`, not just `ok`. A session that says "backed up ✓" off a bare boolean has
    reinvented the silent green light in a nicer font; `state` and `proves` are what
    make the report say what actually happened.
    """
    st = st or {}
    out = {
        "ok": bool(st.get("ok")),
        "state": st.get("state") or STATE_FAILED,
        "bundle_name": st.get("bundle_name"),
        "head_sha": st.get("head_sha"),
        "size_bytes": st.get("size_bytes"),
        "verify_local": st.get("verify_local"),
        "verify_drive": st.get("verify_drive"),
        "drive_path": st.get("drive_path"),
        "cloud_state": (st.get("cloud") or {}).get("state"),
        "errors": list(st.get("errors") or []),
        "proves": st.get("proves") or PROVES_FAILED_RUN,
    }
    if error:
        out["ok"] = False
        out["state"] = STATE_FAILED
        out["errors"] = out["errors"] + [error]
        out["proves"] = PROVES_FAILED_RUN
    return out


_WRAP_VERDICT = {
    STATE_VERIFIED_NEW: "NEW VERIFIED BUNDLE — local + Drive",
    STATE_SKIPPED_HEAD_UNCHANGED: "NO NEW BUNDLE — HEAD unchanged; the existing bundle "
                                  "was re-verified in both places",
    STATE_DRY_RUN: "DRY RUN — nothing created, nothing deleted, heartbeat untouched",
    STATE_FAILED: "FAILED — this run did NOT establish a verified backup",
}


def print_wrap_report(st: dict | None, *, error: str | None = None, out_fn=None) -> dict:
    """Human summary, then the compact JSON. The JSON is ALWAYS the final line.

    Emitted on failure too: a session that wraps and gets nothing machine-readable back
    would have to guess, and guessing about backups is the whole problem.
    """
    out_fn = out_fn or (lambda s: print(s, flush=True))
    s = wrap_summary(st, error=error)
    out_fn("")
    out_fn(f"  result : {_WRAP_VERDICT.get(s['state'], s['state'])}")
    out_fn(f"  bundle : {s['bundle_name']}  ({s['size_bytes']} bytes)")
    out_fn(f"  head   : {s['head_sha']}")
    out_fn(f"  verify : local={s['verify_local']}  |  drive={s['verify_drive']}")
    out_fn(f"  drive  : {s['drive_path']}")
    out_fn(f"  cloud  : {s['cloud_state']}")
    for e in s["errors"]:
        out_fn(f"  ERROR  : {e}")
    out_fn(f"  proves : {s['proves']}")
    out_fn("")
    out_fn(json.dumps(s, separators=(",", ":"), default=str))   # LAST LINE. Keep it last.
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description="Verified git-bundle backup of the TradingDesk repo.")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve + report only; create nothing, delete nothing, "
                         "and never refresh the heartbeat.")
    ap.add_argument("--force", action="store_true",
                    help="bundle even if HEAD has not moved since the newest verified "
                         "bundle (bypasses the redundant-bundle skip; weakens no check).")
    ap.add_argument("--wrap", action="store_true",
                    help="interactive mode for CLAUDE.md's `wrap` force-word: identical "
                         "job and identical fail-closed exit codes, plus progress on "
                         "stdout and a compact-JSON summary as the LAST line.")
    args = ap.parse_args()

    if args.wrap:
        print(f"repo backup (wrap): {REPO} -> local + Drive, verifying both.", flush=True)
    try:
        st = run_backup(dry_run=args.dry_run, force=args.force)
    except Exception as e:  # noqa: BLE001 — an unexpected error is still a FAILED backup
        log(f"UNEXPECTED ERROR (reporting failure, NOT success): {e!r}")
        if args.wrap:
            print_wrap_report(None, error=f"unexpected error: {e!r}")
        return 2
    if args.wrap:
        print_wrap_report(st)
    if args.dry_run:
        return 0 if st.get("drive_resolved") else 1
    return 0 if st.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
