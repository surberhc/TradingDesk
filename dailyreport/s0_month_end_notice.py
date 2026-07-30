"""
s0_month_end_notice.py — once-a-month heads-up that a Strategy 0 rebalance is due.

WHAT IT IS
----------
Strategy 0 (Adaptive All-Weather Core) rebalances PURE MONTHLY in live paper: the
signal is taken on the LAST TRADING DAY of the month and the trade executes the NEXT
trading session (T+1). See strategies\\strategies\\all_weather.py `_signal_dates`
(monthly = last trading day per calendar month) and strategies\\strategies\\config.py
REBALANCE_FREQUENCY="monthly", EXECUTION_LAG_DAYS=1.

So the owner only needs a nudge ONCE A MONTH — on the evening of the month-end signal
day — to know overnight that he should go review + execute the rebalance in the Control
Plane at the next session. This script runs every weekday evening (via the
S0MonthEndNotice scheduled task), self-checks the trading calendar, and emails ONLY on
that one signal day; every other evening it does nothing and exits 0.

SCOPE / SAFETY
--------------
Informational email to the owner only (same recipient as the nightly EOD report). It
touches NO order path, reads NO account, connects to NO gateway — it is a pure
CALENDAR/model decision. It reuses the existing EOD mailer (dailyreport\\mailer.py),
so there is no new mail path and no secret is read here. Not order-affecting: no
version bump.

The exact trade list still lives in the Control Plane (desk dashboard, port 8502,
"Control Plane — S0 rebalance"), behind the arm gate. This email is a heads-up based
on the calendar signal, not a computed trade list. Follow-up: enrich the subject with
an exact trade / no-trade verdict once a month-end account read is wired (the gateway
is down evenings, so that read cannot happen here yet).

CALENDAR SOURCE
---------------
Uses the desk's single verified NYSE trading calendar,
connections\\connections\\market_calendar.py (hand-verified NYSE holiday tables), for
a true FORWARD look — "is TODAY a trading day AND is there no later trading day in the
same calendar month?" If the calendar has no verified table for the relevant year it
degrades LOUDLY to a weekday-only approximation (Mon-Fri, no holiday awareness) and
SAYS SO in the email body + on stderr. That approximation can only mis-fire around a
month-end market holiday (e.g. it would call a Fri before a closed month-end-Mon "not
the signal day"); the fix is to add the year to market_calendar.py.

USAGE
-----
    <venv python> s0_month_end_notice.py              # real run (task uses this)
    <venv python> s0_month_end_notice.py --dry-run    # decide + print, send nothing
    <venv python> s0_month_end_notice.py --dry-run --as-of 2026-07-31
    <venv python> s0_month_end_notice.py --as-of 2026-07-31   # force-decide a date

Idempotent: on a real send it writes a last-sent-date marker under
C:\\TradingDesk-Local\\state\\ (off-repo); a second real run the same day will not
re-send. --dry-run never writes the marker and never sends.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

# --- repo-relative path setup (derive from __file__, per CLAUDE.md) --------- #
# dailyreport is a direct child of the repo root; connections\connections holds the
# `connections` package that exposes market_calendar. Add the repo's connections dir
# so `from connections import market_calendar` resolves, and this file's own dir so
# `import mailer` resolves, regardless of the cwd the task launches us from.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _p in (str(_HERE), str(_REPO / "connections")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Off-repo state marker (never synced, never committed).
_STATE_DIR = Path(r"C:\TradingDesk-Local\state\dailyreport")
_MARKER = _STATE_DIR / "s0_month_end_notice_last_sent.txt"

# Control Plane pointer used in the email body (desk dashboard on :8502).
_CONTROL_PLANE = 'the Control Plane (desk dashboard, port 8502, "Control Plane — S0 rebalance")'

SUBJECT = "S0: month-end rebalance due — review & execute next session"


def _log(msg: str) -> None:
    """Everything goes to stderr so a real run's stdout stays clean and the task log
    still captures diagnostics. Never raises."""
    try:
        print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Trading-calendar decision — the whole point of the script.
# --------------------------------------------------------------------------- #
def _calendar():
    """Return the verified market_calendar module, or None if it cannot be imported."""
    try:
        from connections import market_calendar as mc
        return mc
    except Exception as e:  # import path problem — fall back, loudly
        _log(f"market_calendar unavailable ({type(e).__name__}: {e}); "
             f"using weekday-only approximation")
        return None


def _next_trading_day(as_of: dt.date) -> tuple[dt.date, bool]:
    """The next trading session strictly after `as_of`.

    Returns (date, approximated). approximated=True means the verified calendar was
    unavailable (or raised for an un-tabled year) and a weekday-only rule was used —
    so the caller can flag the limitation. The weekday rule skips Sat/Sun but is
    HOLIDAY-BLIND, so it can be wrong the day before a market holiday.
    """
    mc = _calendar()
    if mc is not None:
        try:
            return mc.next_trading_day(as_of, inclusive=False), False
        except Exception as e:
            _log(f"next_trading_day({as_of}) fell back to weekday rule "
                 f"({type(e).__name__}: {e})")
    d = as_of + dt.timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d += dt.timedelta(days=1)
    return d, True


def is_month_end_signal_day(as_of: dt.date) -> bool:
    """True iff `as_of` is Strategy 0's month-end rebalance SIGNAL day: today is a
    trading session AND there is no later trading session in the same calendar month
    (a forward look). This mirrors `_signal_dates(..., "monthly")`, which takes the
    last trading day of each calendar month.

    Uses the verified NYSE calendar when available; degrades to a weekday-only rule
    (Mon-Fri, holiday-blind) otherwise. Never raises.
    """
    mc = _calendar()
    if mc is not None:
        try:
            if not mc.is_trading_day(as_of):
                return False
            nxt = mc.next_trading_day(as_of, inclusive=False)
            return nxt.month != as_of.month
        except Exception as e:
            _log(f"calendar decision for {as_of} fell back to weekday rule "
                 f"({type(e).__name__}: {e})")
    # Weekday-only approximation (holiday-blind).
    if as_of.weekday() >= 5:
        return False
    nxt = as_of + dt.timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += dt.timedelta(days=1)
    return nxt.month != as_of.month


# --------------------------------------------------------------------------- #
# Email body
# --------------------------------------------------------------------------- #
def _build_body(as_of: dt.date, next_session: dt.date, approximated: bool) -> tuple[str, str]:
    """Return (plain_text, html) for the notice. Short and plain-English."""
    nice_next = next_session.strftime("%A, %b %d, %Y")
    nice_today = as_of.strftime("%A, %b %d, %Y")

    approx_note = ""
    approx_html = ""
    if approximated:
        approx_note = (
            "\n\nNote: the verified NYSE calendar was unavailable, so this used a "
            "weekday-only rule (holiday-blind). Double-check the next-session date "
            "against the market calendar, and add the year to market_calendar.py.")
        approx_html = (
            '<div style="font-size:12px;background:#fef3c7;color:#92400e;'
            'border-radius:6px;padding:7px 10px;margin:10px 0;">'
            "Verified NYSE calendar was unavailable — used a weekday-only rule "
            "(holiday-blind). Double-check the next-session date and add the year to "
            "market_calendar.py.</div>")

    text = (
        f"Strategy 0 month-end rebalance is due.\n\n"
        f"The S0 (Adaptive All-Weather Core) month-end signal fired at today's close "
        f"({nice_today}). A rebalance is scheduled for the next trading session, "
        f"{nice_next}.\n\n"
        f"Overnight or before that session, open {_CONTROL_PLANE} to review the exact "
        f"trades and execute them behind the arm gate. If the preview there shows 0 "
        f"legs, the account already conforms and nothing needs doing.\n\n"
        f"This is a heads-up based on the calendar signal — the exact trade list lives "
        f"in the Control Plane, not in this email."
        f"{approx_note}\n"
    )

    html = (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
        f'max-width:640px;margin:0 auto;color:#111827;">'
        f'<div style="font-size:18px;font-weight:700;">'
        f'Strategy 0 — month-end rebalance due</div>'
        f'<div style="font-size:14px;color:#374151;margin-top:8px;line-height:1.5;">'
        f'The S0 (Adaptive All-Weather Core) month-end signal fired at today\'s close '
        f'(<b>{nice_today}</b>). A rebalance is scheduled for the next trading session, '
        f'<b>{nice_next}</b>.</div>'
        f'{approx_html}'
        f'<div style="font-size:14px;color:#374151;margin-top:12px;line-height:1.5;">'
        f'Overnight or before that session, open {_CONTROL_PLANE} to review the exact '
        f'trades and execute them behind the arm gate. If the preview there shows '
        f'<b>0 legs</b>, the account already conforms and nothing needs doing.</div>'
        f'<div style="font-size:12px;color:#6b7280;margin-top:12px;line-height:1.5;">'
        f'This is a heads-up based on the calendar signal — the exact trade list lives '
        f'in the Control Plane, not in this email.</div>'
        f'<div style="font-size:11px;color:#9ca3af;margin-top:14px;">'
        f'Automated once-a-month notice · TradingDesk\\dailyreport\\s0_month_end_notice.py'
        f'</div></div>')

    return text, html


# --------------------------------------------------------------------------- #
# Idempotency marker
# --------------------------------------------------------------------------- #
def _already_sent(as_of: dt.date) -> bool:
    try:
        if not _MARKER.exists():
            return False
        return _MARKER.read_text(encoding="utf-8").strip() == as_of.isoformat()
    except Exception as e:
        _log(f"could not read marker ({type(e).__name__}: {e}); assuming not sent")
        return False


def _write_marker(as_of: dt.date) -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _MARKER.write_text(as_of.isoformat(), encoding="utf-8")
    except Exception as e:
        _log(f"could not write marker ({type(e).__name__}: {e})")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    """Decide + (maybe) send. Returns a process exit code. Never raises; any error
    logs to stderr and returns non-zero WITHOUT sending a malformed email."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="compute the decision and print subject+body; send nothing, "
                         "write no marker.")
    ap.add_argument("--as-of", metavar="YYYY-MM-DD", default=None,
                    help="override 'today' (for testing).")
    args = ap.parse_args(argv)

    try:
        as_of = (dt.date.fromisoformat(args.as_of) if args.as_of else dt.date.today())
    except ValueError as e:
        _log(f"bad --as-of value {args.as_of!r}: {e}")
        return 2

    try:
        signal_day = is_month_end_signal_day(as_of)
    except Exception as e:  # belt-and-suspenders; the fn already guards
        _log(f"FATAL deciding signal day for {as_of}: {type(e).__name__}: {e}")
        return 1

    if not signal_day:
        _log(f"{as_of} is not the S0 month-end signal day — nothing to do.")
        if args.dry_run:
            print(f"[dry-run] {as_of} is NOT the month-end signal day. "
                  f"No email would be sent.")
        return 0

    # It IS the signal day — build the notice.
    try:
        next_session, approximated = _next_trading_day(as_of)
        text, html = _build_body(as_of, next_session, approximated)
    except Exception as e:
        _log(f"FATAL building notice for {as_of}: {type(e).__name__}: {e}")
        return 1

    if args.dry_run:
        print(f"[dry-run] {as_of} IS the month-end signal day. Would send:")
        print(f"  To:      (EOD report recipient)")
        print(f"  Subject: {SUBJECT}")
        print(f"  Next session: {next_session.isoformat()}"
              f"{'  (weekday approximation!)' if approximated else ''}")
        print("  --- body (plain text) ---")
        print(text)
        return 0

    # Real send path.
    if _already_sent(as_of):
        _log(f"already sent for {as_of} (marker present) — not re-sending.")
        return 0

    try:
        import mailer
        ok = mailer.send_html(SUBJECT, html)
    except Exception as e:
        _log(f"FATAL sending notice for {as_of}: {type(e).__name__}: {e}")
        return 1

    if ok:
        _write_marker(as_of)
        _log(f"sent S0 month-end notice for {as_of} (next session {next_session}).")
        return 0
    _log(f"mailer reported send FAILED for {as_of} (see mailer's Desktop flag).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
