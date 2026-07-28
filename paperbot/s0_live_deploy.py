"""
s0_live_deploy.py — the S0 GROWTH-tier full-account DEPLOY executor (real-transmission,
explicit --conform mode).

WHAT THIS IS
------------
The tiny-test executor (s0_live_exec.py) proves the review -> arm -> transmit path with ONE
1-share order. This is its DEPLOY sibling: it takes a funded account holding ANYTHING and
rebalances it FULLY into the S0 GROWTH target — including SELLING holdings that are outside
the S0 universe (a corp-action / manual position the ongoing rebalancer deliberately leaves
alone). It is pinned to the trust account U14438624 on the Live-Trade Gateway (port 4003),
the same account + gateway the tiny-test uses, and it reuses every one of the tiny-test's
safety idioms verbatim.

WHY A NEW MODE (--conform)
--------------------------
rebalance_engine.plan_account marks a held symbol that is NOT in the S0 universe ALIEN and
NEVER emits a sell for it (the correct default for an ongoing rebalance — a spinoff/rename is
not churned into a taxable round-trip). That default is WRONG for a deliberate one-time
deploy where the whole point is to conform ANY existing book to the target. So this executor
adds an EXPLICIT opt-in: with --conform, every ALIEN line becomes a full-liquidation SELL
(to 0). Without --conform, ALIEN holdings are left untouched and the preview lists them as
"would remain". The ALIEN guard is NOT removed — it is opted out of, deliberately, per run.

DEFAULT IS SAFE. With no flag it runs a PREVIEW: it sizes the plan, builds the full ordered
order list (sells first, then buys), prints exactly what WOULD be transmitted, and sends
nothing. To actually transmit, a human must line up ALL of:
  * the exact CLI token  --arm-i-understand  (sets armed=True; never defaulted/auto-set),
  * the explicit  --conform  flag  (this executor is a conform-deploy tool; transmit requires
    it — separate from the arm token, BOTH required),
  * NO kill-switch sentinel present,
  * the target account is EXACTLY U14438624 (any other refused — single-account wall),
  * every leg whole-share, priced, and through order_router's HARD price guard,
  * total BUY notional <= investable (never over-deploy / no margin), AND no single order's
    notional > 50% of NetLiq (fat-finger / bad-price catch),
  * the Gateway physically ARMED (Read-Only API toggle OFF — measured live with the
    zero-transmission cancel-a-fabricated-order probe), and
  * the pre-transmit dedup gate (order_router.already_present) says EVERY leg is FRESH.
Miss ANY one and the run is a preview that transmits nothing and prints WHY.

There is NO auto-arm, and nothing here is scheduled. A human runs it, reviews the preview,
arms the Gateway by hand, and re-runs with the tokens to fire the deploy.

Run — PREVIEW with the conform list (default; transmits nothing):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe s0_live_deploy.py --conform

Run — ARMED conform deploy (human-supervised; requires an armed Gateway + no kill switch):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe s0_live_deploy.py --conform --arm-i-understand
"""
from __future__ import annotations

import os
import sys
import threading
import time

from ib_async import Stock

import config
import live_quotes
import order_router
import rebalance_engine
import s0_live
import strategy_target
import version
# Reuse, don't reimplement: the read-only pilot already has the NetLiq parse and the
# strategy-universe accessor (for ROTATE_OUT vs ALIEN classification). Importing it runs no
# broker connection at import time.
import s0_live_pilot_run as sp

# ----------------------------------------------------------------------------------------
# SAFETY CONSTANTS — enforced in CODE below (the docstring is not the wall).
# ----------------------------------------------------------------------------------------
# The single account this DEPLOY executor may ever transmit on — the funded, PDT-clear trust
# account (same account the tiny-test targets). Any OTHER account is refused (single-account
# wall, identical in kind to s0_live_exec's).
EXEC_ACCOUNT = "U14438624"
ALLOWED_ACCOUNT = "U14438624"

# The risk tier to deploy. Andrew specified GROWTH (2026-07-28) — NOT the Balanced default.
# Growth: equity_allowance 1.00, tbill_floor 0.00, real-asset sleeve 0.20 (strategies/config
# CLIENT_VERSIONS). Passed explicitly to strategy_target.current_target(version=...) so the
# deploy can never silently fall back to the config.STRATEGY_VERSION (Balanced) default.
DEPLOY_VERSION = "Growth"

# The exact arm token — typed in full, no abbreviation, no default (mirrors
# rebalance_execute.ARM_TOKEN / s0_live_exec.ARM_TOKEN).
ARM_TOKEN = "--arm-i-understand"
# The explicit conform flag — turns ALIEN (non-S0) holdings into full-liquidation SELLs AND
# is a REQUIRED transmit gate (this executor is a conform-deploy tool; without it, transmit
# nothing). Separate from the arm token — BOTH are required to liquidate + transmit.
CONFORM_FLAG = "--conform"

# KILL SWITCH — same sentinel s0_live_exec / morning_execute_run honor. Mirrored as a literal
# so this module pulls in none of their module-level state; if the file exists (any content)
# this run is preview-only.
KILL_SWITCH = r"C:\TradingDesk-Local\AUTOTRADE_DISABLED"

# NOTIONAL SANITY CAPS (sized for a real deploy, not a tiny test):
#   * total BUY notional must be <= the plan's investable (never over-deploy / no margin);
#   * no single order's notional may exceed this fraction of NetLiq (fat-finger / bad-price
#     backstop — a good price for a whole-book deploy leg is well under half the account).
MAX_ORDER_NOTIONAL_PCT_NLV = 0.50


def arm_requested(argv: list[str]) -> bool:
    """True ONLY if the exact arm token is present in argv — the single thing that sets
    armed=True. Mirrors s0_live_exec.arm_requested."""
    return ARM_TOKEN in argv


def conform_requested(argv: list[str]) -> bool:
    """True ONLY if the exact --conform flag is present. Turns ALIEN holdings into
    liquidation SELLs AND is a required transmit gate."""
    return CONFORM_FLAG in argv


def _kill_switch_present() -> bool:
    """True if the AUTOTRADE_DISABLED sentinel exists -> force preview-only."""
    return os.path.exists(KILL_SWITCH)


def _account_safety_ok() -> tuple[bool, str]:
    """Constant-level account guard: EXEC_ACCOUNT must be EXACTLY the single ALLOWED_ACCOUNT
    and no other. Read at call time so a test/monkeypatch of EXEC_ACCOUNT is honored. Hard
    single-account wall — identical in kind to s0_live_exec's."""
    if EXEC_ACCOUNT != ALLOWED_ACCOUNT:
        return False, (f"target account {EXEC_ACCOUNT} is not the single allowed account "
                       f"{ALLOWED_ACCOUNT} — refusing.")
    return True, ""


def _probe_gateway_readonly(ib, timeout: int = 15) -> bool:
    """Return True if the OPEN live-trade (4003) connection's Gateway is READ-ONLY
    (transmission physically BLOCKED), False if it is WRITE-ENABLED (armed).

    Mirrors arming.probe_api_readonly's ZERO-TRANSMISSION technique EXACTLY (identical to
    s0_live_exec._probe_gateway_readonly) — attach an error handler, ask the Gateway (via the
    RAW client call) to cancel a fabricated, never-placed orderId, and read the decisive
    reply:
      * Read-Only API -> code 321 / "read-only mode"                     -> True  (blocked)
      * Write-enabled -> 10147/10148 / "not found"/"cannot be cancelled" -> False (armed)
    No order is ever placed or rested. FAILS CLOSED: no decisive signal -> True (refuse)."""
    signal: dict[str, bool] = {}
    got = threading.Event()

    def on_error(reqId, errorCode, errorString, *_):
        msg = (errorString or "").lower()
        if "read-only mode" in msg or "read only mode" in msg or errorCode == 321:
            signal["readonly"] = True
            got.set()
        elif (errorCode in (10147, 10148) or "not found" in msg
              or "cannot be cancelled" in msg):
            signal["readonly"] = False
            got.set()

    ib.errorEvent += on_error
    try:
        oid = ib.client.getReqId()
        ib.client.cancelOrder(oid, "")   # transmits nothing; no such order exists
        deadline = time.time() + timeout
        while not got.is_set() and time.time() < deadline:
            ib.sleep(0.2)
    finally:
        try:
            ib.errorEvent -= on_error
        except Exception:
            pass
    if "readonly" not in signal:
        # Could not measure the Gateway state -> treat as read-only (refuse to transmit).
        return True
    return signal["readonly"]


def _buying_power(summary) -> float | None:
    """Best-effort BuyingPower off the (already account-FILTERED) accountSummary rows."""
    for row in summary:
        if getattr(row, "tag", None) == "BuyingPower":
            try:
                return float(row.value)
            except (TypeError, ValueError):
                return None
    return None


def _buying_power_ok(summary, notional: float) -> tuple[bool, str]:
    """Fail-closed buying-power sanity check for the total BUY notional. If BuyingPower is
    readable and below the total buy notional, refuse; if it can't be read, allow (the
    investable cap already bounds deployment). Mirrors s0_live_exec._buying_power_ok."""
    bp = _buying_power(summary)
    if bp is not None and bp < notional:
        return False, (f"buying power {bp:,.2f} < total BUY notional {notional:,.2f} — "
                       f"refusing.")
    return True, ""


def _leg_cap(side: str, symbol: str, quotes: dict, prices: dict) -> float | None:
    """A marketable cap near the quote for `side` (BUY = ask*(1+k), SELL = bid*(1-k)),
    falling back to the merged reference price if there is no live two-sided quote. Returns
    None when no usable price exists at all — the caller treats that leg as UNPRICEABLE and
    refuses the deploy (a full-book deploy must be complete). Re-guarded at build time."""
    q = quotes.get(symbol)
    cap = live_quotes.marketable_cap(side, q) if q is not None else None
    if not (cap and cap == cap and cap > 0):
        ref = prices.get(symbol)
        cap = round(float(ref), 2) if (ref and ref == ref and ref > 0) else None
    if not (cap and cap == cap and cap > 0):
        return None
    return cap


def build_deploy_legs(plan, quotes: dict, prices: dict, *, conform: bool):
    """PURE order-list construction from an already-sized AccountPlan. Builds and transmits
    NOTHING — returns the ordered candidate legs plus review metadata.

    Legs:
      * the engine's plan.orders (signed deltas -> BUY/SELL to reach the target), PLUS
      * CONFORM mode (opt-in): for each plan.alien_lines entry, a SELL of its full
        whole-share count (liquidate to 0). When conform is False, ALIEN holdings produce NO
        leg and are returned in `aliens_left` for the preview to list as "would remain".

    SELLS ARE SEQUENCED BEFORE BUYS (raise cash before buying): the returned `legs` list is
    plan-sells + alien-liquidations + plan-buys, so every SELL precedes every BUY.

    WHOLE-SHARE ONLY: every quantity is an int (deltas are already integer; an alien's
    fractional share count is truncated toward 0 — a sub-1-share alien can't be whole-share
    liquidated and is returned in `aliens_left`).

    Returns (legs, aliens_left, unpriceable):
      legs        : ordered list of SimpleNamespace(symbol, side, qty, limit, notional, source)
      aliens_left : alien lines NOT liquidated (conform False, or a sub-1-share alien)
      unpriceable : list of (symbol, side, qty) with no usable price -> a blocking reason
    """
    from types import SimpleNamespace
    sells: list = []
    buys: list = []
    unpriceable: list = []

    for sym in sorted(plan.orders):
        qty = int(plan.orders[sym])          # whole-share; engine deltas are already integer
        if qty == 0:
            continue
        side = "BUY" if qty > 0 else "SELL"
        qty = abs(qty)
        cap = _leg_cap(side, sym, quotes, prices)
        if cap is None:
            unpriceable.append((sym, side, qty))
            continue
        leg = SimpleNamespace(symbol=sym, side=side, qty=qty, limit=cap,
                              notional=qty * cap, source="plan")
        (buys if side == "BUY" else sells).append(leg)

    alien_sells: list = []
    aliens_left: list = []
    if conform:
        for ln in plan.alien_lines:
            qty = int(ln.actual_shares)      # truncate toward 0 — never fractional
            if qty < 1:
                aliens_left.append(ln)       # sub-1-share alien: can't whole-share liquidate
                continue
            cap = _leg_cap("SELL", ln.symbol, quotes, prices)
            if cap is None:
                unpriceable.append((ln.symbol, "SELL", qty))
                continue
            alien_sells.append(SimpleNamespace(
                symbol=ln.symbol, side="SELL", qty=qty, limit=cap,
                notional=qty * cap, source="alien_liquidation"))
    else:
        aliens_left = list(plan.alien_lines)

    # SELLS (plan sells + alien liquidations) BEFORE BUYS — raise cash first.
    legs = sells + alien_sells + buys
    return legs, aliens_left, unpriceable


def _safety_banner(armed: bool, conform: bool, kill: bool) -> None:
    permit_intent = armed and conform and not kill
    print("\n" + "#" * 88)
    print(f"# SAFETY STATE   armed={armed}   arm_token={'present' if armed else 'absent'}   "
          f"conform={'ON' if conform else 'off'}   "
          f"kill_switch={'PRESENT' if kill else 'absent'}")
    print(f"# account={EXEC_ACCOUNT}   target=S0 {DEPLOY_VERSION} tier   "
          f"gateway=Live-Trade port 4003")
    print(f"#   (single-account wall: refuses ANY account other than {ALLOWED_ACCOUNT})")
    print(f"# CAPS   total BUY notional <= investable   per-order notional <= "
          f"{MAX_ORDER_NOTIONAL_PCT_NLV*100:.0f}% of NetLiq   whole-share   price-guarded   "
          f"dedup FRESH")
    if permit_intent:
        print("# *** ARMED + CONFORM INTENT: this run MAY liquidate non-S0 holdings and")
        print("#     transmit a FULL deploy (sells first, then buys) on a FUNDED account IF")
        print("#     every remaining gate passes. Review the full order list below. ***")
    else:
        print("# PREVIEW: sizes + builds the full ordered order list and prints it —")
        print("# transmits NOTHING (not armed, conform off, or kill switch present).")
    print("#" * 88)


def main(armed: bool = False, conform: bool = False, today: object = None) -> int:
    """DEPLOY executor. PREVIEW by default; transmits the full deploy ONLY when armed AND
    conform AND every gate passes. `today` is accepted for signature parity with the other
    runners; the shared brain always runs to the most recent data date."""
    print("=" * 88)
    print(f"S0 LIVE DEPLOY EXECUTOR ({DEPLOY_VERSION} tier) — preview by default, full "
          f"conform deploy when armed   [{version.banner()}]")
    print("=" * 88)

    # [1] Compute the GROWTH target BEFORE connecting (fail fast on stale data; connect
    # nothing on failure). Explicit version=DEPLOY_VERSION — never the Balanced default.
    print(f"\n[1] Computing the S0 {DEPLOY_VERSION} target (shared brain; stale-data "
          f"guarded)...")
    try:
        target = strategy_target.current_target(version=DEPLOY_VERSION)
    except Exception as exc:
        print(f"    COULD NOT BUILD TARGET: {exc}. Nothing connected, nothing transmitted.")
        return 2
    print(f"    {target.version}   as_of={target.as_of.date()}  "
          f"price_date={target.price_date.date()}  ({len(target.weights)} holdings)")

    kill = _kill_switch_present()
    permit_intent = armed and conform and not kill

    # [2] Safety banner — armed/conform/preview state, account, caps.
    _safety_banner(armed, conform, kill)

    # [3] Connect. ARMED+CONFORM intent -> the transmit-capable lane (readonly=False,
    # clientId s0_live_exec); otherwise the read-only pilot lane (readonly=True). A bare armed
    # connection still transmits nothing on its own — only order_router.place(armed=True) does.
    # Whole session in try/finally so it ALWAYS disconnects.
    if permit_intent:
        print("\n[3] Connecting ARMED (readonly=False) to the Live-Trade Gateway "
              "(port 4003)...")
        try:
            ib = s0_live.connect_s0_live_armed()
        except Exception as exc:
            print(f"    could not connect ARMED to the Live-Trade Gateway (port 4003): "
                  f"{type(exc).__name__}: {exc}. Nothing sized, nothing transmitted.")
            return 1
        armed_conn = True
    else:
        print("\n[3] Connecting READ-ONLY to the Live-Trade Gateway (port 4003) for the "
              "preview...")
        try:
            ib = s0_live.connect_s0_live()
        except Exception as exc:
            print(f"    could not connect READ-ONLY to the Live-Trade Gateway (port 4003): "
                  f"{type(exc).__name__}: {exc}. Nothing sized, nothing transmitted.")
            return 1
        armed_conn = False

    try:
        return _run_session(ib, target, armed=armed, conform=conform,
                            armed_conn=armed_conn, kill=kill)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
        print("Session closed.")


def _run_session(ib, target, *, armed: bool, conform: bool, armed_conn: bool,
                 kill: bool) -> int:
    account = EXEC_ACCOUNT

    # [4] Read + FILTER to EXEC_ACCOUNT (the login also exposes the individual account +
    # an 'All' aggregate). Refuse if the target account is not present under the login.
    print(f"\n[4] Reading account summary + positions, filtering to {account}...")
    summary = s0_live.filter_account_summary(ib.accountSummary(), account=account)
    if not summary:
        print(f"    target account {account} not found under the Live-Trade login — "
              f"REFUSING. Nothing sized, nothing transmitted.")
        return 1
    net_liq = sp._net_liq(summary)
    if not net_liq or net_liq <= 0:
        print(f"    could not read a positive NetLiquidation for {account} — REFUSING.")
        return 1
    positions_raw = s0_live.filter_positions(ib.positions(), account=account)
    positions = {p.contract.symbol: p.position for p in positions_raw if p.position != 0}
    print(f"    account={account}   NetLiq={net_liq:,.2f}   open_positions={len(positions)}")

    # [5] Live prices over the union of target + held symbols (same merge the pilot uses).
    universe = sorted(set(target.weights.index) | set(positions))
    print(f"\n[5] Fetching live quotes for {len(universe)} symbol(s) on port 4003...")
    quotes = live_quotes.fetch(ib, universe)
    prices: dict = {}
    for sym in universe:
        q = quotes.get(sym)
        ref = live_quotes.reference_price(q) if q else None
        prices[sym] = ref if (ref and ref > 0) else float(
            target.prices.get(sym, float("nan")))

    # [6] Size the REAL account with the UNCHANGED engine (identical call to the pilot /
    # tiny-test). ALIEN (non-S0) holdings land on plan.alien_lines; plan.orders has the
    # sells+buys to reach the GROWTH target.
    strat_universe = sp._strategy_universe()
    print("\n[6] Sizing the plan with rebalance_engine.plan_account (UNCHANGED engine)...")
    plan = rebalance_engine.plan_account(account, target.version, net_liq, positions,
                                         target, prices=prices, universe=strat_universe)

    # [7] Build the full ordered DEPLOY order list (sells first, then buys; conform adds the
    # ALIEN liquidations). Whole-share, price-guarded caps.
    legs, aliens_left, unpriceable = build_deploy_legs(plan, quotes, prices, conform=conform)
    total_buy = sum(l.notional for l in legs if l.side == "BUY")
    total_sell = sum(l.notional for l in legs if l.side == "SELL")
    per_order_cap = MAX_ORDER_NOTIONAL_PCT_NLV * net_liq

    print(f"\n[7] DEPLOY order list ({len(legs)} leg(s); sells first, then buys) — "
          f"conform={'ON' if conform else 'off'}:")
    if not legs:
        print("    (no legs — account already conforms to the target, or nothing to trade)")
    for l in legs:
        if l.source == "alien_liquidation":
            note = "LIQUIDATE non-S0 -> 0"
        else:
            tw = float(target.weights.get(l.symbol, 0.0)) * 100.0
            note = f"-> target ~{tw:.2f}%"
        print(f"    {l.side:4s} {l.symbol:6s} x{l.qty:<8d} LIMIT ~{l.limit:>10,.2f}  "
              f"notional ~{l.notional:>12,.2f}  [{l.source}]  {note}")
    print(f"    TOTALS   sells ~{total_sell:,.2f}   buys ~{total_buy:,.2f}   "
          f"investable ~{plan.investable:,.2f}   NetLiq ~{net_liq:,.2f}")
    if aliens_left:
        label = ("non-S0 ALIEN holdings that WOULD REMAIN — pass --conform to liquidate"
                 if not conform else
                 "non-S0 ALIEN holdings left (sub-1-share; can't whole-share liquidate)")
        print(f"    {label}:")
        for ln in aliens_left:
            print(f"      {ln.symbol:6s} qty={ln.actual_shares:,.4f}")

    # [8] PRE-TRANSMIT GATE — collect EVERY blocking reason; transmit only if none remain.
    reasons: list[str] = []
    if not armed:
        reasons.append("not armed (default preview; pass --arm-i-understand to arm)")
    if not conform:
        reasons.append(f"conform intent absent (pass {CONFORM_FLAG}) — this DEPLOY executor "
                       f"requires it to liquidate + transmit")
    if kill:
        reasons.append(f"KILL_SWITCH sentinel present ({KILL_SWITCH})")
    acct_ok, acct_reason = _account_safety_ok()
    if not acct_ok:
        reasons.append(acct_reason)
    if not legs:
        reasons.append("no legs to transmit (nothing to deploy)")
    if unpriceable:
        reasons.append(f"{len(unpriceable)} leg(s) have no usable price (deploy must be "
                       f"complete): {unpriceable}")
    # Per-order sanity cap: no single order's notional may exceed 50% of NetLiq.
    for l in legs:
        if l.notional > per_order_cap:
            reasons.append(f"order {l.side} {l.symbol} x{l.qty} notional {l.notional:,.2f} "
                           f"> {MAX_ORDER_NOTIONAL_PCT_NLV*100:.0f}% of NetLiq "
                           f"({per_order_cap:,.2f})")
    # Total-notional sanity cap: total BUY notional must not exceed investable.
    if total_buy > plan.investable:
        reasons.append(f"total BUY notional {total_buy:,.2f} > investable "
                       f"{plan.investable:,.2f} — would over-deploy / use margin")

    # Connection-dependent gates — only meaningful on the armed (4003 transmit) connection,
    # and only worth probing once the code-level gates above are clean.
    if armed and conform and armed_conn and not reasons:
        if _probe_gateway_readonly(ib):
            reasons.append("Gateway is still READ-ONLY on 4003 (arming.probe idiom) — not "
                           "physically armed; a human must turn the Read-Only API toggle off")
    if armed and conform and armed_conn and not reasons:
        for l in legs:
            ref = order_router._order_ref(account, target.as_of, l.side, l.symbol)
            dedup = order_router.already_present(ib, ref, l.qty)
            if dedup != order_router.LegState.FRESH:
                reasons.append(f"dedup gate says {dedup} (not FRESH) for {l.side} "
                               f"{l.symbol} ref={ref}")
    if armed and conform and armed_conn and not reasons:
        bp_ok, bp_reason = _buying_power_ok(summary, total_buy)
        if not bp_ok:
            reasons.append(bp_reason)

    permit = armed and conform and armed_conn and not kill and not reasons

    # [9] Report + (only if permitted) transmit the full deploy (sells first).
    if not permit:
        primary = ("not armed" if not armed
                   else "conform off" if not conform
                   else "kill switch present" if kill
                   else (reasons[0] if reasons else "gate not satisfied"))
        print("\n[9] TRANSMISSION BLOCKED — PREVIEW ONLY. Reason(s):")
        for r in reasons:
            print(f"      - {r}")
        print(f"\n    WOULD TRANSMIT {len(legs)} leg(s) (sells first, then buys) on "
              f"{account}. Nothing was transmitted.")
        print(f"\nTRANSMISSION BLOCKED — {primary}. Nothing transmitted.")
        return 0

    # --- ARMED + CONFORM + every gate passed: build the ordered orders and transmit them. ---
    print(f"\n[9] *** ARMED + CONFORM and all gates passed: transmitting {len(legs)} deploy "
          f"leg(s), SELLS FIRST. ***")
    built: list = []
    for l in legs:
        ref = order_router._order_ref(account, target.as_of, l.side, l.symbol)
        o = order_router.build_marketable_limit(
            l.symbol, l.side, l.qty, l.limit, account=account, order_ref=ref)
        contract = Stock(l.symbol, "SMART", "USD")
        try:
            ib.qualifyContracts(contract)   # read-only validation nicety
        except Exception:
            pass
        built.append(order_router.BuiltOrder(l.symbol, contract, o, ref))

    # IN-PROCESS safety-flag flip — THIS PROCESS ONLY (mirrors s0_live_exec exactly).
    # order_router.transmit_guard fails CLOSED while config.DRY_RUN or config.READONLY is
    # True (the committed desk-wide defaults). We flip both to False in memory ONLY, place,
    # then RESTORE in a finally so the flip can never leak past place(). UNREACHABLE unless
    # `permit` is True (below the `if not permit: return` guard). The `built` list is ordered
    # sells-first, so place() transmits the sells before the buys.
    prev_readonly = config.READONLY
    prev_dry_run = config.DRY_RUN
    try:
        config.READONLY = False
        config.DRY_RUN = False
        res = order_router.place(ib, built, armed=True, account=account,
                                 context="s0_live_deploy_conform")
    finally:
        config.READONLY = prev_readonly
        config.DRY_RUN = prev_dry_run
    fills = res.get("fills", []) if isinstance(res, dict) else []
    print("\n    Result:")
    for f in fills:
        print(f"      {f.get('symbol'):6s} {f.get('status')}  filled={f.get('filled')}  "
              f"remaining={f.get('remaining')}  @ {f.get('avgFillPrice')}")
    if not fills:
        print("      (no fill readback returned)")
    skipped = res.get("skipped", []) if isinstance(res, dict) else []
    for s in skipped:
        print(f"      SKIPPED {s.get('symbol')} ref={s.get('order_ref')} "
              f"state={s.get('state')}")
    print("\nDone. ARMED conform deploy complete — review the fills above and DISARM the "
          "Gateway when finished.")
    return 0


def cli(argv: list[str] | None = None) -> int:
    """CLI entry: --arm-i-understand sets armed=True; --conform sets conform=True. BOTH are
    required to actually liquidate + transmit."""
    argv = sys.argv[1:] if argv is None else argv
    return main(armed=arm_requested(argv), conform=conform_requested(argv))


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(cli())
