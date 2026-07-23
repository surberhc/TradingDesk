r"""
gateway_watchdog_live.py — detect a WEDGED live-data IB Gateway and recover it by
killing the stuck gateway and bringing up exactly ONE fresh — with hard
rate-limiting so it can never become a hot loop.

This is the sibling of gateway_watchdog.py for the SECOND, independent Gateway
instance: the LIVE-side, READ-ONLY-ONLY market-data Gateway defined in
ibkr_live_data.py (distinct install dir C:\IBC-Live-Data, distinct port 4001,
distinct launch-mutex, distinct login). It never touches order transmission —
ibkr_live_data.py has no writable connect path at all (see its module docstring)
— so this watchdog restarting the gateway process carries the same paper-only-
style safety property one level further: even the live-data side it manages
cannot place an order.

REUSE, NOT DUPLICATION, WHERE CLEAN
  * `_kill_gateway_processes` (the PowerShell kill wrapper) is IMPORTED from
    gateway_watchdog.py and called scoped to this instance
    (port=LIVE_DATA_INSTANCE.port, instance=LIVE_DATA_INSTANCE) — the function was
    built with this exact second-instance scoping in mind; no PowerShell logic is
    duplicated here. (2026-07-23: the old `dir_substring=r"C:\IBC-Live-Data"` form
    was safe for THIS lane but the parameter itself was unsafe by design — see
    docs/INCIDENT_2026-07-23_arm_restart_killed_live_gateway.md — and is now an
    exact per-instance GatewayInstance identity instead of a bare substring.)
  * `_in_maintenance_window` and `_prune_restarts` (small pure helpers with no
    hardcoded coupling to the paper module's globals) are IMPORTED and reused.
  * The top-level `run_once(...)` orchestration in gateway_watchdog.py is NOT
    imported: it calls `_in_maintenance_window(now)` with no `window=` override,
    so its maintenance check is permanently bound to the PAPER gateway's
    MAINTENANCE_WINDOW_ET default at that function's definition time. Reusing it
    as-is would silently apply the paper Gateway's nightly-reset window to this,
    unrelated, not-yet-installed live-data instance. Rather than monkeypatch
    another module's baked-in default (fragile, surprising), run_once() below
    mirrors gateway_watchdog.run_once()'s structure exactly, substituting this
    file's own MAINTENANCE_WINDOW_ET (see TODO below) — everything else
    (grace timer, rolling-hour restart cap, alert-once behavior) is identical.

WHY THIS EXISTS (2026-07-10)
  The live-data Gateway (ibkr_live_data.py) is a second, independent IBC-managed
  Gateway process on the same box. It can wedge exactly like the paper Gateway
  can (same one-login-per-username auth-hang failure mode), so it needs the same
  kill-and-relaunch watchdog — scoped to its own port/install dir so it never
  touches the paper Gateway (or vice versa).

  The gateways run ELEVATED. A non-elevated process cannot kill them (taskkill
  Access Denied) and cannot even read their command lines. THEREFORE this
  watchdog's scheduled task MUST run elevated (highest privileges), same as
  gateway_watchdog.py's.

DESIGN
  * ONE check per invocation. The Windows scheduler provides the cadence;
    once-per-invocation is reboot/crash-resilient (no long-lived loop to die).
  * The DECISION LOGIC is a pure, injectable function — run_once(...) — with a
    fake clock and mocked health/kill/launch so the whole policy is unit-tested
    offline. main() is a thin wire-up: real time.time(),
    ibkr_live_data.gateway_running(), the real (imported, instance-scoped) kill
    wrapper, ibkr_live_data.ensure_gateway, load/save state, print.
  * State is LOCAL C: only (never Drive — Drive sync corrupts atomicity and the
    file must be readable/writable by the elevated task). Path overridable via
    its own env var so tests point it at a tmp dir — distinct from the paper
    module's env var and file, so the two watchdogs' state can never collide.
  * main() NEVER raises: a transient error is caught, logged, and we exit 0 so a
    hiccup can never wedge the scheduled task itself.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time

from connections import ibkr_live_data
from connections.gateway_watchdog import (
    LIVE_DATA_INSTANCE,
    _in_maintenance_window,
    _kill_gateway_processes,
    _prune_restarts,
)

# --------------------------------------------------------------------------- #
# TUNABLE POLICY — mirrors gateway_watchdog.py's defaults. These are the whole
# policy surface for this instance.
# --------------------------------------------------------------------------- #
CHECK_INTERVAL_MIN = 5          # informational; the SCHEDULER enforces the cadence
GRACE_SECS = 300               # gateway must be continuously down this long before we force-restart
MAX_RESTARTS_PER_HOUR = 3      # after this many restarts in a rolling hour, STOP and alert

# TODO(live-data-gateway-setup): PLACEHOLDER — the live-data Gateway instance
# (C:\IBC-Live-Data) does not exist yet, so its config.ini AutoRestartTime is
# UNKNOWN. This value is a structural placeholder only, copied from the paper
# default's format so the maintenance-window plumbing has something to point
# at — it is NOT a confirmed nightly-reset time for this instance. UPDATE THIS
# once the user builds the second IBC instance and sets its own AutoRestartTime
# in C:\IBC-Live-Data\config.ini; until then a "down" reading in this window will be
# (incorrectly) treated as an expected nightly bounce rather than a wedge.
MAINTENANCE_WINDOW_ET = ("23:45", "00:45")  # PLACEHOLDER — see TODO above

# --------------------------------------------------------------------------- #
# State file — LOCAL only, never Drive. Overridable via env so tests point it
# at tmp. Distinct path AND distinct env var from the paper module's state, so
# the two watchdogs never collide.
#   down_since : epoch float when the gateway was FIRST seen down (or null)
#   restarts   : list of epoch floats, one per force-restart, pruned to the last hour
#   alerted    : bool — have we already fired the loud "wedge survived N restarts" alert
# --------------------------------------------------------------------------- #
STATE_FILE = os.environ.get(
    "TRADINGDESK_LIVE_DATA_GATEWAY_WATCHDOG_STATE",
    r"C:\TradingDesk-Local\state\live_data\gateway_watchdog_state.json",
)


# --------------------------------------------------------------------------- #
# PURE DECISION LOGIC — fully injectable; tests drive this with a fake clock and
# mocked healthy/kill_fn/launch_fn/log_fn. Returns (new_state, action_taken).
#
# Structurally identical to gateway_watchdog.run_once(); the only substantive
# difference is the maintenance-window check uses THIS file's
# MAINTENANCE_WINDOW_ET rather than the paper module's (see module docstring).
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
               (ibkr_live_data.ensure_gateway). True == it came up.
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

    # 1. Maintenance window -> do NOTHING (no health probe, no restart). Uses
    #    THIS file's MAINTENANCE_WINDOW_ET (placeholder — see TODO above), not
    #    the paper module's.
    if _in_maintenance_window(now, window=MAINTENANCE_WINDOW_ET):
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
# State load/save — LOCAL C: only. Never raises. Own STATE_FILE (see above);
# structurally identical to gateway_watchdog.py's but pointed at this instance's
# own path so the two watchdogs' state can never collide.
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
        print(f"gateway_watchdog_live: could not write state ({e!r})")


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
        msg = (f"{stamp}  ALERT live-data gateway wedged: survived {len(restarts)} "
               f"restarts in the last hour (limit {MAX_RESTARTS_PER_HOUR}); NOT "
               f"restarting again. IBKR-side (likely a locked login) — needs a "
               f"human to clear the 'existing session detected' state.\n")
        with open(_alert_marker_path(), "w", encoding="utf-8") as f:
            f.write(msg)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# main() — thin wire-up. Never raises; always exit 0.
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  gateway_watchdog_live: {msg}", flush=True)


def main() -> int:
    try:
        now = time.time()
        state = _load_state()
        was_alerted = bool(state.get("alerted"))

        new_state, action = run_once(
            now=now,
            healthy=ibkr_live_data.gateway_running,
            state=state,
            kill_fn=lambda: _kill_gateway_processes(
                port=LIVE_DATA_INSTANCE.port, instance=LIVE_DATA_INSTANCE),
            launch_fn=ibkr_live_data.ensure_gateway,
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
