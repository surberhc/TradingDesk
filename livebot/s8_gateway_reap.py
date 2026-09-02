"""s8_gateway_reap.py — S8 live-pilot GATEWAY ORPHAN REAPER (port-4003 invariant).

WHY THIS EXISTS
---------------
``s8_reap.py`` reaps the two PYTHON pilot bots (``s8_service`` / ``s8_collector``) by
command-line marker. It deliberately knows nothing about the JAVA Gateway processes — its
whole safety model is "only kill a pid whose cmdline positively matches a known bot
marker." So orphaned GATEWAY instances fall straight through it, and nothing else cleans
them up. They then accumulate.

The orphan is a recurring failure CLASS, seen three times (2026-07-05 gateway pileup, and
the 2026-07-23 arm-restart race documented in
``docs/INCIDENT_2026-07-23_arm_restart_killed_live_gateway.md``). The mechanism is always
the same: TWO launch paths run ``C:\\IBC-Live-Trade\\StartGatewayLiveTrade.bat`` around the
same moment — e.g. a mid-session self-heal via ``ibkr_live_trade.ensure_gateway()`` AND the
scheduled ``LiveTradeGatewayOpen_0815CT`` (which runs the .bat DIRECTLY, outside
ensure_gateway's launch mutex). One ``java`` wins and binds port 4003; the loser sits
alive, bound to NOTHING, forever. ensure_gateway's mutex only coordinates its own callers,
so it cannot see or prevent the direct-.bat launches. **The port is the only cross-path
ground truth.**

THE INVARIANT THIS ENFORCES
---------------------------
    There must be exactly ONE ``C:\\IBC-Live-Trade`` gateway, and it must be the process
    LISTENING on port 4003. Any ``C:\\IBC-Live-Trade`` gateway that is NOT the port-4003
    owner (and is past the boot grace) is an orphan -> reap it.

SAFETY — THE LOAD-BEARING CHECKS (mirrors s8_reap's discipline)
---------------------------------------------------------------
1. POSITIVE ID by install dir. A pid is a candidate only if its command line positively
   contains ``C:\\IBC-Live-Trade`` (case-insensitive). This is deliberately the FULL sibling
   directory, NEVER the bare ``C:\\IBC`` prefix — matching on the prefix is the exact bug
   that let a paper-lane arm/disarm kill the live gateway on 2026-07-23 (``C:\\IBC`` is a
   string prefix of ``C:\\IBC-Live-Trade`` and ``C:\\IBC-Live-Data``). The re-check at kill
   time refuses any pid whose cmdline no longer matches.
2. NEVER kill the port-4003 owner. The process bound to 4003 IS the live session; it is
   always spared.
3. If the port-4003 owner cannot be determined, or the gateway scan fails, REFUSE TO REAP
   ANYTHING. Doing nothing is always safer than risking the live gateway.
4. BOOT GRACE. A gateway younger than ``BOOT_GRACE_SECS`` is spared even if it is not yet
   bound — it may still be starting up and about to bind 4003. A healthy cold start binds
   well inside this window, so a genuinely healthy boot is never mistaken for an orphan.
5. LOGIN / 2FA GRACE. When NO process yet owns port 4003, an unbound gateway is almost
   always the one-and-only gateway still completing login (typically sitting at the IBKR
   2FA prompt), NOT an orphan — orphans exist only once some gateway has WON the port and a
   loser is left bound to nothing. Such a still-logging-in gateway is spared for the much
   longer ``LOGIN_GRACE_SECS`` window so the reaper can never kill a human's in-progress
   2FA and force a relaunch whose fresh 2FA is killed in turn (the 2026-08-04 reaper-vs-2FA
   thrash: 5 successive logins reaped on the 3-min boot grace, ~53 min of gateway downtime,
   the 08:45 watchdog alert). True orphans — a gateway unbound while ANOTHER already owns
   4003 — are still reaped on the short boot grace, and a genuinely hung unbound gateway is
   still cleaned once past the login window, or the instant a real session binds 4003.
6. DEAD AUTH ends the login grace. The long grace in item 5 exists to protect a PENDING
   2FA. When the gateway's own ``launcher.log`` proves THIS instance's authorization has
   already FAILED (``Authorization failed``, with no later ``Authentication complete``, in
   the window between the process's start and now), no 2FA remains to protect: that process
   can never bind 4003, so it drops back to the short BOOT grace and is reaped promptly.
   Without this, 2026-09-02's dead-auth gateway squatted from 08:14 until the 08:40 sweep —
   straight through the market open. Detection is POSITIVE-ONLY: an unreadable log,
   unparsable stamps, or no decisive event all preserve the full login grace, so item 5's
   protection is never weakened by uncertainty.

NEVER RAISES, NEVER BLOCKS. Every failure is caught and reported in the returned dict;
``main`` always exits 0. This module has NO order path, NO ``ib_async`` import, and NO
knowledge of strategy or PILOT_MODE — zero-transmit like the rest of the pilot.

PURE SEAM: the port-owner probe, the gateway scan, the liveness check, the cmdline lookup
and the kill callable are all injected, so every branch is offline-testable with no real
processes and no PowerShell.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import s8_lock  # noqa: E402  (reuse its dependency-free pid/kill probes — do not duplicate)

LIVE_TRADE_PORT = 4003
# The positive-ID marker: the FULL live-trade install dir. NEVER the bare "C:\IBC" prefix
# (which is a substring of both sibling installs — the 2026-07-23 incident's root cause).
INSTALL_MARKER = r"C:\IBC-Live-Trade"
# A gateway younger than this is spared — it may still be booting toward binding 4003.
BOOT_GRACE_SECS = 180
# When NOBODY yet owns port 4003, a still-unbound gateway is the sole login in progress
# (usually waiting on the IBKR 2FA prompt), not an orphan — spare it this much longer so a
# human has time to answer 2FA. See SAFETY item 5 (the 2026-08-04 reaper-vs-2FA thrash).
LOGIN_GRACE_SECS = 1800
REAP_LOG_NAME = "s8_gateway_reap.log"

# The gateway's OWN launcher log. IBKR writes the decisive auth outcome here; it is the
# only place that distinguishes "2FA still pending" from "2FA already failed".
LAUNCHER_LOG_PATH = os.path.join(INSTALL_MARKER, "GatewaySettings", "launcher.log")
AUTH_FAIL_MARKER = "Authorization failed"
AUTH_OK_MARKER = "Authentication complete"  # also matches IBKR's "Authentication completed."
_LOG_TS_LEN = 23  # "YYYY-MM-DD HH:MM:SS.mmm"
_LOG_TS_FMT = "%Y-%m-%d %H:%M:%S.%f"
# Slack between a process's CreationDate and the log stamps of its own login, absorbing
# small clock skew. Must stay FAR smaller than the gap between a failed login and the next
# relaunch, so one instance's failure can never be pinned on its successor.
AUTH_START_SKEW_SECS = 60

# Tri-state sentinel returned by the port-owner probe when it could not be performed at
# all. Distinct from an int owner pid and from None ("determinate: no listener").
UNKNOWN = "UNKNOWN"


def _cmdline_is_live_trade(cmdline: Optional[str]) -> bool:
    """True iff ``cmdline`` positively identifies a live-trade gateway. A missing/empty
    cmdline is NOT a match — an unidentifiable process is never ours."""
    if not cmdline:
        return False
    return INSTALL_MARKER.lower() in str(cmdline).lower()


def _parse_log_ts(line: str):
    """The leading ``YYYY-MM-DD HH:MM:SS.mmm`` of a launcher-log line, or None."""
    import datetime  # noqa: PLC0415

    try:
        return datetime.datetime.strptime(str(line)[:_LOG_TS_LEN], _LOG_TS_FMT)
    except (ValueError, TypeError):
        return None


def auth_outcome_after(log_text: Optional[str], start_dt, end_dt=None) -> Optional[str]:
    """The LAST decisive auth event at/after ``start_dt``: ``"FAILED"``, ``"OK"``, or None.

    Reading the *last* event (not merely "a failure exists") is what makes this safe for a
    gateway that fails once and then succeeds on a retry inside the SAME process — that
    gateway is healthy and must never be reaped. Undated or pre-``start_dt`` lines belong to
    an earlier instance and are ignored entirely.

    ``end_dt`` bounds the scan at the moment the decision is being made, so the verdict
    rests only on evidence that existed by then. Live, the log simply has no later lines;
    the bound matters when replaying history against a log that SUCCEEDING instances have
    since appended to (all instances share one ``launcher.log``)."""
    if not log_text or start_dt is None:
        return None
    outcome: Optional[str] = None
    for line in str(log_text).splitlines():
        is_fail = AUTH_FAIL_MARKER in line
        is_ok = AUTH_OK_MARKER in line
        if not (is_fail or is_ok):
            continue
        ts = _parse_log_ts(line)
        if ts is None or ts < start_dt:
            continue
        if end_dt is not None and ts > end_dt:
            continue
        outcome = "FAILED" if is_fail else "OK"
    return outcome


def _read_launcher_log(path: str) -> Optional[str]:
    """Best-effort read of the launcher log. None on ANY problem — never raises."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:  # noqa: BLE001 — unreadable log is simply "no evidence"
        return None


def gateway_auth_failed(
    pid: int,
    age_secs: float,
    *,
    log_path: Optional[str] = None,
    now=None,
    read: Callable[[str], Optional[str]] = _read_launcher_log,
    skew_secs: float = AUTH_START_SKEW_SECS,
) -> bool:
    """True ONLY when the launcher log POSITIVELY proves this instance's login already
    failed. Every uncertainty — unreadable log, unparsable stamps, no decisive event —
    returns False, which preserves the full login/2FA grace. Absence of evidence must
    never license a kill."""
    import datetime  # noqa: PLC0415

    try:
        ref = now or datetime.datetime.now()
        start = ref - datetime.timedelta(seconds=float(age_secs) + float(skew_secs))
        text = read(log_path or LAUNCHER_LOG_PATH)
        return auth_outcome_after(text, start, ref) == "FAILED"
    except Exception:  # noqa: BLE001 — never raise, never license a kill
        return False


# --------------------------------------------------------------------------- #
# Default (real) probes — all injected in tests
# --------------------------------------------------------------------------- #

def port_owner_pid(
    port: int = LIVE_TRADE_PORT,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> Any:
    """The pid LISTENING on ``port``: an int owner, ``None`` (determinate: no listener), or
    ``UNKNOWN`` when the probe could not be performed. ``UNKNOWN`` forces a full refusal —
    it is never treated as "no owner" in a way that would license a kill."""
    if os.name != "nt":
        return UNKNOWN
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"$c=Get-NetTCPConnection -LocalPort {int(port)} -State Listen;"
        "if($c){[int](@($c)[0].OwningProcess)}else{Write-Output 'NONE'}"
    )
    try:
        out = run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001 — undeterminable, never a licence to kill
        return UNKNOWN
    text = (getattr(out, "stdout", "") or "").strip()
    if not text:
        return UNKNOWN
    if "NONE" in text:
        return None
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return UNKNOWN


def find_live_trade_gateways(
    *,
    run: Callable[..., Any] = subprocess.run,
) -> Optional[List[Dict[str, Any]]]:
    """Live ``{"pid", "cmdline", "age_secs"}`` dicts for every ``java``/``javaw`` process
    whose command line contains ``INSTALL_MARKER``. ``None`` means the scan could not be
    performed (forces a refusal); ``[]`` is a determinate "none found"."""
    if os.name != "nt":
        return None
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$now=Get-Date;"
        "$p=@(Get-CimInstance Win32_Process -Filter \"Name='java.exe' OR Name='javaw.exe'\");"
        "$m=@($p | Where-Object { $_.CommandLine -and $_.CommandLine -like '*C:\\IBC-Live-Trade*' } |"
        " ForEach-Object { [pscustomobject]@{ pid=$_.ProcessId; cmdline=$_.CommandLine;"
        " age=[double]($now - $_.CreationDate).TotalSeconds } });"
        "if($m.Count -eq 0){Write-Output 'EMPTY'}else{$m | ConvertTo-Json -Compress}"
    )
    try:
        out = run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001 — undeterminable -> refuse (None), never a licence
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


def default_reap_log_path():
    """Off-Drive path for this reaper's OWN log. Honours ``$S8_PILOT_ROOT`` (tests)."""
    import s8_store  # noqa: PLC0415

    return s8_store.get_root() / "logs" / REAP_LOG_NAME


# --------------------------------------------------------------------------- #
# THE REAP — pure logic, every seam injected
# --------------------------------------------------------------------------- #

def reap_orphans(
    *,
    grace_secs: int = BOOT_GRACE_SECS,
    login_grace_secs: int = LOGIN_GRACE_SECS,
    get_owner: Callable[[], Any] = port_owner_pid,
    find_gateways: Callable[[], Optional[List[Dict[str, Any]]]] = find_live_trade_gateways,
    is_alive: Callable[[int], bool] = s8_lock.pid_alive,
    get_cmdline: Callable[[int], Optional[str]] = s8_lock.cmdline_of,
    kill: Callable[[int], bool] = s8_lock.kill_pid,
    auth_failed: Callable[[int, float], bool] = gateway_auth_failed,
    my_pid: Optional[int] = None,
    log: Callable[[str], Any] = print,
) -> Dict[str, Any]:
    """Enforce the port-4003 invariant: reap ``INSTALL_MARKER`` gateways that are not the
    port-4003 owner and are past the boot grace. Never raises.

    Returns::

        {"owner": int|None, "considered": int, "killed": [pid...], "spared": [pid...],
         "refused": [pid...], "aborted": str|None, "error": str|None}
    """
    result: Dict[str, Any] = {
        "owner": None, "considered": 0, "killed": [], "spared": [], "refused": [],
        "aborted": None, "error": None,
    }
    try:
        me = int(my_pid if my_pid is not None else os.getpid())

        owner = get_owner()
        if owner == UNKNOWN:
            result["aborted"] = "port-4003 owner undeterminable"
            log("s8_gateway_reap: REFUSING to reap — cannot determine which process owns "
                "port 4003. Doing nothing is safer than risking the live gateway.")
            return result
        result["owner"] = owner  # int owner pid, or None (no listener)

        gws = find_gateways()
        if gws is None:
            result["aborted"] = "gateway scan failed"
            log("s8_gateway_reap: REFUSING to reap — could not enumerate "
                f"{INSTALL_MARKER} gateway processes.")
            return result
        result["considered"] = len(gws)

        for gw in gws:
            try:
                pid = int(gw.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            try:
                age = float(gw.get("age_secs") or 0.0)
            except (TypeError, ValueError):
                age = 0.0
            if pid <= 0 or pid == me:
                continue

            if owner is not None and pid == owner:
                result["spared"].append(pid)
                log(f"s8_gateway_reap: keeping pid={pid} — it OWNS port 4003 (the live "
                    f"gateway). Never reaped.")
                continue

            # Choose the grace window. When SOME process already owns 4003, any OTHER unbound
            # gateway lost the port-binding race and is a genuine orphan -> the short BOOT
            # grace applies. When NOBODY owns 4003 yet, this is almost certainly the sole
            # gateway still completing login (waiting on the IBKR 2FA prompt); killing it
            # forces a relaunch whose fresh 2FA is killed in turn -- the 2026-08-04
            # reaper-vs-2FA thrash. Give it the much longer LOGIN grace so a human has time to
            # answer 2FA. A truly hung gateway is still reaped once past that window, or the
            # moment a real session binds 4003.
            # The login grace protects an IN-PROGRESS 2FA. Once the gateway's own launcher
            # log proves THIS instance's authorization already FAILED, there is no pending
            # 2FA left to protect: the process can never bind 4003, so holding it for the
            # full 30-minute window only squats the port lane (2026-09-02: a dead-auth
            # gateway sat from 08:14 until the 08:40 sweep, straight through the open).
            # Such a gateway drops back to the short BOOT grace. Detection is positive-only
            # — any doubt keeps the full login grace.
            dead_auth = False
            if owner is None:
                try:
                    dead_auth = bool(auth_failed(pid, age))
                except Exception:  # noqa: BLE001 — probe failure is never a licence to kill
                    dead_auth = False
            if owner is not None or dead_auth:
                effective_grace = grace_secs
            else:
                effective_grace = max(grace_secs, login_grace_secs)
            if age < effective_grace:
                result["spared"].append(pid)
                if owner is None:
                    log(f"s8_gateway_reap: sparing pid={pid} — {age:.0f}s old and NO gateway "
                        f"owns 4003 yet (< {effective_grace}s login/2FA grace); it is likely "
                        f"still completing login. Never kill an in-progress 2FA.")
                else:
                    log(f"s8_gateway_reap: sparing pid={pid} — only {age:.0f}s old "
                        f"(< {grace_secs}s boot grace); it may still be binding 4003.")
                continue

            # Aged, and not the port owner -> orphan candidate. Re-verify positive ID at
            # kill time; an unidentifiable process is never ours.
            if not is_alive(pid):
                log(f"s8_gateway_reap: pid={pid} already gone (nothing to reap).")
                continue
            cmdline = get_cmdline(pid)
            if not _cmdline_is_live_trade(cmdline):
                result["refused"].append(pid)
                log(f"s8_gateway_reap: REFUSING to kill pid={pid} — its command line no "
                    f"longer positively identifies a {INSTALL_MARKER} gateway "
                    f"(cmdline={cmdline!r}).")
                continue
            try:
                ok = bool(kill(pid))
            except Exception as exc:  # noqa: BLE001 — one bad pid must not abort the rest
                result["error"] = f"{type(exc).__name__}: {exc}"
                log(f"s8_gateway_reap: error killing pid={pid} ({result['error']})")
                continue
            why = ("login already FAILED in the launcher log — no 2FA left to protect"
                   if dead_auth else "not bound to 4003")
            log(f"s8_gateway_reap: reaped ORPHAN gateway pid={pid} "
                f"(age={age:.0f}s, {why}) -> {'ok' if ok else 'FAILED'}")
            (result["killed"] if ok else result["refused"]).append(pid)

        return result
    except Exception as exc:  # noqa: BLE001 — the reaper must NEVER raise into a caller
        result["error"] = f"{type(exc).__name__}: {exc}"
        try:
            log(f"s8_gateway_reap: reap failed entirely ({result['error']}); "
                f"nothing was killed and nothing else is affected.")
        except Exception:  # noqa: BLE001
            pass
        return result


def _emit(log_path, line: str) -> None:
    """Best-effort append to the reaper's own log AND stdout. Never raises."""
    print(line)
    try:
        import datetime  # noqa: PLC0415
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(os.path.dirname(str(log_path)), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {line}\n")
    except Exception:  # noqa: BLE001
        pass


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry: run the reap, log to the reaper's own log, ALWAYS exit 0 (a reap failure
    must never fail a teardown or a launch that invokes it)."""
    try:
        log_path = default_reap_log_path()
    except Exception:  # noqa: BLE001
        log_path = None
    res = reap_orphans(log=lambda m: _emit(log_path, m))
    _emit(log_path, f"s8_gateway_reap: done owner={res['owner']} "
                    f"considered={res['considered']} killed={res['killed']} "
                    f"spared={res['spared']} refused={res['refused']} "
                    f"aborted={res['aborted']} error={res['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
