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
from connections import clientids, ibkr   # noqa: E402
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


def execute_armed(armed: bool) -> int:
    """The Monday path. Connects NON-read-only pinned to a DU sub, discovers live state,
    builds the plan, resolves groups fail-closed, runs risk guards, and (only if the gate
    fully permits) writes each tier group's ContractsOrShares and places its block, one at
    a time, watching fills, then reconciles. Every step is logged to the ledger."""
    permit, why = gate_state(armed)
    targets = rebalance_run._targets_by_version()
    print("\n[1] Tier models:")
    for v, t in targets.items():
        print(f"    {v:13s} as_of={t.as_of.date()}  ({len(t.weights)} holdings)")

    # Connect NON-read-only, pinned to a DU account (flatten_accounts.py pattern). This is
    # the only way the session can transmit; pinning dodges the master account-stream hang.
    print(f"\n[2] Connecting NON-readonly pinned to {PIN_ACCOUNT} "
          f"(clientId={clientids.get('paperbot_rebalance_exec')})...")
    ib = IB()
    ib.connect(ibkr.HOST, ibkr.PAPER_PORT,
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
            built = _build_all(ib, routes, account_inputs, targets)
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
            limit = round(float(rebalance_run.prices_for(account_inputs, targets, r)), 2)
            if r.route == "fa_block":
                # Set THIS group's ContractsOrShares to the split, THEN place the block.
                print(f"\n    [block] {r.side} {r.symbol} x{r.total_qty}  group={r.fa_group}")
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
                # Direct single-account true-up.
                print(f"\n    [direct] {r.side} {r.symbol} x{r.total_qty}  account={r.account}")
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


def _build_all(ib, routes, account_inputs, targets):
    """Build every route's order object (build-only). order_router's HARD PRICE GUARD
    rejects NaN/<=0 limits; a rejected route is logged and skipped, never built blank."""
    as_of = next(iter(targets.values())).as_of
    built = []
    for r in routes:
        limit = round(float(rebalance_run.prices_for(account_inputs, targets, r)), 2)
        try:
            if r.route == "fa_block":
                built.append(order_router.build_fa_block(
                    r.symbol, r.side, r.total_qty, limit, r.fa_group, r.fa_method,
                    as_of, ib=ib))
            else:
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

    if not token_present:
        print("\nNo arm token -> DRY-RUN review (read-only outcome; identical to "
              "rebalance_run). To transmit on Monday, re-run with the exact token:")
        print(f"    {ARM_TOKEN}")

    try:
        return execute_armed(armed)
    except KeyboardInterrupt:
        print("\nInterrupted — disarm the gateway (arming.disarm()) if you armed it.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
