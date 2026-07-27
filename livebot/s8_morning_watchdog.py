"""s8_morning_watchdog.py — S8 live-pilot MORNING "STILL DOWN" WATCHDOG.

WHY THIS EXISTS (read this first)
--------------------------------
The S8 live pilot runs against a REAL, funded live account on its own Gateway (port 4003,
IBC auto-login). The scheduled ``LiveTradeGatewayOpen`` task starts trying to bring that
Gateway up at ~08:00 CT and keeps retrying every ~10 min, and each retry re-pushes an IBKR
Mobile 2FA approval to Andrew's phone. If Andrew is away from his phone in the morning,
those pushes can sit unapproved and the Gateway never comes up — so the whole desk (S8
entry + exit monitoring, the intraday collector) silently has no live connection when the
session opens.

This module is the LAST-CHANCE nudge. It is a ONE-SHOT check, run once at ~08:45 CT on a
trading day. If the Gateway's port 4003 is STILL not accepting connections by then, it
emails Andrew ONE alert: "the gateway is still down — if you have an unapproved IBKR Mobile
push, it's the desk's, approve it; if there is NO push, investigate." That closes the gap
between "the retries are quietly failing" and Andrew finding out only when the day's data is
already missing.

It is a companion to ``s8_gateway_alert`` (the MID-SESSION down/relaunch failsafe), and it
deliberately REUSES that module's machinery — the diagnostics capture, the observed-facts
formatter, the state classification, the inverse-2FA security warning, and the mailer send
path — rather than reimplementing any of it. No new mail config and no new credentials are
introduced; email goes out through the same existing ``dailyreport`` mailer every other S8
alert uses.

BEST-EFFORT, ALWAYS (never raises)
----------------------------------
Alerting is a secondary safety channel. A broken mailer, an unwritable path, a probe that
explodes — none of it may take anything down. ``check_and_alert`` catches everything and
returns a result dict; ``main`` always returns rc 0.

ZERO-TRANSMIT: this module has no order path, knows nothing about orders or strategy, and
never touches PILOT_MODE. It only reads observable machine state (is port 4003 listening?)
and sends email.

PURE SEAMS: the port probe, the trading-day calendar, the clock, the diagnostics capture,
the send path, and the CT-date function are all injectable, so every path here is
offline-testable with no real network, no real mail, and no real scheduler. ``s8_service``
is imported LAZILY inside the functions (not at module top) so the module import stays light
and the tests never pull in ib_async.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

# Self-contained sys.path shim — same rationale as the sibling livebot modules: the venv
# editable installs still point at the deleted pre-2026-07-16 My Drive path, so make this
# module's own directory importable so ``import s8_gateway_alert`` / ``import s8_service``
# resolve against the repo's real files regardless of the broken editable installs.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import s8_gateway_alert  # noqa: E402  (reused verbatim — diagnostics, formatter, send, warning)

_CT_ZONE = ZoneInfo("America/Chicago")

# The live-trading Gateway's port — reused from the mid-session failsafe so the two stay in
# lockstep (mirrors connections.clientids.LIVE_TRADE_PORT).
LIVE_TRADE_PORT = s8_gateway_alert.LIVE_TRADE_PORT


def _default_send(subject: str, lines: list, mailer=None,
                  log: Callable[[str], Any] = print) -> bool:
    """Thin wrapper over ``s8_gateway_alert._send`` — one alert through the existing
    dailyreport mailer, same credentials/subject-prefix as every other S8 alert. Never
    raises (the underlying ``_send`` swallows everything)."""
    return s8_gateway_alert._send(subject, lines, mailer=mailer, log=log)


def _default_ct_date() -> str:
    """Today's CT date as YYYYMMDD, via ``s8_service.current_ct_date`` (imported lazily so
    this module's import never pulls ib_async in)."""
    import s8_service  # noqa: PLC0415
    return s8_service.current_ct_date()


def check_and_alert(
    *,
    probe_port: Callable[..., Optional[bool]] = s8_gateway_alert.port_listening,
    is_trading_day: Optional[Callable[[Any], bool]] = None,
    today: Any = None,
    mailer=None,
    capture: Callable[..., Dict[str, Any]] = s8_gateway_alert.capture_diagnostics,
    send: Callable[..., bool] = _default_send,
    ct_date: Callable[[], str] = _default_ct_date,
    log: Callable[[str], Any] = print,
) -> Dict[str, Any]:
    """One-shot morning check: is the S8 live-trading Gateway STILL down at ~08:45 CT?

    Decision, in order:
      a) TRADING-DAY GUARD. If today is not a trading day (weekend/holiday), do nothing and
         send no email — a Gateway that is "down" on a closed market is expected, not an
         alert. Returns ``{"acted": "skipped_non_trading_day", "alerted": False}``.
      b) UP -> QUIET. If ``probe_port(4003)`` returns True the Gateway is serving; the
         morning start worked, so no alert. Returns ``{"acted": "up_no_alert", ...}``.
      c) NOT CONFIRMED UP -> ALERT ONCE. If the probe is False (refused) OR None (could not
         be determined), the Gateway is NOT confirmed up, which at 08:45 is a problem worth
         a nudge. Captures diagnostics and sends ONE email. Returns
         ``{"acted": "alerted", "alerted": True, "port_listening": <up>}``.

    NEVER raises — the whole body is wrapped; on any failure it logs and returns
    ``{"acted": "error", "alerted": False, "error": ...}``.
    """
    try:
        # (a) Trading-day guard — reuse s8_service.resolve_trading_day's pure seam (date +
        # calendar callable injected; fails OPEN on any calendar glitch, i.e. assumes a
        # trading day, so a calendar problem never silently suppresses a real alert).
        import s8_service  # noqa: PLC0415
        if resolve_trading_day := getattr(s8_service, "resolve_trading_day", None):
            trading = resolve_trading_day(today, is_trading_day, log=log)
        else:  # pragma: no cover — defensive; s8_service always has it
            trading = True
        if trading is False:
            log("s8_morning_watchdog: not a trading day; no check.")
            return {"acted": "skipped_non_trading_day", "alerted": False}

        # (b) Is the gateway up? True only if the port is genuinely accepting connections.
        try:
            up = probe_port(LIVE_TRADE_PORT)
        except Exception:  # noqa: BLE001 — a broken probe is UNKNOWN, not confirmed-up
            up = None
        if up is True:
            log("s8_morning_watchdog: gateway 4003 is UP; no alert.")
            return {"acted": "up_no_alert", "alerted": False}

        # (c) Down (False) or unknown (None) — NOT confirmed up. Send one alert.
        diag = capture(source="s8_morning_watchdog", probe_port=probe_port)
        try:
            now_ct = datetime.now(tz=_CT_ZONE).strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:  # noqa: BLE001
            now_ct = "(CT time unavailable)"

        subject = "STILL DOWN at 08:45 CT - approve the 2FA or investigate"
        lines = [
            f"The S8 live-trading gateway (port {LIVE_TRADE_PORT}) is STILL NOT UP as of "
            f"{now_ct}.",
            "",
            "The LiveTradeGatewayOpen task has been retrying every ~10 min since ~08:00 CT, "
            "and each retry re-pushes an IBKR Mobile 2FA approval to your phone.",
            "",
            "WHAT TO DO:",
            "  * If you have an UNAPPROVED IBKR Mobile push waiting, it is THIS DESK's — "
            "approve it to bring S8 up.",
            "  * If there is NO push waiting, something else is wrong — investigate (Gateway "
            "not launching, IBC/2FA loop, machine asleep, etc.).",
            "",
            *s8_gateway_alert.format_diagnostics_lines(diag),
            "",
            s8_gateway_alert._INVERSE_WARNING,
            "",
            "Zero-transmit is unaffected: the pilot is read-only (PILOT_MODE=True) and has "
            "no order path. This is a DATA/connectivity nudge, not a trading one.",
        ]
        sent = send(subject, lines, mailer=mailer, log=log)
        log(f"s8_morning_watchdog: gateway 4003 NOT confirmed up "
            f"(port_listening={up!r}); alert sent={sent!r}.")
        return {"acted": "alerted", "alerted": True, "port_listening": up}
    except Exception as exc:  # noqa: BLE001 — a watchdog can NEVER raise into its caller
        try:
            log(f"s8_morning_watchdog: check_and_alert failed entirely "
                f"({type(exc).__name__}: {exc}); no alert sent.")
        except Exception:  # noqa: BLE001
            pass
        return {"acted": "error", "alerted": False, "error": f"{type(exc).__name__}: {exc}"}


def main(argv=None) -> int:
    """One-shot entrypoint — run the check with real defaults, log the result, ALWAYS rc 0.

    rc is always 0: this is a best-effort morning nudge, and a nonzero exit would only add
    scheduled-task noise for an outcome (no-alert / alert / error) that is already logged.
    ``check_and_alert`` already never raises, but ``main`` still guards the call so that even
    a surprise failure there cannot turn into a nonzero rc / traceback out of the launcher.
    """
    try:
        result = check_and_alert()
    except Exception as exc:  # noqa: BLE001 — never let anything out of a best-effort nudge
        result = {"acted": "error", "alerted": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        print(f"s8_morning_watchdog: {result}")
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
