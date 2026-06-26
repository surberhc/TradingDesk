"""
arming.py — AUTOMATED arm/disarm of the paper Gateway for order transmission.

Order transmission is blocked at the gateway by ReadOnlyApi=yes in C:\\IBC\\config.ini.
Arming flips it to 'no' and restarts the Gateway; disarming restores 'yes' and restarts.
This is the deliberate, authorized step that lets the paperbot place PAPER orders — and
it is fully automated so no manual gateway fiddling is required. PAPER ONLY.

Minimal + reversible: exactly one line of config.ini changes, and we always restore it.
"""
from __future__ import annotations

import re
import subprocess
import time

from connections import ibkr

CONFIG_INI = r"C:\IBC\config.ini"


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


def stop_gateway(wait: int = 20) -> bool:
    """Kill ONLY the Gateway process (the one listening on the paper port), not other
    java apps (e.g. ThetaData). Returns True once the port is closed."""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"$p=(Get-NetTCPConnection -LocalPort {ibkr.PAPER_PORT} -State Listen "
         f"-ErrorAction SilentlyContinue).OwningProcess; if ($p) "
         f"{{ Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }}"],
        check=False, capture_output=True)
    for _ in range(wait):
        if not ibkr.gateway_running():
            return True
        time.sleep(1)
    return not ibkr.gateway_running()


def restart_gateway() -> bool:
    """Stop then start the Gateway (start uses connections.ibkr's java_version fix)."""
    stop_gateway()
    time.sleep(2)
    return ibkr.ensure_gateway()


def arm() -> bool:
    """Allow transmission: ReadOnlyApi=no + restart. Returns True once serving."""
    print("  ARM: setting ReadOnlyApi=no and restarting the Gateway...")
    set_readonly_api(False)
    ok = restart_gateway()
    print(f"  ARM: gateway {'up (transmission allowed)' if ok else 'FAILED to come up'}.")
    return ok


def disarm() -> bool:
    """Restore the safe lock: ReadOnlyApi=yes + restart. Returns True once serving."""
    print("  DISARM: restoring ReadOnlyApi=yes and restarting the Gateway...")
    set_readonly_api(True)
    ok = restart_gateway()
    print(f"  DISARM: gateway {'up (transmission re-locked)' if ok else 'FAILED to come up'}.")
    return ok
