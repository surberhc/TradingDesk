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
        run["_built"] = built
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
    # THE GATEWAY'S OWN ORDER LIMITS. An oversized block is rejected at the gateway, which
    # mid-run means a failed sell, a halted buy phase and a wasted window. Caught here, before
    # anything is sent. Found on the 2026-09-04 Growth (Custom) plan: SELL BUCK 347,419 shares
    # / $8,098,337 against limits of 100,000 and $5,000,000.
    import group_rebalance as _gr
    oversize = _gr.blocks_over_gateway_limits(groups)
    if oversize:
        detail = "; ".join(
            "{} {} = {:,.0f} shares / ${:,.0f}{}{}".format(
                r["side"], r["symbol"], r["shares"], r["value"],
                " (over the {:,} share limit)".format(r["size_limit"]) if r["over_size"] else "",
                " (over the ${:,.0f} value limit)".format(r["value_limit"]) if r["over_value"] else "")
            for r in oversize)
        checks.append((False,
                       "{} block(s) exceed the gateway's order limits and would be REJECTED: "
                       "{}. This run cannot be sent as planned.".format(len(oversize), detail)))
    else:
        checks.append((True, "Every block is inside the gateway's order size and value limits."))

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

    # Housekeeping and Reset live ABOVE step 1 on purpose: both are needed most when a trade
    # is NOT prepared -- after a run, or when the page is holding stale state. They used to
    # sit below the "nothing prepared yet" early return, which made them invisible exactly
    # when they were wanted.
    top_left, mid, right = st.columns([2, 1, 1])
    with mid:
        if st.button("Reset this page", key="reset_group_trade",
                     use_container_width=True):
            # A RESET CLEARS EVERYTHING, not just the prepared trade. The strategy tickboxes
            # are Streamlit widget state under "gt_<model>" keys and survive a rerun on their
            # own, so dropping only _STATE left the boxes ticked and the page half-reset.
            st.session_state.pop(_STATE, None)
            for k in [k for k in st.session_state if str(k).startswith("gt_")]:
                st.session_state.pop(k, None)
            st.rerun()
    with right:
        _purge_button()

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
        with st.spinner("Creating the groups and sending the orders..."):
            try:
                result = _send(run)
            except Exception as exc:
                st.error("The run was refused and NOTHING was sent: {}".format(exc))
                return
        _render_result(result)


def _send(run: dict) -> dict:
    """Connect on the transmit lane, create the groups, place the blocks.

    The gateway's own Read-Only toggle is the physical wall: connecting with readonly=False
    only succeeds if a human has turned it off, and the executor probes it again before
    writing anything. Nothing here can arm the gateway.
    """
    from ib_async import IB
    import group_execute as ge

    built = run["_built"]
    target = ge.live_gateway(built["versions"])
    ib = IB()
    ib.connect(target.host, target.port, clientId=116, readonly=False, timeout=30,
               account=target.pin_account)
    try:
        return ge.execute_group_run(
            ib, target, run, built,
            allowed_accounts=built["roster"], armed=True, backup_path=None)
    finally:
        ib.disconnect()


def _purge_button() -> None:
    """Clear spent throwaway groups from the master. TOP RIGHT, always visible, no expander
    and no prepared trade required: this is needed most when a trade CANNOT be prepared,
    which is exactly the situation it fixes. Every run leaves one group per block behind at
    IBKR; four runs took that document from 8 groups to 198 on 2026-09-04 and the gateway
    stopped committing writes (error 10230), which blocked trading outright."""
    from ib_async import IB
    import group_execute as ge

    if not st.button("Purge old groups", key="purge_groups", use_container_width=True,
                     help="Deletes spent run groups at IBKR. Permanent groups are never "
                          "touched. Backs up first and verifies before writing."):
        return
    ib = IB()
    try:
        ib.connect("127.0.0.1", 4003, clientId=118, readonly=False, timeout=30,
                   account=ge.LIVE_MASTER_ACCOUNT)
    except Exception as exc:
        st.error("Could not reach the gateway: {}".format(exc))
        return
    try:
        res = ge.purge_run_groups(ib, armed=True)
    except Exception as exc:
        st.error("Purge refused, nothing was changed: {}".format(exc))
        return
    finally:
        ib.disconnect()
    if res.get("refused"):
        st.warning(res["refused"])
    elif res.get("written"):
        # Counted from the LIVE document after settling, never from what we asked for -- an
        # earlier version reported deleting 82 groups while deleting none, because the read
        # straight after a write is served stale.
        msg = ("Deleted {} group(s) over {} pass(es). {} group(s) remain: {} permanent, "
               "{} still to clear.").format(res["deleted"], res["passes"], res["remaining"],
                                            res["permanent"], res["remaining_dated"])
        if res["remaining_dated"]:
            st.warning(msg + "  Press again to continue.")
        else:
            st.success(msg)
        for n in res.get("notes", []):
            st.caption(n)
        st.caption("Backup: {}".format(res["backup"]))
    else:
        st.info("Nothing was written.")


def _render_result(result: dict) -> None:
    """What actually happened, in plain sentences. Positions are the truth, not order status."""
    created = result.get("created") or {}
    st.markdown("**Groups created:** {}".format(created.get("created", 0)))
    if result.get("note"):
        st.warning(result["note"])
        return
    ex = result.get("executed") or {}
    if ex.get("refused"):
        st.error("The run was refused: {}".format(ex.get("refused_reason") or "no reason given"))
        return
    fills = ex.get("placed_fills") or []
    outcomes = ex.get("outcomes") or {}
    counts = outcomes.get("counts") or {}
    n_filled, n_partial = counts.get("FILLED", 0), counts.get("PARTIAL", 0)
    n_nofill, n_skipped = counts.get("NO_FILL", 0), counts.get("SKIPPED", 0)

    # "Sent" is not an outcome. A block that was placed, sat and cancelled having traded
    # nothing used to count toward a green success box -- that is how a run in which 12 of 25
    # blocks did nothing reported as a success. Green ONLY when every block filled in full.
    if outcomes.get("complete"):
        st.success("Every block filled in full - {} of {}.".format(n_filled, n_filled))
    else:
        st.error(
            "This run did NOT complete. {} block(s) filled, {} filled only partly, "
            "{} were placed and traded NOTHING, {} never went out.".format(
                n_filled, n_partial, n_nofill, n_skipped))

    if ex.get("halted"):
        st.error("STOPPED AFTER THE SELLS: {}".format(ex.get("halted_reason") or ""))

    shortfalls = outcomes.get("shortfalls") or []
    if shortfalls:
        st.markdown("**What did not trade, and why:**")
        st.dataframe(pd.DataFrame(shortfalls), hide_index=True, use_container_width=True)

    if fills:
        st.markdown("**What actually filled:**")
        st.dataframe(pd.DataFrame(fills), hide_index=True, use_container_width=True)
    for label, key in (("Dropped for pattern-day-trader limits", "pdt_dropped"),
                       ("Buy blocks dropped for cash", "dropped_buy_blocks"),
                       ("Proceeds left uninvested", "uninvested")):
        rows = ex.get(key) or []
        if rows:
            st.warning("{}: {}".format(label, len(rows)))
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    # THE ANSWER TO "IS ANYTHING LEFT BEHIND": the book re-read after the run, per account and
    # per line. Order counts describe what we asked for; this describes what the accounts are.
    sync = result.get("sync") or {}
    if not sync:
        return
    if not sync.get("ok"):
        st.warning("Could not verify the accounts afterwards: {}".format(sync.get("error", "")))
        return
    n_ok, n_off = sync.get("in_sync", 0), sync.get("out_of_sync", 0)
    if n_off == 0:
        st.success("All {} account(s) are on target. Nothing left behind.".format(n_ok))
        return
    st.error("{} account(s) on target, {} still OFF target.".format(n_ok, n_off))
    off_rows = [ln for acct in sync.get("accounts", []) if not acct.get("in_sync")
                for ln in acct.get("lines_off", [])]
    if off_rows:
        st.markdown("**Still off target - account by account, line by line:**")
        st.dataframe(pd.DataFrame(off_rows), hide_index=True, use_container_width=True)
