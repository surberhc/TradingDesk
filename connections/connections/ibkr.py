"""
ibkr.py — the one way to start and connect to the IBKR PAPER Gateway.

PAPER ONLY: paper login, paper port 4002. There is no real-money path here. A
read-only connection (the default) is physically incapable of transmitting an order;
the paperbot keeps it read-only until a human deliberately arms order transmission.

Gateway launch reuses the proven IBController script the dailyreport already uses
(`C:\\IBC\\StartGateway.bat`), which auto-logs into the paper account.
"""
from __future__ import annotations

import os
import subprocess
import time

from ib_async import IB, Stock

from connections import clientids

HOST = "127.0.0.1"
PAPER_PORT = clientids.PAPER_PORT          # 4002
GATEWAY_BAT = r"C:\IBC\StartGateway.bat"   # IBController auto-login (paper)


def _gateway_env() -> dict:
    """Environment for launching the Gateway via IBC.

    Works around a real bug in IBC 3.24.0's StartIBC.bat with Gateway 1045: its
    JRE-version probe (`java.exe -XshowSettings:properties | findstr "java.version ="`)
    comes back EMPTY in the launch context, and the next line
    `if not "%java_version:1.8=%"=="%java_version%" set moduleAccess=` then throws
    "set was unexpected at this time", aborting BEFORE Java ever starts. The probe
    only assigns java_version if it matches output, so pre-seeding java_version to a
    non-1.8 value makes the broken line a safe no-op and the Gateway launches.
    No IBKR script is modified - this is purely an inherited environment variable.
    (Verified 2026-06-26: with this set, port 4002 comes up in ~15s.)
    """
    env = dict(os.environ)
    env["java_version"] = "17"
    return env


def gateway_running(client_id: int = clientids.CLIENT_IDS["dailyreport_gateway_check"],
                    timeout: int = 8) -> bool:
    """True if the paper Gateway is up and serving data (a real data round-trip)."""
    ib = IB()
    try:
        ib.connect(HOST, PAPER_PORT, clientId=client_id, readonly=True, timeout=timeout)
        spy = Stock("SPY", "SMART", "USD")
        ib.qualifyContracts(spy)
        bars = ib.reqHistoricalData(
            spy, endDateTime="", durationStr="1 D", barSizeSetting="1 day",
            whatToShow="TRADES", useRTH=True, formatDate=1, timeout=20)
        ib.disconnect()
        return len(bars) > 0
    except Exception:
        try:
            ib.disconnect()
        except Exception:
            pass
        return False


def ensure_gateway(wait_secs: int = 180) -> bool:
    """Make sure the paper Gateway is up; launch it (IBC auto-login) if not. Returns
    True once it's serving data, False if it never came up within wait_secs."""
    if gateway_running():
        return True
    subprocess.Popen(["cmd", "/c", GATEWAY_BAT], creationflags=subprocess.CREATE_NEW_CONSOLE,
                     env=_gateway_env())
    waited = 0
    while waited < wait_secs:
        time.sleep(10)
        waited += 10
        if gateway_running():
            return True
    return False


def connect(consumer: str, readonly: bool = True, launch: bool = False, timeout: int = 10) -> IB:
    """Connect to the PAPER Gateway using a registered clientId.

    consumer : a key in connections.clientids.CLIENT_IDS (e.g. "paperbot").
    readonly : True (default) -> the session cannot transmit orders. The paperbot
               only flips this to False when a human arms order transmission.
    launch   : if True, start the Gateway first when it's down.
    """
    client_id = clientids.get(consumer)
    if launch and not gateway_running():
        if not ensure_gateway():
            raise RuntimeError("paper Gateway did not come up")
    ib = IB()
    ib.connect(HOST, PAPER_PORT, clientId=client_id, readonly=readonly, timeout=timeout)
    return ib
