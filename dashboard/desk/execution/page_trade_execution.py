"""page_trade_execution.py — TRADE EXECUTION. Pick strategies, prepare, check, send.

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
import time
from pathlib import Path

import pandas as pd
import streamlit as st

# This module lives at dashboard/desk/execution/page_trade_execution.py, so the repo root
# is parents[3].
_PAPERBOT = str(Path(__file__).resolve().parents[3] / "paperbot")
if _PAPERBOT not in sys.path:
    sys.path.insert(0, _PAPERBOT)

CONFIRM_PHRASE = "SEND GROUP TRADE"
_STATE = "group_trade_prepared"

# HOW OLD A PREPARED TRADE MAY BE BEFORE IT HAS TO BE PREPARED AGAIN.
# _prepare() reads positions and live prices ONCE and stores the sized plan; _send() then
# reuses that stored plan word for word — it never re-reads the accounts and never works the
# share counts out again. So an operator who prepares a trade, leaves the tab open and comes
# back later would send share counts and prices from whenever they pressed Prepare. Nothing
# caught that: the only staleness check was on changing the strategy selection, which says
# nothing about how much time has passed. A prepared trade therefore EXPIRES and has to be
# prepared again. This can only ever STOP a send; it can never allow one the existing checks
# would have refused. 30 minutes matches the window the Control Plane already uses for its
# own reviewed preview (PREVIEW_FRESHNESS_SECS in page_control_plane.py).
PREPARED_FRESHNESS_SECS = 1800.0


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
    # A plain epoch timestamp ALONGSIDE stamp, never instead of it — other code reads
    # stamp, and it is minute-resolution text rather than something to do arithmetic on.
    # This is what the freshness gate below measures the run's age against.
    prepared_at = time.time()
    ib = IB()
    ib.connect("127.0.0.1", 4003, clientId=115, readonly=True, timeout=30)
    try:
        built = ge.build_plans_for_scope(ib, models=models)
        run = ge.plan_group_run(built["plans"], run_stamp=stamp, prices=built["prices"])
        run["roster"] = built["roster"]
        run["skipped"] = built["skipped"]
        run["outside"] = ge.accounts_outside_the_wall(run["group_plans"], built["roster"])
        run["stamp"] = stamp
        run["prepared_at"] = prepared_at
        run["models"] = list(models)
        run["_built"] = built
        return run
    finally:
        ib.disconnect()


def _freshness_of(prepared_at, now: float,
                  window_secs: float = PREPARED_FRESHNESS_SECS) -> tuple:
    """Pure freshness decision — unit-testable without Streamlit. Both times are plain
    epoch seconds. Returns (age_secs, is_fresh). A missing or unusable prepared_at gives
    (None, False), so a run that never recorded when it was prepared counts as stale
    rather than being waved through. Fresh iff age <= window (a small negative age from
    clock skew counts as fresh)."""
    if prepared_at is None:
        return (None, False)
    try:
        age = float(now) - float(prepared_at)
    except (TypeError, ValueError):  # a bad timestamp is simply "not fresh"
        return (None, False)
    if age != age:  # not-a-number never compares true, so say so plainly
        return (None, False)
    return (age, age <= window_secs)


def _age_phrase(age_secs) -> str:
    """How long ago, spelled out in full English words — no shorthand, no abbreviations."""
    if age_secs is None:
        return "at a time that was not recorded"
    if age_secs < 0:
        return "just now"
    minutes = int(age_secs // 60)
    if minutes < 1:
        return "less than a minute ago"
    if minutes == 1:
        return "about 1 minute ago"
    if minutes < 60:
        return "about {} minutes ago".format(minutes)
    hours, rest = divmod(minutes, 60)
    hour_words = "1 hour" if hours == 1 else "{} hours".format(hours)
    if rest == 0:
        return "about {} ago".format(hour_words)
    minute_words = "1 minute" if rest == 1 else "{} minutes".format(rest)
    return "about {} and {} ago".format(hour_words, minute_words)


def _freshness_check(run: dict, now: float = None) -> tuple:
    """The prepared-trade freshness line, in the same (ok, sentence) shape as _checks()'s
    own lines so it renders and blocks exactly like them.

    KEPT OUT OF _checks() ON PURPOSE. The Raise withdrawal cash page reuses _checks()
    word for word, but it prepares its runs through paperbot/withdrawal_cash_raise.py,
    which records no prepare time — so folding this line into _checks() would leave that
    page permanently unable to send. This check belongs to this page's own Send step.
    """
    age, fresh = _freshness_of(run.get("prepared_at"),
                               time.time() if now is None else now)
    minutes = int(PREPARED_FRESHNESS_SECS // 60)
    if fresh:
        return (True, "This trade was prepared {}, so the share counts and prices below "
                      "are still current.".format(_age_phrase(age)))
    if age is None:
        return (False, "This trade did not record when it was prepared, so there is no way "
                       "to tell whether its share counts and prices are still current. "
                       "Press Prepare again before sending.")
    return (False, "This trade was prepared {} and is now too old to send. The share counts "
                   "and prices were worked out at that moment and are not checked again "
                   "when you press Send, so a trade older than {} minutes has to be "
                   "prepared again. Press Prepare again.".format(_age_phrase(age), minutes))


def _prepared_at_text(run: dict) -> str:
    """The clock time the run was prepared, for showing the operator. Never blank — an
    unrecorded time says so in words rather than showing nothing."""
    prepared_at = run.get("prepared_at")
    if prepared_at is None:
        return "a time that was not recorded"
    try:
        when = datetime.datetime.fromtimestamp(float(prepared_at))
    except (TypeError, ValueError, OSError, OverflowError):
        return "a time that could not be read"
    return when.strftime("%I:%M %p on %B %d").lstrip("0")


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


def render_trade_execution() -> None:
    st.markdown("## Trade Execution")
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

    # WHEN this was prepared, always on screen. _send() reuses the plan above word for
    # word, so the age of that plan is part of what the operator is approving and must
    # never be invisible.
    _age, _fresh = _freshness_of(run.get("prepared_at"), time.time())
    st.caption("Prepared at {} — {}. The share counts above were worked out then and are "
               "not checked again when you press Send.".format(
                   _prepared_at_text(run), _age_phrase(_age)))

    st.markdown("#### 3. Checks")
    # The freshness line is appended here rather than inside _checks(), which the Raise
    # withdrawal cash page reuses word for word — see _freshness_check's own note.
    checks = _checks(run) + [_freshness_check(run)]
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

    The freshness refusal below is deliberately repeated here as well as in the checks the
    operator reads. The checks already stop a stale run from reaching this function, so
    this is the backstop that keeps that true if the page is ever rearranged: it refuses
    BEFORE the gateway is contacted, so a stale run opens no connection and sends nothing.
    """
    from ib_async import IB
    import group_execute as ge

    _age, _fresh = _freshness_of(run.get("prepared_at"), time.time())
    if not _fresh:
        raise RuntimeError(
            "This trade was prepared {} and is too old to send, so nothing was sent. The "
            "share counts and prices were worked out at that moment and are not checked "
            "again at send time. Press Prepare again.".format(_age_phrase(_age)))

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

    # TRADE DUST THIS RUN LEFT BEHIND (D.5 fix 3): a sub-share stub a full-exit sell could not
    # clear because IBKR's API refuses any fractional order (error 10243, config.
    # BLOCK_ORDERS_WHOLE_SHARES_ONLY). Reported here, once, at trade time, rather than only
    # turning up later when the nightly foreign-holding scan rediscovers it.
    dust = result.get("dust") or []
    if dust:
        st.warning("{} sub-share stub(s) left from this run - clear these in TWS:"
                   .format(len(dust)))
        st.dataframe(pd.DataFrame(dust), hide_index=True, use_container_width=True)
