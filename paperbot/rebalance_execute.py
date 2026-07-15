"""
rebalance_execute.py — the TRANSMIT-CAPABLE multi-account rebalance EXECUTOR.

This is the Monday-path sibling of rebalance_run.py. rebalance_run is REVIEW-ONLY: it
connects readonly=True and hardcodes armed=False, so it physically cannot place an order
— it is the source of truth for the NUMBERS, not the thing that sends them. THIS file is
the one that CAN transmit, behind a hard, multi-condition gate.

DEFAULT IS SAFE. With no special flag, running this file does exactly what rebalance_run
does: a read-only DRY-RUN review that builds order objects, logs them, and transmits
NOTHING. To actually transmit you must line up ALL FOUR of:
  1. config.READONLY  is False   (flipped in-process only by the arm path; the committed
     default stays True)
  2. config.DRY_RUN   is False   (likewise flipped in-process only by the arm path)
  3. a human passes   armed=True (never defaulted, never auto-set)
  4. an explicit CLI token  --arm-i-understand  is present on the command line
There is NO auto-arm anywhere. Omit the token and the run is a dry review even if you
typo `--armed`; the executor refuses to flip the flags without the exact token.

ARMED FLOW (each step logged to the ledger), mirroring MONDAY_RUNBOOK steps B–G but
fully in code:
  * connect NON-read-only, PINNED to a DU sub-account (flatten_accounts pattern) so the
    session can transmit AND so ib_async never hangs on the FA master's account stream;
  * accounts.discover -> live NetLiq + positions for every enrolled+funded sub;
  * live quotes -> per-symbol reference/limit price (fall back to the strategy close);
  * rebalance_engine.build_plan -> plans / blocks / routes (pure);
  * resolve_tier_groups via requestFA(1) — FAIL CLOSED on any ambiguity;
  * risk_manager.evaluate per account BEFORE any transmit (kill switch + guards);
  * read the LIVE FA-groups XML once, BACK IT UP to a timestamped file (like the prior
    fa_groups_backup.xml), then for each FA-block route set ONLY that tier group's
    ContractsOrShares to the RoutePlan per_account_split via replaceFA (a serialized
    config write — the full GROUPS XML is replaced each time; untouched groups preserved);
  * build the FA-block (faMethod="") / direct orders — order_router's HARD PRICE GUARD
    rejects any NaN/<=0 limit before an order is built;
  * place ONE block at a time with order_router.place(armed=True) and watch fills. NEVER
    whatIfOrder a group order (it hangs) — we skip what-if for blocks entirely;
  * reconcile each account back to model with recon_report's read-only readout.

This file leaves config.py's committed defaults (READONLY=True / DRY_RUN=True) untouched
on disk; the arm path flips them in memory for THIS process only, exactly like
live_fill_test.py. PAPER ONLY (port 4002, DU sub-accounts under master DF...141).

Run — DRY-RUN review (default, transmits nothing, identical to rebalance_run):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe rebalance_execute.py

Run — ARMED execute (Monday, human-supervised; ALL of: arm token + non-read-only):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe rebalance_execute.py --arm-i-understand

Optional SCOPE (restrict the run; default = full enrolled fleet, unchanged):
  --only-account DU8922142   only that account's DIRECT routes (ALL fa_block routes
                             dropped — no replaceFA / no block orders for other tiers)
  --only-tier Conservative   only that tier's routes
  e.g. complete just DU142's stuck direct legs, armed:
  ...rebalance_execute.py --arm-i-understand --only-account DU8922142
Scoping NEVER bypasses the arm gate; an unknown account/tier FAILS CLOSED.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ib_async import IB   # noqa: E402

import accounts          # noqa: E402
import config            # noqa: E402
import ledger            # noqa: E402
import live_quotes       # noqa: E402
import order_router      # noqa: E402
import risk_manager      # noqa: E402
import version           # noqa: E402
from connections import clientids, ibkr_paper   # noqa: E402
from gateway_lock import GatewayBusyRefuse, gateway_lock   # noqa: E402
from rebalance_engine import build_plan   # noqa: E402
# Reuse, don't duplicate: the review runner already has the preview, the fail-closed
# group resolver, the per-version target compute, the price lookup and the direct-intent.
import rebalance_run     # noqa: E402

# The arm token must be typed EXACTLY. No abbreviation, no default.
ARM_TOKEN = "--arm-i-understand"

# Pin the transmit connection to a DU sub-account (NEVER the master DF...141 — it hangs
# the account-update stream and rejects direct orders). Lowest-numbered enrolled DU.
PIN_ACCOUNT = sorted(config.ENROLLMENT)[0]   # DU8922142

# Where FA-config backups land (off Drive, with the rest of paperbot state).
_BACKUP_DIR = os.path.join(config.STATE_DIR, "fa_backups")


# --- the gate ------------------------------------------------------------------
def arm_requested(argv: list[str]) -> bool:
    """True ONLY if the exact arm token is present. This is condition (4) of the gate
    and the single thing that authorizes flipping the in-process safety flags."""
    return ARM_TOKEN in argv


# --- optional SCOPE filter (restrict a run to one account or one tier) ----------
# Default behavior is UNCHANGED: with neither flag the full enrolled fleet runs exactly
# as before. The flags only NARROW the set of routes the execute loop acts on; they do NOT
# touch the arm gate (scoping never bypasses or weakens arming) or the engine math.
SCOPE_ACCOUNT_FLAG = "--only-account"
SCOPE_TIER_FLAG = "--only-tier"


def parse_scope(argv: list[str]) -> tuple[str | None, str | None]:
    """Pull the optional scope from argv: (only_account, only_tier), each None if absent.
    Accepts `--only-account DU8922142` and `--only-tier Conservative` (space-separated
    value, mirroring how the arm token is a bare flag). Raises ValueError on a flag given
    without a value so a typo fails loudly rather than silently widening scope."""
    only_account = None
    only_tier = None
    for flag, setter in ((SCOPE_ACCOUNT_FLAG, "account"), (SCOPE_TIER_FLAG, "tier")):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise ValueError(f"{flag} requires a value (e.g. {flag} "
                                 f"{'DU8922142' if setter == 'account' else 'Conservative'})")
            val = argv[i + 1]
            if setter == "account":
                only_account = val
            else:
                only_tier = val
    return only_account, only_tier


def validate_scope(only_account: str | None, only_tier: str | None) -> None:
    """FAIL CLOSED on a scope that names something not enrolled/valid — better to abort
    than to silently match zero and look like a clean no-op for the wrong reason."""
    if only_account is not None and only_account not in config.ENROLLMENT:
        raise ValueError(
            f"--only-account {only_account!r} is not in config.ENROLLMENT "
            f"({sorted(config.ENROLLMENT)}). FAILING CLOSED — no run.")
    if only_tier is not None and only_tier not in config.VALID_VERSIONS:
        raise ValueError(
            f"--only-tier {only_tier!r} is not a valid version "
            f"({list(config.VALID_VERSIONS)}). FAILING CLOSED — no run.")


def filter_routes(routes, only_account: str | None, only_tier: str | None):
    """Narrow the route list to the requested scope. Applied AFTER build_plan, so the
    engine math/allocation is untouched — we only choose which routes the loop acts on.

      --only-account A : keep ONLY `direct` routes whose account == A. ALL `fa_block`
                         routes are dropped (a block spans a whole tier's accounts and
                         must never be partially written/placed for one account — this is
                         exactly what protects the unproven FA-block legs DU143-146 during
                         a lone-DU142 completion).
      --only-tier T    : keep ONLY routes for tier/version T (direct or block).

    Both may be given together (intersection). Neither -> routes unchanged."""
    out = list(routes)
    if only_tier is not None:
        out = [r for r in out if r.version == only_tier]
    if only_account is not None:
        out = [r for r in out if r.route == "direct" and r.account == only_account]
    return out


def gate_state(armed: bool) -> tuple[bool, str]:
    """Whether transmission is permitted RIGHT NOW. Delegates to the canonical
    order_router.transmit_guard (fails closed) so there is one definition of the gate:
    permitted iff READONLY=False AND DRY_RUN=False AND armed=True."""
    return order_router.transmit_guard(armed)


def _safety_banner(armed: bool, token_present: bool) -> None:
    permit, why = gate_state(armed)
    print("\n" + "#" * 92)
    print(f"# SAFETY STATE   READONLY={config.READONLY}   DRY_RUN={config.DRY_RUN}   "
          f"armed={armed}   arm_token={'present' if token_present else 'absent'}")
    print(f"# transmission: {'PERMITTED' if permit else 'BLOCKED'} ({why})")
    if permit:
        print("# *** ARMED EXECUTOR: this run CAN transmit PAPER orders and WRITE FA config "
              "(replaceFA). ***")
    else:
        print("# DRY-RUN review: builds + logs order objects, writes no FA config, transmits "
              "nothing.")
    print("#" * 92)


# --- FA config write (ContractsOrShares via replaceFA) -------------------------
def backup_fa_groups(ib) -> str:
    """Read the live GROUPS XML once and write it to a timestamped backup file (like the
    prior fa_groups_backup.xml). replaceFA is destructive (full-XML overwrite), so we
    always snapshot first. Returns the backup path."""
    xml = ib.requestFA(1)   # 1 = GROUPS (read-only)
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(_BACKUP_DIR, f"fa_groups_backup_{stamp}.xml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(xml or ""))
    return path


def set_group_contracts_or_shares(ib, fa_group: str, per_account_split: dict) -> str:
    """Set ONE FA group's allocation to ContractsOrShares == per_account_split via
    replaceFA, preserving every OTHER group untouched.

    requestFA(1) returns the FULL groups XML; replaceFA(1, xml) REPLACES the full set.
    So we read current, mutate only the named group's <defaultMethod> -> ContractsOrShares
    and its <ListOfAccts> Account/percent entries to the split, leave all other groups
    byte-for-byte, and write it back. Returns the XML we wrote (for the ledger).

    Serialize this — it is a shared-plumbing write. The caller does ONE group at a time,
    in lockstep with placing that group's block (ContractsOrShares is per-order-quantity).
    """
    import xml.etree.ElementTree as ET

    current = str(ib.requestFA(1) or "").strip()
    if not current:
        raise RuntimeError(
            f"requestFA(1) returned no groups XML — cannot safely set ContractsOrShares "
            f"on '{fa_group}'. FAILING CLOSED (no config written, no order placed).")
    root = ET.fromstring(current)

    target = None
    for grp in root.iter():
        if grp.tag.split("}")[-1].lower() != "group":
            continue
        name = next((c.text for c in grp.iter()
                     if c.tag.split("}")[-1].lower() == "name" and c.text), None)
        if name and name.strip() == fa_group:
            target = grp
            break
    if target is None:
        raise RuntimeError(
            f"FA group '{fa_group}' not found in live groups XML — cannot set its "
            f"ContractsOrShares. FAILING CLOSED.")

    # defaultMethod -> ContractsOrShares (an order-level faMethod is rejected, Err 10226).
    for child in target.iter():
        if child.tag.split("}")[-1].lower() == "defaultmethod":
            child.text = "ContractsOrShares"
            break

    # Rewrite the member list to exactly the split: each Account's amount = its shares.
    loa = next((c for c in target.iter()
                if c.tag.split("}")[-1].lower() == "listofaccts"), None)
    if loa is None:
        raise RuntimeError(
            f"FA group '{fa_group}' has no ListOfAccts element — refusing to guess the "
            f"member layout. FAILING CLOSED.")
    # Clear existing members, then re-add one <Account> per split entry with its share
    # amount in <amount> (the per-account ContractsOrShares value).
    tag_prefix = loa.tag[:loa.tag.find("}") + 1] if "}" in loa.tag else ""
    for acct_el in list(loa):
        loa.remove(acct_el)
    for acct, shares in sorted(per_account_split.items()):
        ael = ET.SubElement(loa, f"{tag_prefix}Account")
        nm = ET.SubElement(ael, f"{tag_prefix}acct")
        nm.text = acct
        amt = ET.SubElement(ael, f"{tag_prefix}amount")
        amt.text = str(int(shares))

    new_xml = ET.tostring(root, encoding="unicode")
    ib.replaceFA(1, new_xml)   # serialized config write
    return new_xml


# --- the armed execution path --------------------------------------------------
def _net_liq(summary, account: str):
    return next((float(r.value) for r in summary
                 if r.account == account and r.tag == "NetLiquidation"), None)


def execute_armed(armed: bool, only_account: str | None = None,
                  only_tier: str | None = None) -> int:
    """The Monday path. Connects NON-read-only pinned to a DU sub, discovers live state,
    builds the plan, resolves groups fail-closed, runs risk guards, and (only if the gate
    fully permits) writes each tier group's ContractsOrShares and places its block, one at
    a time, watching fills, then reconciles. Every step is logged to the ledger.

    Optional SCOPE (only_account / only_tier): narrows the routes the loop acts on AFTER
    build_plan. Default (both None) = full enrolled fleet, identical to before. Scoping
    applies in BOTH the dry-review and armed paths and composes with — never bypasses —
    the arm gate."""
    permit, why = gate_state(armed)
    targets = rebalance_run._targets_by_version()
    print("\n[1] Tier models:")
    for v, t in targets.items():
        print(f"    {v:13s} as_of={t.as_of.date()}  ({len(t.weights)} holdings)")

    # GATEWAY LOCK (Slice 3): acquire the single-process Gateway mutex BEFORE connecting and
    # hold it across the ENTIRE armed flow (connect -> replaceFA -> place blocks one at a time
    # -> reconcile -> disconnect). The heartbeat thread keeps the lease alive through a
    # legitimately long laddered execution. This is the transmit-capable path, so it INSISTS:
    # on_busy="refuse" waits a short bounded time then REFUSES — naming the holder — and ABORTS
    # BEFORE any connect, FA-config write, or order build. Never transmit into a contended
    # Gateway. The context manager releases on normal exit AND on exception.
    try:
        with gateway_lock(purpose="rebalance_execute",
                          client_id=clientids.get("paperbot_rebalance_exec"),
                          on_busy="refuse"):
            return _run_armed_session(armed, only_account, only_tier, permit, why, targets)
    except GatewayBusyRefuse as busy:
        holder = busy.holder or {}
        print(f"\n[2] REFUSING to start the armed execute — gateway held by "
              f"{holder.get('purpose')} pid {holder.get('pid')} clientId "
              f"{holder.get('client_id')} since "
              f"{holder.get('acquired_at') or holder.get('acquired_ts')}. No connection "
              f"opened, NO orders built, nothing transmitted, no FA config written, no "
              f"replaceFA. Re-run once the holder finishes.")
        return 2


def _run_armed_session(armed: bool, only_account: str | None, only_tier: str | None,
                       permit: bool, why: str, targets: dict) -> int:
    """The connect -> (FA config write + place blocks) -> reconcile -> disconnect body, run
    only while the gateway lock is HELD. Factored out of execute_armed so the
    `with gateway_lock(...)` block wraps the WHOLE armed flow — not just the connect — and the
    heartbeat keeps the lease alive across the laddered execution. Behavior inside is
    unchanged from before the lock was added."""
    # Connect NON-read-only, pinned to a DU account (flatten_accounts.py pattern). This is
    # the only way the session can transmit; pinning dodges the master account-stream hang.
    print(f"\n[2] Connecting NON-readonly pinned to {PIN_ACCOUNT} "
          f"(clientId={clientids.get('paperbot_rebalance_exec')})...")
    ib = IB()
    ib.connect(ibkr_paper.HOST, ibkr_paper.PAPER_PORT,
               clientId=clientids.get("paperbot_rebalance_exec"),
               readonly=False, timeout=15, account=PIN_ACCOUNT)
    placed_fills: list[dict] = []
    backup_path = ""
    halted = False
    halt_reason = ""
    try:
        managed = ib.managedAccounts()
        if PIN_ACCOUNT not in managed:
            print(f"    ABORT: pin {PIN_ACCOUNT} not in managed accounts {managed}.")
            return 2

        # [3] Live state per enrolled+funded sub.
        infos = accounts.discover(ib)
        clients = [i for i in infos if i.enrolled and i.funded and not i.is_master]
        if not clients:
            print("\n    No enrolled + funded client accounts to rebalance. Done.")
            return 0

        universe = sorted({s for t in targets.values() for s in t.weights.index})
        print(f"\n[3] Fetching live quotes for {len(universe)} symbol(s)...")
        quotes = live_quotes.fetch(ib, universe)

        account_inputs: list[dict] = []
        for info in sorted(clients, key=lambda x: x.number):
            positions = {p.contract.symbol: p.position
                         for p in ib.positions(info.number) if p.position != 0}
            tier_prices = targets[info.version].prices
            prices = {}
            for sym in set(tier_prices.index) | set(positions):
                q = quotes.get(sym)
                ref = live_quotes.reference_price(q) if q else None
                prices[sym] = ref if (ref and ref > 0) else float(
                    tier_prices.get(sym, float("nan")))
            account_inputs.append({
                "account": info.number, "version": info.version,
                "net_liq": info.net_liq, "positions": positions, "prices": prices})

        # [4] Resolve version -> FA group by LIVE membership (fail closed).
        print("\n[4] Resolving version->FA group via requestFA(1) (fail-closed)...")
        enrolled_versions = {i.version for i in clients}
        try:
            tier_groups = rebalance_run.resolve_tier_groups(ib, enrolled_versions)
        except RuntimeError as exc:
            print(f"    {exc}\n    -> No orders built.")
            return 2
        for v, g in sorted(tier_groups.items()):
            print(f"    {v:13s} -> group '{g}'")

        # [5] Preview (pure) + the plan we will act on.
        out = rebalance_run.build_preview(account_inputs, targets, tier_groups=tier_groups)
        routes = out["routes"]

        # [5b] SCOPE FILTER (optional) — narrow to one account/tier, AFTER build_plan so the
        # engine math is untouched. A --only-account run drops ALL fa_block routes, so no
        # replaceFA write and no block placement happens for the out-of-scope tiers.
        if only_account is not None or only_tier is not None:
            scope_desc = ", ".join(
                p for p in (f"account={only_account}" if only_account else "",
                            f"tier={only_tier}" if only_tier else "") if p)
            before = len(routes)
            routes = filter_routes(routes, only_account, only_tier)
            dropped_blocks = [r for r in out["routes"]
                              if r.route == "fa_block" and r not in routes]
            print(f"\n[5b] SCOPE active ({scope_desc}): {len(routes)}/{before} routes in "
                  f"scope; {len(dropped_blocks)} fa_block route(s) DROPPED "
                  f"(no replaceFA / no block placement for them).")
            if not routes:
                print("    Scope matched ZERO routes — nothing to do. Transmitting nothing, "
                      "writing no FA config.")
                _ledger(armed, account_inputs, routes, placed_fills, backup_path,
                        halted=False, halt_reason="")
                return 0

        # [6] RISK GUARDS per account BEFORE any transmit (kill switch + per-order caps).
        print("\n[6] Risk guards (risk_manager) per account, pre-transmit...")
        plans_by_acct = {p.account: p for p in out["plans"]}
        for ai in account_inputs:
            p = plans_by_acct.get(ai["account"])
            if not p or not p.orders:
                continue
            intents = [rebalance_run._DirectIntent(
                sym, "BUY" if d > 0 else "SELL", abs(d),
                round(float(ai["prices"].get(sym, float("nan"))), 2))
                for sym, d in p.orders.items()]
            rep = risk_manager.evaluate(ai["net_liq"], 0.0, ai["positions"], intents,
                                        targets[ai["version"]])
            if rep.halted:
                halted, halt_reason = True, rep.halt_reason
                print(f"    {ai['account']}: HALTED — {rep.halt_reason}")
            elif not rep.all_clear:
                vetoes = [f"{v.symbol}:{','.join(v.reasons)}"
                          for v in rep.order_verdicts if not v.ok] + rep.batch_reasons
                halted, halt_reason = True, f"{ai['account']} risk veto: {vetoes}"
                print(f"    {ai['account']}: VETO — {vetoes}")
            else:
                print(f"    {ai['account']}: clear ({len(intents)} order(s))")
        if halted:
            print("\n    SAFETY STOP: risk guard halted/vetoed — NOTHING transmitted, no "
                  "FA config written.")
            _ledger(armed, account_inputs, routes, placed_fills, backup_path,
                    halted=True, halt_reason=halt_reason)
            return 2

        # If the gate does not fully permit, we stop here: build-only, log, no transmit.
        if not permit:
            print(f"\n[7] Transmission BLOCKED ({why}). Building order objects for the log "
                  f"only (place armed=False) — NOTHING sent, no FA config written.")
            built = _build_all(ib, routes, account_inputs, targets, quotes=quotes)
            order_router.place(ib, built, armed=False)
            _ledger(armed, account_inputs, routes, placed_fills, backup_path,
                    halted=False, halt_reason="")
            print("\nDone. DRY-RUN: orders built + logged, nothing transmitted, no FA "
                  "config written.")
            return 0

        # --- ARMED + permitted: write FA config + place blocks one at a time. ---
        print("\n[7] ARMED. Backing up live FA groups XML before any replaceFA write...")
        backup_path = backup_fa_groups(ib)
        print(f"    backup -> {backup_path}")

        as_of = next(iter(targets.values())).as_of
        for r in routes:
            if r.route == "fa_block":
                # Approach b: price the single block limit MARKETABLE so thin-book block
                # legs (TFLO) cross. Everything else about the FA-block path is unchanged.
                limit = _fa_block_limit(r, quotes, account_inputs, targets)
                # Set THIS group's ContractsOrShares to the split, THEN place the block.
                print(f"\n    [block] {r.side} {r.symbol} x{r.total_qty}  group={r.fa_group} "
                      f"limit={limit} (marketable={config.FA_BLOCK_MARKETABLE})")
                print(f"      writing ContractsOrShares: {dict(sorted(r.per_account_split.items()))}")
                set_group_contracts_or_shares(ib, r.fa_group, r.per_account_split)
                # build_fa_block runs the HARD PRICE GUARD (NaN/<=0 rejected) before build.
                try:
                    bo = order_router.build_fa_block(
                        r.symbol, r.side, r.total_qty, limit, r.fa_group, r.fa_method,
                        as_of, ib=ib)
                except ValueError as exc:
                    print(f"      SKIP — {exc}")
                    continue
                # NEVER what-if a group order (it hangs). Place directly, watch fills.
                res = order_router.place(ib, [bo], armed=True)
                placed_fills.extend(res.get("fills", []))
            else:
                # Direct single-account true-up. DIRECT legs are where the laddered
                # router applies first (FA-block algo compatibility is a separate probe —
                # see config.LADDER_FA_BLOCKS / research §4). The ladder escalates passive
                # -> marketable so a thin Treasury/cash leg (TFLO/VGSH) can never hang.
                print(f"\n    [direct] {r.side} {r.symbol} x{r.total_qty}  account={r.account}")
                q = quotes.get(r.symbol)
                res = _place_direct_laddered(ib, r, q, as_of, armed=True)
                if res is None:
                    # Fall back to the legacy single capped-limit path (no usable quote
                    # for the ladder caps, or ladder disabled). Neutral reference, unchanged.
                    limit = round(float(rebalance_run.prices_for(account_inputs, targets, r)), 2)
                    intent = rebalance_run._DirectIntent(r.symbol, r.side, r.total_qty, limit)
                    try:
                        built = order_router.build([intent], r.account, as_of, ib=ib)
                    except ValueError as exc:
                        print(f"      SKIP — {exc}")
                        continue
                    res = order_router.place(ib, built, armed=True)
                placed_fills.extend(res.get("fills", []))

        # [8] Reconcile each account to model (read-only readout, in-process).
        print("\n[8] Reconciling fills to model (recon_report)...")
        try:
            import recon_report
            recon_plans = []
            for info in sorted(clients, key=lambda x: x.number):
                positions = {p.contract.symbol: p.position
                             for p in ib.positions(info.number) if p.position != 0}
                recon_plans.append(recon_report.plan_account(
                    info.number, info.version, info.net_liq, positions,
                    targets[info.version]))
            for p in recon_plans:
                state = "REBALANCE (still drifted)" if p.needs_rebalance else "in-band"
                print(f"    {p.account} [{p.version}] -> {state}")
        except Exception as exc:
            print(f"    (reconcile readout skipped: {exc})")

        _ledger(armed, account_inputs, routes, placed_fills, backup_path,
                halted=False, halt_reason="")
        print("\nDone. ARMED run complete. Review fills + reconciliation above; DISARM the "
              "gateway (arming.disarm()) when finished.")
        return 0
    finally:
        ib.disconnect()
        print("Session closed.")


def _fa_block_limit(route, quotes, account_inputs, targets) -> float:
    """The limit price for an FA-BLOCK route. Approach b: when config.FA_BLOCK_MARKETABLE
    is on (default), use the MARKETABLE cap from the live quote (BUY ask*(1+k) /
    SELL bid*(1-k), via live_quotes.marketable_cap with ORDER_CAP_K) so a thin-book block
    leg (e.g. TFLO) actually crosses — the fix for the legs that didn't fill at the neutral
    reference. Liquid block legs get ~touch (harmless). If no usable quote is available (or
    the flag is off), fall back to the neutral reference (prices_for), exactly as before.

    The returned value is rounded to a cent and STILL passes through order_router's HARD
    PRICE GUARD inside build_fa_block — a NaN/<=0 can never be sent. This function never
    returns a NaN: prices_for itself falls back to the tier close, and the guard catches
    any residual bad value at build time."""
    if config.FA_BLOCK_MARKETABLE:
        q = quotes.get(route.symbol) if quotes else None
        if q is not None:
            cap = live_quotes.marketable_cap(route.side, q)   # same logic as the direct ladder
            if cap is not None and cap == cap and cap > 0:
                return round(float(cap), 2)
        # No usable quote -> fall back to the neutral reference (then the guard still applies).
        print(f"      (FA block {route.symbol}: no usable quote for marketable cap — "
              f"falling back to neutral reference price)")
    return round(float(rebalance_run.prices_for(account_inputs, targets, route)), 2)


def _ladder_caps(side: str, q) -> dict | None:
    """Compute the per-rung worst-case cap prices for a DIRECT laddered leg from the live
    quote. Every rung order_type maps to the same marketable cap (BUY ask*(1+k) /
    SELL bid*(1-k)); MIDPRICE/Adaptive use it as their lmtPrice worst-case, REL as auxPrice,
    marketable_limit as the limit. Returns None if no usable price exists (caller falls
    back to the legacy capped-limit path), so a missing quote can never build a NaN cap."""
    if q is None:
        return None
    cap = live_quotes.marketable_cap(side, q)
    if cap is None or cap != cap or cap <= 0:
        return None
    return {"marketable_limit": cap, "midprice": cap, "adaptive": cap, "rel": cap}


def _place_direct_laddered(ib, route, q, as_of, armed: bool):
    """Place ONE direct route through the laddered router. Returns the ladder result, or
    None to signal the caller to fall back to the legacy single-limit path (quote
    unusable for caps). Classification is data-driven via order_router.classify_instrument
    using the live relative spread; the per-rung PRICE GUARD still validates every cap."""
    caps = _ladder_caps(route.side, q)
    if caps is None:
        print("      (no usable quote for ladder caps — using legacy capped-limit path)")
        return None
    rel_spread = live_quotes.relative_spread(q) if q else None
    instrument_class = order_router.classify_instrument(
        route.symbol, sec_type="STK", relative_spread=rel_spread)
    order_ref = order_router._order_ref(route.account, as_of, route.side, route.symbol)
    return order_router.place_laddered(
        ib, symbol=route.symbol, side=route.side, total_qty=route.total_qty, caps=caps,
        instrument_class=instrument_class, account=route.account, order_ref=order_ref,
        armed=armed)


def _build_all(ib, routes, account_inputs, targets, quotes=None):
    """Build every route's order object (build-only) for the DRY-RUN log. order_router's
    HARD PRICE GUARD rejects NaN/<=0 limits; a rejected route is logged and skipped, never
    built blank. FA-block legs use the SAME marketable limit the armed path would send
    (_fa_block_limit), so the dry review reflects the real price; direct legs keep the
    neutral reference here (the laddered caps are computed at place-time, not build-time)."""
    as_of = next(iter(targets.values())).as_of
    built = []
    for r in routes:
        try:
            if r.route == "fa_block":
                limit = _fa_block_limit(r, quotes, account_inputs, targets)
                built.append(order_router.build_fa_block(
                    r.symbol, r.side, r.total_qty, limit, r.fa_group, r.fa_method,
                    as_of, ib=ib))
            else:
                limit = round(float(rebalance_run.prices_for(account_inputs, targets, r)), 2)
                intent = rebalance_run._DirectIntent(r.symbol, r.side, r.total_qty, limit)
                built.extend(order_router.build([intent], r.account, as_of, ib=ib))
        except ValueError as exc:
            print(f"    PRICE GUARD skipped a route: {exc}")
    return built


def _ledger(armed, account_inputs, routes, fills, backup_path, halted, halt_reason):
    """Append one audit record for this run (dry or armed)."""
    permit, why = gate_state(armed)
    ledger.record_run({
        "mode": "REBALANCE_EXEC_ARMED" if permit else "REBALANCE_EXEC_DRYRUN",
        "account": "ALL_ENROLLED", "nav": 0.0, "daily_pnl": 0.0,
        "target_as_of": "n/a", "target_weights": {},
        "intents": [{"route": r.route, "side": r.side, "symbol": r.symbol,
                     "qty": r.total_qty, "group": r.fa_group, "account": r.account,
                     "split": r.per_account_split} for r in routes],
        "n_intents": len(routes), "n_approved": len(routes),
        "n_transmitted": len(fills), "fills": fills,
        "fa_backup": backup_path, "halted": halted, "halt_reason": halt_reason,
        "order_vetoes": [], "batch_vetoes": [],
        "gate": {"readonly": config.READONLY, "dry_run": config.DRY_RUN,
                 "armed": armed, "permitted": permit, "why": why},
        **version.stamp(),
    })


# --- entry point ---------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    print("=" * 92)
    print(f"MULTI-ACCOUNT REBALANCE EXECUTOR (transmit-CAPABLE)   [{version.banner()}]")
    print("=" * 92)

    token_present = arm_requested(argv)
    # CONDITION (4): only the exact token authorizes flipping the in-process safety flags.
    # We flip READONLY/DRY_RUN in memory ONLY (config on disk stays True/True), exactly
    # like live_fill_test.py. armed=True is set ONLY alongside the token — never defaulted.
    armed = False
    if token_present:
        config.READONLY = False
        config.DRY_RUN = False
        armed = True   # condition (3): a human passed the arm token = armed

    _safety_banner(armed, token_present)

    # Optional SCOPE — narrows the run to one account/tier. FAIL CLOSED on a bad value.
    try:
        only_account, only_tier = parse_scope(argv)
        validate_scope(only_account, only_tier)
    except ValueError as exc:
        print(f"\nSCOPE ERROR: {exc}")
        return 2
    if only_account or only_tier:
        scope_desc = ", ".join(p for p in (
            f"{SCOPE_ACCOUNT_FLAG} {only_account}" if only_account else "",
            f"{SCOPE_TIER_FLAG} {only_tier}" if only_tier else "") if p)
        print(f"\nSCOPE: this run is restricted to [{scope_desc}]. Out-of-scope FA-block "
              f"tiers will NOT be touched (no replaceFA, no block orders).")

    if not token_present:
        print("\nNo arm token -> DRY-RUN review (read-only outcome; identical to "
              "rebalance_run). To transmit on Monday, re-run with the exact token:")
        print(f"    {ARM_TOKEN}")

    try:
        return execute_armed(armed, only_account=only_account, only_tier=only_tier)
    except KeyboardInterrupt:
        print("\nInterrupted — disarm the gateway (arming.disarm()) if you armed it.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
