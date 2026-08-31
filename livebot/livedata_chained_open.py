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
  * 4001 ALREADY listening       -> do nothing (already up; AutoRestartTime + the watchdog
                                    keep it alive INDEPENDENT of 4003 from here on).
  * a 4001 gateway PROCESS exists and is YOUNG (within LOGIN_GRACE_SECS)
                                 -> "wait_login": do nothing (see THE PROCESS GATE below).
  * a 4001 gateway PROCESS exists and every one of them is PAST the login window
                                 -> "reap_wedged": kill the wedged orphans, then ONE launch
                                    may proceed (only if 4003 is up).
  * 4001 down, 4003 confirmed UP -> LAUNCH 4001 now (this fires 4001's own 2FA push).
  * 4001 down, 4003 NOT up yet   -> do nothing this cycle (4003's 2FA is still pending;
                                    firing 4001 now would create a second pending 2FA).
                                    Wait and re-check next cycle.

"Port listening" is the up-signal for BOTH lanes: IB Gateway binds its API socket only
AFTER a successful login (past 2FA), so a listening port means "logged in and serving",
and a gateway sitting at the 2FA screen is NOT listening. If 4003's state is
undeterminable (probe None) we treat it as NOT up and WAIT — conservative, never fires a
second 2FA on a guess.

THE PROCESS GATE — WHY THE PORT ALONE IS NOT ENOUGH (incident 2026-08-31)
------------------------------------------------------------------------
That same property makes the port BLIND during login: a gateway wedged at the IBKR login /
2FA screen has not bound 4001 yet, so the port probe cannot tell it apart from "no gateway
at all". On 2026-08-31 the machine accumulated **49 stacked IB Gateway windows** — one per
5-minute cycle of ``LiveDataGatewayChainedOpen_0805CT`` from 08:05 to 12:00. Every cycle saw
"4001 not listening" + "4003 up" and launched yet another rival; each rival fired its own
2FA push, and (``ExistingSessionDetectedAction=primary``) whichever session logged in LAST
evicted the one the human had just approved, so the pileup was self-sustaining.

The sibling live-trade lane learned this exact lesson on 2026-08-25 and fixed it in step 1b
of ``run_live_trade_gateway_open.cmd``: **the PROCESS is ground truth, not the port.** That
fix was never ported here; this module is that port. A live-data gateway process existing at
all is sufficient reason not to start another one — bound or not.

FAIL-OPEN, exactly like the sibling: only a DEFINITE "one exists" answer suppresses the
launch. A process scan that cannot be performed (``None``) falls straight through to the old
4003 gate, so a genuinely gateway-less morning is never left without a gateway. A determinate
"none found" (``[]``) likewise falls through and launches.

The gate governs ONLY the initial daily bring-up. Once 4001 is up it stays up via its own
AutoRestartTime and the LiveDataGatewayWatchdog — it is NEVER torn down when 4003 is
(the 15:05 CT S8 teardown is scoped to C:\\IBC-Live-Trade / port 4003 and cannot touch
4001), so 4001 remains up for the 17:30 CT forward pull regardless of 4003.

SAFETY: the LAUNCH goes through ``connections.ibkr_live_data.ensure_gateway()``, which
holds the SAME narrow launch mutex + relaunch cooldown the watchdog uses — so the chained
open and the watchdog can never stack two 4001 launches (no orphan pileup). At most ONE
launch happens per cycle, ever. The reap is positively identified by the FULL live-data
install dir and re-verified at kill time, so it can never touch the 4003 lane's gateway.
This module reads 4003 only by observing its port; it never touches the 4003 lane, its
tasks, or its config. Read-only lane: nothing here can transmit an order. NEVER raises;
main() exits 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional

# Self-contained sys.path shim — same rationale as the sibling livebot modules.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import s8_gateway_alert  # noqa: E402  (reused: the generic TCP port probe)
import s8_lock  # noqa: E402  (reused: its dependency-free taskkill — do not duplicate)

# Mirror connections.clientids.{LIVE_DATA_PORT, LIVE_TRADE_PORT}; hardcoded so this
# best-effort morning task never depends on importing the connections package just to
# read a constant. The launch path DOES import connections (lazily) — see _default_launch.
LIVE_DATA_PORT = 4001
LIVE_TRADE_PORT = 4003

# The positive-ID marker for a live-DATA gateway process: the FULL live-data install dir.
# CRITICAL SAFETY NOTE — this must NEVER be shortened to the bare "C:\IBC" prefix. "C:\IBC"
# is a string prefix of BOTH C:\IBC-Live-Data and C:\IBC-Live-Trade, and matching on that
# prefix is the exact bug that let one lane kill another lane's gateway on 2026-07-23 (see
# docs/INCIDENT_2026-07-23_arm_restart_killed_live_gateway.md and s8_gateway_reap's SAFETY
# item 1). Only the full sibling directory is a safe positive ID.
INSTALL_MARKER = r"C:\IBC-Live-Data"
# A live-data gateway younger than this is presumed to still be completing login/2FA and is
# NEVER killed and NEVER raced with a rival launch. Mirrors s8_gateway_reap.LOGIN_GRACE_SECS.
LOGIN_GRACE_SECS = 1800


def _cmdline_is_live_data(cmdline: Optional[str]) -> bool:
    """True iff ``cmdline`` positively identifies a live-DATA gateway. A missing/empty
    cmdline is NEVER a match — an unidentifiable process is never ours."""
    if not cmdline:
        return False
    return INSTALL_MARKER.lower() in str(cmdline).lower()


def decide_action(
    *,
    up_4001: Optional[bool],
    up_4003: Optional[bool],
    proc_ages: Optional[List[float]] = None,
) -> str:
    """Pure gate. Returns one of:
        "already_up"  — 4001 is listening; nothing to do.
        "wait_login"  — a 4001 gateway PROCESS already exists and is young enough to still
                        be completing login/2FA; do NOT launch a rival.
        "reap_wedged" — 4001 gateway processes exist, ALL are past the login window, and none
                        has bound 4001; they are wedged orphans -> reap, then one launch may
                        proceed.
        "launch"      — no 4001 process in the way AND 4003 confirmed up; launch 4001 now.
        "wait_4003"   — 4001 down AND 4003 not confirmed up; wait (no launch, no 2FA push).

    up_xxxx is True (listening), False (refused), or None (undeterminable). Only an
    explicit True counts as "up": a None 4003 -> wait (never fire a 2nd 2FA on a guess);
    a None 4001 -> not "already up", so we fall through to the process/4003 gates (and
    ensure_gateway fast-paths if 4001 is in fact already serving).

    ``proc_ages`` is the age in seconds of every existing live-data gateway process, or
    ``None`` when the process scan could not be performed. It defaults to ``None``, which
    reproduces the pre-2026-08-31 behaviour EXACTLY.

    WHY THE PROCESS GATE. The port is BLIND during login — a gateway wedged at the IBKR
    login/2FA screen has not bound 4001, so the port probe cannot distinguish it from "no
    gateway at all". "wait_login" means a gateway process already exists and is plausibly
    still completing login/2FA, so launching another would create a rival that fires its own
    2FA push and evicts the pending one — that is precisely the 2026-08-31 49-window pileup.
    "reap_wedged" means every existing gateway is past the login window and STILL has not
    bound 4001, so they are wedged orphans: kill them, then a single fresh launch may
    proceed.

    FAIL-OPEN: an undeterminable process scan (``None``) falls through to the old 4003 gate,
    so a genuinely gateway-less morning is never left without a gateway — only a DEFINITE
    "one exists" answer suppresses the launch."""
    if up_4001 is True:
        return "already_up"
    if proc_ages:
        # The YOUNGEST process protects the whole set: if any one of them could still be a
        # human's in-progress login, neither a rival launch nor a reap is safe.
        if any(float(age) <= LOGIN_GRACE_SECS for age in proc_ages):
            return "wait_login"
        return "reap_wedged"
    if up_4003 is True:
        return "launch"
    return "wait_4003"


def find_livedata_gateways(
    *,
    run: Callable[..., Any] = subprocess.run,
) -> Optional[List[Dict[str, Any]]]:
    """Live ``{"pid", "cmdline", "age_secs"}`` dicts for every ``java``/``javaw`` process
    whose command line contains ``INSTALL_MARKER`` (case-insensitive). ``None`` means the
    scan could not be performed (undeterminable -> the caller FAILS OPEN); ``[]`` is a
    determinate "none found". Never raises. Modelled on
    ``s8_gateway_reap.find_live_trade_gateways``."""
    if os.name != "nt":
        return None
    # INSTALL_MARKER is interpolated rather than re-typed so the PowerShell filter can never
    # drift from the Python-side positive ID (and can never decay to the "C:\IBC" prefix).
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$now=Get-Date;"
        "$p=@(Get-CimInstance Win32_Process -Filter \"Name='java.exe' OR Name='javaw.exe'\");"
        f"$m=@($p | Where-Object {{ $_.CommandLine -and $_.CommandLine -like '*{INSTALL_MARKER}*' }} |"
        " ForEach-Object { [pscustomobject]@{ pid=$_.ProcessId; cmdline=$_.CommandLine;"
        " age=[double]($now - $_.CreationDate).TotalSeconds } });"
        "if($m.Count -eq 0){Write-Output 'EMPTY'}else{$m | ConvertTo-Json -Compress}"
    )
    try:
        out = run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001 — undeterminable -> None, the caller fails open
        return None
    text = (getattr(out, "stdout", "") or "").strip()
    if not text:
        return None
    if "EMPTY" in text:
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return None
    rows: List[Dict[str, Any]] = []
    for row in parsed:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        try:
            age = float(row.get("age") or 0.0)
        except (TypeError, ValueError):
            age = 0.0
        if pid > 0:
            rows.append({"pid": pid, "cmdline": row.get("cmdline"), "age_secs": age})
    return rows


def reap_livedata_gateways(
    gateways: Optional[List[Dict[str, Any]]],
    *,
    kill: Optional[Callable[[int], bool]] = None,
    log: Callable[[str], Any] = print,
) -> int:
    """Kill the wedged live-data gateways in ``gateways``; return how many were killed.

    SAFETY (mirrors ``s8_gateway_reap``'s discipline):
      * RE-CHECK AT KILL TIME. An entry is killed only if its command line STILL positively
        contains ``INSTALL_MARKER``. A missing/empty cmdline is NEVER a match, and a
        ``C:\\IBC-Live-Trade`` cmdline can never match — this lane must not be able to reach
        across and kill the live-trade gateway (the 2026-07-23 cross-lane incident).
      * NEVER RAISES. A failure on one pid is counted and logged; the rest still proceed."""
    if kill is None:
        kill = s8_lock.kill_pid
    killed = 0
    try:
        for gw in (gateways or []):
            try:
                if not isinstance(gw, dict):
                    continue
                try:
                    pid = int(gw.get("pid") or 0)
                except (TypeError, ValueError):
                    continue
                if pid <= 0:
                    continue
                cmdline = gw.get("cmdline")
                if not _cmdline_is_live_data(cmdline):
                    log(f"livedata_chained_open: REFUSING to kill pid={pid} — its command "
                        f"line does not positively identify a {INSTALL_MARKER} gateway "
                        f"(cmdline={cmdline!r}).")
                    continue
                try:
                    age = float(gw.get("age_secs") or 0.0)
                except (TypeError, ValueError):
                    age = 0.0
                ok = bool(kill(pid))
                log(f"livedata_chained_open: reaped WEDGED live-data gateway pid={pid} "
                    f"(age={age:.0f}s, never bound {LIVE_DATA_PORT}) -> "
                    f"{'ok' if ok else 'FAILED'}")
                if ok:
                    killed += 1
            except Exception as exc:  # noqa: BLE001 — one bad pid must not abort the rest
                try:
                    log(f"livedata_chained_open: error reaping {gw!r} "
                        f"({type(exc).__name__}: {exc}); continuing.")
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001 — the reap must NEVER raise into its caller
        try:
            log(f"livedata_chained_open: reap failed entirely "
                f"({type(exc).__name__}: {exc}).")
        except Exception:  # noqa: BLE001
            pass
    return killed


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
    probe_procs: Callable[[], Optional[List[Dict[str, Any]]]] = None,
    launch_4001: Callable[[], bool] = _default_launch,
    reap: Callable[..., int] = None,
    log: Callable[[str], Any] = print,
) -> dict:
    """One gated cycle. Probes 4001 first (short-circuit if already up), then the live-data
    gateway PROCESSES, then 4003, and launches 4001 AT MOST ONCE. NEVER raises.

    Order: 4001 -> processes -> 4003 -> ``decide_action``. Every probe that raises is caught
    and treated as ``None`` (undeterminable), which FAILS OPEN to the old 4003 gate.

    Returns ``{"action": <str>, "launched": <bool>, "up_4001": ..., "up_4003": ...,
    "proc_count": int|None, "reaped": int}``.
    """
    if probe_4001 is None:
        probe_4001 = lambda: s8_gateway_alert.port_listening(LIVE_DATA_PORT)  # noqa: E731
    if probe_4003 is None:
        probe_4003 = lambda: s8_gateway_alert.port_listening(LIVE_TRADE_PORT)  # noqa: E731
    if probe_procs is None:
        probe_procs = find_livedata_gateways
    if reap is None:
        reap = reap_livedata_gateways
    try:
        try:
            up1 = probe_4001()
        except Exception:  # noqa: BLE001 — undeterminable, not "up"
            up1 = None

        if up1 is True:
            log("livedata_chained_open: 4001 already listening — nothing to do "
                "(watchdog/AutoRestart keep it alive, independent of 4003).")
            return {"action": "already_up", "launched": False, "up_4001": up1,
                    "up_4003": None, "proc_count": None, "reaped": 0}

        try:
            procs = probe_procs()
        except Exception:  # noqa: BLE001 — undeterminable -> fail OPEN to the old 4003 gate
            procs = None
        proc_ages: Optional[List[float]] = None
        if procs is not None:
            proc_ages = []
            for gw in procs:
                try:
                    proc_ages.append(float((gw or {}).get("age_secs") or 0.0))
                except Exception:  # noqa: BLE001 — an unreadable age counts as YOUNG (0.0),
                    proc_ages.append(0.0)  # i.e. protected: never launched over, never reaped

        try:
            up3 = probe_4003()
        except Exception:  # noqa: BLE001 — undeterminable, treat as NOT up -> wait
            up3 = None

        proc_count = None if procs is None else len(procs)
        action = decide_action(up_4001=up1, up_4003=up3, proc_ages=proc_ages)

        if action == "wait_login":
            youngest = min(proc_ages) if proc_ages else 0.0
            log(f"livedata_chained_open: {proc_count} live-data gateway process(es) already "
                f"exist and the youngest is {youngest:.0f}s old (< {LOGIN_GRACE_SECS}s "
                f"login/2FA grace) — it is plausibly still completing login, so 4001 is not "
                f"bound YET rather than absent. NOT launching a rival this cycle (a rival "
                f"fires its own 2FA push and evicts the pending one — the 2026-08-31 "
                f"49-window pileup). Will re-check next cycle.")
            return {"action": "wait_login", "launched": False, "up_4001": up1,
                    "up_4003": up3, "proc_count": proc_count, "reaped": 0}

        if action == "reap_wedged":
            log(f"livedata_chained_open: {proc_count} live-data gateway process(es) exist, "
                f"ALL past the {LOGIN_GRACE_SECS}s login window, and none has bound "
                f"{LIVE_DATA_PORT} — they are wedged orphans. Reaping them now.")
            try:
                reaped = int(reap(procs, log=log))
            except Exception as exc:  # noqa: BLE001 — a failed reap must not raise or launch
                reaped = 0
                log(f"livedata_chained_open: reap raised ({type(exc).__name__}: {exc}); "
                    f"treating as 0 reaped.")
            log(f"livedata_chained_open: reaped {reaped} wedged live-data gateway(s).")
            if up3 is True:
                log(f"livedata_chained_open: 4003 is UP (past 2FA); launching ONE fresh 4001 "
                    f"now — this fires 4001's own 2FA push.")
                launched = bool(launch_4001())
                log(f"livedata_chained_open: 4001 launch attempted -> up={launched!r}.")
                return {"action": "reap_wedged", "launched": launched, "up_4001": up1,
                        "up_4003": up3, "proc_count": proc_count, "reaped": reaped}
            log(f"livedata_chained_open: 4003 NOT confirmed up (up_4003={up3!r}) — wedged "
                f"gateways reaped but NOT launching this cycle (avoids a 2nd pending 2FA).")
            return {"action": "reap_wedged", "launched": False, "up_4001": up1,
                    "up_4003": up3, "proc_count": proc_count, "reaped": reaped}

        if action == "launch":
            log(f"livedata_chained_open: 4003 is UP (past 2FA) and 4001 is down "
                f"(up_4001={up1!r}, proc_count={proc_count!r}); launching 4001 now — this "
                f"fires 4001's own 2FA push.")
            launched = bool(launch_4001())
            log(f"livedata_chained_open: 4001 launch attempted -> up={launched!r}.")
            return {"action": "launch", "launched": launched, "up_4001": up1,
                    "up_4003": up3, "proc_count": proc_count, "reaped": 0}

        log(f"livedata_chained_open: 4003 NOT confirmed up (up_4003={up3!r}); its 2FA is "
            f"still pending or unknown — NOT launching 4001 this cycle (avoids a 2nd "
            f"pending 2FA). Will re-check next cycle.")
        return {"action": "wait_4003", "launched": False, "up_4001": up1,
                "up_4003": up3, "proc_count": proc_count, "reaped": 0}
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
