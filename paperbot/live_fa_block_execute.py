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
  * safe_execute's PROVEN TWO-PHASE CASH GATE — its constants (PHASE_TERMINAL_TIMEOUT_SEC,
    CASH_SETTLE_SEC, CASH_SAFETY_BUFFER_PCT, _TERMINAL_STATUSES), its realized-cash reader
    (_total_cash_value), its whole-share cash scaler (_scale_buys_to_cash) and its LOUD phase
    reporter (_report_phase) are IMPORTED AND USED HERE, never re-implemented (see
    "TWO-PHASE CASH GATE" below).

TWO-PHASE CASH GATE (conductor #64, applied to the BLOCK lane)
--------------------------------------------------------------
safe_execute.execute_plan is the desk's single source of truth for "sell first, re-read the
REALIZED cash, then buy only what that cash covers". It is PER-ACCOUNT and LEG-based, so the
block lane cannot call it: an FA block is ONE order per group per symbol, allocated across
sub-accounts by the group's stored ContractsOrShares split. This module therefore gives the
BLOCK lane the SAME discipline out of the SAME pieces:

  1. routes are PARTITIONED into SELL blocks and BUY blocks; SELLS ALWAYS GO FIRST. A route
     whose side is neither BUY nor SELL FAILS CLOSED — the whole run is refused, never guessed.
  2. every SELL block is placed and WAITED to terminal state (bounded by
     safe_execute.PHASE_TERMINAL_TIMEOUT_SEC); anything still working at the timeout is
     CANCELLED and reported LOUDLY. Group writes stay in lockstep with their own block, one at
     a time, exactly as before (a block's allocation is the group's live ContractsOrShares).
  3. after CASH_SETTLE_SEC, accountSummary is re-read and safe_execute._total_cash_value gives
     each SUB-ACCOUNT its REALIZED cash. Missing/unparseable -> that account contributes ZERO
     to the buy phase (FAIL CLOSED) and that fact is reported LOUDLY.
  4. the BUY blocks are RE-SIZED to that realized cash with CASH_SAFETY_BUFFER_PCT applied
     exactly as the per-account path does. This is the only genuinely new logic: BOTH the
     block quantity AND the group's ContractsOrShares split are recomputed so each sub-account
     buys only what its OWN realized cash covers. Cash is NEVER netted across accounts and
     quantities are NEVER rounded up (whole-share floor + greedy trim, via _scale_buys_to_cash).
  5. the re-sized BUY blocks are placed.
  6. any account that RAISED proceeds but ended the run with cash left UNINVESTED is reported
     loudly and machine-readably (account, dollars, reason) — a sold-but-not-reinvested account
     is never left silently sitting in cash.

PREVIEW shows the whole phasing (what would be sold first, that buys would be re-sized to
realized cash) with ZERO broker interaction: no accountSummary read, no cash gate, no writes.

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
from dataclasses import dataclass, field, replace as dc_replace
from types import SimpleNamespace

from ib_async import IB   # noqa: E402

import accounts           # noqa: E402
import config             # noqa: E402
import ledger             # noqa: E402
import live_quotes        # noqa: E402
import order_router       # noqa: E402
import rebalance_execute  # noqa: E402  (backup_fa_groups + the shared group-XML primitives)
import rebalance_run      # noqa: E402  (resolve_tier_groups + build_preview + prices_for)
import recon_report       # noqa: E402  (_portfolio_values — the ONE held-aside pricing reader)
import s0_live            # noqa: E402  (filter_account_summary — the per-account summary filter)
import strategy_target    # noqa: E402
import version            # noqa: E402
from connections import clientids, gateway_probe, ibkr_paper   # noqa: E402
from gateway_lock import GatewayBusyRefuse, gateway_lock        # noqa: E402
from rebalance_engine import build_plan                         # noqa: E402
# THE TWO-PHASE CASH GATE IS safe_execute's — imported, never re-implemented. The
# underscore-prefixed names are private-by-convention module internals of the desk's ONE proven
# transmit chokepoint; importing them (rather than copying their bodies here) is deliberate, so
# the block lane and the per-account lane can NEVER drift on the cash discipline. Duplicating a
# cash reader / terminal-status set / safety buffer for this lane is exactly the failure mode
# conductor #64 exists to prevent. _run_id rides in on the SAME reasoning: the PER-RUN orderRef
# stamp (v0.34.0) must be ONE convention desk-wide, so this lane reuses safe_execute's generator
# verbatim rather than inventing a second wall-clock format.
from safe_execute import (CASH_SAFETY_BUFFER_PCT, CASH_SETTLE_SEC,        # noqa: E402
                          PHASE_TERMINAL_TIMEOUT_SEC, account_wall_ok, armed_session,
                          _TERMINAL_STATUSES, _margin_preflight_ok, _report_phase,
                          _run_id, _scale_buys_to_cash, _total_cash_value)


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
    pre-flight's investable/NAV read). Pure: rebalance_engine.plan_account, no broker.

    `sec_types`/`values` are threaded through so this re-derivation is BYTE-IDENTICAL to the
    plan build_plan already produced. Without them a bond-holding account would be re-planned
    against its FULL NetLiq here while the routed block was sized against the managed sleeve —
    two different investables for the same account inside one run."""
    import rebalance_engine
    return rebalance_engine.plan_account(
        account_input["account"], account_input["version"], account_input["net_liq"],
        account_input["positions"], targets[account_input["version"]],
        prices=account_input.get("prices"),
        sec_types=account_input.get("sec_types"), values=account_input.get("values"),
        # Carry the execution lane's live-quote-only rule, or this re-derivation would be
        # sized off the stored close while the routed block was sized off live quotes.
        strict_prices=bool(account_input.get("strict_prices", False)))


# ========================================================================================
# PATTERN-DAY-TRADER (PDT) pre-flight over the split.
#
# WHY THIS EXISTS. A US margin account under $25,000 that IBKR has flagged a PATTERN DAY
# TRADER rejects ORDINARY orders — not just orders that would themselves create a day trade.
# EVIDENCE (2026-07-28): account U5721712 bounced a plain BUY of 1 USFR (~$50) with no
# offsetting sell. So the question this gate asks is NOT "does this run create a day trade"
# (an order-shape analyzer would be the wrong tool); it is "has the broker already flagged
# this account". IBKR answers that directly with the DayTradesRemaining accountSummary tag.
#
# SCALE. 113 of the 304 roster accounts are margin under $25,000 (53 in Andrew's book), none
# flagged no_trade. Today's only mitigation is that S0 trades ONE hand-picked PDT-clear
# account; that evaporates the moment the book-wide block rail (owner decision D1) runs.
#
# ZERO NEW BROKER READS. ib_async requests DayTradesRemaining by DEFAULT, so the tag is
# already sitting in the per-account `summaries` this lane holds. VERIFIED READ-ONLY against
# the live-trade gateway (port 4003, 2026-08-25): the tag comes back PER SUB-ACCOUNT — not
# master-only, and NOT on the aggregate 'All' scope — U14438624 = '-1' (unrestricted),
# U5721712 = '0' (the account that actually bounced).
#
# SEMANTICS: -1 = unlimited / not PDT-restricted; 0 = none left; n>0 = n day trades remain.
# MISSING OR UNPARSEABLE FAILS CLOSED (this lane's stance everywhere else — the unknown-side
# refusal, the unreadable-cash zero — NOT _buying_power_ok's fail-open).
# ========================================================================================
PDT_TAG = "DayTradesRemaining"


def day_trades_remaining(summary_rows) -> int | None:
    """PURE: IBKR's DayTradesRemaining for ONE account, read off the accountSummary rows this
    lane already holds. Accepts either the row-object list ib.accountSummary() returns or the
    {tag: value} dict shape s0_live.filter_account_summary can produce.

    Returns the integer, or None when the tag is absent, blank or unparseable — the CALLER
    treats None as REFUSE (fail closed). Never raises."""
    raw = None
    if isinstance(summary_rows, dict):
        raw = summary_rows.get(PDT_TAG)
    else:
        for row in (summary_rows or []):
            if str(getattr(row, "tag", "")) == PDT_TAG:
                raw = getattr(row, "value", None)
                break
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def pdt_account_ok(summary_rows) -> tuple[bool, str]:
    """PURE: the PDT verdict for ONE account. (True, "") when the broker says the account is
    not day-trade-restricted (-1 unlimited, or a positive count remaining); (False, reason)
    when it says 0 — and (False, reason) when the tag cannot be read at all (FAIL CLOSED: an
    account we cannot clear is an account we do not trade)."""
    n = day_trades_remaining(summary_rows)
    if n is None:
        return False, (f"{PDT_TAG} is absent/unparseable in this account's accountSummary — "
                       f"cannot confirm the account is not pattern-day-trader restricted. "
                       f"FAILING CLOSED.")
    if n == -1:
        return True, ""
    if n > 0:
        return True, ""
    return False, (f"{PDT_TAG}={n} — IBKR has flagged this account pattern-day-trader "
                   f"restricted with no day trades left. Such an account rejects ORDINARY "
                   f"orders regardless of order shape (2026-07-28, U5721712: a plain BUY of "
                   f"1 USFR was bounced). REFUSING to include it.")


def pdt_blocked_in_split(route, summaries: dict | None = None) -> list[dict]:
    """PURE: every account in a block's split the PDT gate refuses, as
    [{account, shares, day_trades_remaining, reason}] sorted by account. Empty list = the
    whole split clears. No broker — reads only the summaries the lane already holds."""
    summaries = summaries or {}
    blocked: list[dict] = []
    for acct in sorted(route.per_account_split):
        rows = summaries.get(acct, [])
        ok, reason = pdt_account_ok(rows)
        if ok:
            continue
        blocked.append({"account": acct,
                        "shares": int(route.per_account_split.get(acct, 0)),
                        "day_trades_remaining": day_trades_remaining(rows),
                        "reason": reason})
    return blocked


def pdt_preflight_over_split(route, summaries: dict | None = None) -> tuple[bool, str]:
    """PURE per-account PDT pre-flight over a block's split — the BLOCK-level verdict.

    A PDT-blocked account is DROPPED from the split (see pdt_drop_blocked_from_split), NOT
    treated as a veto: one $957 restricted account must never refuse a 60-account rebalance.
    So this returns (False, reason) ONLY when EVERY account in the split is blocked and the
    split would empty — there is then no block left to place. Otherwise (True, "").

    Sibling of margin_preflight_over_split; same shape, same fail-closed instinct."""
    blocked = pdt_blocked_in_split(route, summaries)
    if not blocked:
        return True, ""
    if len(blocked) < len(route.per_account_split):
        return True, ""
    detail = "; ".join(f"{b['account']} ({PDT_TAG}={b['day_trades_remaining']!r})"
                       for b in blocked)
    return False, (f"EVERY account in this block's split is pattern-day-trader blocked "
                   f"[{detail}] — the split empties, so there is no block to place. "
                   f"REFUSING the block.")


def pdt_drop_blocked_from_split(route, summaries: dict | None = None) -> tuple:
    """PURE: re-issue `route` with every PDT-blocked account REMOVED from per_account_split
    and total_qty recomputed off the survivors (dataclasses.replace, exactly as
    resize_buy_routes_to_realized_cash does — the engine's original route is untouched).

    Returns (route, dropped) where `dropped` is pdt_blocked_in_split's list. The CALLER MUST
    surface `dropped` loudly, in the printed run report AND in the ledger record: an account
    that quietly did not get rebalanced is precisely the failure mode that let an $826k
    rollover sit idle 131 days. A silent drop is a bug.

    Callers must run pdt_preflight_over_split FIRST — this function does not defend against
    the split emptying (it would return a zero-quantity route)."""
    dropped = pdt_blocked_in_split(route, summaries)
    if not dropped:
        return route, []
    blocked_accts = {d["account"] for d in dropped}
    split = {a: int(q) for a, q in route.per_account_split.items()
             if a not in blocked_accts}
    return (dc_replace(route, per_account_split=dict(sorted(split.items())),
                       total_qty=int(sum(split.values()))),
            dropped)


# ========================================================================================
# ENGINE INPUTS — the ONE place this lane turns broker state into rebalance_engine input.
# ========================================================================================
def build_account_inputs(ib, clients, targets, quotes: dict | None = None) -> tuple:
    """Read each enrolled+funded client sub off the ONE FA-master login and build BOTH the
    engine's per-account input dicts AND the per-account accountSummary rows every gate reads.

    Returns (account_inputs, summaries).

    HELD-ASIDE (owner decision D6): each input carries `sec_types` (the broker's OWN
    contract.secType per held symbol) and `values` (broker-reported market values). Without
    them rebalance_engine.plan_account's carve_out short-circuits to "nothing held aside" and
    a bond-holding account is sized against its FULL NetLiq — the bond silently INSIDE the
    target allocation, when D6 puts it outside and applies the model to the remaining managed
    sleeve as its own 100%. The per-account batch rail (batch_rebalance_execute.py) has always
    built sec_types this way; this lane did not, which is the defect this function closes.

    `values` reuses recon_report's ONE portfolio reader; any failure there degrades to {} and
    the plan then reports the holding UNPRICED and withholds orders (fail closed).

    `quotes` defaults to the module quote cache the connected driver populated.
    Broker reads only — nothing is built, armed or transmitted here."""
    quotes = _quotes_cache if quotes is None else quotes
    account_inputs: list[dict] = []
    summaries: dict = {}
    all_summary = ib.accountSummary()
    for info in sorted(clients, key=lambda x: x.number):
        # Keep the RAW position objects: the carve-out needs each contract's secType, which a
        # {symbol: position} comprehension throws away.
        positions_raw = [p for p in ib.positions(info.number) if p.position != 0]
        positions = {p.contract.symbol: p.position for p in positions_raw}
        sec_types = {p.contract.symbol: getattr(p.contract, "secType", None)
                     for p in positions_raw}
        values = recon_report._portfolio_values(ib, info.number)
        tier_prices = targets[info.version].prices
        # LIVE QUOTE ONLY (owner decision, v0.42.0): the tier's stored daily close is no
        # longer substituted for a quote IBKR would not give. A symbol with no live quote
        # does not trade on this lane and is named in the tally below.
        prices, unquoted = live_quotes.execution_prices(
            quotes, set(tier_prices.index) | set(positions))
        if unquoted:
            print(f"    {info.number}: no live IBKR quote for {len(unquoted)} symbol(s): "
                  f"{', '.join(unquoted)} — they will NOT be traded.")
        account_inputs.append({
            "account": info.number, "version": info.version,
            "net_liq": info.net_liq, "positions": positions, "prices": prices,
            "sec_types": sec_types, "values": values,
            # EXECUTION LANE: `prices` above is the ONLY price source for sizing.
            "strict_prices": True})
        summaries[info.number] = [r for r in all_summary
                                  if getattr(r, "account", None) == info.number]
    return account_inputs, summaries


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
# THE CASH GATE, BLOCK-SHAPED — the only genuinely NEW logic in this module. safe_execute's
# per-account gate scales a LEG list to one account's realized cash; a BLOCK is one order
# shared by many sub-accounts, so the split itself must be re-sized. Both functions below are
# PURE (no broker, no config, no mutation of their inputs) so they are testable offline.
# ========================================================================================
def read_realized_cash(ib, accounts_) -> dict:
    """Re-read REALIZED cash (TotalCashValue) per SUB-ACCOUNT off a FRESH accountSummary.

    Composed from the two shared readers rather than a new one: s0_live.filter_account_summary
    narrows the master's blended summary to ONE account, then safe_execute._total_cash_value
    parses it — the SAME ground truth the per-account lane's between-phases buy sizing uses,
    never the plan's expected sale proceeds.

    Returns {account: float | None}. None means missing/unparseable, and the caller then FAILS
    CLOSED (that account contributes ZERO to the buy phase). A whole-summary read failure fails
    every account closed the same way."""
    try:
        all_summary = ib.accountSummary()
    except Exception as exc:   # noqa: BLE001
        print(f"      !! accountSummary read FAILED ({type(exc).__name__}: {exc}) — EVERY "
              f"account FAILS CLOSED and contributes ZERO to the buy phase.")
        return {a: None for a in sorted(accounts_)}
    return {acct: _total_cash_value(s0_live.filter_account_summary(all_summary, account=acct))
            for acct in sorted(accounts_)}


def resize_buy_routes_to_realized_cash(buy_routes_with_limits, cash_by_account, *,
                                       buffer_pct: float = CASH_SAFETY_BUFFER_PCT):
    """PURE: re-size BUY blocks so each SUB-ACCOUNT buys only what its OWN realized cash covers.

    `buy_routes_with_limits` is [(RoutePlan, block_limit)]; `cash_by_account` is
    {account: realized cash | None} (None -> FAIL CLOSED -> that account is treated as 0.0).

    Each account's slice of every buy block becomes one pseudo-leg, and the account's OWN legs
    are scaled by safe_execute._scale_buys_to_cash — the SAME whole-share floor + greedy trim
    the per-account lane uses, with the SAME CASH_SAFETY_BUFFER_PCT. Quantities are only ever
    reduced (never rounded up), and each account is scaled against ITS OWN cash ALONE, so one
    account's proceeds can NEVER fund another's buy.

    Both the group's ContractsOrShares split AND the block's total quantity are recomputed (the
    block is re-issued via dataclasses.replace, leaving the engine's original route untouched).

    Returns (resized_with_limits, dropped_with_limits, detail):
      resized_with_limits : [(RoutePlan, limit)] whose re-sized total_qty is > 0
      dropped_with_limits : [(RoutePlan, limit)] that scaled all the way to zero (no cash)
      detail              : {account: {realized_cash, cash_read_ok, budget, planned_notional,
                                       sized_notional, min_buy_limit, had_buy_legs,
                                       adjustments}}"""
    legs_by_acct: dict = {}
    for idx, (r, limit) in enumerate(buy_routes_with_limits):
        lim = float(limit) if limit is not None else float("nan")
        if not (lim == lim and lim > 0):
            # UNPRICEABLE block (NaN/<=0 reference). order_router's HARD price guard would
            # reject it at build time anyway; dropping it here keeps the cash gate from ever
            # sizing against a bad price. Fails CLOSED: no legs, so the block scales to zero.
            print(f"      !! UNPRICEABLE buy block {r.symbol} group={r.fa_group} "
                  f"(limit={limit!r}) — DROPPED before cash sizing (fail closed).")
            continue
        for acct, qty in sorted(r.per_account_split.items()):
            q = int(qty)
            if q <= 0:
                continue
            # `source` carries the route index so the scaled result maps back to its block.
            legs_by_acct.setdefault(acct, []).append(SimpleNamespace(
                symbol=r.symbol, side="BUY", qty=q, limit=lim, notional=q * lim,
                source=f"fa_block#{idx}"))

    detail: dict = {}
    new_qty: dict = {}
    for acct in sorted(set(legs_by_acct) | set(cash_by_account)):
        raw = cash_by_account.get(acct)
        cash_ok = raw is not None
        cash = float(raw) if cash_ok else 0.0          # FAIL CLOSED: unreadable cash buys NOTHING
        legs = legs_by_acct.get(acct, [])
        budget = max(0.0, cash * (1.0 - buffer_pct))
        scaled, adjustments = _scale_buys_to_cash(legs, cash, buffer_pct=buffer_pct)
        sized_notional = sum(w.notional for w in scaled)
        # HARD per-account invariant — the load-bearing anti-negative check, per account and
        # NEVER netted across accounts (mirrors safe_execute.execute_plan's assert).
        assert sized_notional <= budget + 1e-6, (
            f"block cash-gate invariant violated for {acct}: buy notional {sized_notional} > "
            f"budget {budget}")
        for w in scaled:
            new_qty[(int(str(w.source).split("#")[1]), acct)] = int(w.qty)
        detail[acct] = {
            "realized_cash": (float(raw) if cash_ok else None),
            "cash_read_ok": cash_ok,
            "budget": budget,
            "planned_notional": sum(l.notional for l in legs),
            "sized_notional": sized_notional,
            "min_buy_limit": (min(l.limit for l in legs) if legs else None),
            "had_buy_legs": bool(legs),
            "adjustments": adjustments,
        }

    resized, dropped = [], []
    for idx, (r, limit) in enumerate(buy_routes_with_limits):
        split = {a: q for (i, a), q in new_qty.items() if i == idx and q > 0}
        total = sum(split.values())
        if total <= 0:
            dropped.append((r, limit))
            continue
        resized.append((dc_replace(r, per_account_split=dict(sorted(split.items())),
                                   total_qty=int(total)), limit))
    return resized, dropped, detail


def uninvested_proceeds_report(proceeds_by_account: dict, cash_detail: dict,
                               placed_notional_by_account: dict) -> list[dict]:
    """PURE: the OPERATOR EXCEPTION REPORT — every account that RAISED proceeds in the sell
    phase but ended the run with that money sitting in cash. A sold-but-not-reinvested account
    must never be silently left behind.

    `proceeds_by_account` is what the SELL phase ACTUALLY raised per account (split shares x the
    block limit x the block's fill fraction) — accounts with no realized proceeds are not an
    exception and are not reported.

    The shortfall is measured against the account's own INTENDED buy notional, NOT against its
    TotalCashValue: total cash legitimately includes a pre-existing cash sleeve, so
    budget-minus-placed would cry wolf on every run. Fires when:
      * CASH_UNREADABLE            — realized cash could not be read, so the account contributed
                                     ZERO to the buy phase (fail closed) and its whole intended
                                     buy went undone;
      * NO_BUY_ROUTE               — it raised proceeds and this run had no buy block for it;
      * BUY_SHORT_OF_REALIZED_CASH — its placed buys came up short of its intended buys by at
                                     least ONE share of its cheapest buy leg (re-sized down,
                                     refused, dropped, or unfilled). Sub-one-share whole-share
                                     remainder is irreducible and is NOT an exception.

    Returns [{account, dollars_uninvested, proceeds_raised, realized_cash, planned_notional,
    placed_notional, reason, detail}] — machine-readable, sorted by account."""
    report: list[dict] = []
    for acct in sorted(proceeds_by_account):
        proceeds = float(proceeds_by_account.get(acct, 0.0) or 0.0)
        if proceeds <= 0.0:
            continue
        d = cash_detail.get(acct) or {}
        planned = float(d.get("planned_notional", 0.0) or 0.0)
        placed = float(placed_notional_by_account.get(acct, 0.0) or 0.0)
        shortfall = max(0.0, planned - placed)
        row = {"account": acct, "dollars_uninvested": shortfall, "proceeds_raised": proceeds,
               "realized_cash": d.get("realized_cash"), "planned_notional": planned,
               "placed_notional": placed}
        if not d.get("cash_read_ok", False):
            row["reason"] = "CASH_UNREADABLE"
            row["detail"] = ("realized TotalCashValue could not be read — this account "
                             "contributed ZERO to the buy phase (FAIL CLOSED); its sale "
                             "proceeds are sitting UNINVESTED.")
            report.append(row)
            continue
        if not d.get("had_buy_legs", False):
            row["dollars_uninvested"] = proceeds
            row["reason"] = "NO_BUY_ROUTE"
            row["detail"] = ("account raised proceeds but this run had NO buy block for it — "
                             "the whole realized amount is sitting in cash.")
            report.append(row)
            continue
        floor_price = float(d.get("min_buy_limit") or 0.0)
        if shortfall > 0.0 and shortfall >= floor_price:
            row["reason"] = "BUY_SHORT_OF_REALIZED_CASH"
            row["detail"] = ("placed buys came up short of this account's INTENDED buys by at "
                             "least one share of its cheapest leg (re-sized down / refused / "
                             "dropped / unfilled) — proceeds left in cash.")
            report.append(row)
    return report


# ========================================================================================
# THE FA-BLOCK ROUTE LOOP — the e2e body: per fa_block route, surface the group-write DIFF,
# run the account wall + margin pre-flight over the split, then (armed+permitted) write the
# group's ContractsOrShares + place ONE block; else build-only. NEVER whatIf a block.
# ========================================================================================
def _cancel_working_block(ib, order_ref: str) -> int:
    """Cancel any order still WORKING under this block's orderRef at the phase timeout — the
    block-lane equivalent of safe_execute._transmit_phase's "cancel + report LOUDLY" straggler
    give-up. A sell still working is cash NOT raised, so it must never be left alive while the
    buy phase sizes against realized cash. Fully fail-soft (a broker read/cancel error is
    swallowed and reported by the caller's LOUD phase report). Returns the number cancelled."""
    trades = None
    for reader in ("openTrades", "reqAllOpenOrders"):
        fn = getattr(ib, reader, None)
        if fn is None:
            continue
        try:
            trades = fn() or []
        except Exception:   # noqa: BLE001
            trades = None
            continue
        if trades:
            break
    cancelled = 0
    for t in (trades or []):
        o = getattr(t, "order", None) or t
        if getattr(o, "orderRef", None) != order_ref:
            continue
        try:
            ib.cancelOrder(o)
            cancelled += 1
        except Exception:   # noqa: BLE001
            pass
    if cancelled:
        print(f"      !! CANCELLED {cancelled} still-working order(s) for ref={order_ref} at "
              f"the phase timeout.")
    return cancelled


def _execute_one_route(ib, r, account_inputs, targets, allowed, as_of, limit, *,
                       permit: bool, summaries: dict | None, phase_label: str,
                       run_id: str | None = None,
                       adaptive_priority: str | None = None) -> dict:
    """Run ONE fa_block route: account wall over the split, margin pre-flight over the split,
    the PDT pre-flight over the split (blocked accounts DROPPED, block refused only if the split
    empties — both BEFORE the group diff, so a refused account never causes a replaceFA write),
    the would-write group DIFF, then (armed+permitted) the lockstep replaceFA + block place, or
    (preview) build-only. This is the UNCHANGED per-route body — every gate in the same order,
    with the same fail-closed `SKIP` semantics — lifted into a function so the phase runner can
    call it for the SELL blocks first and the BUY blocks after.

    `run_id` is the run's PER-RUN orderRef stamp (v0.34.0) — the SAME value for the SELL phase
    and the BUY phase of one run, so within-run dedup still blocks a double-submit of one leg
    while a LATER run of the same group/symbol/side is correctly seen as new work.

    Returns one result dict in safe_execute._report_phase's per-leg shape (symbol/side/
    requested/filled/status/reprices/skipped/reason) plus block extras (group, split, limit,
    replace_fa, fills, order_ref)."""
    res = {"symbol": r.symbol, "side": r.side, "requested": float(r.total_qty), "filled": 0.0,
           "status": "PREVIEW" if not permit else "NOT_PLACED", "reprices": 0, "skipped": False,
           "reason": "", "group": r.fa_group, "split": dict(sorted(r.per_account_split.items())),
           "limit": None, "replace_fa": 0, "fills": [], "order_ref": "", "phase": phase_label,
           "pdt_dropped": []}

    print("\n" + "-" * 88)
    print(f"    [{phase_label}][fa_block] {r.side} {r.symbol} x{r.total_qty}  "
          f"group={r.fa_group}  faMethod='{r.fa_method}'  "
          f"split={dict(sorted(r.per_account_split.items()))}")

    def _skip(status: str, reason: str) -> dict:
        res["skipped"], res["status"], res["reason"] = True, status, reason
        return res

    # ACCOUNT WALL over the WHOLE split (fail closed).
    wall_ok, wall_reason = account_wall_over_split(r.per_account_split, allowed)
    if not wall_ok:
        print(f"      SKIP — account wall: {wall_reason}")
        return _skip("SKIPPED_ACCOUNT_WALL", wall_reason)

    # Per-account MARGIN pre-flight over the split (the whatIf substitute; fail closed).
    mg_ok, mg_reason = margin_preflight_over_split(r, account_inputs, targets, summaries)
    print(f"      margin_preflight_over_split ok={mg_ok}"
          + ("" if mg_ok else f"   reason: {mg_reason}"))
    if not mg_ok:
        print("      SKIP — margin pre-flight refused this block.")
        return _skip("SKIPPED_MARGIN", mg_reason)

    # PATTERN-DAY-TRADER gate over the split. MUST run BEFORE the group-XML diff and before
    # set_group_contracts_or_shares, so a refused account can never cause a replaceFA write.
    # A blocked account is DROPPED (not a veto); the block is refused only if the split empties.
    pdt_ok, pdt_reason = pdt_preflight_over_split(r, summaries)
    if not pdt_ok:
        print(f"      SKIP — PDT pre-flight refused this block: {pdt_reason}")
        return _skip("SKIPPED_PDT", pdt_reason)
    r, pdt_dropped = pdt_drop_blocked_from_split(r, summaries)
    res["pdt_dropped"] = pdt_dropped
    if pdt_dropped:
        # LOUD by design. A silently omitted account is the failure mode that let an $826k
        # rollover sit idle 131 days — never let a drop pass without saying so.
        print(f"      !! PDT DROP (LOUD) — {len(pdt_dropped)} account(s) are PATTERN-DAY-TRADER "
              f"blocked and are NOT rebalanced by this block. THEY NEED HUMAN REVIEW:")
        for d in pdt_dropped:
            print(f"        -> account={d['account']} shares_dropped={d['shares']} "
                  f"{PDT_TAG}={d['day_trades_remaining']!r} :: {d['reason']}")
        print(f"      block RE-SIZED after the PDT drop: total_qty "
              f"{int(res['requested'])} -> {r.total_qty}  "
              f"split={dict(sorted(r.per_account_split.items()))}")
        res["requested"] = float(r.total_qty)
        res["split"] = dict(sorted(r.per_account_split.items()))
    else:
        print("      pdt_preflight_over_split ok=True (no account PDT-blocked)")

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
        return _skip("SKIPPED_GROUP_WRITE", str(exc))

    if limit is None:
        limit = rebalance_execute._fa_block_limit(r, _quotes_cache, account_inputs, targets)
    res["limit"] = limit
    print(f"      block limit = {limit} (marketable={config.FA_BLOCK_MARKETABLE})")

    if not permit:
        # PREVIEW: build the block object (build-only) and log; write NO FA config.
        try:
            bo = order_router.build_fa_block(
                r.symbol, r.side, r.total_qty, limit, r.fa_group, r.fa_method, as_of, ib=ib,
                run_id=run_id, adaptive_priority=adaptive_priority)
        except ValueError as exc:
            print(f"      PRICE GUARD skipped this block: {exc}")
            return _skip("SKIPPED_PRICE_GUARD", str(exc))
        res["order_ref"] = bo.order_ref
        order_router.place(ib, [bo], armed=False)
        print("      PREVIEW — block built + logged; NO replaceFA, nothing transmitted.")
        return res

    # ARMED + permitted: write THIS group's ContractsOrShares, THEN place its block.
    print(f"      writing ContractsOrShares via replaceFA: "
          f"{dict(sorted(r.per_account_split.items()))}")
    rebalance_execute.set_group_contracts_or_shares(ib, r.fa_group, r.per_account_split)
    res["replace_fa"] = 1
    try:
        bo = order_router.build_fa_block(
            r.symbol, r.side, r.total_qty, limit, r.fa_group, r.fa_method, as_of, ib=ib,
            run_id=run_id, adaptive_priority=adaptive_priority)
    except ValueError as exc:
        print(f"      PRICE GUARD skipped this block AFTER the group write: {exc}")
        return _skip("SKIPPED_PRICE_GUARD_AFTER_WRITE", str(exc))
    res["order_ref"] = bo.order_ref
    # NEVER what-if a block (it hangs). Place directly, watch fills. Dedup lives in place().
    # The fill watch is BOUNDED by safe_execute.PHASE_TERMINAL_TIMEOUT_SEC — the SAME bound the
    # per-account phase discipline uses, so a phase always terminates and never blocks the wire.
    placed = order_router.place(ib, [bo], armed=True,
                                fill_timeout=int(PHASE_TERMINAL_TIMEOUT_SEC))
    fills = list(placed.get("fills", []) or [])
    res["fills"] = fills
    res["filled"] = sum(float(f.get("filled", 0.0) or 0.0) for f in fills)
    if placed.get("skipped"):
        res["skipped"] = True
        res["status"] = "SKIPPED_WORKING"
        res["reason"] = "an identical WORKING block is already open — not double-submitting"
        return res
    if not fills:
        res["status"] = "NO_FILL_RECORD"
        res["reason"] = "block transmitted but no order status came back — LOUD, needs review"
        return res
    res["status"] = str(fills[0].get("status", "") or "")
    if res["status"] not in _TERMINAL_STATUSES:
        # STRAGGLER give-up, mirroring _transmit_phase: cancel and report LOUDLY. Never leave a
        # working block alive across the cash re-read.
        _cancel_working_block(ib, res["order_ref"])
        res["reason"] = (f"NOT TERMINAL after {PHASE_TERMINAL_TIMEOUT_SEC:.0f}s "
                         f"(status={res['status']}) — cancelled at the phase timeout")
    elif res["filled"] < res["requested"]:
        res["reason"] = "UNFILLED remainder (block reached terminal state short)"
    return res


def _notional_by_account(routes_with_limits, results) -> dict:
    """PURE: per-account dollars a phase ACTUALLY transacted — the account's split shares x the
    block limit x the block's FILL FRACTION (a block allocates pro-rata to its ContractsOrShares
    split, so a partial block fill lands pro-rata too). Skipped/refused blocks contribute zero.
    Used for BOTH the sell phase's realized proceeds and the buy phase's placed notional.

    The split read is the RESULT's (`res["split"]`), not the incoming route's: the PDT gate can
    drop an account from a block after routing, and a dropped account transacted NOTHING. Falls
    back to the route's own split when a result carries none (older/synthetic result dicts)."""
    out: dict = {}
    for (r, _limit), res in zip(routes_with_limits, results):
        if res.get("skipped"):
            continue
        requested = float(res.get("requested", 0.0) or 0.0)
        frac = min(max((float(res.get("filled", 0.0) or 0.0) / requested)
                       if requested > 0 else 0.0, 0.0), 1.0)
        lim = float(res.get("limit") or 0.0)
        split = res.get("split") or r.per_account_split
        for acct, q in split.items():
            out[acct] = out.get(acct, 0.0) + float(q) * lim * frac
    return out


def _run_block_phase(ib, phase_label, routes_with_limits, account_inputs, targets, allowed,
                     as_of, *, permit: bool, summaries: dict | None,
                     run_id: str | None = None,
                     adaptive_priority: str | None = None) -> list[dict]:
    """Run ONE phase's blocks (all sells, or all buys) and return their result dicts.

    Blocks are run ONE AT A TIME: an FA block's allocation IS its group's live
    ContractsOrShares, so the replaceFA and the block it governs must stay in lockstep (two
    blocks on the same group in flight would race the group's split). order_router.place
    already waits each block to terminal within the bounded fill window, so serial execution is
    also what makes "all sells reach terminal state before any buy is sized" true."""
    print(f"\n    ===== PHASE {phase_label} — {len(routes_with_limits)} block(s) =====")
    if not routes_with_limits:
        print(f"    [{phase_label}] no blocks.")
        return []
    return [_execute_one_route(ib, r, account_inputs, targets, allowed, as_of, limit,
                               permit=permit, summaries=summaries, phase_label=phase_label,
                               run_id=run_id,
                              adaptive_priority=adaptive_priority)
            for r, limit in routes_with_limits]


def execute_fa_block_routes(ib, routes, account_inputs, targets, target: TargetGateway,
                            *, permit: bool, summaries: dict | None = None,
                            run_id: str | None = None,
                            adaptive_priority: str | None = None) -> dict:
    """Drive the fa_block routes under the SAME two-phase cash gate the per-account lane uses
    (see the module docstring): SELL blocks first, then a fresh REALIZED-cash read per
    sub-account, then BUY blocks RE-SIZED to that cash, then the uninvested-proceeds exception
    report.

    `permit` is the FULL armed gate result (code gate AND gateway write-enabled) computed by the
    caller. When permit is False this is a PREVIEW: it prints the phase plan, each route's
    would-write group DIFF and builds the block (build-only, place(armed=False)), writing NO FA
    config, transmitting nothing, and performing NO broker cash read. When permit is True it
    writes each group's ContractsOrShares (replaceFA) in lockstep with placing that group's
    block, one at a time, watching fills. Reuses order_router.build_fa_block/place (dedup +
    price guard live inside).

    A route whose side is neither BUY nor SELL FAILS CLOSED: the WHOLE run is refused before the
    backup, before any group write and before any order — the phase order cannot be guessed.

    Direct routes (a tier with a single account) are OUT OF SCOPE for this module (the FA-block
    path needs >=2 accounts); they are listed and SKIPPED here — the per-account-direct path
    (batch_rebalance_execute.py) owns them.

    PER-RUN ORDER-REF STAMP (v0.34.0): ONE `run_id` is minted here (safe_execute._run_id, the
    same generator the per-account lane uses) and stamped onto EVERY block ref this call places
    — sells and buys alike, since both phases belong to one run. The block ref used to end at
    the model `as_of` (effectively a MONTH stamp), so order_router.place's dedup read a SECOND
    run of the same group/symbol/side as "an identical WORKING order already exists" and sent
    nothing — the 2026-07-28 root cause, fixed then on the per-account lane only. WITHIN this
    call the stamp is constant, so a double-submit of one leg is STILL blocked; a LATER run gets
    a new stamp and is correctly treated as new work. Returned as `run_id` so _ledger can join
    the audit record to the refs on the wire.

    Returns a summary dict. The original keys are UNCHANGED for existing callers —
    {n_blocks, n_direct_skipped, replace_fa_writes, placed_fills, backup} — and are EXTENDED
    with {refused, refused_reason, n_sell_blocks, n_buy_blocks, sell_results, buy_results,
    realized_cash, buy_resize, dropped_buy_blocks, uninvested, pdt_dropped,
    pdt_refused_blocks, run_id}."""
    fa_routes = [r for r in routes if r.route == "fa_block"]
    direct_routes = [r for r in routes if r.route != "fa_block"]
    placed_fills: list[dict] = []
    backup_path = ""
    run_id = run_id or _run_id()

    def _summary(**extra) -> dict:
        base = {"n_blocks": len(fa_routes), "n_direct_skipped": len(direct_routes),
                "replace_fa_writes": 0, "placed_fills": placed_fills, "backup": backup_path,
                "refused": False, "refused_reason": "", "n_sell_blocks": 0, "n_buy_blocks": 0,
                "sell_results": [], "buy_results": [], "realized_cash": {}, "buy_resize": {},
                "dropped_buy_blocks": [], "uninvested": [], "pdt_dropped": [],
                "pdt_refused_blocks": [], "run_id": run_id}
        base.update(extra)
        return base

    if direct_routes:
        print(f"\n    NOTE: {len(direct_routes)} DIRECT route(s) are OUT OF SCOPE for the "
              f"FA-block module and are SKIPPED here (per-account-direct path owns them): "
              f"{', '.join(f'{r.side} {r.symbol} @ {r.account}' for r in direct_routes)}")

    # [1] PARTITION — SELLS ALWAYS FIRST. An unknown side FAILS CLOSED (refuse, never guess).
    sell_routes = [r for r in fa_routes if str(r.side).upper() == "SELL"]
    buy_routes = [r for r in fa_routes if str(r.side).upper() == "BUY"]
    unknown = [r for r in fa_routes if str(r.side).upper() not in ("BUY", "SELL")]
    if unknown:
        detail = ", ".join(f"{r.side!r} {r.symbol} group={r.fa_group}" for r in unknown)
        reason = (f"{len(unknown)} fa_block route(s) with an UNKNOWN side ({detail}) — the "
                  f"sell-before-buy phase order cannot be determined. FAILING CLOSED: the "
                  f"WHOLE run is refused. No backup, no replaceFA, no order, nothing "
                  f"transmitted.")
        print(f"\n    !! REFUSING THE RUN — {reason}")
        return _summary(refused=True, refused_reason=reason)

    if not fa_routes:
        print("\n    No FA-block routes (a group needs >=2 accounts). Nothing to write/place.")
        return _summary()

    as_of = next(iter(targets.values())).as_of
    allowed = list(target.enrollment.keys())

    print(f"\n    RUN ID {run_id} — stamped onto EVERY block orderRef this run places (sells "
          f"and buys share it). A later run gets a NEW stamp and is treated as NEW WORK.")
    print(f"\n    PHASE PLAN (two-phase cash gate): SELL {len(sell_routes)} block(s) FIRST, "
          f"then re-read REALIZED cash per sub-account, then BUY {len(buy_routes)} block(s) "
          f"RE-SIZED to that cash (safety buffer {CASH_SAFETY_BUFFER_PCT:.2%}).")
    for r in sell_routes:
        print(f"      1. SELL {r.symbol} x{r.total_qty} group={r.fa_group}")
    for r in buy_routes:
        print(f"      2. BUY  {r.symbol} x{r.total_qty} group={r.fa_group}  "
              f"(quantity + group split WILL be re-sized to realized cash)")

    # Block limits are computed ONCE, up front, so the buy re-sizing prices the SAME cap the
    # block is actually placed at (a second quote read could drift and break the cash gate).
    sell_with_limits = [(r, rebalance_execute._fa_block_limit(r, _quotes_cache, account_inputs,
                                                              targets)) for r in sell_routes]
    buy_with_limits = [(r, rebalance_execute._fa_block_limit(r, _quotes_cache, account_inputs,
                                                             targets)) for r in buy_routes]

    # ARMED + permitted: MANDATORY backup of the whole groups XML BEFORE any replaceFA write.
    if permit:
        print("\n    ARMED. Backing up live FA groups XML before any replaceFA write...")
        backup_path = rebalance_execute.backup_fa_groups(ib)
        print(f"      backup -> {backup_path}")

    # [2] PHASE 1 — SELLS. Every sell block reaches terminal state (bounded) before any buy is
    # sized; stragglers are cancelled and reported LOUDLY inside _execute_one_route.
    sell_results = _run_block_phase(ib, "SELL", sell_with_limits, account_inputs, targets,
                                   allowed, as_of, permit=permit, summaries=summaries,
                                   run_id=run_id, adaptive_priority=adaptive_priority)
    for res in sell_results:
        placed_fills.extend(res.get("fills", []))

    # [3] BETWEEN PHASES — RE-READ realized cash. NEVER trust the plan's expected proceeds; a
    # cancelled/short sell block means that cash never landed. PREVIEW does NO broker read.
    realized_cash: dict = {}
    cash_accounts = sorted({a for r, _l in buy_with_limits for a in r.per_account_split}
                           | {a for r, _l in sell_with_limits for a in r.per_account_split})
    if permit:
        ib.sleep(CASH_SETTLE_SEC)      # let streaming account values catch up to the fills
        realized_cash = read_realized_cash(ib, cash_accounts)
        print(f"\n    REALIZED cash (fresh TotalCashValue per sub-account) — buys are sized to "
              f"THIS, never to expected proceeds:")
        for acct in cash_accounts:
            val = realized_cash.get(acct)
            if val is None:
                print(f"      !! {acct}: TotalCashValue UNREADABLE — FAIL CLOSED, this account "
                      f"contributes ZERO to the buy phase.")
            else:
                print(f"      {acct}: {val:,.2f}")
    else:
        print(f"\n    PREVIEW — would now wait {CASH_SETTLE_SEC:.0f}s and re-read realized "
              f"TotalCashValue for {', '.join(cash_accounts) or '(none)'}, then RE-SIZE every "
              f"buy block (quantity AND group split) so each account buys only what its OWN "
              f"realized cash covers. No broker read performed; buy quantities below are the "
              f"UNRESIZED plan.")

    # [4] RE-SIZE the buy blocks to realized cash (armed only — preview keeps plan quantities so
    # the operator sees the engine's intent; the print above says they would be re-sized).
    buy_resize: dict = {}
    dropped: list = []
    if permit:
        buy_with_limits, dropped, buy_resize = resize_buy_routes_to_realized_cash(
            buy_with_limits, realized_cash)
        for acct in sorted(buy_resize):
            for adj in buy_resize[acct]["adjustments"]:
                verb = "SKIP  " if adj["new_qty"] == 0 else "REDUCE"
                print(f"      {verb} {acct} {adj['symbol']}: {adj['orig_qty']} -> "
                      f"{adj['new_qty']} shares (fit to that account's OWN realized cash)")
        for r, _limit in dropped:
            print(f"      !! DROPPED buy block {r.symbol} group={r.fa_group}: every account "
                  f"scaled to zero shares against realized cash.")

    # [5] PHASE 2 — BUYS (re-sized).
    buy_results = _run_block_phase(ib, "BUY", buy_with_limits, account_inputs, targets,
                                  allowed, as_of, permit=permit, summaries=summaries,
                                  run_id=run_id, adaptive_priority=adaptive_priority)
    for res in buy_results:
        placed_fills.extend(res.get("fills", []))

    replace_fa_writes = sum(res.get("replace_fa", 0)
                            for res in (sell_results + buy_results))

    # [6a] PDT EXCEPTION REPORT — PREVIEW AND ARMED ALIKE. Every account any block dropped for
    # pattern-day-trader restriction, and every block refused outright because its whole split
    # was blocked. An account that quietly did not get rebalanced is the failure mode that let
    # an $826k rollover sit idle 131 days, so this prints on every run, not just armed ones.
    all_results = sell_results + buy_results
    pdt_dropped: list = []
    seen_drop: set = set()
    for res in all_results:
        for d in res.get("pdt_dropped", []) or []:
            row = dict(d)
            row["symbol"], row["side"] = res.get("symbol"), res.get("side")
            row["group"], row["phase"] = res.get("group"), res.get("phase")
            pdt_dropped.append(row)
            seen_drop.add(d["account"])
    pdt_refused_blocks = [{"symbol": res.get("symbol"), "side": res.get("side"),
                           "group": res.get("group"), "phase": res.get("phase"),
                           "reason": res.get("reason", "")}
                          for res in all_results if res.get("status") == "SKIPPED_PDT"]
    if pdt_dropped or pdt_refused_blocks:
        print("\n    !! PATTERN-DAY-TRADER EXCEPTIONS (LOUD — these accounts were NOT "
              "rebalanced; needs human review):")
        for row in pdt_dropped:
            print(f"      -> account={row['account']} DROPPED from {row['phase']} block "
                  f"{row['side']} {row['symbol']} group={row['group']} "
                  f"shares_dropped={row['shares']} "
                  f"{PDT_TAG}={row['day_trades_remaining']!r}")
        for row in pdt_refused_blocks:
            print(f"      -> BLOCK REFUSED {row['phase']} {row['side']} {row['symbol']} "
                  f"group={row['group']}: {row['reason']}")
        print(f"      {len(seen_drop)} distinct account(s) dropped, "
              f"{len(pdt_refused_blocks)} block(s) refused outright.")
    else:
        print("\n    Pattern-day-trader check: CLEAN — every account in every block's split "
              "cleared IBKR's DayTradesRemaining.")

    # [6b] LOUD reporting. Armed runs get safe_execute's phase reporter verbatim (same format the
    # per-account lane prints) plus the uninvested-proceeds exception report.
    uninvested: list = []
    if permit:
        _report_phase("SELL", sell_results)
        _report_phase("BUY", buy_results)

        proceeds_by_account = _notional_by_account(sell_with_limits, sell_results)
        placed_by_account = _notional_by_account(buy_with_limits, buy_results)
        uninvested = uninvested_proceeds_report(proceeds_by_account, buy_resize,
                                                placed_by_account)
        if uninvested:
            print("\n    !! UNINVESTED PROCEEDS (LOUD — an account SOLD but is sitting in "
                  "cash; needs human review):")
            for row in uninvested:
                print(f"      -> account={row['account']} dollars_uninvested="
                      f"{row['dollars_uninvested']:,.2f} reason={row['reason']} "
                      f"proceeds_raised={row['proceeds_raised']:,.2f} "
                      f"realized_cash={row['realized_cash']} "
                      f"planned_notional={row['planned_notional']:,.2f} "
                      f"placed_notional={row['placed_notional']:,.2f} :: {row['detail']}")
        else:
            print("\n    Uninvested-proceeds check: CLEAN — every account that raised proceeds "
                  "redeployed them (within the whole-share remainder + safety buffer).")

    return _summary(replace_fa_writes=replace_fa_writes, run_id=run_id,
                    refused=False, refused_reason="",
                    n_sell_blocks=len(sell_routes), n_buy_blocks=len(buy_routes),
                    sell_results=sell_results, buy_results=buy_results,
                    realized_cash=realized_cash, buy_resize=buy_resize,
                    dropped_buy_blocks=[r.symbol for r, _l in dropped],
                    uninvested=uninvested,
                    pdt_dropped=pdt_dropped, pdt_refused_blocks=pdt_refused_blocks)


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

        account_inputs, summaries = build_account_inputs(ib, clients, targets)

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

        # [5] Size + route (pure engine). The corp-action guard is ON: without the strategy
        # universe reconcile collapses every unrecognised holding to UNTRACKED, which ALWAYS
        # breaches the band and sizes as delta = 0 - held — a FULL LIQUIDATION of a spinoff /
        # rename / client holding / sweep. FAILS CLOSED (v0.41.0).
        try:
            strat_universe = recon_report.strategy_universe_or_refuse()
        except recon_report.CorpActionGuardUnavailable as exc:
            print(f"\n[5] REFUSING — {exc}\n    No orders built, nothing transmitted, no FA "
                  f"config written.")
            return 2
        out = build_plan(account_inputs, targets, tier_groups=tier_groups,
                         universe=strat_universe)
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
        if result.get("refused"):
            # Unknown route side -> the phase order could not be determined -> the WHOLE run was
            # refused before any backup/write/order. Non-zero rc so a scheduler never reads it
            # as a clean run.
            print(f"\nREFUSED — {result.get('refused_reason')}")
            return 2
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
        # PER-RUN ORDER-REF STAMP (v0.34.0). Every block ref this run put on the wire ends in
        # this run_id, so a human (or an examiner) can join an IBKR orderRef back to THIS audit
        # record and vice versa. Durable here precisely because the ref alone is not a record.
        "run_id": result.get("run_id", ""),
        # Two-phase cash gate (conductor #64): the audit trail for what was sold first, what
        # cash actually landed, how the buys were re-sized, and any account left in cash.
        "phases": {"n_sell_blocks": result.get("n_sell_blocks", 0),
                   "n_buy_blocks": result.get("n_buy_blocks", 0),
                   "realized_cash": result.get("realized_cash", {}),
                   "dropped_buy_blocks": result.get("dropped_buy_blocks", []),
                   "uninvested": result.get("uninvested", [])},
        # PATTERN-DAY-TRADER gate (v0.36.0). TOP-LEVEL, not buried in phases: an account the
        # gate dropped did NOT get rebalanced, and that omission has to be as findable in the
        # audit trail as it is loud on the console. Empty list = nothing was dropped.
        "pdt_dropped": result.get("pdt_dropped", []),
        "pdt_refused_blocks": result.get("pdt_refused_blocks", []),
        "halted": bool(result.get("refused")),
        "halt_reason": result.get("refused_reason", ""),
        "gate": {"target": target.name, "port": target.port,
                 "readonly": config.READONLY, "dry_run": config.DRY_RUN,
                 "armed": armed, "permitted": permit, "why": why},
        **version.stamp(),
    })


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
