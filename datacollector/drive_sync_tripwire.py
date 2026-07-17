r"""drive_sync_tripwire.py — pages if C:\TradingDesk-Local ever comes under Google
Drive sync/backup/mirror management.

WHY THIS EXISTS (read before changing anything)
------------------------------------------------
C:\TradingDesk-Local holds ~99 GB of IRREPLACEABLE market data, the venv, running
state, and secrets. It is deliberately OUTSIDE Google Drive because Drive's sync
client corrupts market data AND once silently synced the WRONG FOLDER for 9 days
(2026-07-07 .. 2026-07-16), moving/duplicating folders and orphaning work while NO
ERROR WAS EVER RAISED (see repo_backup.py's header for the full incident).

We now back the *repo* up to Drive via rclone (explicit checksummed API uploads —
safe). That does NOT put TradingDesk-Local under the Drive Desktop SYNC client, and
must never. The residual risk this module addresses is purely HUMAN: someone later
opens Google Drive Desktop and manually adds C:\TradingDesk-Local (or a subfolder,
or an ancestor) to the sync/mirror/backup client — re-arming the exact bomb. This
converts "everyone must remember never to do that" into "you get PAGED the moment
anyone does."

WHAT IT DETECTS, AND HOW HONESTLY (the feasibility was investigated on THIS machine
2026-07-17 before this was written; the numbers below are measured, not assumed)
--------------------------------------------------------------------------------
There are TWO distinct threats, and they need DIFFERENT detectors:

  THREAT 1 — MIRROR / STREAMING: the protected tree ends up UNDER a DriveFS virtual
    volume (a "My Drive" mount). RELIABLY DETECTABLE. We REUSE repo_backup's
    is_drive_managed(), which asks the OS for the path's volume mount root + label:
    a real DriveFS mount is a separate volume labelled 'Google Drive', an ordinary
    local folder resolves to 'C:\' labelled 'Windows'. Verified on the live machine:
    every protected path currently resolves to the SYSTEM volume (healthy).

  THREAT 2 — FOLDER BACKUP / MIRROR-OF-A-LOCAL-FOLDER ("My Computer" tab, or a
    mirrored local folder): Drive syncs an arbitrary LOCAL folder IN PLACE. The files
    STAY as ordinary local files on C:\ — so a volume/mount test (THREAT 1) does NOT
    catch it. This is the MORE DANGEROUS one: it is the shape of the 9-day wrong-folder
    incident. Detecting it requires reading DriveFS's own record of which local folders
    it is configured to sync. On this machine that record IS readable:

      * %LOCALAPPDATA%\Google\DriveFS\root_preference_sqlite.db -> `roots` table.
        Columns include root_path AND last_seen_absolute_path (full absolute local
        paths), sync_type, is_my_drive, state. This is the authoritative registry of
        mirror/backup roots. repo_backup already reads this same table. Measured today
        it has 0 rows (a clean streaming-mode install — nothing mirrored/backed-up),
        which IS the healthy baseline. When a folder is added for backup/mirror, a row
        appears here whose root_path names the local folder.
      * %LOCALAPPDATA%\Google\DriveFS\<account_id>\mirror_sqlite.db -> `mirror_item`
        (local_filename, is_root) + `root_config` + `machine_root`. Also all 0 rows
        today. Read opportunistically as a second signal.

    HONEST LIMIT ON THREAT 2 (do not oversell this): the registry tables are EMPTY
    right now, so the match logic (any registered path overlapping the protected tree
    -> page) has NOT been exercised against a real populated backup row on this
    machine. The column semantics are self-evident and match repo_backup's existing
    use of `roots`, but a future Drive-client schema change, or a backup recorded ONLY
    in a form whose path we don't recognise, could be missed by the DB read alone.
    That residual gap is exactly why this module does not rely on the DB alone: it
    ALSO runs THREAT 1 and a schema-independent artifact PROXY, and it FAILS CLOSED
    (see below) rather than silently passing when it cannot read the registry.

  PROXY (positive-only, schema-independent): a shallow scan of the protected tree for
    on-disk fingerprints of an ACTIVE Drive sync — NTFS reparse-point/placeholder
    attributes, and Drive's transfer temp dirs (.tmp.drivedownload / .tmp.driveupload).
    If any are found it is strong evidence Drive is syncing the tree, so we page. Its
    ABSENCE proves nothing (a backed-up-in-place file is a normal file, and transfer
    temp dirs are transient), so absence NEVER contributes to the green — it is a
    one-way tripwire only. Baseline measured clean today (0 reparse points, no temp
    dirs).

FAIL-OPEN vs FAIL-CLOSED — a deliberate decision, because a tripwire that goes silent
when it can't look is itself a silent failure (the whole disease):
  * A check that AFFIRMATIVELY finds a threat -> TRIPPED -> page (the loud remediation).
  * A check that CANNOT BE EVALUATED (DriveFS is installed but its registry DB can't
    be read/opened; the volume mount root can't be determined; this module's repo_backup
    import failed) -> UNEVALUABLE -> ALSO page, but with "could not evaluate" wording.
    We FAIL CLOSED. Silence is not treated as safety.
  * The ONE exception, and it is genuinely safe: if DriveFS is NOT INSTALLED at all
    (no DriveFS dir), there is no client that could manage anything, so the registry
    checks are trivially clean rather than unevaluable. THREAT 1 + the proxy still run.

  So the page fires when: (threat found) OR (a check that should be answerable wasn't).
  Green requires: no threat found AND every applicable check actually evaluated.

This module NEVER raises for policy reasons — evaluate() returns a verdict dict. It
is stdlib-only (verified against sys.stdlib_module_names) and imports repo_backup for
its DriveFS helpers exactly as heartbeat_alarm.py does. It reads secrets\ only as a
PATH to test (it never opens the files inside it).

Run:
    <venv python> drive_sync_tripwire.py            # evaluate + human report, exit 0=green
    <venv python> drive_sync_tripwire.py --json     # same, machine-readable last line
Exit codes: 0 = GREEN (no threat, all applicable checks evaluated). 1 = would page
(tripped or unevaluable). The scheduled paging path is heartbeat_alarm.py, which
imports this module and evaluates it every sweep; this CLI is for humans + tests.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# repo_backup owns the DriveFS volume-identity helpers (is_drive_managed,
# _volume_mount_root) and the DriveFS DB path. Import it rather than reinventing —
# same reuse heartbeat_alarm.py already relies on. Imported defensively: if it ever
# fails to import, THREAT 1 becomes UNEVALUABLE (fail closed), never silently skipped.
try:
    import repo_backup as _rb
except Exception:  # noqa: BLE001 — a failed import must not crash the alarm sweep
    _rb = None


# --------------------------------------------------------------------------- #
# Paths / tunables
# --------------------------------------------------------------------------- #
# The 99 GB folder we are guarding. Overridable for tests; the default is the one
# CLAUDE.md pins. We do NOT derive this from repo_backup.LOCAL_BACKUP_DIR: that
# constant is itself env-overridable and could be pointed elsewhere, and the thing we
# protect must not move just because the backup dir did.
PROTECTED_ROOT = Path(os.environ.get("TRADINGDESK_LOCAL", r"C:\TradingDesk-Local"))

# Major subfolders worth naming individually in an alert (each checked only if it
# exists). warehouse = the options data; secrets = keys; backups = the repo bundles;
# canslim / bt_data / state / venv = data + running state. Naming them makes a page
# actionable ("which part?") without depending on the tree's exact shape.
PROTECTED_SUBFOLDERS = ("warehouse", "canslim", "secrets", "backups",
                        "bt_data", "state", "venv")

# The DriveFS state dir, derived from repo_backup's own DB constant so the two never
# drift. Its .parent is %LOCALAPPDATA%\Google\DriveFS.
_DRIVE_DB = _rb.DRIVE_DB if _rb is not None else Path(
    r"C:\Users\andre\AppData\Local\Google\DriveFS\root_preference_sqlite.db")
DRIVEFS_DIR = _DRIVE_DB.parent

# Drive's on-disk fingerprints for the positive-only proxy scan.
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
DRIVE_TRANSFER_DIRS = (".tmp.drivedownload", ".tmp.driveupload")
PROXY_SCAN_DEPTH = 1               # top level + one level in — cheap, runs every sweep

# The remediation copy. Named plainly and verbatim per the module's contract — a paged
# human at 2am must read exactly what is wrong and exactly what to do.
REMEDIATION = (
    r"C:\TradingDesk-Local appears to be under Google Drive sync/backup management — "
    r"this is the wrong-folder corruption risk; disconnect it in Google Drive Desktop "
    r"immediately.")


# --------------------------------------------------------------------------- #
# Path-overlap — the core THREAT-2 predicate
# --------------------------------------------------------------------------- #
def _paths_overlap(a, b) -> bool:
    """True if paths a and b touch the same tree: equal, or one contains the other.

    A Drive backup root is a threat whether it sits AT the protected root, ABOVE it
    (an ancestor folder whose backup would sweep the whole tree in), or BELOW it (a
    single protected subfolder added on its own). So "overlap" is deliberately
    symmetric — ancestor OR descendant OR equal all count.

    Case-insensitive and separator-normalised (Windows). Different drives never
    overlap. Never raises.
    """
    try:
        na = os.path.normcase(os.path.normpath(str(a)))
        nb = os.path.normcase(os.path.normpath(str(b)))
    except Exception:  # noqa: BLE001
        return False
    if na == nb:
        return True
    try:
        common = os.path.commonpath([na, nb])
    except ValueError:
        # Raised when the paths are on different drives / mixed absolute-relative.
        return False
    return common == na or common == nb


# --------------------------------------------------------------------------- #
# THREAT 1 — is any protected path ON the DriveFS virtual volume?
# --------------------------------------------------------------------------- #
def check_threat1_volume(paths, *, managed_fn=None, mount_root_fn=None) -> dict:
    """Reuse repo_backup.is_drive_managed() over the protected paths.

    Classifies each path into one of three states, because "we couldn't tell" must
    NOT collapse into "it's fine":
      * ON THE DRIVE VOLUME  -> tripped
      * ON THE SYSTEM VOLUME -> clean
      * MOUNT ROOT UNKNOWN   -> unevaluable (fail closed)

    is_drive_managed gives the tripped verdict; we probe _volume_mount_root directly
    only to separate the UNKNOWN case from a genuine system-volume answer (both look
    like managed=False otherwise).
    """
    if _rb is None:
        return {"tripped": False, "unevaluable": True, "hits": [],
                "note": ("repo_backup could not be imported, so the DriveFS volume "
                         "check (THREAT 1) could not run — failing closed")}
    managed_fn = managed_fn or _rb.is_drive_managed
    mount_root_fn = mount_root_fn or _rb._volume_mount_root

    hits: list[str] = []
    unknown: list[str] = []
    for p in paths:
        root = mount_root_fn(str(p))
        if not root:
            unknown.append(str(p))
            continue
        managed, why = managed_fn(p)
        if managed:
            hits.append(f"{p} is ON THE DRIVE VOLUME — {why}")
    if hits:
        return {"tripped": True, "unevaluable": False, "hits": hits,
                "note": "one or more protected paths are on the DriveFS volume"}
    if unknown:
        return {"tripped": False, "unevaluable": True, "hits": [],
                "note": ("could not determine the volume mount root for: "
                         + ", ".join(unknown) + " — failing closed")}
    return {"tripped": False, "unevaluable": False, "hits": [],
            "note": "every protected path is on the local system volume (not Drive)"}


# --------------------------------------------------------------------------- #
# THREAT 2 — does DriveFS's registry list a local root overlapping the tree?
# --------------------------------------------------------------------------- #
def read_registered_roots(db_path=None, *, connect_fn=None) -> tuple[list[dict] | None, str]:
    """Read the `roots` table's registered LOCAL paths. -> (rows|None, note).

    None == COULD NOT READ (the DB is present but unreadable / the expected table is
    absent) -> the caller fails closed. [] == read OK, nothing registered (healthy).

    Opened READ-ONLY (mode=ro, NOT immutable — immutable can serve a stale cached
    read, which bit repo_backup on 2026-07-16). Each row yields root_path AND
    last_seen_absolute_path; either may be the live location, so both are returned.
    """
    path = Path(db_path or _DRIVE_DB)
    if not path.exists():
        return None, f"root_preference DB not present at {path}"
    try:
        con = (connect_fn(path) if connect_fn
               else sqlite3.connect(f"file:{path}?mode=ro", uri=True))
    except Exception as e:  # noqa: BLE001
        return None, f"could not open root_preference DB read-only ({e!r})"
    try:
        cur = con.cursor()
        tables = {n for (n,) in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "roots" not in tables:
            # DriveFS is installed but the registry table we rely on is absent. That is
            # not "clean" — it is "we cannot answer the question", so fail closed.
            return None, ("the `roots` table is absent from the root_preference DB — "
                          "cannot read the Drive backup/mirror registry")
        rows = list(cur.execute(
            "SELECT root_path, last_seen_absolute_path, sync_type, is_my_drive, state "
            "FROM roots"))
    except sqlite3.Error as e:
        return None, f"error reading the `roots` table ({e!r})"
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass

    out: list[dict] = []
    for root_path, last_seen, sync_type, is_my_drive, state in rows:
        out.append({"root_path": root_path, "last_seen_absolute_path": last_seen,
                    "sync_type": sync_type, "is_my_drive": is_my_drive, "state": state})
    return out, f"read {len(out)} row(s) from the `roots` registry"


def _account_dirs(drivefs_dir=None) -> list[Path]:
    """DriveFS per-account subdirs (numeric-named) that contain a mirror_sqlite.db."""
    d = Path(drivefs_dir or DRIVEFS_DIR)
    found: list[Path] = []
    try:
        for child in d.iterdir():
            if child.is_dir() and (child / "mirror_sqlite.db").exists():
                found.append(child)
    except OSError:
        pass
    return found


def read_mirror_local_paths(account_dirs=None, *, connect_fn=None) -> tuple[list[str], str]:
    """Best-effort: absolute-looking local paths recorded in each account's
    mirror_sqlite.db. -> (paths, note).

    POSITIVE-ONLY and deliberately lenient: mirror_item.local_filename is not always a
    full path (Drive can store a leaf name + parent chain we do not reconstruct here),
    so this NEVER fails closed on its own and NEVER contributes to the green — it only
    ever ADDS a path to overlap-test. The authoritative THREAT-2 source is the `roots`
    table above; this is corroboration. An unreadable mirror DB is silently skipped
    for that reason (the roots read already carries the fail-closed weight).
    """
    dirs = account_dirs if account_dirs is not None else _account_dirs()
    paths: list[str] = []
    notes: list[str] = []
    for d in dirs:
        db = Path(d) / "mirror_sqlite.db"
        try:
            con = (connect_fn(db) if connect_fn
                   else sqlite3.connect(f"file:{db}?mode=ro", uri=True))
        except Exception as e:  # noqa: BLE001
            notes.append(f"{db}: unreadable ({e!r})")
            continue
        try:
            cur = con.cursor()
            for (val,) in cur.execute(
                    "SELECT local_filename FROM mirror_item WHERE local_filename IS NOT NULL"):
                s = str(val)
                # Only absolute paths are usable for an overlap test.
                if os.path.isabs(s):
                    paths.append(s)
        except sqlite3.Error as e:
            notes.append(f"{db}: query error ({e!r})")
        finally:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
    return paths, ("; ".join(notes) if notes else f"scanned {len(dirs)} mirror DB(s)")


def check_threat2_registry(protected_paths, *, drivefs_present_fn=None, roots_fn=None,
                           mirror_fn=None) -> dict:
    """Does DriveFS's registry name a local root overlapping the protected tree?

    Fail-closed on an unreadable `roots` registry WHEN DriveFS is installed; trivially
    clean when DriveFS is not installed at all.
    """
    present = (drivefs_present_fn() if drivefs_present_fn
               else Path(DRIVEFS_DIR).is_dir())
    if not present:
        return {"tripped": False, "unevaluable": False, "hits": [],
                "note": "Google Drive Desktop (DriveFS) is not installed — no client "
                        "could be managing the tree"}

    roots_fn = roots_fn or read_registered_roots
    mirror_fn = mirror_fn or read_mirror_local_paths

    rows, rnote = roots_fn()
    if rows is None:
        # DriveFS present but its registry cannot be read -> fail closed.
        return {"tripped": False, "unevaluable": True, "hits": [],
                "note": f"could not read the Drive backup/mirror registry — {rnote}"}

    hits: list[str] = []
    for row in rows:
        for key in ("root_path", "last_seen_absolute_path"):
            val = row.get(key)
            if not val:
                continue
            for prot in protected_paths:
                if _paths_overlap(val, prot):
                    hits.append(f"Drive `roots` registry entry {val!r} (sync_type="
                                f"{row.get('sync_type')}, is_my_drive={row.get('is_my_drive')}) "
                                f"overlaps protected path {prot}")
    # Opportunistic mirror-DB corroboration (never fails closed).
    mpaths, _mnote = mirror_fn()
    for val in mpaths:
        for prot in protected_paths:
            if _paths_overlap(val, prot):
                hits.append(f"Drive mirror_item local_filename {val!r} overlaps "
                            f"protected path {prot}")

    if hits:
        return {"tripped": True, "unevaluable": False, "hits": hits,
                "note": "the Drive registry names a local root overlapping the tree"}
    return {"tripped": False, "unevaluable": False, "hits": [],
            "note": f"{rnote}; none overlap the protected tree"}


# --------------------------------------------------------------------------- #
# PROXY — positive-only on-disk fingerprints of an active Drive sync
# --------------------------------------------------------------------------- #
def _has_reparse_attr(path) -> bool:
    """True if `path` carries the NTFS reparse-point/placeholder attribute.

    DriveFS streaming content is materialised as reparse-point placeholders; a
    junction/symlink INTO a Drive mount also shows this. Never raises; off-Windows or
    on any error returns False (the proxy is positive-only, so a False is harmless)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        a = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return a != 0xFFFFFFFF and bool(a & FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:  # noqa: BLE001
        return False


def check_proxy_artifacts(root=None, *, depth: int = PROXY_SCAN_DEPTH,
                          scandir_fn=None, reparse_fn=None) -> dict:
    """Shallow scan for Drive-sync fingerprints. POSITIVE-ONLY: a hit trips; a clean
    scan proves nothing and never contributes to the green (never unevaluable)."""
    root = Path(root or PROTECTED_ROOT)
    scandir_fn = scandir_fn or (lambda d: list(os.scandir(d)))
    reparse_fn = reparse_fn or _has_reparse_attr
    hits: list[str] = []

    def _walk(d, lvl):
        try:
            entries = scandir_fn(d)
        except OSError:
            return
        for e in entries:
            name = getattr(e, "name", "")
            path = getattr(e, "path", str(Path(d) / name))
            if name in DRIVE_TRANSFER_DIRS:
                hits.append(f"Drive transfer temp dir present: {path}")
            if reparse_fn(path):
                hits.append(f"reparse-point/placeholder attribute on: {path}")
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except Exception:  # noqa: BLE001
                is_dir = False
            if is_dir and lvl > 0:
                _walk(path, lvl - 1)

    _walk(root, depth)
    if hits:
        return {"tripped": True, "unevaluable": False, "hits": hits,
                "note": "found on-disk fingerprints of an active Drive sync"}
    return {"tripped": False, "unevaluable": False, "hits": [],
            "note": "no Drive-sync artifacts in the shallow scan (proves nothing on "
                    "its own — positive-only signal)"}


# --------------------------------------------------------------------------- #
# The verdict — combine the checks
# --------------------------------------------------------------------------- #
def _protected_paths(root=None) -> list[Path]:
    """The protected root plus any of its major subfolders that actually exist."""
    root = Path(root or PROTECTED_ROOT)
    paths = [root]
    for sub in PROTECTED_SUBFOLDERS:
        p = root / sub
        try:
            if p.exists():
                paths.append(p)
        except OSError:
            pass
    return paths


def evaluate(*, root=None, threat1_fn=None, threat2_fn=None, proxy_fn=None) -> dict:
    """Run all checks and combine into one verdict. NEVER raises for policy reasons.

    Returns:
      ok          : bool   green — nothing tripped AND nothing unevaluable
      tripped     : bool   a threat is affirmatively TRUE (page: remediation)
      unevaluable : bool   a check that should have answered did not (page: fail-closed)
      should_page : bool   tripped OR unevaluable
      reasons     : [str]  human hit/failure lines for the alert body
      remediation : str    the verbatim remediation copy
      checks      : dict   per-check detail (for the status/log)
      protected   : [str]  the paths evaluated
    """
    root = Path(root or PROTECTED_ROOT)
    protected = _protected_paths(root)
    pstr = [str(p) for p in protected]

    t1 = (threat1_fn or check_threat1_volume)(protected)
    t2 = (threat2_fn or check_threat2_registry)(pstr)
    px = (proxy_fn or check_proxy_artifacts)(root)

    checks = {"threat1_volume": t1, "threat2_registry": t2, "proxy_artifacts": px}
    tripped = any(c.get("tripped") for c in checks.values())
    unevaluable = any(c.get("unevaluable") for c in checks.values())

    reasons: list[str] = []
    for cname, c in checks.items():
        for h in c.get("hits", []):
            reasons.append(f"[{cname}] {h}")
        if c.get("unevaluable"):
            reasons.append(f"[{cname}] COULD NOT EVALUATE — {c.get('note')}")

    return {
        "ok": not (tripped or unevaluable),
        "tripped": tripped,
        "unevaluable": unevaluable,
        "should_page": tripped or unevaluable,
        "reasons": reasons,
        "remediation": REMEDIATION,
        "checks": checks,
        "protected": pstr,
    }


# --------------------------------------------------------------------------- #
# CLI — for humans and tests (the scheduled paging path is heartbeat_alarm.py)
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Tripwire: page if C:\\TradingDesk-Local comes under Google Drive "
                    "sync/backup management.")
    ap.add_argument("--json", action="store_true",
                    help="print the verdict dict as a machine-readable last line.")
    args = ap.parse_args()

    v = evaluate()
    print(f"protected root : {PROTECTED_ROOT}")
    print(f"DriveFS dir     : {DRIVEFS_DIR}  (installed={Path(DRIVEFS_DIR).is_dir()})")
    for cname, c in v["checks"].items():
        flag = ("TRIPPED" if c.get("tripped") else
                "UNEVALUABLE" if c.get("unevaluable") else "clean")
        print(f"  {cname:18s}: {flag} — {c.get('note')}")
        for h in c.get("hits", []):
            print(f"       hit: {h}")
    if v["ok"]:
        print("\nRESULT: GREEN — TradingDesk-Local is NOT under Drive management.")
    elif v["tripped"]:
        print(f"\nRESULT: TRIPPED — WOULD PAGE.\n  {v['remediation']}")
    else:
        print("\nRESULT: UNEVALUABLE — WOULD PAGE (fail-closed: a check could not run).")
    for r in v["reasons"]:
        print(f"  reason: {r}")

    if args.json:
        print(json.dumps(v, separators=(",", ":"), default=str))
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
