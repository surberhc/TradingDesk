"""
s4_daily_run.py — runnable DAILY driver for the S4 vol-control fund (review-only).

This is the entry point Andrew will SCHEDULE later (Windows Task Scheduler, run-whether-
logged-on — see products/S4_vol_control_fund/DEPLOY.md). It is NOT registered as a scheduled
task by this code, and it arms nothing.

What it does each run:
  1. TRADING-DAY GATE (market_calendar): if today is not a US trading session (weekend/
     holiday), it is a NO-OP — prints why and exits 0 without connecting. The S4 fund
     rebalances daily off the SPY close, so there is nothing to do on a non-session day.
  2. Otherwise it delegates to s4_rebalance_run.main(account, profile) — the single-account
     REVIEW-ONLY runner (build-only, transmits nothing, connects read-only behind the
     gateway lock).

The account id is a REQUIRED parameter (never hardcoded). Profile is a runtime dial.

Run (deploy):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe s4_daily_run.py --account DU89221XX --profile conservative
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from connections import market_calendar as mc

import s4_rebalance_run


def is_trading_today(today: dt.date | None = None) -> tuple[bool, str]:
    """(should_run, reason). False on weekends/holidays. If the calendar year is not yet
    tabled, FAIL CLOSED (do not run) with a loud reason — never guess the session."""
    today = dt.date.today() if today is None else today
    try:
        if not mc.is_trading_day(today):
            name = None
            try:
                name = mc.holiday_name(today)
            except mc.CalendarYearMissing:
                pass
            why = name or ("weekend" if mc.is_weekend(today) else "market closed")
            return False, f"{today} is not a trading session ({why})"
    except mc.CalendarYearMissing as exc:
        return False, f"calendar year not tabled — FAILING CLOSED, not running: {exc}"
    return True, f"{today} is a trading session"


def main(account: str | None = None, *, profile: str | None = None,
         target_vol: float | None = None, leverage_cap: float | None = None,
         today: dt.date | None = None) -> int:
    today = dt.date.today() if today is None else today
    run, reason = is_trading_today(today)
    print(f"[S4 daily] {reason}.")
    if not run:
        print("[S4 daily] NO-OP: nothing to review on a non-trading day. Exiting cleanly.")
        return 0
    if not account:
        print("[S4 daily] SAFETY STOP: no --account given (never hardcoded).")
        return 2
    return s4_rebalance_run.main(account=account, profile=profile,
                                 target_vol=target_vol, leverage_cap=leverage_cap,
                                 today=today)


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(description="S4 daily review-only driver (calendar-gated)")
    ap.add_argument("--account", required=True, help="paper account (DU/DF...) — never hardcoded")
    ap.add_argument("--profile", default=None, choices=["balanced", "conservative"],
                    help="named deploy cell; omit to use overrides or the conservative default")
    ap.add_argument("--target-vol", type=float, default=None)
    ap.add_argument("--leverage-cap", type=float, default=None)
    return ap.parse_args(argv)


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    args = _parse_args()
    sys.exit(main(account=args.account, profile=args.profile,
                  target_vol=args.target_vol, leverage_cap=args.leverage_cap))
