"""
forward_daily_live.py — ONE daily EOD pass of the IBKR forward option collector,
against the SECOND, restricted, read-only-only LIVE-DATA Gateway instance.

Mirrors forward_daily.py exactly, repointed at connections.ibkr_live_data (port
4001) instead of connections.ibkr_paper (paper, port 4002). This is NOT paper and NOT
live trading — it is a read-only market-data-only Gateway, backed by a personal
live IBKR login that IBKR itself restricts to visibility into exactly one account
with no execution capability at the account-permission level, and whose connect()
has no `readonly` parameter at all (every connection it makes is hardcoded
read-only). Nothing in this module or in ibkr_forward_live.py places, modifies, or
cancels an order.

Fired once per trading day by Windows Task Scheduler (mirror of run_forward.bat,
not yet created). This is the production wrapper around ibkr_forward_live: it is a
ONE-SHOT (connect -> snapshot today's full chains for the whole universe -> write
-> exit), NOT a forever loop — the scheduler is what makes it recur. Same universe,
same warehouse schema/writer as the paper variant; only the Gateway connection
differs.

Resilience: weekday guard, launches the live-data Gateway if it's down (via
connections.ibkr_live_data.ensure_gateway()), per-root error isolation (one bad
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
import sys
from datetime import date

import config
import ibkr_forward_live as fwd
from connections import ibkr_live_data as gw

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


def _connect(real_errors: list[str]):
    """Fresh read-only connection (clientId 48) with farm-OK error filtering.

    connections.ibkr_live_data.connect() has no `readonly` parameter — every
    connection it makes is hardcoded read-only, so there is nothing to pass here.
    """
    ib = gw.connect(fwd.CLIENT)
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
    if not gw.ensure_gateway():
        log("Live-data Gateway did not come up within timeout - aborting; retry next scheduled run.")
        jobstatus.write("forward", "fail", message="Gateway did not come up", day=daystr)
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
                    message=f"EOD option-chain collect, live-data Gateway ({ok} roots written)")

    # Exit non-zero ONLY when nothing was collected on a trading day ("fail"): that is
    # a genuine outage the scheduler must show red. "partial" (some roots written, some
    # failed) stays exit 0 — data landed and the jobstatus "partial" key is what surfaces
    # the degraded roots to the heartbeat watchdog, so it doesn't warrant a hard failure.
    return 0 if overall in ("ok", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
