"""
s0_live_exec.py — the S0 (adaptive_all_weather) TINY-TEST real-transmission executor.

WHAT THIS IS
------------
The desk's FIRST real-transmission path. Every other order path in this codebase is
zero-transmit (paper on 4002, or read-only/PILOT_MODE on 4003). This one CAN transmit a
real order on the funded, live-trading individual TEST account U5721712 on the Live-Trade
Gateway (port 4003) — but only for a single, hard-capped "tiny test", and only when a
human deliberately arms it. It exists to prove the review -> arm -> transmit path end to
end with the smallest possible real order, not to trade the strategy.

It REUSES the read-only pilot (s0_live_pilot_run.py) verbatim for the parts that decide
WHAT to trade: strategy_target.current_target() -> rebalance_engine.plan_account() against
U5721712 with live prices, filtered to the individual account (never the trust account
U14438624). It reimplements no sizing. The ONLY thing it adds on top of the pilot is a
single, capped, marketable LIMIT built with order_router.build_marketable_limit and — only
behind a non-bypassable gate — transmitted with order_router.place(armed=True).

DEFAULT IS SAFE. With no flag it runs a PREVIEW: it sizes the plan, picks + builds the one
candidate order, prints exactly what WOULD be transmitted, and sends nothing. To actually
transmit, a human must line up ALL of:
  * the exact CLI token  --arm-i-understand  (sets armed=True; never defaulted/auto-set),
  * NO kill-switch sentinel present,
  * the target account is EXACTLY U5721712 (never the trust account U14438624),
  * the single order is a BUY of a whitelisted symbol (USFR only),
  * qty <= MAX_TEST_SHARES (1) AND shares*limit <= MAX_TEST_NOTIONAL ($150),
  * the Gateway is physically ARMED (its Read-Only API toggle is OFF — measured live with
    the zero-transmission cancel-a-fabricated-order probe), and
  * the pre-transmit dedup gate (order_router.already_present) says the leg is FRESH.
Miss ANY one and the run is a preview that transmits nothing and prints WHY.

There is NO auto-arm, and nothing here is scheduled. A human runs it, reviews the preview,
arms the Gateway by hand, and re-runs with the token to fire the one order.

Run — PREVIEW (default, transmits nothing):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe s0_live_exec.py

Run — ARMED tiny test (human-supervised; requires an armed Gateway + no kill switch):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe s0_live_exec.py --arm-i-understand
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
# The ONLY account this executor may ever transmit on. Pinned to S0's individual live TEST
# account; the trust account U14438624 (S8's) under the same 4003 login is FORBIDDEN.
EXEC_ACCOUNT = s0_live.S0_LIVE_ACCOUNT          # "U5721712"
FORBIDDEN_TRUST_ACCOUNT = "U14438624"           # S8's trust account — never this one

# The ONLY symbol it may transmit (a single, liquid, ultra-short Treasury FRN ETF).
TEST_SYMBOL_WHITELIST = {"USFR"}

MAX_TEST_SHARES = 1                             # never more than one share
MAX_TEST_NOTIONAL = 150.0                       # USD; refuse if shares*limit exceeds
MAX_ORDERS_PER_RUN = 1                          # exactly one order per run, ever

# The exact arm token — typed in full, no abbreviation, no default (mirrors
# rebalance_execute.ARM_TOKEN).
ARM_TOKEN = "--arm-i-understand"

# KILL SWITCH — same sentinel morning_execute_run.py honors
# (morning_execute_run.AUTOTRADE_DISABLED_SENTINEL). Mirrored as a literal here rather than
# imported so this module pulls in none of morning_execute's module-level state; if the file
# exists (any content, even empty) this run is preview-only.
KILL_SWITCH = r"C:\TradingDesk-Local\AUTOTRADE_DISABLED"

# clientId used by the armed transmit connection (see connect_s0_live_armed -> "s0_live_exec"
# = 58). Preview sizing uses the read-only pilot lane (s0_live_pilot = 57).


def arm_requested(argv: list[str]) -> bool:
    """True ONLY if the exact arm token is present in argv — the single thing that sets
    armed=True. Mirrors rebalance_execute.arm_requested."""
    return ARM_TOKEN in argv


def _kill_switch_present() -> bool:
    """True if the AUTOTRADE_DISABLED sentinel exists -> force preview-only."""
    return os.path.exists(KILL_SWITCH)


def _account_safety_ok() -> tuple[bool, str]:
    """Constant-level account guard: EXEC_ACCOUNT must be EXACTLY S0's individual account
    and must NEVER be the trust account. Read at call time so a test/monkeypatch of
    EXEC_ACCOUNT is honored."""
    if EXEC_ACCOUNT == FORBIDDEN_TRUST_ACCOUNT:
        return False, (f"target account {EXEC_ACCOUNT} is the FORBIDDEN trust account "
                       f"{FORBIDDEN_TRUST_ACCOUNT} — refusing.")
    if EXEC_ACCOUNT != s0_live.S0_LIVE_ACCOUNT:
        return False, (f"target account {EXEC_ACCOUNT} is not S0's individual account "
                       f"{s0_live.S0_LIVE_ACCOUNT} — refusing.")
    return True, ""


def _probe_gateway_readonly(ib, timeout: int = 15) -> bool:
    """Return True if the OPEN live-trade (4003) connection's Gateway is READ-ONLY
    (transmission physically BLOCKED), False if it is WRITE-ENABLED (armed).

    Mirrors arming.probe_api_readonly's ZERO-TRANSMISSION technique EXACTLY — attach an
    error handler, ask the Gateway (via the RAW client call) to cancel a fabricated,
    never-placed orderId, and read the decisive reply:
      * Read-Only API -> code 321 / "read-only mode"                 -> True  (blocked)
      * Write-enabled -> 10147/10148 / "not found"/"cannot be cancelled" -> False (armed)
    No order is ever placed or rested. FAILS CLOSED: no decisive signal -> True (refuse).

    Why not call arming.probe_api_readonly() directly: that function is PAPER-4002-only — it
    hardcodes ibkr_paper.PAPER_PORT and the paper clientId 39 and cannot be pointed at the
    4003 Gateway we actually transmit on without editing arming.py (off-limits). So its idiom
    is reused here against the already-open 4003 connection, which is the correct physical
    human wall for THIS Gateway."""
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
    """Lightweight, fail-closed buying-power sanity check for the one tiny BUY (no dedicated
    S0 margin preflight exists — s4_risk.margin_preflight is exposure/leverage-specific and
    wrong for a cash buy). If BuyingPower is readable and is below the order's notional,
    refuse; if it can't be read, allow (the $150 cap already bounds the exposure)."""
    bp = _buying_power(summary)
    if bp is not None and bp < notional:
        return False, (f"buying power {bp:,.2f} < order notional {notional:,.2f} — refusing.")
    return True, ""


def _pick_test_order(plan, quotes: dict, prices: dict):
    """PURE candidate selection: from an already-sized AccountPlan, pick a SINGLE BUY of a
    whitelisted symbol (USFR), clamp the quantity DOWN to MAX_TEST_SHARES (never up-size),
    and compute a sane marketable BUY limit near the ask. Returns a SimpleNamespace with
    symbol/raw_qty/qty/limit/notional, or None if there is no whitelisted BUY to place (or
    no usable price to build one). Builds and transmits nothing."""
    from types import SimpleNamespace
    for sym in sorted(plan.orders):
        delta = plan.orders[sym]
        if delta <= 0 or sym not in TEST_SYMBOL_WHITELIST:
            continue                       # only a BUY of a whitelisted symbol qualifies
        qty = min(int(delta), MAX_TEST_SHARES)   # clamp DOWN — never size up
        if qty < 1:
            continue                       # sub-1-share deltas can't be a whole-share test
        # A marketable BUY cap near the ask (BUY = ask*(1+k)); fall back to the merged
        # reference price if there is no live two-sided quote. Guarded again at build time.
        q = quotes.get(sym)
        cap = live_quotes.marketable_cap("BUY", q) if q is not None else None
        if not (cap and cap == cap and cap > 0):
            ref = prices.get(sym)
            cap = round(float(ref), 2) if (ref and ref == ref and ref > 0) else None
        if not cap:
            continue                       # no usable price -> can't build; skip
        return SimpleNamespace(symbol=sym, raw_qty=delta, qty=qty, limit=cap,
                               notional=qty * cap)
    return None


def _safety_banner(armed: bool, kill: bool) -> None:
    permit_intent = armed and not kill
    print("\n" + "#" * 88)
    print(f"# SAFETY STATE   armed={armed}   arm_token={'present' if armed else 'absent'}   "
          f"kill_switch={'PRESENT' if kill else 'absent'}")
    print(f"# account={EXEC_ACCOUNT}   gateway=Live-Trade port 4003   "
          f"(NEVER the trust account {FORBIDDEN_TRUST_ACCOUNT})")
    print(f"# CAPS   symbols={sorted(TEST_SYMBOL_WHITELIST)}   max_shares={MAX_TEST_SHARES}   "
          f"max_notional=${MAX_TEST_NOTIONAL:,.0f}   max_orders/run={MAX_ORDERS_PER_RUN}")
    if permit_intent:
        print("# *** ARMED INTENT: this run MAY transmit ONE real, capped LIMIT order on a")
        print("#     FUNDED account IF every remaining gate (account/symbol/caps/gateway-")
        print("#     armed/dedup) passes. Review the candidate below before it fires. ***")
    else:
        print("# PREVIEW: sizes + builds the candidate order and prints 'WOULD TRANSMIT' —")
        print("# transmits NOTHING (not armed, or kill switch present).")
    print("#" * 88)


def main(armed: bool = False, today: object = None) -> int:
    """Tiny-test executor. PREVIEW by default; transmits ONE capped order only when
    armed AND every gate passes. `today` is accepted for signature parity with the other
    runners; the shared brain always runs to the most recent data date."""
    print("=" * 88)
    print(f"S0 LIVE TINY-TEST EXECUTOR — preview by default, ONE capped order when armed   "
          f"[{version.banner()}]")
    print("=" * 88)

    # [1] Compute the target BEFORE connecting (fail fast on stale data; connect nothing on
    # failure). Same shared-brain path the pilot uses.
    print("\n[1] Computing the S0 target (shared brain; stale-data guarded)...")
    try:
        target = strategy_target.current_target()
    except Exception as exc:
        print(f"    COULD NOT BUILD TARGET: {exc}. Nothing connected, nothing transmitted.")
        return 2
    print(f"    {target.version}   as_of={target.as_of.date()}  "
          f"price_date={target.price_date.date()}  ({len(target.weights)} holdings)")

    kill = _kill_switch_present()
    armed_intent = armed and not kill

    # [2] Safety banner — armed/preview state, account, caps.
    _safety_banner(armed, kill)

    # [3] Connect. ARMED intent -> the transmit-capable lane (readonly=False, clientId 58);
    # otherwise the read-only pilot lane (readonly=True, clientId 57). A bare armed
    # connection still transmits nothing on its own — only order_router.place(armed=True)
    # does, and only if we reach it. Whole session in try/finally so it ALWAYS disconnects.
    if armed_intent:
        print("\n[3] Connecting ARMED (readonly=False, clientId s0_live_exec) to the "
              "Live-Trade Gateway (port 4003)...")
        try:
            ib = s0_live.connect_s0_live_armed()
        except Exception as exc:
            print(f"    could not connect ARMED to the Live-Trade Gateway (port 4003): "
                  f"{type(exc).__name__}: {exc}. Nothing sized, nothing transmitted.")
            return 1
        armed_conn = True
    else:
        print("\n[3] Connecting READ-ONLY (clientId s0_live_pilot) to the Live-Trade "
              "Gateway (port 4003) for the preview...")
        try:
            ib = s0_live.connect_s0_live()
        except Exception as exc:
            print(f"    could not connect READ-ONLY to the Live-Trade Gateway (port 4003): "
                  f"{type(exc).__name__}: {exc}. Nothing sized, nothing transmitted.")
            return 1
        armed_conn = False

    try:
        return _run_session(ib, target, armed=armed, armed_conn=armed_conn, kill=kill)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
        print("Session closed.")


def _run_session(ib, target, *, armed: bool, armed_conn: bool, kill: bool) -> int:
    account = EXEC_ACCOUNT

    # [4] Read + FILTER to EXEC_ACCOUNT (the login also exposes the trust account + an 'All'
    # aggregate). Refuse if the target account is not present under the login.
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

    # [6] Size the REAL account with the UNCHANGED engine (identical call to the pilot).
    strat_universe = sp._strategy_universe()
    print("\n[6] Sizing the plan with rebalance_engine.plan_account (UNCHANGED engine)...")
    plan = rebalance_engine.plan_account(account, target.version, net_liq, positions,
                                         target, prices=prices, universe=strat_universe)

    # [7] Pick the SINGLE candidate: a whitelisted BUY (USFR), clamped to <= MAX_TEST_SHARES.
    pick = _pick_test_order(plan, quotes, prices)
    if pick is None:
        n_orders = len(plan.orders)
        print(f"\n[7] No whitelisted BUY (of {sorted(TEST_SYMBOL_WHITELIST)}) with a usable "
              f"price in the plan's {n_orders} order(s) — nothing to transmit. "
              f"(plan orders: {dict(sorted(plan.orders.items()))})")
        print("\nPREVIEW ONLY — nothing transmitted.")
        return 0

    order_ref = order_router._order_ref(account, target.as_of, "BUY", pick.symbol)
    print(f"\n[7] Candidate tiny-test order:")
    print(f"    BUY {pick.symbol} x{pick.qty}  LIMIT ~{pick.limit:,.2f}  "
          f"notional ~{pick.notional:,.2f}  (plan wanted x{pick.raw_qty}, clamped to "
          f"<= {MAX_TEST_SHARES})   ref={order_ref}")

    # [8] PRE-TRANSMIT GATE — collect EVERY blocking reason; transmit only if none remain.
    reasons: list[str] = []
    if not armed:
        reasons.append("not armed (default preview; pass --arm-i-understand to arm)")
    if kill:
        reasons.append(f"KILL_SWITCH sentinel present ({KILL_SWITCH})")
    acct_ok, acct_reason = _account_safety_ok()
    if not acct_ok:
        reasons.append(acct_reason)
    if pick.symbol not in TEST_SYMBOL_WHITELIST:
        reasons.append(f"symbol {pick.symbol} not in whitelist {sorted(TEST_SYMBOL_WHITELIST)}")
    if pick.qty > MAX_TEST_SHARES:
        reasons.append(f"qty {pick.qty} > MAX_TEST_SHARES {MAX_TEST_SHARES}")
    if pick.notional > MAX_TEST_NOTIONAL:
        reasons.append(f"notional {pick.notional:,.2f} > MAX_TEST_NOTIONAL "
                       f"{MAX_TEST_NOTIONAL:,.2f}")

    # Connection-dependent gates — only meaningful on the armed (4003 transmit) connection,
    # and only worth probing once the code-level gates above are clean.
    if armed and armed_conn and not reasons:
        if _probe_gateway_readonly(ib):
            reasons.append("Gateway is still READ-ONLY on 4003 (arming.probe idiom) — not "
                           "physically armed; a human must turn the Read-Only API toggle off")
    if armed and armed_conn and not reasons:
        dedup = order_router.already_present(ib, order_ref, pick.qty)
        if dedup != order_router.LegState.FRESH:
            reasons.append(f"dedup gate says {dedup} (not FRESH) for ref={order_ref}")
    if armed and armed_conn and not reasons:
        bp_ok, bp_reason = _buying_power_ok(summary, pick.notional)
        if not bp_ok:
            reasons.append(bp_reason)

    permit = armed and armed_conn and not kill and not reasons

    # [9] Report + (only if permitted) transmit the SINGLE order.
    if not permit:
        print("\n[9] TRANSMISSION BLOCKED — PREVIEW ONLY. Reason(s):")
        for r in reasons:
            print(f"      - {r}")
        print(f"\n    WOULD TRANSMIT: BUY {pick.symbol} x{pick.qty} LIMIT ~{pick.limit:,.2f} "
              f"on {account}. Nothing was transmitted.")
        return 0

    # --- ARMED + every gate passed: build ONE order and transmit it. ---
    print("\n[9] *** ARMED and all gates passed: transmitting ONE capped tiny-test order. ***")
    o = order_router.build_marketable_limit(
        pick.symbol, "BUY", pick.qty, pick.limit, account=account, order_ref=order_ref)
    contract = Stock(pick.symbol, "SMART", "USD")
    try:
        ib.qualifyContracts(contract)   # read-only validation nicety
    except Exception:
        pass
    built = order_router.BuiltOrder(pick.symbol, contract, o, order_ref)

    # IN-PROCESS safety-flag flip — THIS PROCESS ONLY. order_router.transmit_guard fails
    # CLOSED while config.DRY_RUN or config.READONLY is True, and both are the committed
    # desk-wide defaults (config.py stays True/True on disk — nothing here writes that file).
    # We flip them to False in memory ONLY, mirroring rebalance_execute.execute_armed (which
    # sets config.READONLY=False / config.DRY_RUN=False behind its arm token). This is
    # UNREACHABLE unless `permit` is True (armed AND armed_conn AND not kill AND no reasons),
    # because it lives below the `if not permit: return` guard above. We save the prior values
    # and RESTORE them in a finally so the flip can never leak past place() — a strictly safer
    # discipline than rebalance_execute's reliance on process exit, and it keeps the suite
    # clean if main() is ever called in-process (e.g. tests).
    prev_readonly = config.READONLY
    prev_dry_run = config.DRY_RUN
    try:
        config.READONLY = False
        config.DRY_RUN = False
        res = order_router.place(ib, [built], armed=True, account=account,
                                 context="s0_live_exec_tiny_test")
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
    print("\nDone. ARMED tiny-test complete — review the fill above and DISARM the Gateway "
          "when finished.")
    return 0


def cli(argv: list[str] | None = None) -> int:
    """CLI entry: --arm-i-understand (and nothing weaker) sets armed=True."""
    argv = sys.argv[1:] if argv is None else argv
    return main(armed=arm_requested(argv))


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(cli())
