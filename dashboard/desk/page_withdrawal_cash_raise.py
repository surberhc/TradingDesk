"""page_withdrawal_cash_raise.py — RAISE WITHDRAWAL CASH. Report-driven, one button.

DELIBERATELY NARROW (Andrew's explicit requirement, 2026-09-05): the account list on this
page is NEVER hand-picked. It is exactly today's live withdrawal-reserve shortfall report
(dailyreport/withdrawal_reserve_check.py's own decide(), via
paperbot/withdrawal_cash_raise.accounts_needing_cash()) — read fresh every time the page
loads. There is no control here to add or remove an account: the whole point of this trigger
is that it is driven by the report, never by hand, and it trades EXACTLY those accounts, never
a whole model's book just because one of its accounts came up short.

REUSE, NOT A PARALLEL PATH. Everything after "which accounts" is the SAME machinery
page_group_trade.py already drives for a normal model-scoped run:

    paperbot/withdrawal_cash_raise.prepare_run()
        -> group_execute.build_plans_for_accounts   (the account-list-driven core
                                                       build_plans_for_scope also uses,
                                                       once IT has resolved a model scope
                                                       down to an account list)
        -> group_execute.plan_group_run             (-> group_rebalance.plan_ticker_groups,
                                                       confirmed account-list-driven, not
                                                       model-scoped)

This page also borrows page_group_trade.py's own ``_checks()`` and ``_render_result()``
UNCHANGED, so the pass/fail wording a reviewer sees and the outcome report after sending are
identical to the page Andrew already knows — only "which accounts" and the two preview/send
connections (own clientIds, so this page can never collide with Group Trade if both happen to
be open at once) are new. Sending still goes through group_execute.execute_group_run's own
preview -> arm -> transmit gate, with allowed_accounts set to EXACTLY the flagged accounts.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

_PAPERBOT = str(Path(__file__).resolve().parents[2] / "paperbot")
_DAILYREPORT = str(Path(__file__).resolve().parents[2] / "dailyreport")
for _p in (_PAPERBOT, _DAILYREPORT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import page_group_trade as _gt  # reuse _checks()/_render_result() verbatim — no parallel UI

CONFIRM_PHRASE = "SEND GROUP TRADE"
_STATE = "wcr_prepared"


def _accounts_needing_cash() -> list:
    """Live read of today's shortfall list. Thin call-through so the page has one place to
    mock in a test; the actual decision logic lives in withdrawal_reserve_check, reused via
    paperbot/withdrawal_cash_raise (see that module's docstring)."""
    import withdrawal_cash_raise as wcr
    return wcr.accounts_needing_cash()


def _household_names(accounts: list) -> dict:
    """account -> CRM master_name, best-effort. A CRM miss leaves the column blank and never
    blocks the shortfall table — the shortfall numbers came straight off the live broker read,
    not the CRM. advisor_name=None reads the whole book so an account under any advisor is
    still named, matching the fact that accounts_needing_cash() is not advisor-scoped either."""
    if not accounts:
        return {}
    try:
        import crm_roster
        names = {}
        for r in crm_roster.fetch_roster(advisor_name=None):
            names[crm_roster.account_identifier(r)] = r.get("master_name") or ""
        return {a: names.get(a, "") for a in accounts}
    except Exception:
        return {a: "" for a in accounts}


def _prepare(accounts: list) -> dict:
    """Read-only: the flagged accounts -> sized plans -> ticker groups -> routes.

    Own clientId (119) — distinct from page_group_trade.py's preview connection (115) — so
    the two pages can never collide at the gateway if both happen to be open at once.
    """
    from ib_async import IB
    import withdrawal_cash_raise as wcr

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    ib = IB()
    ib.connect("127.0.0.1", 4003, clientId=119, readonly=True, timeout=30)
    try:
        return wcr.prepare_run(accounts, ib, run_stamp=stamp)
    finally:
        ib.disconnect()


def _send(run: dict) -> dict:
    """Connect on the transmit lane, create the groups, place the blocks — the same gate as
    page_group_trade.py's own ``_send()``: the gateway's Read-Only API toggle is the physical
    wall; connecting with readonly=False only succeeds if a human has turned it off, and the
    executor probes it again before writing anything. ``allowed_accounts`` is EXACTLY the
    accounts this run was scoped to, so ``accounts_outside_the_wall`` would catch it as a bug
    if the plan ever tried to touch anything else.

    Own clientId (120) — distinct from page_group_trade.py's send connection (116).
    """
    from ib_async import IB
    import group_execute as ge

    built = run["_built"]
    target = ge.live_gateway(built["versions"])
    ib = IB()
    ib.connect(target.host, target.port, clientId=120, readonly=False, timeout=30,
              account=target.pin_account)
    try:
        return ge.execute_group_run(
            ib, target, run, built,
            allowed_accounts=run["accounts_scope"], armed=True, backup_path=None)
    finally:
        ib.disconnect()


def render_withdrawal_cash_raise() -> None:
    st.markdown("## Raise withdrawal cash")
    st.caption("Trades ONLY the accounts currently short of their withdrawal reserve — "
               "never a whole model's book. The list below is today's live report; there is "
               "no way to add or remove an account here by hand.")

    top_left, mid, right = st.columns([2, 1, 1])
    with mid:
        if st.button("Reset this page", key="wcr_reset", use_container_width=True):
            st.session_state.pop(_STATE, None)
            st.rerun()

    st.markdown("#### 1. Today's shortfall report")
    try:
        rows = _accounts_needing_cash()
    except Exception as exc:
        st.error("Could not read the withdrawal-reserve report: {}".format(exc))
        return

    if not rows:
        st.success("No account is short of its withdrawal reserve right now. Nothing to do.")
        return

    accounts = [r["account"] for r in rows]
    names = _household_names(accounts)
    st.dataframe(pd.DataFrame([{
        "Account": r["account"],
        "Household": names.get(r["account"], ""),
        "Cash on hand": round(r["total_cash"]),
        "Reserve needed": round(r["reserve"]),
        "Shortfall": round(r["shortfall"]),
    } for r in rows]), hide_index=True, use_container_width=True)
    st.markdown("**{} account(s) short, ${:,.0f} combined shortfall.**".format(
        len(rows), sum(r["shortfall"] for r in rows)))

    st.markdown("#### 2. Prepare the trade")
    if st.button("Prepare", type="primary", key="wcr_prepare"):
        with st.spinner("Reading positions and prices, and working out the orders..."):
            try:
                st.session_state[_STATE] = _prepare(accounts)
            except Exception as exc:
                st.session_state.pop(_STATE, None)
                st.error("Could not prepare the trade: {}".format(exc))

    run = st.session_state.get(_STATE)
    if not run:
        st.caption("Nothing prepared yet. Nothing has been sent and no group has been made.")
        return
    if run.get("accounts_scope") != accounts:
        st.warning("The shortfall report has changed since you prepared this trade (an "
                   "account's read changed, or the schedule did). Press Prepare again.")
        return

    groups = run.get("group_plans") or []
    if groups:
        st.dataframe(pd.DataFrame([{
            "Buy / sell": g.side,
            "Holding": g.symbol,
            "Shares": g.total_qty,
            "Approx value": None if g.est_notional is None else round(g.est_notional),
            "Accounts": g.n_accounts,
        } for g in groups]), hide_index=True, use_container_width=True)

    st.markdown("#### 3. Checks")
    checks = _gt._checks(run)
    for ok, text in checks:
        st.markdown("{} {}".format("PASS -" if ok else "STOP -", text))
    blocking = [t for ok, t in checks if not ok]

    st.markdown("#### 4. Send")
    if blocking:
        st.error("This trade cannot be sent while a check above is failing.")
        return
    st.caption("Uncheck Read-Only API on the port-4003 gateway, then type {} below.".format(
        CONFIRM_PHRASE))
    typed = st.text_input("Type {} to confirm".format(CONFIRM_PHRASE), value="",
                          key="wcr_confirm", placeholder=CONFIRM_PHRASE)
    ready = typed.strip().upper() == CONFIRM_PHRASE
    if st.button("Send group trade", type="primary", disabled=not ready, key="wcr_send"):
        with st.spinner("Creating the groups and sending the orders..."):
            try:
                result = _send(run)
            except Exception as exc:
                st.error("The run was refused and NOTHING was sent: {}".format(exc))
                return
        _gt._render_result(result)
