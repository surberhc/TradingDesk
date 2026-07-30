"""
arming.py — AUTOMATED, SELF-VERIFYING arm/disarm of the paper Gateway for order
transmission. PAPER ONLY (paper login, port 4002). Nothing here transmits an order.

WHY THIS IS MORE THAN A ONE-LINE CONFIG EDIT
--------------------------------------------
Order transmission is gated at the Gateway by the "Read-Only API" checkbox. That
checkbox's true state lives in the Gateway's OWN encrypted per-user settings file
(C:\\Jts\\ibgateway\\1045\\<profile>\\ibg.xml — an `IBGZENC` blob we cannot read or
write), NOT in C:\\IBC\\config.ini. IBC's job on each launch is to DRIVE THE GUI to
make that checkbox match `ReadOnlyApi` in config.ini.

The failure we hit live: setting `ReadOnlyApi=no` in config.ini was NOT enough — when
the Gateway toggles the Read-Only API checkbox it raises a confirmation dialog, and if
IBC doesn't get a clean GUI to drive (zombie session / leftover dialog from an unclean
restart), the toggle silently never commits. The user then saw a warning telling them
to uncheck the box by hand. Evidence: `ibg.xml`'s mtime never advanced across the
armed runs, proving the live checkbox state never changed.

THE FIX (defense in depth):
  1. Still drive the toggle through config.ini (the correct, IBC-supported mechanism).
  2. HARDEN the restart so IBC always gets a clean GUI: fully kill the old Gateway,
     wait until the process is gone AND port 4002 is closed, settle, THEN relaunch
     (with java_version=17, see connections.ibkr_paper). No zombie => no stranded dialog.
  3. VERIFY the toggle actually took effect by probing the live API's read-only state
     with a ZERO-TRANSMISSION primitive (a cancel of a non-existent orderId — see
     `probe_api_readonly`). Raise loudly if it didn't take, instead of proceeding.

THE ELEVATION GAP (found 2026-07-07)
-------------------------------------
Step 2's kill (`stop_gateway()`, still defined below) runs `Stop-Process` from a
NON-ELEVATED PowerShell context. The IB Gateway process runs ELEVATED (see
connections\\connections\\gateway_watchdog.py's docstring — the SAME elevation gap
caused the 2026-07-05 gateway-pileup incident and was fixed there by giving the
watchdog's OWN scheduled task RunLevel=Highest). A non-elevated kill against an
elevated process silently no-ops: no error, but the old Gateway never actually dies,
so IBC never gets the clean GUI step 2 promises and the toggle never commits.
Confirmed directly: `arming.py arm` failed twice in a row with "port still looked
open after stop" then "ARM FAILED: Gateway is STILL Read-Only after restart."

THE FIX'S SECOND LAYER: `restart_gateway()` no longer runs the kill itself. It
triggers the on-demand, elevated `GatewayArmRestart` scheduled task (RunLevel=Highest,
registered separately — see `gateway_arm_restart_elevated.py`), which reuses
gateway_watchdog's proven `_kill_gateway_processes()` kill routine and then relaunches.
`restart_gateway()` polls that task's JSON completion-state file for a fresh result
and returns True/False accordingly — same `-> bool` contract as before, so `arm()`/
`disarm()` did not need to change. `stop_gateway()` is kept in this file (still
useful/importable, e.g. for tests) but is no longer part of the restart path.

ZERO-TRANSMISSION GUARANTEE
---------------------------
The verification never places an order. It calls `ib.cancelOrder` on a fabricated,
never-placed orderId. The Gateway's response distinguishes the two states without any
order ever reaching the market:
  * Read-Only API  -> rejection whose message contains "Read-Only" (code ~2148).
  * Write-enabled  -> "OrderId ... not found" (code 10147/10148). Nothing transmits;
    there is no such order to cancel.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

from ib_async import IB

from connections import clientids, gateway_probe, ibkr_paper

CONFIG_INI = r"C:\IBC\config.ini"
_VERIFY_CLIENT_ID = clientids.get("paperbot_arm_verify")  # 39

# Elevated restart task (see gateway_arm_restart_elevated.py + THE ELEVATION GAP
# above). On-demand only — no schedule; triggered here via `schtasks /run`.
ARM_RESTART_TASK = "GatewayArmRestart"
ARM_RESTART_STATE_FILE = os.environ.get(
    "TRADINGDESK_GATEWAY_ARM_RESTART_STATE",
    r"C:\TradingDesk-Local\state\paperbot\gateway_arm_restart_state.json",
)
ARM_RESTART_POLL_TIMEOUT = 60  # seconds to wait for a fresh state-file result


# --------------------------------------------------------------------------- #
# config.ini toggle (the IBC-driven mechanism)
# --------------------------------------------------------------------------- #
def read_readonly_api() -> str | None:
    """Current ReadOnlyApi value ('yes'/'no'), or None if the line is absent."""
    with open(CONFIG_INI, "r", encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"\s*ReadOnlyApi\s*=\s*(\w+)", line)
            if m:
                return m.group(1).lower()
    return None


def set_readonly_api(readonly: bool) -> str:
    """Set ReadOnlyApi=yes/no in config.ini (one line). Returns the new value."""
    target = "yes" if readonly else "no"
    with open(CONFIG_INI, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    changed = False
    for i, line in enumerate(lines):
        if re.match(r"\s*ReadOnlyApi\s*=", line):
            lines[i] = f"ReadOnlyApi={target}\n"
            changed = True
            break
    if not changed:
        raise RuntimeError("ReadOnlyApi line not found in C:\\IBC\\config.ini")
    with open(CONFIG_INI, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    return target


# --------------------------------------------------------------------------- #
# Hardened restart (give IBC a clean GUI so the toggle commits)
# --------------------------------------------------------------------------- #
def _port_closed() -> bool:
    """True when nothing is listening on the paper port."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"if (Get-NetTCPConnection -LocalPort {ibkr_paper.PAPER_PORT} -State Listen "
         f"-ErrorAction SilentlyContinue) {{ 'OPEN' }} else {{ 'CLOSED' }}"],
        check=False, capture_output=True, text=True)
    return "CLOSED" in (out.stdout or "")


def stop_gateway(wait: int = 30) -> bool:
    """Kill ONLY the Gateway process (the one listening on the paper port), not other
    java apps (e.g. the ThetaData collector on its own port). Returns True once the
    port is closed AND no listener remains — the clean state IBC needs to relaunch.

    SUPERSEDED as part of restart_gateway()'s path (see "THE ELEVATION GAP" above):
    this runs Stop-Process from a NON-ELEVATED context and silently no-ops against the
    elevated Gateway process. Kept here because it's still importable/useful (e.g.
    tests, or killing a gateway that happens to be running non-elevated)."""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"$p=(Get-NetTCPConnection -LocalPort {ibkr_paper.PAPER_PORT} -State Listen "
         f"-ErrorAction SilentlyContinue).OwningProcess; if ($p) "
         f"{{ Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }}"],
        check=False, capture_output=True)
    for _ in range(wait):
        if _port_closed() and not ibkr_paper.gateway_running():
            return True
        time.sleep(1)
    return _port_closed()


def _read_arm_restart_state() -> dict | None:
    """Read the elevated task's JSON completion-state file. None if missing/unreadable."""
    try:
        with open(ARM_RESTART_STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and "ts" in data and "ok" in data:
            return data
    except (OSError, ValueError):
        pass
    return None


def restart_gateway() -> bool:
    """Clean-restart recipe — routed through the ELEVATED `GatewayArmRestart`
    scheduled task (see "THE ELEVATION GAP" above), because a non-elevated kill
    silently no-ops against the elevated Gateway process.

    Deletes any stale completion-state file, triggers the task via
    `schtasks /run /tn GatewayArmRestart`, then polls the state file for a FRESH
    result (timestamped after the trigger). Returns True/False on the task's own
    `ok` field — same contract as before, so arm()/disarm() need no changes. Returns
    False (never raises) if the trigger fails or no fresh result shows up in time."""
    try:
        os.remove(ARM_RESTART_STATE_FILE)
    except OSError:
        pass  # missing/unreadable stale file is fine — just means nothing to clear

    trigger_time = time.time()
    triggered = subprocess.run(
        ["schtasks", "/run", "/tn", ARM_RESTART_TASK],
        check=False, capture_output=True, text=True)
    if triggered.returncode != 0:
        print(f"  WARN: failed to trigger '{ARM_RESTART_TASK}' task: "
              f"{(triggered.stderr or triggered.stdout or '').strip()}")
        return False

    deadline = trigger_time + ARM_RESTART_POLL_TIMEOUT
    while time.time() < deadline:
        state = _read_arm_restart_state()
        if state is not None and state.get("ts", 0) > trigger_time:
            ok = bool(state.get("ok"))
            detail = state.get("detail", "")
            print(f"  GatewayArmRestart task result: ok={ok} detail={detail!r}")
            return ok
        time.sleep(1)

    print(f"  WARN: no fresh '{ARM_RESTART_TASK}' result within "
          f"{ARM_RESTART_POLL_TIMEOUT}s.")
    return False


# --------------------------------------------------------------------------- #
# Verification probe — distinguishes read-only vs armed WITHOUT transmitting
# --------------------------------------------------------------------------- #
def probe_api_readonly(timeout: int = 15) -> bool:
    """Return True if the live PAPER Gateway API is READ-ONLY (transmission BLOCKED),
    False if it is WRITE-ENABLED (transmission allowed). Raises RuntimeError if it
    cannot get a definitive signal.

    Opens its OWN paper-4002 connection (readonly=False so the *connection* is
    write-capable; the gateway-level Read-Only API checkbox is what we actually measure),
    then delegates the ZERO-TRANSMISSION cancel-a-fabricated-order technique to the ONE
    shared probe (connections.gateway_probe.probe_api_readonly). No order is ever sent or
    rested. The public signature + connect/disconnect + raise-on-no-signal behavior are
    UNCHANGED — arm()/disarm()/verify and morning_execute_run see the same contract as
    before (raise_on_indeterminate=True preserves the loud UNVERIFIED failure, which
    disarm's verify relies on to never silently claim 'locked' on an unmeasurable line).
    """
    ib = IB()
    try:
        ib.connect(ibkr_paper.HOST, ibkr_paper.PAPER_PORT, clientId=_VERIFY_CLIENT_ID,
                   readonly=False, timeout=timeout)
        return gateway_probe.probe_api_readonly(
            ib, port=ibkr_paper.PAPER_PORT, timeout=timeout,
            raise_on_indeterminate=True)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Public arm / disarm — each self-verifies and raises loudly on mismatch
# --------------------------------------------------------------------------- #
def arm() -> bool:
    """Allow transmission: ReadOnlyApi=no + clean restart, then VERIFY the live API
    is actually write-enabled. Raises RuntimeError if the toggle didn't take."""
    print("  ARM: setting ReadOnlyApi=no and clean-restarting the Gateway...")
    set_readonly_api(False)
    if not restart_gateway():
        raise RuntimeError("ARM FAILED: Gateway did not come back up after restart.")
    still_readonly = probe_api_readonly()
    if still_readonly:
        raise RuntimeError(
            "ARM FAILED: Gateway is STILL Read-Only after restart — the IBC toggle "
            "did not commit (likely a confirmation dialog blocked it). DO NOT proceed; "
            "the API will reject orders. A human must uncheck "
            "Configure > Settings > API > 'Read-Only API' in the Gateway GUI once.")
    print("  ARM: VERIFIED write-enabled (transmission allowed).")
    return True


def disarm() -> bool:
    """Restore the safe lock: ReadOnlyApi=yes + clean restart, then VERIFY the live
    API is actually Read-Only (transmission blocked). Raises if it didn't lock."""
    print("  DISARM: restoring ReadOnlyApi=yes and clean-restarting the Gateway...")
    set_readonly_api(True)
    if not restart_gateway():
        raise RuntimeError("DISARM FAILED: Gateway did not come back up after restart.")
    is_readonly = probe_api_readonly()
    if not is_readonly:
        raise RuntimeError(
            "DISARM FAILED: Gateway is STILL write-enabled after restart — the lock "
            "did NOT take. A human must check Configure > Settings > API > "
            "'Read-Only API' in the Gateway GUI.")
    print("  DISARM: VERIFIED Read-Only (transmission re-locked).")
    return True


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Arm/disarm/verify the paper Gateway (PAPER ONLY).")
    p.add_argument("action", choices=["arm", "disarm", "verify"],
                   help="arm=allow transmission, disarm=re-lock, verify=report state only")
    args = p.parse_args()
    if args.action == "arm":
        arm()
    elif args.action == "disarm":
        disarm()
    else:
        ro = probe_api_readonly()
        print(f"  VERIFY: Gateway API is {'READ-ONLY (locked)' if ro else 'WRITE-ENABLED (armed)'}; "
              f"config.ini ReadOnlyApi={read_readonly_api()}")
