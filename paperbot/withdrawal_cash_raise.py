"""
withdrawal_cash_raise.py — trade EXACTLY the accounts short of their withdrawal reserve.

WHAT IT IS
----------
A narrowly-scoped trigger (Andrew's explicit requirement, 2026-09-05): when one or more
accounts are short of the cash they must hold liquid for a scheduled withdrawal
(dailyreport/withdrawal_reserve_check.py's own logic — reused here, never duplicated), raise
cash in EXACTLY those accounts, in ONE run that may span however many models the flagged
accounts happen to sit in. It never re-trades a whole model's book just because one of its
accounts came up short, and it never expands the account list by hand — the flagged accounts
ARE the scope, end to end.

It is deliberately NOT a new trading rail. Every step below is the SAME primitive
page_group_trade.py / group_execute.py already uses for an ordinary model-scoped run:

    accounts_needing_cash()              -- WHICH accounts (dailyreport's own decide())
    group_execute.build_plans_for_accounts(ib, accounts)
                                          -- SIZE those accounts (rebalance_engine.plan_account,
                                             via the account-list-driven core group_execute
                                             already factors out of build_plans_for_scope)
    group_execute.plan_group_run(plans, run_stamp=...)
                                          -- PIVOT into per-ticker block plans
                                             (group_rebalance.plan_ticker_groups, confirmed
                                             account-list-driven, not model-scoped)
    group_execute.execute_group_run(...) -- the SAME preview -> arm -> transmit pipeline,
                                             with allowed_accounts SET TO EXACTLY the flagged
                                             list, so accounts_outside_the_wall would catch it
                                             as a bug if the plan ever touched anything else.

The only genuinely NEW code in this module is the account list itself (item 1 below) and the
thin call that hands that list to the existing per-account planner (item 2). Nothing here
builds an order, opens a broker write connection, or creates an FA group.

SCOPE / SAFETY
--------------
``accounts_needing_cash`` does a LIVE READ (via withdrawal_reserve_check.read_cash, the same
read-only port-4003 connection already proven there — no new gateway login, no new clientId).
``prepare_run`` also reads live (positions/prices, via group_execute.build_plans_for_accounts)
but builds no order and transmits nothing; it is a preview exactly like
page_group_trade.py's own ``_prepare()``. Nothing in this module can arm the gateway or place
an order — that stays inside group_execute.execute_group_run's existing arm gate, unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _p in (str(_HERE), str(_REPO / "dailyreport"), str(_REPO / "connections")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# --------------------------------------------------------------------------- #
# 1. WHICH accounts — reuses withdrawal_reserve_check's own accounts_to_check() +
#    decide() + read_cash() directly. This is NOT a second implementation of the
#    shortfall decision; it is the same one, called live.
# --------------------------------------------------------------------------- #
def accounts_needing_cash(as_of=None) -> list[dict]:
    """LIVE read: every account withdrawal_reserve_check tracks that is CURRENTLY short of
    its withdrawal reserve.

    ``as_of`` is accepted for interface symmetry with other desk report functions (the
    dashboard/tests may want to name "today") but is unused — the underlying check is always
    a live NOW read of NetLiq/cash, never a backdated one.

    Returns ``[{account, net_liq, total_cash, reserve, shortfall}]``, sorted by account, for
    exactly the accounts where withdrawal_reserve_check.decide()'s ``should_alert`` is True.
    An account that cannot be read live (gateway down, account not visible under this login)
    is SKIPPED, never guessed at, with a note to stderr — mirroring
    withdrawal_reserve_check.main's own failure handling exactly.
    """
    import withdrawal_reserve_check as wrc

    out: list[dict] = []
    for account in wrc.accounts_to_check():
        try:
            data = wrc.read_cash(account)
        except Exception as exc:  # noqa: BLE001
            print(f"withdrawal_cash_raise: {account}: could not read the account live "
                  f"({type(exc).__name__}: {exc}); skipped, not counted as short.",
                  file=sys.stderr)
            continue
        d = wrc.decide(account, data.get("net_liq"), data.get("total_cash"))
        if not d.get("ok"):
            print(f"withdrawal_cash_raise: {account}: cash check inconclusive "
                  f"({d.get('reason', '')}); skipped, not counted as short.", file=sys.stderr)
            continue
        if d.get("should_alert"):
            out.append({
                "account": account,
                "net_liq": d["net_liq"],
                "total_cash": d["total_cash"],
                "reserve": d["reserve"],
                "shortfall": d["shortfall"],
            })
    return sorted(out, key=lambda r: r["account"])


# --------------------------------------------------------------------------- #
# 2. Size ONLY those accounts. Thin delegation to group_execute's account-list-driven
#    core — see group_execute.build_plans_for_accounts for the actual steps (resolve each
#    account's CRM model label, build that model's Target, price + position-read, then
#    rebalance_engine.plan_account per account). Nothing here re-implements any of that.
# --------------------------------------------------------------------------- #
def build_restricted_plans(accounts: list[str], ib, *, band_pct=None) -> dict:
    """Build sized AccountPlans for EXACTLY ``accounts`` — never a whole model's roster.

    ``accounts`` is normally the account list from :func:`accounts_needing_cash` (just the
    account numbers, e.g. ``[r["account"] for r in accounts_needing_cash()]``). Each
    account's CRM-assigned model is resolved purely to pick which model's TARGET WEIGHTS
    apply for sizing that one account — no other account in that model is ever pulled in.

    Delegates entirely to ``group_execute.build_plans_for_accounts`` (the same account-list
    core ``build_plans_for_scope`` uses once IT has resolved a model scope down to an account
    list) so this path can never size an account differently than the ordinary group-trade
    page would. Returns the same shape: ``{"plans", "prices", "versions", "targets", "metas",
    "skipped", "account_inputs", "summaries"}``.
    """
    import group_execute as ge
    return ge.build_plans_for_accounts(ib, list(accounts or []), band_pct=band_pct)


# --------------------------------------------------------------------------- #
# 3 + 4. Pivot into per-ticker block plans and package the run for the SAME
#    preview -> arm -> transmit flow group_execute/page_group_trade.py already drive.
# --------------------------------------------------------------------------- #
def prepare_run(accounts: list[str], ib, *, run_stamp: str, band_pct=None) -> dict:
    """Read-only: restricted accounts -> sized plans -> ticker groups -> routes.

    Mirrors page_group_trade.py's own ``_prepare()`` exactly, with one difference: the scope
    is an EXPLICIT account list (the accounts a shortfall check flagged), never a model
    selection. ``run["outside"]`` is computed against that SAME exact list — the wall this
    run must never cross — so a bug that widened the plan to a sibling account in the same
    model would be caught here as a non-empty ``outside``, exactly as it would on the
    model-scoped page.

    Creates nothing, sends nothing. ``run["_built"]`` carries the full build (account_inputs,
    summaries, targets, versions) the executor needs, unchanged from build_restricted_plans.
    """
    import group_execute as ge

    accounts = [str(a).strip() for a in (accounts or []) if str(a).strip()]
    built = build_restricted_plans(accounts, ib, band_pct=band_pct)
    run = ge.plan_group_run(built["plans"], run_stamp=run_stamp, prices=built["prices"])
    run["outside"] = ge.accounts_outside_the_wall(run["group_plans"], accounts)
    run["stamp"] = run_stamp
    run["accounts_scope"] = accounts
    run["skipped"] = built.get("skipped") or []
    run["_built"] = built
    return run
