"""
forward_daily.py — ONE daily EOD pass of the IBKR forward option collector.

Fired once per trading day by Windows Task Scheduler (run_forward.bat). This is the
production wrapper around ibkr_forward: it is a ONE-SHOT (connect → snapshot today's
full chains for the whole universe → write → exit), NOT a forever loop — the
scheduler is what makes it recur. Contrast with the ThetaData supervisor, which
loops because that grab is a one-time finite backfill; forward collection is an
open-ended daily cadence, so "daily trigger + one-shot" is the right shape.

Resilience: weekday guard, launches the Gateway if it's down (the java_version fix
lives in connections.ibkr_paper), per-root error isolation (one bad root never aborts the
run), resumable (skips any root already on disk for today). Logs to
warehouse\forward.log and updates warehouse\forward_heartbeat.txt so a glance
confirms it ran and how far it got.

Run manually any time:  <venv python> forward_daily.py
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
from datetime import date

import config
import ibkr_forward as fwd
from connections import ibkr_paper as gw

# status.py lives in the sibling dailyreport project (the EOD reporter reads it).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "dailyreport"))
import status as jobstatus  # noqa: E402

LOG = config.DATA_ROOT / "forward.log"
HEARTBEAT = config.DATA_ROOT / "forward_heartbeat.txt"


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)          # pythonw has no stdout — guard it
    except Exception:
        pass
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _connect(real_errors: list[str]):
    """Fresh readonly connection (clientId 25) with farm-OK error filtering."""
    ib = gw.connect(fwd.CLIENT, readonly=True)
    ib.errorEvent += lambda rid, code, msg, c: (
        real_errors.append(f"[{code}] {msg}") if code not in fwd.OK_STATUS else None)
    ib.reqMarketDataType(3)              # delayed — EOD snapshot doesn't need live entitlement
    return ib


def main() -> None:
    today = date.today()
    daystr = today.strftime("%Y%m%d")
    if today.weekday() >= 5:             # 5=Sat, 6=Sun
        log(f"{daystr} is a weekend — nothing to collect.")
        jobstatus.write("forward", "ok", message="weekend — no trading day", day=daystr)
        return

    log(f"=== forward run {daystr} start (per-root depth: SPX/SPXW band=+/-"
        f"{config.FORWARD_DEEP_STRIKE_BAND} exps<={config.FORWARD_DEEP_MAX_EXPIRATIONS}; "
        f"others band=+/-{config.FORWARD_STRIKE_BAND} exps<={config.FORWARD_MAX_EXPIRATIONS}) ===")
    if not gw.ensure_gateway():
        log("Gateway did not come up within timeout - aborting; retry next scheduled run.")
        jobstatus.write("forward", "fail", message="Gateway did not come up", day=daystr)
        return

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

    log(f"=== forward run {daystr} done: ok={ok} skip={skip} empty={empty} fail={fail} "
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
                    message=f"EOD option-chain collect ({ok} roots written)")


if __name__ == "__main__":
    main()
