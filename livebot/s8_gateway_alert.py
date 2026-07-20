"""s8_gateway_alert.py — S8 live-pilot GATEWAY DOWN / RELAUNCH EMAIL FAILSAFE.

WHY THIS EXISTS (the failsafe's actual purpose — read this first)
-----------------------------------------------------------------
The S8 live pilot runs against a REAL, funded live account on its own Gateway (port 4003,
IBC auto-login). If that Gateway drops mid-session the desk relaunches it, and IBKR then
pushes a 2FA approval prompt to Andrew's phone — often while he is nowhere near the desk.

The question that prompt raises is a SECURITY question, not a convenience one:
**"is this 2FA push mine, or is someone else logging into my live account?"**

This module is the answer channel. It emails Andrew the instant the desk detects a
mid-session gateway loss and starts a relaunch. So:

  * EMAIL PRESENT  -> the 2FA push is the desk relaunching. It is safe to approve.
  * EMAIL ABSENT   -> **the security signal.** A 2FA prompt with NO matching email means
    something OTHER than this desk launched the gateway. DO NOT APPROVE IT.

The absence of an email is therefore load-bearing, which is why this module also sends a
FOLLOW-UP when the gateway comes back (``send_gateway_back_up_alert``) and an explicit
FAILURE alert when the relaunch never succeeds (``send_gateway_relaunch_failed_alert``).
Silence must never be interpretable as "everything is fine".

OBSERVATIONS ONLY — NEVER A GUESSED ROOT CAUSE
----------------------------------------------
``capture_diagnostics`` reports only what can be directly observed from this machine:
is THIS gateway's JVM alive (discriminated by its own listening port / install dir, so the
paper gateway and the ThetaData terminal cannot be mistaken for it — and UNKNOWN rather
than a guess whenever that cannot be told)? is port 4003 accepting a TCP connection? what exact exception
was seen? what time (CT)? how long since the last known-good connect? The *underlying
cause* — IBKR-side maintenance, a network blip, an IBC restart, the machine sleeping — is
NOT determinable from here, and every email says so in plain words. Guessing a cause in an
alert Andrew reads on his phone would be worse than useless.

DEDUP (why a lock is needed at all)
-----------------------------------
Both all-day pilot processes (``s8_service``, clientId 55, and ``s8_collector``, clientId
56) hold their own connection to the same Gateway, so ONE gateway drop is detected TWICE,
in two separate processes, within seconds. Without coordination that is two identical
emails — noise that erodes the signal this failsafe depends on. So the first process to
detect a drop takes an atomic marker file
(``<state>/gateway_alert.lock``) using the same ``os.open(O_CREAT|O_EXCL|O_WRONLY)`` +
JSON-record + stale-reclaim convention as ``s8_lock.SingleInstanceLock`` and
``ibkr_live_trade.ensure_gateway``'s launch mutex. Any other process that detects the same
outage inside ``ALERT_COOLDOWN_SECS`` silently does nothing. Once the cooldown has elapsed
a genuinely NEW outage alerts again.

RELAUNCH SAFETY
---------------
The relaunch itself is delegated to ``connections.ibkr_live_trade.ensure_gateway()``, which
already carries its own narrow launch mutex and ``RELAUNCH_COOLDOWN_SECS`` — launches
cannot stack no matter how many processes call it. This module adds no launching machinery
of its own.

BEST-EFFORT, ALWAYS (never raises into the caller)
--------------------------------------------------
Alerting is a secondary safety channel. A broken mailer, an unwritable state dir, or an
``ensure_gateway`` that raises must NEVER take the pilot down — ``handle_gateway_down``
catches everything and returns a result dict instead.

ZERO-TRANSMIT: this module has no order path, knows nothing about orders or strategy, and
never touches PILOT_MODE. It only reads observable machine state and sends email.

PURE SEAMS: the mailer, the ``ensure_gateway`` callable, the clock, and both diagnostic
probes are all injectable, so every path here is offline-testable with no real email, no
real broker, and no real gateway.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

_CT_ZONE = ZoneInfo("America/Chicago")

# The live-trading Gateway's port (mirrors connections.clientids.LIVE_TRADE_PORT; kept as a
# plain constant so this module stays importable with no connections/ import at test time).
LIVE_TRADE_PORT = 4003

# DEDUP WINDOW. One gateway outage is detected by BOTH pilot processes within seconds; the
# first to take the marker alerts, everyone else inside this window stays silent. Also
# rate-limits a flapping gateway to at most one email per window. 5 minutes comfortably
# exceeds the detect-both-processes gap while still letting a genuinely later, separate
# outage raise its own alert.
ALERT_COOLDOWN_SECS = 300

ALERT_LOCK_NAME = "gateway_alert.lock"

# Subject prefix — same shape s8_runner._alert_email already uses, so these land in the
# same filter/thread as every other S8 live-pilot alert.
_SUBJECT_PREFIX = "[TradingDesk S8 LIVE]"

# The inverse rule that makes this a real failsafe rather than a convenience notification.
# It appears in EVERY email this module sends, including the follow-ups, because the email
# Andrew happens to open may not be the first one.
_INVERSE_WARNING = (
    "SECURITY RULE: if you get an IBKR 2FA prompt and did NOT receive an email like "
    "this one, DO NOT APPROVE IT. This desk emails you every time it relaunches the "
    "gateway, so an unexplained 2FA push means something else is trying to log into "
    "the live account."
)

_CAUSE_DISCLAIMER = (
    "These are OBSERVATIONS ONLY. The underlying cause (IBKR-side maintenance, a network "
    "problem, an IBC restart, the machine sleeping, something else) is NOT determinable "
    "from this machine and is deliberately NOT guessed here."
)


# --------------------------------------------------------------------------- #
# Default (real) probes and collaborators — all injected in tests
# --------------------------------------------------------------------------- #

def port_listening(port: int = LIVE_TRADE_PORT, host: str = "127.0.0.1",
                   timeout: float = 2.0) -> Optional[bool]:
    """True/False if a TCP connect to ``host:port`` succeeds/refuses; None if undeterminable.

    None is reported honestly as "could not determine" rather than being collapsed into
    False — a fabricated observation is exactly what this module must not produce.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(float(timeout))
            return s.connect_ex((host, int(port))) == 0
    except Exception:  # noqa: BLE001 — undeterminable, not "down"
        return None


def _ps_single_quote(s: str) -> str:
    """Escape a string for embedding in a PowerShell single-quoted literal."""
    return str(s).replace("'", "''")


# THIS Gateway instance's install dir. The live-trading Gateway is a SEPARATE IBC install
# from the paper one (see connections.ibkr_live_trade.GATEWAY_BAT); kept as a plain
# constant so this module stays importable with no connections/ import at test time.
LIVE_TRADE_DIR = r"C:\IBC-Live-Trade"

# Discriminate THIS Gateway's JVM from every other java process on the box (the PAPER
# gateway on 4002, the ThetaData terminal, anything else). Same two-part discriminator
# ``connections.gateway_watchdog._KILL_PS_TEMPLATE`` already uses:
#   * PRIMARY   — the process actually LISTENING on this instance's port, and
#   * SECONDARY — a java/javaw whose CommandLine contains this instance's install dir
#                 (catches the window before it has bound the port).
# ``$all.Count -eq 0`` can only mean the enumeration itself failed, so that path reports
# PROBE_FAILED -> None ("UNKNOWN") rather than a confident, wrong "NO".
_PROBE_PS_TEMPLATE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$dirSubstring = '{dir_substring}'
$all = @(Get-CimInstance Win32_Process)
if ($all.Count -eq 0) {{ Write-Output 'PROBE_FAILED'; exit 0 }}
$gwPid = (Get-NetTCPConnection -LocalPort {port} -State Listen).OwningProcess |
         Select-Object -Unique
$procs = @($all | Where-Object {{
    ($_.Name -eq 'java.exe' -or $_.Name -eq 'javaw.exe') -and
    (
        ($_.ProcessId -in $gwPid) -or
        ($_.CommandLine -match [regex]::Escape($dirSubstring))
    )
}})
@{{ found = [bool]($procs.Count -gt 0); pids = @($procs | ForEach-Object {{ $_.ProcessId }}) }} |
    ConvertTo-Json -Compress
"""


def gateway_process_alive(
    port: int = LIVE_TRADE_PORT,
    dir_substring: str = LIVE_TRADE_DIR,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> Optional[bool]:
    """True if **THIS** Gateway's JVM appears to be running; None if undeterminable.

    Sharpened (was: any ``javaw.exe``/``java.exe``, which could not tell this Gateway's JVM
    apart from the paper gateway's or the ThetaData terminal's). A java/javaw process now
    counts only if it is the one LISTENING on this instance's port, or its command line
    contains this instance's install dir — the same port + install-dir discriminator
    ``connections.gateway_watchdog`` uses to scope its kill to one Gateway instance.

    Honest by construction: if the probe cannot answer — not Windows, PowerShell failed,
    the process enumeration came back empty, unparsable output — it returns **None**, which
    renders as "UNKNOWN (could not be determined)". A confident wrong answer in an alert
    Andrew reads on his phone would be worse than no answer.

    NOT a hot path: this runs only when an alert actually fires, which is rare, so the
    PowerShell spawn is affordable here and is never taken during normal operation.
    """
    if os.name != "nt":
        return None
    ps = _PROBE_PS_TEMPLATE.format(port=int(port),
                                   dir_substring=_ps_single_quote(dir_substring))
    try:
        out = run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001 — undeterminable, not "down"
        return None
    text = (getattr(out, "stdout", "") or "").strip()
    if not text or "PROBE_FAILED" in text:
        return None
    try:
        parsed = json.loads(text.splitlines()[-1].strip())
    except (ValueError, TypeError, IndexError):
        return None
    if not isinstance(parsed, dict) or "found" not in parsed:
        return None
    return bool(parsed.get("found"))


# --------------------------------------------------------------------------- #
# DISCONNECT DETAIL RECORDER — where "the exact error observed" actually comes from
#
# WHY THIS EXISTS. The pilot loops do NOT learn about a mid-session drop by catching an
# exception: they poll ``ib.isConnected()``. By the time that poll returns False the only
# party that ever saw a reason is the IB API client itself, which logs it ("Peer closed
# connection.") and emits it on ``ib.client.apiError``. Nothing was catching that emit, so
# ``handle_gateway_down`` was called with ``error=None`` and the single most useful field in
# the email rendered "UNKNOWN (could not be determined)" — even though the process knew the
# reason. This recorder subscribes to that event so the real string is available at alert
# time.
# --------------------------------------------------------------------------- #

# Named so the email can say WHICH source was consulted rather than a bare "UNKNOWN".
DISCONNECT_DETAIL_SOURCE = "the IB API client's own apiError/disconnect message"


class DisconnectDetailRecorder:
    """Remembers the LAST message the IB API itself reported. Best-effort, never raises.

    Callable so it can be used directly as an ``ib.client.apiError`` handler. ``reset()`` is
    called on a successful (re)connect so a stale message from an EARLIER outage can never
    be presented as this outage's reason.
    """

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self.message: Optional[str] = None
        self.recorded_at: Optional[float] = None
        self.attach_error: Optional[str] = None

    def __call__(self, msg=None, *_a, **_k) -> None:
        try:
            text = str(msg).strip() if msg is not None else ""
            if not text:
                return
            self.message = text
            self.recorded_at = float(self._now())
        except Exception:  # noqa: BLE001 — a recorder must never disturb the API loop
            pass

    def reset(self) -> None:
        self.message = None
        self.recorded_at = None

    def detail_source(self) -> str:
        """What was consulted for the error string — named either way, so a missing detail
        is an HONEST "checked X, it had nothing", never a bare UNKNOWN."""
        if self.attach_error:
            return (f"{DISCONNECT_DETAIL_SOURCE} could not be subscribed to "
                    f"({self.attach_error})")
        if self.message:
            return DISCONNECT_DETAIL_SOURCE
        return (f"{DISCONNECT_DETAIL_SOURCE} was checked and reported nothing; the drop was "
                f"detected by polling ib.isConnected(), which raises no exception")


def attach_disconnect_recorder(ib, *, recorder: Optional[DisconnectDetailRecorder] = None,
                               now: Callable[[], float] = time.time,
                               log: Callable[[str], Any] = print) -> DisconnectDetailRecorder:
    """Subscribe a recorder to ``ib.client.apiError``. Never raises — on failure the
    returned recorder reports (honestly) that it could not be attached."""
    rec = recorder if recorder is not None else DisconnectDetailRecorder(now=now)
    try:
        ib.client.apiError += rec
        rec.attach_error = None
    except Exception as exc:  # noqa: BLE001
        rec.attach_error = f"{type(exc).__name__}: {exc}"
        try:
            log(f"s8_gateway_alert: could not subscribe to ib.client.apiError "
                f"({rec.attach_error}); the alert will say so rather than claim UNKNOWN.")
        except Exception:  # noqa: BLE001
            pass
    return rec


def _default_mailer():
    """The EXISTING dailyreport mailer — the same module/path ``s8_runner._alert_email``
    uses. Imported lazily (with the repo-derived sys.path shim the other livebot modules
    use) so the offline tests never touch mail config or credentials. No new mail config
    and no new credentials are introduced by this module."""
    import sys

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(repo_root, "dailyreport")
    if p not in sys.path:
        sys.path.insert(0, p)
    import mailer  # noqa: PLC0415

    return mailer


def _default_ensure_gateway() -> bool:
    """Relaunch via the EXISTING machinery: ``ibkr_live_trade.ensure_gateway()`` already
    holds a narrow launch mutex plus ``RELAUNCH_COOLDOWN_SECS``, so concurrent callers can
    never stack Gateway launches. Nothing is reimplemented here."""
    import sys

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(repo_root, "connections")
    if p not in sys.path:
        sys.path.insert(0, p)
    from connections import ibkr_live_trade  # noqa: PLC0415

    return bool(ibkr_live_trade.ensure_gateway())


def default_lock_path():
    """Off-Drive dedup-marker path under the pilot store's state dir.

    Derived from ``s8_store.get_root()`` so it honours ``$S8_PILOT_ROOT`` (tests) and is
    NEVER a My Drive path — same convention as ``s8_lock.default_lock_path``.
    """
    import s8_store  # noqa: PLC0415

    return s8_store.get_root() / "state" / ALERT_LOCK_NAME


# --------------------------------------------------------------------------- #
# DIAGNOSTICS — observable facts only
# --------------------------------------------------------------------------- #

def capture_diagnostics(
    *,
    source: str = "s8",
    error: Optional[BaseException] = None,
    detail: Optional[str] = None,
    detail_source: Optional[str] = None,
    last_connect_ok_ts: Optional[float] = None,
    port: int = LIVE_TRADE_PORT,
    now: Callable[[], float] = time.time,
    ct_now: Optional[Callable[[], datetime]] = None,
    probe_port: Callable[..., Optional[bool]] = port_listening,
    probe_process: Callable[[], Optional[bool]] = gateway_process_alive,
) -> Dict[str, Any]:
    """Snapshot what can be OBSERVED about the gateway right now. Never raises.

    Every value here is a direct observation or a verbatim record of the exception that was
    seen. There is deliberately NO ``cause`` field and no inference: the returned dict
    carries ``observations_only=True`` and ``cause="NOT DETERMINABLE FROM THIS MACHINE"`` so
    a reader (and the email body) cannot mistake this for a diagnosis.

    Fields:
      ``source``                    which pilot process observed the loss
      ``observed_at_ct``            CT wall-clock timestamp of the observation
      ``observed_at_epoch``         the same instant as an epoch float
      ``port`` / ``port_listening`` is 4003 accepting a TCP connection? (None = unknown)
      ``gateway_process_alive``     is THIS Gateway's JVM visible — discriminated by its
                                    listening port / install dir, not just "some java"?
                                    (None = unknown)
      ``error_type`` / ``error``    the exact exception type + message observed, or None
      ``error_detail_source``       WHAT was consulted for that string — so a missing error
                                    is "checked X, it had nothing", never a bare UNKNOWN
      ``seconds_since_last_connect``  seconds since the last known-good connect, or None

    Two independent ways the error string can arrive, because the two detection paths
    differ: ``error`` is an exception actually caught at the detection point, while
    ``detail`` is a plain string the IB API itself reported (e.g. "Peer closed connection.",
    captured by ``DisconnectDetailRecorder``). The mid-session loops poll
    ``ib.isConnected()`` and therefore have NO exception — ``detail`` is the only path that
    can carry the real reason for them.
    """
    ct = None
    try:
        ct = (ct_now() if ct_now is not None else datetime.now(tz=_CT_ZONE))
    except Exception:  # noqa: BLE001
        ct = None

    try:
        epoch = float(now())
    except Exception:  # noqa: BLE001
        epoch = None  # type: ignore[assignment]

    try:
        listening = probe_port(port)
    except Exception:  # noqa: BLE001
        listening = None

    try:
        proc = probe_process()
    except Exception:  # noqa: BLE001
        proc = None

    since = None
    if last_connect_ok_ts is not None and epoch is not None:
        try:
            since = max(0.0, round(float(epoch) - float(last_connect_ok_ts), 1))
        except (TypeError, ValueError):
            since = None

    # The error string, from whichever source actually had it. An exception caught at the
    # detection point wins; otherwise the IB API's own reported message; otherwise NEITHER,
    # and we name what was consulted instead of printing a bare "UNKNOWN".
    err_type: Optional[str] = None
    err_text: Optional[str] = None
    if error is not None:
        err_type = type(error).__name__
        err_text = f"{error}"
        src = detail_source or "an exception caught at the detection point"
    else:
        try:
            txt = str(detail).strip() if detail is not None else ""
        except Exception:  # noqa: BLE001
            txt = ""
        if txt:
            err_text = txt
            src = detail_source or DISCONNECT_DETAIL_SOURCE
        else:
            src = detail_source or (
                "no exception was raised (the drop is detected by polling "
                "ib.isConnected()) and no IB API disconnect message was recorded")

    return {
        "source": str(source),
        "observations_only": True,
        "cause": "NOT DETERMINABLE FROM THIS MACHINE",
        "observed_at_ct": (ct.strftime("%Y-%m-%d %H:%M:%S %Z") if ct is not None else None),
        "observed_at_epoch": epoch,
        "port": int(port),
        "port_listening": listening,
        "gateway_process_alive": proc,
        "error_type": err_type,
        "error": err_text,
        "error_detail_source": src,
        "seconds_since_last_connect": since,
    }


# --------------------------------------------------------------------------- #
# STATE CLASSIFICATION — say plainly WHAT dropped, derived only from the two probes
#
# The old body said the gateway "dropped" while the same email reported "JVM alive YES /
# port 4003 YES", which reads as a contradiction. It is not one: it is the ordinary case of
# the API SESSION being lost while the Gateway PROCESS keeps running. That distinction is
# fully derivable from the two probes already collected, so it is stated outright. No cause
# is inferred — only which of the two things went away.
# --------------------------------------------------------------------------- #

STATE_SESSION_DROPPED = "API_SESSION_DROPPED_PROCESS_ALIVE"
STATE_PROCESS_GONE = "GATEWAY_PROCESS_GONE"
STATE_PROCESS_UP_PORT_CLOSED = "PROCESS_ALIVE_PORT_CLOSED"
STATE_PORT_OPEN_PROCESS_NOT_FOUND = "PORT_OPEN_PROCESS_NOT_FOUND"
STATE_UNDETERMINED = "UNDETERMINED"


def classify_gateway_state(diagnostics: Dict[str, Any]) -> Dict[str, str]:
    """Turn the JVM-alive + port probes into ONE unambiguous sentence about what was lost.

    Returns ``{"key", "headline", "explanation"}``. Purely mechanical: it restates the two
    observations, it never guesses WHY either of them is what it is.
    """
    d = diagnostics or {}
    proc = d.get("gateway_process_alive")
    listening = d.get("port_listening")
    port = d.get("port", LIVE_TRADE_PORT)

    if proc is None or listening is None:
        unknown = []
        if proc is None:
            unknown.append("whether this Gateway's JVM is alive")
        if listening is None:
            unknown.append(f"whether port {port} is accepting TCP")
        return {
            "key": STATE_UNDETERMINED,
            "headline": "THE API SESSION WAS LOST. Whether the gateway PROCESS is still up "
                        "could not be determined.",
            "explanation": ("The pilot's API connection to the gateway went away — that much "
                            "is certain, it is what triggered this email. But the probe(s) "
                            "for " + " and ".join(unknown) + " could not answer, so this "
                            "email cannot tell you whether the Gateway process itself is "
                            "still running."),
        }
    if proc and listening:
        return {
            "key": STATE_SESSION_DROPPED,
            "headline": "THE API SESSION DROPPED — THE GATEWAY PROCESS IS STILL UP.",
            "explanation": (f"This Gateway's JVM is still running and port {port} is still "
                            f"accepting TCP connections, but the pilot's API session to it "
                            f"was lost. So what went away is the API CONNECTION, not the "
                            f"Gateway program. Reconnecting may or may not need a fresh "
                            f"login — that is why a relaunch (and possibly a 2FA push) can "
                            f"still follow even though the process never died."),
        }
    if proc and not listening:
        return {
            "key": STATE_PROCESS_UP_PORT_CLOSED,
            "headline": f"THE GATEWAY PROCESS IS STILL UP BUT PORT {port} IS NOT ACCEPTING "
                        f"CONNECTIONS.",
            "explanation": (f"This Gateway's JVM is still running, but nothing is answering "
                            f"on port {port}, so the pilot cannot reach the API. The process "
                            f"is alive and NOT serving."),
        }
    if (not proc) and listening:
        return {
            "key": STATE_PORT_OPEN_PROCESS_NOT_FOUND,
            "headline": f"PORT {port} IS ACCEPTING CONNECTIONS BUT THIS GATEWAY'S JVM WAS "
                        f"NOT FOUND.",
            "explanation": (f"The two probes disagree: something is listening on port {port} "
                            f"while the process scan did not match this Gateway's JVM. Both "
                            f"observations are reported as seen; no attempt is made here to "
                            f"reconcile them."),
        }
    return {
        "key": STATE_PROCESS_GONE,
        "headline": "THE GATEWAY PROCESS IS GONE — THE JVM IS NOT RUNNING AND THE PORT IS "
                    "CLOSED.",
        "explanation": (f"This Gateway's JVM was not found and port {port} is refusing "
                        f"connections. The Gateway program itself is down, not just the API "
                        f"session."),
    }


def _fmt(value: Any) -> str:
    """Render an observation for the email, keeping "unknown" visibly distinct from False."""
    if value is None:
        return "UNKNOWN (could not be determined)"
    if value is True:
        return "YES"
    if value is False:
        return "NO"
    return str(value)


def _format_error_line(diagnostics: Dict[str, Any]) -> str:
    """The "exact error observed" line.

    An exception renders as ``Type: message``; an IB-API-reported string renders on its own
    (there is no exception type for it, and inventing one would be a fabrication). When
    NEITHER source had anything, the line names the source that was consulted and came up
    empty — an honest "checked X, nothing there" instead of a bare UNKNOWN.
    """
    d = diagnostics or {}
    text = d.get("error")
    etype = d.get("error_type")
    src = d.get("error_detail_source")
    if text and etype:
        line = f"{etype}: {text}"
    elif text:
        line = str(text)
    else:
        return ("NONE RECORDED — " + (str(src) if src
                                      else "no error source was consulted"))
    return line + (f"   [source: {src}]" if src else "")


def format_diagnostics_lines(diagnostics: Dict[str, Any]) -> list:
    """The OBSERVED-FACTS block shared by every email this module sends."""
    d = diagnostics or {}
    state = classify_gateway_state(d)
    return [
        "WHAT HAPPENED (stated from the two probes below, nothing else):",
        f"  {state['headline']}",
        f"  {state['explanation']}",
        "",
        "OBSERVED (facts only — no cause is inferred):",
        f"  detected by ............... {d.get('source')}",
        f"  time (CT) ................. {_fmt(d.get('observed_at_ct'))}",
        f"  THIS gateway's JVM alive? . {_fmt(d.get('gateway_process_alive'))}",
        f"  port {d.get('port', LIVE_TRADE_PORT)} accepting TCP? ..... "
        f"{_fmt(d.get('port_listening'))}",
        f"  exact error observed ...... {_format_error_line(d)}",
        f"  seconds since last good connect: "
        f"{_fmt(d.get('seconds_since_last_connect'))}",
        "",
        _CAUSE_DISCLAIMER,
    ]


# --------------------------------------------------------------------------- #
# DEDUP MARKER — atomic O_CREAT|O_EXCL + stale reclaim (s8_lock / ensure_gateway pattern)
# --------------------------------------------------------------------------- #

def _read_record(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            rec = json.loads(f.read() or "{}")
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


def acquire_alert_marker(
    event_id: str,
    *,
    path=None,
    now: Callable[[], float] = time.time,
    cooldown_secs: float = ALERT_COOLDOWN_SECS,
    log: Callable[[str], Any] = print,
) -> bool:
    """True if THIS process should send the down-alert for this outage; False to stay quiet.

    Atomic ``os.open(O_CREAT|O_EXCL|O_WRONLY)`` — exactly one of N racing detectors can win.
    A marker younger than ``cooldown_secs`` means somebody already alerted for this same
    outage, so we return False regardless of ``event_id``: the two pilot processes cannot
    agree on an id for a drop they each noticed independently, so RECENCY (not id equality)
    is the correct dedup key. A marker older than the cooldown is stale — reclaimed, and a
    genuinely new outage alerts again.

    Never raises. A filesystem problem FAILS OPEN (returns True): an extra email is a far
    smaller harm than a silent gateway relaunch, because silence is what tells Andrew a 2FA
    push is NOT ours.
    """
    p = str(path if path is not None else default_lock_path())
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
    except OSError as exc:
        log(f"s8_gateway_alert: cannot prepare state dir ({exc!r}); alerting anyway")
        return True

    for _attempt in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            rec = _read_record(p)
            try:
                age = float(now()) - float((rec or {}).get("alerted_at") or 0.0)
            except (TypeError, ValueError):
                age = float(cooldown_secs) + 1.0  # unreadable -> treat as stale
            if rec is not None and age < float(cooldown_secs):
                log(f"s8_gateway_alert: another process already alerted "
                    f"{age:.0f}s ago (event={rec.get('event_id')!r}) — suppressing a "
                    f"duplicate email for the same outage.")
                return False
            try:
                os.unlink(p)      # stale marker -> reclaim and retry the atomic create
            except OSError:
                return True       # fail OPEN: better a duplicate than silence
            continue
        except OSError as exc:
            log(f"s8_gateway_alert: marker error ({exc!r}); alerting anyway")
            return True
        else:
            try:
                os.write(fd, json.dumps({
                    "event_id": str(event_id),
                    "alerted_at": float(now()),
                    "pid": os.getpid(),
                }).encode("utf-8"))
            finally:
                os.close(fd)
            return True
    return True


# --------------------------------------------------------------------------- #
# EMAILS — all through the existing dailyreport mailer (no new config/credentials)
# --------------------------------------------------------------------------- #

def _send(subject: str, lines: list, mailer=None, log: Callable[[str], Any] = print) -> bool:
    """Send one alert through ``dailyreport.mailer.send_html`` — the same helper and the
    same credentials ``s8_runner._alert_email`` already uses. Never raises."""
    try:
        m = mailer if mailer is not None else _default_mailer()
        html = "<html><body><pre>" + "\n".join(str(x) for x in lines) + "</pre></body></html>"
        return bool(m.send_html(f"{_SUBJECT_PREFIX} {subject}", html))
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort, always
        log(f"s8_gateway_alert: alert email itself failed ({type(exc).__name__}: {exc})")
        return False


def send_gateway_down_alert(diagnostics: Dict[str, Any], relaunching: bool,
                            mailer=None, log: Callable[[str], Any] = print) -> bool:
    """The immediate "gateway dropped, the desk is relaunching it" email.

    This is the message whose PRESENCE authorises the 2FA push Andrew is about to receive.
    """
    d = diagnostics or {}
    port = d.get("port", LIVE_TRADE_PORT)
    state = classify_gateway_state(d)
    # Lead with WHAT was lost. "dropped" alone alongside "JVM alive YES / port YES" reads as
    # a contradiction; the classification says which of the two actually went away.
    opening = (f"The S8 live pilot LOST ITS CONNECTION to the live-trading gateway "
               f"(port {port}) MID-SESSION.")
    if relaunching:
        subject = "GATEWAY DOWN - relaunching, approve the 2FA"
        head = [
            opening,
            "",
            state["headline"],
            state["explanation"],
            "",
            "THE DESK IS RELAUNCHING THE GATEWAY NOW. THIS RELAUNCH WAS INITIATED BY THE "
            "DESK. Expect an IBKR Mobile 2FA push within about a minute — it is ours, and "
            "you can approve it.",
        ]
    else:
        subject = "GATEWAY DOWN - no relaunch attempted"
        head = [
            opening,
            "",
            state["headline"],
            state["explanation"],
            "",
            "NO relaunch was attempted by the desk, so you should NOT expect a 2FA push.",
        ]
    lines = head + ["", *format_diagnostics_lines(diagnostics), "",
                    _INVERSE_WARNING, "",
                    "Zero-transmit is unaffected: the pilot is read-only (PILOT_MODE=True) "
                    "and has no order path."]
    return _send(subject, lines, mailer=mailer, log=log)


def send_gateway_back_up_alert(diagnostics: Dict[str, Any], seconds_down: float,
                               mailer=None, log: Callable[[str], Any] = print) -> bool:
    """FOLLOW-UP: the gateway is serving again, and for how long it was down.

    Sent so the down-alert is never left dangling — an unresolved alert would leave Andrew
    unable to tell "recovered" from "still broken and nobody said so".
    """
    try:
        secs = f"{float(seconds_down):.0f}"
    except (TypeError, ValueError):
        secs = "?"
    lines = [
        f"The S8 live-pilot gateway is BACK UP and serving again after {secs}s down.",
        "",
        "This closes out the GATEWAY DOWN alert you just received. No action needed.",
        "",
        *format_diagnostics_lines(diagnostics),
        "",
        "(The block above is the state OBSERVED AT THE MOMENT OF THE DROP, kept for the "
        "record — it is not the current state.)",
        "",
        _INVERSE_WARNING,
    ]
    return _send(f"gateway back up after {secs}s", lines, mailer=mailer, log=log)


def send_gateway_relaunch_failed_alert(diagnostics: Dict[str, Any], seconds_down: float,
                                       reason: Optional[str] = None,
                                       mailer=None, log: Callable[[str], Any] = print) -> bool:
    """FOLLOW-UP: the relaunch did NOT succeed. Say so plainly.

    Silence must never mean "fine" — if the desk cannot bring the gateway back, that fact
    gets its own email rather than being inferred from a missing follow-up.
    """
    try:
        secs = f"{float(seconds_down):.0f}"
    except (TypeError, ValueError):
        secs = "?"
    lines = [
        "The S8 live-pilot gateway RELAUNCH FAILED. The gateway did NOT come back up "
        f"after {secs}s of trying.",
        "",
        "The pilot is NOT collecting live data right now and needs a look. Nothing is at "
        "risk financially (zero-transmit, read-only, PILOT_MODE=True) — this is a DATA "
        "outage, not a trading one.",
    ]
    if reason:
        lines += ["", f"Relaunch reported: {reason}"]
    lines += ["", *format_diagnostics_lines(diagnostics), "",
              "Because the relaunch failed, you may or may not see a 2FA push.",
              _INVERSE_WARNING]
    return _send("GATEWAY RELAUNCH FAILED - still down", lines, mailer=mailer, log=log)


# --------------------------------------------------------------------------- #
# ORCHESTRATOR — the single entrypoint both pilot processes call
# --------------------------------------------------------------------------- #

def handle_gateway_down(
    source: str,
    *,
    error: Optional[BaseException] = None,
    detail: Optional[str] = None,
    detail_source: Optional[str] = None,
    recorder: Optional["DisconnectDetailRecorder"] = None,
    last_connect_ok_ts: Optional[float] = None,
    port: int = LIVE_TRADE_PORT,
    mailer=None,
    ensure_gateway: Optional[Callable[[], bool]] = None,
    clock: Callable[[], float] = time.time,
    lock_path=None,
    cooldown_secs: float = ALERT_COOLDOWN_SECS,
    probe_port: Callable[..., Optional[bool]] = port_listening,
    probe_process: Callable[[], Optional[bool]] = gateway_process_alive,
    log: Callable[[str], Any] = print,
) -> Dict[str, Any]:
    """Capture -> dedup -> alert -> relaunch -> follow up. **NEVER raises into the caller.**

    Order matters: the DOWN email goes out BEFORE the relaunch is started, so the email
    reliably beats (or at worst races closely with) the 2FA push it authorises.

    Relaunching is delegated to ``ibkr_live_trade.ensure_gateway`` (its own launch mutex +
    relaunch cooldown means concurrent callers cannot stack Gateway launches). The result
    decides which follow-up is sent: back-up, or relaunch-failed. Both are sent — there is
    no path here that alerts on the way down and then goes quiet.

    Returns a result dict (``alerted`` / ``deduped`` / ``relaunched`` / ``seconds_down`` /
    ``diagnostics`` / ``error``) for logging and tests. Every failure mode — a raising
    mailer, a raising ``ensure_gateway``, an unwritable marker — is swallowed and reported
    in that dict instead of propagating: best-effort alerting must not take the pilot down.
    """
    result: Dict[str, Any] = {
        "alerted": False, "deduped": False, "relaunched": None,
        "seconds_down": None, "diagnostics": None, "error": None,
    }
    try:
        # A ``recorder`` (subscribed to the IB API's own apiError/disconnect message) is the
        # ONLY thing that knows the reason on the polling detection path, where no exception
        # is ever raised. Explicit detail/detail_source still win if a caller passes them.
        if recorder is not None:
            try:
                if detail is None:
                    detail = recorder.message
                if detail_source is None:
                    detail_source = recorder.detail_source()
            except Exception:  # noqa: BLE001 — a bad recorder must not break alerting
                pass
        diag = capture_diagnostics(
            source=source, error=error, detail=detail, detail_source=detail_source,
            last_connect_ok_ts=last_connect_ok_ts,
            port=port, now=clock, probe_port=probe_port, probe_process=probe_process,
        )
        result["diagnostics"] = diag

        event_id = f"{source}:{diag.get('observed_at_epoch')}"
        if not acquire_alert_marker(event_id, path=lock_path, now=clock,
                                    cooldown_secs=cooldown_secs, log=log):
            # Another process already emailed for this same outage — one drop, one email.
            result["deduped"] = True
            return result

        result["alerted"] = send_gateway_down_alert(diag, True, mailer=mailer, log=log)

        started = clock()
        try:
            fn = ensure_gateway if ensure_gateway is not None else _default_ensure_gateway
            came_up = bool(fn())
            reason = None
        except Exception as exc:  # noqa: BLE001 — a failed relaunch is an ALERT, not a crash
            came_up = False
            reason = f"{type(exc).__name__}: {exc}"
            log(f"s8_gateway_alert: ensure_gateway raised ({reason})")
        seconds_down = max(0.0, float(clock()) - float(started))
        result["relaunched"] = came_up
        result["seconds_down"] = seconds_down

        if came_up:
            send_gateway_back_up_alert(diag, seconds_down, mailer=mailer, log=log)
        else:
            send_gateway_relaunch_failed_alert(diag, seconds_down, reason=reason,
                                               mailer=mailer, log=log)
        return result
    except Exception as exc:  # noqa: BLE001 — alerting can NEVER sink the pilot
        result["error"] = f"{type(exc).__name__}: {exc}"
        try:
            log(f"s8_gateway_alert: handle_gateway_down failed entirely "
                f"({result['error']}); the pilot continues unaffected.")
        except Exception:  # noqa: BLE001
            pass
        return result
