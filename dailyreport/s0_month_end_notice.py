"""
s0_month_end_notice.py — Job B: the EVENING EXACT month-end verdict for Strategy 0.

WHAT IT IS
----------
Strategy 0 (Adaptive All-Weather Core) rebalances PURE MONTHLY in live paper: the signal is
taken on the LAST TRADING DAY of the month and the trade executes the NEXT session (T+1). See
strategies\\strategies\\all_weather.py `_signal_dates` (monthly = last trading day per calendar
month) and strategies\\strategies\\config.py REBALANCE_FREQUENCY="monthly", EXECUTION_LAG_DAYS=1.

This is the second of a two-job pair that turns that once-a-month nudge into an EXACT
"TRADE tomorrow / NO trade tomorrow" heads-up:

  * Job A (s0_month_end_snapshot.py, ~2:50pm CT) reads the account's ACTUAL holdings +
    NetLiquidation at the close, while the live-trading Gateway is still up, and writes them
    to an off-repo JSON.
  * Job B (THIS file, ~7:15pm CT, AFTER the ~7pm Tiingo close-data pull) loads that snapshot,
    computes S0's target on the FINAL close data (strategy_target.current_target), sizes the
    plan against the snapshotted holdings (rebalance_engine.plan_account, the UNCHANGED
    planner), counts the resulting order legs, and emails one of three exact verdicts:
      - legs > 0 -> "S0: TRADE tomorrow — <N> leg(s) at next open"  (+ the sells/buys)
      - legs == 0 -> "S0: NO trade tomorrow — account already conforms"
      - snapshot missing/failed -> "S0: month-end — could not read holdings at close"
        (FAIL-HONEST — it NEVER guesses a verdict when it could not read the account)

Runs every weekday evening; self-checks the trading calendar and only acts on the month-end
SIGNAL day. Every other evening it does nothing and exits 0.

SCOPE / SAFETY — INFORMATIONAL + READ-ONLY
------------------------------------------
Informational email to the owner only (same recipient as the nightly EOD report). Job B
itself connects to NO gateway and reads NO account live — it only reads the JSON snapshot Job
A already wrote and runs the pure, offline planner. It transmits NOTHING and touches no order
path. It reuses the existing EOD mailer (dailyreport\\mailer.py). Not order-affecting: no
paperbot version bump. The exact trade list still lives behind the arm gate in the Control
Plane; this email is a heads-up computed from the snapshot + model, not a transmit path.

CALENDAR SOURCE
---------------
Uses the desk's single verified NYSE trading calendar,
connections\\connections\\market_calendar.py, for a true FORWARD look — "is TODAY a trading
day AND is there no later trading day in the same calendar month?". If the calendar has no
verified table for the relevant year it degrades LOUDLY to a weekday-only approximation
(Mon-Fri, no holiday awareness) and SAYS SO in the email body + on stderr.

USAGE
-----
    <venv python> s0_month_end_notice.py              # real run (task uses this)
    <venv python> s0_month_end_notice.py --dry-run    # decide + print, send nothing
    <venv python> s0_month_end_notice.py --dry-run --as-of 2026-07-31
    <venv python> s0_month_end_notice.py --as-of 2026-07-31   # force-decide a date

The snapshot file path can be overridden with the env var TRADINGDESK_S0_MONTHEND_SNAPSHOT
(shared with Job A; used by tests/verification so the real state file is never touched).

Idempotent: on a real send it writes a last-sent-date marker under C:\\TradingDesk-Local\\state\\
(off-repo); a second real run the same day will not re-send. --dry-run never writes the marker
and never sends.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

# --- repo-relative path setup (derive from __file__, per CLAUDE.md) --------- #
# dailyreport is a direct child of the repo root. We need:
#   * this file's own dir           -> `import mailer`
#   * <repo>\connections            -> `from connections import market_calendar`
#   * <repo>\paperbot               -> `import strategy_target`, `import rebalance_engine`
#   * <repo> (root)                 -> `from strategies.all_weather import universe`
# The paperbot/strategies imports are LAZY (inside compute_plan) so the calendar/verdict
# logic — and its tests — never require the backtester to import.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _p in (str(_HERE), str(_REPO / "connections"), str(_REPO / "paperbot"), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Off-repo state marker (never synced, never committed).
_STATE_DIR = Path(r"C:\TradingDesk-Local\state\dailyreport")
_MARKER = _STATE_DIR / "s0_month_end_notice_last_sent.txt"

# Default snapshot file Job A writes; overridable (shared with Job A) for tests/verification.
_DEFAULT_SNAPSHOT = Path(r"C:\TradingDesk-Local\state\dailyreport\s0_month_end_snapshot.json")

# The model version whose target drives the verdict (owner's choice for the S0 pilot account).
STRATEGY_VERSION = "Growth"

# Control Plane pointer used in the email body (desk dashboard on :8502).
_CONTROL_PLANE = 'the Control Plane (desk dashboard, port 8502, "Control Plane — S0 rebalance")'

# --- verdict subjects ------------------------------------------------------- #
SUBJECT_NO_TRADE = "S0: NO trade tomorrow — account already conforms"
SUBJECT_NO_READ = "S0: month-end — could not read holdings at close"


def subject_trade(n_legs: int) -> str:
    """TRADE subject with the exact leg count."""
    return f"S0: TRADE tomorrow — {n_legs} leg{'s' if n_legs != 1 else ''} at next open"


def snapshot_path() -> Path:
    env = os.environ.get("TRADINGDESK_S0_MONTHEND_SNAPSHOT")
    return Path(env) if env else _DEFAULT_SNAPSHOT


def _log(msg: str) -> None:
    """Everything goes to stderr so a real run's stdout stays clean. Never raises."""
    try:
        print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Trading-calendar decision.
# --------------------------------------------------------------------------- #
def _calendar():
    """Return the verified market_calendar module, or None if it cannot be imported."""
    try:
        from connections import market_calendar as mc
        return mc
    except Exception as e:
        _log(f"market_calendar unavailable ({type(e).__name__}: {e}); "
             f"using weekday-only approximation")
        return None


def _next_trading_day(as_of: dt.date) -> tuple[dt.date, bool]:
    """The next trading session strictly after `as_of`.

    Returns (date, approximated). approximated=True means the verified calendar was
    unavailable (or raised for an un-tabled year) and a weekday-only rule was used.
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
    """True iff `as_of` is Strategy 0's month-end rebalance SIGNAL day: today is a trading
    session AND there is no later trading session in the same calendar month (a forward look).
    Uses the verified NYSE calendar when available; degrades to a weekday-only rule
    (Mon-Fri, holiday-blind) otherwise. Never raises."""
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
    if as_of.weekday() >= 5:
        return False
    nxt = as_of + dt.timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += dt.timedelta(days=1)
    return nxt.month != as_of.month


# --------------------------------------------------------------------------- #
# Snapshot loading + plan computation.
# --------------------------------------------------------------------------- #
def load_snapshot(as_of: dt.date) -> dict | None:
    """Load Job A's close-time holdings snapshot for `as_of`.

    Returns the parsed dict if the file exists and is stamped for `as_of`; None if the file
    is missing, unreadable, or stamped for a different date (a stale prior-day snapshot must
    NOT be treated as today's). A dict with ok=false (Job A's FAILED marker) is returned as-is
    so the caller can fail HONESTLY.
    """
    p = snapshot_path()
    try:
        if not p.exists():
            _log(f"no snapshot at {p}")
            return None
        rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"could not read snapshot {p} ({type(e).__name__}: {e})")
        return None
    if not isinstance(rec, dict):
        _log(f"snapshot {p} is not a JSON object; ignoring")
        return None
    if rec.get("as_of") != as_of.isoformat():
        _log(f"snapshot {p} is stamped {rec.get('as_of')!r}, not today ({as_of}); ignoring "
             f"(stale snapshot is never treated as today's).")
        return None
    return rec


def compute_plan(snapshot: dict, version: str = STRATEGY_VERSION):
    """Compute S0's target on the FINAL close data and size it against the snapshotted
    holdings with the UNCHANGED planner. Returns (target, plan). Pure/offline — heavy
    imports (backtester brain + planner) are done here so the calendar/verdict logic stays
    import-light and unit-testable without the backtester.

    Raises on any computation failure so the caller fails HONESTLY rather than guessing.
    """
    import rebalance_engine
    import strategy_target

    account = snapshot.get("account", "")
    net_liq = float(snapshot["net_liq"])
    positions = {str(s): float(q) for s, q in (snapshot.get("positions") or {}).items()}

    target = strategy_target.current_target(version=version)

    try:
        from strategies.all_weather import universe as _s0_universe
        universe = _s0_universe()
    except Exception as e:
        _log(f"could not resolve strategy universe ({e}); falling back to legacy "
             f"UNTRACKED classification.")
        universe = None

    plan = rebalance_engine.plan_account(account, version, net_liq, positions, target,
                                         universe=universe)
    return target, plan


# --------------------------------------------------------------------------- #
# Verdict classification + email bodies (PURE).
# --------------------------------------------------------------------------- #
def verdict_case(snapshot: dict | None, plan) -> str:
    """Classify the evening verdict. PURE — the single source of truth for which of the
    three emails goes out:
        'no_read'  -> snapshot missing / failed / could not compute a plan
        'trade'    -> plan has >= 1 order leg
        'no_trade' -> plan has 0 order legs (account already conforms)
    """
    if snapshot is None or not snapshot.get("ok") or plan is None:
        return "no_read"
    return "trade" if len(plan.orders) > 0 else "no_trade"


def _order_lines(plan, prices: dict) -> list[str]:
    """Human-readable sell/buy lines from an AccountPlan's signed-integer orders."""
    lines: list[str] = []
    for sym in sorted(plan.orders, key=lambda s: (plan.orders[s] >= 0, s)):
        delta = plan.orders[sym]
        verb = "BUY " if delta > 0 else "SELL"
        px = prices.get(sym)
        px_txt = f"@~{px:,.2f}" if isinstance(px, (int, float)) and px == px else ""
        lines.append(f"    {verb} {sym:<6} x{abs(delta):<8} {px_txt}".rstrip())
    return lines


def build_verdict(as_of: dt.date, next_session: dt.date, approximated: bool,
                  snapshot: dict | None, plan, prices: dict | None,
                  error: str | None = None) -> tuple[str, str, str]:
    """Return (subject, plain_text, html) for the evening verdict. PURE — no I/O, no send.

    `error` (optional) is a short reason string surfaced in the NO_READ body (e.g. why the
    snapshot was missing or the plan could not be computed)."""
    nice_next = next_session.strftime("%A, %b %d, %Y")
    nice_today = as_of.strftime("%A, %b %d, %Y")
    case = verdict_case(snapshot, plan)

    approx_note = ""
    approx_html = ""
    if approximated:
        approx_note = (
            "\n\nNote: the verified NYSE calendar was unavailable, so this used a "
            "weekday-only rule (holiday-blind). Double-check the next-session date against "
            "the market calendar, and add the year to market_calendar.py.")
        approx_html = (
            '<div style="font-size:12px;background:#fef3c7;color:#92400e;border-radius:6px;'
            'padding:7px 10px;margin:10px 0;">Verified NYSE calendar was unavailable — used a '
            "weekday-only rule (holiday-blind). Double-check the next-session date and add the "
            "year to market_calendar.py.</div>")

    def _wrap(headline: str, color: str, body_html: str) -> str:
        return (
            f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
            f'max-width:640px;margin:0 auto;color:#111827;">'
            f'<div style="font-size:18px;font-weight:700;color:{color};">{headline}</div>'
            f'{approx_html}{body_html}'
            f'<div style="font-size:11px;color:#9ca3af;margin-top:14px;">'
            f'Automated once-a-month S0 verdict · TradingDesk\\dailyreport\\s0_month_end_notice.py'
            f'</div></div>')

    if case == "no_read":
        why = f" ({error})" if error else ""
        text = (
            f"S0 month-end — could NOT read the holdings at today's close{why}.\n\n"
            f"Today ({nice_today}) is Strategy 0's month-end signal day, and a rebalance is "
            f"scheduled for the next trading session, {nice_next}. But the close-time holdings "
            f"snapshot is missing or failed, so this notice CANNOT compute an exact "
            f"trade / no-trade verdict — and it will not guess one.\n\n"
            f"In the morning, open {_CONTROL_PLANE} to check the exact trades and execute them "
            f"behind the arm gate if needed. If the preview there shows 0 legs, the account "
            f"already conforms and nothing needs doing."
            f"{approx_note}\n")
        html = _wrap(
            "S0 month-end — could not read holdings at close", "#b45309",
            f'<div style="font-size:14px;color:#374151;margin-top:8px;line-height:1.5;">'
            f'Today (<b>{nice_today}</b>) is Strategy 0\'s month-end signal day; a rebalance is '
            f'scheduled for the next session, <b>{nice_next}</b>. But the close-time holdings '
            f'snapshot is <b>missing or failed</b>{why}, so this notice cannot compute an exact '
            f'verdict — and it will not guess one.</div>'
            f'<div style="font-size:14px;color:#374151;margin-top:12px;line-height:1.5;">'
            f'In the morning, open {_CONTROL_PLANE} to check the exact trades and execute them '
            f'behind the arm gate if needed. If the preview there shows <b>0 legs</b>, the '
            f'account already conforms and nothing needs doing.</div>')
        return SUBJECT_NO_READ, text, html

    if case == "no_trade":
        text = (
            f"S0 month-end — NO trade tomorrow.\n\n"
            f"Today ({nice_today}) is Strategy 0's month-end signal day. Based on the "
            f"close-time holdings and the model target computed on the final close, the account "
            f"already conforms: 0 order legs. Nothing needs to be done at the next session "
            f"({nice_next}).\n\n"
            f"No action required. (You can still open {_CONTROL_PLANE} to confirm.)"
            f"{approx_note}\n")
        html = _wrap(
            "S0 month-end — NO trade tomorrow", "#166534",
            f'<div style="font-size:14px;color:#374151;margin-top:8px;line-height:1.5;">'
            f'Today (<b>{nice_today}</b>) is Strategy 0\'s month-end signal day. Based on the '
            f'close-time holdings and the model target computed on the final close, the account '
            f'<b>already conforms: 0 order legs</b>. Nothing needs doing at the next session '
            f'(<b>{nice_next}</b>).</div>'
            f'<div style="font-size:13px;color:#6b7280;margin-top:12px;line-height:1.5;">'
            f'No action required. You can still open {_CONTROL_PLANE} to confirm.</div>')
        return SUBJECT_NO_TRADE, text, html

    # case == "trade"
    prices = prices or {}
    n = len(plan.orders)
    order_lines = _order_lines(plan, prices)
    text = (
        f"S0 month-end — TRADE tomorrow ({n} leg{'s' if n != 1 else ''}).\n\n"
        f"Today ({nice_today}) is Strategy 0's month-end signal day. Based on the close-time "
        f"holdings and the model target computed on the final close, the account needs a "
        f"rebalance at the next session ({nice_next}) — {n} order leg"
        f"{'s' if n != 1 else ''}:\n\n"
        + "\n".join(order_lines) +
        f"\n\nBefore that session, open {_CONTROL_PLANE} to review these exact trades and "
        f"execute them behind the arm gate."
        f"{approx_note}\n")

    rows = "".join(
        f'<tr><td style="padding:2px 10px 2px 0;font-weight:600;'
        f'color:{"#166534" if plan.orders[s] > 0 else "#b91c1c"};">'
        f'{"BUY" if plan.orders[s] > 0 else "SELL"}</td>'
        f'<td style="padding:2px 10px 2px 0;">{s}</td>'
        f'<td style="padding:2px 10px 2px 0;">x{abs(plan.orders[s])}</td>'
        f'<td style="padding:2px 0;color:#6b7280;">'
        f'{("@~%0.2f" % prices[s]) if isinstance(prices.get(s), (int, float)) and prices.get(s) == prices.get(s) else ""}</td></tr>'
        for s in sorted(plan.orders, key=lambda s: (plan.orders[s] >= 0, s)))
    html = _wrap(
        f"S0 month-end — TRADE tomorrow ({n} leg{'s' if n != 1 else ''})", "#b91c1c",
        f'<div style="font-size:14px;color:#374151;margin-top:8px;line-height:1.5;">'
        f'Today (<b>{nice_today}</b>) is Strategy 0\'s month-end signal day. Based on the '
        f'close-time holdings and the model target computed on the final close, the account '
        f'needs a rebalance at the next session (<b>{nice_next}</b>) — <b>{n} order '
        f'leg{"s" if n != 1 else ""}</b>:</div>'
        f'<table style="font-size:14px;margin-top:10px;border-collapse:collapse;">{rows}</table>'
        f'<div style="font-size:14px;color:#374151;margin-top:12px;line-height:1.5;">'
        f'Before that session, open {_CONTROL_PLANE} to review these exact trades and execute '
        f'them behind the arm gate.</div>')
    return subject_trade(n), text, html


# --------------------------------------------------------------------------- #
# Idempotency marker.
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
# Main.
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    """Decide + (maybe) send the evening verdict. Returns a process exit code. Never raises;
    any error logs to stderr. FAIL-HONEST: a missing snapshot or a failed target/plan
    computation sends the 'could not read holdings' email, never a guessed verdict."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="compute the verdict and print subject+body; send nothing, write no "
                         "marker.")
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
    except Exception as e:
        _log(f"FATAL deciding signal day for {as_of}: {type(e).__name__}: {e}")
        return 1

    if not signal_day:
        _log(f"{as_of} is not the S0 month-end signal day — nothing to do.")
        if args.dry_run:
            print(f"[dry-run] {as_of} is NOT the month-end signal day. No email would be sent.")
        return 0

    # It IS the signal day. Load Job A's snapshot; compute the plan if we have holdings.
    next_session, approximated = _next_trading_day(as_of)
    snapshot = load_snapshot(as_of)
    plan = None
    prices: dict = {}
    error: str | None = None

    if snapshot is None:
        error = "no snapshot file for today"
    elif not snapshot.get("ok"):
        error = f"snapshot marked failed: {snapshot.get('error', 'unknown error')}"
    else:
        try:
            target, plan = compute_plan(snapshot)
            prices = {s: float(target.prices.get(s)) for s in
                      set(plan.orders) | set(snapshot.get("positions") or {})
                      if target.prices.get(s) is not None}
        except Exception as e:
            _log(f"could not compute plan for {as_of}: {type(e).__name__}: {e}")
            plan = None
            error = f"could not compute the target/plan: {type(e).__name__}: {e}"

    try:
        subject, text, html = build_verdict(as_of, next_session, approximated,
                                            snapshot, plan, prices, error=error)
    except Exception as e:
        _log(f"FATAL building verdict for {as_of}: {type(e).__name__}: {e}")
        return 1

    if args.dry_run:
        print(f"[dry-run] {as_of} IS the month-end signal day. Would send:")
        print(f"  To:      (EOD report recipient)")
        print(f"  Subject: {subject}")
        print(f"  Next session: {next_session.isoformat()}"
              f"{'  (weekday approximation!)' if approximated else ''}")
        print("  --- body (plain text) ---")
        print(text)
        return 0

    if _already_sent(as_of):
        _log(f"already sent for {as_of} (marker present) — not re-sending.")
        return 0

    try:
        import mailer
        ok = mailer.send_html(subject, html)
    except Exception as e:
        _log(f"FATAL sending verdict for {as_of}: {type(e).__name__}: {e}")
        return 1

    if ok:
        _write_marker(as_of)
        _log(f"sent S0 month-end verdict for {as_of}: {subject!r}")
        return 0
    _log(f"mailer reported send FAILED for {as_of} (see mailer's Desktop flag).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
