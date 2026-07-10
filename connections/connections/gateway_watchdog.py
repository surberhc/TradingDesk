r"""
gateway_watchdog.py — detect a WEDGED IB paper Gateway and recover it by killing
the stuck gateway and bringing up exactly ONE fresh — with hard rate-limiting so
it can never become a hot loop.

PAPER ONLY. This never touches order transmission; it only restarts the login/data
gateway process. `ensure_gateway()` (which this calls to relaunch) uses the proven
paper StartGateway.bat auto-login and the narrow launch-mutex.

WHY THIS EXISTS (2026-07-05)
  A wedged IBKR login (one-login-per-username -> "existing session detected"
  auth-hang) let a per-symbol reconnect loop stack ~91 dead gateways and pin the
  machine. The launch-mutex fix (commit 4b2a827) stopped the *pileup*, but nothing
  KILLS a wedged gateway and brings up a fresh one — that is this watchdog.

  The gateways run ELEVATED. A non-elevated process cannot kill them (taskkill
  Access Denied) and cannot even read their command lines. THEREFORE this
  watchdog's scheduled task MUST run elevated (highest privileges) — see
  register_gateway_watchdog.ps1.

DESIGN
  * ONE check per invocation. The Windows scheduler provides the 5-minute cadence;
    once-per-invocation is reboot/crash-resilient (no long-lived loop to die).
  * The DECISION LOGIC is a pure, injectable function — run_once(...) — with a fake
    clock and mocked health/kill/launch so the whole policy is unit-tested offline.
    main() is a thin wire-up: real time.time(), ibkr.gateway_running(), the real
    kill wrapper, ibkr.ensure_gateway, load/save state, print.
  * State is LOCAL C: only (never Drive — Drive sync corrupts atomicity and the file
    must be readable/writable by the elevated task). Path overridable via env var
    so tests point it at a tmp dir.
  * main() NEVER raises: a transient error is caught, logged, and we exit 0 so a
    hiccup can never wedge the scheduled task itself.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from zoneinfo import ZoneInfo

from connections import ibkr

# --------------------------------------------------------------------------- #
# TUNABLE POLICY — Andrew's chosen defaults. These are the whole policy surface.
# --------------------------------------------------------------------------- #
CHECK_INTERVAL_MIN = 5          # informational; the SCHEDULER enforces the cadence
GRACE_SECS = 300               # gateway must be continuously down this long before we force-restart
MAX_RESTARTS_PER_HOUR = 3      # after this many restarts in a rolling hour, STOP and alert
# Skip the IBKR nightly reset window (America/New_York). Sourced from C:\IBC\config.ini
# AutoRestartTime = 11:45 PM: the gateway restarts ITSELF at 23:45, so a "down" reading
# in this window is the expected nightly bounce, not a wedge. Window spans midnight.
MAINTENANCE_WINDOW_ET = ("23:45", "00:45")

# --------------------------------------------------------------------------- #
# State file — LOCAL only, never Drive. Overridable via env so tests point it at tmp.
#   down_since : epoch float when the gateway was FIRST seen down (or null)
#   restarts   : list of epoch floats, one per force-restart, pruned to the last hour
#   alerted    : bool — have we already fired the loud "wedge survived N restarts" alert
# --------------------------------------------------------------------------- #
STATE_FILE = os.environ.get(
    "TRADINGDESK_GATEWAY_WATCHDOG_STATE",
    r"C:\TradingDesk-Local\state\paperbot\gateway_watchdog_state.json",
)

_NY = ZoneInfo("America/New_York")
_HOUR = 3600.0


# --------------------------------------------------------------------------- #
# Maintenance-window test (pure; midnight-spanning aware)
# --------------------------------------------------------------------------- #
def _in_maintenance_window(now: float, window=MAINTENANCE_WINDOW_ET) -> bool:
    """True if `now` (epoch) falls inside the ET maintenance window.

    Handles a window that SPANS MIDNIGHT (start > end), e.g. 23:45 -> 00:45.
    Comparison is done in America/New_York via stdlib zoneinfo (DST-correct)."""
    start_s, end_s = window
    sh, sm = (int(x) for x in start_s.split(":"))
    eh, em = (int(x) for x in end_s.split(":"))
    local = dt.datetime.fromtimestamp(now, tz=_NY)
    cur = local.hour * 60 + local.minute
    start = sh * 60 + sm
    end = eh * 60 + em
    if start <= end:
        return start <= cur < end
    # Spans midnight: inside if at/after start OR before end.
    return cur >= start or cur < end


def _prune_restarts(restarts, now: float):
    """Keep only restart timestamps within the last rolling hour."""
    return [t for t in restarts if (now - t) < _HOUR]


# --------------------------------------------------------------------------- #
# PURE DECISION LOGIC — fully injectable; tests drive this with a fake clock and
# mocked healthy/kill_fn/launch_fn/log_fn. Returns (new_state, action_taken).
#
# action_taken is one of:
#   "maintenance" | "healthy" | "grace_started" | "within_grace"
#   | "restarted" | "restart_failed" | "rate_limited"
# --------------------------------------------------------------------------- #
def run_once(*, now, healthy, state, kill_fn, launch_fn, log_fn):
    """Run one watchdog cycle. Never raises for policy reasons; returns updated state.

    Parameters
    ----------
    now      : float epoch seconds (injected clock).
    healthy  : zero-arg callable -> bool. The gateway health probe. NOT called at all
               inside the maintenance window (verified by tests).
    state    : dict with keys down_since (float|None), restarts (list[float]),
               alerted (bool). Missing keys are defaulted.
    kill_fn  : zero-arg callable that kills all IB gateway processes (returns killed
               PIDs; the return value is logged but not otherwise used).
    launch_fn: zero-arg callable -> bool. Brings up exactly ONE fresh gateway
               (ibkr.ensure_gateway). True == it came up.
    log_fn   : one-arg callable(str) for a human log line.
    """
    # Normalize incoming state so a partial/garbage file can't crash policy.
    down_since = state.get("down_since")
    restarts = list(state.get("restarts") or [])
    alerted = bool(state.get("alerted"))

    def _persist(action):
        return ({"down_since": down_since,
                 "restarts": restarts,
                 "alerted": alerted}, action)

    # 1. Maintenance window -> do NOTHING (no health probe, no restart). The gateway
    #    bounces itself at AutoRestartTime; a "down" reading here is expected.
    if _in_maintenance_window(now):
        log_fn("maintenance window, skipping (no health check, no restart)")
        return _persist("maintenance")

    # 2. Probe health.
    is_up = bool(healthy())

    # Always prune the rolling-hour restart list so the rate limit is a true window.
    restarts = _prune_restarts(restarts, now)

    # 3. Healthy -> clear the down timer and any alert; keep the (pruned) restart
    #    list for the rolling limit.
    if is_up:
        if down_since is not None or alerted:
            log_fn("healthy (recovered) — cleared down timer and alert")
        else:
            log_fn("healthy")
        down_since = None
        alerted = False
        return _persist("healthy")

    # 4. Not healthy.
    if down_since is None:
        down_since = now
        log_fn("gateway down; grace timer started")
        return _persist("grace_started")

    down_for = now - down_since
    if down_for < GRACE_SECS:
        log_fn(f"down {int(down_for)}s, within grace ({GRACE_SECS}s)")
        return _persist("within_grace")

    # Down >= GRACE_SECS -> WEDGED. Enforce the rolling-hour restart limit.
    if len(restarts) >= MAX_RESTARTS_PER_HOUR:
        # A wedge that survived MAX_RESTARTS_PER_HOUR fresh restarts is IBKR-side
        # (e.g. a locked session that a human must clear). Do NOT restart again.
        if not alerted:
            log_fn(
                f"ALERT: gateway wedged — survived {len(restarts)} restarts in the "
                f"last hour (limit {MAX_RESTARTS_PER_HOUR}); NOT restarting again. "
                f"This is IBKR-side (likely a locked login) and needs a human.")
            alerted = True
        else:
            log_fn(
                f"still wedged past the {MAX_RESTARTS_PER_HOUR}/hr restart limit; "
                f"already alerted — holding (no restart).")
        return _persist("rate_limited")

    # Under the limit -> RESTART: kill the stuck gateway(s), record the attempt,
    # then bring up exactly one fresh.
    log_fn(
        f"gateway wedged (down {int(down_for)}s >= grace {GRACE_SECS}s); "
        f"force-restart {len(restarts) + 1}/{MAX_RESTARTS_PER_HOUR} this hour")
    try:
        killed = kill_fn()
        log_fn(f"killed gateway processes: {killed}")
    except Exception as e:  # noqa: BLE001 — a kill hiccup must not skip counting/relaunch
        log_fn(f"kill_fn error: {e!r} (continuing to relaunch)")
    # The attempt counts against the rolling limit whether or not it comes up.
    restarts.append(now)

    came_up = False
    try:
        came_up = bool(launch_fn())
    except Exception as e:  # noqa: BLE001 — a launch hiccup must not raise out of policy
        log_fn(f"launch_fn error: {e!r}")
        came_up = False

    if came_up:
        log_fn("fresh gateway came up — cleared down timer and alert")
        down_since = None
        alerted = False
        return _persist("restarted")

    # Did not come up: LEAVE down_since so the next cycle keeps counting toward the
    # limit. The restart still counted (appended above).
    log_fn("fresh gateway did NOT come up within launch window; "
           "down timer retained, restart counted")
    return _persist("restart_failed")


# --------------------------------------------------------------------------- #
# REAL kill wrapper (mocked in tests). Stdlib + PowerShell only, NO new pip deps.
# --------------------------------------------------------------------------- #
# We kill, for THIS Gateway instance only:
#   * java processes whose command line matches "IbcGateway" (the IB Gateway JVM
#     launched by IBController), AND
#   * cmd processes whose command line matches "StartGateway" (the launcher shell),
# ...AND (instance scoping, added when a second independent Gateway instance was
# introduced on the same box on a different port/install dir):
#   * the process is the one actually LISTENING on `port` (PRIMARY discriminator —
#     same Get-NetTCPConnection idiom already used below for the ThetaData
#     carve-out), OR
#   * its CommandLine contains `dir_substring` (SECONDARY discriminator — catches a
#     not-yet-port-bound process during a wedge/launch race window, before it's
#     listening).
# We SPARE:
#   * the ThetaData terminal (the java that owns local port 25503), and
#   * ALL python (never kill ourselves or any sibling desk process).
# Because the task runs ELEVATED it can read command lines and kill elevated java.
# Implemented via a single PowerShell Get-CimInstance Win32_Process filter piped to
# Stop-Process (mirrors the approach that worked during the incident cleanup).
#
# Defaults (port=ibkr.PAPER_PORT, dir_substring=C:\IBC) reproduce the ORIGINAL,
# pre-multi-instance behavior for the paper Gateway: every call site today
# (gateway_watchdog.main() and paperbot/gateway_arm_restart_elevated.py) invokes
# this with NO arguments and must keep doing so unchanged.
_KILL_PS_TEMPLATE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$dirSubstring = '{dir_substring}'
# PID that owns local port 25503 = the ThetaData terminal -> SPARE it.
$thetaPid = (Get-NetTCPConnection -LocalPort 25503 -State Listen).OwningProcess |
            Select-Object -Unique
# PID that owns local port {port} = THIS Gateway instance -> primary discriminator
# (mirrors the ThetaData carve-out idiom above).
$gwPid = (Get-NetTCPConnection -LocalPort {port} -State Listen).OwningProcess |
         Select-Object -Unique
$procs = Get-CimInstance Win32_Process | Where-Object {{
    $n  = $_.Name
    $cl = $_.CommandLine
    (
        ($n -eq 'java.exe' -and $cl -match 'IbcGateway') -or
        ($n -eq 'cmd.exe'  -and $cl -match 'StartGateway')
    ) -and
    (
        ($_.ProcessId -in $gwPid) -or
        ($cl -match [regex]::Escape($dirSubstring))
    ) -and
    ($_.ProcessId -notin $thetaPid) -and
    ($n -ne 'python.exe') -and ($n -ne 'pythonw.exe')
}}
$killed = @()
foreach ($p in $procs) {{
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    if ($?) {{ $killed += $p.ProcessId }}
}}
# Emit the killed PIDs as JSON on the last line for the caller to parse.
$killed | ConvertTo-Json -Compress
"""


def _ps_single_quote(s: str) -> str:
    """Escape a string for embedding in a PowerShell single-quoted literal."""
    return s.replace("'", "''")


def _kill_gateway_processes(port: int = ibkr.PAPER_PORT,
                            dir_substring: str = r"C:\IBC") -> list[int]:
    """Kill THIS Gateway instance's processes only — sparing the ThetaData
    terminal, all python, and any OTHER Gateway instance running on a different
    port/install dir (see the module comment above for the discriminator logic).

    port          : the port THIS instance's Gateway listens on (primary match).
    dir_substring : a substring of THIS instance's install dir, matched against
                    CommandLine (secondary match, for the pre-listen race window).
    Defaults (ibkr.PAPER_PORT / C:\\IBC) reproduce the original unscoped behavior
    for the paper Gateway — every call site today invokes this with NO arguments.

    Returns the list of killed PIDs. Never raises — on any failure returns []."""
    ps = _KILL_PS_TEMPLATE.format(
        port=int(port),
        dir_substring=_ps_single_quote(dir_substring),
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:  # noqa: BLE001 — the kill must never crash the watchdog
        print(f"_kill_gateway_processes: subprocess error {e!r}")
        return []
    text = (out.stdout or "").strip()
    if not text:
        return []
    last = text.splitlines()[-1].strip()
    try:
        parsed = json.loads(last)
    except (ValueError, TypeError):
        return []
    if parsed is None:
        return []
    if isinstance(parsed, int):
        return [parsed]
    if isinstance(parsed, list):
        return [int(x) for x in parsed]
    return []


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
        print(f"gateway_watchdog: could not write state ({e!r})")


# --------------------------------------------------------------------------- #
# Loud alert marker — a file next to the state, updated when we hit the rate limit.
# The heartbeat-alarm sweep (datacollector/heartbeat_alarm.py) can be extended to
# read this; for now it is a durable, human-readable breadcrumb + the ALERT log line.
# --------------------------------------------------------------------------- #
def _alert_marker_path() -> str:
    return os.path.join(os.path.dirname(STATE_FILE), "gateway_watchdog_alert.txt")


def _write_alert_marker(now: float, restarts) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        stamp = dt.datetime.fromtimestamp(now).isoformat(timespec="seconds")
        msg = (f"{stamp}  ALERT gateway wedged: survived {len(restarts)} restarts in "
               f"the last hour (limit {MAX_RESTARTS_PER_HOUR}); NOT restarting again. "
               f"IBKR-side (likely a locked login) — needs a human to clear the "
               f"'existing session detected' state.\n")
        with open(_alert_marker_path(), "w", encoding="utf-8") as f:
            f.write(msg)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# main() — thin wire-up. Never raises; always exit 0.
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  gateway_watchdog: {msg}", flush=True)


def main() -> int:
    try:
        now = time.time()
        state = _load_state()
        was_alerted = bool(state.get("alerted"))

        new_state, action = run_once(
            now=now,
            healthy=ibkr.gateway_running,
            state=state,
            kill_fn=_kill_gateway_processes,
            launch_fn=ibkr.ensure_gateway,
            log_fn=_log,
        )

        # On a fresh transition INTO the rate-limited/alerted state, write the loud
        # marker file so a human (and the heartbeat sweep) sees it out-of-band.
        if action == "rate_limited" and new_state.get("alerted") and not was_alerted:
            _write_alert_marker(now, new_state.get("restarts") or [])

        _save_state(new_state)
    except Exception as e:  # noqa: BLE001 — a transient error must never wedge the task
        _log(f"unexpected error (exiting 0 so the task keeps its cadence): {e!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
