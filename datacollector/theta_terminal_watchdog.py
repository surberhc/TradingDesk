"""
theta_terminal_watchdog.py — keep the ThetaData local Terminal alive.

WHY THIS EXISTS
---------------
The SPXW 1-minute collector has a self-healing supervisor (Windows Scheduled
Task `Spxw1mCollector`) that restarts the COLLECTOR. But nothing restarts the
ThetaData TERMINAL — the local Java REST gateway on 127.0.0.1:25503. On
2026-06-28 the terminal died and the collector just stalled silently against a
dead port (the supervisor's stall-watchdog eventually kills the collector, but
it can only relaunch the collector against the same dead terminal). ROOT CAUSE
= no terminal watchdog. This script is that watchdog.

WHAT IT DOES
------------
On a short interval it TCP-probes 127.0.0.1:25503. If the port is unreachable
for N consecutive probes (debounce, to ride out a brief blip), it relaunches the
terminal via start_terminal.py — but ONLY after confirming no terminal java
process is already running (so we never run two). Every probe/restart is logged
with a timestamp; a heartbeat file is refreshed each cycle so an external
monitor (or Andrew) can see at a glance that the watchdog is alive.

Singleton-guarded by an atomic PID lock so two watchdogs never race to relaunch
(mirrors spxw_1m_supervisor.py). Crash-safe: every cycle is wrapped so the loop
can never die on a transient error.

PAPER / research infra only. This only starts a local data gateway; it places no
orders and touches no account.

Run:  pythonw theta_terminal_watchdog.py   (registered as Scheduled Task
       `ThetaTerminalWatchdog`; survives logoff/reboot via run_theta_watchdog.bat)

A one-shot self-test of the port probe (no restart, no side effects):
       python theta_terminal_watchdog.py --selftest
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import subprocess
import sys
import time

import config

# --------------------------------------------------------------------------- #
# Paths / files (all small state under the LOCAL warehouse, never Drive)
# --------------------------------------------------------------------------- #
HERE = config.CODE_ROOT
START_TERMINAL = HERE / "start_terminal.py"
LOG = config.DATA_ROOT / "theta_watchdog.log"
HEARTBEAT = config.DATA_ROOT / "theta_watchdog_heartbeat.txt"
LOCK = config.DATA_ROOT / "theta_watchdog.lock"

# --------------------------------------------------------------------------- #
# Probe / debounce tuning
# --------------------------------------------------------------------------- #
HOST = "127.0.0.1"
PORT = 25503               # ThetaData v3 REST gateway (config.THETA_BASE_URL)
PROBE_TIMEOUT = 3          # seconds per TCP connect attempt
TICK = 25                  # seconds between probes
FAILS_BEFORE_RESTART = 3   # consecutive failures -> restart (~3 x 25s ≈ 75s of debounce)
# After a relaunch, give the Java terminal time to boot + bind the port before we
# resume probing, so we don't immediately re-trigger on a still-starting terminal.
RESTART_GRACE = 90

# Interpreter selection: this venv's Scripts\python.exe is a relauncher STUB, so
# we spawn children with the REAL base interpreter and put the venv site-packages
# on PYTHONPATH (identical convention to spxw_1m_supervisor.py / run_spxw_1m.bat).
PY = getattr(sys, "_base_executable", None) or sys.executable


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def heartbeat(state: str) -> None:
    try:
        HEARTBEAT.write_text(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {state}")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Port probe — the core check
# --------------------------------------------------------------------------- #
def port_up(host: str = HOST, port: int = PORT, timeout: float = PROBE_TIMEOUT) -> bool:
    """True iff a TCP connection to host:port succeeds within `timeout`.

    Pure stdlib, no dependence on the terminal answering HTTP — a successful
    TCP handshake means something is listening on the port (the terminal). This
    is deliberately the cheapest possible liveness signal so the watchdog has no
    heavy deps and can't itself wedge.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Terminal process detection — never run two
# --------------------------------------------------------------------------- #
def terminal_running() -> bool:
    """True if a ThetaTerminal java process appears to be running.

    Belt-and-suspenders to the port probe: even if the port momentarily isn't
    answering, we must not spawn a SECOND terminal if a java process for the jar
    is already up (it may just be booting / mid-restart). We match on the jar
    name in the java command line via WMIC/CIM.
    """
    if os.name != "nt":
        return False
    jar = config.THETA_TERMINAL_JAR.name  # "ThetaTerminalv3.jar"
    # PowerShell CIM query is the most reliable way to read full command lines.
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" "
        "| Select-Object -ExpandProperty CommandLine"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20,
        )
        return jar.lower() in (out.stdout or "").lower()
    except Exception:
        # If we genuinely can't tell, err toward NOT spawning a duplicate.
        log("WARN: could not enumerate java processes; assuming a terminal is "
            "running to avoid a duplicate launch")
        return True


def launch_terminal() -> None:
    """Relaunch the terminal via start_terminal.py, detached, no console window.

    start_terminal.py blocks (it runs the java process to completion), so we
    launch it as a fire-and-forget child. We do NOT capture its handle: the
    terminal must outlive any single watchdog cycle and survive a watchdog
    restart. The next probe loop confirms it actually came up.
    """
    env = os.environ.copy()
    site = _venv_site()
    if site:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = site + (os.pathsep + existing if existing else "")
    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: fully independent child,
        # no console, not killed when the watchdog exits/restarts.
        creationflags = 0x00000008 | 0x00000200
    log(f"LAUNCH: starting terminal via {START_TERMINAL.name}")
    subprocess.Popen(
        [PY, str(START_TERMINAL)],
        cwd=str(config.DATA_ROOT),
        env=env,
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _venv_site() -> str:
    try:
        for p in sys.path:
            if p.endswith("site-packages") and "venv" in p.lower():
                return p
    except Exception:
        pass
    cand = os.path.join(sys.prefix, "Lib", "site-packages")
    return cand if os.path.isdir(cand) else ""


# --------------------------------------------------------------------------- #
# Singleton lock (atomic O_CREAT|O_EXCL PID lock; mirrors the supervisor)
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
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
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock() -> bool:
    me = os.getpid()
    for _ in range(2):
        try:
            fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(me))
            return True
        except FileExistsError:
            try:
                holder = int(LOCK.read_text().strip() or "0")
            except (OSError, ValueError):
                holder = 0
            if holder == me:
                return True
            if holder and _pid_alive(holder):
                log(f"another watchdog is live (pid={holder}) -> exiting")
                return False
            log(f"stale lock (pid={holder} not running) -> reclaiming")
            try:
                LOCK.unlink()
            except OSError:
                pass
            continue
    return False


def release_lock() -> None:
    try:
        if LOCK.exists() and LOCK.read_text().strip() == str(os.getpid()):
            LOCK.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def main() -> None:
    if not acquire_lock():
        return
    try:
        log("=== theta terminal watchdog start "
            f"(probe {HOST}:{PORT} every {TICK}s, "
            f"restart after {FAILS_BEFORE_RESTART} consecutive fails) ===")
        consecutive_fails = 0
        while True:
            try:
                if port_up():
                    if consecutive_fails:
                        log(f"port back UP after {consecutive_fails} fail(s)")
                    consecutive_fails = 0
                    heartbeat(f"UP  port={PORT}")
                else:
                    consecutive_fails += 1
                    log(f"port DOWN ({consecutive_fails}/{FAILS_BEFORE_RESTART})")
                    heartbeat(f"DOWN {consecutive_fails}/{FAILS_BEFORE_RESTART} "
                              f"port={PORT}")
                    if consecutive_fails >= FAILS_BEFORE_RESTART:
                        if terminal_running():
                            log("debounce tripped but a terminal java process is "
                                "already running (booting?) — NOT launching a "
                                "second; will keep probing")
                        else:
                            launch_terminal()
                            heartbeat(f"RESTARTING port={PORT}")
                            log(f"launched; sleeping {RESTART_GRACE}s grace for "
                                "the terminal to bind the port")
                            time.sleep(RESTART_GRACE)
                        consecutive_fails = 0
            except Exception as e:        # never let the watchdog itself die
                log(f"watchdog error: {e!r}; continuing")
            time.sleep(TICK)
    finally:
        release_lock()


def selftest() -> int:
    """One-shot, side-effect-free check of the probe. Exit 0 if port UP."""
    up = port_up()
    print(f"theta watchdog selftest: {HOST}:{PORT} -> "
          f"{'UP' if up else 'DOWN'}")
    print(f"  terminal java process detected: {terminal_running()}")
    return 0 if up else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
