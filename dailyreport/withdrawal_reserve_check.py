"""
withdrawal_reserve_check.py — the real-client withdrawal-reserve SHORTFALL check.

WHAT IT IS
----------
Every account in paperbot/cashflows.SCHEDULE with a "distribution" Flow has a scheduled
recurring cash withdrawal, and cashflows.reserve_for() defines how much cash it must hold
liquid (RESERVE_MONTHS of the upcoming distribution) so a withdrawal is never funded by a
fire sale. Accounts that are custom allocations managed BY HAND (no_trade in the CRM roster)
are deliberately excluded from the rebalancer's automatic reserve-carving — but they still
draft the same scheduled ACH withdrawal every month, so they still need this protection.
This job checks them directly against cashflows.SCHEDULE (never the CRM roster's no_trade
filter), and if an account's current cash does not cover its reserve, posts a plain-English
"withdrawal reserve short" notice to the in-app Action Center (dashboard/desk/action_center.py)
so a human (Ted) can raise cash by hand before the next scheduled withdrawal date.

It NEVER trades: read-only account reads only, no order path anywhere in this file.

Cadence: run on a schedule (e.g. daily). Below-shortfall accounts post nothing. It
de-duplicates PER ACCOUNT (dedup_key=f"withdrawal_reserve_{account}"), so repeated runs
while an account stays short keep ONE current notice per account, not a growing pile.

SCOPE / SAFETY — INFORMATIONAL + READ-ONLY, ZERO-TRANSMIT
----------------------------------------------------------
Connects to the transmit-CAPABLE live-trading Gateway (port 4003) but is read-only by
construction: ibkr_live_trade.connect(readonly=True), and there is NO order path in this
file (it only reads accountSummary()). Not order-affecting: no paperbot version bump.

USAGE
-----
    <venv python> withdrawal_reserve_check.py            # real run (task will use this)
    <venv python> withdrawal_reserve_check.py --dry-run  # read live, decide + print, post NOTHING
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

# cashflows.describe() embeds real Unicode punctuation (a minus sign, U+2212, for
# distributions) that build_notice() below folds into console output. Windows' default
# console codepage (cp1252) can't encode it, so make stdout/stderr UTF-8-safe up front
# rather than crash mid-run on an otherwise-successful read. Best-effort: some
# environments (e.g. captured/piped streams without a .reconfigure) don't support this.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import cashflows  # paperbot/cashflows.py — the SCHEDULE + reserve_for()  # noqa: E402


def _log(msg: str) -> None:
    import datetime as dt
    try:
        print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Which real accounts to check — derived LIVE from cashflows.SCHEDULE, never hardcoded.
# --------------------------------------------------------------------------- #
def accounts_to_check() -> list[str]:
    """Every account key in cashflows.SCHEDULE with at least one 'distribution' Flow.

    An account with ONLY 'contribution' flows is skipped -- a deposit needs no reserve
    (see cashflows.reserve_for). Sorted for stable, readable output."""
    return sorted(
        account for account, flows in cashflows.SCHEDULE.items()
        if any(f.kind == "distribution" for f in flows)
    )


# --------------------------------------------------------------------------- #
# Pure decision — no broker, unit-testable.
# --------------------------------------------------------------------------- #
def decide(account: str, net_liq: float | None, total_cash: float | None) -> dict:
    """Decide whether this account's cash is short of its withdrawal reserve. Pure function.

    Returns {ok, account, net_liq, total_cash, reserve, shortfall, should_alert, reason}.
    ok=False (never alerts) when NAV is missing/non-positive or cash is unreadable. An
    account with NO SCHEDULE entry at all has reserve=0.0 and therefore never alerts (0.0
    cash can't be short of a 0.0 reserve). Trigger is STRICTLY shortfall > 0."""
    out = {"ok": False, "account": account, "net_liq": net_liq, "total_cash": total_cash,
           "reserve": None, "shortfall": None, "should_alert": False, "reason": ""}
    if not net_liq or net_liq <= 0:
        out["reason"] = "no positive NetLiquidation could be read"
        return out
    if total_cash is None:
        out["reason"] = "no cash balance could be read"
        return out
    reserve = cashflows.reserve_for(account, net_liq)
    shortfall = reserve - total_cash
    out.update({"ok": True, "reserve": reserve, "shortfall": shortfall})
    if shortfall > 1e-9:
        out["should_alert"] = True
        out["reason"] = "cash on hand does not cover the withdrawal reserve"
    else:
        out["reason"] = "cash on hand covers the withdrawal reserve"
    return out


# --------------------------------------------------------------------------- #
# Read-only account read (near-verbatim of s0_cash_deploy_check.read_cash, generalized
# to take any account).
# --------------------------------------------------------------------------- #
def _filter_account(rows, account):
    return [r for r in rows if getattr(r, "account", None) == account]


def _tag(summary_rows, tag: str):
    for r in summary_rows:
        if getattr(r, "tag", None) == tag:
            try:
                return float(r.value)
            except (TypeError, ValueError):
                return None
    return None


def read_cash(account: str) -> dict:
    """Connect READ-ONLY to the live-trading Gateway (port 4003), read this account's
    NetLiquidation + TotalCashValue. Transmits NOTHING. Raises on any connect/read problem
    (including the account not being visible under this login) so the caller can log
    honestly and post nothing."""
    from connections import ibkr_live_trade
    ib = ibkr_live_trade.connect("withdrawal_reserve_check", launch=True, readonly=True)
    try:
        summary = _filter_account(ib.accountSummary(), account)
        if not summary:
            seen = sorted(str(a) for a in
                          {getattr(r, "account", None) for r in ib.accountSummary()}
                          if a is not None)
            raise RuntimeError(
                f"account {account} not found under the live-trading login "
                f"(accounts seen: {seen})")
        net_liq = _tag(summary, "NetLiquidation")
        total_cash = _tag(summary, "TotalCashValue")
        return {"net_liq": net_liq, "total_cash": total_cash}
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Notice text (plain English, per the dashboard-labels standard).
# --------------------------------------------------------------------------- #
def build_notice(d: dict) -> tuple[str, str, str]:
    """(title, body, action_hint) for a should_alert decision."""
    account = d["account"]
    cash = d["total_cash"]
    reserve = d["reserve"]
    shortfall = d["shortfall"]
    title = f"Withdrawal reserve short — {account} needs ~${shortfall:,.0f} more"
    body = (
        f"Account {account} is holding ${cash:,.0f} in cash, but needs to hold "
        f"${reserve:,.0f} liquid to cover its next {cashflows.RESERVE_MONTHS} months of "
        f"scheduled withdrawals ({cashflows.describe(account, d['net_liq'])}). That's a "
        f"shortfall of about ${shortfall:,.0f}."
    )
    hint = (
        "This account is marked no-trade / managed manually (a custom allocation) — raise "
        "cash by hand before the next scheduled withdrawal date. Nothing here trades "
        "automatically."
    )
    return title, body, hint


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="read every account live and print each decision, but post "
                         "NOTHING to the Action Center.")
    args = ap.parse_args(argv)

    accounts = accounts_to_check()
    if not accounts:
        _log("no accounts in cashflows.SCHEDULE have a distribution flow; nothing to check.")
        return 0

    action_center = None
    if not args.dry_run:
        import action_center as _ac
        action_center = _ac

    any_failure = False
    for account in accounts:
        try:
            data = read_cash(account)
        except Exception as e:
            any_failure = True
            _log(f"account={account}: could not read the account ({type(e).__name__}: {e}); "
                 f"posting nothing for it. The live-trading Gateway (port 4003) may be down "
                 f"or not logged in.")
            continue

        d = decide(account, data.get("net_liq"), data.get("total_cash"))
        if not d["ok"]:
            any_failure = True
            _log(f"account={account}: cash check inconclusive: {d['reason']}; "
                 f"posting nothing for it.")
            continue

        _log(f"account={account} NetLiq={d['net_liq']:,.2f} cash={d['total_cash']:,.2f} "
             f"reserve={d['reserve']:,.2f} shortfall={d['shortfall']:,.2f} "
             f"-> should_alert={d['should_alert']}")

        if not d["should_alert"]:
            print(f"{account}: OK — cash (${d['total_cash']:,.0f}) covers the "
                  f"${d['reserve']:,.0f} withdrawal reserve.")
            continue

        title, body, hint = build_notice(d)
        dedup_key = f"withdrawal_reserve_{account}"
        if args.dry_run:
            print(f"[dry-run] {account}: WOULD post an Action Center notice (posting nothing):")
            print(f"  title: {title}")
            print(f"  body:  {body}")
            print(f"  hint:  {hint}")
            continue

        if action_center.is_snoozed(dedup_key):
            print(f"{account}: withdrawal-reserve notice is snoozed (ignored) by the "
                  f"operator; posting nothing.")
            continue
        key = action_center.post_notice(kind="withdrawal_reserve", title=title, body=body,
                                        severity="warn", action_hint=hint, dedup_key=dedup_key)
        if key:
            print(f"{account}: posted withdrawal-reserve shortfall notice to the Action "
                  f"Center (notice {key}).")
        else:
            any_failure = True
            _log(f"account={account}: posting the Action Center notice failed.")

    return 1 if any_failure else 0


if __name__ == "__main__":
    sys.exit(main())
