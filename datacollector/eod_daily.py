"""
eod_daily.py — the DAILY EOD option-chain grab, served by the local ThetaData
Terminal (v3). This REPLACES the IBKR forward collector (forward_daily.py +
ibkr_forward.py), which pulled a band-limited (+/-50 strike), degraded-greeks
snapshot through the live Gateway and was the source of the quality "seam" between
the historical ThetaData warehouse and the forward extension.

Shape: a ONE-SHOT pass fired once per trading day by Windows Task Scheduler
(run_eoddaily.bat -> ThetaEodDaily). For TODAY (weekday-guarded) plus a short
self-healing look-back over the last ~5 business days, for every root in
config.all_roots() it pulls the FULL chain (strike=*, expiration=*) EOD greeks
joined with open interest and writes it to the SAME warehouse layout the one-time
grab uses:  raw/options/{SYMBOL}/{YYYYMMDD}.parquet.

Because the file is one-per-(root, day) and have_day() keys off file presence, the
job is idempotent and resumable: a day already on disk is skipped, so the look-back
heals only genuine holes (e.g. a night the Terminal was down) without re-pulling.
Nothing else writes today's EOD file, so there is no collision with the 1-min SPXW
collector or the GEX rebuild that also hit the Terminal — but those DO share the
Terminal, so this job tolerates timeouts/retries (thetadata_client already retries,
and one bad root never aborts the run).

It reuses the existing building blocks verbatim:
  * download.pull_day(sym, day)  — full-chain greeks JOIN open_interest for one day
  * storage.have_day / write_day — idempotent atomic per-day parquet writes
  * thetadata_client.connected() — Terminal up-check before doing anything
  * config.all_roots()           — the curated universe

STATUS: it writes the SAME status artifact the EOD email already reads
(dailyreport status key "forward"), so the report's forward section keeps working
unchanged with the new source. It also writes warehouse\forward_heartbeat.txt in
the format the report's heartbeat fallback parses.

Run manually any time:
    <venv python> eod_daily.py                # today + 5-day self-heal, full universe
    <venv python> eod_daily.py SPX SPXW       # restrict to specific roots
    <venv python> eod_daily.py --days 10      # widen the self-heal look-back
    <venv python> eod_daily.py --date 20260626  # force a single specific day
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
from datetime import date

import pandas as pd

import config
import download
import storage
import thetadata_client as td

# status.py lives in the sibling dailyreport project (the EOD reporter reads it).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "dailyreport"))
import status as jobstatus  # noqa: E402

LOG = config.DATA_ROOT / "forward.log"          # reuse the same log the report knows
HEARTBEAT = config.DATA_ROOT / "forward_heartbeat.txt"

# How many business days back to self-heal (today inclusive). Small by design: the
# warehouse is normally current, so this only fills genuine gaps.
DEFAULT_LOOKBACK_DAYS = 5


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)          # pythonw has no stdout — guard it
    except Exception:
        pass
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _target_days(today: date, lookback: int) -> list[str]:
    """The last `lookback` business days ending today (inclusive), oldest first.

    Weekends are dropped here so the self-heal never wastes a request on a Sat/Sun.
    If `today` is itself a weekend the most recent business days are still returned,
    which is the desired self-heal behavior for a Saturday/Sunday scheduled misfire.
    """
    end = pd.Timestamp(today)
    # pull a generous calendar window then keep the last `lookback` business days
    start = end - pd.Timedelta(days=lookback * 2 + 7)
    bdays = pd.bdate_range(start, end)
    days = [d.strftime("%Y%m%d") for d in bdays][-lookback:]
    return days


def run(roots: list[str], days: list[str], current_daystr: str | None = None) -> dict:
    """Pull+write every missing (root, day). Returns metric counts for the status.

    `current_daystr` (YYYYMMDD) is the CURRENT trading day inside `days`, if any. That
    day is pulled via the per-expiration current-day path (expiration=* is 400 for
    today) and its outcomes are tracked SEPARATELY (cur_ok / cur_empty / cur_fail) so
    the status logic can tell "today wrote nothing because it errored" (a real red
    failure) apart from prior-day self-heal happening to have written rows.
    """
    ok = skip = empty = fail = 0
    cur_ok = cur_empty = cur_fail = 0
    fail_detail: list[str] = []
    n = len(roots)
    for i, sym in enumerate(roots, 1):
        for daystr in days:
            is_current = (daystr == current_daystr)
            try:
                if storage.have_day(sym, daystr):
                    skip += 1
                    continue
                df = download.pull_day(sym, daystr, current_day=is_current)
                rows = storage.write_day(sym, daystr, df)
                if rows == 0:
                    empty += 1
                    if is_current:
                        cur_empty += 1
                    log(f"  {sym:6} {daystr} empty (holiday/no-data)")
                else:
                    ok += 1
                    if is_current:
                        cur_ok += 1
                    log(f"  {sym:6} {daystr} wrote rows={rows:,}")
            except Exception as e:           # one bad (root, day) never aborts the run
                fail += 1
                if is_current:
                    cur_fail += 1
                msg = f"{sym} {daystr}: {type(e).__name__}: {e}"
                fail_detail.append(msg)
                log(f"  {sym:6} FAIL {msg}")
        HEARTBEAT.write_text(
            f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {days[-1]}  {i}/{n} roots  "
            f"ok={ok} skip={skip} empty={empty} fail={fail}")
    return {"ok": ok, "skip": skip, "empty": empty, "fail": fail,
            "cur_ok": cur_ok, "cur_empty": cur_empty, "cur_fail": cur_fail,
            "fail_detail": fail_detail}


def compute_status(m: dict, has_current_day: bool) -> str:
    """Map the run metrics to an EOD-report status: "ok" | "partial" | "fail".

    The critical rule (the bug this fixes): when there WAS a current trading day and
    it wrote ZERO roots because its requests ERRORED (cur_ok == 0 and cur_fail > 0),
    the day's dealer-gamma inputs never landed — that is a hard "fail" (red), even if
    prior-day self-heal wrote rows (ok > 0). The old logic called that "partial"
    (amber) because ok > 0, masking a 100%-current-day failure.

    Distinctions preserved:
      * current day errored (cur_fail > 0, cur_ok == 0)      -> fail   (red)
      * current day legitimately empty (holiday/not settled;
        cur_empty > 0, cur_ok == 0, cur_fail == 0)           -> not a failure by itself
      * prior-day self-heal accounting (ok / skip / fail)    -> partial/ok as before
    """
    cur_ok = m.get("cur_ok", 0)
    cur_fail = m.get("cur_fail", 0)
    fail = m.get("fail", 0)
    ok = m.get("ok", 0)
    skip = m.get("skip", 0)

    # Current day present but wrote nothing AND at least one current-day request
    # errored -> today's inputs are missing due to failure. Red.
    if has_current_day and cur_ok == 0 and cur_fail > 0:
        return "fail"
    if fail == 0:
        return "ok"
    if ok > 0 or skip > 0:
        return "partial"
    return "fail"


def main() -> None:
    today = date.today()
    daystr = today.strftime("%Y%m%d")

    # ---- arg parsing (all optional) -------------------------------------- #
    argv = sys.argv[1:]
    lookback = DEFAULT_LOOKBACK_DAYS
    forced_day = None
    do_catalog = False
    cli_roots: list[str] = []
    it = iter(argv)
    for a in it:
        if a == "--days":
            lookback = int(next(it))
        elif a == "--date":
            forced_day = next(it)
        elif a == "--catalog":
            do_catalog = True
        elif a.startswith("--"):
            continue
        else:
            cli_roots.append(a.upper())
    roots = cli_roots or config.all_roots()

    # ---- weekday guard (today-only matters; self-heal still runs) --------- #
    weekend = today.weekday() >= 5
    if forced_day:
        days = [forced_day]
    else:
        days = _target_days(today, lookback)

    # Which day in the window is the CURRENT (unsettled) trading day, if any. Only
    # `today` on a weekday is "current" — a forced --date or a weekend run is always
    # a settled/historical day and takes the fast expiration=* path.
    current_daystr = None if (weekend or forced_day) else (
        daystr if daystr in days else None)

    log(f"=== eod_daily {daystr} start (full-chain ThetaData; "
        f"{len(roots)} roots; days={days[0]}..{days[-1]}; "
        f"{'WEEKEND — today skipped, self-heal only' if weekend else 'weekday'}) ===")

    # ---- Terminal up-check ---------------------------------------------- #
    if not td.connected(retries=3, backoff_s=5.0):
        log(f"Theta Terminal not reachable at {config.THETA_BASE_URL} "
            "(checked 3x with backoff — likely busy, not down) — aborting; "
            "retry next scheduled run.")
        jobstatus.write("forward", "fail", day=daystr,
                        metrics={"roots": len(roots), "ok": 0, "skip": 0,
                                 "empty": 0, "fail": 0, "real_errors": 0},
                        message="ThetaData Terminal not reachable — nothing collected")
        HEARTBEAT.write_text(
            f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {daystr}  COMPLETE "
            f"ok=0 skip=0 empty=0 fail=0  (terminal down)")
        return

    # ---- do the work ----------------------------------------------------- #
    m = run(roots, days, current_daystr=current_daystr)
    ok, skip, empty, fail = m["ok"], m["skip"], m["empty"], m["fail"]

    log(f"=== eod_daily {daystr} done: ok={ok} skip={skip} empty={empty} fail={fail} ===")
    for d in m["fail_detail"][:20]:
        log(f"    fail {d}")

    HEARTBEAT.write_text(
        f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {daystr}  COMPLETE "
        f"ok={ok} skip={skip} empty={empty} fail={fail}")

    # ---- status artifact the EOD email reads (key 'forward') ------------- #
    # WRITE THIS BEFORE the catalog rebuild. rebuild_catalog() drives DuckDB, which
    # has been observed to crash the interpreter FATALLY (PyEval_SaveThread / GIL)
    # in this environment — a fatal crash bypasses try/except entirely, so anything
    # after it never runs. The status JSON is the contract the EOD email depends on,
    # so it must be durable before we touch DuckDB.
    overall = compute_status(m, has_current_day=current_daystr is not None)
    msg_bits = [f"{ok} root-days written", f"{skip} already had"]
    if empty:
        msg_bits.append(f"{empty} empty/holiday")
    if fail:
        msg_bits.append(f"{fail} root-days FAILED")
    if current_daystr is not None:
        msg_bits.append(
            f"current day {current_daystr}: "
            f"{m['cur_ok']} written / {m['cur_empty']} empty / {m['cur_fail']} FAILED")
    message = "Full-chain EOD grab (ThetaData) · " + ", ".join(msg_bits)
    jobstatus.write("forward", overall, day=daystr,
                    metrics={"roots": len(roots), "ok": ok, "skip": skip,
                             "empty": empty, "fail": fail, "real_errors": fail},
                    message=message)

    # Rebuild the DuckDB catalog so the new days are queryable. OFF by default and
    # opt-in via --catalog: storage.rebuild_catalog() drives DuckDB 1.5.4, which in
    # THIS environment reliably crashes the interpreter FATALLY (PyEval_SaveThread /
    # GIL) — a hard crash that no try/except can catch. That crash is harmless to the
    # pipeline (the parquet AND the status JSON above are already durable), but it
    # would spew a fatal-error trace into forward.log every night. The catalog is an
    # ad-hoc query convenience, not part of the daily contract, so the nightly job
    # leaves it alone; rebuild it on demand with `eod_daily.py --catalog ...` or the
    # one-time grab's own rebuild. Guarded by ok>0 too (no point rescanning 2k+ files
    # when nothing changed).
    if do_catalog and ok > 0:
        try:
            storage.rebuild_catalog()
        except Exception as e:
            log(f"  catalog rebuild skipped: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
