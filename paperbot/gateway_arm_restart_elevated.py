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
"""
from __future__ import annotations

import json
import os
import sys
import time

from connections import ibkr_paper
from connections.gateway_watchdog import _kill_gateway_processes

STATE_FILE = os.environ.get(
    "TRADINGDESK_GATEWAY_ARM_RESTART_STATE",
    r"C:\TradingDesk-Local\state\paperbot\gateway_arm_restart_state.json",
)

KILL_WAIT_SECS = 30   # poll window waiting for the port to close after the kill
SETTLE_SECS = 4       # extra settle time so no zombie/dialog survives (mirrors
                       # arming.restart_gateway()'s existing settle delay)


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


def run() -> bool:
    """Kill the (elevated) Gateway process, wait for it to fully die, relaunch.
    Returns True on success. Raises on unexpected errors — caught by main()."""
    print("gateway_arm_restart_elevated: killing Gateway process (elevated kill)...")
    killed = _kill_gateway_processes()
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
    return came_up


def main() -> int:
    try:
        ok = run()
        detail = "restarted and serving data" if ok else "gateway did not come back up within wait window"
        _write_state(ok, detail)
        return 0 if ok else 1
    except Exception as e:  # noqa: BLE001 — must never crash without recording state
        _write_state(False, f"unexpected error: {e!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
