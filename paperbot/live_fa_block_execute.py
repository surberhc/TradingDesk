"""
live_fa_block_execute.py — the ADVISOR-ACCOUNT / FA-GROUP BLOCK execution module.

WHAT THIS IS
------------
The desk's chosen book-wide trading architecture is: ONE advisor (FA master) login, tier FA
GROUPS under it, and BLOCK orders placed against a group (the master fills one block at one
average price and allocates to the group's sub-accounts by the group's stored
ContractsOrShares split). This module is that path, built as a PORT of the PROVEN paper
FA-block flow in rebalance_execute.py — composed from the SAME primitives, not a rewrite:

  * rebalance_engine.build_plan            -> per-account target shares + fa_block routes
  * rebalance_run.resolve_tier_groups      -> version -> live FA group, fail-closed (paper)
  * rebalance_execute.backup_fa_groups     -> MANDATORY groups-XML backup before any write
  * rebalance_execute.compute_group_contracts_or_shares_xml  -> the PURE group-XML mutation
    (shared with the armed writer, so the previewed DIFF is byte-identical to what is written)
  * rebalance_execute.set_group_contracts_or_shares          -> the armed replaceFA write
  * order_router.build_fa_block / place / transmit_guard / already_present  -> the block order
  * safe_execute.armed_session / account_wall_ok / _probe_gateway_readonly / _margin_preflight_ok

PARAMETERIZED TARGET GATEWAY (the config flip)
----------------------------------------------
The target gateway/login is a PARAMETER (`TARGET`, a TargetGateway). It is BUILT + VALIDATED
against the PAPER advisor master DF8922141 (paper gateway 4002, subs DU8922142-146), which
EXISTS today. The live-trade 4003 login is NOT yet an advisor account (tested live 2026-08-05:
2 direct accounts, no master, requestFA times out). When the owner PROVISIONS the live advisor
login, pointing this module at it is a ONE-LINE config flip (set TARGET = LIVE_GATEWAY) — no
logic change. Nothing here requires the live advisor account to build or test.

DEFAULT IS SAFE — PREVIEW BY DEFAULT
------------------------------------
With no flag this runs a PREVIEW: it sizes each enrolled account with the frozen engine,
resolves the tier groups read-only, prints — for every fa_block route — the exact WRITTEN-XML
DIFF that a replaceFA WOULD apply to the live groups (for human review), builds the block
order (build-only), and transmits NOTHING / writes NO FA config. To actually transmit + write
FA config a human must line up ALL of:
  * the exact CLI token  --arm-i-understand  (sets armed=True; never defaulted / auto-set),
  * config.READONLY False AND config.DRY_RUN False (flipped IN-PROCESS only by the arm path,
    restored in a finally — the committed on-disk defaults stay True/True), AND
  * the Gateway physically ARMED (Read-Only API toggle OFF — measured live with the shared
    zero-transmission cancel-a-fabricated-order probe on the target port).
Miss ANY one and the run is a preview that transmits nothing and prints WHY. There is NO
auto-arm and nothing here is scheduled.

GROUP WRITE (load-bearing, per the build plan)
----------------------------------------------
replaceFA overwrites the ENTIRE groups XML. So the armed write is guarded by: (1) a MANDATORY
timestamped backup of the current groups XML BEFORE any write; (2) the pure mutation preserves
every OTHER group untouched; (3) it FAILS CLOSED on blank/missing XML, a missing group, or a
missing ListOfAccts; and (4) the unarmed preview surfaces the exact would-write DIFF for human
review. All four are the same guarantees the paper executor proved.

NEVER whatIf a BLOCK order (it hangs — memory: fa-block-order-allocation). The block is placed
directly with order_router.place(armed=True) and its fills are watched. faMethod="" (an
order-level faMethod is rejected, Err 10226); the GROUP's ContractsOrShares governs allocation.
FA-block whatIf is OWNER-GATED and OFF by design (FA_BLOCK_WHATIF_ENABLED=False stub below).

Run — PREVIEW (default; transmits nothing, writes no FA config):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe live_fa_block_execute.py

Run — ARMED (human-supervised; requires the arm token + a physically-armed gateway):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe live_fa_block_execute.py --arm-i-understand
"""
from __future__ import annotations

import difflib
import sys
from contextlib import nullcontext
from dataclasses import dataclass, field

from ib_async import IB   # noqa: E402

import accounts           # noqa: E402
import config             # noqa: E402
import ledger             # noqa: E402
import live_quotes        # noqa: E402
import order_router       # noqa: E402
import rebalance_execute  # noqa: E402  (backup_fa_groups + the shared group-XML primitives)
import rebalance_run      # noqa: E402  (resolve_tier_groups + build_preview + prices_for)
import strategy_target    # noqa: E402
import version            # noqa: E402
from connections import clientids, gateway_probe, ibkr_paper   # noqa: E402
from gateway_lock import GatewayBusyRefuse, gateway_lock        # noqa: E402
from rebalance_engine import build_plan                         # noqa: E402
from safe_execute import (account_wall_ok, armed_session,       # noqa: E402
                          _margin_preflight_ok)


# The arm token must be typed EXACTLY. No abbreviation, no default. (Same kind as the other
# transmit-capable lanes: rebalance_execute / s0_live_deploy / batch_rebalance_execute.)
ARM_TOKEN = "--arm-i-understand"

# FA-block whatIf is OWNER-GATED and OFF by design — it would pull in the official ibapi
# (non-PyPI) AND whatIf on a block HANGS. Ship WITHOUT it; the substitute is the self-computed
# per-account margin pre-flight over the split (see margin_preflight_over_split) before placing.
FA_BLOCK_WHATIF_ENABLED = False


# ========================================================================================
# TARGET GATEWAY — the ONE parameter that points this module at an advisor login. PAPER today
# (built + validated against DF8922141 / 4002); a future LIVE flip once the owner provisions
# the live advisor account. Everything below reads `TARGET`; nothing hard-codes a port/login.
# ========================================================================================
@dataclass(frozen=True)
class TargetGateway:
    """Where the FA-block executor connects and which accounts/groups it governs.

    host/port          : the gateway to connect (paper 4002 today; live 4003 later).
    clientid_consumer  : a key in connections.clientids.CLIENT_IDS for this lane.
    master_account     : the FA master / advisor login account — NEVER traded, NEVER pinned
                         (its account-update stream hangs the session — memory).
    pin_account        : a CLIENT sub-account to pin the connection to (dodges the master hang).
    enrollment         : {account -> strategy version} for the accounts under this master.
    group_names        : {version -> FA group name} to use directly (e.g. live 'Growth'); when
                         None, groups are resolved from LIVE membership, fail-closed (paper)."""
    name: str
    host: str
    port: int
    clientid_consumer: str
    master_account: str
    pin_account: str
    enrollment: dict
    group_names: dict | None = None


# PAPER advisor master DF8922141 (paper gateway 4002). The build + validate target — it EXISTS.
PAPER_GATEWAY = TargetGateway(
    name="PAPER",
    host=ibkr_paper.HOST,
    port=ibkr_paper.PAPER_PORT,                 # 4002
    clientid_consumer="paperbot_live_fa_block_exec",   # clientId 62
    master_account="DF8922141",
    pin_account=sorted(config.ENROLLMENT)[0],   # lowest-numbered enrolled DU sub (DU8922142)
    enrollment=dict(config.ENROLLMENT),
    group_names=None,                           # resolve by live membership (fail-closed)
)

# LIVE advisor master (port 4003) — INACTIVE PLACEHOLDER. The owner provisions the live advisor
# login (an FA master with >=2 live client accounts), then fills these in and flips TARGET to it.
# Documented, NOT wired: the live 4003 login is NOT an advisor account today (2 direct accounts,
# no master). Uses the reserved clientId "live_fa_block_exec" (63) and the live group name(s).
#
# LIVE_GATEWAY = TargetGateway(
#     name="LIVE",
#     host="127.0.0.1",
#     port=clientids.LIVE_TRADE_PORT,           # 4003
#     clientid_consumer="live_fa_block_exec",   # clientId 63 (reserved)
#     master_account="<live advisor master, provisioned by the owner>",
#     pin_account="<a live client sub under that master>",
#     enrollment={"<Uxxxxxxx>": "Growth", "<Uyyyyyyy>": "Growth"},
#     group_names={"Growth": "Growth"},         # exact live group name; no membership resolve
# )

# THE CONFIG FLIP: point the executor at an advisor login. PAPER until the owner provisions live.
TARGET = PAPER_GATEWAY


# ========================================================================================
# THE GATE — one definition, delegating to order_router.transmit_guard (fails closed).
# ========================================================================================
def arm_requested(argv: list[str]) -> bool:
    """True ONLY if the exact arm token is present. The single thing that authorizes flipping
    the in-process safety flags. A near-miss (--armed / --arm) does NOT arm."""
    return ARM_TOKEN in argv


def gate_state(armed: bool) -> tuple[bool, str]:
    """Whether transmission is permitted RIGHT NOW. Delegates to the canonical
    order_router.transmit_guard so there is ONE definition of the gate: permitted iff
    READONLY=False AND DRY_RUN=False AND armed=True. Fails closed (any reason -> blocked)."""
    return order_router.transmit_guard(armed)


def probe_target_readonly(ib, target: TargetGateway = TARGET, timeout: int = 15) -> bool:
    """True if the OPEN connection's Gateway is READ-ONLY (transmission physically BLOCKED),
    False if WRITE-ENABLED (armed). The port is a PARAMETER (target.port) — reuses the shared
    connections.gateway_probe zero-transmission cancel-a-fabricated-order idiom that
    safe_execute._probe_gateway_readonly wraps for 4003; here the target port is passed through
    so the same probe serves the paper (4002) build and a future live (4003) target. Fails
    CLOSED (no decisive signal -> True/refuse)."""
    return gateway_probe.probe_api_readonly(ib, port=target.port, timeout=timeout)


def transmission_permitted(ib, armed: bool, target: TargetGateway = TARGET) -> tuple[bool, str]:
    """The FULL armed gate for the block path: the code gate (gate_state) AND a physically
    write-enabled gateway (probe not read-only). Only when BOTH pass may a block transmit /
    a replaceFA be written. Fails closed on either. `ib` may be None when only the code gate is
    being evaluated (probe skipped -> treated as still read-only, i.e. blocked)."""
    permit, why = gate_state(armed)
    if not permit:
        return False, why
    if ib is None:
        return False, "no gateway connection to probe (cannot confirm physically armed)"
    if probe_target_readonly(ib, target):
        return False, (f"Gateway is still READ-ONLY on port {target.port} — not physically "
                       f"armed; a human must turn the Read-Only API toggle OFF")
    return True, "ARMED (code gate + gateway write-enabled)"


# ========================================================================================
# GROUP-WRITE PLAN — the PURE would-write DIFF surfaced in the unarmed preview. Reuses the
# shared rebalance_execute.compute_group_contracts_or_shares_xml so the previewed XML is
# byte-identical to what the armed writer (set_group_contracts_or_shares) would replaceFA.
# ========================================================================================
def group_write_plan(current_xml: str, fa_group: str,
                     per_account_split: dict) -> tuple[str, str]:
    """PURE (no broker): return (new_xml, unified_diff_text) for setting `fa_group`'s
    ContractsOrShares to `per_account_split` against `current_xml`. Preserves every OTHER
    group (the shared mutation only touches the named group's subtree) and FAILS CLOSED
    (raises RuntimeError) on blank/missing XML, a missing group, or a missing ListOfAccts —
    exactly the guarantees replaceFA's whole-XML overwrite requires. Writes/transmits NOTHING.

    The diff is line-oriented over pretty-printed XML so a human can eyeball exactly which
    members/amounts/method change and confirm no OTHER group is touched."""
    new_xml = rebalance_execute.compute_group_contracts_or_shares_xml(
        current_xml, fa_group, per_account_split)
    old_pp = _pretty_xml(current_xml)
    new_pp = _pretty_xml(new_xml)
    diff = "\n".join(difflib.unified_diff(
        old_pp.splitlines(), new_pp.splitlines(),
        fromfile=f"groups.xml (current)", tofile=f"groups.xml (after replaceFA: {fa_group})",
        lineterm=""))
    return new_xml, diff


def _pretty_xml(xml: str) -> str:
    """Best-effort pretty-print for a readable diff. Falls back to the raw string if the XML
    cannot be parsed (the diff is human-review sugar, never load-bearing)."""
    import xml.dom.minidom as minidom
    try:
        return minidom.parseString(str(xml or "").strip()).toprettyxml(indent="  ")
    except Exception:
        return str(xml or "")


# ========================================================================================
# ACCOUNT WALL over the WHOLE split + per-account MARGIN pre-flight over the split. Both reuse
# the shared safe_execute primitives; a block spans multiple accounts, so EVERY account in the
# split must clear (fail closed on the first that does not).
# ========================================================================================
def account_wall_over_split(per_account_split: dict,
                            allowed_accounts) -> tuple[bool, str]:
    """The account wall applied to EVERY account in a block's split: each must be in
    `allowed_accounts` (the enrolled roster) or the whole block is refused. This is what stops
    a block from ever touching an account outside the enrolled set. Fails closed on the first
    account not allowed. Pure — reuses safe_execute.account_wall_ok per account."""
    for acct in sorted(per_account_split):
        ok, reason = account_wall_ok(acct, allowed_accounts)
        if not ok:
            return False, (f"block split account {acct} refused: {reason}")
    return True, ""


def margin_preflight_over_split(route, account_inputs: list, targets: dict,
                                summaries: dict | None = None) -> tuple[bool, str]:
    """Self-computed per-account MARGIN pre-flight over a block's split (the whatIf substitute).
    For each account in the split, reuse safe_execute._margin_preflight_ok with that account's
    own sized plan/target/summary. For an unlevered S0/rebalance book (exposure <= 1.0) this is
    (True, "") on any account type; it fails closed only on a genuinely levered request that
    cannot confirm margin capacity. Pure — no broker. Fails closed on the first account refused."""
    summaries = summaries or {}
    ai_by_acct = {a["account"]: a for a in account_inputs}
    for acct in sorted(route.per_account_split):
        ai = ai_by_acct.get(acct)
        if ai is None:
            # No sized input for a split member -> we cannot confirm margin -> fail closed.
            return False, (f"no sized account input for split member {acct} — cannot confirm "
                           f"margin. FAILING CLOSED.")
        target = targets[ai["version"]]
        # An FA block is ONE symbol; the account's share of the block's BUY notional is its
        # split shares * the block limit reference. Sizing the exposure off plan.investable
        # (the strategy's own risk ceiling) is what _margin_preflight_ok uses; total_buy here
        # is informational for its signature (unlevered branch ignores it).
        plan = _plan_for(ai, targets)
        ok, reason = _margin_preflight_ok(
            summaries.get(acct, []), ai["net_liq"], 0.0, plan, target)
        if not ok:
            return False, f"block split account {acct}: {reason}"
    return True, ""


def _plan_for(account_input: dict, targets: dict):
    """Re-derive ONE account's sized AccountPlan from its engine input (for the margin
    pre-flight's investable/NAV read). Pure: rebalance_engine.plan_account, no broker."""
    import rebalance_engine
    return rebalance_engine.plan_account(
        account_input["account"], account_input["version"], account_input["net_liq"],
        account_input["positions"], targets[account_input["version"]],
        prices=account_input.get("prices"))


# ========================================================================================
# TARGETS + CONNECTION (parameterized by TARGET).
# ========================================================================================
def targets_for(target: TargetGateway = TARGET) -> dict:
    """Run the validated shared brain ONCE per distinct enrolled version under this target's
    master (the model per risk tier). Read-only compute, identical code path to the backtest."""
    return {v: strategy_target.current_target(version=v)
            for v in sorted(set(target.enrollment.values()))}


def connect_target(target: TargetGateway = TARGET, *, armed: bool, timeout: int = 15) -> IB:
    """Connect to the target gateway, PINNED to a client sub-account (never the FA master — its
    account-update stream hangs the session). readonly = NOT armed: the preview lane is
    physically read-only; only the armed path passes readonly=False (and even then the gateway's
    own Read-Only toggle + the probe stay the physical wall). Raw ib.connect(account=pin) is the
    only way to pin — ibkr_paper.connect() does not expose the pin, and pinning is what dodges
    the master hang (memory: fa-block-order-allocation)."""
    ib = IB()
    ib.connect(target.host, target.port, clientId=clientids.get(target.clientid_consumer),
               readonly=not armed, timeout=timeout, account=target.pin_account)
    return ib


# ========================================================================================
# THE FA-BLOCK ROUTE LOOP — the e2e body: per fa_block route, surface the group-write DIFF,
# run the account wall + margin pre-flight over the split, then (armed+permitted) write the
# group's ContractsOrShares + place ONE block; else build-only. NEVER whatIf a block.
# ========================================================================================
def execute_fa_block_routes(ib, routes, account_inputs, targets, target: TargetGateway,
                            *, permit: bool, summaries: dict | None = None) -> dict:
    """Drive the fa_block routes. `permit` is the FULL armed gate result (code gate AND gateway
    write-enabled) computed by the caller. When permit is False this is a PREVIEW: it prints
    each route's would-write group DIFF and builds the block (build-only, place(armed=False)),
    writing NO FA config and transmitting nothing. When permit is True it writes each group's
    ContractsOrShares (replaceFA) in lockstep with placing that group's block, one at a time,
    watching fills. Reuses order_router.build_fa_block/place (dedup + price guard live inside).

    Direct routes (a tier with a single account) are OUT OF SCOPE for this module (the FA-block
    path needs >=2 accounts); they are listed and SKIPPED here — the per-account-direct path
    (batch_rebalance_execute.py) owns them.

    Returns a summary dict: {n_blocks, n_direct_skipped, replace_fa_writes, placed_fills, backup}."""
    fa_routes = [r for r in routes if r.route == "fa_block"]
    direct_routes = [r for r in routes if r.route != "fa_block"]
    placed_fills: list[dict] = []
    replace_fa_writes = 0
    backup_path = ""

    if direct_routes:
        print(f"\n    NOTE: {len(direct_routes)} DIRECT route(s) are OUT OF SCOPE for the "
              f"FA-block module and are SKIPPED here (per-account-direct path owns them): "
              f"{', '.join(f'{r.side} {r.symbol} @ {r.account}' for r in direct_routes)}")

    if not fa_routes:
        print("\n    No FA-block routes (a group needs >=2 accounts). Nothing to write/place.")
        return {"n_blocks": 0, "n_direct_skipped": len(direct_routes),
                "replace_fa_writes": 0, "placed_fills": placed_fills, "backup": backup_path}

    as_of = next(iter(targets.values())).as_of

    # ARMED + permitted: MANDATORY backup of the whole groups XML BEFORE any replaceFA write.
    if permit:
        print("\n    ARMED. Backing up live FA groups XML before any replaceFA write...")
        backup_path = rebalance_execute.backup_fa_groups(ib)
        print(f"      backup -> {backup_path}")

    allowed = list(target.enrollment.keys())
    for r in fa_routes:
        print("\n" + "-" * 88)
        print(f"    [fa_block] {r.side} {r.symbol} x{r.total_qty}  group={r.fa_group}  "
              f"faMethod='{r.fa_method}'  split={dict(sorted(r.per_account_split.items()))}")

        # ACCOUNT WALL over the WHOLE split (fail closed).
        wall_ok, wall_reason = account_wall_over_split(r.per_account_split, allowed)
        if not wall_ok:
            print(f"      SKIP — account wall: {wall_reason}")
            continue

        # Per-account MARGIN pre-flight over the split (the whatIf substitute; fail closed).
        mg_ok, mg_reason = margin_preflight_over_split(r, account_inputs, targets, summaries)
        print(f"      margin_preflight_over_split ok={mg_ok}"
              + ("" if mg_ok else f"   reason: {mg_reason}"))
        if not mg_ok:
            print("      SKIP — margin pre-flight refused this block.")
            continue

        # Surface the exact would-write group DIFF (both preview and armed) for human review.
        try:
            current_xml = str(ib.requestFA(1) or "").strip() if ib is not None else ""
            _new_xml, diff = group_write_plan(current_xml, r.fa_group, r.per_account_split)
            print("      WOULD-WRITE group DIFF (replaceFA overwrites the WHOLE groups XML; "
                  "only this group changes):")
            if diff:
                for line in diff.splitlines():
                    print(f"        {line}")
            else:
                print("        (no change — the group already holds this exact split)")
        except RuntimeError as exc:
            print(f"      GROUP-WRITE FAILED CLOSED — {exc}")
            print("      SKIP — refusing to write/place against an unresolved group.")
            continue

        limit = rebalance_execute._fa_block_limit(r, _quotes_cache, account_inputs, targets)
        print(f"      block limit = {limit} (marketable={config.FA_BLOCK_MARKETABLE})")

        if not permit:
            # PREVIEW: build the block object (build-only) and log; write NO FA config.
            try:
                bo = order_router.build_fa_block(
                    r.symbol, r.side, r.total_qty, limit, r.fa_group, r.fa_method, as_of, ib=ib)
            except ValueError as exc:
                print(f"      PRICE GUARD skipped this block: {exc}")
                continue
            order_router.place(ib, [bo], armed=False)
            print("      PREVIEW — block built + logged; NO replaceFA, nothing transmitted.")
            continue

        # ARMED + permitted: write THIS group's ContractsOrShares, THEN place its block.
        print(f"      writing ContractsOrShares via replaceFA: "
              f"{dict(sorted(r.per_account_split.items()))}")
        rebalance_execute.set_group_contracts_or_shares(ib, r.fa_group, r.per_account_split)
        replace_fa_writes += 1
        try:
            bo = order_router.build_fa_block(
                r.symbol, r.side, r.total_qty, limit, r.fa_group, r.fa_method, as_of, ib=ib)
        except ValueError as exc:
            print(f"      PRICE GUARD skipped this block AFTER the group write: {exc}")
            continue
        # NEVER what-if a block (it hangs). Place directly, watch fills. Dedup lives in place().
        res = order_router.place(ib, [bo], armed=True)
        placed_fills.extend(res.get("fills", []))

    return {"n_blocks": len(fa_routes), "n_direct_skipped": len(direct_routes),
            "replace_fa_writes": replace_fa_writes, "placed_fills": placed_fills,
            "backup": backup_path}


# Module-level quote cache the route loop reads for marketable-cap block pricing (reuses
# rebalance_execute._fa_block_limit, which takes a quotes dict). Populated by the connected
# driver before the loop; empty {} in offline tests -> _fa_block_limit falls back to the
# neutral reference price (never a NaN — the HARD price guard still applies at build time).
_quotes_cache: dict = {}


# ========================================================================================
# WHATIF STUB — OWNER-GATED, OFF BY DESIGN (ship WITHOUT whatIf).
# ========================================================================================
def fa_block_whatif_preflight(*_args, **_kwargs):
    """DESIGN STUB for the owner-gated FA-block live whatIf margin pre-flight — DISABLED.

    whatIf on a BLOCK order HANGS (memory: fa-block-order-allocation), and the broker-preview
    array needs the non-PyPI official ibapi. This module ships WITHOUT it: the substitute is the
    self-computed per-account margin pre-flight over the split (margin_preflight_over_split)
    BEFORE placing, then place + watch fills. This stub keeps the seam visible and OFF."""
    raise NotImplementedError(
        "FA-block whatIf pre-flight is an owner-gated branch requiring the official ibapi "
        "(non-PyPI) and is disabled by design (FA_BLOCK_WHATIF_ENABLED=False). whatIf on a "
        "block hangs; the FA-block executor uses per-account margin pre-flight over the split "
        "instead.")


# ========================================================================================
# SAFETY BANNER + CONNECTED DRIVER (preview by default; armed behind the full gate).
# ========================================================================================
def _safety_banner(target: TargetGateway, armed: bool, token_present: bool) -> None:
    permit, why = gate_state(armed)
    print("\n" + "#" * 92)
    print(f"# SAFETY STATE   READONLY={config.READONLY}   DRY_RUN={config.DRY_RUN}   "
          f"armed={armed}   arm_token={'present' if token_present else 'absent'}")
    print(f"# target={target.name}  gateway {target.host}:{target.port}  "
          f"master={target.master_account}  pin={target.pin_account}")
    print(f"# enrolled accounts ({len(target.enrollment)}): "
          f"{', '.join(f'{a}->{v}' for a, v in sorted(target.enrollment.items()))}")
    print(f"# code gate: transmission {'PERMITTED' if permit else 'BLOCKED'} ({why}); "
          f"a physically-armed gateway probe is ALSO required to transmit.")
    if permit:
        print("# *** ARMED: this run CAN write FA config (replaceFA) + transmit a BLOCK order "
              "IF the gateway is physically armed. Review every group DIFF below. ***")
    else:
        print("# PREVIEW: sizes accounts, prints each group's would-write DIFF, builds the "
              "block (build-only), transmits nothing, writes no FA config.")
    print("#" * 92)


def main(argv: list[str] | None = None, target: TargetGateway = TARGET) -> int:
    """Connected driver. PREVIEW by default; ARMED (write FA config + place blocks) only when
    the arm token is present AND config flags are flipped AND the gateway is physically armed.
    Whole session in try/finally so it ALWAYS disconnects."""
    argv = sys.argv[1:] if argv is None else argv
    print("=" * 92)
    print(f"FA-GROUP BLOCK EXECUTOR (advisor-account block path)   [{version.banner()}]")
    print("=" * 92)

    token_present = arm_requested(argv)
    armed = bool(token_present)
    _safety_banner(target, armed, token_present)

    if not token_present:
        print("\nNo arm token -> PREVIEW (read-only; builds + logs + prints DIFFs, transmits "
              f"nothing). To arm, re-run with the exact token: {ARM_TOKEN}")

    # Tier models BEFORE connecting (fail fast on stale data; connect nothing on failure).
    print("\n[1] Computing tier models (one per distinct enrolled version)...")
    try:
        targets = targets_for(target)
    except Exception as exc:   # noqa: BLE001
        print(f"    COULD NOT BUILD TARGETS: {type(exc).__name__}: {exc}. Nothing connected.")
        return 2
    for v, t in targets.items():
        print(f"    {v:13s} as_of={t.as_of.date()}  ({len(t.weights)} holdings)")

    # ARM-GATE: flip READONLY/DRY_RUN False IN-PROCESS behind the token (restored in a finally)
    # via the shared armed_session; hold the gateway lock across the whole armed body. The dry
    # path (no token) never enters armed_session and keeps its committed-safe posture.
    arm_ctx = (armed_session(purpose="live_fa_block_execute",
                             client_id=clientids.get(target.clientid_consumer),
                             gateway_lock_on_busy=None)
               if armed else nullcontext())
    with arm_ctx:
        try:
            with gateway_lock(purpose="live_fa_block_execute",
                              client_id=clientids.get(target.clientid_consumer),
                              on_busy="refuse"):
                return _run_session(target, targets, armed=armed)
        except GatewayBusyRefuse as busy:
            holder = busy.holder or {}
            print(f"\n[2] REFUSING to start — gateway held by {holder.get('purpose')} pid "
                  f"{holder.get('pid')} clientId {holder.get('client_id')}. No connection "
                  f"opened, no block built, nothing transmitted, no replaceFA.")
            return 2


def _run_session(target: TargetGateway, targets: dict, *, armed: bool) -> int:
    """Connect (readonly iff not armed, pinned to a sub), read live state, size with the frozen
    engine, resolve tier groups fail-closed, and drive the fa_block route loop. Held inside the
    gateway lock. The FULL armed gate (code gate AND physical gateway probe) decides permit."""
    global _quotes_cache
    print(f"\n[2] Connecting to {target.name} gateway {target.host}:{target.port} "
          f"(clientId={clientids.get(target.clientid_consumer)}, readonly={not armed}, "
          f"pinned to {target.pin_account})...")
    try:
        ib = connect_target(target, armed=armed)
    except Exception as exc:   # noqa: BLE001
        print(f"    COULD NOT CONNECT: {type(exc).__name__}: {exc}. Nothing sized/transmitted.")
        return 1

    backup_path = ""
    try:
        managed = ib.managedAccounts()
        if target.pin_account not in managed:
            print(f"    ABORT: pin {target.pin_account} not in managed accounts {managed}.")
            return 2

        # [3] Live state per enrolled+funded sub (accounts.discover + ib.positions).
        infos = accounts.discover(ib)
        clients = [i for i in infos if i.enrolled and i.funded and not i.is_master]
        if not clients:
            print("\n    No enrolled + funded client accounts. Nothing to do.")
            return 0

        universe = sorted({s for t in targets.values() for s in t.weights.index})
        print(f"\n[3] Fetching live quotes for {len(universe)} symbol(s)...")
        _quotes_cache = live_quotes.fetch(ib, universe)

        account_inputs: list[dict] = []
        summaries: dict = {}
        all_summary = ib.accountSummary()
        for info in sorted(clients, key=lambda x: x.number):
            positions = {p.contract.symbol: p.position
                         for p in ib.positions(info.number) if p.position != 0}
            tier_prices = targets[info.version].prices
            prices = {}
            for sym in set(tier_prices.index) | set(positions):
                q = _quotes_cache.get(sym)
                ref = live_quotes.reference_price(q) if q else None
                prices[sym] = ref if (ref and ref > 0) else float(
                    tier_prices.get(sym, float("nan")))
            account_inputs.append({
                "account": info.number, "version": info.version,
                "net_liq": info.net_liq, "positions": positions, "prices": prices})
            summaries[info.number] = [r for r in all_summary
                                      if getattr(r, "account", None) == info.number]

        # [4] Resolve version -> FA group. Live membership (fail-closed) for paper; an explicit
        # group_names map (e.g. live 'Growth') when the target supplies one.
        print("\n[4] Resolving version -> FA group (fail-closed)...")
        enrolled_versions = {i.version for i in clients}
        if target.group_names is not None:
            tier_groups = {v: target.group_names[v] for v in enrolled_versions
                           if v in target.group_names}
            missing = enrolled_versions - set(tier_groups)
            if missing:
                print(f"    tier(s) {sorted(missing)} have no group name in the target's "
                      f"group_names map. FAILING CLOSED — no orders built.")
                return 2
        else:
            try:
                tier_groups = rebalance_run.resolve_tier_groups(ib, enrolled_versions)
            except RuntimeError as exc:
                print(f"    {exc}\n    -> No orders built.")
                return 2
        for v, g in sorted(tier_groups.items()):
            print(f"    {v:13s} -> group '{g}'")

        # [5] Size + route (pure engine).
        out = build_plan(account_inputs, targets, tier_groups=tier_groups)
        routes = out["routes"]

        # [6] The FULL armed gate: code gate AND a physically write-enabled gateway.
        permit, why = transmission_permitted(ib if armed else None, armed, target)
        print(f"\n[5] Armed-gate check: permit={permit} ({why})")

        # [7] Drive the fa_block route loop (preview or armed).
        print("\n[6] FA-block routes:")
        result = execute_fa_block_routes(ib, routes, account_inputs, targets, target,
                                         permit=permit, summaries=summaries)
        backup_path = result.get("backup", "")

        _ledger(target, armed, permit, why, routes, result, backup_path)
        if permit:
            print("\nDone. ARMED run complete — review fills + the group writes above, then "
                  "DISARM the gateway.")
        else:
            print("\nDone. PREVIEW — group DIFFs + block objects built + logged; nothing "
                  "transmitted, no FA config written.")
        return 0
    finally:
        try:
            ib.disconnect()
        except Exception:   # noqa: BLE001
            pass
        print("Session closed.")


def _ledger(target, armed, permit, why, routes, result, backup_path) -> None:
    """One audit record per run (preview or armed)."""
    fa_routes = [r for r in routes if r.route == "fa_block"]
    ledger.record_run({
        "mode": "LIVE_FA_BLOCK_ARMED" if permit else "LIVE_FA_BLOCK_PREVIEW",
        "account": target.master_account, "nav": 0.0, "daily_pnl": 0.0,
        "target_as_of": "n/a", "target_weights": {},
        "intents": [{"route": r.route, "side": r.side, "symbol": r.symbol,
                     "qty": r.total_qty, "group": r.fa_group,
                     "split": r.per_account_split} for r in fa_routes],
        "n_intents": len(fa_routes), "n_approved": len(fa_routes),
        "n_transmitted": len(result.get("placed_fills", [])),
        "fills": result.get("placed_fills", []),
        "fa_backup": backup_path,
        "replace_fa_writes": result.get("replace_fa_writes", 0),
        "halted": False, "halt_reason": "",
        "gate": {"target": target.name, "port": target.port,
                 "readonly": config.READONLY, "dry_run": config.DRY_RUN,
                 "armed": armed, "permitted": permit, "why": why},
        **version.stamp(),
    })


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
