"""s8_lock.py — S8 live-pilot SINGLE-INSTANCE / ORPHAN GUARD (shared, pure, testable).

WHY THIS EXISTS (proven by the go-live dry run)
-----------------------------------------------
The S8 pilot processes are launched by Windows Task Scheduler through a ``.cmd`` wrapper.
``Stop-ScheduledTask`` (and the task's own time-limit expiry) kills the WRAPPER — the child
``python.exe`` SURVIVES. That orphan is still connected to the live-trading Gateway holding
its registered clientId (55 for ``s8_service``, 56 for ``s8_collector``), so the NEXT
morning's scheduled start collides: "clientId already in use".

THE GUARD
---------
Each process takes its OWN single-instance lock (``s8_service.lock`` /
``s8_collector.lock``) under the off-Drive state dir. On startup:

  * no lock                      -> acquire and run;
  * lock whose holder PID is DEAD -> stale from a crash: reclaim and run;
  * lock whose holder PID is ALIVE -> a stale ORPHAN from a previous session. These
    processes are singletons and crash-safe/idempotent (they reload all state from the
    durable store), so the correct move is to TAKE OVER: terminate the orphan, log it
    clearly, then acquire and run.

SAFETY (the load-bearing check)
-------------------------------
We NEVER kill a PID we have not positively identified as our own script. Before any kill,
the holder's COMMAND LINE is looked up and must contain this process's script marker
(``s8_service.py`` / ``s8_collector.py``). If the cmdline does not match — PID reuse, a
lock file pointing at some unrelated process, or an unreadable cmdline — we REFUSE to kill
and refuse to start, logging why. Failing to start is always safer than killing a stranger.

CONVENTION
----------
Follows ``connections.ibkr_live_trade.ensure_gateway``'s lockfile pattern verbatim in
spirit: atomic ``os.open(O_CREAT|O_EXCL|O_WRONLY)`` create (only one of N racing starts can
win), a JSON record holding ``pid`` / ``started_at`` / ``host``, ``tasklist``-based PID
liveness (dependency-free on Windows), stale reclaim by unlink-and-retry, and release only
if the recorded pid is still ours. Same as ``datacollector/spxw_1m_supervisor.acquire_lock``
— with the deliberate difference that a LIVE holder is taken over rather than deferred to,
because an S8 orphan holds a clientId the new instance needs.

Zero-transmit is untouched: this module knows nothing about orders, IB, or strategy.

PURE SEAM
---------
``decide_lock_action`` is a pure function of (record, my_pid) plus the injected
pid-liveness and cmdline lookups — no filesystem, no processes. ``acquire`` takes the
liveness check, the cmdline lookup and the kill callable as injected parameters, so every
branch (acquire / reclaim / take over / refuse) is offline-testable with fakes.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from typing import Any, Callable, Dict, Optional, Tuple

# Decision outcomes returned by the pure seam.
ACQUIRE = "acquire"            # no lock (or it is ours) -> just take it
RECLAIM = "reclaim"            # holder PID is dead/garbage -> stale lock, take it over
TAKE_OVER = "take_over"        # holder PID is ALIVE and IS our script -> kill it, take over
REFUSE = "refuse"              # holder PID is ALIVE but is NOT our script -> do not kill

_ACQUIRE_ATTEMPTS = 5          # bounded reclaim/take-over retries; never spins forever


# --------------------------------------------------------------------------- #
# Default (real) probes — injected in tests
# --------------------------------------------------------------------------- #

def pid_alive(pid: int) -> bool:
    """True if a process with this PID is currently running (Windows + POSIX).

    Mirrors ``ibkr_live_trade._pid_alive`` / ``spxw_1m_supervisor._pid_alive``: ``tasklist``
    is the dependency-free liveness test on Windows. An UNDETERMINABLE result is treated as
    ALIVE — conservative here too, because "alive" routes into the cmdline-verified path
    (which refuses to kill anything it cannot positively identify) rather than blindly
    reclaiming a lock that may still be held.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=15,
            )
            return str(pid) in out.stdout
        except Exception:  # noqa: BLE001 — can't tell -> assume alive (conservative)
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cmdline_of(pid: int) -> Optional[str]:
    """The full command line of ``pid``, or None if it cannot be determined.

    None is a REFUSAL signal, never a licence to kill: an unidentifiable process is treated
    as "not ours". On Windows this uses CIM (``Win32_Process``) because ``wmic`` is
    deprecated/absent on Windows 11; on POSIX it reads ``/proc/<pid>/cmdline``.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=30,
            )
            line = (out.stdout or "").strip()
            return line or None
        except Exception:  # noqa: BLE001
            return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip() or None
    except Exception:  # noqa: BLE001
        return None


def kill_pid(pid: int) -> bool:
    """Terminate ``pid``. Returns True if the kill command reported success.

    Only ever called after ``decide_lock_action`` has POSITIVELY matched the holder's
    command line against our own script marker.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                 capture_output=True, text=True, timeout=30)
            return out.returncode == 0
        except Exception:  # noqa: BLE001
            return False
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# PURE SEAM — the whole decision, no filesystem and no processes
# --------------------------------------------------------------------------- #

def cmdline_matches(cmdline: Optional[str], marker: str) -> bool:
    """True iff ``cmdline`` positively identifies our own script.

    Conservative by construction: a missing/empty cmdline is NOT a match, so an
    unidentifiable process can never be killed.
    """
    if not cmdline or not marker:
        return False
    return marker.lower() in str(cmdline).lower()


def decide_lock_action(
    record: Optional[Dict[str, Any]],
    marker: str,
    *,
    my_pid: int,
    is_alive: Callable[[int], bool],
    get_cmdline: Callable[[int], Optional[str]],
) -> Tuple[str, str]:
    """Decide what to do about an existing lock ``record``. PURE (probes injected).

    Returns ``(action, reason)`` where action is ACQUIRE / RECLAIM / TAKE_OVER / REFUSE and
    reason is the human line to log. See the module docstring for the policy.
    """
    if not isinstance(record, dict):
        return RECLAIM, "lock file is missing or unreadable/garbage; reclaiming"
    try:
        holder = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        holder = 0
    if holder <= 0:
        return RECLAIM, "lock file has no usable pid; reclaiming"
    if holder == int(my_pid):
        return ACQUIRE, f"lock is already held by this process (pid={holder})"
    if not is_alive(holder):
        return RECLAIM, f"stale lock found (holder pid={holder} is not running); reclaiming"
    cmdline = get_cmdline(holder)
    if cmdline_matches(cmdline, marker):
        return TAKE_OVER, (f"found live prior instance pid={holder} holding the lock; "
                           f"terminating stale orphan and taking over")
    return REFUSE, (f"lock held by LIVE pid={holder} whose command line does NOT match "
                    f"{marker!r} (cmdline={cmdline!r}) — refusing to kill an unrelated "
                    f"process, and refusing to start")


# --------------------------------------------------------------------------- #
# Lock file I/O (atomic create, JSON record) — ibkr_live_trade convention
# --------------------------------------------------------------------------- #

def _write_record(fd_or_path, my_pid: int) -> None:
    rec = {"pid": int(my_pid), "started_at": time.time(), "host": socket.gethostname()}
    data = json.dumps(rec).encode("utf-8")
    if isinstance(fd_or_path, int):
        os.write(fd_or_path, data)
    else:
        # Atomic replace via a temp file in the same dir (never a partial record on disk).
        tmp = f"{fd_or_path}.tmp{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, fd_or_path)


def read_record(path) -> Optional[Dict[str, Any]]:
    """The holder record dict, or None if missing/unreadable/garbage."""
    try:
        with open(str(path), "r", encoding="utf-8") as f:
            rec = json.loads(f.read() or "{}")
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


def default_lock_path(name: str):
    """Off-Drive lock path for ``name`` (e.g. "s8_service") under the store's state dir.

    Derived from ``s8_store.get_root()`` so it honours ``$S8_PILOT_ROOT`` (tests) and is
    NEVER a My Drive path.
    """
    import s8_store
    return s8_store.get_root() / "state" / f"{name}.lock"


class SingleInstanceLock:
    """A per-process single-instance lock with orphan take-over.

    Usage (both live entrypoints)::

        lock = SingleInstanceLock("s8_service", "s8_service.py")
        if not lock.acquire():
            raise SystemExit(4)
        try:
            ...run...
        finally:
            lock.release()
    """

    def __init__(
        self,
        name: str,
        marker: str,
        *,
        path=None,
        my_pid: Optional[int] = None,
        is_alive: Callable[[int], bool] = pid_alive,
        get_cmdline: Callable[[int], Optional[str]] = cmdline_of,
        kill: Callable[[int], bool] = kill_pid,
        log: Callable[[str], Any] = print,
        settle_secs: float = 2.0,
        sleep: Callable[[float], Any] = time.sleep,
    ) -> None:
        self.name = name
        self.marker = marker
        self._path_override = path
        self.my_pid = int(my_pid if my_pid is not None else os.getpid())
        self._is_alive = is_alive
        self._get_cmdline = get_cmdline
        self._kill = kill
        self._log = log
        self._settle_secs = float(settle_secs)
        self._sleep = sleep
        self.held = False

    @property
    def path(self):
        return self._path_override if self._path_override is not None \
            else default_lock_path(self.name)

    def acquire(self) -> bool:
        """Take the lock, reclaiming a stale one and terminating a verified orphan.

        Returns True if we now hold it, False if we must not start (a LIVE holder whose
        cmdline we could not positively identify as our own script, or an unusable lock
        directory). Never raises.
        """
        path = str(self.path)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError as exc:
            self._log(f"{self.name}: cannot prepare lock dir ({exc!r}); refusing to start")
            return False

        for _ in range(_ACQUIRE_ATTEMPTS):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                action, reason = decide_lock_action(
                    read_record(path), self.marker, my_pid=self.my_pid,
                    is_alive=self._is_alive, get_cmdline=self._get_cmdline,
                )
                self._log(f"{self.name}: {reason}")
                if action == REFUSE:
                    return False
                if action == ACQUIRE:
                    self.held = True
                    return True
                if action == TAKE_OVER:
                    rec = read_record(path) or {}
                    holder = int(rec.get("pid") or 0)
                    killed = self._kill(holder)
                    self._log(f"{self.name}: terminate pid={holder} -> "
                              f"{'ok' if killed else 'FAILED'}")
                    if not killed:
                        return False
                    if self._settle_secs:
                        self._sleep(self._settle_secs)  # let the gateway drop its clientId
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            except OSError as exc:
                self._log(f"{self.name}: lock error ({exc!r}); refusing to start")
                return False
            else:
                try:
                    _write_record(fd, self.my_pid)
                finally:
                    os.close(fd)
                self.held = True
                self._log(f"{self.name}: single-instance lock acquired "
                          f"(pid={self.my_pid}, lock={os.path.basename(path)})")
                return True

        self._log(f"{self.name}: could not acquire the single-instance lock after "
                  f"{_ACQUIRE_ATTEMPTS} attempts; refusing to start")
        return False

    def release(self) -> None:
        """Release the lock iff the recorded pid is still ours. Never raises."""
        if not self.held:
            return
        path = str(self.path)
        try:
            rec = read_record(path)
            if rec is not None and int(rec.get("pid") or 0) == self.my_pid:
                os.unlink(path)
        except OSError:
            pass
        finally:
            self.held = False
