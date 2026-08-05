"""livedata_chained_open.py — DEPENDENCY-GATED morning bring-up for the read-only
live-DATA Gateway (port 4001), chained behind the 4003 live-trade lane.

WHY A GATE, NOT A CLOCK
-----------------------
The owner brings both live gateways up in the morning by approving one IBKR Mobile 2FA
push each. To keep the two 2FA prompts from tangling, 4001 must come up ONLY AFTER 4003
is confirmed fully logged in — never on a fixed timer. A fixed offset (e.g. "08:12")
fails if the 08:00 4003 push is not answered promptly: the 4001 push would still fire and
two 2FA requests would be pending at once. This module gates on 4003's actual state
instead, so there is never more than ONE pending 2FA.

THE GATE — evaluated each cycle (see ``decide_action``):
  * 4001 ALREADY listening      -> do nothing (already up; AutoRestartTime + the watchdog
                                    keep it alive INDEPENDENT of 4003 from here on).
  * 4001 down, 4003 confirmed UP -> LAUNCH 4001 now (this fires 4001's own 2FA push).
  * 4001 down, 4003 NOT up yet   -> do nothing this cycle (4003's 2FA is still pending;
                                    firing 4001 now would create a second pending 2FA).
                                    Wait and re-check next cycle.

"Port listening" is the up-signal for BOTH lanes: IB Gateway binds its API socket only
AFTER a successful login (past 2FA), so a listening port means "logged in and serving",
and a gateway sitting at the 2FA screen is NOT listening. If 4003's state is
undeterminable (probe None) we treat it as NOT up and WAIT — conservative, never fires a
second 2FA on a guess.

The gate governs ONLY the initial daily bring-up. Once 4001 is up it stays up via its own
AutoRestartTime and the LiveDataGatewayWatchdog — it is NEVER torn down when 4003 is
(the 15:05 CT S8 teardown is scoped to C:\\IBC-Live-Trade / port 4003 and cannot touch
4001), so 4001 remains up for the 17:30 CT forward pull regardless of 4003.

SAFETY: the LAUNCH goes through ``connections.ibkr_live_data.ensure_gateway()``, which
holds the SAME narrow launch mutex + relaunch cooldown the watchdog uses — so the chained
open and the watchdog can never stack two 4001 launches (no orphan pileup). This module
reads 4003 only by observing its port; it never touches the 4003 lane, its tasks, or its
config. Read-only lane: nothing here can transmit an order. NEVER raises; main() exits 0.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, Optional

# Self-contained sys.path shim — same rationale as the sibling livebot modules.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import s8_gateway_alert  # noqa: E402  (reused: the generic TCP port probe)

# Mirror connections.clientids.{LIVE_DATA_PORT, LIVE_TRADE_PORT}; hardcoded so this
# best-effort morning task never depends on importing the connections package just to
# read a constant. The launch path DOES import connections (lazily) — see _default_launch.
LIVE_DATA_PORT = 4001
LIVE_TRADE_PORT = 4003


def decide_action(*, up_4001: Optional[bool], up_4003: Optional[bool]) -> str:
    """Pure gate. Returns one of:
        "already_up"  — 4001 is listening; nothing to do.
        "launch"      — 4001 down AND 4003 confirmed up; launch 4001 now.
        "wait_4003"   — 4001 down AND 4003 not confirmed up; wait (no launch, no 2FA push).

    up_xxxx is True (listening), False (refused), or None (undeterminable). Only an
    explicit True counts as "up": a None 4003 -> wait (never fire a 2nd 2FA on a guess);
    a None 4001 -> not "already up", so we fall through to the 4003 gate (and
    ensure_gateway fast-paths if 4001 is in fact already serving)."""
    if up_4001 is True:
        return "already_up"
    if up_4003 is True:
        return "launch"
    return "wait_4003"


def _default_launch() -> bool:
    """Launch the 4001 gateway via connections.ibkr_live_data.ensure_gateway() — the SAME
    mutex-guarded path the watchdog uses, so the two can never stack a launch. Returns
    True if 4001 is up within ensure_gateway's window. Never raises."""
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(repo_root, "connections")
        if p not in sys.path:
            sys.path.insert(0, p)
        from connections import ibkr_live_data  # noqa: PLC0415
        return bool(ibkr_live_data.ensure_gateway())
    except Exception:  # noqa: BLE001 — a launch hiccup must never raise out of a morning task
        return False


def run_once(
    *,
    probe_4001: Callable[[], Optional[bool]] = None,
    probe_4003: Callable[[], Optional[bool]] = None,
    launch_4001: Callable[[], bool] = _default_launch,
    log: Callable[[str], Any] = print,
) -> dict:
    """One gated cycle. Probes 4001 first (short-circuit if already up), else probes 4003
    and launches 4001 only when 4003 is confirmed up. NEVER raises.

    Returns ``{"action": <str>, "launched": <bool>, "up_4001": ..., "up_4003": ...}``.
    """
    if probe_4001 is None:
        probe_4001 = lambda: s8_gateway_alert.port_listening(LIVE_DATA_PORT)  # noqa: E731
    if probe_4003 is None:
        probe_4003 = lambda: s8_gateway_alert.port_listening(LIVE_TRADE_PORT)  # noqa: E731
    try:
        try:
            up1 = probe_4001()
        except Exception:  # noqa: BLE001 — undeterminable, not "up"
            up1 = None

        if up1 is True:
            log("livedata_chained_open: 4001 already listening — nothing to do "
                "(watchdog/AutoRestart keep it alive, independent of 4003).")
            return {"action": "already_up", "launched": False, "up_4001": up1, "up_4003": None}

        try:
            up3 = probe_4003()
        except Exception:  # noqa: BLE001 — undeterminable, treat as NOT up -> wait
            up3 = None

        action = decide_action(up_4001=up1, up_4003=up3)
        if action == "launch":
            log(f"livedata_chained_open: 4003 is UP (past 2FA) and 4001 is down "
                f"(up_4001={up1!r}); launching 4001 now — this fires 4001's own 2FA push.")
            launched = bool(launch_4001())
            log(f"livedata_chained_open: 4001 launch attempted -> up={launched!r}.")
            return {"action": "launch", "launched": launched, "up_4001": up1, "up_4003": up3}

        log(f"livedata_chained_open: 4003 NOT confirmed up (up_4003={up3!r}); its 2FA is "
            f"still pending or unknown — NOT launching 4001 this cycle (avoids a 2nd "
            f"pending 2FA). Will re-check next cycle.")
        return {"action": "wait_4003", "launched": False, "up_4001": up1, "up_4003": up3}
    except Exception as exc:  # noqa: BLE001 — a morning task can NEVER raise into its caller
        try:
            log(f"livedata_chained_open: run_once failed entirely "
                f"({type(exc).__name__}: {exc}); nothing launched.")
        except Exception:  # noqa: BLE001
            pass
        return {"action": "error", "launched": False, "error": f"{type(exc).__name__}: {exc}"}


def main(argv=None) -> int:
    """One-shot entrypoint — run the gated cycle with real defaults, log, ALWAYS rc 0."""
    try:
        result = run_once()
    except Exception as exc:  # noqa: BLE001 — never let anything out of a best-effort task
        result = {"action": "error", "launched": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        print(f"livedata_chained_open: {result}")
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
