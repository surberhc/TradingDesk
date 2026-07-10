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

DESIGN (rewritten 2026-07-10 — see the incident below)
-------------------------------------------------------
ONE check per invocation, same pattern as connections/gateway_watchdog.py: the
Windows scheduler provides the cadence (every 1 min), and main() is a thin
wire-up around a pure, injectable run_once(...) so the whole policy is
unit-tested offline with a fake clock. State (down_since / restarts / alerted)
persists to a local JSON file between invocations. main() never raises and
always exits 0, so a hiccup can never wedge the scheduled task's own cadence.

WHY THE REWRITE — 2026-07-09 INCIDENT: the previous design ran as a single
long-lived daemon (infinite while-loop, woken once at 6am + at logon). That
process itself silently died sometime after 09:00:41 and nothing noticed for
11h52m (until an unrelated manual check at 20:52:55) — Task Scheduler's daily
trigger only refuses to double-start (singleton lock correctly saw the prior
day's process as "still alive" at 6am), so once that one process died there
was NO supervisor for the supervisor. The 5:30pm ThetaEodDaily forward-collector
run hit the dead terminal and lost the entire 2026-07-09 options-chain snapshot.
A daemon that can silently die is architecturally the same failure family the
gateway watchdog was explicitly built to avoid (see gateway_watchdog.py) — this
rewrite closes that gap by never staying resident: each scheduler tick is a
fresh, short-lived process, so a dead previous instance costs at most one
missed tick (~1 minute), never a full day.

WHAT IT DOES
------------
Each invocation TCP-probes 127.0.0.1:25503 once. If down, a persisted
down_since timestamp accumulates across invocations; once continuously down
for >= GRACE_SECS, it relaunches the terminal via start_terminal.py — but only
after confirming no terminal java process is already running (so we never run
two, and don't re-trigger on a still-booting terminal). A rolling-hour restart
cap (mirroring gateway_watchdog) stops a repeatedly-crashing terminal from
restart-looping forever; past the cap it alerts once (log + marker file) and
holds.

PAPER / research infra only. This only starts a local data gateway; it places
no orders and touches no account.

Run:  python theta_terminal_watchdog.py     (registered as Scheduled Task
       `ThetaTerminalWatchdog`, every 1 min, via run_theta_watchdog.bat)

A one-shot self-test of the port probe (no restart, no side effects):
       python theta_terminal_watchdog.py --selftest
"""

from __future__ import annotations

import datetime as dt
import json
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
STATE_FILE = os.environ.get(
    "TRADINGDESK_THETA_WATCHDOG_STATE",
    str(config.DATA_ROOT / "theta_watchdog_state.json"),
)
ALERT_MARKER = config.DATA_ROOT / "theta_watchdog_alert.txt"

# --------------------------------------------------------------------------- #
# Probe / policy tuning
# --------------------------------------------------------------------------- #
HOST = "127.0.0.1"
PORT = 25503               # ThetaData v3 REST gateway (config.THETA_BASE_URL)
PROBE_TIMEOUT = 3          # seconds per TCP connect attempt
GRACE_SECS = 90            # continuously down this long (across invocations) -> restart
MAX_RESTARTS_PER_HOUR = 3  # after this many restarts in a rolling hour, STOP and alert
# After a relaunch, the terminal needs time to boot + bind the port; a probe in the
# next invocation or two may still see it down — that's fine, it just re-starts the
# grace clock rather than a duplicate launch (terminal_running() guards that).

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
    name in the java command line via CIM.
    """
    if os.name != "nt":
        return False
    jar = config.THETA_TERMINAL_JAR.name  # "ThetaTerminalv3.jar"
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


def launch_terminal() -> bool:
    """Relaunch the terminal via start_terminal.py, detached, no console window.

    start_terminal.py blocks (it runs the java process to completion), so we
    launch it as a fire-and-forget child. We do NOT capture its handle: the
    terminal must outlive this (short-lived) watchdog invocation. The NEXT
    invocation's probe confirms it actually came up — this function only
    reports whether the launch subprocess itself was spawned without error.
    """
    env = os.environ.copy()
    site = _venv_site()
    if site:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = site + (os.pathsep + existing if existing else "")
    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: fully independent child,
        # no console, not killed when this watchdog invocation exits.
        creationflags = 0x00000008 | 0x00000200
    try:
        subprocess.Popen(
            [PY, str(START_TERMINAL)],
            cwd=str(config.DATA_ROOT),
            env=env,
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:  # noqa: BLE001 — a launch hiccup must not raise out of policy
        log(f"launch_terminal error: {e!r}")
        return False


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
# PURE DECISION LOGIC — fully injectable; tests drive this with a fake clock and
# mocked healthy/already_running/launch_fn/log_fn. Returns (new_state, action).
#
# action is one of:
#   "healthy" | "grace_started" | "within_grace" | "booting"
#   | "restarted" | "restart_failed" | "rate_limited"
# --------------------------------------------------------------------------- #
def run_once(*, now, healthy, already_running, state, launch_fn, log_fn):
    """Run one watchdog cycle. Never raises for policy reasons; returns updated state.

    Parameters
    ----------
    now             : float epoch seconds (injected clock).
    healthy         : zero-arg callable -> bool. TCP port probe.
    already_running : zero-arg callable -> bool. True if a terminal java process
                      is already up (mid-boot) — a wedged-past-grace check must
                      NOT launch a second one in that case.
    state           : dict with keys down_since (float|None), restarts (list[float]),
                      alerted (bool). Missing keys are defaulted.
    launch_fn       : zero-arg callable -> bool. Fire-and-forget launch of the
                      terminal; True == the launch subprocess was spawned cleanly.
    log_fn          : one-arg callable(str) for a human log line.
    """
    down_since = state.get("down_since")
    restarts = list(state.get("restarts") or [])
    alerted = bool(state.get("alerted"))

    def _persist(action):
        return ({"down_since": down_since,
                 "restarts": restarts,
                 "alerted": alerted}, action)

    is_up = bool(healthy())

    # Prune the rolling-hour restart list so the rate limit is a true window.
    restarts = [t for t in restarts if (now - t) < 3600.0]

    if is_up:
        if down_since is not None or alerted:
            log_fn("port back UP — cleared down timer and alert")
        else:
            log_fn("healthy")
        down_since = None
        alerted = False
        return _persist("healthy")

    if down_since is None:
        down_since = now
        log_fn("port DOWN; grace timer started")
        return _persist("grace_started")

    down_for = now - down_since
    if down_for < GRACE_SECS:
        log_fn(f"port down {int(down_for)}s, within grace ({GRACE_SECS}s)")
        return _persist("within_grace")

    # Down >= GRACE_SECS -> candidate restart. Don't double-launch a booting terminal.
    if already_running():
        log_fn("grace expired but a terminal java process is already running "
               "(booting?) — NOT launching a second; will keep probing")
        return _persist("booting")

    if len(restarts) >= MAX_RESTARTS_PER_HOUR:
        if not alerted:
            log_fn(
                f"ALERT: terminal wedged — survived {len(restarts)} restarts in "
                f"the last hour (limit {MAX_RESTARTS_PER_HOUR}); NOT restarting "
                f"again. Needs a human to look at the ThetaData terminal/host.")
            alerted = True
        else:
            log_fn(
                f"still wedged past the {MAX_RESTARTS_PER_HOUR}/hr restart limit; "
                f"already alerted — holding (no restart).")
        return _persist("rate_limited")

    log_fn(f"port wedged (down {int(down_for)}s >= grace {GRACE_SECS}s); "
           f"force-restart {len(restarts) + 1}/{MAX_RESTARTS_PER_HOUR} this hour")
    launched = bool(launch_fn())
    restarts.append(now)   # the attempt counts against the rolling limit regardless
    if launched:
        log_fn("launched — sleeping until next invocation confirms it bound the port")
        return _persist("restarted")
    log_fn("launch_fn reported failure; down timer retained, restart counted")
    return _persist("restart_failed")


# --------------------------------------------------------------------------- #
# State load/save — LOCAL C: only. Never raises.
# --------------------------------------------------------------------------- #
def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.loads(f.read() or "{}")
        if not isinstance(s, dict):
            return {"down_since": None, "restarts": [], "alerted": False}
        return {
            "down_since": s.get("down_since"),
            "restarts": list(s.get("restarts") or []),
            "alerted": bool(s.get("alerted")),
        }
    except (OSError, ValueError):
        return {"down_since": None, "restarts": [], "alerted": False}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, indent=2))
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log(f"could not write state ({e!r})")


def _write_alert_marker(now: float, restarts) -> None:
    try:
        stamp = dt.datetime.fromtimestamp(now).isoformat(timespec="seconds")
        msg = (f"{stamp}  ALERT theta terminal wedged: survived {len(restarts)} "
               f"restarts in the last hour (limit {MAX_RESTARTS_PER_HOUR}); NOT "
               f"restarting again. Needs a human to check the ThetaData terminal.\n")
        ALERT_MARKER.parent.mkdir(parents=True, exist_ok=True)
        ALERT_MARKER.write_text(msg, encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# main() — thin wire-up. Never raises; always exit 0 (scheduler-friendly).
# --------------------------------------------------------------------------- #
def main() -> int:
    try:
        now = time.time()
        state = _load_state()
        was_alerted = bool(state.get("alerted"))

        new_state, action = run_once(
            now=now,
            healthy=port_up,
            already_running=terminal_running,
            state=state,
            launch_fn=launch_terminal,
            log_fn=log,
        )

        if action == "rate_limited" and new_state.get("alerted") and not was_alerted:
            _write_alert_marker(now, new_state.get("restarts") or [])

        heartbeat(f"{action} port={PORT}")
        _save_state(new_state)
    except Exception as e:  # noqa: BLE001 — a transient error must never wedge the task
        log(f"unexpected error (exiting 0 so the task keeps its cadence): {e!r}")
    return 0


def selftest() -> int:
    """One-shot, side-effect-free check of the probe. Exit 0 if port UP."""
    up = port_up()
    print(f"theta watchdog selftest: {HOST}:{PORT} -> {'UP' if up else 'DOWN'}")
    print(f"  terminal java process detected: {terminal_running()}")
    return 0 if up else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
