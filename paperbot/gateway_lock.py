"""
gateway_lock.py — single-process mutex on the paper Gateway (Slice 1, INERT).

WHY THIS EXISTS
---------------
clientIds (connections/clientids.py) stop two of our sessions from grabbing the
SAME IBKR API client slot. They do NOTHING to stop two of OUR processes from
operating the one paper Gateway (127.0.0.1:4002) concurrently on DIFFERENT
clientIds — e.g. the read-only account monitor snapshotting positions/cash in the
middle of a laddered rebalance that is moving them. This module is an INTER-PROCESS
mutex on the Gateway as a shared resource, orthogonal to identity: at most one of
our processes operates the Gateway at a time. See docs/GATEWAY_LOCK_SPEC.md.

REUSE, DO NOT REINVENT
----------------------
The hard part — atomic acquire + dead-holder reclaim — is the proven pattern already
shipping in `datacollector/spxw_1m_supervisor.py` (acquire_lock / release_lock /
_pid_alive). We lift it verbatim in spirit and CITE it:
  * Atomic acquire ............ os.open(LOCK, O_CREAT|O_EXCL|O_WRONLY)   (supervisor ~L137)
  * Liveness check ............ tasklist /FI "PID eq <pid>"             (supervisor ~L95-117)
  * Stale reclaim ............. holder PID dead -> unlink + retry create (supervisor ~L152-159)
  * Release-iff-owner ......... only delete if recorded pid is ours      (supervisor ~L163-169)
We extend it in two blessed ways (per the spec's owner-ruled decisions):
  1. A RICHER JSON payload (pid/client_id/purpose/timestamps) so a blocked caller can
     name the holder in its refusal/skip — not just a bare PID.
  2. A HEARTBEAT LEASE: the holder refreshes heartbeat_ts every ~30s in a daemon thread,
     and a blocked caller treats an ALIVE-but-silent holder (no heartbeat for ~300s) as
     wedged and reclaims it. This lets a legitimately long laddered rebalance hold the
     Gateway for many minutes without being mistaken for hung, while a genuinely stuck
     holder is still recovered.

WHERE THE FILE LIVES — LOCAL ONLY
---------------------------------
`gateway.lock` lives in config.STATE_DIR = C:\\TradingDesk-Local\\state\\paperbot\\,
NEVER on Google Drive. Drive's background sync renames/rewrites files non-atomically and
replicates them across machines, which would break O_CREAT|O_EXCL atomicity and the
liveness check — exactly the properties the lock depends on.

STATUS: Slice 1 is INERT. Nothing imports this yet; wrapping the monitor (Slice 2) and the
rebalance (Slice 3) connect paths is deferred. Importing this module changes no runtime
behavior and transmits nothing.

PAPER only, as always. This module only guards the mutex — it never connects, never
transmits, never reads strategy config; the review -> arm -> transmit gate is untouched.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from datetime import datetime

import config

# --- Where the lock lives (LOCAL state dir, never Drive) -----------------------
LOCK_PATH = os.path.join(config.STATE_DIR, "gateway.lock")

# --- OWNER-RULED PARAMETERS (one knob each; the only judgment calls per the spec) --
# Decision 1 — monitor: wait briefly then SKIP this cycle.
MONITOR_WAIT_SECS = 10.0
# Decision 3 — rebalance: wait a short bounded time then REFUSE, naming the holder.
REBALANCE_WAIT_SECS = 30.0
# Decision 2 — heartbeat horizon: an ALIVE holder silent this long is presumed wedged.
STALE_HEARTBEAT_SECS = 300.0     # 5 minutes of no heartbeat -> reclaimable
# How often the holder refreshes heartbeat_ts while it holds (well under the horizon).
HEARTBEAT_INTERVAL_SECS = 30.0
# How often a blocked caller re-tries the atomic create while waiting.
POLL_INTERVAL_SECS = 0.5

# Policy names callers pass as on_busy=...
POLICY_SKIP = "skip"        # monitor-style: brief wait, then raise GatewayBusySkip
POLICY_REFUSE = "refuse"    # rebalance-style: longer wait, then raise GatewayBusyRefuse

# Default wait per policy when wait_secs is not given explicitly.
_DEFAULT_WAIT = {POLICY_SKIP: MONITOR_WAIT_SECS, POLICY_REFUSE: REBALANCE_WAIT_SECS}


# --- Exceptions ----------------------------------------------------------------
class GatewayBusy(Exception):
    """Base: the Gateway is held by a live, non-stale holder. Carries the holder record.

    `holder` is the parsed lock JSON (pid/client_id/purpose/acquired_ts/heartbeat_ts/host)
    so callers can name who holds it. May be None if the record was unreadable.
    """

    def __init__(self, message: str, holder: dict | None = None):
        super().__init__(message)
        self.holder = holder or {}


class GatewayBusySkip(GatewayBusy):
    """Monitor-style busy outcome: caller should SKIP this cycle (a non-event)."""


class GatewayBusyRefuse(GatewayBusy):
    """Rebalance-style busy outcome: caller should REFUSE to start; message names holder."""


# --- Liveness (lifted from spxw_1m_supervisor._pid_alive) ----------------------
def _pid_alive(pid: int) -> bool:
    """True if a process with this PID is running (Windows + POSIX).

    Verbatim policy from spxw_1m_supervisor.py (~L95-117): on Windows use `tasklist`
    (dependency-free); if we cannot tell, ASSUME ALIVE — safer to refuse than to risk
    two processes operating the Gateway. For a transmit path we never reclaim on ambiguity.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=15,
            )
            return str(pid) in out.stdout
        except Exception:
            return True  # can't tell -> assume alive (conservative)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- Lock-record helpers -------------------------------------------------------
def _read_record(path: str) -> dict | None:
    """Parse the lock file's JSON record; None if missing/garbage."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            txt = fh.read().strip()
        if not txt:
            return None
        return json.loads(txt)
    except (OSError, ValueError):
        return None


def _heartbeat_age(record: dict, now: float) -> float | None:
    """Seconds since the record's heartbeat_ts (epoch float). None if unreadable."""
    hb = record.get("heartbeat_ts")
    try:
        return now - float(hb)
    except (TypeError, ValueError):
        return None


def _is_stale(record: dict | None, now: float, pid_alive=_pid_alive) -> bool:
    """A held lock is STALE (reclaimable) if either:
      (a) the recorded PID is not alive (crash/kill/Ctrl-C left the file), OR
      (b) the PID IS alive but its heartbeat has been silent >= STALE_HEARTBEAT_SECS
          (the process is wedged, not working).
    Unreadable record -> treat as stale garbage (reclaim it). Unreadable heartbeat on an
    alive PID -> NOT stale (conservative: don't reclaim a live holder on ambiguity).
    """
    if record is None:
        return True  # garbage/empty file from a torn write -> reclaim
    pid = record.get("pid")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True  # malformed pid -> garbage -> reclaim
    if not pid_alive(pid):
        return True  # (a) dead holder
    age = _heartbeat_age(record, now)
    if age is None:
        return False  # alive PID, unreadable heartbeat -> don't reclaim on ambiguity
    return age >= STALE_HEARTBEAT_SECS  # (b) alive-but-wedged


# --- The context manager -------------------------------------------------------
class _GatewayLock:
    """Held instance returned by `gateway_lock(...)`. Use via the context-manager API.

    Acquire on __enter__ (atomic create + stale reclaim, polling up to wait_secs), run a
    daemon heartbeat thread while held, release on __exit__ on BOTH normal exit and
    exception (try/finally semantics), and never stomp a successor's lock (release-iff-owner).
    """

    def __init__(
        self,
        purpose: str,
        client_id: int,
        on_busy: str = POLICY_SKIP,
        wait_secs: float | None = None,
        *,
        lock_path: str = LOCK_PATH,
        poll_interval: float = POLL_INTERVAL_SECS,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECS,
        # Injection seams for offline tests (no real sleeps, no real clock, no tasklist):
        pid_alive=_pid_alive,
        now_fn=time.time,
        sleep_fn=time.sleep,
        extra: dict | None = None,
    ):
        if on_busy not in (POLICY_SKIP, POLICY_REFUSE):
            raise ValueError(f"on_busy must be {POLICY_SKIP!r} or {POLICY_REFUSE!r}, got {on_busy!r}")
        self.purpose = purpose
        self.client_id = client_id
        self.on_busy = on_busy
        self.wait_secs = _DEFAULT_WAIT[on_busy] if wait_secs is None else wait_secs
        self.lock_path = lock_path
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self._pid_alive = pid_alive
        self._now = now_fn
        self._sleep = sleep_fn
        self._extra = extra or {}

        self.pid = os.getpid()
        self.host = socket.gethostname()
        self.acquired_ts: float | None = None
        self._stop = threading.Event()
        self._beat_thread: threading.Thread | None = None
        self._held = False

    # -- record payload ---------------------------------------------------------
    def _record(self, now: float) -> dict:
        return {
            "pid": self.pid,
            "client_id": self.client_id,
            "purpose": self.purpose,
            "host": self.host,
            "acquired_ts": self.acquired_ts,
            "acquired_at": datetime.fromtimestamp(self.acquired_ts).isoformat(timespec="seconds")
            if self.acquired_ts else None,
            "heartbeat_ts": now,
            "heartbeat_at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            **self._extra,
        }

    def _write_record(self, fd: int, now: float) -> None:
        """Write our JSON record into the already-held exclusive fd.

        The atomic primitive is the EXCLUSIVE CREATE of the path (we won the race); the
        JSON write follows into the held fd — identical ordering to the supervisor, richer
        payload.
        """
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._record(now)))

    def _try_create(self) -> bool:
        """One atomic-create attempt. True if we now hold the lock (record written)."""
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        now = self._now()
        self.acquired_ts = now
        self._write_record(fd, now)
        return True

    # -- acquire / release ------------------------------------------------------
    def acquire(self) -> "_GatewayLock":
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        deadline = self._now() + self.wait_secs
        while True:
            # 1) Try the atomic create (the supervisor's win-the-race primitive).
            if self._try_create():
                self._held = True
                self._start_heartbeat()
                return self

            # 2) Someone holds (or held) it. Inspect the record for stale reclaim.
            record = _read_record(self.lock_path)
            if _is_stale(record, self._now(), self._pid_alive):
                # Dead PID or wedged-no-heartbeat (or garbage) -> reclaim and retry create.
                try:
                    os.unlink(self.lock_path)
                except OSError:
                    pass  # a racing reclaimer beat us; the retry-create below handles it
                continue

            # 3) Live, non-stale holder -> wait and re-poll, or raise the policy exception.
            if self._now() >= deadline:
                raise self._busy_exc(record)
            self._sleep(min(self.poll_interval, max(0.0, deadline - self._now())))

    def _busy_exc(self, record: dict | None) -> GatewayBusy:
        who = self._describe(record)
        if self.on_busy == POLICY_REFUSE:
            return GatewayBusyRefuse(f"REFUSING to start — Gateway {who}", holder=record)
        return GatewayBusySkip(f"SKIP this cycle — Gateway {who}", holder=record)

    @staticmethod
    def _describe(record: dict | None) -> str:
        if not record:
            return "is held (holder record unreadable)"
        return (f"held by {record.get('purpose')} pid {record.get('pid')} "
                f"clientId {record.get('client_id')} since "
                f"{record.get('acquired_at') or record.get('acquired_ts')}")

    def _i_own_it(self) -> bool:
        """True iff the on-disk record's pid is still ours (release-iff-owner)."""
        record = _read_record(self.lock_path)
        return bool(record) and record.get("pid") == self.pid

    def release(self) -> None:
        """Release iff we still own it; never stomp a successor's lock."""
        self._stop.set()
        if self._beat_thread is not None:
            self._beat_thread.join(timeout=2.0)
            self._beat_thread = None
        try:
            if os.path.exists(self.lock_path) and self._i_own_it():
                os.unlink(self.lock_path)
        except OSError:
            pass
        self._held = False

    # -- heartbeat --------------------------------------------------------------
    def _start_heartbeat(self) -> None:
        self._stop.clear()
        t = threading.Thread(target=self._heartbeat_loop, name="gateway-lock-heartbeat",
                             daemon=True)
        self._beat_thread = t
        t.start()

    def _heartbeat_loop(self) -> None:
        """Refresh heartbeat_ts every ~interval while held, so a long-but-healthy hold
        keeps its lease alive. Stops promptly when release() sets the stop event.
        """
        while not self._stop.wait(self.heartbeat_interval):
            self.beat()

    def beat(self) -> None:
        """Rewrite the record with a fresh heartbeat_ts — but only if we still own it.

        Called by the daemon thread (and available for an explicit between-blocks call).
        Guarded by _i_own_it so a beat can never resurrect a lock we already lost/reclaimed.
        """
        if not self._i_own_it():
            return
        try:
            with open(self.lock_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(self._record(self._now())))
        except OSError:
            pass

    # -- context manager protocol ----------------------------------------------
    def __enter__(self) -> "_GatewayLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Release on BOTH normal exit and exception (try/finally semantics): a crashed or
        # Ctrl-C'd holder never leaves a poisoned lock. Returning False re-raises any
        # in-body exception unchanged.
        self.release()
        return False


def gateway_lock(
    purpose: str,
    client_id: int,
    on_busy: str = POLICY_SKIP,
    wait_secs: float | None = None,
    **kwargs,
) -> _GatewayLock:
    """Acquire the single-process Gateway mutex as a context manager.

    Parameters
    ----------
    purpose : str
        Human label recorded in the lock and surfaced on a busy outcome (e.g. "monitor",
        "rebalance_execute").
    client_id : int
        The IBKR clientId this caller will connect with (recorded so a refusal can name it).
    on_busy : {"skip", "refuse"}
        Policy when the Gateway is held by a live, non-stale holder:
          * "skip"   (monitor-style): wait ~MONITOR_WAIT_SECS then raise GatewayBusySkip.
          * "refuse" (rebalance-style): wait ~REBALANCE_WAIT_SECS then raise
            GatewayBusyRefuse, whose message names the holder (purpose/pid/clientId/since).
    wait_secs : float | None
        Override the per-policy default wait. Tests inject a tiny value to stay fast.

    Returns
    -------
    _GatewayLock
        Use as `with gateway_lock(...):`. Acquires on enter, runs a heartbeat thread while
        held, releases on exit (normal AND exception). Raises GatewayBusySkip /
        GatewayBusyRefuse on a contended, non-stale Gateway.

    Notes
    -----
    Stale holders are reclaimed automatically: a dead holder PID (tasklist liveness) OR an
    alive-but-silent holder (no heartbeat for STALE_HEARTBEAT_SECS) is unlinked and the
    atomic create retried — the proven spxw_1m_supervisor reclaim, extended with the
    heartbeat horizon.
    """
    return _GatewayLock(purpose, client_id, on_busy=on_busy, wait_secs=wait_secs, **kwargs)
