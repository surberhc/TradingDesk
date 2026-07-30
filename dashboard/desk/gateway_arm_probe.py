"""gateway_arm_probe.py — a standalone READ-ONLY, ZERO-TRANSMISSION probe of the
port-4003 live-trade Gateway's armed state (Read-Only API on/off).

WHAT THIS IS
------------
The Control Plane page (dashboard/desk/page_control_plane.py) needs to SHOW whether the
port-4003 live-trade Gateway is physically armed (Read-Only API unchecked -> can transmit)
or safe (Read-Only API checked -> nothing can transmit), instead of only instructing the
operator to check it by hand. Streamlit runs in an auto-refreshing web process, so this
probe is a SEPARATE, import-light script it shells out to — no broker socket is ever opened
inside the Streamlit process.

ZERO-TRANSMIT, by construction:
  * Connects READ-ONLY (readonly=True) to the live-trade Gateway on port 4003 using the
    reused ibkr_live_trade.connect pattern and the dedicated `cp_arm_probe` clientId (its
    own registry entry, distinct from every S8 / S0 consumer on 4003).
  * The actual armed-state measurement REUSES s0_live_deploy._probe_gateway_readonly — the
    exact same technique the hardened executor uses: attach an error handler, ask the
    Gateway to cancel a FABRICATED, never-placed orderId, and read the decisive reply
    (code 321 / "read-only mode" => read-only / NOT armed; 10147/10148 / "not found" /
    "cannot be cancelled" => write-enabled / armed). No order is ever placed or rested.
  * This script places, modifies, and transmits NOTHING. It only tests the Gateway's
    read-only toggle; it needs no account data at all.

OUTPUT CONTRACT
---------------
Prints EXACTLY one uppercase token on the LAST stdout line:
  * READONLY     — Gateway is read-only / NOT armed / safe (nothing can transmit)
  * ARMED        — Gateway is write-enabled / armed (it CAN transmit)
  * UNREACHABLE  — could not connect to the Gateway
A short human-readable sentence goes to STDERR (never stdout), so the caller can parse the
last stdout line without stripping prose.

FAIL-CLOSED. Any exception or ambiguity is resolved toward SAFE:
  * a connect failure -> UNREACHABLE
  * an ambiguous / no-signal probe reply -> READONLY (the reused probe already fails closed
    to read-only, i.e. "not armed", on no decisive signal)
This script NEVER raises out; it always prints a token and exits 0.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe gateway_arm_probe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# --- Make the sibling packages importable (reuse, don't rebuild) ---------------
# Mirror how page_control_plane.py bootstraps sys.path so this runs from ANY cwd.
# This file lives at dashboard/desk/gateway_arm_probe.py, so the repo root is parents[2].
REPO = Path(__file__).resolve().parents[2]
for _sub in ("paperbot", "connections"):
    _p = REPO / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
_conn = REPO / "connections"
if str(_conn) not in sys.path:
    sys.path.insert(0, str(_conn))

# The dedicated read-only clientId for THIS probe (its own registry entry, never collides
# with the S8 / S0 consumers on 4003).
PROBE_CLIENTID = "cp_arm_probe"


def probe() -> str:
    """Connect READ-ONLY to the 4003 live-trade Gateway, measure its armed state with the
    reused zero-transmission probe, and return one of READONLY / ARMED / UNREACHABLE.

    Fail-closed: a connect failure -> UNREACHABLE; an ambiguous probe reply -> READONLY
    (the reused probe fails closed to read-only on no decisive signal). Disconnects in a
    finally. Never raises."""
    # Heavy imports are LAZY (inside the function) so importing this module opens no socket.
    try:
        from connections import ibkr_live_trade
    except Exception as exc:  # noqa: BLE001 — any import failure is treated as unreachable
        print(f"could not import the live-trade connection module: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return "UNREACHABLE"

    # CONNECT (read-only). Reuses the exact ibkr_live_trade.connect pattern s0_live uses,
    # but with the probe's own clientId. launch=False — never boot the Gateway from a probe.
    try:
        ib = ibkr_live_trade.connect(PROBE_CLIENTID, launch=False, readonly=True)
    except Exception as exc:  # noqa: BLE001 — connect failure -> UNREACHABLE (fail-closed)
        print(f"could not reach the live-trade Gateway on port 4003: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return "UNREACHABLE"

    try:
        # REUSE the hardened executor's zero-transmission armed-state probe verbatim.
        import s0_live_deploy
        is_readonly = s0_live_deploy._probe_gateway_readonly(ib)
        return "READONLY" if is_readonly else "ARMED"
    except Exception as exc:  # noqa: BLE001 — an ambiguous/failed probe fails closed to READONLY
        print(f"could not measure the Gateway's armed state (failing closed to read-only): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return "READONLY"
    finally:
        try:
            ib.disconnect()
        except Exception:  # noqa: BLE001 — a clean shutdown must never mask the result
            pass


def main() -> int:
    token = "UNREACHABLE"
    try:
        token = probe()
    except Exception as exc:  # noqa: BLE001 — belt-and-suspenders: never raise out of the script
        print(f"probe failed unexpectedly: {type(exc).__name__}: {exc}", file=sys.stderr)
        token = "UNREACHABLE"
    # Human sentence -> stderr; the machine token is the LAST stdout line.
    sentences = {
        "READONLY": "Gateway is READ-ONLY (Read-Only API ON) — NOT armed; nothing can "
                    "transmit.",
        "ARMED": "Gateway is WRITE-ENABLED (Read-Only API OFF) — ARMED; it CAN transmit.",
        "UNREACHABLE": "Could not reach the port-4003 live-trade Gateway — is it up and "
                       "logged in? Nothing was transmitted.",
    }
    print(sentences.get(token, sentences["UNREACHABLE"]), file=sys.stderr)
    print(token)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001 — reconfigure is a nicety, not required
        pass
    sys.exit(main())
