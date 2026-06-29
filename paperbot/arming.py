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
     (with java_version=17, see connections.ibkr). No zombie => no stranded dialog.
  3. VERIFY the toggle actually took effect by probing the live API's read-only state
     with a ZERO-TRANSMISSION primitive (a cancel of a non-existent orderId — see
     `probe_api_readonly`). Raise loudly if it didn't take, instead of proceeding.

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

import re
import subprocess
import threading
import time

from ib_async import IB

from connections import clientids, ibkr

CONFIG_INI = r"C:\IBC\config.ini"
_VERIFY_CLIENT_ID = clientids.get("paperbot_arm_verify")  # 39


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
         f"if (Get-NetTCPConnection -LocalPort {ibkr.PAPER_PORT} -State Listen "
         f"-ErrorAction SilentlyContinue) {{ 'OPEN' }} else {{ 'CLOSED' }}"],
        check=False, capture_output=True, text=True)
    return "CLOSED" in (out.stdout or "")


def stop_gateway(wait: int = 30) -> bool:
    """Kill ONLY the Gateway process (the one listening on the paper port), not other
    java apps (e.g. the ThetaData collector on its own port). Returns True once the
    port is closed AND no listener remains — the clean state IBC needs to relaunch."""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"$p=(Get-NetTCPConnection -LocalPort {ibkr.PAPER_PORT} -State Listen "
         f"-ErrorAction SilentlyContinue).OwningProcess; if ($p) "
         f"{{ Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }}"],
        check=False, capture_output=True)
    for _ in range(wait):
        if _port_closed() and not ibkr.gateway_running():
            return True
        time.sleep(1)
    return _port_closed()


def restart_gateway() -> bool:
    """Clean-restart recipe: stop the Gateway, WAIT until the port is fully closed,
    settle ~4s so no zombie/dialog survives, THEN relaunch (java_version=17 fix in
    connections.ibkr). A clean GUI is what lets IBC actually commit the ReadOnly
    toggle. Returns True once the Gateway is serving data again."""
    if not stop_gateway():
        print("  WARN: paper port still looked open after stop; proceeding to relaunch anyway.")
    time.sleep(4)  # let the old session fully die before IBC relaunches
    return ibkr.ensure_gateway()


# --------------------------------------------------------------------------- #
# Verification probe — distinguishes read-only vs armed WITHOUT transmitting
# --------------------------------------------------------------------------- #
def probe_api_readonly(timeout: int = 15) -> bool:
    """Return True if the live Gateway API is READ-ONLY (transmission BLOCKED),
    False if it is WRITE-ENABLED (transmission allowed). Raises RuntimeError if it
    cannot get a definitive signal.

    Zero-transmission: connects readonly=False and asks the Gateway to cancel an
    orderId that was never placed, via the RAW client call (the high-level
    ib.cancelOrder is blocked client-side for unknown ids and never reaches TWS).
    No order is ever sent or rested. The Gateway's reply is decisive (verified live):
      * Read-Only API -> code 321, "The API interface is currently in Read-Only mode."
                         -> returns True.
      * Write-enabled -> "OrderId ... not found / cannot be cancelled" (10147/10148)
                         -> returns False.
    """
    ib = IB()
    signal: dict[str, bool] = {}
    got = threading.Event()

    def on_error(reqId, errorCode, errorString, *_):
        msg = (errorString or "").lower()
        if "read-only mode" in msg or "read only mode" in msg or errorCode == 321:
            signal["readonly"] = True
            got.set()
        elif errorCode in (10147, 10148) or "not found" in msg or "cannot be cancelled" in msg:
            signal["readonly"] = False
            got.set()

    try:
        # readonly=False so the *connection* is write-capable; the gateway-level
        # Read-Only API checkbox is the thing we're actually measuring.
        ib.connect(ibkr.HOST, ibkr.PAPER_PORT, clientId=_VERIFY_CLIENT_ID,
                   readonly=False, timeout=timeout)
        ib.errorEvent += on_error
        # Fresh in-range orderId the server will treat as an unknown live order.
        # Raw client.cancelOrder reaches TWS; it transmits nothing (no order exists).
        oid = ib.client.getReqId()
        ib.client.cancelOrder(oid, "")
        deadline = time.time() + timeout
        while not got.is_set() and time.time() < deadline:
            ib.sleep(0.2)
    finally:
        try:
            ib.errorEvent -= on_error
        except Exception:
            pass
        try:
            ib.disconnect()
        except Exception:
            pass

    if "readonly" not in signal:
        raise RuntimeError(
            "arm-verify: could NOT determine the Gateway's Read-Only API state "
            "(no decisive error returned within timeout). Treat as UNVERIFIED.")
    return signal["readonly"]


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
