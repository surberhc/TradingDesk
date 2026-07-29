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

2026-07-28 INCIDENT -> REBUILT SAFER (conductor #63 / log #145)
---------------------------------------------------------------
The first live deploy on U14438624 left the account ~$40k NEGATIVE. Root cause was in THIS
executor's funding sequence: it transmitted ALL 15 legs at once — buying against the cash the
sells were EXPECTED to raise, WITHOUT waiting for the sells to fill. One sell (BUCK ~$40k)
CANCELLED (thin/stale quote, no re-price), so that cash never landed -> ~$115k of buys were
committed against ~$76k of real cash -> the account went negative and had to be hand-traded
out. Two other flaws surfaced: (a) a cancelled straggler was left as a hole instead of being
re-priced/chased, and (b) a legitimate re-buy of a manually-sold symbol was dedup-BLOCKED
because the ref was keyed on the monthly as_of date, so a bought-then-sold symbol still looked
"already done". The account was also still inside IBKR model "Main" (98.8% allocated) which our
sub-account read-only view CANNOT see. This rebuild fixes all four:

  1. TWO-PHASE, CASH-GATED execution. Phase 1 transmits ONLY the sells (plan sells + conform
     ALIEN liquidations) and WAITS for them to reach a terminal state. Between phases it
     RE-READS the account's live TotalCashValue (realized cash, never the plan's expected
     proceeds). Phase 2 sizes the buys to that ACTUAL cash — total BUY notional is held
     <= available cash minus a small safety buffer, scaling down / skipping whole-share buys
     if a sell fell short. Buying against money that has not arrived is now structurally
     impossible.
  2. RE-PRICE / chase stragglers. Any leg that has not filled within REPRICE_AFTER_SEC is
     cancelled and re-placed at a MORE aggressive marketable limit (toward the far touch),
     up to REPRICE_MAX_ATTEMPTS. A leg that still will not fill is left cancelled and reported
     LOUDLY — never silently dropped.
  3. PER-RUN order ref. The deploy ref now carries a per-run stamp, so a fresh run's legs can
     never match a prior run's fills. Correctness comes from the ENGINE's delta-vs-current-
     positions (plan.orders already reflects what is held), NOT from historical-fill dedup.
     Double-submit protection is kept by a symbol+side check against currently WORKING/open
     orders — so a still-live identical order is not duplicated, but a re-fire to complete the
     remaining gap is allowed.

NOTE (2026-07-29): the 2026-07-28 rebuild also added a --model-clear affirmation gate and a
best-effort IBKR model-overlay detection. Both were REMOVED per the account owner's explicit
direction — he manages model divestment manually, and the check was unwanted friction. All
other deploy safety gates below are unchanged.

WHY A --conform MODE
--------------------
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
    notional > 50% of NetLiq (fat-finger / bad-price catch), and
  * the Gateway physically ARMED (Read-Only API toggle OFF — measured live with the
    zero-transmission cancel-a-fabricated-order probe).
Miss ANY one and the run is a preview that transmits nothing and prints WHY.

There is NO auto-arm, and nothing here is scheduled. A human runs it, reviews the preview,
arms the Gateway by hand, and re-runs with the tokens to fire the deploy.

Run — PREVIEW with the conform list (default; transmits nothing):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe s0_live_deploy.py --conform

Run — ARMED conform deploy (human-supervised; requires an armed Gateway + no kill switch):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe s0_live_deploy.py --conform \\
      --arm-i-understand
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime

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

# TWO-PHASE / RE-PRICE / CASH-GATE tuning (2026-07-28 rebuild). All bounded so a run always
# terminates and never blocks on the wire.
PHASE_TERMINAL_TIMEOUT_SEC = 90.0    # max wait for one phase's legs to reach terminal state
REPRICE_AFTER_SEC = 18.0             # unfilled longer than this -> cancel + re-price (chase)
REPRICE_MAX_ATTEMPTS = 3             # cap on cancel-replace re-prices per leg
POLL_SEC = 1.0                       # phase poll cadence
CASH_SETTLE_SEC = 3.0                # let streaming account values update after the sells fill
# Keep a small slice of realized cash UNSPENT so rounding / a late fill can never tip negative.
CASH_SAFETY_BUFFER_PCT = 0.01
# Terminal order statuses (filled OR done-without-fill). Mirrors ib_async DoneStates + the
# reject/inactive set order_router already treats as terminal.
_TERMINAL_STATUSES = frozenset({
    "Filled", "Cancelled", "ApiCancelled", "Inactive", "Rejected", "ValidationError",
})

# DEPLOY ORDER-REF NAMESPACE. The tiny-test (s0_live_exec) builds its orderRef with the SAME
# order_router._order_ref(account, as_of, side, symbol) format, so a one-off tiny-test BUY of
# a symbol the deploy also buys (e.g. USFR) shares an identical ref. The :deploy tag gives the
# deploy its own ref namespace. On top of that, EACH RUN appends a per-run stamp (see
# _deploy_ref's run_id): the 2026-07-28 incident showed a monthly-as_of ref cannot tell a
# bought-then-sold symbol (net position back below target) needs re-buying — it looked "already
# done". A per-run ref means a fresh run's legs never collide with ANY prior run's fills, so the
# engine's delta-vs-current-positions is the sole source of truth for what to trade. Double-
# submit protection is preserved separately by a symbol+side check against currently WORKING
# orders (see _working_order_present), which is ref-INDEPENDENT and so still catches a live order
# from a prior run.
DEPLOY_REF_TAG = "deploy"


def _deploy_ref(account, as_of, side, symbol, run_id=None) -> str:
    """The deploy-namespaced orderRef: the standard order_router ref plus the deploy tag, plus
    a PER-RUN stamp when run_id is given. Used identically by the transmit path so the checked
    ref and the transmitted ref are byte-identical. With run_id=None it yields the stable base
    (back-compat / ref-format tests); a real run always passes the run stamp."""
    base = f"{order_router._order_ref(account, as_of, side, symbol)}:{DEPLOY_REF_TAG}"
    return f"{base}:{run_id}" if run_id else base


def _run_id() -> str:
    """A per-run identifier for the deploy ref. This is a normal on-demand process (not a
    deterministic workflow), so reading the wall clock here is fine and is exactly what makes a
    re-fire not collide with a prior run's fills."""
    return datetime.now().strftime("%Y%m%dT%H%M%S")


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


def _total_cash_value(summary) -> float | None:
    """REALIZED cash (TotalCashValue) off the (already account-FILTERED) accountSummary rows.
    This is the ground truth the between-phases buy sizing uses — never the plan's expected
    sale proceeds. None if the tag is missing/unparseable (caller then FAILS CLOSED)."""
    for row in summary:
        if getattr(row, "tag", None) == "TotalCashValue":
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


def _more_aggressive_cap(side: str, symbol: str, quotes: dict, base_limit: float,
                         attempt: int) -> float:
    """A MORE aggressive marketable cap for a re-price attempt: escalate ORDER_CAP_K by the
    attempt number off the live quote (BUY chases up toward/over the ask, SELL down toward/
    under the bid). Falls back to a directional bump off the previous limit when no quote is
    available, and guarantees the new cap is strictly more aggressive than the previous one so
    every re-price is real progress. Always positive; still passed through order_router's HARD
    price guard when the order is built."""
    k = config.ORDER_CAP_K * (attempt + 1)
    q = quotes.get(symbol)
    cap = live_quotes.marketable_cap(side, q, k=k) if q is not None else None
    if not (cap and cap == cap and cap > 0):
        bump = config.ORDER_CAP_K * (attempt + 1)
        cap = base_limit * (1 + bump) if side == "BUY" else base_limit * (1 - bump)
        cap = round(cap, 2)
    # Guarantee monotonic aggressiveness relative to the previous limit.
    if side == "BUY":
        cap = max(cap, round(base_limit * 1.001, 2))
    else:
        cap = min(cap, round(base_limit * 0.999, 2))
    return round(cap, 2)


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


def _scale_buys_to_cash(buy_legs, available_cash: float, *,
                        buffer_pct: float = CASH_SAFETY_BUFFER_PCT):
    """Size the BUY legs to the REALIZED cash so the account can NEVER go negative.

    Hard rule: sum(returned buy notional) <= available_cash * (1 - buffer_pct). Scaling is
    WHOLE-SHARE — a proportional floor first, then a greedy per-share trim (from the most
    expensive leg) to absorb rounding — so the invariant holds exactly. A leg trimmed to 0
    shares is dropped (and reported).

    Returns (scaled_legs, adjustments):
      scaled_legs : the qty>0 buys to transmit (notional recomputed at the whole-share qty)
      adjustments : [{symbol, orig_qty, new_qty}] for every leg whose qty was reduced/skipped
    """
    from types import SimpleNamespace
    if not buy_legs:
        return [], []
    budget = max(0.0, float(available_cash) * (1.0 - buffer_pct))
    total = sum(l.notional for l in buy_legs)
    factor = 1.0 if (total <= budget or total <= 0) else (budget / total)

    work = []
    for l in buy_legs:
        nq = int(l.qty * factor)             # floor to whole shares
        work.append(SimpleNamespace(symbol=l.symbol, side=l.side, qty=nq, limit=l.limit,
                                    notional=nq * l.limit, source=l.source, orig_qty=l.qty))

    def running() -> float:
        return sum(w.notional for w in work)

    # Greedy per-share trim from the most expensive leg until we fit the budget exactly.
    guard = 0
    while running() > budget and any(w.qty > 0 for w in work):
        w = max((w for w in work if w.qty > 0), key=lambda w: w.limit)
        w.qty -= 1
        w.notional = w.qty * w.limit
        guard += 1
        if guard > 5_000_000:                # pathological safety valve — never spin forever
            break

    scaled = [w for w in work if w.qty > 0]
    adjustments = [{"symbol": w.symbol, "orig_qty": w.orig_qty, "new_qty": w.qty}
                   for w in work if w.qty != w.orig_qty]
    return scaled, adjustments


def _working_order_present(ib, symbol: str, side: str) -> bool:
    """True if the broker currently has a WORKING/open order for this EXACT symbol+side (any
    orderRef). Double-submit protection that is ref-INDEPENDENT: with a per-run ref a prior
    run's still-working order carries a DIFFERENT ref, so ref-keyed dedup would miss it — we
    match on symbol+side against live open orders instead. FAILS CLOSED: any open-orders read
    error -> treat as present (skip the leg) so we never double-submit blind."""
    try:
        open_trades = ib.reqAllOpenOrders()
    except Exception as exc:
        print(f"      ! open-orders read FAILED for {side} {symbol}: {type(exc).__name__}: "
              f"{exc} -> treating as WORKING (skip; never double-submit blind).")
        return True
    for t in (open_trades or []):
        o = getattr(t, "order", None) or t
        c = getattr(t, "contract", None)
        sym = getattr(c, "symbol", None)
        act = getattr(o, "action", None)
        if sym == symbol and act == side:
            return True
    return False


def _trade_done(trade) -> bool:
    """Terminal-state test for one placed order. Prefers ib_async Trade.isDone(); falls back to
    the orderStatus.status against the terminal set."""
    try:
        return bool(trade.isDone())
    except Exception:
        st = getattr(trade, "orderStatus", None)
        return getattr(st, "status", "") in _TERMINAL_STATUSES


def _cum_filled(active: dict) -> float:
    """Cumulative filled shares for a leg across any cancel/replace re-prices (fills on a
    cancelled order are carried in filled_prior; the live trade's own fill is added on top)."""
    st = getattr(active["trade"], "orderStatus", None)
    live = float(getattr(st, "filled", 0.0) or 0.0)
    return float(active.get("filled_prior", 0.0)) + live


def _transmit_phase(ib, legs, *, account, as_of, run_id, phase_label, quotes, prices):
    """Transmit ONE phase's legs (all sells, or all buys), then WAIT for terminal state with
    bounded straggler re-pricing. Returns a list of per-leg result dicts:
      {symbol, side, requested, filled, status, reprices, skipped, reason}

    Safety: honors order_router.transmit_guard (fails closed — transmits nothing unless the
    guard is fully open); skips any leg that already has an identical WORKING order (no double-
    submit); uses per-run refs; and cancels + re-prices an unfilled leg toward the far touch up
    to REPRICE_MAX_ATTEMPTS, re-placing ONLY the still-unfilled remainder (never over-fills).
    Any leg still unfilled at the phase timeout is cancelled and reported LOUDLY."""
    results: list[dict] = []
    if not legs:
        print(f"    [{phase_label}] no legs.")
        return results

    permit, why = order_router.transmit_guard(armed=True)
    if not permit:
        # Fail closed — unreachable inside the armed flip, but NEVER transmit if the guard is
        # not fully open.
        print(f"    [{phase_label}] transmit_guard BLOCKED ({why}) — transmitting nothing.")
        for l in legs:
            results.append({"symbol": l.symbol, "side": l.side, "requested": l.qty,
                            "filled": 0.0, "status": "BLOCKED", "reprices": 0,
                            "skipped": True, "reason": f"transmit_guard {why}"})
        return results

    active: list[dict] = []
    for l in legs:
        if _working_order_present(ib, l.symbol, l.side):
            print(f"    [{phase_label}] SKIP {l.side} {l.symbol}: an identical WORKING order is "
                  f"already open — not double-submitting.")
            results.append({"symbol": l.symbol, "side": l.side, "requested": l.qty,
                            "filled": 0.0, "status": "SKIPPED_WORKING", "reprices": 0,
                            "skipped": True, "reason": "identical working order open"})
            continue
        ref = _deploy_ref(account, as_of, l.side, l.symbol, run_id)
        order = order_router.build_marketable_limit(
            l.symbol, l.side, l.qty, l.limit, account=account, order_ref=ref)
        contract = Stock(l.symbol, "SMART", "USD")
        try:
            ib.qualifyContracts(contract)   # read-only validation nicety
        except Exception:
            pass
        order.transmit = True
        trade = ib.placeOrder(contract, order)
        print(f"    [{phase_label}] SENT {l.side} {l.symbol} x{l.qty} LIMIT {l.limit:,.2f} "
              f"ref={ref}")
        active.append({"leg": l, "trade": trade, "contract": contract, "order": order,
                       "ref": ref, "limit": l.limit, "attempts": 0, "filled_prior": 0.0,
                       "placed_at": time.time()})

    # Poll to terminal with bounded straggler re-pricing.
    deadline = time.time() + PHASE_TERMINAL_TIMEOUT_SEC
    while time.time() < deadline:
        if all(_trade_done(a["trade"]) for a in active):
            break
        ib.sleep(POLL_SEC)
        for a in active:
            if _trade_done(a["trade"]):
                continue
            if (time.time() - a["placed_at"] >= REPRICE_AFTER_SEC
                    and a["attempts"] < REPRICE_MAX_ATTEMPTS):
                l = a["leg"]
                prior = _cum_filled(a)
                remaining = int(l.qty - prior)
                if remaining <= 0:
                    continue                  # filled during the wait; nothing to re-price
                a["attempts"] += 1
                a["filled_prior"] = prior
                new_cap = _more_aggressive_cap(l.side, l.symbol, quotes, a["limit"],
                                               a["attempts"])
                print(f"    [{phase_label}] RE-PRICE {l.side} {l.symbol} attempt "
                      f"{a['attempts']}/{REPRICE_MAX_ATTEMPTS}: {a['limit']:,.2f} -> "
                      f"{new_cap:,.2f} (chase far touch); cancel + replace remainder "
                      f"x{remaining}.")
                try:
                    ib.cancelOrder(a["order"])
                except Exception:
                    pass
                ib.sleep(POLL_SEC)            # let the cancel settle before re-placing
                new_order = order_router.build_marketable_limit(
                    l.symbol, l.side, remaining, new_cap, account=account, order_ref=a["ref"])
                new_order.transmit = True
                a["order"] = new_order
                a["limit"] = new_cap
                a["trade"] = ib.placeOrder(a["contract"], new_order)
                a["placed_at"] = time.time()

    for a in active:
        l = a["leg"]
        if not _trade_done(a["trade"]):
            try:
                ib.cancelOrder(a["order"])    # give up: cancel the straggler, report loudly
            except Exception:
                pass
        st = getattr(a["trade"], "orderStatus", None)
        filled = _cum_filled(a)
        status = str(getattr(st, "status", "") or "")
        reason = "" if filled >= l.qty else "UNFILLED remainder (chased to cap, gave up)"
        results.append({"symbol": l.symbol, "side": l.side, "requested": l.qty,
                        "filled": filled, "status": status, "reprices": a["attempts"],
                        "skipped": False, "reason": reason})
    return results


def _report_phase(label: str, results) -> None:
    """Print a phase's per-leg results and a LOUD summary of anything unfilled/skipped."""
    if not results:
        print(f"\n    {label} phase: no legs.")
        return
    print(f"\n    {label} phase results:")
    flagged: list[dict] = []
    for r in results:
        line = (f"      {r['side']:4s} {r['symbol']:6s} requested={r['requested']:g} "
                f"filled={r['filled']:g} status={r['status']} reprices={r['reprices']}")
        if r.get("skipped"):
            line += f"  SKIPPED ({r.get('reason', '')})"
        print(line)
        if r.get("skipped") or r["filled"] < r["requested"]:
            flagged.append(r)
    if flagged:
        print(f"    !! {label} UNFILLED / SKIPPED legs (LOUD — needs human review):")
        for r in flagged:
            print(f"      -> {r['side']} {r['symbol']}: requested {r['requested']:g}, filled "
                  f"{r['filled']:g} [{r['status']}] {r.get('reason', '')}")


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
          f"{MAX_ORDER_NOTIONAL_PCT_NLV*100:.0f}% of NetLiq   whole-share   price-guarded")
    print(f"# EXECUTION   two-phase cash-gated (sells -> re-read TotalCashValue -> buys sized "
          f"to REALIZED cash)   straggler re-price x{REPRICE_MAX_ATTEMPTS:.0f}")
    if permit_intent:
        print("# *** ARMED + CONFORM INTENT: this run MAY liquidate non-S0")
        print("#     holdings and transmit a TWO-PHASE cash-gated deploy on a FUNDED account IF")
        print("#     every remaining gate passes. Review the full order list below. ***")
    else:
        print("# PREVIEW: sizes + builds the full ordered order list and prints it —")
        print("# transmits NOTHING (not armed, conform off, or kill switch present).")
    print("#" * 88)


def main(armed: bool = False, conform: bool = False, today: object = None) -> int:
    """DEPLOY executor. PREVIEW by default; transmits the TWO-PHASE cash-gated deploy ONLY when
    armed AND conform AND every gate passes. `today` is accepted for signature parity with the
    other runners; the shared brain always runs to the most recent data date."""
    print("=" * 88)
    print(f"S0 LIVE DEPLOY EXECUTOR ({DEPLOY_VERSION} tier) — preview by default, two-phase "
          f"cash-gated conform deploy when armed   [{version.banner()}]")
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

    # [3] Connect. ARMED intent -> the transmit-capable lane (readonly=False, clientId
    # s0_live_exec); otherwise the read-only pilot lane (readonly=True). A bare armed connection
    # still transmits nothing on its own — only order_router.place(armed=True) does. Whole
    # session in try/finally so it ALWAYS disconnects.
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


def _run_session(ib, target, *, armed: bool, conform: bool,
                 armed_conn: bool, kill: bool) -> int:
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
    print("    NOTE: buys will be RE-SIZED to REALIZED cash after the sells fill (two-phase); "
          "the buy figures above are the pre-cash-gate plan.")
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
    # Total-notional sanity cap: total BUY notional must not exceed investable. (The two-phase
    # cash gate re-sizes buys to realized cash at transmit time; this is the plan-level cap.)
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
        bp_ok, bp_reason = _buying_power_ok(summary, total_buy)
        if not bp_ok:
            reasons.append(bp_reason)

    permit = (armed and conform and armed_conn and not kill and not reasons)

    # [9] Report + (only if permitted) transmit the two-phase cash-gated deploy.
    if not permit:
        primary = ("not armed" if not armed
                   else "conform off" if not conform
                   else "kill switch present" if kill
                   else (reasons[0] if reasons else "gate not satisfied"))
        print("\n[9] TRANSMISSION BLOCKED — PREVIEW ONLY. Reason(s):")
        for r in reasons:
            print(f"      - {r}")
        print(f"\n    WOULD TRANSMIT {len(legs)} leg(s) (two-phase: sells first, then buys "
              f"sized to realized cash) on {account}. Nothing was transmitted.")
        print(f"\nTRANSMISSION BLOCKED — {primary}. Nothing transmitted.")
        return 0

    # --- ARMED + CONFORM + every gate passed: TWO-PHASE cash-gated transmit. ---
    run_id = _run_id()
    sell_legs = [l for l in legs if l.side == "SELL"]
    buy_legs = [l for l in legs if l.side == "BUY"]
    print(f"\n[9] *** ARMED + CONFORM — TWO-PHASE CASH-GATED DEPLOY (run "
          f"{run_id}). Phase 1 sells -> re-read realized cash -> phase 2 buys sized to that "
          f"cash. ***")

    # IN-PROCESS safety-flag flip — THIS PROCESS ONLY (mirrors s0_live_exec exactly).
    # order_router.transmit_guard fails CLOSED while config.DRY_RUN or config.READONLY is True
    # (the committed desk-wide defaults). We flip both to False in memory ONLY, run BOTH phases,
    # then RESTORE in a finally so the flip can never leak past the transmit. UNREACHABLE unless
    # `permit` is True (above the `if not permit: return` guard).
    prev_readonly = config.READONLY
    prev_dry_run = config.DRY_RUN
    try:
        config.READONLY = False
        config.DRY_RUN = False

        # PHASE 1 — SELLS (raise cash), wait for terminal, re-price stragglers.
        print(f"\n    PHASE 1 — SELLS ({len(sell_legs)} leg(s)): transmit, then WAIT for "
              f"terminal state (fill/cancel) before sizing any buy.")
        sell_results = _transmit_phase(ib, sell_legs, account=account, as_of=target.as_of,
                                       run_id=run_id, phase_label="SELL", quotes=quotes,
                                       prices=prices)

        # BETWEEN PHASES — RE-READ realized cash. NEVER trust the plan's expected proceeds; a
        # cancelled sell (the 2026-07-28 BUCK failure) means that cash never landed.
        ib.sleep(CASH_SETTLE_SEC)             # let streaming account values catch up to fills
        fresh_summary = s0_live.filter_account_summary(ib.accountSummary(), account=account)
        available_cash = _total_cash_value(fresh_summary)
        if available_cash is None:
            print("\n    !! Could not read realized TotalCashValue after the sells — FAIL "
                  "CLOSED: transmitting NO buys (never buy against unverified cash).")
            available_cash = 0.0
        else:
            print(f"\n    Realized available cash (TotalCashValue, fresh read): "
                  f"{available_cash:,.2f}. Buys sized to THIS — never expected proceeds.")

        # PHASE 2 — size buys to realized cash (whole-share; can NEVER go negative), transmit.
        scaled_buys, adjustments = _scale_buys_to_cash(buy_legs, available_cash)
        placed_buy_notional = sum(l.notional for l in scaled_buys)
        budget = max(0.0, available_cash * (1.0 - CASH_SAFETY_BUFFER_PCT))
        if adjustments:
            print("    BUYS REDUCED / SKIPPED to fit realized cash (short proceeds -> never "
                  "negative):")
            for adj in adjustments:
                verb = "SKIP  " if adj["new_qty"] == 0 else "REDUCE"
                print(f"      {verb} {adj['symbol']}: {adj['orig_qty']} -> {adj['new_qty']} "
                      f"shares")
        # HARD invariant — the load-bearing anti-negative check.
        assert placed_buy_notional <= budget + 1e-6, (
            f"cash-gate invariant violated: buy notional {placed_buy_notional} > budget "
            f"{budget}")
        print(f"\n    PHASE 2 — BUYS ({len(scaled_buys)} leg(s), total notional "
              f"{placed_buy_notional:,.2f} <= cash budget {budget:,.2f}): transmit.")
        buy_results = _transmit_phase(ib, scaled_buys, account=account, as_of=target.as_of,
                                      run_id=run_id, phase_label="BUY", quotes=quotes,
                                      prices=prices)
    finally:
        config.READONLY = prev_readonly
        config.DRY_RUN = prev_dry_run

    # Consolidated result — LOUD on anything unfilled/skipped in either phase.
    _report_phase("SELL", sell_results)
    _report_phase("BUY", buy_results)
    print("\nDone. Two-phase cash-gated deploy complete — review the fills above and DISARM the "
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
