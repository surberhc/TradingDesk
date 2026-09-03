"""page_group_trade.py — GROUP TRADE. Pick strategies, prepare, check, send.

DELIBERATELY SMALL. The Control Plane is 2,359 lines of nested expanders, tables and prose,
and the owner verdict on it (2026-09-03) was that it is unreadable: list after list and box
after box. This page is a reset, not an extension of it. Four steps, one screen, no nesting:

    1. PICK      tick the strategies to trade
    2. PREPARE   one button; read-only; shows the orders it would send
    3. CHECK     a short list of pass/fail lines in plain English
    4. SEND      arm, type the phrase, one button

Everything heavy lives in paperbot (group_execute / group_rebalance). This file decides
nothing about trading - it renders what those return and gates the send. If a read fails it
shows one plain sentence instead of a traceback.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

_PAPERBOT = str(Path(__file__).resolve().parents[2] / "paperbot")
if _PAPERBOT not in sys.path:
    sys.path.insert(0, _PAPERBOT)

CONFIRM_PHRASE = "SEND GROUP TRADE"
_STATE = "group_trade_prepared"


def _strategies() -> list:
    """[{model, accounts, value}] for every model on the blessed roster. CRM read, no broker."""
    import batch_rebalance_execute as bre
    import crm_roster
    import roster as roster_mod

    scan = roster_mod.enrolled_roster_scan()
    accounts = scan.get("accounts") or []
    if not accounts:
        return []
    versions = bre.resolve_roster_versions(accounts)
    navs = {}
    try:
        for r in crm_roster.fetch_roster(advisor_name=crm_roster.DEFAULT_ADVISOR):
            navs[crm_roster.account_identifier(r)] = float(r.get("total_value") or 0.0)
    except Exception:
        navs = {}
    out = {}
    for acct in accounts:
        m = versions.get(acct, "(unmapped)")
        row = out.setdefault(m, {"model": m, "accounts": 0, "value": 0.0})
        row["accounts"] += 1
        row["value"] += navs.get(acct, 0.0)
    return sorted(out.values(), key=lambda r: -r["accounts"])


def _prepare(models: list) -> dict:
    """Read-only: scope -> plans -> ticker groups -> routes. Creates nothing, sends nothing."""
    from ib_async import IB
    import group_execute as ge

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    ib = IB()
    ib.connect("127.0.0.1", 4003, clientId=115, readonly=True, timeout=30)
    try:
        built = ge.build_plans_for_scope(ib, models=models)
        run = ge.plan_group_run(built["plans"], run_stamp=stamp, prices=built["prices"])
        run["roster"] = built["roster"]
        run["skipped"] = built["skipped"]
        run["outside"] = ge.accounts_outside_the_wall(run["group_plans"], built["roster"])
        run["stamp"] = stamp
        run["models"] = list(models)
        return run
    finally:
        ib.disconnect()


def _checks(run: dict) -> list:
    """Plain-English pass/fail. Every line is a full sentence; the marker only sorts them."""
    groups = run.get("group_plans") or []
    checks = []
    checks.append((bool(groups),
                   "{} block order(s) to send.".format(len(groups)) if groups
                   else "Nothing in these strategies needs to trade."))
    outside = run.get("outside") or []
    checks.append((not outside,
                   "Every account is on the approved roster." if not outside
                   else "{} account(s) are NOT on the approved roster: {}. This run cannot "
                        "be sent.".format(len(outside), ", ".join(outside))))
    skipped = run.get("skipped") or []
    checks.append((not skipped,
                   "Every account in scope has a readable account value." if not skipped
                   else "{} account(s) have no readable value and were left out: {}.".format(
                        len(skipped), ", ".join(skipped))))
    unpriced = sorted({g.symbol for g in groups if g.est_notional is None})
    checks.append((not unpriced,
                   "Every holding has a live price." if not unpriced
                   else "No live price for {} - those will not trade.".format(
                        ", ".join(unpriced))))
    biggest = max((g.total_qty for g in groups), default=0)
    checks.append((biggest <= 100000,
                   "Largest single order is {:,} shares.".format(biggest)
                   if biggest <= 100000
                   else "Largest single order is {:,} shares, which is very large. Check the "
                        "size limit in the gateway presets first.".format(biggest)))
    return checks


def render_group_trade() -> None:
    st.markdown("## Group trade")
    st.caption("One order per holding, shared across every account that needs it, so "
               "everyone gets the same price.")

    st.markdown("#### 1. Pick the strategies")
    try:
        strategies = _strategies()
    except Exception as exc:
        st.error("Could not read the strategy list from the client records: {}".format(exc))
        return
    if not strategies:
        st.warning("No accounts are on the approved roster, so there is nothing to trade.")
        return

    picked = []
    cols = st.columns(2)
    for i, s in enumerate(strategies):
        label = "**{}** - {} account(s) - ${:,.0f}".format(
            s["model"], s["accounts"], s["value"])
        if cols[i % 2].checkbox(label, key="gt_{}".format(s["model"])):
            picked.append(s["model"])

    if not picked:
        st.info("Tick one or more strategies above. Start with the smallest.")
        return

    n_acct = sum(s["accounts"] for s in strategies if s["model"] in picked)
    n_val = sum(s["value"] for s in strategies if s["model"] in picked)
    st.markdown("Selected: **{} strategy(ies), {} account(s), ${:,.0f}**".format(
        len(picked), n_acct, n_val))

    st.markdown("#### 2. Prepare the trade")
    if st.button("Prepare", type="primary"):
        with st.spinner("Reading positions and prices, and working out the orders..."):
            try:
                st.session_state[_STATE] = _prepare(picked)
            except Exception as exc:
                st.session_state.pop(_STATE, None)
                st.error("Could not prepare the trade: {}".format(exc))

    run = st.session_state.get(_STATE)
    if not run:
        st.caption("Nothing prepared yet. Nothing has been sent and no group has been made.")
        return
    if run.get("models") != picked:
        st.warning("You changed the strategies after preparing. Press Prepare again.")
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
    checks = _checks(run)
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
                          key="gt_confirm", placeholder=CONFIRM_PHRASE)
    ready = typed.strip().upper() == CONFIRM_PHRASE
    if st.button("Send group trade", type="primary", disabled=not ready):
        st.warning("Order placement is not wired yet - nothing was sent. The groups, the "
                   "share splits and every check above are real; the last step is the "
                   "block executor.")
