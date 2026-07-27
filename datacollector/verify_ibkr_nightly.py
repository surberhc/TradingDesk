"""verify_ibkr_nightly.py — ONE-SHOT "did tonight's IBKR EOD pull succeed?" checker.

WHY THIS EXISTS (read this first)
---------------------------------
On 2026-07-27 the nightly EOD option feed cut over from the (now-lapsed) ThetaData
subscription to the IBKR forward collector. The scheduled ``IbkrForwardEodDaily`` task runs
at 17:30 CT and fires ``forward_daily_live.py SPX SPXW RUT NDX``, which snapshots each root's
EOD option chain and writes one parquet per root per day to the MAIN warehouse namespace at
``config.RAW_OPTIONS/{ROOT}/{YYYYMMDD}.parquet``.

This module is the independent confirmation that it actually happened. Run once at ~18:45 CT
(a separate, reviewed Scheduled Task — this build does NOT register it), it looks on disk for
today's parquet for each root, counts the rows, and emails Andrew a single PASS/FAIL. It does
NOT re-run the collection, connect to IBKR, or read the jobstatus record — it verifies the
OBSERVABLE OUTPUT (the files the collector is supposed to have produced), so a collector that
lies about its own success (writes a "forward ok" status but no files) is still caught.

BEST-EFFORT, ALWAYS (never raises)
----------------------------------
A checker is a secondary safety channel. A missing warehouse dir, a corrupt parquet, a broken
mailer — none of it may turn into a traceback out of the scheduler. ``check()`` catches
everything and returns a result dict; ``main()`` always returns rc 0.

NO NEW CREDENTIALS: email goes out through the SAME existing ``dailyreport`` mailer every
other desk alert uses (``dailyreport.mailer.send_html``). This module introduces no mail
config and no secrets of its own, and it has no order path of any kind — it reads files and
sends one email.

PURE SEAMS: the date, the row-count reader, and the send path are all injectable, so every
path here is offline-testable with no real warehouse, no real parquet, and no real mail.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Tuple
from zoneinfo import ZoneInfo

# Self-contained sys.path shim so ``import config`` resolves against this repo's datacollector
# folder regardless of how the checker is launched (the venv editable installs still point at
# the deleted pre-2026-07-16 My Drive path — same rationale as the sibling modules).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402  (datacollector warehouse paths — RAW_OPTIONS lives here)

_CT_ZONE = ZoneInfo("America/Chicago")

# The four roots IbkrForwardEodDaily collects nightly (forward_daily_live.py SPX SPXW RUT NDX).
DEFAULT_ROOTS: Tuple[str, ...] = ("SPX", "SPXW", "RUT", "NDX")


def _resolve_today(today: Any = None) -> str:
    """Today's CT date as YYYYMMDD. Injectable for tests (pass a string/date-like); otherwise
    read from the America/Chicago wall clock so it matches the daystr forward_daily_live.py
    used at 17:30 CT the same evening."""
    if today is not None:
        return str(today)
    return datetime.now(tz=_CT_ZONE).strftime("%Y%m%d")


def _read_rows_pandas(path) -> int:
    """Row count of a parquet file via pandas. A missing OR corrupt OR zero-column
    "no-data-day" marker file all resolve to 0 rows — NEVER raises. 0 rows is a real signal
    here (the collector writes a 0-row marker for a holiday / no-data root), and 0 fails the
    check, which is the intended behavior on a night we expected data."""
    try:
        import pandas as pd  # noqa: PLC0415 — lazy so the module imports without pandas
        if not os.path.exists(path):
            return 0
        return int(len(pd.read_parquet(path)))
    except Exception:  # noqa: BLE001 — missing/corrupt/unreadable all count as 0 rows
        return 0


def _default_mailer():
    """The EXISTING dailyreport mailer — same module/path the S8 alerts and the EOD report
    already use. Imported lazily (with a repo-derived sys.path shim) so the offline tests
    never touch mail config or credentials. No new mail config is introduced here."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(repo_root, "dailyreport")
    if p not in sys.path:
        sys.path.insert(0, p)
    import mailer  # noqa: PLC0415
    return mailer


def _default_send(subject: str, lines: Iterable[Any], *, mailer=None,
                  log: Callable[[str], Any] = print) -> bool:
    """Send ONE email through ``dailyreport.mailer.send_html`` — the same helper and the same
    credentials the rest of the desk uses. Body is a <pre> block of the given lines. Never
    raises (a broken mailer returns False, it does not take the checker down)."""
    try:
        m = mailer if mailer is not None else _default_mailer()
        body = "\n".join(str(x) for x in lines)
        html = "<html><body><pre>" + body + "</pre></body></html>"
        return bool(m.send_html(subject, html))
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort, always
        try:
            log(f"verify_ibkr_nightly: email send failed ({type(exc).__name__}: {exc})")
        except Exception:  # noqa: BLE001
            pass
        return False


def _build_email(daystr: str, counts: Dict[str, int], missing: List[str],
                 passed: bool) -> Tuple[str, List[str]]:
    """(subject, body-lines) for the result email. Subject leads with PASS/FAIL so it reads at
    a glance on a phone; on FAIL it names the missing/empty roots inline."""
    if passed:
        subject = "IBKR nightly EOD: PASS"
        headline = f"All {len(counts)} roots have a today parquet with rows. Nightly EOD pull OK."
    else:
        subject = "IBKR nightly EOD: FAIL - " + ",".join(missing) + " missing"
        headline = ("MISSING/EMPTY today: " + ", ".join(missing) +
                    " -- the nightly EOD pull did NOT fully land.")
    lines = [
        headline,
        "",
        f"Date checked (CT) ..... {daystr}",
        "",
        "Per-root row counts (raw/options/{ROOT}/" + daystr + ".parquet):",
    ]
    for root in counts:
        rows = counts[root]
        mark = "OK " if rows > 0 else "!! "
        lines.append(f"  {mark}{root:6} rows={rows}")
    lines += [
        "",
        "This replaced the retired ThetaData nightly EOD feed as of the 2026-07-27 cutover; "
        "the source of these files is now the IBKR forward collector (IbkrForwardEodDaily, "
        "17:30 CT).",
        "",
        "This is a DATA-integrity check only — it reads warehouse files and sends this one "
        "email. It has no order path.",
    ]
    return subject, lines


def check(
    *,
    today: Any = None,
    roots: Iterable[str] = DEFAULT_ROOTS,
    read_rows: Callable[[Any], int] = _read_rows_pandas,
    send: Callable[..., bool] = _default_send,
    log: Callable[[str], Any] = print,
) -> Dict[str, Any]:
    """One-shot verification of tonight's IBKR EOD pull. Sends exactly ONE email; never raises.

    For each root, checks ``config.RAW_OPTIONS/{ROOT}/{today}.parquet`` for its row count via
    the injected ``read_rows`` (default: pandas, missing/corrupt -> 0). PASS iff EVERY root has
    a today parquet with > 0 rows; otherwise FAIL, listing the missing/empty roots.

    Returns ``{"pass": bool, "roots": {root: rows}, "missing": [...], "emailed": bool,
    "date": YYYYMMDD, "error": None|str}``. On any internal failure it still returns a dict
    (pass=False, error set) and makes a best-effort attempt to email the failure.
    """
    roots = list(roots)
    result: Dict[str, Any] = {
        "pass": False, "roots": {}, "missing": list(roots),
        "emailed": False, "date": None, "error": None,
    }
    try:
        daystr = _resolve_today(today)
        result["date"] = daystr

        counts: Dict[str, int] = {}
        for root in roots:
            try:
                path = config.RAW_OPTIONS / root / f"{daystr}.parquet"
                rows = int(read_rows(path))
            except Exception as exc:  # noqa: BLE001 — one unreadable root is 0, not a crash
                log(f"verify_ibkr_nightly: reading {root} raised "
                    f"({type(exc).__name__}: {exc}); counting 0 rows.")
                rows = 0
            counts[root] = rows
        result["roots"] = counts

        missing = [r for r in roots if counts.get(r, 0) <= 0]
        result["missing"] = missing
        passed = (len(roots) > 0) and (len(missing) == 0)
        result["pass"] = passed

        subject, lines = _build_email(daystr, counts, missing, passed)
        try:
            emailed = bool(send(subject, lines, log=log))
        except Exception as exc:  # noqa: BLE001 — a raising mailer must not propagate
            log(f"verify_ibkr_nightly: send() raised ({type(exc).__name__}: {exc}); "
                f"treating as not emailed.")
            emailed = False
        result["emailed"] = emailed

        log(f"verify_ibkr_nightly: {daystr} pass={passed} roots={counts} "
            f"missing={missing} emailed={emailed}")
        return result
    except Exception as exc:  # noqa: BLE001 — a checker can NEVER raise into its caller
        result["error"] = f"{type(exc).__name__}: {exc}"
        try:
            log(f"verify_ibkr_nightly: check failed entirely ({result['error']}); "
                f"attempting a failure email.")
        except Exception:  # noqa: BLE001
            pass
        try:
            emailed = bool(send(
                "IBKR nightly EOD: FAIL - checker error",
                ["The IBKR nightly EOD verifier errored before it could confirm the pull.",
                 "", f"Error: {result['error']}",
                 "", f"Date (best-effort): {result.get('date')}",
                 "", "Check the warehouse and forward_live.log by hand tonight."],
                log=log))
            result["emailed"] = emailed
        except Exception:  # noqa: BLE001 — best-effort only
            pass
        return result


def main(argv=None) -> int:
    """One-shot entrypoint — run the check with real defaults, log the result, ALWAYS rc 0.

    rc is always 0: a nonzero exit would only add scheduled-task noise for an outcome (PASS /
    FAIL / error) that is already emailed and logged. ``check()`` already never raises, but
    ``main`` still guards the call so even a surprise failure there cannot become a nonzero rc
    or a traceback out of the launcher."""
    try:
        result = check()
    except Exception as exc:  # noqa: BLE001 — never let anything out of a best-effort checker
        result = {"pass": False, "roots": {}, "missing": None, "emailed": False,
                  "date": None, "error": f"{type(exc).__name__}: {exc}"}
    try:
        print(f"verify_ibkr_nightly: {result}")
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
