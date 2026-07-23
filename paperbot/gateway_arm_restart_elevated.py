r"""
gateway_arm_restart_elevated.py — the ELEVATED half of the arm/disarm Gateway
restart. PAPER ONLY (port 4002). This never transmits an order; it only
kills-and-relaunches the login/data Gateway process.

WHY THIS EXISTS
----------------
`paperbot.arming.arm()`/`disarm()` flip the Gateway's "Read-Only API" checkbox by
editing C:\IBC\config.ini and clean-restarting the Gateway so IBC can drive the GUI
toggle. The restart used to go through `arming.stop_gateway()`, which runs
`Stop-Process` against the Gateway's PID from a NON-ELEVATED PowerShell context.

The IB Gateway process runs ELEVATED (see connections\connections\gateway_watchdog.py's
docstring — the same elevation gap caused the 2026-07-05 gateway-pileup incident and
was fixed there by registering gateway_watchdog's OWN scheduled task with
RunLevel=Highest). A non-elevated kill against an elevated process silently no-ops:
Stop-Process reports no error, but the old Gateway process never actually dies, so IBC
never gets a clean GUI to relaunch into and the Read-Only API toggle never commits.
Confirmed directly: `arming.py arm` failed twice in a row with "port still looked open
after stop" followed by "ARM FAILED: Gateway is STILL Read-Only after restart."

THE FIX: run the kill+relaunch from an ELEVATED, on-demand Windows Scheduled Task
("GatewayArmRestart", RunLevel=Highest) so this script inherits admin rights and can
actually kill the elevated Gateway process. It reuses gateway_watchdog's
`_kill_gateway_processes()` — the SAME kill routine gateway_watchdog's own elevated
task already uses successfully every 5 minutes (spares the ThetaData terminal on port
25503 and all python processes; kills only the Gateway's java/IbcGateway and
cmd/StartGateway processes).

`paperbot.arming.restart_gateway()` is the caller: it triggers this task via
`schtasks /run /tn GatewayArmRestart` and polls the completion-state file this script
writes (see STATE_FILE below) for a fresh ok/fail result, rather than calling any of
this elevation-dependent logic directly from its own non-elevated process.

This script never raises out of main(): any exception is caught, written to the state
file as a failure, and reported via a non-zero exit code — a hiccup here must never
leave the scheduled task itself in a broken state or hang the caller's poll loop.

2026-07-23 INCIDENT — WHY THE KILL IS NOW EXPLICITLY LANE-SCOPED AND POST-CHECKED
---------------------------------------------------------------------------------
This script used to call `_kill_gateway_processes()` with NO arguments. Its default
secondary discriminator was the bare substring `C:\IBC`, which is a string PREFIX of
the sibling installs `C:\IBC-Live-Data` and `C:\IBC-Live-Trade`. On 2026-07-23 a
routine `arming.py arm` therefore killed the S8 live-pilot Gateway on port 4003 (a
REAL funded account's pilot, 2m51s blind) — and then wrote
`{"ok": true, "detail": "restarted and serving data"}`, because it only ever checked
that the PAPER gateway came back. Two changes here:
  * the kill is called with an EXPLICIT port + GatewayInstance (never the defaults), and
  * the other lanes' listeners are snapshotted BEFORE the kill and re-checked AFTER
    the relaunch; losing one is a hard failure written into the state file with a
    non-zero exit, so a success record can never again hide collateral damage.
See docs/INCIDENT_2026-07-23_arm_restart_killed_live_gateway.md and conductor #46.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from connections import ibkr_paper
from connections.gateway_watchdog import (
    PAPER_INSTANCE,
    _kill_gateway_processes,
)

STATE_FILE = os.environ.get(
    "TRADINGDESK_GATEWAY_ARM_RESTART_STATE",
    r"C:\TradingDesk-Local\state\paperbot\gateway_arm_restart_state.json",
)

KILL_WAIT_SECS = 30   # poll window waiting for the port to close after the kill
SETTLE_SECS = 4       # extra settle time so no zombie/dialog survives (mirrors
                       # arming.restart_gateway()'s existing settle delay)

# The OTHER lanes this paper-only restart must never disturb (connections/GATEWAYS.md).
# 2026-07-23 INCIDENT: this script's kill destroyed the S8 live-pilot Gateway on 4003
# and then wrote {"ok": true, "detail": "restarted and serving data"} — the success
# record was actively misleading. We now snapshot these lanes' listeners BEFORE the
# kill and re-check AFTER the relaunch; any lane that WAS listening and is no longer
# is a hard failure (recorded in the state file, non-zero exit).
# See docs/INCIDENT_2026-07-23_arm_restart_killed_live_gateway.md.
OTHER_LANE_PORTS = {4001: "live-data", 4003: "live-trade"}


def _listening_ports(ports) -> set[int]:
    """Which of `ports` currently have a LISTEN socket. Read-only; never connects
    (a probe connect to an IB API port would open a real API session)."""
    ports = sorted(int(p) for p in ports)
    if not ports or os.name != "nt":
        return set()
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"@({','.join(str(p) for p in ports)}) | ForEach-Object {{"
        " if ((Get-NetTCPConnection -LocalPort $_ -State Listen)) { $_ } }"
        " | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:  # noqa: BLE001 — an unreadable probe must not crash the arm
        print(f"gateway_arm_restart_elevated: listener probe failed: {e!r}")
        return set()
    text = (out.stdout or "").strip()
    if not text:
        return set()
    try:
        parsed = json.loads(text.splitlines()[-1].strip())
    except (ValueError, TypeError):
        return set()
    if parsed is None:
        return set()
    if isinstance(parsed, int):
        return {parsed}
    if isinstance(parsed, list):
        return {int(x) for x in parsed}
    return set()


def _write_state(ok: bool, detail: str) -> None:
    state = {"ts": time.time(), "ok": bool(ok), "detail": detail}
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, indent=2))
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        # Best-effort: if we can't even write the state file, print so the task's
        # captured output (if any) still shows the failure.
        print(f"gateway_arm_restart_elevated: could not write state file: {e!r}")


def collateral_damage(before: set[int], after: set[int]) -> list[str]:
    """Pure: lanes that WERE listening before the kill and are NOT listening after.
    Returns human-readable 'name (port)' strings, empty when nothing was lost."""
    lost = sorted(set(before) - set(after))
    return [f"{OTHER_LANE_PORTS.get(p, 'unknown')} ({p})" for p in lost]


def run() -> tuple[bool, str]:
    """Kill the (elevated) PAPER Gateway process, wait for it to die, relaunch, and
    verify no OTHER lane's Gateway was taken down with it.
    Returns (ok, detail). Raises on unexpected errors — caught by main()."""
    # BEFORE snapshot — taken first so the post-check is a real before/after compare.
    before = _listening_ports(OTHER_LANE_PORTS)
    print("gateway_arm_restart_elevated: other-lane listeners BEFORE kill: "
          f"{sorted(before) or 'none'}")

    print("gateway_arm_restart_elevated: killing Gateway process (elevated kill), "
          f"scoped to lane {PAPER_INSTANCE.name!r} / port {PAPER_INSTANCE.port}...")
    # EXPLICIT lane scoping — never the bare defaults (2026-07-23 incident).
    killed = _kill_gateway_processes(port=PAPER_INSTANCE.port,
                                     instance=PAPER_INSTANCE)
    print(f"gateway_arm_restart_elevated: killed PIDs: {killed}")

    deadline = time.time() + KILL_WAIT_SECS
    while time.time() < deadline:
        if not ibkr_paper.gateway_running():
            break
        time.sleep(1)
    else:
        print("gateway_arm_restart_elevated: WARN — gateway still looked up after "
              f"{KILL_WAIT_SECS}s wait; proceeding to relaunch anyway.")

    time.sleep(SETTLE_SECS)  # let the old session fully die before IBC relaunches

    print("gateway_arm_restart_elevated: relaunching via ensure_gateway()...")
    came_up = bool(ibkr_paper.ensure_gateway())
    print(f"gateway_arm_restart_elevated: ensure_gateway() -> {came_up}")

    # AFTER check — a paper restart must never cost another lane its Gateway.
    after = _listening_ports(OTHER_LANE_PORTS)
    print("gateway_arm_restart_elevated: other-lane listeners AFTER relaunch: "
          f"{sorted(after) or 'none'}")
    lost = collateral_damage(before, after)
    if lost:
        detail = ("COLLATERAL DAMAGE — this paper restart took down another lane's "
                  f"Gateway: {', '.join(lost)} was LISTENING before the kill and is "
                  "NOT listening now. Bring it back up immediately (live-trade: "
                  "scheduled task LiveTradeGatewayOpen_0815CT). See "
                  "docs/INCIDENT_2026-07-23_arm_restart_killed_live_gateway.md.")
        print(f"gateway_arm_restart_elevated: {detail}")
        # Fail LOUDLY even if the paper gateway itself came up fine.
        return False, detail

    if not came_up:
        return False, "gateway did not come back up within wait window"
    return True, "restarted and serving data"


def main() -> int:
    try:
        ok, detail = run()
        _write_state(ok, detail)
        return 0 if ok else 1
    except Exception as e:  # noqa: BLE001 — must never crash without recording state
        _write_state(False, f"unexpected error: {e!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
