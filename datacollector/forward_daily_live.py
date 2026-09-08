"""
forward_daily_live.py — ONE daily EOD pass of the IBKR forward option collector,
against the LIVE-TRADING Gateway (port 4003).

CONSOLIDATED 2026-09-08. This job used to run on a third Gateway instance — the
restricted live-DATA login on port 4001, which was read-only by construction. That
lane is retired: it required its own separate 2FA, was only ever launched inside an
08:05–12:00 window gated on 4003 already being up, and repeatedly died before the
17:30 run, losing four trading days of chain data (2026-09-02..05). Port 4003 is the
one Gateway that survives IBKR's nightly restart on a stored session token, and it
serves this data BETTER: SPX/SPXW chains return with no entitlement complaints at
all, where 4001 logged ~48,000 "not subscribed, displaying delayed" messages a night.
See connections/GATEWAYS.md.

THE TRADE-OFF, STATED PLAINLY: 4003 is transmit-CAPABLE. On the old lane read-only
was structural — connect() had no `readonly` parameter and the account had no
execution capability. Here it is an explicit argument, passed at every call site in
_connect() below and in ibkr_forward_live.main(). Nothing in datacollector/ places,
modifies, or cancels an order, and readonly=True must never be removed or flipped
from this package. That discipline is now the wall.

Fired once per trading day by Windows Task Scheduler (mirror of run_forward.bat,
not yet created). This is the production wrapper around ibkr_forward_live: it is a
ONE-SHOT (connect -> snapshot today's full chains for the whole universe -> write
-> exit), NOT a forever loop — the scheduler is what makes it recur. Same universe,
same warehouse schema/writer as the paper variant; only the Gateway connection
differs.

Resilience: weekday guard, then a TCP probe of the Gateway — if 4003 is down this run
SKIPS and EMAILS rather than launching (a 17:30 launch fires a 2FA push nobody may be
there to answer; see gateway_up() below), per-root error isolation (one bad
root never aborts the run), resumable (skips any root already on disk for today).
Logs to warehouse\\forward_live.log and updates warehouse\\forward_heartbeat_live.txt
so a glance confirms it ran and how far it got. As of the 2026-07-27 ThetaData->IBKR
cutover this is THE production nightly EOD option collector, so it writes the canonical
"forward" jobstatus key (dailyreport/status.py) that the EOD report and
heartbeat_alarm's "forward" deadline watchdog read — taking over from the retired
ThetaData eod_daily.py. (It formerly wrote "forward_live" during the A/B window to avoid
colliding with the paper variant forward_daily.py, which is a manual/dev tool and not
scheduled nightly.)

Run manually any time:  <venv python> forward_daily_live.py
"""

from __future__ import annotations

import datetime as dt
import pathlib
import socket
import sys
from datetime import date

import config
import ibkr_forward_live as fwd
from connections import ibkr_live_trade as gw

# status.py lives in the sibling dailyreport project (the EOD reporter reads it).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "dailyreport"))
import status as jobstatus  # noqa: E402

LOG = config.DATA_ROOT / "forward_live.log"
HEARTBEAT = config.DATA_ROOT / "forward_heartbeat_live.txt"


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)          # pythonw has no stdout — guard it
    except Exception:
        pass
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def gateway_up(timeout: float = 3.0) -> bool:
    """Is the live-trading Gateway (4003) listening? A PLAIN TCP PROBE — it NEVER
    launches anything.

    This deliberately does NOT call ``gw.ensure_gateway()``. A launch from here fires
    an IBKR Mobile 2FA push at 17:30 CT that nobody may be there to answer, which is
    exactly the pattern the tap-to-launch design (livebot/s8_desk_launch_link.py)
    removed from the morning: an unanswered push fails the login and burns one of the
    failed logins IBKR counts. If the gateway is down at 17:30 the right answer is to
    skip tonight's pull and say so, not to start a login nobody can finish.

    A listening port is the right go/no-go here precisely because we are not launching.
    (The "port is blind during login" lesson in connections/GATEWAYS.md applies to
    LAUNCH decisions — a wedged login looks identical to no gateway. Here a false
    "up" just means _connect() fails and the per-root error handling reports it.)
    """
    try:
        with socket.create_connection((gw.HOST, gw.LIVE_TRADE_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def alert_gateway_down(daystr: str) -> bool:
    """Email that tonight's pull was SKIPPED. Best-effort — never raises.

    Uses the EXISTING dailyreport mailer (already importable: this module puts
    dailyreport on sys.path for status.py). No new mail config, no new credentials.
    """
    try:
        import mailer  # noqa: PLC0415 — lazy so offline tests never touch mail config
        html = (
            "<p><b>Tonight's end-of-day option-chain pull did not run.</b></p>"
            f"<p>Date: {daystr}. The live-trading gateway on port 4003 was not up when "
            "the job started at 17:30 Central.</p>"
            "<p>The job <b>deliberately did not try to start the gateway</b>. Starting it "
            "would send a 2FA approval to your phone that may go unanswered, which fails "
            "the login and counts against IBKR's failed-login limit. Skipping is the "
            "safer choice.</p>"
            "<p><b>What this costs:</b> one day of SPX / SPXW / RUT / NDX end-of-day "
            "option-chain data. It cannot be back-filled later — these are daily "
            "snapshots of live chains, which is why they are captured nightly.</p>"
            "<p><b>What to do:</b> bring the gateway up when convenient (the tap-to-launch "
            "email button is the normal way). Tomorrow's run needs no action.</p>")
        return bool(mailer.send_html(
            f"Trading Desk — EOD option pull SKIPPED {daystr} (gateway 4003 not up)", html))
    except Exception as e:                # noqa: BLE001 — a broken mailer must not change the outcome
        log(f"  (gateway-down alert email failed: {e!r})")
        return False


def _connect(real_errors: list[str]):
    """Fresh read-only connection (clientId 68) with farm-OK error filtering.

    Consolidated onto the live-TRADING Gateway (port 4003) on 2026-09-08. That lane
    is transmit-CAPABLE, so `readonly=True` is passed EXPLICITLY here and is the wall
    that keeps this nightly data job a reader. The old ibkr_live_data lane made that
    structural (no readonly parameter existed); here it is a deliberate argument.
    Never remove it, and never pass readonly=False from datacollector/.
    """
    ib = gw.connect(fwd.CLIENT, readonly=True)
    ib.errorEvent += lambda rid, code, msg, c: (
        real_errors.append(f"[{code}] {msg}") if code not in fwd.OK_STATUS else None)
    ib.reqMarketDataType(3)              # delayed — EOD snapshot doesn't need live entitlement
    return ib


def main() -> int:
    """Run one EOD pass. Returns a process exit code: 0 = success or a legitimate
    no-op (weekend / market holiday); non-zero = a genuine failure the scheduler
    must surface (gateway never came up, or no root produced data on a trading
    day). The jobstatus "forward" key is written on EVERY path — it is the second,
    independent detection channel (heartbeat_alarm's watchdog) and must keep
    working regardless of the exit code."""
    today = date.today()
    daystr = today.strftime("%Y%m%d")
    if today.weekday() >= 5:             # 5=Sat, 6=Sun
        log(f"{daystr} is a weekend — nothing to collect.")
        jobstatus.write("forward", "ok", message="weekend — no trading day", day=daystr)
        return 0
    # Full-day market holiday: a clean no-op like a weekend — a closed session
    # would return empty chains for every root and be scored "fail" below. Defensive:
    # a missing/edge calendar year must never BLOCK a real collection, so any
    # calendar error falls through and we proceed as if it were a trading day.
    try:
        from connections import market_calendar as _cal
        if _cal.is_holiday(today):
            name = _cal.holiday_name(today) or "market holiday"
            log(f"{daystr} is a market holiday ({name}) — nothing to collect.")
            jobstatus.write("forward", "ok", message=f"{name} — no trading day", day=daystr)
            return 0
    except Exception as e:                # noqa: BLE001 — never block collection on a calendar hiccup
        log(f"  (holiday check skipped: {e!r}); proceeding as a trading day")

    log(f"=== forward_live run {daystr} start (per-root depth: SPX/SPXW band=+/-"
        f"{config.FORWARD_DEEP_STRIKE_BAND} exps<={config.FORWARD_DEEP_MAX_EXPIRATIONS}; "
        f"others band=+/-{config.FORWARD_STRIKE_BAND} exps<={config.FORWARD_MAX_EXPIRATIONS}) ===")
    if not gateway_up():
        log("Live-trading Gateway (4003) is NOT up - SKIPPING tonight's pull. "
            "Deliberately NOT launching it: a launch fires a 2FA push nobody may be "
            "there to answer. Emailing instead; retry is tomorrow's scheduled run.")
        jobstatus.write("forward", "fail",
                        message="SKIPPED - gateway 4003 not up (no launch attempted)",
                        day=daystr)
        alert_gateway_down(daystr)
        return 1

    real_errors: list[str] = []
    ib = _connect(real_errors)
    # Optional CLI roots (for manual/single-root ops runs); default = full universe.
    cli_roots = [a.upper() for a in sys.argv[1:] if not a.startswith("--")]
    roots = cli_roots or config.all_roots()
    ok = empty = fail = skip = 0
    try:
        for i, sym in enumerate(roots, 1):
            # Per-root retry that survives a mid-run socket drop (WinError 64): a
            # disconnect loses at most the current root, which collect_day re-harvests
            # from scratch (have_day is still False since nothing was written).
            for attempt in (1, 2):
                try:
                    if not ib.isConnected():
                        ib = _connect(real_errors)
                        log("  (reconnected)")
                    band, max_exps = config.forward_depth(sym)
                    status, n = fwd.collect_day(ib, sym, daystr, band=band, max_exps=max_exps)
                    if status == "ok":
                        ok += 1
                    elif status == "skip":
                        skip += 1
                    else:                # no-chain / no-data (holiday or transient)
                        empty += 1
                    log(f"  {sym:6} {status:9} rows={n}")
                    break
                except Exception as e:   # one root failing must not kill the run
                    log(f"  {sym:6} attempt {attempt} ERROR {e!r}")
                    try:
                        if not ib.isConnected():
                            ib = _connect(real_errors)
                    except Exception:
                        pass
                    if attempt == 2:
                        fail += 1
            HEARTBEAT.write_text(
                f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {daystr}  {i}/{len(roots)} roots  "
                f"ok={ok} skip={skip} empty={empty} fail={fail}")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    log(f"=== forward_live run {daystr} done: ok={ok} skip={skip} empty={empty} fail={fail} "
        f"real_errors={len(real_errors)} ===")
    if real_errors:
        for e in real_errors[:20]:
            log(f"    err {e}")
    HEARTBEAT.write_text(
        f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {daystr}  COMPLETE "
        f"ok={ok} skip={skip} empty={empty} fail={fail}")

    overall = "ok" if fail == 0 and (ok > 0 or skip > 0) else ("partial" if ok > 0 else "fail")
    jobstatus.write("forward", overall, day=daystr,
                    metrics={"roots": len(roots), "ok": ok, "skip": skip,
                             "empty": empty, "fail": fail, "real_errors": len(real_errors)},
                    message=f"EOD option-chain collect, live-trading Gateway 4003 ({ok} roots written)")

    # Exit non-zero ONLY when nothing was collected on a trading day ("fail"): that is
    # a genuine outage the scheduler must show red. "partial" (some roots written, some
    # failed) stays exit 0 — data landed and the jobstatus "partial" key is what surfaces
    # the degraded roots to the heartbeat watchdog, so it doesn't warrant a hard failure.
    return 0 if overall in ("ok", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
