"""s8_desk_launch_link.py — BRING THE DESK UP BY TAPPING A LINK.

WHY THIS EXISTS (read this first)
---------------------------------
The S8 live pilot's IBKR gateway (port 4003) needs an IBKR Mobile 2FA approval to finish
logging in. It used to be launched on a SCHEDULE (``LiveTradeGatewayOpen``, retrying every
~10 min from ~08:00 CT). That is the whole problem: a scheduled launch fires whether or not
Andrew is holding his phone, so the push lands unanswered, the login fails, and the gateway
sits dead — and each retry burns another failed login (IBKR counts those in
``loginFailFrequency.txt``).

On 2026-09-02 exactly that happened: launched 08:00:04, push at 08:00:08, nobody there,
authorization failed 08:14:13, and the lane stayed dead through the market open.

THE FIX IS TO REMOVE THE PROBLEM, NOT MANAGE IT. Nothing launches the gateway on a
schedule any more. Instead:

    1. Each trading morning this module emails Andrew ONE message with ONE button.
    2. He taps it when he actually has his phone and signal.
    3. The tap records a request (public Edge Function -> Supabase); this module polls,
       sees it, and launches the gateway RIGHT THEN.
    4. The 2FA push therefore arrives at a moment he is ready to approve.

No retry caps, no hold flags, no dead-auth suppression: with no unrequested launches there
is nothing to suppress. A tap is the ONLY thing that ever starts the gateway.

WHY A POLL AND NOT AN INBOUND CALL
----------------------------------
The desk sits on a home LAN, so a link tapped on a phone cannot reach it. The desk polls
OUTBOUND instead, which means no inbound port, tunnel, or daemon is ever exposed on the
trading machine. The desk needs no write privilege either: single use is enforced by the
UNIQUE ``token_id`` in the database plus the locally recorded last-seen request id.

SECURITY
--------
The link carries an HMAC-SHA256 token (see ``mint_token``) signed with a secret shared only
with the Edge Function. Worst case if a link leaks: someone triggers ONE gateway launch and
one 2FA push. They cannot approve it, log in, place an order, or move money — the push
still lands on Andrew's phone.

ZERO-TRANSMIT: no order path, no ib_async import, no knowledge of strategy or PILOT_MODE.
It starts a process and sends mail; nothing else.

NEVER RAISES: every entry point catches everything. ``main`` always returns 0 — a mail or
database hiccup must never take the desk down.

PURE SEAMS: the clock, the id source, the port probe, the mailer, the database query, the
launcher and the state path are all injected, so every branch is offline-testable with no
gateway, no network and no mail.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import uuid
from base64 import urlsafe_b64encode
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import s8_gateway_alert  # noqa: E402  (reuse its port probe and mailer resolution)

FUNCTION_URL = "https://xazhttkwrstuqvkpojff.supabase.co/functions/v1/gateway-relaunch"
LIVE_TRADE_PORT = s8_gateway_alert.LIVE_TRADE_PORT
SECRET_ENV_PATH = r"C:\TradingDesk-Local\secrets\.env"
SECRET_KEY = "DESK_RELAUNCH_SECRET"
DSN_ENV = "TRADINGDESK_CRM_DSN"
STATE_NAME = "desk_launch_link.json"
# The link outlives the trading day so a morning email is still usable late in the session.
TOKEN_TTL_SECS = 14 * 3600
OPEN_CMD = r"C:\TradingDesk\livebot\run_live_trade_gateway_open.cmd"


# --------------------------------------------------------------------------- #
# TOKEN — must stay byte-compatible with the gateway-relaunch Edge Function
# --------------------------------------------------------------------------- #

def mint_token(secret: str, *, ttl_secs: int = TOKEN_TTL_SECS,
               now: Optional[Callable[[], float]] = None,
               jti: Optional[str] = None) -> str:
    """``<payload_b64url>.<hmac_sha256_hex>``.

    The payload is compact JSON ``{"jti": ..., "exp": ...}``. The signature covers the
    ENCODED payload text, not the decoded object, so both sides sign exactly the same bytes
    and no JSON-formatting difference can change the result."""
    import time as _time  # noqa: PLC0415

    clock = now or _time.time
    claims = {"jti": jti or uuid.uuid4().hex, "exp": int(clock()) + int(ttl_secs)}
    payload = urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def link_for(token: str, *, base_url: str = FUNCTION_URL) -> str:
    return f"{base_url}?token={token}"


def load_secret(*, path: str = SECRET_ENV_PATH) -> Optional[str]:
    """The signing secret from the desk's .env. None if absent — never raises."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(SECRET_KEY + "="):
                    return line.split("=", 1)[1].strip() or None
    except Exception:  # noqa: BLE001
        return None
    return None


# --------------------------------------------------------------------------- #
# STATE — which day we emailed, and the last request id we acted on
# --------------------------------------------------------------------------- #

def default_state_path():
    import s8_store  # noqa: PLC0415

    return s8_store.get_root() / "state" / STATE_NAME


def read_state(path=None) -> Dict[str, Any]:
    try:
        with open(str(path or default_state_path()), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt state is simply "nothing done yet"
        return {}


def write_state(state: Dict[str, Any], path=None) -> bool:
    p = str(path or default_state_path())
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# DATABASE — read-only; the desk never writes to the request lane
# --------------------------------------------------------------------------- #

def fetch_requests(after_id: int = 0, *, dsn: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Relaunch requests newer than ``after_id``, oldest first. None if the lane could not
    be read (no DSN, no driver, database down) — which is NOT the same as "none pending"
    and must never be treated as one."""
    conn_str = (dsn if dsn is not None else os.environ.get(DSN_ENV, "")).strip()
    if not conn_str:
        return None
    try:
        import psycopg2  # noqa: PLC0415
    except ImportError:
        return None
    try:
        with psycopg2.connect(conn_str, connect_timeout=15) as conn, conn.cursor() as cur:
            cur.execute(
                "select id, token_id, requested_at from public.desk_gateway_relaunch_requests "
                "where id > %s order by id asc limit 20",
                (int(after_id),),
            )
            return [{"id": r[0], "token_id": r[1], "requested_at": r[2]} for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# ACTIONS
# --------------------------------------------------------------------------- #

def launch_gateway(*, run: Callable[..., Any] = subprocess.run,
                   log: Callable[[str], Any] = print) -> bool:
    """Run the existing cold-start script. It is already idempotent: it reaps orphans and
    refuses to start a second gateway when one is alive or 4003 is bound."""
    try:
        out = run(["cmd", "/c", OPEN_CMD], capture_output=True, text=True, timeout=180)
        rc = getattr(out, "returncode", None)
        log(f"s8_desk_launch_link: launch script finished rc={rc!r}")
        return rc == 0
    except Exception as exc:  # noqa: BLE001
        log(f"s8_desk_launch_link: launch failed ({type(exc).__name__}: {exc})")
        return False


def _email_html(url: str) -> str:
    return f"""<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;
 background:#f5f5f7;margin:0;padding:24px;color:#1d1d1f">
<div style="max-width:460px;margin:0 auto;background:#fff;border-radius:14px;padding:24px">
<h2 style="margin:0 0 12px;font-size:19px">Bring the trading desk up</h2>
<p style="font-size:16px;line-height:1.5;margin:0 0 8px">
The IBKR live-trade gateway is not running. Tap the button when you have your phone in hand
&mdash; it launches the gateway and sends you a two-factor push within about a minute.</p>
<p style="font-size:14px;color:#6e6e73;line-height:1.5;margin:0 0 18px">
Nothing launches on its own any more, so there is no push waiting and nothing expires if you
tap this later. The desk stays down until you do.</p>
<a href="{url}" style="display:block;text-align:center;background:#0b57d0;color:#fff;
 text-decoration:none;padding:17px;border-radius:11px;font-size:18px;font-weight:600">
Bring the desk up</a>
<p style="font-size:13px;color:#6e6e73;margin:18px 0 0;line-height:1.5">
One tap is all it takes &mdash; the page just confirms it started. You will
get a &ldquo;gateway back up&rdquo; email once it is serving.</p>
</div></body></html>"""


def send_launch_email(url: str, *, mailer=None, log: Callable[[str], Any] = print) -> bool:
    """One HTML email carrying the tap-to-launch button. Never raises."""
    try:
        m = mailer if mailer is not None else s8_gateway_alert._default_mailer()
        return bool(m.send_html("[S8] Tap to bring the trading desk up", _email_html(url)))
    except Exception as exc:  # noqa: BLE001 — mail is best-effort, always
        log(f"s8_desk_launch_link: email failed ({type(exc).__name__}: {exc})")
        return False


# --------------------------------------------------------------------------- #
# THE TICK — run once a minute
# --------------------------------------------------------------------------- #

def tick(
    *,
    probe_port: Callable[..., Optional[bool]] = s8_gateway_alert.port_listening,
    get_requests: Callable[..., Optional[List[Dict[str, Any]]]] = fetch_requests,
    launch: Callable[..., bool] = launch_gateway,
    send: Callable[..., bool] = send_launch_email,
    secret: Optional[str] = None,
    state_path=None,
    ct_date: Optional[Callable[[], str]] = None,
    is_trading_day: Optional[Callable[[Any], bool]] = None,
    today: Any = None,
    mint: Callable[..., str] = mint_token,
    log: Callable[[str], Any] = print,
) -> Dict[str, Any]:
    """One pass. Returns a result dict; never raises.

    Order matters: a pending TAP is honoured before anything else, so a tap is acted on even
    on a day we would not otherwise email (a holiday, or a second launch after a crash).
    """
    result: Dict[str, Any] = {"acted": "noop", "launched": False, "emailed": False}
    try:
        state = read_state(state_path)

        # (1) Did Andrew tap? Honour it regardless of day or time.
        after = int(state.get("last_request_id") or 0)
        reqs = get_requests(after)
        if reqs:
            newest = reqs[-1]
            state["last_request_id"] = int(newest["id"])
            # Record BEFORE launching: if the launch crashes the process, we must not
            # replay the same tap forever on every subsequent tick.
            write_state(state, state_path)
            log(f"s8_desk_launch_link: tap received (request id={newest['id']}) — launching.")
            ok = bool(launch(log=log))
            result.update(acted="launched_on_tap", launched=ok, request_id=newest["id"])
            return result

        # (2) No tap. If the gateway is already serving there is nothing to say.
        try:
            up = probe_port(LIVE_TRADE_PORT)
        except Exception:  # noqa: BLE001 — a broken probe is UNKNOWN, not confirmed-up
            up = None
        if up is True:
            result["acted"] = "up_quiet"
            return result

        # (3) Down. Email the button ONCE per trading day.
        import s8_service  # noqa: PLC0415
        resolver = getattr(s8_service, "resolve_trading_day", None)
        trading = resolver(today, is_trading_day, log=log) if resolver else True
        if trading is False:
            result["acted"] = "skipped_non_trading_day"
            return result

        date_fn = ct_date or _default_ct_date
        day = date_fn()
        if state.get("emailed_ct_date") == day:
            result["acted"] = "already_emailed_today"
            return result

        sec = secret if secret is not None else load_secret()
        if not sec:
            log("s8_desk_launch_link: no signing secret configured; cannot build a link.")
            result["acted"] = "no_secret"
            return result

        url = link_for(mint(sec))
        sent = bool(send(url, log=log))
        if sent:
            state["emailed_ct_date"] = day
            write_state(state, state_path)
        result.update(acted="emailed" if sent else "email_failed", emailed=sent)
        return result
    except Exception as exc:  # noqa: BLE001 — a helper must NEVER raise into its caller
        try:
            log(f"s8_desk_launch_link: tick failed ({type(exc).__name__}: {exc})")
        except Exception:  # noqa: BLE001
            pass
        result.update(acted="error", error=f"{type(exc).__name__}: {exc}")
        return result


def _default_ct_date() -> str:
    from datetime import datetime  # noqa: PLC0415
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        return datetime.now(tz=ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y-%m-%d")


def main(argv: Optional[List[str]] = None) -> int:
    """Always rc 0 — this is a best-effort helper, never a gate on anything."""
    res = tick()
    print(f"s8_desk_launch_link: {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
