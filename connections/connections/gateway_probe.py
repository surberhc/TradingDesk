"""
gateway_probe.py — the ONE shared, port-parameterized, ZERO-TRANSMISSION probe of a
Gateway's Read-Only API state.

Consolidates the two duplicated idioms that used to live in paperbot/arming.py (PAPER,
port 4002) and paperbot/safe_execute.py (LIVE-TRADE, port 4003) into a single primitive
(spec docs/PRODUCTION_REBALANCE_CONTROL_PLANE.md §2.2 item 2, conductor #64). It lives in
`connections` — the lowest, broker-facing layer — so BOTH the paper arming path and the
live-trade execution gate can import it WITHOUT a circular import: `connections` never
imports `paperbot` (arming.py already does `from connections import ...`).

ZERO-TRANSMISSION, by construction
----------------------------------
The probe never places or rests an order. It attaches an error handler, asks the Gateway
(via the RAW client call, which reaches TWS even for an unknown id) to cancel a FABRICATED,
never-placed orderId, and reads the decisive reply:
  * Read-Only API -> code 321 / "read-only mode"                      -> True  (blocked)
  * Write-enabled -> 10147/10148 / "not found" / "cannot be cancelled" -> False (armed)
No order ever reaches the market — there is no such order to cancel.

CONTRACT
--------
Takes an ALREADY-OPEN `ib` (the caller owns the connect/disconnect and picks the lane and
clientId). `port` is CONTEXTUAL ONLY — used in log/error messages so an operator can see
WHICH Gateway was measured; it is NEVER used to open a connection here.

FAILS CLOSED on no decisive signal within `timeout`:
  * default (raise_on_indeterminate=False) -> return True (treat as read-only / refuse to
    transmit). This is what the execute_plan gate and the Control-Plane probe rely on.
  * raise_on_indeterminate=True            -> raise RuntimeError instead of returning True,
    so a caller that must VERIFY a definite state (arming's arm/disarm self-check) fails
    loudly rather than silently reporting "read-only" on an unmeasurable connection.
"""
from __future__ import annotations

import threading
import time


def probe_api_readonly(ib, *, port=None, timeout: int = 15,
                       raise_on_indeterminate: bool = False) -> bool:
    """Return True if the OPEN Gateway connection is READ-ONLY (transmission physically
    BLOCKED), False if it is WRITE-ENABLED (armed). Zero-transmission (see module docstring).

    `port` is used only for context in the fail-closed message, never to connect. FAILS
    CLOSED on no decisive signal within `timeout`: return True by default, or raise
    RuntimeError if `raise_on_indeterminate` is set (the arm/disarm verify path)."""
    signal: dict[str, bool] = {}
    got = threading.Event()

    def on_error(reqId, errorCode, errorString, *_):
        msg = (errorString or "").lower()
        if "read-only mode" in msg or "read only mode" in msg or errorCode == 321:
            signal["readonly"] = True
            got.set()
        elif (errorCode in (10147, 10148) or "not found" in msg
              or "cannot be cancelled" in msg):
            signal["readonly"] = False
            got.set()

    ib.errorEvent += on_error
    try:
        # Fresh in-range orderId the server treats as an unknown live order. The RAW
        # client.cancelOrder reaches TWS; it transmits nothing (no such order exists).
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

    if "readonly" not in signal:
        where = f" on port {port}" if port is not None else ""
        if raise_on_indeterminate:
            raise RuntimeError(
                f"gateway probe{where}: could NOT determine the Gateway's Read-Only API "
                f"state (no decisive error returned within timeout). Treat as UNVERIFIED.")
        # Could not measure the Gateway state -> treat as read-only (refuse to transmit).
        return True
    return signal["readonly"]
