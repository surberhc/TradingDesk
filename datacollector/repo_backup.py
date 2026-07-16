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

WHAT IT DOES
------------
  * `git bundle create <dest> --all`  — a full, self-contained clone of every ref.
  * `git bundle verify` — must report BOTH "is okay" AND "records a complete
    history". Anything else is a hard failure; we do not report success.
  * Writes the bundle to the resolved Google Drive folder (TradingDesk-Backups)
    AND keeps a local copy under C:\TradingDesk-Local\backups\ (Drive is not
    trusted alone — it is the thing that just failed us).
  * Re-verifies the bundle at the Drive destination after the copy.
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
  * NOTHING HERE PROVES CLOUD ARRIVAL. Every check in this file is a local
    filesystem/log check. Confirming the bytes are in the Drive mount is NOT
    confirming they are in Google's datacenter. Proving arrival would require
    querying the Drive API for the uploaded file's id/checksum, which this job
    deliberately does not do (no new deps, no OAuth). The honest guarantee is:
    "a verified-complete bundle exists locally AND has been placed on the real
    Drive volume with sync not visibly paused." The LOCAL copy is what makes that
    acceptable — it is a real backup on its own.

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
    <venv python> repo_backup.py            # real run
    <venv python> repo_backup.py --dry-run  # resolve + report, create nothing, delete nothing

Exit codes: 0 = verified success (heartbeat refreshed). Non-zero = failure of some
kind (heartbeat deliberately NOT refreshed, so the alarm goes off on schedule).
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
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
# git helpers — READ-ONLY git only (bundle create/verify, rev-parse, rev-list).
# This job must never mutate the repo: no add/commit/reset/checkout/clean/gc.
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
def run_backup(*, now=None, dry_run: bool = False, resolve_fn=None, paused_fn=None,
               create_fn=None, verify_fn=None, copy_fn=None, prune_fn=None,
               facts_fn=None, size_fn=None, status_fn=None, heartbeat_fn=None,
               log_fn=None) -> dict:
    """One backup run. Returns the status dict. Never raises for policy reasons.

    THE INVARIANT, stated once so it cannot be lost in a refactor:
        ok=True  <=>  a bundle verified okay-with-complete-history locally,
                      AND landed on a confirmed Drive-managed destination,
                      AND re-verified there,
                      AND Drive sync was not visibly paused.
        heartbeat is refreshed IF AND ONLY IF ok=True.
    Any other outcome leaves the heartbeat cold so the alarm fires on silence.
    """
    log_fn = log_fn or log
    resolve_fn = resolve_fn or resolve_drive_dest
    paused_fn = paused_fn or is_sync_paused
    verify_fn = verify_fn or bundle_verify
    facts_fn = facts_fn or repo_facts
    prune_fn = prune_fn or prune_old_bundles
    status_fn = status_fn or write_status
    heartbeat_fn = heartbeat_fn or touch_heartbeat
    size_fn = size_fn or (lambda p: Path(p).stat().st_size)
    copy_fn = copy_fn or (lambda s, d: shutil.copy2(str(s), str(d)))

    now = now or dt.datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    name = f"{BUNDLE_PREFIX}{stamp}.bundle"

    st: dict = {
        "job": "repo_backup",
        "timestamp": now.isoformat(timespec="seconds"),
        "ok": False,
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
        "pruned_local": [],
        "pruned_drive": [],
        "errors": [],
        "dry_run": bool(dry_run),
        # Stated in the artifact itself so nobody reading this file later mistakes
        # it for proof of upload.
        "proves": ("bundle verified locally and at the Drive-volume destination; "
                   "does NOT prove cloud arrival"),
    }

    def _fail(msg: str) -> dict:
        st["errors"].append(msg)
        st["ok"] = False
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
        st["errors"].append("dry-run: created nothing, deleted nothing, "
                            "heartbeat NOT refreshed")
        log_fn(f"dry-run: would write {name} to {info['dest']} and {LOCAL_BACKUP_DIR}")
        status_fn(st)
        return st

    # 3. Bundle locally, then verify locally.
    local_dir = LOCAL_BACKUP_DIR
    try:
        local_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _fail(f"could not create local backup dir {local_dir} ({e!r})")
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

    # 4. Copy to Drive, then RE-VERIFY at the destination. Verifying the copy is what
    #    turns "we called copy()" into "git can read a complete bundle over there".
    dest_dir = Path(info["dest"])
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

    # 5. Retention — only our own bundles, only in the two allow-listed dirs.
    allowed = [str(local_dir), str(dest_dir)]
    st["pruned_local"] = prune_fn(local_dir, KEEP_LAST, allowed_dirs=allowed)
    st["pruned_drive"] = prune_fn(dest_dir, KEEP_LAST, allowed_dirs=allowed)

    # 6. Success — and ONLY now does the heartbeat move.
    st["ok"] = True
    status_fn(st)
    heartbeat_fn(
        f"{now:%Y-%m-%d %H:%M:%S}  repo backup verified  head={st['head_sha']} "
        f"commits={st['commit_count']} size={st['size_bytes']} "
        f"verify=okay+full-history drive={drive_path}")
    log_fn(f"SUCCESS: verified backup {name} -> local + Drive")
    return st


def main() -> int:
    ap = argparse.ArgumentParser(description="Verified git-bundle backup of the TradingDesk repo.")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve + report only; create nothing, delete nothing, "
                         "and never refresh the heartbeat.")
    args = ap.parse_args()
    try:
        st = run_backup(dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001 — an unexpected error is still a FAILED backup
        log(f"UNEXPECTED ERROR (reporting failure, NOT success): {e!r}")
        return 2
    if args.dry_run:
        return 0 if st.get("drive_resolved") else 1
    return 0 if st.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
