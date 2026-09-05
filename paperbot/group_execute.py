"""group_execute.py — the LIVE advisor-master target for the per-ticker group rail.

WHAT THIS IS
------------
The thin wiring layer between the pure planner (group_rebalance) and the proven block
executor (live_fa_block_execute.execute_fa_block_routes). It exists as its own module so
neither of those is edited: the block executor keeps working exactly as it does for the paper
master, and the per-account batch rail is not touched at all.

It also owns the LIVE advisor-master TargetGateway, which could not be written until the
master actually existed. It does now.

THE MASTER, READ LIVE 2026-09-03
--------------------------------
The port-4003 gateway login was switched from ``apsv1816`` to Andrew's advisor login
``asurber219`` and probed read-only:

    master              F6795549          (an F-prefixed live master; the PAPER one is DF...)
    client accounts     354               (the old login carried 18)
    existing FA groups  8                 Main 86, Ted 79, MainSmall 74, Rebalance 33,
                                          No Trade 14, Dougs Group 8, Income 8, Rob 4

All eight of those round-trip through fa_membership.parse_group_membership as a NO-OP, which
closed the standing MASTER_PLAN A.2 caveat that our group code had only ever seen fixtures.

STALE COMMENT WARNING. connections/clientids.py still says of consumer 63 that "the live 4003
login is NOT yet an advisor account: tested 2026-08-05 — 2 direct accounts, no master,
requestFA times out". That was true on 2026-08-05 and is NOT true now.

THE ACCOUNT WALL MATTERS MORE THAN IT USED TO
---------------------------------------------
The old login physically could not reach Ted's or Doug's books. This one carries all 354
accounts, so software is now the only thing that scopes a run. Verified 2026-09-03:
``roster.enrolled_roster()`` returns 185 — Andrew's book only, sourced from the CRM, with the
unfunded dropped. Every route still passes through the executor's own account wall on top of
that.
"""
from __future__ import annotations

import fa_group_sync
import group_rebalance
import reconcile
from connections import clientids
from live_fa_block_execute import TargetGateway

# The live advisor master, read from the gateway on 2026-09-03. F-prefixed; the paper master
# is DF8922141. NEVER traded and NEVER pinned — the master's own account-update stream hangs
# the session (memory: fa-block-order-allocation), which is why connect pins to a client sub.
LIVE_MASTER_ACCOUNT = "F6795549"


def live_gateway(enrollment: dict, *, pin_account: str | None = None) -> TargetGateway:
    """Build the LIVE TargetGateway for the port-4003 advisor master.

    ``enrollment`` is ``{account -> model label}`` and MUST come from the CRM roster
    (roster.enrolled_roster_scan), never from config.ENROLLMENT. That hardcoded map is the
    paper build's five DU subs; on the live master it would be both wrong and dangerous,
    because the login now carries 354 accounts including two other advisors' books.

    ``pin_account`` defaults to the lowest-numbered enrolled account, deterministically, so
    two runs of the same scope pin the same way and a run is reproducible. It is only the
    connection pin — it confers nothing on that account and it is not traded differently.

    ``group_names`` is None on purpose. The per-tier group map (TIER_GROUPS) is meaningless
    here: this rail creates ONE GROUP PER TICKER PER RUN and the group name travels on the
    route itself (group_rebalance.routes_from_group_plans), so there is no static map to
    resolve and nothing that can drift between runs.

    Raises ValueError on an empty enrollment — an empty roster read must never silently
    produce a gateway that would then trade nothing, or worse, be widened by a later caller.
    """
    book = {str(a).strip(): str(v) for a, v in (enrollment or {}).items() if str(a).strip()}
    if not book:
        raise ValueError(
            "live_gateway: the enrollment is EMPTY. An empty roster read must never produce a "
            "live advisor-master gateway. FAILING LOUD.")

    pin = str(pin_account or "").strip() or sorted(book)[0]
    if pin not in book:
        raise ValueError(
            f"live_gateway: pin_account {pin!r} is not in the enrollment. The connection must "
            f"pin to an account this run is actually scoped to. FAILING LOUD.")
    if pin == LIVE_MASTER_ACCOUNT:
        raise ValueError(
            f"live_gateway: refusing to pin to the master {LIVE_MASTER_ACCOUNT} — its "
            f"account-update stream hangs the session. Pin a CLIENT sub-account.")

    return TargetGateway(
        name="LIVE",
        host="127.0.0.1",
        port=clientids.LIVE_TRADE_PORT,          # 4003
        clientid_consumer="live_fa_block_exec",  # 63, reserved for exactly this
        master_account=LIVE_MASTER_ACCOUNT,
        pin_account=pin,
        enrollment=book,
        group_names=None,                        # per-run groups; the name is on the route
    )


# ========================================================================================
# THE PURE PLANNING CORE. Takes AccountPlans somebody else already built and returns the
# whole run as reviewable data. No broker, no XML, no order.
#
# WHY IT TAKES PLANS RATHER THAN BUILDING THEM. rebalance_engine.plan_account is the frozen
# engine and batch_rebalance_execute already drives it correctly - roster scope, per-account
# universe, per-model cash reserve, held-aside carve-out, strict live prices. Rebuilding that
# here would be a SECOND place for it to drift, and the two rails would silently size the same
# account differently. So the caller hands plans in and this module never sizes anything.
# ========================================================================================
def plan_group_run(plans, *, run_stamp: str, prices=None) -> dict:
    """PURE: AccountPlans -> ticker groups -> block routes, plus a reviewable summary.

    Returns ``{"group_plans", "routes", "summary_text", "n_groups", "n_accounts",
    "n_buy", "n_sell", "accounts", "symbols"}``.

    ``run_stamp`` identifies the run and becomes part of every group name, which is what makes
    each group the audit record of ONE run. Two runs on the same day MUST pass different
    stamps; a collision is refused downstream at creation rather than silently reusing another
    run's group.

    Builds nothing and transmits nothing. Every refusal in the pure layer (a split that does
    not sum, an empty group, duplicate group names, an account on both sides of a symbol)
    raises here, BEFORE anything is created at the broker.
    """
    group_plans = group_rebalance.plan_ticker_groups(
        plans, run_stamp=run_stamp, prices=prices)
    routes = group_rebalance.routes_from_group_plans(group_plans)
    accounts = sorted({a for g in group_plans for a in g.per_account})
    return {
        "group_plans": group_plans,
        "routes": routes,
        "summary_text": group_rebalance.summarize(group_plans),
        "n_groups": len(group_plans),
        "n_accounts": len(accounts),
        "n_buy": sum(1 for g in group_plans if g.side == "BUY"),
        "n_sell": sum(1 for g in group_plans if g.side == "SELL"),
        "accounts": accounts,
        "symbols": sorted({g.symbol for g in group_plans}),
    }


def accounts_outside_the_wall(group_plans, allowed_accounts) -> list:
    """Every account in the run that is NOT on the human-blessed roster, sorted.

    A SECOND, INDEPENDENT check of the account wall, run over the GROUP SPLITS before any
    group is created. The block executor applies its own wall per route, but a group is
    created BEFORE its route is executed - so without this an account outside the roster could
    be written into a live FA group at the master, and only refused later at the order. The
    group would still be sitting there naming a client this run had no business touching.

    This matters far more than it used to. The 4003 login now carries 354 accounts, including
    Ted's 98 and Doug's 17; until 2026-09-03 it carried 18 and could not reach them at all.
    Software is now the only thing scoping a run.

    Returns [] when everything is in scope. PURE.
    """
    allowed = {str(a).strip() for a in (allowed_accounts or []) if str(a).strip()}
    in_run = {a for g in group_plans for a in g.per_account}
    return sorted(in_run - allowed)


class GroupRunRefused(RuntimeError):
    """The run was refused before ANY group was created and before any order. Nothing at the
    broker was touched."""


def create_run_groups(ib, group_plans, *, allowed_accounts, armed: bool = False,
                      backup_path: str | None = None) -> dict:
    """Create EVERY group this run needs, up front, before a single order is placed.

    ORDER OF OPERATIONS, AND WHY.

    1. THE ACCOUNT WALL, OVER THE WHOLE RUN, FIRST. If any account in any group is off the
       human-blessed roster the WHOLE run is refused and nothing is created. This is checked
       here rather than only per-route because a group is created BEFORE its route executes:
       without it, an off-roster account could be written into a live FA group at the master
       and only refused later at the order, leaving a group standing that names a client this
       run had no business touching. The 4003 login now carries 354 accounts including two
       other advisors' books, so this is the wall that matters.

    2. THEN CREATE ALL GROUPS, before any order. A creation that fails half way leaves some
       groups created and ZERO orders placed - recoverable, and no money has moved. Creating
       them lazily, one before each block, would mean a failure at group 9 of 17 had already
       traded the first eight tickers.

    Each creation is its own read -> plan -> backup -> arm-gated replaceFA -> strict read-back
    (fa_group_sync.create_run_group), so N creations compose correctly: each one reads the
    document the previous one wrote.

    UNARMED this is a PREVIEW: every creation is planned and its diff returned, nothing is
    written. Returns ``{"refused", "refused_reason", "results", "created", "previewed"}``.
    """
    plans = list(group_plans or [])
    if not plans:
        return {"refused": False, "refused_reason": "", "results": [],
                "created": 0, "previewed": 0}

    outside = accounts_outside_the_wall(plans, allowed_accounts)
    if outside:
        raise GroupRunRefused(
            f"create_run_groups: {len(outside)} account(s) in this run are NOT on the "
            f"human-blessed roster: {', '.join(outside)}. The whole run is refused. NOTHING "
            f"was created and no order was placed. The login carries every account under the "
            f"advisor master, so the roster is the only thing scoping a run.")

    results = []
    created = previewed = 0
    for g in plans:
        res = fa_group_sync.create_run_group(
            ib, g.group_name, g.accounts, armed=armed, backup_path=backup_path)
        res["symbol"] = g.symbol
        res["side"] = g.side
        res["total_qty"] = g.total_qty
        results.append(res)
        if res.get("wrote"):
            created += 1
        else:
            previewed += 1
    return {"refused": False, "refused_reason": "", "results": results,
            "created": created, "previewed": previewed}


def build_plans_for_accounts(ib, accounts: list, *, band_pct=None) -> dict:
    """Read state, price the universe, and size EXACTLY the given accounts. Read-only.

    This is the account-list-driven core :func:`build_plans_for_scope` uses once it has
    resolved a model scope into an account list — factored out (2026-09-05, the
    withdrawal-cash-raise trigger) so a caller that already knows precisely which accounts
    it wants (e.g. the ones a shortfall check flagged) can size THOSE accounts and nothing
    else, without going through a model selection at all. Nothing below reads a model list;
    ``accounts`` is the whole scope.

    Returns ``{"plans", "prices", "versions", "targets", "metas", "skipped",
    "account_inputs", "summaries"}``. The last two are the block executor's inputs, built
    from the SAME state the plans were sized on so its margin and PDT pre-flights can never
    disagree with the plan about an account.

    EVERY SUBSTANTIVE STEP IS THE BATCH RAIL'S OWN FUNCTION, called not copied:
      * resolve_roster_versions                   - account -> model label
      * build_targets                             - SOURCE-based dispatch; a custom label is
                                                    built from the published CRM allocation
                                                    and never falls through to the S0 engine
      * build_per_account_state                   - positions, secTypes, contracts, NAV
      * build_execution_prices                    - live-quote-only + the mutual-fund mark
      * account_universe / account_reserve_pct    - per-account universe and cash reserve
      * rebalance_engine.plan_account             - the frozen engine, untouched

    So this function decides nothing on its own. It is the loop that hands those decisions to
    the engine, which is why the group rail and the per-account rail cannot size the same
    account differently — and why a caller scoped to 2 accounts spanning 2 different models
    gets back plans for exactly those 2 accounts, never a 3rd sibling in either model.
    """
    import batch_rebalance_execute as bre
    import rebalance_engine

    accounts = [str(a).strip() for a in (accounts or []) if str(a).strip()]
    if not accounts:
        return {"plans": [], "prices": {}, "versions": {}, "targets": {}, "metas": {},
                "skipped": [], "account_inputs": [], "summaries": {}}

    versions = bre.resolve_roster_versions(accounts)
    targets, metas = bre.build_targets(sorted(set(versions.values())))
    state, held_symbols, held_contracts = bre.build_per_account_state(ib, accounts)
    prices, _quotes, _universe = bre.build_execution_prices(
        ib, accounts, targets, state, held_symbols, held_contracts)

    # THE S0 BASE UNIVERSE, resolved exactly as the batch rail resolves it. It is only used
    # for an S0 model - a custom allocation derives its own universe from the published
    # allocation plus the account's held symbols - but a run whose scope included an S0 label
    # would size against the wrong universe if this were guessed, so it is not guessed.
    #
    # REFUSE, do not degrade, when it cannot be resolved: without it reconcile cannot tell a
    # spinoff, a rename, a client's own holding or a money-market sweep apart from a symbol
    # the model dropped, and every one of them would size as a FULL LIQUIDATION. Same refusal
    # the batch rail makes, for the same reason.
    import s0_live_pilot_run as sp
    strat_universe = sp._strategy_universe()
    if not strat_universe:
        raise GroupRunRefused(
            "build_plans_for_accounts: the strategy's tradeable universe could not be "
            "resolved, so a spinoff, a rename, a client's own holding or a money-market "
            "sweep cannot be told apart from a symbol the model dropped - every one of them "
            "would size as a FULL LIQUIDATION. Nothing sized, nothing created, nothing "
            "transmitted.")

    plans, skipped = [], []
    # The block executor's own margin and PDT pre-flights re-derive each account's plan from
    # these, so they are built HERE from the same state the plans were sized on. Building them
    # a second time somewhere else is how the two would disagree about one account.
    account_inputs, summaries = [], {}
    for account in accounts:
        st = state[account]
        v = versions[account]
        net_liq = st["net_liq"]
        if not net_liq or net_liq <= 0:
            # An account with no readable positive NetLiq cannot be acted on. Named, skipped,
            # never silently sized to zero (which would read as a full liquidation).
            skipped.append(account)
            continue
        target = targets[v]
        acct_universe = bre.account_universe(target, metas.get(v), st["positions"],
                                             base=strat_universe)
        account_inputs.append({
            "account": account, "version": target.version, "net_liq": net_liq,
            "positions": st["positions"], "prices": prices, "sec_types": st["sec_types"],
            "strict_prices": True})
        summaries[account] = st["summary"]
        plans.append(rebalance_engine.plan_account(
            account, target.version, net_liq, st["positions"], target,
            prices=prices,
            universe=acct_universe,
            sec_types=st["sec_types"],
            cash_reserve_pct=bre.account_reserve_pct(metas.get(v)),
            band_pct=band_pct,
            # EXECUTION RAIL: the live IBKR quotes above are the ONLY price source. A model's
            # stored close is never substituted for a quote we could not get.
            strict_prices=True))
    return {"plans": plans, "prices": prices, "versions": versions, "targets": targets,
            "metas": metas, "skipped": skipped,
            "account_inputs": account_inputs, "summaries": summaries}


def build_plans_for_scope(ib, *, models=None, band_pct=None) -> dict:
    """Scope by MODEL, read state, price the universe, and size every account. Read-only.

    Returns ``{"plans", "prices", "versions", "targets", "metas", "roster", "scan",
    "skipped", "account_inputs", "summaries"}``. The last two are the block executor's
    inputs, built from the SAME state the plans were sized on so its margin and PDT
    pre-flights can never disagree with the plan about an account.

    Thin wrapper (2026-09-05): resolves the model scope into an account list via
    ``roster.enrolled_roster_scan``, then hands that list to :func:`build_plans_for_accounts`
    for every substantive step. This function decides nothing beyond the model->account
    resolution — the sizing loop lives in exactly one place.

    THE MODEL SCOPE IS THE RUN. Pass models=["Conservative (Custom)"] and the run is that
    model's accounts - one account today. That is what makes the staged rollout possible.
    """
    import roster as roster_mod

    scan = roster_mod.enrolled_roster_scan(models=models)
    accounts = scan["accounts"]
    if not accounts:
        return {"plans": [], "prices": {}, "versions": {}, "targets": {}, "metas": {},
                "roster": [], "scan": scan, "skipped": []}

    built = build_plans_for_accounts(ib, accounts, band_pct=band_pct)
    built["roster"] = accounts
    built["scan"] = scan
    return built


# NO ALGO ON A REBALANCE BLOCK (owner decision 2026-09-04: "this is trading not scalping").
# Adaptive/Patient works an order toward the midpoint and refuses to cross, which turns a
# rebalance into a fill-or-time-out lottery: on 2026-09-04 every buy block sat unfilled for
# its full 90s window and cancelled. A rebalance needs to GET DONE at a fair price, not to
# save a basis point. None = a plain marketable limit at the cap, which crosses and fills.
# Set to "Urgent"/"Normal"/"Patient" only for a deliberate experiment.
ADAPTIVE_PRIORITY = None


def verify_in_sync(ib, models) -> dict:
    """RE-READ the book after a run and answer ONE question per account: is it on target?

    THE POINT OF AUTOMATING THIS. The run result says what the ORDERS did. That is not the
    same question as whether the ACCOUNTS are right, and on 2026-09-04 the difference was the
    whole story: 25 blocks were "sent", 12 traded nothing, and five accounts were left part
    way through a rebalance with no surface anywhere saying so.

    This re-prices and re-plans the same scope, read-only, straight after the run. An account
    is IN SYNC when the engine would place no further orders for it. Anything else is named,
    with the exact symbol, what is held, what the model wants, and the remaining delta -- per
    account, per line. Never a count of blocks, never "sent".

    NEVER raises into a finished run: a verification failure is reported as ok=False, because
    losing the check is bad but losing it AND crashing after the money moved is worse.
    """
    try:
        after = build_plans_for_scope(ib, models=list(models or []))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "accounts": [],
                "in_sync": 0, "out_of_sync": 0}
    rows = []
    for pl in after.get("plans") or []:
        off = []
        for ln in pl.lines:
            delta = (pl.orders or {}).get(ln.symbol)
            if delta:
                off.append({"account": pl.account, "symbol": ln.symbol,
                            "held": float(ln.actual_shares or 0.0),
                            "model_wants": float(ln.target_shares or 0.0),
                            "still_needs": float(delta),
                            "status": ln.status})
        rows.append({"account": pl.account, "model": pl.version,
                     "in_sync": not pl.needs_rebalance, "lines_off": off})
    return {"ok": True, "error": "", "accounts": rows,
            "in_sync": sum(1 for r in rows if r["in_sync"]),
            "out_of_sync": sum(1 for r in rows if not r["in_sync"])}


def dust_stubs_from_sync(sync: dict, *, scope: set | None = None) -> list[dict]:
    """Pull the sub-share stubs THIS RUN left behind out of verify_in_sync's own report.

    A FRACTIONAL line (reconcile.FRACTIONAL: "held, weight 0, int(shares) == 0 but shares !=
    0") IS the remainder a full-exit sell could not clear, because config.BLOCK_ORDERS_
    WHOLE_SHARES_ONLY truncates the block to whole shares before it ever reaches the wire
    (IBKR error 10243 — see config.py). verify_in_sync already re-reads the book and reports
    that exact leftover as a `lines_off` entry; this is a FILTER over data it already
    computed, not a new calculation.

    `scope`, when given, is the set of (account, symbol) pairs THIS run actually sent a SELL
    for — verify_in_sync rescans the whole model, which also carries every OLDER stub the
    nightly foreign-holding scan already knows about (see D.5's cleanup list), and those must
    not be re-reported here as if this run just made them. Pass the run's own SELL pairs to
    get only the NEW stubs this run's truncation left; omit it to see every FRACTIONAL line
    verify_in_sync found (used by callers with no run-scope of their own, and by tests).
    """
    out = []
    for acct in sync.get("accounts", []):
        for ln in acct.get("lines_off", []):
            if ln.get("status") != reconcile.FRACTIONAL:
                continue
            if scope is not None and (ln["account"], ln["symbol"]) not in scope:
                continue
            out.append({"account": ln["account"], "symbol": ln["symbol"],
                        "quantity": abs(ln["held"])})
    return out


def execute_group_run(ib, target, run, built, *, allowed_accounts, armed: bool = False,
                      backup_path: str | None = None,
                      adaptive_priority: str | None = ADAPTIVE_PRIORITY) -> dict:
    """Create this run's groups, then hand its routes to the proven block executor.

    ORDER OF OPERATIONS. Groups are created FIRST, all of them, before any order - a creation
    that fails half way leaves groups made and ZERO orders placed, which is recoverable and
    costs nothing. Only then does live_fa_block_execute.execute_fa_block_routes run, which
    owns everything from there: the two-phase SELL-then-BUY gate, the realized-cash re-read
    between phases, the per-account margin and PDT pre-flights over the split, writing each
    group's ContractsOrShares in lockstep with placing that group's block, and the
    uninvested-proceeds report. None of that is reimplemented here.

    UNARMED IS A PREVIEW END TO END: no group is created, no FA config is written, no order is
    placed. ``permit`` is passed straight through, so the executor's own gate decides.

    ``adaptive_priority`` defaults to Patient - IBKR's Adaptive algo works the block between
    the bid and ask instead of crossing the spread. Pass None to fall back to the plain capped
    marketable limit, which is the one-argument retreat if IBKR refuses an algo on a group
    order (undocumented either way; the first staged run is the test).
    """
    import live_fa_block_execute as fab
    import order_router
    import safe_execute
    from connections import clientids

    plans = run.get("group_plans") or []
    routes = run.get("routes") or []

    if not armed:
        # PREVIEW. Every group is planned and its diff returned; nothing is written.
        created = create_run_groups(ib, plans, allowed_accounts=allowed_accounts,
                                    armed=False, backup_path=backup_path)
        return {"created": created, "executed": None,
                "note": "Preview only - no group was created, no FA config was written and "
                        "no order was placed."}

    if not routes:
        return {"created": {"created": 0, "previewed": 0, "results": []}, "executed": None,
                "note": "Nothing in this scope needs to trade."}

    # THE ARM GATE. config.READONLY / config.DRY_RUN are committed True on disk on purpose, so
    # nothing transmits from a fresh process no matter what a caller passes. armed_session is
    # the ONE place they are flipped, in-process, restored in a finally, and it holds the
    # gateway lock for the whole armed body so no other desk task can use the connection
    # underneath us. BOTH halves must be inside it: replaceFA (creating each group) is gated by
    # the same flags as placing an order, so creating the groups outside the session would
    # silently produce a preview and then "send" nothing.
    with safe_execute.armed_session(
            purpose="group_trade",
            client_id=clientids.get(target.clientid_consumer),
            gateway_lock_on_busy="refuse"):
        created = create_run_groups(ib, plans, allowed_accounts=allowed_accounts,
                                    armed=True, backup_path=backup_path)
        permit, why = order_router.transmit_guard(True)
        if not permit:
            return {"created": created, "executed": None,
                    "note": f"Refused inside the arm gate ({why}). Nothing was placed."}
        executed = fab.execute_fa_block_routes(
            ib, routes, built["account_inputs"], built["targets"], target,
            permit=permit, summaries=built.get("summaries"), run_id=run.get("stamp"),
            adaptive_priority=adaptive_priority)
        _record_run(target, run, routes, created, executed,
                    adaptive_priority=adaptive_priority)

    # OUTSIDE the arm gate: the book is re-read read-only, so the arming flags are already
    # restored and the gateway lock is released before this slower scan runs.
    sync = verify_in_sync(ib, run.get("models"))

    # TRADE-DUST REPORTING (D.5 fix 3, 2026-09-05). This run's own SELL blocks are the only
    # source of a NEW sub-share stub (config.BLOCK_ORDERS_WHOLE_SHARES_ONLY truncates a
    # full-exit sell to whole shares before it goes out). Scoping to exactly the (account,
    # symbol) pairs this run sent a SELL for keeps this list to what THIS run just left
    # behind, not the whole model's pre-existing dust (that backlog is D.5's one-time cleanup
    # list, not something every run should re-announce).
    sell_pairs = {(a, p.symbol) for p in plans if p.side == "SELL" for a in p.per_account}
    dust = dust_stubs_from_sync(sync, scope=sell_pairs)
    if dust:
        print(f"    !! {len(dust)} sub-share stub(s) left from this run - clear these in "
              f"TWS: {dust}")

    try:
        import ledger
        ledger.record_run({"mode": "GROUP_TRADE_SYNC_CHECK", "run_id": run.get("stamp"),
                           "models": list(run.get("models") or []), "sync": sync,
                           "dust": dust})
    except Exception as exc:
        print(f"    !! sync check not written to the ledger ({type(exc).__name__}: {exc})")
    return {"created": created, "executed": executed, "sync": sync, "dust": dust, "note": ""}


def _record_run(target, run, routes, created, executed, *, adaptive_priority=None) -> None:
    """Write ONE durable audit record for a group run. NEVER raises into the run.

    WHY THIS EXISTS. On 2026-09-04 a group run created 17 FA groups and sent 17 block orders
    and left NO record anywhere: execute_fa_block_routes does not write the ledger - the block
    rail's own main() does - and this path calls the executor directly, so it skipped it. The
    result dict went to the screen and nowhere else, and once the page moved on the detail was
    gone. IBKR does not retain rejected orders past the session, so WHY those blocks produced
    nothing is now unrecoverable. That must never be possible again.

    Records the run scope, the groups it created, every route with its per-account split, the
    fills, and the run_id - so an IBKR orderRef, which ends in that run_id, joins back to this
    record and vice versa.

    Wrapped so a ledger failure can never take down a run that has already traded: losing the
    record is bad, but losing it AND crashing mid-flight is worse.
    """
    try:
        import ledger
        ex = executed or {}
        ledger.record_run({
            "mode": "GROUP_TRADE_ARMED",
            "account": f"<group run, {len(run.get('accounts') or [])} account(s)>",
            "master": target.master_account,
            "models": run.get("models") or [],
            "accounts": run.get("accounts") or [],
            "nav": 0.0, "daily_pnl": 0.0,
            "target_as_of": run.get("stamp", ""), "target_weights": {},
            "adaptive_priority": adaptive_priority,
            "groups_created": [r.get("fa_group") for r in (created.get("results") or [])],
            "n_groups_created": created.get("created", 0),
            "group_backups": [r.get("backup") for r in (created.get("results") or [])
                              if r.get("backup")],
            "intents": [{"route": r.route, "side": r.side, "symbol": r.symbol,
                         "qty": r.total_qty, "group": r.fa_group,
                         "split": r.per_account_split} for r in routes],
            "n_intents": len(routes), "n_approved": len(routes),
            "n_transmitted": len(ex.get("placed_fills") or []),
            "fills": ex.get("placed_fills") or [],
            "sell_results": ex.get("sell_results") or [],
            "buy_results": ex.get("buy_results") or [],
            "realized_cash": ex.get("realized_cash") or {},
            "buy_resize": ex.get("buy_resize") or {},
            "dropped_buy_blocks": ex.get("dropped_buy_blocks") or [],
            "pdt_dropped": ex.get("pdt_dropped") or [],
            "uninvested": ex.get("uninvested") or [],
            "refused": ex.get("refused", False),
            "refused_reason": ex.get("refused_reason", ""),
            "replace_fa_writes": ex.get("replace_fa_writes", 0),
            "run_id": ex.get("run_id") or run.get("stamp", ""),
            "halted": False, "halt_reason": "",
        })
    except Exception as exc:  # noqa: BLE001
        print(f"    !! COULD NOT WRITE THE AUDIT RECORD ({type(exc).__name__}: {exc}). The run "
              f"itself is unaffected, but THIS RUN IS UNRECORDED - capture the screen before "
              f"moving on.")

def purge_run_groups(ib, *, armed: bool = False, keep_stamps=(),
                     client_id: int = 118, max_passes: int = 8,
                     batch: int = 40, settle_sec: float = 4.0) -> dict:
    """Delete SPENT throwaway run-groups from the master, IN PASSES.

    WHY PASSES (measured 2026-09-04). Every group run leaves one throwaway group per block
    behind as its audit record; four runs took the master's document from 8 groups to 198,
    and the gateway then stopped committing FA writes at all -- IBKR error 10230, "You have
    unsaved FA changes" -- which blocked trading outright.

    The first attempt to delete all 190 in ONE replaceFA removed 108 and then stopped: the
    gateway accepts a write and refuses further ones until it settles. A second identical
    call deleted NOTHING while reporting success, because the verify read straight after a
    write is served STALE. So: delete a BATCH, wait, RE-READ on its own terms, and repeat
    until either nothing dated is left or a pass makes no progress. Truthful either way --
    `deleted` is measured from the live document, never from what we intended.

    Only groups whose name carries a dated run stamp (``YYYYMMDD-HHMM``) are touched;
    anything without one is PERMANENT and is never removed. ``keep_stamps`` spares named
    stamps, e.g. a run still in flight.
    """
    import re
    import time
    import xml.etree.ElementTree as ET
    import rebalance_execute as rx
    import fa_group_sync as fgs
    import safe_execute
    from fa_membership import serialize_groups as fa_membership_serialize

    stamp_re = re.compile(r"\d{8}-\d{4}")
    spare = set(keep_stamps)

    def _name(g):
        e = g.find("name")
        return (e.text or "").strip() if e is not None else ""

    def _dated(names):
        out = []
        for n in names:
            m = stamp_re.search(n)
            if m and m.group(0) not in spare:
                out.append(n)
        return out

    backup = rx.backup_fa_groups(ib)
    first_names = [_name(g) for g in ET.fromstring(fgs.read_live_groups(ib)).findall(".//Group")]
    started_with = len(first_names)
    result = {"backup": backup, "started_with": started_with, "deleted": 0, "passes": 0,
              "remaining": started_with, "remaining_dated": len(_dated(first_names)),
              "permanent": started_with - len(_dated(first_names)),
              "written": False, "refused": "", "notes": []}

    if not _dated(first_names):
        result["refused"] = "Nothing to clean up - no dated run groups found."
        return result
    if not armed:
        result["refused"] = "Preview only - not armed, nothing written."
        return result

    for attempt in range(1, max_passes + 1):
        xml = fgs.read_live_groups(ib)
        root = ET.fromstring(xml)
        groups = list(root.findall(".//Group"))
        before = len(groups)
        doomed, keep = [], []
        for g in groups:
            n = _name(g)
            m = stamp_re.search(n)
            (doomed if (m and m.group(0) not in spare) else keep).append(g)
        if not doomed:
            result["notes"].append(f"pass {attempt}: nothing dated left")
            break
        if not keep:
            result["refused"] = ("REFUSING: every group carries a run stamp; this would empty "
                                 "the master's configuration.")
            break

        chunk = doomed[:max(1, int(batch))]
        parent = root.find(".//ListOfGroups")
        if parent is None:
            parent = root
        for g in chunk:
            parent.remove(g)
        new_xml = fa_membership_serialize(root)
        expect = before - len(chunk)
        if len(ET.fromstring(new_xml).findall(".//Group")) != expect:
            result["refused"] = f"REFUSING on pass {attempt}: post-edit count mismatch."
            break

        with safe_execute.armed_session(purpose="fa_group_purge", client_id=client_id,
                                        gateway_lock_on_busy="refuse"):
            fgs.apply_membership_change(ib, new_xml, armed=True, backup_path=backup)
        result["written"] = True

        # SETTLE, then re-read on its OWN terms. A read straight after a write is stale, which
        # is how the second attempt reported deleting 82 groups while deleting none.
        time.sleep(float(settle_sec))
        now = len(ET.fromstring(fgs.read_live_groups(ib)).findall(".//Group"))
        moved = before - now
        result["passes"] = attempt
        result["notes"].append(f"pass {attempt}: asked to drop {len(chunk)}, actually dropped {moved}")
        if moved <= 0:
            result["notes"].append("gateway stopped accepting writes - stopping here")
            break

    final = [_name(g) for g in ET.fromstring(fgs.read_live_groups(ib)).findall(".//Group")]
    result["remaining"] = len(final)
    result["remaining_dated"] = len(_dated(final))
    result["permanent"] = len(final) - len(_dated(final))
    result["deleted"] = started_with - len(final)
    return result
