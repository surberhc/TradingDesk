r"""
gateway_watchdog.py — detect a WEDGED IB paper Gateway and recover it by killing
the stuck gateway and bringing up exactly ONE fresh — with hard rate-limiting so
it can never become a hot loop.

PAPER ONLY. This never touches order transmission; it only restarts the login/data
gateway process. `ensure_gateway()` (which this calls to relaunch) uses the proven
paper StartGatewayPaper.bat auto-login and the narrow launch-mutex.

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
    main() is a thin wire-up: real time.time(), ibkr_paper.gateway_running(), the real
    kill wrapper, ibkr_paper.ensure_gateway, load/save state, print.
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

from connections import ibkr_paper

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
               (ibkr_paper.ensure_gateway). True == it came up.
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
# GATEWAY INSTANCE (LANE) IDENTITY — the discriminator, as PURE PYTHON.
# --------------------------------------------------------------------------- #
# INCIDENT 2026-07-23 (docs/INCIDENT_2026-07-23_arm_restart_killed_live_gateway.md)
# ------------------------------------------------------------------------------
# The old secondary discriminator was `CommandLine -match 'C:\IBC'` (the paper
# install dir). `C:\IBC` is a STRING PREFIX of the sibling installs C:\IBC-Live-Data
# and C:\IBC-Live-Trade, so a zero-argument paper kill matched and DESTROYED the S8
# live-pilot Gateway on port 4003 (2m51s outage on a real funded account's pilot).
#
# The obvious "add a trailing separator" fix (`C:\IBC\`) is NOT sufficient and was
# verified insufficient against the real running processes: ALL THREE lanes launch
# IBC from the SHARED classpath entry `C:\IBC\IBC.jar` (IBC_PATH=%SYSTEMDRIVE%\IBC in
# every StartGateway*.bat), so EVERY Gateway JVM's command line contains the literal
# `C:\IBC\`. That fix would spare the cmd.exe launchers and still kill sibling JVMs.
#
# THE ONLY THING THAT IS ACTUALLY PER-INSTANCE is the IBC config file and the
# launcher .bat:
#     paper       C:\IBC\config.ini                 C:\IBC\StartGateway.bat
#     live-data   C:\IBC-Live-Data\config.ini       C:\IBC-Live-Data\StartGatewayLiveData.bat
#     live-trade  C:\IBC-Live-Trade\config.ini      C:\IBC-Live-Trade\StartGatewayLiveTrade.bat
# None of those strings is a substring of another. The JVM carries its config path as
# the trailing argument to ibcalpha.ibc.IbcGateway (and its settings dir as
# -DjtsConfigDir); the cmd.exe launcher shell carries the .bat path.
#
# We kill, for THIS Gateway instance only:
#   * java processes whose command line matches "IbcGateway" (the IB Gateway JVM
#     launched by IBController), or cmd processes whose command line matches
#     "StartGateway" (the launcher shell), AND
#   * the process is the one actually LISTENING on `port` (PRIMARY discriminator —
#     same Get-NetTCPConnection idiom used for the ThetaData carve-out), OR its
#     CommandLine carries one of THIS instance's exact identity markers (SECONDARY
#     discriminator — catches a not-yet-port-bound process during a wedge/launch
#     race window, before it is listening).
# We SPARE, unconditionally:
#   * any process carrying ANOTHER lane's identity marker (the HARD NEVER-KILL
#     guard — it overrides even the port match; see _foreign_instance_marker),
#   * the ThetaData terminal (the java that owns local port 25503), and
#   * ALL python (never kill ourselves or any sibling desk process).
# Because the task runs ELEVATED it can read command lines and kill elevated java.
#
# The process enumeration happens in PowerShell (Get-CimInstance Win32_Process, no
# new pip deps); the DECISION is made here in Python by `should_kill()` so it is
# directly unit-testable against real captured command lines — the old all-in-one
# PowerShell filter could only be tested by asserting on the generated script text,
# which is exactly why the prefix collision was invisible to the suite.

# The SHARED IBC program directory. All three lanes set IBC_PATH=%SYSTEMDRIVE%\IBC
# and put C:\IBC\IBC.jar on the classpath, so this path identifies NOTHING about
# which lane a process belongs to and must never be used as a discriminator.
IBC_PROGRAM_DIR = r"C:\IBC"


class GatewayInstance:
    """One Gateway lane's identity — the exact strings that distinguish it from
    every other Gateway instance on this box.

    install_dir   : the lane's own IBC instance directory.
    config_path   : the lane's own config.ini (the JVM's trailing IbcGateway arg).
    launcher_path : the lane's own StartGateway*.bat (the cmd.exe launcher's arg).
    """
    __slots__ = ("name", "port", "install_dir", "config_path", "launcher_path")

    def __init__(self, name: str, port: int, install_dir: str,
                 config_path: str, launcher_path: str):
        self.name = name
        self.port = int(port)
        self.install_dir = install_dir
        self.config_path = config_path
        self.launcher_path = launcher_path

    def __repr__(self) -> str:  # pragma: no cover — debugging aid only
        return f"GatewayInstance({self.name!r}, port={self.port})"

    def identity_markers(self) -> tuple[str, ...]:
        """Strings whose presence in a command line PROVES the process belongs to
        this lane. Exact per-instance paths only — never a bare install root."""
        return (self.config_path, self.launcher_path)

    def exclusion_markers(self) -> tuple[str, ...]:
        """Strings whose presence proves the process belongs to this lane, used to
        EXCLUDE it when some other lane is the kill target. Adds the install root
        (with a trailing separator) for lanes that own their own directory — but
        NOT for the shared IBC program dir, which every lane's JVM references."""
        markers = list(self.identity_markers())
        if self.install_dir.rstrip("\\").lower() != IBC_PROGRAM_DIR.lower():
            markers.append(self.install_dir.rstrip("\\") + "\\")
        return tuple(markers)


PAPER_INSTANCE = GatewayInstance(
    "paper", ibkr_paper.PAPER_PORT, r"C:\IBC",
    r"C:\IBC\config.ini", r"C:\IBC\StartGateway.bat")
LIVE_DATA_INSTANCE = GatewayInstance(
    "live-data", 4001, r"C:\IBC-Live-Data",
    r"C:\IBC-Live-Data\config.ini", r"C:\IBC-Live-Data\StartGatewayLiveData.bat")
LIVE_TRADE_INSTANCE = GatewayInstance(
    "live-trade", 4003, r"C:\IBC-Live-Trade",
    r"C:\IBC-Live-Trade\config.ini", r"C:\IBC-Live-Trade\StartGatewayLiveTrade.bat")

KNOWN_INSTANCES: tuple[GatewayInstance, ...] = (
    PAPER_INSTANCE, LIVE_DATA_INSTANCE, LIVE_TRADE_INSTANCE)


def _norm(cl) -> str:
    """Normalize a command line for matching: str, lowercased. Windows paths are
    case-insensitive, and Win32_Process reports the JVM path in lowercase."""
    return (cl or "").lower()


def matches_instance(command_line, instance: GatewayInstance) -> bool:
    """True if `command_line` carries one of `instance`'s EXACT identity markers.

    This is the secondary (pre-port-bind) discriminator. It is deliberately exact:
    no bare install-root substring test, because C:\\IBC is a prefix of the sibling
    installs AND is on every lane's classpath (see the incident note above)."""
    cl = _norm(command_line)
    return any(_norm(m) in cl for m in instance.identity_markers())


def foreign_instance_marker(command_line, instance: GatewayInstance,
                            known=KNOWN_INSTANCES):
    """HARD NEVER-KILL guard.

    Returns the (lane_name, marker) of a DIFFERENT lane whose identity marker is
    present in `command_line`, else None. A process that trips this is excluded
    from the kill no matter what else matched — including the port discriminator.
    Belt and braces: the 2026-07-23 incident proves one discriminator failing open
    is enough to lose a live Gateway."""
    cl = _norm(command_line)
    for other in known:
        if other.name == instance.name:
            continue
        for marker in other.exclusion_markers():
            if _norm(marker) in cl:
                return (other.name, marker)
    return None


def should_kill(*, name: str, command_line, pid: int,
                gw_pids, theta_pids, instance: GatewayInstance,
                known=KNOWN_INSTANCES) -> bool:
    """The whole kill predicate, as a pure function (see the module comment).

    name        : process image name (e.g. 'java.exe').
    command_line: the process's full CommandLine (may be None).
    pid         : the process id.
    gw_pids     : pids currently LISTENING on this instance's port (primary match).
    theta_pids  : pids owning port 25503 (the ThetaData terminal) — always spared.
    instance    : the lane being restarted.
    """
    nm = (name or "").lower()
    cl = _norm(command_line)
    pid = int(pid)
    gw_pids = {int(p) for p in (gw_pids or [])}
    theta_pids = {int(p) for p in (theta_pids or [])}

    # Never kill ourselves or any sibling desk process.
    if nm in ("python.exe", "pythonw.exe"):
        return False
    # Never kill the ThetaData terminal.
    if pid in theta_pids:
        return False
    # Must look like a Gateway process at all.
    is_gateway_proc = ((nm == "java.exe" and "ibcgateway" in cl) or
                       (nm == "cmd.exe" and "startgateway" in cl))
    if not is_gateway_proc:
        return False
    # HARD NEVER-KILL: another lane's process, whatever else matched.
    if foreign_instance_marker(command_line, instance, known) is not None:
        return False
    # Primary: it owns this instance's port. Secondary: exact instance markers.
    return (pid in gw_pids) or matches_instance(command_line, instance)


# --------------------------------------------------------------------------- #
# REAL kill wrapper (mocked in tests). Stdlib + PowerShell only, NO new pip deps.
# Phase 1 ENUMERATES (PowerShell), Python DECIDES, phase 2 KILLS the chosen pids.
# --------------------------------------------------------------------------- #
_ENUM_PS_TEMPLATE = r"""
$ErrorActionPreference = 'SilentlyContinue'
# PID that owns local port 25503 = the ThetaData terminal -> SPARE it.
$thetaPid = @((Get-NetTCPConnection -LocalPort 25503 -State Listen).OwningProcess |
              Select-Object -Unique)
# PID that owns local port {port} = THIS Gateway instance -> primary discriminator
# (mirrors the ThetaData carve-out idiom above).
$gwPid = @((Get-NetTCPConnection -LocalPort {port} -State Listen).OwningProcess |
           Select-Object -Unique)
$procs = @(Get-CimInstance Win32_Process | Where-Object {{
    ($_.Name -eq 'java.exe' -and $_.CommandLine -match 'IbcGateway') -or
    ($_.Name -eq 'cmd.exe'  -and $_.CommandLine -match 'StartGateway')
}} | ForEach-Object {{
    [pscustomobject]@{{ pid = $_.ProcessId; name = $_.Name; cl = $_.CommandLine }}
}})
[pscustomobject]@{{ theta = $thetaPid; gw = $gwPid; procs = $procs }} |
    ConvertTo-Json -Depth 4 -Compress
"""

_KILL_PS_TEMPLATE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$killed = @()
foreach ($id in @({pids})) {{
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    if ($?) {{ $killed += $id }}
}}
# Emit the killed PIDs as JSON on the last line for the caller to parse.
$killed | ConvertTo-Json -Compress
"""


def _ps_single_quote(s: str) -> str:
    """Escape a string for embedding in a PowerShell single-quoted literal."""
    return s.replace("'", "''")


def _run_ps(script: str, timeout: int = 60):
    """Run a PowerShell script, return stdout text ('' on any failure)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001 — the kill must never crash the watchdog
        print(f"_run_ps: subprocess error {e!r}")
        return ""
    return (out.stdout or "").strip()


def _parse_json_last_line(text: str):
    """Parse the LAST line of `text` as JSON; None on any failure."""
    if not text:
        return None
    last = text.splitlines()[-1].strip()
    try:
        return json.loads(last)
    except (ValueError, TypeError):
        return None


def _as_list(v) -> list:
    """PowerShell's ConvertTo-Json collapses single-element arrays; normalize."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _kill_gateway_processes(port: int = ibkr_paper.PAPER_PORT,
                            instance: GatewayInstance = PAPER_INSTANCE) -> list[int]:
    r"""Kill THIS Gateway instance's processes ONLY.

    Spares, unconditionally: the ThetaData terminal (port 25503), all python, and —
    this is the 2026-07-23 fix — ANY process carrying another lane's identity marker
    (C:\IBC-Live-Data\* / C:\IBC-Live-Trade\* / C:\IBC\config.ini as appropriate).
    See docs/INCIDENT_2026-07-23_arm_restart_killed_live_gateway.md.

    port     : the port THIS instance's Gateway listens on (PRIMARY discriminator —
               the pid that owns it). This discriminator was always correct.
    instance : the lane's GatewayInstance — supplies the EXACT per-instance config
               and launcher paths used as the secondary (pre-port-bind)
               discriminator and, for every OTHER lane, as a hard never-kill guard.
               Formerly `dir_substring`, a bare install-root substring; that name and
               semantics are gone because C:\IBC is a prefix of the sibling installs
               AND appears on every lane's classpath, so it identified nothing.

    `port` and `instance.port` must agree; a mismatch is a wiring bug and we refuse
    to kill anything rather than guess (a refused kill leaves a wedged gateway,
    which is loud and recoverable; a wrong kill is not).

    Returns the list of killed PIDs. Never raises — on any failure returns []."""
    port = int(port)
    if port != instance.port:
        print(f"_kill_gateway_processes: REFUSING — port {port} does not match "
              f"instance {instance.name!r} (port {instance.port}). Killing nothing.")
        return []

    info = _parse_json_last_line(_run_ps(_ENUM_PS_TEMPLATE.format(port=port)))
    if not isinstance(info, dict):
        return []
    theta_pids = _as_list(info.get("theta"))
    gw_pids = _as_list(info.get("gw"))
    procs = _as_list(info.get("procs"))

    targets: list[int] = []
    for p in procs:
        if not isinstance(p, dict):
            continue
        try:
            pid = int(p.get("pid"))
        except (TypeError, ValueError):
            continue
        cl = p.get("cl")
        if should_kill(name=p.get("name") or "", command_line=cl, pid=pid,
                       gw_pids=gw_pids, theta_pids=theta_pids, instance=instance):
            targets.append(pid)
        else:
            foreign = foreign_instance_marker(cl, instance)
            if foreign is not None:
                print(f"_kill_gateway_processes: SPARING pid {pid} ({p.get('name')}) "
                      f"— belongs to lane {foreign[0]!r} (marker {foreign[1]!r})")

    if not targets:
        return []

    pids_literal = ",".join(str(t) for t in targets)
    parsed = _parse_json_last_line(
        _run_ps(_KILL_PS_TEMPLATE.format(pids=pids_literal)))
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
            healthy=ibkr_paper.gateway_running,
            state=state,
            # EXPLICIT lane scoping — never rely on the defaults (2026-07-23 incident:
            # a defaults-only call from the arm path killed the live-trade Gateway).
            kill_fn=lambda: _kill_gateway_processes(
                port=PAPER_INSTANCE.port, instance=PAPER_INSTANCE),
            launch_fn=ibkr_paper.ensure_gateway,
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
