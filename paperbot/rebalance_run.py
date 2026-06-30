"""
rebalance_run.py — multi-account REBALANCE RUNNER with a hard review->arm->transmit gate.

This is the orchestration layer that ties together the pure offline engine
(rebalance_engine.build_plan) and the live, arm-gated order_router, behind exactly the
same READONLY + DRY_RUN + armed safety as the rest of the paperbot. It does two things:

  * build_preview(account_inputs, targets) — a PURE function (no broker, no I/O) that
    runs rebalance_engine.build_plan and prints a full, reviewable report: per account
    (tier, NetLiq, investable, target shares + $ per symbol), then the per-tier BLOCKS
    (symbol/side/total_qty + the per-account ContractsOrShares split), then the routes
    (fa_block vs direct). This is the SHAPE of the rebalance, with nothing transmitted.

  * main() — the LIVE path (used Monday). It REQUIRES config.READONLY and config.DRY_RUN
    for dry-run mode, connects read-only pinned to a DU sub-account (master account-stream
    hang avoidance), discovers accounts, reads live NetLiq + positions, sizes off live
    quotes (falling back to the strategy close), computes each tier's model once, prints
    the preview, RESOLVES version->FA group by reading the live groups via requestFA(1)
    (FAILING CLOSED on any ambiguity), builds the FA-block / direct orders BUILD-ONLY, and
    calls order_router.place(..., armed=False) so it logs but transmits nothing.

HARD SAFETY (unchanged from the rest of the engine): transmission is impossible unless
config.READONLY is False AND config.DRY_RUN is False AND a human passes armed=True. There
is NO auto-arm anywhere in this file. The runner connects read-only (physically cannot
transmit) and never writes FA config (no replaceFA) — setting each group's
ContractsOrShares to the computed split is a serialized, human admin step OUTSIDE this file.

Reuses, never duplicates: strategy_target.current_target (tier models), accounts.discover
(+ ib.positions) for live state, live_quotes.fetch for sizing/limit prices,
rebalance_engine.build_plan for the whole plan->block->route pipeline, and
order_router.build_fa_block / build / place for the (build-only) order objects.

Run (gateway auto-starts if down):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\paperbot\\rebalance_run.py"
"""
from __future__ import annotations

import sys

import accounts
import config
import live_quotes
import order_router
import strategy_target
import version
from connections import clientids, ibkr
from gateway_lock import GatewayBusyRefuse, gateway_lock
from rebalance_engine import build_plan


# --- preview (PURE — no broker, no I/O beyond printing) ------------------------
def build_preview(account_inputs: list[dict], targets: dict,
                  band_pct: float | None = None,
                  tier_groups: dict | None = None) -> dict:
    """Run the engine and print the full per-account + per-block + per-route report.

    PURE: calls rebalance_engine.build_plan only (which touches no broker), then prints.
    Returns the same {"plans", "blocks", "routes"} dict build_plan returns so a caller
    can keep working with it. Nothing is built as an order object and nothing is sent."""
    out = build_plan(account_inputs, targets, band_pct=band_pct, tier_groups=tier_groups)
    plans, blocks, routes = out["plans"], out["blocks"], out["routes"]

    band = config.REBALANCE_BAND_PCT if band_pct is None else band_pct
    reserve_pct = config.RISK_LIMITS["cash_reserve_pct"]

    # --- Section A: per-account target book (shares + $) ---
    print("\n" + "=" * 92)
    print(f"PER-ACCOUNT TARGET BOOK   band=+/-{band*100:.0f}% of NAV   "
          f"cash_reserve={reserve_pct*100:.0f}%")
    print("=" * 92)
    for p in plans:
        action = "REBALANCE" if p.needs_rebalance else "in-band (no trades)"
        print(f"\n  {p.account}  [{p.version}]   NetLiq={p.net_liq:>14,.0f}   "
              f"reserve={p.reserve:>10,.0f}   investable={p.investable:>14,.0f}   -> {action}")
        # Only the model holdings (weight>0) plus any held line; show target shares + $.
        rows = [ln for ln in p.lines if ln.target_weight > 0 or ln.actual_shares != 0]
        if not rows:
            print("      (no target holdings)")
            continue
        print(f"      {'SYM':6s} {'TGT_W':>7s} {'TGT_SH':>8s} {'TGT_$':>15s} "
              f"{'CUR_SH':>9s} {'DELTA_SH':>9s}")
        print("      " + "-" * 62)
        for ln in sorted(rows, key=lambda l: -l.target_weight):
            price = float(p_price(targets, p.version, ln.symbol))
            tgt_dollars = ln.target_shares * price if price == price else float("nan")
            delta = p.orders.get(ln.symbol, ln.target_shares - int(ln.actual_shares))
            shown_delta = p.orders.get(ln.symbol, 0)   # only nonzero if the account rebalances
            print(f"      {ln.symbol:6s} {ln.target_weight*100:>6.2f}% {ln.target_shares:>8d} "
                  f"{tgt_dollars:>15,.0f} {ln.actual_shares:>9,.0f} {shown_delta:>+9d}")

    # --- Section B: per-tier BLOCKS (the per-account ContractsOrShares split) ---
    print("\n" + "=" * 92)
    print("BLOCK ORDERS (one average price per block; per-account split = each tier "
          "group's ContractsOrShares)")
    print("=" * 92)
    if not blocks:
        print("  none — every enrolled account is within the drift band. No trades.")
    else:
        cur_tier = None
        for b in blocks:
            if b.version != cur_tier:
                cur_tier = b.version
                print(f"\n  [{cur_tier}]")
            split = "  ".join(f"{a}:{q}" for a, q in sorted(b.per_account.items()))
            print(f"    {b.side:4s} {b.symbol:6s} x{b.total_qty:<7d}  split-> {split}")
        print(f"\n  {len(blocks)} block(s) across "
              f"{sum(1 for p in plans if p.needs_rebalance)} account(s) needing rebalance.")

    # --- Section C: routes (fa_block vs direct) ---
    print("\n" + "=" * 92)
    print("ROUTES (>=2 accounts -> FA block w/ faMethod=''; exactly 1 -> direct true-up)")
    print("=" * 92)
    if not routes:
        print("  none.")
    else:
        for r in routes:
            if r.route == "fa_block":
                split = "  ".join(f"{a}:{q}" for a, q in sorted(r.per_account_split.items()))
                print(f"    fa_block  {r.side:4s} {r.symbol:6s} x{r.total_qty:<7d}  "
                      f"group={r.fa_group}  faMethod='{r.fa_method}'  split-> {split}")
            else:
                print(f"    direct    {r.side:4s} {r.symbol:6s} x{r.total_qty:<7d}  "
                      f"account={r.account}")
    return out


def p_price(targets: dict, version: str, symbol: str) -> float:
    """Latest price for (version, symbol) from that tier's Target — for $ valuation only."""
    t = targets.get(version)
    if t is None:
        return float("nan")
    return float(t.prices.get(symbol, float("nan")))


# --- LIVE helpers (Monday path) ------------------------------------------------
def _net_liq(summary, account: str) -> float | None:
    for row in summary:
        if row.account == account and row.tag == "NetLiquidation":
            return float(row.value)
    return None


def _targets_by_version() -> dict:
    """Run the validated engine ONCE per distinct enrolled version (the model per risk
    tier). Read-only compute, identical code path to the backtest."""
    out = {}
    for v in sorted(set(config.ENROLLMENT.values())):
        out[v] = strategy_target.current_target(version=v)
    return out


def _parse_fa_group_members(xml: str) -> dict:
    """Parse the requestFA(1) GROUPS xml into {group_name: set(member_accounts)}.

    Uses the stdlib XML parser; tolerant of the exact tag casing IBKR emits (Group/name/
    ListOfAccts/Account or String). Returns {} if nothing parseable, so the caller fails
    closed rather than guessing a group name."""
    import xml.etree.ElementTree as ET

    groups: dict[str, set] = {}
    if not xml or not str(xml).strip():
        return groups
    try:
        root = ET.fromstring(str(xml))
    except Exception:
        return groups
    # Find every <Group> element regardless of nesting/namespace.
    for grp in root.iter():
        tag = grp.tag.split("}")[-1].lower()
        if tag != "group":
            continue
        name = None
        members: set = set()
        for child in grp.iter():
            ctag = child.tag.split("}")[-1].lower()
            text = (child.text or "").strip()
            if ctag == "name" and text:
                name = text
            elif ctag in ("account", "acct", "string") and text:
                members.add(text)
        if name:
            groups[name] = members
    return groups


def resolve_tier_groups(ib, enrolled_versions: set) -> dict:
    """Read the LIVE FA groups (requestFA(1)) and resolve each enrolled version to the
    UNIQUE group whose membership exactly matches that tier's enrolled accounts.

    FAILS CLOSED: raises RuntimeError if any tier cannot be mapped to exactly one group
    by membership. We never hard-code group names — the gateway's actual names win, and
    ambiguity stops the run rather than risking an order against the wrong group.

    Returns {version: group_name} for use as build_plan's tier_groups override."""
    members_by_version: dict[str, set] = {}
    for acct, ver in config.ENROLLMENT.items():
        members_by_version.setdefault(ver, set()).add(acct)

    xml = ib.requestFA(1)   # 1 = GROUPS (read-only; transmits nothing)
    live_groups = _parse_fa_group_members(xml)
    if not live_groups:
        raise RuntimeError("requestFA(1) returned no parseable FA groups — cannot resolve "
                           "tier groups by membership. FAILING CLOSED (no order built).")

    resolved: dict[str, str] = {}
    for ver in sorted(enrolled_versions):
        want = members_by_version.get(ver, set())
        matches = [g for g, mem in live_groups.items() if mem == want]
        if len(matches) != 1:
            raise RuntimeError(
                f"tier '{ver}' (accounts {sorted(want)}) did not map to exactly one live FA "
                f"group by membership (candidates={matches}; live groups="
                f"{ {g: sorted(m) for g, m in live_groups.items()} }). FAILING CLOSED.")
        resolved[ver] = matches[0]
    return resolved


def _safety_banner(armed: bool) -> None:
    permit, why = order_router.transmit_guard(armed)
    print("\n" + "#" * 92)
    print(f"# SAFETY STATE   READONLY={config.READONLY}   DRY_RUN={config.DRY_RUN}   "
          f"armed={armed}")
    print(f"# transmission: {'PERMITTED' if permit else 'BLOCKED'} ({why})")
    print("# This runner connects READ-ONLY and is BUILD-ONLY. It writes no FA config "
          "(no replaceFA).")
    print("# Setting each group's ContractsOrShares to the split, and arming, are separate "
          "human steps.")
    print("#" * 92)


def main(armed: bool = False) -> int:
    """LIVE dry-run path. Requires READONLY + DRY_RUN. Builds orders but transmits nothing
    (place(armed=False) and the read-only connection both block transmission). `armed` is
    NEVER set True here — it is exposed only so the guard's full shape is visible; flipping
    it has no effect while READONLY/DRY_RUN hold."""
    print("=" * 92)
    print(f"MULTI-ACCOUNT REBALANCE RUNNER — DRY RUN (build-only, transmits nothing)   "
          f"[{version.banner()}]")
    print("=" * 92)
    _safety_banner(armed)

    if not (config.READONLY and config.DRY_RUN):
        print("\nSAFETY STOP: this runner requires READONLY=True and DRY_RUN=True in config.")
        return 2

    # [1] Tier models BEFORE connecting (fail fast if data is stale).
    print("\n[1] Computing tier models (one per distinct enrolled version)...")
    targets = _targets_by_version()
    for v, t in targets.items():
        print(f"    {v:13s} as_of={t.as_of.date()}  price_date={t.price_date.date()}  "
              f"({len(t.weights)} holdings)")

    # GATEWAY LOCK (Slice 3): acquire the single-process Gateway mutex BEFORE connecting and
    # hold it through the ENTIRE session (connect -> build -> disconnect). The rebalance is the
    # value-bearing, human-supervised path, so it INSISTS: on_busy="refuse" waits a short
    # bounded time then, if still held, REFUSES — naming the holder — and ABORTS before any
    # connect or order work. Never operate the Gateway blind into a contended session.
    try:
        with gateway_lock(purpose="rebalance_run",
                          client_id=clientids.get("paperbot_rebalance"), on_busy="refuse"):
            return _run_gateway_session(armed, targets)
    except GatewayBusyRefuse as busy:
        holder = busy.holder or {}
        print(f"\n[2] REFUSING to start the rebalance — gateway held by "
              f"{holder.get('purpose')} pid {holder.get('pid')} clientId "
              f"{holder.get('client_id')} since "
              f"{holder.get('acquired_at') or holder.get('acquired_ts')}. No connection "
              f"opened, NO orders built, nothing transmitted, no FA config written. Re-run "
              f"once the holder finishes.")
        return 2


def _run_gateway_session(armed: bool, targets: dict) -> int:
    """The connect -> build -> disconnect body, run only while the gateway lock is HELD.

    Factored out of main() so the `with gateway_lock(...)` block wraps the WHOLE session, not
    just the connect call — the lock is held across connect, the live reads, the build, and
    place(armed=False), then released after disconnect."""
    # [2] Connect read-only, PINNED to a DU sub-account so the master account stream
    # (DF...141) never hangs the session. We pick the lowest-numbered enrolled DU account.
    pin_account = sorted(config.ENROLLMENT)[0]
    print(f"\n[2] Connecting to PAPER gateway {ibkr.HOST}:{ibkr.PAPER_PORT} "
          f"(clientId={clientids.get('paperbot_rebalance')}, readonly=True, "
          f"pinned to {pin_account})...")
    try:
        ib = ibkr.connect("paperbot_rebalance", readonly=True, launch=True)
    except Exception as exc:
        print(f"    COULD NOT CONNECT: {exc}")
        return 1

    try:
        # [3] Discover accounts; build account_inputs for every enrolled+funded sub.
        infos = accounts.discover(ib)
        clients = [i for i in infos if i.enrolled and i.funded and not i.is_master]
        if not clients:
            print("\n    No enrolled + funded client accounts to rebalance. Done.")
            return 0

        # Sizing universe: every symbol any tier wants (one quote pull for all).
        universe = sorted({s for t in targets.values() for s in t.weights.index})
        print(f"\n[3] Fetching live quotes for {len(universe)} symbol(s) "
              "(read-only market data, fall back to strategy close)...")
        quotes = live_quotes.fetch(ib, universe)
        n_live = sum(1 for q in quotes.values() if q.md_type == 1)
        print(f"    {len(quotes)} quoted; {n_live} live, {len(quotes) - n_live} delayed/unavailable.")

        account_inputs: list[dict] = []
        for info in sorted(clients, key=lambda x: x.number):
            positions = {p.contract.symbol: p.position
                         for p in ib.positions(info.number) if p.position != 0}
            # Live reference price per symbol if available, else the tier's strategy close.
            tier_prices = targets[info.version].prices
            prices = {}
            for sym in set(tier_prices.index) | set(positions):
                q = quotes.get(sym)
                ref = live_quotes.reference_price(q) if q else None
                prices[sym] = ref if (ref and ref > 0) else float(tier_prices.get(sym, float("nan")))
            account_inputs.append({
                "account": info.number, "version": info.version,
                "net_liq": info.net_liq, "positions": positions, "prices": prices})

        # [4] Resolve version->FA group by LIVE membership (fail closed on ambiguity).
        print("\n[4] Resolving version->FA group via requestFA(1) (membership match, "
              "fail-closed)...")
        enrolled_versions = {i.version for i in clients}
        try:
            tier_groups = resolve_tier_groups(ib, enrolled_versions)
        except RuntimeError as exc:
            print(f"    {exc}")
            print("    -> No orders built. Resolve the FA group names/membership and re-run.")
            return 2
        for v, g in sorted(tier_groups.items()):
            print(f"    {v:13s} -> group '{g}'")

        # [5] PREVIEW (pure) — using the LIVE-resolved groups.
        out = build_preview(account_inputs, targets, tier_groups=tier_groups)
        routes = out["routes"]

        # [6] BUILD the order objects (build-only) and place(armed=False) -> logs, no send.
        print("\n" + "=" * 92)
        print("[6] Building order objects (build-only) and routing through "
              "order_router.place(armed=False)")
        print("=" * 92)
        if not routes:
            print("    no routes — nothing to build.")
        as_of = next(iter(targets.values())).as_of
        built = []
        for r in routes:
            limit = round(float(prices_for(account_inputs, targets, r)), 2)
            if r.route == "fa_block":
                # fa_method="" on purpose — the GROUP's ContractsOrShares (== split) governs.
                # NOTE: never what_if a group order (it hangs). Build only.
                bo = order_router.build_fa_block(r.symbol, r.side, r.total_qty, limit,
                                                 r.fa_group, r.fa_method, as_of, ib=ib)
                built.append(bo)
            else:
                # direct single-account true-up via order_router.build (duck-typed intent).
                intent = _DirectIntent(r.symbol, r.side, r.total_qty, limit)
                built.extend(order_router.build([intent], r.account, as_of, ib=ib))

        # place() logs every constructed order and, because READONLY+DRY_RUN hold and
        # armed=False, transmits NOTHING (guard fails closed).
        order_router.place(ib, built, armed=armed)

        print("\nDone. READ-ONLY + DRY_RUN: orders were BUILT and LOGGED, nothing was "
              "transmitted, no FA config was written.")
        return 0
    finally:
        ib.disconnect()
        print("Read-only session closed.")


class _DirectIntent:
    """Minimal duck-typed intent for order_router.build (needs symbol/side/quantity/
    limit_price). A direct true-up touches a single account."""
    def __init__(self, symbol: str, side: str, quantity: int, limit_price: float):
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.limit_price = limit_price


def prices_for(account_inputs: list[dict], targets: dict, route) -> float:
    """Limit-price reference for a route's symbol: the live/close price the engine sized
    against. For a block, any member account's price map has it (sizes are identical
    inputs); for a direct, that account's. Falls back to the tier close."""
    for a in account_inputs:
        if route.account is None or a["account"] == route.account:
            px = a.get("prices", {}).get(route.symbol)
            if px and px == px:
                return px
    return p_price(targets, route.version, route.symbol)


if __name__ == "__main__":
    sys.exit(main())
