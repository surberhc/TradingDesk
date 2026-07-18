"""s8_reap.py — S8 live-pilot PRE-LAUNCH / TEARDOWN ORPHAN REAPER.

WHY THIS EXISTS (both halves proven in the go-live dry runs)
------------------------------------------------------------
``Stop-ScheduledTask`` kills the ``.cmd`` WRAPPER, not its ``python.exe`` child. The
surviving orphan keeps THREE things it should not:

  1. a read-only connection to the live-trading Gateway, holding its registered clientId
     (55 for ``s8_service``, 56 for ``s8_collector``) — the next start collides;
  2. its single-instance lock file, which then looks "held";
  3. **the day's log file handle**, which is what makes this a separate problem from the
     in-python guard in ``s8_lock``. The wrapper opens the day log BEFORE python starts,
     so a locked day log forces the wrapper into its timestamped FALLBACK file and a
     single session's logs end up split across two files.

``s8_lock.SingleInstanceLock`` already takes over an orphan — but only once python is
already running, which is far too late for (3). This module is the same policy, hoisted
to run BEFORE the wrapper opens any log, and reusable as a standalone teardown step.

TWO CALL SITES
--------------
  * ``run_s8_service.cmd`` / ``run_s8_collector.cmd`` invoke it at the TOP, before the
    day log is probed — so the handle is already released when logging starts. A failed
    reap NEVER prevents the launch (the mandatory-launch guarantee from ad9113e).
  * The ``S8SessionTeardown`` scheduled task (15:05 CT weekdays) runs it for BOTH
    processes after the close, covering the "stopped and never restarted" case where
    nothing else would ever reap the orphan.

SAFETY — THE LOAD-BEARING CHECK (identical to s8_lock's)
---------------------------------------------------------
No PID is ever killed unless its COMMAND LINE positively contains this target's script
marker (``s8_service.py`` / ``s8_collector.py``), verified through
``s8_lock.cmdline_matches``. A missing, empty or unreadable command line is NOT a match,
so an unidentifiable process can never be killed — PID reuse, a lock pointing at a
stranger, or an unreadable cmdline all REFUSE. Doing nothing is always safer than killing
a process we have not positively identified.

TWO SOURCES, ONE VERIFICATION
-----------------------------
  * the LOCK RECORD's holder pid (the normal case — the orphan still holds its lock), and
  * a best-effort SCAN for live processes whose command line contains the marker (catches
    an orphan whose lock was already unlinked).
Both funnel through the same cmdline verification, so the scan cannot widen the blast
radius. Our own pid is always excluded.

NEVER RAISES, NEVER BLOCKS. Every failure is caught and reported in the returned dict;
``main`` always exits 0 unless explicitly asked otherwise, because a reap failure must not
stop a launch.

Its own output goes to STDOUT and to its OWN small log (``logs/s8_reap.log``) — never the
day log, whose handle is the very thing being released.

Zero-transmit: this module knows nothing about orders, IB, or strategy.

PURE SEAM: the liveness check, the cmdline lookup, the process scan, the kill callable and
the clock are all injected, so every branch is offline-testable with no real processes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import s8_lock  # noqa: E402  (reuse its cmdline verification + probes — do not duplicate)

# The two reapable pilot processes: (lock name, script marker). The marker is the ONLY
# thing that authorises a kill.
TARGETS: Tuple[Tuple[str, str], ...] = (
    ("s8_service", "s8_service.py"),
    ("s8_collector", "s8_collector.py"),
)

REAP_LOG_NAME = "s8_reap.log"


# --------------------------------------------------------------------------- #
# Default (real) probes — all injected in tests
# --------------------------------------------------------------------------- #

def find_pids_by_marker(
    marker: str,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> Optional[List[Tuple[int, str]]]:
    """Live ``(pid, cmdline)`` pairs whose command line contains ``marker``; None if the
    scan could not be performed at all.

    None means "could not determine" and is NEVER treated as "nothing is running" in a way
    that licenses a kill — it simply means this secondary source contributes nothing. On
    Windows this uses CIM (``Win32_Process``) because ``wmic`` is deprecated/absent on
    Windows 11; elsewhere the scan is unavailable (returns None) and the lock record is the
    only source.
    """
    if os.name != "nt":
        return None
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$all=@(Get-CimInstance Win32_Process);"
        "if($all.Count -eq 0){Write-Output 'PROBE_FAILED'}else{"
        "@($all | Where-Object { $_.CommandLine } |"
        " Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress}"
    )
    try:
        out = run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001 — undeterminable, never a licence to kill
        return None
    text = (getattr(out, "stdout", "") or "").strip()
    if not text or "PROBE_FAILED" in text:
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return None
    found: List[Tuple[int, str]] = []
    for row in parsed:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        cmdline = row.get("CommandLine")
        if pid > 0 and s8_lock.cmdline_matches(cmdline, marker):
            found.append((pid, str(cmdline)))
    return found


def default_reap_log_path():
    """Off-Drive path for the reaper's OWN log — deliberately NOT the day log, whose
    handle is the thing being released. Honours ``$S8_PILOT_ROOT`` (tests)."""
    import s8_store  # noqa: PLC0415

    return s8_store.get_root() / "logs" / REAP_LOG_NAME


# --------------------------------------------------------------------------- #
# THE REAP — one target
# --------------------------------------------------------------------------- #

def reap_one(
    name: str,
    marker: str,
    *,
    lock_path=None,
    my_pid: Optional[int] = None,
    is_alive: Callable[[int], bool] = s8_lock.pid_alive,
    get_cmdline: Callable[[int], Optional[str]] = s8_lock.cmdline_of,
    find_pids: Optional[Callable[[str], Optional[List[Tuple[int, str]]]]] = None,
    kill: Callable[[int], bool] = s8_lock.kill_pid,
    log: Callable[[str], Any] = print,
) -> Dict[str, Any]:
    """Terminate a stale, POSITIVELY IDENTIFIED ``marker`` process and clear its lock.

    Returns a result dict::

        {"name", "killed": [pid...], "refused": [pid...], "lock_cleared": bool,
         "scanned": bool, "error": str|None}

    Never raises. A no-op (nothing running, no lock) is the normal, quiet case.
    """
    result: Dict[str, Any] = {
        "name": str(name), "killed": [], "refused": [], "lock_cleared": False,
        "scanned": False, "error": None,
    }
    try:
        me = int(my_pid if my_pid is not None else os.getpid())
        path = str(lock_path if lock_path is not None else s8_lock.default_lock_path(name))

        # --- candidate pids: the lock's holder, then the (best-effort) marker scan ---
        candidates: List[int] = []
        rec = s8_lock.read_record(path)
        holder = 0
        if isinstance(rec, dict):
            try:
                holder = int(rec.get("pid") or 0)
            except (TypeError, ValueError):
                holder = 0
        if holder > 0 and holder != me:
            candidates.append(holder)

        scan = find_pids if find_pids is not None else find_pids_by_marker
        try:
            hits = scan(marker)
        except Exception:  # noqa: BLE001 — the scan is strictly best-effort
            hits = None
        if hits is not None:
            result["scanned"] = True
            for pid, _cl in hits:
                if pid != me and pid not in candidates:
                    candidates.append(pid)

        # --- kill only what we can POSITIVELY identify as this target ---
        for pid in candidates:
            try:
                if not is_alive(pid):
                    log(f"s8_reap[{name}]: pid={pid} is not running (nothing to reap)")
                    continue
                cmdline = get_cmdline(pid)
                if not s8_lock.cmdline_matches(cmdline, marker):
                    result["refused"].append(pid)
                    log(f"s8_reap[{name}]: REFUSING to kill live pid={pid} — its command "
                        f"line does not match {marker!r} (cmdline={cmdline!r}). An "
                        f"unidentified process is never ours.")
                    continue
                ok = bool(kill(pid))
                log(f"s8_reap[{name}]: reaped stale orphan pid={pid} -> "
                    f"{'ok' if ok else 'FAILED'}")
                if ok:
                    result["killed"].append(pid)
                else:
                    result["refused"].append(pid)
            except Exception as exc:  # noqa: BLE001 — one bad pid must not abort the rest
                result["error"] = f"{type(exc).__name__}: {exc}"
                log(f"s8_reap[{name}]: error handling pid={pid} ({result['error']})")

        # --- clear the stale lock, but ONLY if nobody live still legitimately holds it ---
        if os.path.exists(path):
            if holder > 0 and holder in result["refused"]:
                log(f"s8_reap[{name}]: leaving the lock in place — it is held by live "
                    f"pid={holder}, which we refused to kill.")
            else:
                try:
                    os.unlink(path)
                    result["lock_cleared"] = True
                    log(f"s8_reap[{name}]: cleared stale lock {os.path.basename(path)}")
                except OSError as exc:
                    log(f"s8_reap[{name}]: could not clear lock ({exc!r})")
        return result
    except Exception as exc:  # noqa: BLE001 — the reaper must NEVER raise into a wrapper
        result["error"] = f"{type(exc).__name__}: {exc}"
        try:
            log(f"s8_reap[{name}]: reap failed entirely ({result['error']}); "
                f"the launch proceeds regardless.")
        except Exception:  # noqa: BLE001
            pass
        return result


def reap(
    names: Optional[List[str]] = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Reap one or more targets by lock name (default: ALL of ``TARGETS``). Never raises."""
    wanted = list(names) if names else [n for n, _m in TARGETS]
    markers = dict(TARGETS)
    out: List[Dict[str, Any]] = []
    for n in wanted:
        marker = markers.get(n)
        if marker is None:
            out.append({"name": n, "killed": [], "refused": [], "lock_cleared": False,
                        "scanned": False, "error": f"unknown target {n!r}"})
            continue
        out.append(reap_one(n, marker, **kwargs))
    return out


# --------------------------------------------------------------------------- #
# CLI — invoked from the wrappers (before the day log) and by S8SessionTeardown
# --------------------------------------------------------------------------- #

def _file_logger(path) -> Callable[[str], None]:
    """Log to stdout AND to the reaper's own small file. Never raises, and never touches
    the day log — releasing that handle is the whole point."""

    def _log(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        try:
            os.makedirs(os.path.dirname(str(path)), exist_ok=True)
            with open(str(path), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    return _log


def main(argv: Optional[List[str]] = None) -> int:
    """Reap the named targets (or all of them). ALWAYS returns 0.

    A nonzero exit here would be read by the wrapper as a reason to stop, and the launch
    must never depend on the reap succeeding.
    """
    args = [a for a in (argv if argv is not None else sys.argv[1:]) if a not in ("--all",)]
    names = args or [n for n, _m in TARGETS]
    try:
        log = _file_logger(default_reap_log_path())
    except Exception:  # noqa: BLE001
        log = print
    try:
        results = reap(names, log=log)
        for r in results:
            log(f"s8_reap[{r['name']}]: done killed={r['killed']} refused={r['refused']} "
                f"lock_cleared={r['lock_cleared']} error={r['error']}")
    except Exception as exc:  # noqa: BLE001
        try:
            log(f"s8_reap: unexpected error ({type(exc).__name__}: {exc}); "
                f"exiting 0 so the launch proceeds.")
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
