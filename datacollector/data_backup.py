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

  1. The heartbeat is refreshed ONLY on a fully verified success — copy succeeded AND
     an independent checksum verification (`rclone check`) reported 0 differences and
     0 errors. Every failure path (rclone missing, copy error, ANY check difference or
     error) leaves the heartbeat UNTOUCHED and exits non-zero. A failed backup and a
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

SECRETS
-------
The rclone remote token lives in the rclone config file (RCLONE_CONFIG, under the
off-Drive secrets folder). This job references that PATH on the command line so rclone
can authenticate, but it NEVER reads, prints, echoes, or logs the file's CONTENTS.
Nothing here surfaces a token, and rclone's own output does not carry it.

Run:
    <venv python> data_backup.py            # real run (the DataBackupDaily path)
    <venv python> data_backup.py --dry-run  # rclone --dry-run: transfer nothing, do
                                            #   NOT verify, do NOT refresh the heartbeat

Exit codes: 0 = verified success (copy done AND check clean; heartbeat refreshed).
Non-zero = failure of some kind (rclone missing, copy error, check differences/errors),
heartbeat deliberately NOT refreshed, so heartbeat_alarm.py pages on the silence.
"""

from __future__ import annotations

import argparse
import datetime as dt
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
PROVES_FAILED = (
    "nothing — this run did not complete a verified data backup; read `errors` for the "
    "failure (rclone copy and rclone check did not BOTH fully succeed), so an "
    "up-to-date, checksum-verified off-machine copy is NOT proven")


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


def copy_argv(rclone_path: str, dry_run: bool = False) -> list[str]:
    """`rclone copy SRC REMOTE` — additive, never deletes on the remote (see the
    COPY-NOT-SYNC note in the module docstring). --checksum forces content comparison
    on the copy itself; --drive-use-trash=false so an overwrite frees space instead of
    filling Drive's trash."""
    argv = [rclone_path, "copy", str(DATA_SOURCE), RCLONE_REMOTE]
    argv += _base_flags()
    argv += ["--checksum", "--drive-use-trash=false"]
    if dry_run:
        argv += ["--dry-run"]
    return argv


def check_argv(rclone_path: str) -> list[str]:
    """`rclone check SRC REMOTE` — recompute + compare hashes (md5) local vs remote.
    This is the integrity proof. rclone check compares by hash by default when both
    ends support it (Google Drive does), so no extra flag is needed to make it a
    checksum comparison."""
    argv = [rclone_path, "check", str(DATA_SOURCE), RCLONE_REMOTE]
    argv += _base_flags()
    return argv


def _run_copy(rclone_path: str, dry_run: bool):
    return subprocess.run(copy_argv(rclone_path, dry_run), capture_output=True,
                          text=True, timeout=RCLONE_TIMEOUT, check=False)


def _run_check(rclone_path: str):
    return subprocess.run(check_argv(rclone_path), capture_output=True,
                          text=True, timeout=RCLONE_TIMEOUT, check=False)


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
def run_backup(*, now=None, dry_run: bool = False, resolve_fn=None, copy_fn=None,
               check_fn=None, parse_fn=None, bytes_fn=None, status_fn=None,
               heartbeat_fn=None, log_fn=None) -> dict:
    """One data-backup run. Returns the status dict. Never raises for policy reasons.

    THE INVARIANT, stated once so it cannot be lost in a refactor:
        ok=True  <=>  rclone is resolved,
                      AND `rclone copy` exited 0,
                      AND `rclone check` exited 0 AND reported 0 differences / 0 errors
                          / 0 missing (a genuine checksum match of local vs remote).
        heartbeat is refreshed IF AND ONLY IF ok=True.
    Any other outcome leaves the heartbeat cold so the alarm fires on silence.

    A --dry-run passes rclone --dry-run to copy, runs NO check, and is NEVER a success
    (ok stays False, heartbeat untouched) — the same contract repo_backup.py uses.
    """
    log_fn = log_fn or log
    resolve_fn = resolve_fn or resolve_rclone
    copy_fn = copy_fn or _run_copy
    check_fn = check_fn or _run_check
    parse_fn = parse_fn or parse_check_output
    bytes_fn = bytes_fn or parse_transferred_bytes
    status_fn = status_fn or write_status
    heartbeat_fn = heartbeat_fn or touch_heartbeat

    now = now or dt.datetime.now()

    st: dict = {
        "job": "data_backup",
        "timestamp": now.isoformat(timespec="seconds"),
        "ok": False,
        "source": str(DATA_SOURCE),
        "remote": RCLONE_REMOTE,
        "rclone_path": None,
        "rclone_note": None,
        "copy_returncode": None,
        "check_returncode": None,
        "files_checked": None,
        "bytes": None,
        "check_result": None,
        "differences": None,
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

    # 2. Copy (additive; never deletes on the remote). --dry-run passes through.
    try:
        proc = copy_fn(rclone_path, dry_run)
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
    try:
        cproc = check_fn(rclone_path)
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

    # Success requires BOTH: rclone check's own exit code is 0 (authoritative — it is
    # non-zero on ANY difference/error) AND our parse found no problems. The two agree
    # in practice; requiring both means a parse that missed a bad line still fails via
    # the exit code, and an exit code we somehow misread still fails via the parse.
    if st["check_returncode"] != 0:
        return _fail(f"rclone check FAILED verification (exit "
                     f"{st['check_returncode']}): {parsed.get('summary')} — the remote "
                     f"copy is NOT proven byte-identical; {ccombined.strip()[-400:]}")
    if parsed.get("problems", 0) > 0:
        return _fail(f"rclone check reported differences/errors despite exit 0: "
                     f"{parsed.get('summary')} — refusing to report a verified backup")

    # 5. Verified success — and ONLY now does the heartbeat move.
    st["ok"] = True
    n = st["files_checked"]
    if isinstance(n, int):
        st["proves"] = PROVES_VERIFIED.format(n=n, src=DATA_SOURCE, remote=RCLONE_REMOTE)
    else:
        st["proves"] = PROVES_VERIFIED_NO_COUNT.format(src=DATA_SOURCE,
                                                       remote=RCLONE_REMOTE)
    status_fn(st)
    heartbeat_fn(
        f"{now:%Y-%m-%d %H:%M:%S}  data backup verified  source={DATA_SOURCE} "
        f"remote={RCLONE_REMOTE} files_checked={n} bytes_this_run={st['bytes']} "
        f"check=0-differences+0-errors")
    log_fn(f"SUCCESS: verified data backup ({st['check_result']}) -> {RCLONE_REMOTE}")
    return st


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verified rclone backup of C:\\TradingDesk-Local to Google Drive.")
    ap.add_argument("--dry-run", action="store_true",
                    help="pass rclone --dry-run to copy: transfer nothing, run NO "
                         "checksum verification, and never refresh the heartbeat.")
    args = ap.parse_args()

    try:
        st = run_backup(dry_run=args.dry_run)
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
