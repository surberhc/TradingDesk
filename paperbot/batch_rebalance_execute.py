"""
batch_rebalance_execute.py — the MULTI-ACCOUNT BATCH REBALANCE executor (real-transmission,
port-4003 FA master), built READY TO ROLL: it transmits NOTHING until a human explicitly arms.

WHAT THIS IS
------------
The single-account sibling s0_live_deploy.py rebalances ONE funded account (U14438624) into
its S0 target behind the review -> arm -> transmit gate. THIS file is its multi-account
BATCH sibling: it rebalances EVERY out-of-spec account on the human-blessed execution roster
to that account's model, one account at a time, behind the SAME gate — reusing the SAME
shared safe-execution engine (safe_execute.execute_plan, the desk's one real-money transmit
chokepoint) for each account.

It DOES NOT re-implement any execution logic. It is a THIN driver that:
  * pulls the execution allow-list from roster.enrolled_roster() (the CRM-blessed roster, or
    the config.ENROLLMENT fallback — currently only the 2 blessed accounts, on purpose);
  * reads each blessed account's live NetLiq/positions off the ONE 4003 FA-master login
    (s0_live.filter_account_summary / filter_positions — the same per-account filtering the
    single-account lane already uses);
  * sizes each account with the UNCHANGED frozen engine (rebalance_engine.plan_account);
  * maps the sized plans to per-account ExecutionRequests via the PURE
    crm_execute.requests_from_crm_plan (purpose=REBALANCE, conform=False, allowed_accounts=
    the roster) — which SKIPS every already-in-band account, so the set that actually trades
    is exactly the OUT-OF-SPEC subset of the roster; and
  * drives safe_execute.execute_plan per request — PREVIEW by default (sends nothing),
    ARMED (two-phase cash-gated transmit, per-account margin pre-flight) only when the human
    lines up the arm token AND the physical 4003 Gateway is armed.

SANDBOXED TO THE ROSTER (two independent walls)
-----------------------------------------------
  1. We only ever READ + SIZE accounts that are IN roster.enrolled_roster(). A non-roster
     account is never touched.
  2. Every ExecutionRequest carries allowed_accounts=the roster, so safe_execute's
     account_wall_ok independently REFUSES any account not on the roster — the wall never
     "allows whatever the planner produced". Widening execution is a deliberate roster edit
     (Andrew widens the CRM roster in stages), NOT a code change here.

DEFAULT IS SAFE. With no flag this runs a PREVIEW: it sizes every roster account, prints each
account's would-trade order list + the self-computed per-account margin pre-flight (#57), an
aggregate summary, and transmits NOTHING. To actually transmit, a human must line up ALL of:
  * the exact CLI token  --arm-i-understand  (sets armed=True; never defaulted/auto-set),
  * NO kill-switch sentinel present,
  * every target account IN the enrolled roster (any other refused — the account wall),
  * every leg whole-share, priced, through order_router's HARD price guard,
  * each account's own caps (total BUY <= investable, per-order <= 50% NetLiq) + margin
    pre-flight passing, AND
  * the Gateway physically ARMED (Read-Only API toggle OFF — measured live per account with
    the zero-transmission cancel-a-fabricated-order probe).
Miss ANY one and that account is a preview that transmits nothing and prints WHY. There is NO
auto-arm and nothing here is scheduled.

FA-BLOCK whatIf (owner-gated, OPTIONAL — left stubbed by design)
----------------------------------------------------------------
This batch places PER-ACCOUNT direct orders through execute_plan's proven two-phase path
(each order carries its sub-account; the 4003 FA master routes it), each with its own
margin pre-flight. A separate, owner-gated optimisation would instead place ONE FA-BLOCK per
symbol across accounts and margin-check it with a live whatIf via the OFFICIAL ibapi — a
NON-PyPI dependency the desk deliberately does not add here. That branch is designed for but
left stubbed (fa_block_whatif_preflight below); the per-account path needs none of it.

Run — PREVIEW (default; transmits nothing):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe batch_rebalance_execute.py

Run — ARMED batch rebalance (human-supervised; requires an armed Gateway + no kill switch):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe batch_rebalance_execute.py --arm-i-understand
"""
from __future__ import annotations

import os
import sys

import config
import crm_execute
import crm_roster
import live_quotes
import rebalance_engine
import roster
import s0_live
import safe_execute
import strategy_target
import version
from safe_execute import MODE_ARMED, MODE_PREVIEW, PURPOSE_REBALANCE
# Reuse, don't reimplement: the read-only pilot already has the strategy-universe accessor
# (ROTATE_OUT vs ALIEN classification) the single-account deploy uses. Importing it opens no
# broker connection.
import s0_live_pilot_run as sp

# The live-trade Gateway port every account on this FA master login shares (same login the
# single-account lane already uses). Contextual only — the connection is opened by s0_live.
LIVE_TRADE_PORT = 4003

# The exact arm token — typed in full, no abbreviation, no default (identical in kind to
# s0_live_deploy.ARM_TOKEN / rebalance_execute.ARM_TOKEN).
ARM_TOKEN = "--arm-i-understand"

# KILL SWITCH — the SAME sentinel s0_live_deploy / s0_live_exec / morning_execute_run honor.
# If the file exists (any content) this run is preview-only for EVERY account.
KILL_SWITCH = r"C:\TradingDesk-Local\AUTOTRADE_DISABLED"
# Register the exact sentinel path with the shared engine so the "KILL_SWITCH sentinel present
# (...)" block reason reads byte-for-byte identically to the single-account lane's message.
safe_execute.register_kill_switch_label(KILL_SWITCH)

# The batch places per-account direct orders (see module docstring). The FA-block whatIf
# optimisation is OWNER-GATED and OFF by design — it would pull in the official ibapi
# (non-PyPI). Left False + stubbed so no non-PyPI dependency is added here.
FA_BLOCK_WHATIF_ENABLED = False


def arm_requested(argv: list[str]) -> bool:
    """True ONLY if the exact arm token is present in argv — the single thing that sets
    armed=True. Mirrors s0_live_deploy.arm_requested."""
    return ARM_TOKEN in argv


def _kill_switch_present() -> bool:
    """True if the AUTOTRADE_DISABLED sentinel exists -> force preview-only for the batch."""
    return os.path.exists(KILL_SWITCH)


# ========================================================================================
# ROSTER + PER-ACCOUNT MODEL VERSION (the execution allow-list, human-blessed).
# ========================================================================================
def resolve_roster_versions(roster_accounts: list[str]) -> dict[str, str]:
    """Map each blessed roster account -> its model version, from the SAME source the roster
    derives from. Prefer the CRM roster ``model`` (v_tradingdesk_roster), fall back to the
    local config.ENROLLMENT map, then to config.STRATEGY_VERSION for anything still unmapped.

    Pure/read-only: SELECTs from the read-only CRM role when configured, else reads config;
    contacts no broker, builds no order."""
    crm_models: dict[str, str] = {}
    if crm_roster.is_configured():
        try:
            for r in crm_roster.fetch_roster(advisor_name=crm_roster.DEFAULT_ADVISOR):
                crm_models[crm_roster.account_identifier(r)] = (
                    r.get("model") or "")
        except crm_roster.CrmRosterUnavailable:
            crm_models = {}   # CRM unreachable -> config fallback below
    out: dict[str, str] = {}
    for a in roster_accounts:
        out[a] = (crm_models.get(a) or config.ENROLLMENT.get(a)
                  or config.STRATEGY_VERSION)
    return out


# ========================================================================================
# FA-BLOCK whatIf — OWNER-GATED, OPTIONAL, STUBBED (no non-PyPI dependency added here).
# ========================================================================================
def fa_block_whatif_preflight(*_args, **_kwargs):
    """DESIGN STUB for the owner-gated FA-block live whatIf margin pre-flight.

    The per-account batch path does not need this: each account is margin-checked by
    safe_execute._margin_preflight_ok (#57) and transmitted via execute_plan's two-phase
    path. A future FA-BLOCK optimisation (one block per symbol across accounts) would margin-
    check the block with a LIVE whatIf through the OFFICIAL ibapi — a non-PyPI dependency the
    desk deliberately does not add here. This stub keeps the seam visible and OFF."""
    raise NotImplementedError(
        "FA-block whatIf pre-flight is an owner-gated branch requiring the official ibapi "
        "(non-PyPI) and is disabled by design (FA_BLOCK_WHATIF_ENABLED=False). The batch uses "
        "per-account direct orders with per-account margin pre-flight instead.")


# ========================================================================================
# PURE request assembly + per-account margin pre-flight line (#57) — unit-testable offline.
# ========================================================================================
def build_batch_requests(plans: list, *, targets, quotes, prices, roster_accounts,
                         summaries=None, armed=False, kill=False) -> list:
    """PURE: turn the per-account sized plans into per-account ExecutionRequests by REUSING
    crm_execute.requests_from_crm_plan (no re-implementation).

    `plans` are rebalance_engine AccountPlans (one per blessed roster account). We wrap them
    in the {"plans": [...]} shape that crm_execute expects and delegate. That helper SKIPS any
    account whose orders are all-zero (already in-band), so the returned requests are exactly
    the OUT-OF-SPEC subset. Every request carries allowed_accounts=the roster (the account
    wall), purpose=REBALANCE, conform=False. Builds and transmits nothing."""
    crm_result = {"plans": list(plans), "blocks": [], "routes": []}
    return crm_execute.requests_from_crm_plan(
        crm_result, targets=targets, quotes=quotes, prices=prices,
        roster=roster_accounts, summaries=summaries or {}, armed=armed, kill=kill)


def margin_preflight_line(request, result) -> tuple[bool, str]:
    """Self-computed per-account MARGIN pre-flight (#57), surfaced for the preview. REUSES
    safe_execute._margin_preflight_ok with the account's own plan/target/summary and the
    total BUY notional read off the previewed leg list. For an unlevered S0/rebalance book
    (exposure <= 1.0) this is (True, "") on any account type; it fails closed only on a
    genuinely levered request that cannot confirm margin capacity. Pure — no broker."""
    total_buy = sum(l.notional for l in (result.legs or []) if l.side == "BUY")
    return safe_execute._margin_preflight_ok(
        request.summary, request.net_liq, total_buy, request.plan, request.target)


def summarize_batch(plans: list, requests: list, results: list) -> dict:
    """Aggregate the batch for the summary line. `plans` is every roster account we sized;
    `requests`/`results` are the OUT-OF-SPEC subset that produced a preview/transmit. Pure."""
    total_sells = 0.0
    total_buys = 0.0
    total_legs = 0
    for res in results:
        for l in (res.legs or []):
            total_legs += 1
            if l.side == "SELL":
                total_sells += l.notional
            else:
                total_buys += l.notional
    return {
        "n_roster": len(plans),
        "n_out_of_spec": len(requests),
        "n_in_spec": len(plans) - len(requests),
        "total_sells": total_sells,
        "total_buys": total_buys,
        "total_legs": total_legs,
    }


# ========================================================================================
# SAFETY BANNER + SUMMARY PRINTING.
# ========================================================================================
def _safety_banner(roster_accounts: list[str], versions: dict[str, str], armed: bool,
                   kill: bool) -> None:
    permit_intent = armed and not kill
    roster_str = "{" + ", ".join(roster_accounts) + "}" if roster_accounts else "{}"
    print("\n" + "#" * 92)
    print(f"# SAFETY STATE   armed={armed}   arm_token={'present' if armed else 'absent'}   "
          f"kill_switch={'PRESENT' if kill else 'absent'}")
    print(f"# roster (execution allow-list, {len(roster_accounts)} account(s)): {roster_str}")
    print(f"#   (account wall: refuses ANY account not on the enrolled roster)")
    print(f"# gateway=Live-Trade port {LIVE_TRADE_PORT} (one FA master login for all accounts)")
    print(f"# per account: purpose=REBALANCE   conform=off   two-phase cash-gated   "
          f"per-account margin pre-flight (#57)")
    if permit_intent:
        print("# *** ARMED INTENT: this run MAY transmit a TWO-PHASE cash-gated rebalance on")
        print("#     EACH out-of-spec roster account IF that account's full gate passes.")
        print("#     Review every account's order list below. ***")
    else:
        print("# PREVIEW: sizes every roster account, prints each order list + margin pre-")
        print("# flight, and transmits NOTHING (not armed, or kill switch present).")
    print("#" * 92)


def main(armed: bool = False, today: object = None) -> int:
    """BATCH REBALANCE executor. PREVIEW by default; transmits per-account ONLY when armed AND
    every per-account gate passes. `today` is accepted for signature parity with the other
    runners; the shared brain always runs to the most recent data date."""
    print("=" * 92)
    print(f"BATCH REBALANCE EXECUTOR (multi-account, roster-scoped) — preview by default, "
          f"per-account two-phase rebalance when armed   [{version.banner()}]")
    print("=" * 92)

    # [1] The human-blessed execution roster (allow-list) + each account's model version.
    print("\n[1] Resolving the human-blessed execution roster (roster.enrolled_roster)...")
    try:
        roster_accounts = roster.enrolled_roster()
    except Exception as exc:  # noqa: BLE001 — never crash before the safety banner
        print(f"    COULD NOT RESOLVE ROSTER: {type(exc).__name__}: {exc}. Nothing "
              f"connected, nothing transmitted.")
        return 2
    if not roster_accounts:
        print("    The enrolled execution roster is EMPTY — nothing to rebalance. Nothing "
              "connected, nothing transmitted.")
        return 0
    versions = resolve_roster_versions(roster_accounts)
    print(f"    roster ({len(roster_accounts)} account(s)):")
    for a in roster_accounts:
        print(f"      {a}  ->  {versions[a]}")

    # [2] Compute a target per DISTINCT version BEFORE connecting (fail fast on stale data;
    # connect nothing on failure).
    distinct_versions = sorted(set(versions.values()))
    print(f"\n[2] Computing target(s) for {len(distinct_versions)} distinct version(s) "
          f"(shared brain; stale-data guarded)...")
    targets: dict = {}
    for v in distinct_versions:
        try:
            targets[v] = strategy_target.current_target(version=v)
        except Exception as exc:  # noqa: BLE001
            print(f"    COULD NOT BUILD TARGET for {v!r}: {exc}. Nothing connected, nothing "
                  f"transmitted.")
            return 2
        t = targets[v]
        print(f"    {t.version:13s} as_of={t.as_of.date()}  price_date={t.price_date.date()}"
              f"  ({len(t.weights)} holdings)")

    kill = _kill_switch_present()
    permit_intent = armed and not kill

    # [3] Safety banner.
    _safety_banner(roster_accounts, versions, armed, kill)

    # [4] Connect ONCE. ARMED intent -> the transmit-capable lane (readonly=False); otherwise
    # the read-only lane. A bare armed connection still transmits nothing on its own — only
    # execute_plan's gated placeOrder does. Whole session in try/finally so it ALWAYS
    # disconnects.
    if permit_intent:
        print(f"\n[3] Connecting ARMED (readonly=False) to the Live-Trade Gateway "
              f"(port {LIVE_TRADE_PORT})...")
        try:
            ib = s0_live.connect_s0_live_armed()
        except Exception as exc:  # noqa: BLE001
            print(f"    could not connect ARMED to the Live-Trade Gateway (port "
                  f"{LIVE_TRADE_PORT}): {type(exc).__name__}: {exc}. Nothing sized, nothing "
                  f"transmitted.")
            return 1
        armed_conn = True
    else:
        print(f"\n[3] Connecting READ-ONLY to the Live-Trade Gateway (port {LIVE_TRADE_PORT}) "
              f"for the preview...")
        try:
            ib = s0_live.connect_s0_live()
        except Exception as exc:  # noqa: BLE001
            print(f"    could not connect READ-ONLY to the Live-Trade Gateway (port "
                  f"{LIVE_TRADE_PORT}): {type(exc).__name__}: {exc}. Nothing sized, nothing "
                  f"transmitted.")
            return 1
        armed_conn = False

    try:
        return run_batch_session(ib, roster_accounts, versions, targets,
                                 armed=armed, armed_conn=armed_conn, kill=kill)
    finally:
        try:
            ib.disconnect()
        except Exception:  # noqa: BLE001
            pass
        print("Session closed.")


def run_batch_session(ib, roster_accounts: list[str], versions: dict[str, str],
                      targets: dict, *, armed: bool, armed_conn: bool, kill: bool) -> int:
    """Read each blessed account off the ONE 4003 login, size it with the frozen engine, and
    drive safe_execute.execute_plan per OUT-OF-SPEC account (PREVIEW or ARMED). Reads the
    broker; every transmit decision lives inside the shared engine's gate."""
    strat_universe = sp._strategy_universe()

    # [4] Read the whole login's account summary + positions ONCE, then filter per account.
    print(f"\n[4] Reading account summary + positions for {len(roster_accounts)} roster "
          f"account(s) off the one FA-master login...")
    all_summary = ib.accountSummary()
    all_positions = ib.positions()

    # Union of every version's target symbols + every held symbol across the roster, for one
    # batched live-quote fetch.
    held_symbols: set[str] = set()
    per_account_state: dict[str, dict] = {}
    for account in roster_accounts:
        summary = s0_live.filter_account_summary(all_summary, account=account)
        net_liq = sp._net_liq(summary) if summary else None
        positions_raw = s0_live.filter_positions(all_positions, account=account)
        positions = {p.contract.symbol: p.position
                     for p in positions_raw if p.position != 0}
        # HELD-ASIDE classification input: the broker's OWN contract.secType ("STK",
        # "BOND", ...) — the authoritative signal, never a guess off the ticker text.
        # The engine uses it to carve instruments the desk never trades out of NetLiq,
        # size the model against the remaining sleeve, and refuse to emit a leg for one.
        # A position whose secType is blank/unrecognised is held aside and flagged, not
        # traded (fail closed).
        sec_types = {p.contract.symbol: getattr(p.contract, "secType", None)
                     for p in positions_raw if p.position != 0}
        held_symbols |= set(positions)
        per_account_state[account] = {
            "summary": summary, "net_liq": net_liq, "positions": positions,
            "sec_types": sec_types}

    target_symbols: set[str] = set()
    for t in targets.values():
        target_symbols |= set(t.weights.index)
    universe = sorted(target_symbols | held_symbols)

    print(f"\n[5] Fetching live quotes for {len(universe)} symbol(s) on port "
          f"{LIVE_TRADE_PORT}...")
    quotes = live_quotes.fetch(ib, universe)
    prices: dict = {}
    for sym in universe:
        q = quotes.get(sym)
        ref = live_quotes.reference_price(q) if q else None
        if ref and ref > 0:
            prices[sym] = ref
        else:
            # Fall back to any version's target close for the symbol.
            for t in targets.values():
                px = t.prices.get(sym)
                if px is not None and float(px) == float(px):
                    prices[sym] = float(px)
                    break

    # [6] Size each roster account with the UNCHANGED engine. Refuse (skip) an account with no
    # readable positive NetLiq — an unfunded/invisible account cannot be acted on.
    print("\n[6] Sizing each roster account with rebalance_engine.plan_account (UNCHANGED "
          "engine)...")
    plans: list = []
    summaries: dict = {}
    skipped: list[str] = []
    for account in roster_accounts:
        st = per_account_state[account]
        v = versions[account]
        target = targets[v]
        net_liq = st["net_liq"]
        if not net_liq or net_liq <= 0:
            print(f"    {account} [{v}]: no readable positive NetLiquidation under the login "
                  f"— SKIPPED (nothing sized, nothing transmitted).")
            skipped.append(account)
            continue
        plan = rebalance_engine.plan_account(
            account, target.version, net_liq, st["positions"], target,
            prices=prices, universe=strat_universe, sec_types=st["sec_types"])
        plans.append(plan)
        summaries[account] = st["summary"]
        print(f"    {account} [{v}]: NetLiq={net_liq:,.2f}  positions={len(st['positions'])}"
              f"  would-trade legs={sum(1 for d in plan.orders.values() if int(d) != 0)}")
        # Held-aside holdings are priced, counted and named here — never folded silently
        # into NAV, and never a leg. The model sized the MANAGED sleeve only.
        if plan.held_aside:
            print(f"      held aside (never traded, outside the model allocation): "
                  f"{len(plan.held_aside)} holding(s) worth "
                  f"{plan.held_aside_value:,.2f}; model applied to the remaining "
                  f"{plan.managed_net_liq:,.2f}")
            for h in plan.held_aside:
                mv = "unpriced" if h.market_value is None else f"{h.market_value:,.2f}"
                print(f"        {h.sec_type:8s} {h.symbol:26s} qty={h.quantity:>14,.2f}"
                      f"  value={mv:>16s}   {h.reason}")
        for reason in plan.blocked_reasons:
            print(f"      ORDERS HELD BACK: {reason}")

    # [7] Map the sized plans -> per-account ExecutionRequests (PURE; REUSES crm_execute). The
    # helper SKIPS in-band accounts, so `requests` is exactly the OUT-OF-SPEC subset. Every
    # request carries allowed_accounts=the roster (the independent account wall).
    requests = build_batch_requests(
        plans, targets=targets, quotes=quotes, prices=prices,
        roster_accounts=roster_accounts, summaries=summaries, armed=armed, kill=kill)
    out_of_spec_accounts = [r.account for r in requests]
    in_spec_accounts = [p.account for p in plans if p.account not in out_of_spec_accounts]

    print(f"\n[7] Out-of-spec roster accounts to rebalance: "
          f"{len(out_of_spec_accounts)} of {len(plans)} sized "
          f"({', '.join(out_of_spec_accounts) or 'none'}).")
    if in_spec_accounts:
        print(f"    In-spec (already conform — nothing to trade): "
              f"{', '.join(in_spec_accounts)}")

    # [8] Drive the shared engine per out-of-spec account. PREVIEW unless the whole run is
    # armed on the transmit lane. Each account prints its own [7]/[8]/[9] order list + gate,
    # then we print the self-computed per-account margin pre-flight (#57).
    mode = MODE_ARMED if armed_conn else MODE_PREVIEW
    results: list = []
    for req in requests:
        print("\n" + "-" * 92)
        print(f"--- BATCH ACCOUNT {req.account} [{req.strategy_version}] "
              f"(purpose=REBALANCE) ---")
        result = safe_execute.execute_plan(req, mode=mode, ib=ib)
        results.append(result)
        mg_ok, mg_reason = margin_preflight_line(req, result)
        print(f"    margin_preflight_ok={mg_ok}   (#57 self-computed per-account pre-flight)"
              + (f"   reason: {mg_reason}" if not mg_ok else ""))
        # A parseable per-account line for the Control Plane's batch preview.
        n_legs = len(result.legs or [])
        n_sell = sum(1 for l in (result.legs or []) if l.side == "SELL")
        n_buy = sum(1 for l in (result.legs or []) if l.side == "BUY")
        print(f"    BATCH-ACCOUNT account={req.account} version={req.strategy_version} "
              f"status={result.status} legs={n_legs} sells={n_sell} buys={n_buy} "
              f"margin_preflight_ok={mg_ok}")

    # [9] Aggregate summary.
    summary = summarize_batch(plans, requests, results)
    print("\n" + "=" * 92)
    print("BATCH SUMMARY")
    print(f"    BATCH-SUMMARY roster={summary['n_roster']} out_of_spec="
          f"{summary['n_out_of_spec']} in_spec={summary['n_in_spec']} skipped={len(skipped)} "
          f"total_legs={summary['total_legs']} total_sells={summary['total_sells']:.2f} "
          f"total_buys={summary['total_buys']:.2f}")

    # Terminal batch verdict: transmitted anything, or preview-only.
    any_transmitted = any(
        r.status in (safe_execute.STATUS_COMPLETE, safe_execute.STATUS_PARTIAL_LOUD)
        for r in results)
    if any_transmitted:
        print("\nBATCH ARMED COMPLETE — review every account's fills above and DISARM the "
              "Gateway when finished.")
    else:
        print("\nBATCH TRANSMISSION BLOCKED — PREVIEW ONLY. Nothing was placed, armed, or "
              "sent on any account.")
    return 0


def cli(argv: list[str] | None = None) -> int:
    """CLI entry: --arm-i-understand sets armed=True (the only thing that arms). No token ->
    preview that transmits nothing."""
    argv = sys.argv[1:] if argv is None else argv
    return main(armed=arm_requested(argv))


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(cli())
