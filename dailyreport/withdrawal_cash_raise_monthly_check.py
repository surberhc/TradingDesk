"""
withdrawal_cash_raise_monthly_check.py — ONE consolidated monthly "raise withdrawal cash"
Action Center notice.

WHAT IT IS
----------
Andrew's explicit requirement (2026-09-05): the withdrawal-reserve shortfall check
(dailyreport/withdrawal_reserve_check.py, reused live via
paperbot/withdrawal_cash_raise.accounts_needing_cash() — NOT re-implemented here) already
posts ONE Action Center notice PER flagged account, refreshed daily. That per-account shape is
right for the daily nag, but wrong for a monthly rhythm: Andrew wants exactly ONE consolidated
notice per monthly cycle naming every account that currently needs cash raised, pointing at the
"Raise withdrawal cash" dashboard page (dashboard/desk/page_withdrawal_cash_raise.py) where the
report-driven, no-hand-picking trade can be prepared and sent by a human.

This file adds NO new shortfall logic. It is a thin monthly wrapper around
``withdrawal_cash_raise.accounts_needing_cash()`` (itself a thin reuse of
withdrawal_reserve_check's own accounts_to_check()/read_cash()/decide()): read the live
shortfall list, and if it is non-empty, post ONE notice — a single dedup_key
("withdrawal_cash_raise_monthly") for the WHOLE cycle, not one per account, so re-running
mid-cycle (e.g. the scheduled task fires more than once, or the operator re-runs by hand)
updates that ONE notice in place with fresh numbers instead of stacking duplicates.

It NEVER trades: read-only account reads only (via accounts_needing_cash(), which itself only
calls withdrawal_reserve_check.read_cash() — see that module's own docstring for the read-only
Gateway posture). There is no order path anywhere in this file.

Cadence: scheduled monthly, around the 20th of each month (WithdrawalCashRaiseMonthly
scheduled task) — a reminder rhythm, independent of the existing daily per-account nag, which
keeps running unchanged.

SCOPE / SAFETY — INFORMATIONAL + READ-ONLY, ZERO-TRANSMIT
----------------------------------------------------------
Calls accounts_needing_cash() only, which reads live via
withdrawal_reserve_check.read_cash() (readonly=True on the live-trading Gateway, port 4003).
Builds no order, opens no broker write connection, transmits nothing.

USAGE
-----
    <venv python> withdrawal_cash_raise_monthly_check.py            # real run (task uses this)
    <venv python> withdrawal_cash_raise_monthly_check.py --dry-run  # read live, print, post NOTHING
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _p in (str(_HERE), str(_REPO / "connections"), str(_REPO / "paperbot"),
           str(_REPO / "dashboard" / "desk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# cashflows.describe() (reached via withdrawal_reserve_check) embeds real Unicode punctuation
# that stderr logging below can echo. Windows' default console codepage (cp1252) can't encode
# it, so make stdout/stderr UTF-8-safe up front rather than crash mid-run on an otherwise-
# successful read. Best-effort: some environments don't support this.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# The dashboard page this notice points at, named exactly as it's registered in the
# navigation (dashboard/desk/desk_app.py) so the hint is unambiguous.
DASHBOARD_PAGE_NAME = "Raise withdrawal cash — reserve-short accounts only"

_DEDUP_KEY = "withdrawal_cash_raise_monthly"


def _log(msg: str) -> None:
    import datetime as dt
    try:
        print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Household names — best-effort, via crm_roster.fetch_household_names (the client's real
# household name, joined accounts -> households; NOT master_name, which is the IBKR
# advisor/master entity name and identical for every account under one advisor). A CRM miss
# leaves the name blank and never blocks the notice: the shortfall numbers came straight off
# the live broker read, not the CRM.
# --------------------------------------------------------------------------- #
def _household_names(accounts: list[str]) -> dict[str, str]:
    if not accounts:
        return {}
    try:
        import crm_roster
        names = crm_roster.fetch_household_names(accounts)
        return {a: names.get(a, "") for a in accounts}
    except Exception:
        return {a: "" for a in accounts}


# --------------------------------------------------------------------------- #
# Notice text (plain English, per the dashboard-labels standard) — ONE notice for the whole
# flagged list, not one per account.
# --------------------------------------------------------------------------- #
def build_notice(rows: list[dict]) -> tuple[str, str, str]:
    """(title, body, action_hint) for the WHOLE flagged list. Pure function — no broker, no
    Action Center call — so it's directly unit-testable."""
    n = len(rows)
    names = _household_names([r["account"] for r in rows])
    total_shortfall = sum(r["shortfall"] for r in rows)

    plural = "account needs" if n == 1 else "accounts need"
    possessive = "its" if n == 1 else "their"
    title = f"{n} {plural} withdrawal cash raised this cycle"

    lines = []
    for r in rows:
        household = names.get(r["account"], "")
        who = f"{r['account']} ({household})" if household else r["account"]
        lines.append(f"- {who}: short about ${r['shortfall']:,.0f} "
                     f"(holding ${r['total_cash']:,.0f} of the ${r['reserve']:,.0f} needed)")
    body = (
        f"{n} {plural} {possessive} withdrawal cash reserve raised this monthly cycle, "
        f"${total_shortfall:,.0f} combined shortfall:\n" + "\n".join(lines)
    )
    hint = (
        f'Open the "{DASHBOARD_PAGE_NAME}" page on the desk dashboard to review and, if you '
        f"want to proceed, prepare and send the trade there. That page reads this same live "
        f"report and trades EXACTLY these accounts, never a whole model's book. Nothing here "
        f"trades automatically."
    )
    return title, body, hint


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="read live and print the consolidated notice, but post NOTHING to "
                         "the Action Center.")
    args = ap.parse_args(argv)

    import withdrawal_cash_raise as wcr

    rows = wcr.accounts_needing_cash()
    if not rows:
        _log("No accounts need withdrawal cash raised this cycle.")
        print("No accounts need withdrawal cash raised this cycle.")
        return 0

    title, body, hint = build_notice(rows)
    _log(f"{len(rows)} account(s) flagged for this monthly cycle: "
         f"{', '.join(r['account'] for r in rows)}")

    if args.dry_run:
        print("[dry-run] WOULD post ONE consolidated Action Center notice (posting nothing):")
        print(f"  title: {title}")
        print(f"  body:  {body}")
        print(f"  hint:  {hint}")
        return 0

    import action_center
    if action_center.is_snoozed(_DEDUP_KEY):
        print("Monthly withdrawal-cash-raise notice is snoozed (ignored) by the operator; "
              "posting nothing.")
        return 0

    key = action_center.post_notice(kind="withdrawal_cash_raise_monthly", title=title,
                                    body=body, severity="warn", action_hint=hint,
                                    dedup_key=_DEDUP_KEY)
    if key:
        print(f"Posted the consolidated withdrawal-cash-raise notice to the Action Center "
              f"(notice {key}), naming {len(rows)} account(s).")
        return 0
    _log("posting the Action Center notice failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
