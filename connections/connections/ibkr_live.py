"""
ibkr_live.py — the one way to start and connect to the LIVE-TRADING Gateway: a
separate, FUNDED, transmit-CAPABLE account used for Strategy 8's zero-transmit
live pilot.

This is a SEPARATE Gateway instance from BOTH the paper Gateway in ibkr.py (port
4002) AND the read-only live-data Gateway in ibkr_live_data.py (port 4001): a
distinct install directory, a distinct port (4003, LIVE_TRADE_PORT), and a
distinct live-trading login that — unlike the deliberately access-restricted
live-data login — CAN transmit orders at the account-permission level. There is
no account-level backstop here; execution capability exists on this account by
design, because this is where S8 will eventually trade for real.

Because this account is transmit-capable, this module is NOT structurally
read-only the way ibkr_live_data is, and it does not claim to be. `connect()`
DOES expose a real `readonly` parameter — but it DEFAULTS to True (fail-closed).
The two walls that keep the S8 pilot at zero transmissions are:

  1. PRIMARY, load-bearing: `PILOT_MODE=True` (hardcoded) in livebot/s8_runner.py.
     Nothing transmits while PILOT_MODE is set; that is the deliberate, armed gate.
  2. Fail-safe default: `connect(readonly=True)` here. During the pilot the S8
     runner only READS (account summary + a 0DTE SPXW chain snapshot), so the
     read-only default costs it nothing and guarantees a mere connection can never
     write. The future S8 executor is the ONLY intended caller that will ever pass
     `readonly=False` — a deliberate act, mirroring the paperbot rebalance
     runner/executor split.

This module's read-only default is a safety default, NOT the primary wall — do
not treat it as a substitute for PILOT_MODE.

Gateway launch mirrors ibkr.py's proven IBController pattern, pointed at a not-yet-
built install (`C:\\IBC-Live-Trade\\StartGatewayLiveTrade.bat`) that the user will
set up separately.

`ensure_gateway()` carries the same NARROW launch mutex as ibkr.py / ibkr_live_data.py:
an atomic lockfile that lets at most ONE StartGatewayLiveTrade.bat launch be in
flight across all our processes, with a relaunch cooldown. It uses its OWN lockfile,
distinct from BOTH the paper module's and the live-data module's, so the three
Gateways' launch coordination never overlaps.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time

from ib_async import IB, Stock

from connections import clientids
from connections.ibkr import _gateway_env  # shared JRE-probe workaround only; no paper/live logic

HOST = "127.0.0.1"
LIVE_TRADE_PORT = clientids.LIVE_TRADE_PORT       # 4003
GATEWAY_BAT = r"C:\IBC-Live-Trade\StartGatewayLiveTrade.bat"  # IBController auto-login (live-trading account) — not yet installed

# Launch mutex state. LOCAL C: only — Drive sync corrupts O_EXCL atomicity, and
# the file must be readable by non-elevated processes. Overridable via env so
# tests can point it at a tmp dir. Distinct path AND distinct env var from BOTH
# the paper module's and the live-data module's locks — the three must never
# share a lockfile.
GATEWAY_LAUNCH_LOCK = os.environ.get(
    "TRADINGDESK_LIVE_TRADE_GATEWAY_LAUNCH_LOCK",
    r"C:\TradingDesk-Local\state\live_trade\gateway_launch.lock",
)
RELAUNCH_COOLDOWN_SECS = 180   # no relaunch within this long of the last attempt


def gateway_running(client_id: int = clientids.CLIENT_IDS["s8_live_pilot"],
                    timeout: int = 8) -> bool:
    """True if the live-trading Gateway is up and serving data (a real data round-trip).

    Connects readonly=True — a health probe never needs write access, even though
    this module's connect() can grant it.
    """
    ib = IB()
    try:
        ib.connect(HOST, LIVE_TRADE_PORT, clientId=client_id, readonly=True, timeout=timeout)
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

    Mirrors ibkr.py's _pid_alive: tasklist is the dependency-free liveness test on
    Windows, and an undeterminable result is treated as ALIVE — here that makes us
    a waiter rather than risk a dup launch.
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
    """Make sure the live-trading Gateway is up; launch it (IBC auto-login) if not.
    Returns True once it's serving data, False if it never came up within wait_secs.

    NARROW launch mutex (see module docstring): coordinates via an atomic local
    lockfile so at most ONE StartGatewayLiveTrade.bat launch is ever in flight
    across all our processes, and no relaunch happens within RELAUNCH_COOLDOWN_SECS
    of the previous attempt. Own lockfile, entirely separate from the paper and
    live-data modules' locks.

    Fast path is byte-for-byte equivalent to before: a healthy gateway returns
    immediately WITHOUT touching the filesystem or launching anything.

    Any filesystem/permission error in the locking path FAILS SAFE toward
    not-stacking: we degrade to the waiter path (poll, never Popen), never raise.
    """
    if gateway_running():
        return True

    # Below here the gateway is down. Coordinate a launch via the lockfile.
    # Same pattern as ibkr.py / datacollector/spxw_1m_supervisor.py's acquire_lock:
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


def connect(consumer: str, launch: bool = False, readonly: bool = True, timeout: int = 10) -> IB:
    """Connect to the LIVE-TRADING Gateway (port 4003) using a registered clientId.

    consumer : a key in connections.clientids.CLIENT_IDS (e.g. "s8_live_pilot").
    launch   : if True, start the Gateway first when it's down.
    readonly : whether the API session refuses order transmission. DEFAULTS to True
               (fail-closed).

    Unlike ibkr_live_data.connect(), this module targets a transmit-CAPABLE
    live-trading account, so `readonly` IS a real, honored parameter here — there
    is no account-level backstop making writes impossible. It DEFAULTS to True as a
    fail-safe: the S8 pilot only ever READS (account summary + a 0DTE SPXW chain
    snapshot), so a read-only session is all it needs, and a bare connection can
    therefore never transmit. The ONLY intended caller that will ever pass
    readonly=False is the future S8 executor — a deliberate, gated act.

    This read-only default is a SECONDARY, fail-safe control. PILOT_MODE=True
    (hardcoded) in livebot/s8_runner.py remains the PRIMARY, load-bearing
    zero-transmit wall; do not lean on this default in its place.
    """
    client_id = clientids.get(consumer)
    if launch and not gateway_running():
        if not ensure_gateway():
            raise RuntimeError("live-trading Gateway did not come up")
    ib = IB()
    ib.connect(HOST, LIVE_TRADE_PORT, clientId=client_id, readonly=readonly, timeout=timeout)
    return ib
