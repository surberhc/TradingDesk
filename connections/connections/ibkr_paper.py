"""
ibkr_paper.py — the one way to start and connect to the IBKR PAPER Gateway.

PAPER ONLY: paper login, paper port 4002. There is no real-money path here. A
read-only connection (the default) is physically incapable of transmitting an order;
the paperbot keeps it read-only until a human deliberately arms order transmission.

Gateway launch reuses the proven IBController script the dailyreport already uses
(`C:\\IBC-Paper\\StartGatewayPaper.bat`), which auto-logs into the paper account.

`ensure_gateway()` carries a NARROW launch mutex: an atomic lockfile that lets at
most ONE StartGatewayPaper.bat launch be in flight across all our processes, with a
relaunch cooldown. This is the fix for the incident where a wedged login made
canslim/ibkr_price_gapfill's per-symbol reconnect loop stack ~91 dead gateways in
3.5h. It is NOT the full monitor/rebalance operate-mutex described in
docs/GATEWAY_LOCK_SPEC.md (that remains future work) — it only serializes launches.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time

from ib_async import IB, Stock

from connections import clientids

HOST = "127.0.0.1"
PAPER_PORT = clientids.PAPER_PORT          # 4002
GATEWAY_BAT = r"C:\IBC\StartGateway.bat"   # IBController auto-login (paper)

# Launch mutex state. LOCAL C: only — Drive sync corrupts O_EXCL atomicity, and
# the file must be readable by non-elevated processes (the incident's cleanup
# needed elevation precisely because we couldn't inspect elevated java procs).
# Overridable via env so tests can point it at a tmp dir.
GATEWAY_LAUNCH_LOCK = os.environ.get(
    "TRADINGDESK_PAPER_GATEWAY_LAUNCH_LOCK",
    r"C:\TradingDesk-Local\state\paper\gateway_launch.lock",
)
RELAUNCH_COOLDOWN_SECS = 180   # no relaunch within this long of the last attempt


def _gateway_env() -> dict:
    """Environment for launching the Gateway via IBC.

    Works around a real bug in IBC 3.24.0's StartIBC.bat with Gateway 1045: its
    JRE-version probe (`java.exe -XshowSettings:properties | findstr "java.version ="`)
    comes back EMPTY in the launch context, and the next line
    `if not "%java_version:1.8=%"=="%java_version%" set moduleAccess=` then throws
    "set was unexpected at this time", aborting BEFORE Java ever starts. The probe
    only assigns java_version if it matches output, so pre-seeding java_version to a
    non-1.8 value makes the broken line a safe no-op and the Gateway launches.
    No IBKR script is modified - this is purely an inherited environment variable.
    (Verified 2026-06-26: with this set, port 4002 comes up in ~15s.)
    """
    env = dict(os.environ)
    env["java_version"] = "17"
    return env


def gateway_running(client_id: int = clientids.CLIENT_IDS["dailyreport_gateway_check"],
                    timeout: int = 8) -> bool:
    """True if the paper Gateway is up and serving data (a real data round-trip)."""
    ib = IB()
    try:
        ib.connect(HOST, PAPER_PORT, clientId=client_id, readonly=True, timeout=timeout)
        spy = Stock("SPY", "SMART", "USD")
        ib.qualifyContracts(spy)
        bars = ib.reqHistoricalData(
            spy, endDateTime="", durationStr="1 D", barSizeSetting="1 day",
            whatToShow="TRADES", useRTH=True, formatDate=1, timeout=20)
        ib.disconnect()
        return len(bars) > 0
    except Exception:
        try:
            ib.disconnect()
        except Exception:
            pass
        return False


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID is currently running (Windows + POSIX).

    Copied from datacollector/spxw_1m_supervisor.py's _pid_alive: tasklist is the
    dependency-free liveness test on Windows, and an undeterminable result is
    treated as ALIVE — here that makes us a waiter rather than risk a dup launch.
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
            # Can't tell -> assume alive (conservative: prefer to wait, not stack).
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_lock_record(fd_or_path, *, started_at=None, attempt_done_at=None) -> None:
    """Write our launcher record as JSON. fd_or_path is an open fd (int) or path.

    started_at defaults to now (acquire time). Pass the original acquire time when
    RE-writing the record on release so stamping attempt_done_at doesn't reset it
    (which would corrupt the in_flight computation for subsequent callers).
    """
    rec = {
        "pid": os.getpid(),
        "started_at": time.time() if started_at is None else started_at,
        "attempt_done_at": attempt_done_at,
        "host": socket.gethostname(),
    }
    data = json.dumps(rec)
    if isinstance(fd_or_path, int):
        os.write(fd_or_path, data.encode("utf-8"))
    else:
        with open(fd_or_path, "w", encoding="utf-8") as f:
            f.write(data)


def _read_lock_record(path: str):
    """Return the holder record dict, or None if missing/unreadable/garbage."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            rec = json.loads(f.read() or "{}")
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


def _poll_until_up(wait_secs: int) -> bool:
    """Poll gateway_running() every ~10s up to wait_secs. True as soon as it's up."""
    waited = 0
    while waited < wait_secs:
        time.sleep(10)
        waited += 10
        if gateway_running():
            return True
    return False


def ensure_gateway(wait_secs: int = 180) -> bool:
    """Make sure the paper Gateway is up; launch it (IBC auto-login) if not. Returns
    True once it's serving data, False if it never came up within wait_secs.

    NARROW launch mutex (see module docstring): coordinates via an atomic local
    lockfile so at most ONE StartGatewayPaper.bat launch is ever in flight across all
    our processes, and no relaunch happens within RELAUNCH_COOLDOWN_SECS of the
    previous attempt. This exists because a wedged login (one-login-per-username)
    otherwise lets a per-symbol reconnect loop stack dead gateways and pin the box.

    Fast path is byte-for-byte equivalent to before: a healthy gateway returns
    immediately WITHOUT touching the filesystem or launching anything.

    Any filesystem/permission error in the locking path FAILS SAFE toward
    not-stacking: we degrade to the waiter path (poll, never Popen), never raise.
    """
    if gateway_running():
        return True

    # Below here the gateway is down. Coordinate a launch via the lockfile.
    # Reuses datacollector/spxw_1m_supervisor.py's acquire_lock pattern:
    # O_CREAT|O_EXCL create -> launcher; existing+live+recent -> waiter;
    # existing+dead/stale -> reclaim (unlink + retry) -> launcher.
    try:
        os.makedirs(os.path.dirname(GATEWAY_LAUNCH_LOCK), exist_ok=True)
    except OSError as e:
        # Can't even make the state dir -> fail safe: wait, do not launch.
        print(f"ensure_gateway: cannot prepare lock dir ({e!r}); waiting, not launching")
        return _poll_until_up(wait_secs)

    is_launcher = False
    acquire_started_at = None
    for _attempt in range(2):
        try:
            fd = os.open(GATEWAY_LAUNCH_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            rec = _read_lock_record(GATEWAY_LAUNCH_LOCK)
            if rec is None:
                # Unreadable/garbage record: treat as stale -> reclaim once.
                try:
                    os.unlink(GATEWAY_LAUNCH_LOCK)
                except OSError:
                    return _poll_until_up(wait_secs)   # fail safe: wait
                continue
            holder_pid = int(rec.get("pid") or 0)
            started_at = rec.get("started_at") or 0
            attempt_done_at = rec.get("attempt_done_at")
            now = time.time()
            in_flight = (now - started_at) < wait_secs
            cooling_down = (attempt_done_at is not None
                            and (now - attempt_done_at) < RELAUNCH_COOLDOWN_SECS)
            if _pid_alive(holder_pid) and (in_flight or cooling_down):
                # Someone else is launching or the last attempt is still cooling
                # down -> we must NOT launch. Just wait.
                return _poll_until_up(wait_secs)
            # Holder is dead, or the record is older than both windows (stale) ->
            # reclaim it and retry the atomic create to become the launcher.
            try:
                os.unlink(GATEWAY_LAUNCH_LOCK)
            except OSError:
                return _poll_until_up(wait_secs)       # fail safe: wait
            continue
        except OSError as e:
            # Any other lock error (permissions, etc.) -> fail safe: wait.
            print(f"ensure_gateway: lock error ({e!r}); waiting, not launching")
            return _poll_until_up(wait_secs)
        else:
            # We won the atomic create -> we are the launcher.
            acquire_started_at = time.time()
            try:
                _write_lock_record(fd, started_at=acquire_started_at,
                                   attempt_done_at=None)
            finally:
                os.close(fd)
            is_launcher = True
            break

    if not is_launcher:
        # Exhausted reclaim attempts without winning the lock -> wait, don't launch.
        return _poll_until_up(wait_secs)

    # We are the sole launcher. Popen EXACTLY ONCE, then poll.
    came_up = False
    try:
        subprocess.Popen(["cmd", "/c", GATEWAY_BAT],
                         creationflags=subprocess.CREATE_NEW_CONSOLE, env=_gateway_env())
        came_up = _poll_until_up(wait_secs)
    finally:
        # Release iff we still own the lock (its recorded pid is ours).
        #   - CAME UP  -> unlink (clean release; healthy callers hit the fast path,
        #                 and a later down-event is free to relaunch immediately).
        #   - DID NOT  -> DO NOT unlink. Stamp attempt_done_at=now while PRESERVING
        #                 the original started_at, leaving a cooldown marker so the
        #                 next caller sees cooling_down and refuses to relaunch until
        #                 RELAUNCH_COOLDOWN_SECS elapses; after that the stale-by-age
        #                 reclaim path lets a later caller reclaim + relaunch.
        try:
            rec = _read_lock_record(GATEWAY_LAUNCH_LOCK)
            if rec is not None and int(rec.get("pid") or 0) == os.getpid():
                if came_up:
                    os.unlink(GATEWAY_LAUNCH_LOCK)
                else:
                    _write_lock_record(GATEWAY_LAUNCH_LOCK,
                                       started_at=acquire_started_at,
                                       attempt_done_at=time.time())
        except OSError:
            pass
    return came_up


def connect(consumer: str, readonly: bool = True, launch: bool = False, timeout: int = 10) -> IB:
    """Connect to the PAPER Gateway using a registered clientId.

    consumer : a key in connections.clientids.CLIENT_IDS (e.g. "paperbot").
    readonly : True (default) -> the session cannot transmit orders. The paperbot
               only flips this to False when a human arms order transmission.
    launch   : if True, start the Gateway first when it's down.
    """
    client_id = clientids.get(consumer)
    if launch and not gateway_running():
        if not ensure_gateway():
            raise RuntimeError("paper Gateway did not come up")
    ib = IB()
    ib.connect(HOST, PAPER_PORT, clientId=client_id, readonly=readonly, timeout=timeout)
    return ib
