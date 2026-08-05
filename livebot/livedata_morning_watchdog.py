"""livedata_morning_watchdog.py — LIVE-DATA (port 4001) weekly "STILL DOWN" 2FA-tap nudge.

WHY THIS EXISTS
---------------
The read-only live-DATA Gateway (port 4001, IBKR login ``databot0001``) is what the nightly
EOD option collector (``datacollector/forward_daily_live.py``) connects to. Under the
2026-08-05 continuous-uptime design that Gateway is kept up 24/7 by IBC auto-restart, which
reuses the authenticated session with NO 2FA on the daily bounce — so a human only has to
approve an IBKR Mobile 2FA push **~once a week**, on the first login after the weekly Sunday
01:00 ET security-token invalidation (or after a crash / cold launch).

This module is the LAST-CHANCE nudge for that weekly tap. It is a ONE-SHOT check, run once
at ~08:45 CT on a trading day. If port 4001 is STILL not serving by then, it emails ONE
alert: "approve the IBKR Mobile push for the LIVE-DATA gateway, or investigate." That closes
the gap between a missed weekly re-auth and the owner finding out only when that evening's
option pull silently fails.

It is the twin of ``livebot/s8_morning_watchdog.py`` (the port-4003 live-TRADE nudge) and
deliberately REUSES that lane's generic machinery — ``s8_gateway_alert.port_listening`` (a
plain TCP probe with the port injected) and ``s8_gateway_alert._default_mailer`` (the SAME
existing ``dailyreport`` mailer) — rather than reimplementing any of it. No new mail config
and no new credentials are introduced. The subject prefix is LIVE-DATA-specific so the alert
is never confused with an S8 live-trade alert.

BEST-EFFORT, ALWAYS (never raises). ZERO-TRANSMIT / NO order path: it only TCP-probes port
4001 and may send one email. It launches nothing (safe to run even before the first login is
seeded — it can only email, never push a 2FA itself).
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

# Self-contained sys.path shim — same rationale as the sibling livebot modules: the venv
# editable installs may still point at the deleted pre-2026-07-16 My Drive path, so make
# this module's own directory importable so ``import s8_gateway_alert`` resolves against
# the repo's real files.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import s8_gateway_alert  # noqa: E402  (reused: the generic TCP probe + the dailyreport mailer)

_CT_ZONE = ZoneInfo("America/Chicago")

# The read-only live-DATA Gateway's port. Mirrors connections.clientids.LIVE_DATA_PORT (4001);
# hardcoded so this best-effort nudge never depends on importing the connections package.
LIVE_DATA_PORT = 4001

_SUBJECT_PREFIX = "[TradingDesk LIVE-DATA]"


def _default_send(subject: str, lines: list, mailer=None,
                  log: Callable[[str], Any] = print) -> bool:
    """Send ONE alert through the EXISTING dailyreport mailer (same helper/credentials the
    S8 alerts use), with a LIVE-DATA subject prefix. Never raises."""
    try:
        m = mailer if mailer is not None else s8_gateway_alert._default_mailer()
        html = "<html><body><pre>" + "\n".join(str(x) for x in lines) + "</pre></body></html>"
        return bool(m.send_html(f"{_SUBJECT_PREFIX} {subject}", html))
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort, always
        try:
            log(f"livedata_morning_watchdog: alert email itself failed "
                f"({type(exc).__name__}: {exc})")
        except Exception:  # noqa: BLE001
            pass
        return False


def _default_is_trading_day(d: date) -> bool:
    """True if the market is open on ``d`` (via connections.market_calendar). FAILS OPEN: on
    any calendar glitch (missing year, import error) it returns True so a calendar problem
    can never silently SUPPRESS a real alert — worst case is one extra email on a closed day."""
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(repo_root, "connections")
        if p not in sys.path:
            sys.path.insert(0, p)
        from connections import market_calendar  # noqa: PLC0415
        return bool(market_calendar.is_trading_day(d))
    except Exception:  # noqa: BLE001 — fail OPEN (assume trading day) so we never miss an alert
        return True


def check_and_alert(
    *,
    probe_port: Callable[..., Optional[bool]] = s8_gateway_alert.port_listening,
    is_trading_day: Callable[[date], bool] = _default_is_trading_day,
    today: Optional[date] = None,
    mailer=None,
    send: Callable[..., bool] = _default_send,
    log: Callable[[str], Any] = print,
) -> dict:
    """One-shot morning check: is the LIVE-DATA Gateway (port 4001) STILL down at ~08:45 CT?

    Decision, in order:
      a) TRADING-DAY GUARD. Not a trading day (weekend/holiday) -> do nothing, send nothing
         (a Gateway that is down on a closed market is expected). Returns
         ``{"acted": "skipped_non_trading_day", "alerted": False}``.
      b) UP -> QUIET. ``probe_port(4001)`` True -> serving; no alert.
      c) NOT CONFIRMED UP -> ALERT ONCE. Probe False (refused) OR None (undeterminable) at
         08:45 is a problem worth a nudge -> send ONE email.

    NEVER raises — the whole body is wrapped.
    """
    try:
        td = today if today is not None else datetime.now(tz=_CT_ZONE).date()
        try:
            trading = bool(is_trading_day(td))
        except Exception:  # noqa: BLE001 — a broken calendar must not suppress the alert
            trading = True
        if not trading:
            log("livedata_morning_watchdog: not a trading day; no check.")
            return {"acted": "skipped_non_trading_day", "alerted": False}

        try:
            up = probe_port(LIVE_DATA_PORT)
        except Exception:  # noqa: BLE001 — a broken probe is UNKNOWN, not confirmed-up
            up = None
        if up is True:
            log("livedata_morning_watchdog: gateway 4001 is UP; no alert.")
            return {"acted": "up_no_alert", "alerted": False}

        try:
            now_ct = datetime.now(tz=_CT_ZONE).strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:  # noqa: BLE001
            now_ct = "(CT time unavailable)"

        subject = "LIVE-DATA gateway STILL DOWN at 08:45 CT - approve the 2FA or investigate"
        lines = [
            f"The live-DATA gateway (port {LIVE_DATA_PORT}, login databot0001) is STILL NOT "
            f"UP as of {now_ct}.",
            "",
            "This gateway is kept up 24/7 and normally needs a human 2FA tap only ONCE A "
            "WEEK, on the first login after the Sunday 01:00 ET security-token reset. If it "
            "is down on a weekday morning, the weekly re-auth (or a crash relaunch) is most "
            "likely waiting on an unapproved IBKR Mobile push.",
            "",
            "WHAT TO DO:",
            "  * If you have an UNAPPROVED IBKR Mobile push waiting, it is THIS DESK's "
            "live-data gateway - approve it to bring 4001 back up.",
            "  * If there is NO push waiting, something else is wrong - investigate (gateway "
            "not launching, IBC/2FA loop, machine asleep, etc.). If it stays down, tonight's "
            "EOD option-chain pull (IbkrForwardEodDaily) will fail.",
            "",
            "To seed a fresh login by hand: run C:\\IBC-Live-Data\\StartGatewayLiveData.bat "
            "(or the LiveDataGwManual scheduled task) and approve the IBKR Mobile push.",
            "",
            "This is a DATA/connectivity nudge only. The live-data login is read-only (no "
            "execution capability); nothing here places or transmits an order.",
        ]
        sent = send(subject, lines, mailer=mailer, log=log)
        log(f"livedata_morning_watchdog: gateway 4001 NOT confirmed up "
            f"(port_listening={up!r}); alert sent={sent!r}.")
        return {"acted": "alerted", "alerted": True, "port_listening": up}
    except Exception as exc:  # noqa: BLE001 — a watchdog can NEVER raise into its caller
        try:
            log(f"livedata_morning_watchdog: check_and_alert failed entirely "
                f"({type(exc).__name__}: {exc}); no alert sent.")
        except Exception:  # noqa: BLE001
            pass
        return {"acted": "error", "alerted": False, "error": f"{type(exc).__name__}: {exc}"}


def main(argv=None) -> int:
    """One-shot entrypoint — run the check with real defaults, log the result, ALWAYS rc 0."""
    try:
        result = check_and_alert()
    except Exception as exc:  # noqa: BLE001 — never let anything out of a best-effort nudge
        result = {"acted": "error", "alerted": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        print(f"livedata_morning_watchdog: {result}")
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
