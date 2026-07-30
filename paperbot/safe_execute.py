"""
safe_execute.py — the SHARED SAFE EXECUTION ENGINE (Production Rebalance Control Plane,
conductor #64/#66, spec docs/PRODUCTION_REBALANCE_CONTROL_PLANE.md §2).

WHAT THIS IS
------------
ONE reusable primitive — `execute_plan(request, *, mode, ib=None)` — that encapsulates
everything AFTER planning: sizing + building the ordered leg list, the full fail-closed
pre-flight gate, and the two-phase cash-gated transmit. It was EXTRACTED (moved, not
rewritten) verbatim from `s0_live_deploy.py` (v0.25.1), the bespoke S0 GROWTH full-account
deploy executor that first proved these guarantees on the funded trust account U14438624.

The extraction is byte-for-byte behavior-preserving on purpose: this touches the desk's one
real-money transmit chokepoint. `s0_live_deploy` is now a thin caller that reads the account,
sizes the plan with the UNCHANGED engine, builds an ExecutionRequest, and delegates here. It
re-exports every name below so its existing imports/tests keep working unchanged.

It is built ON `order_router` (transmit_guard, builders, price guard) — NOT a rewrite of it.

MODES
-----
  * PREVIEW (default) — sizes + builds the ordered leg list, runs the gate collecting EVERY
    blocking reason, prints exactly what WOULD transmit, and sends NOTHING.
  * ARMED — transmits the two-phase cash-gated deploy ONLY if the full gate passes. Flips
    config.READONLY/DRY_RUN False IN-PROCESS behind the gate and RESTORES them in a finally,
    so the enablement can never outlive the batch.
There is no third (auto) mode.

SCOPE NOTE (Phase 2 only)
-------------------------
This increment MOVES the s0_live_deploy execution logic into the shared engine. It does NOT
yet unify with rebalance_execute's arm gate or consolidate arming.probe — those are separate
later increments (spec §2.2). See the TODO(safe-execute) markers.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace

from ib_async import Stock

import config
import live_quotes
import order_router
import s0_live

# ----------------------------------------------------------------------------------------
# EXECUTION MODES — PREVIEW (transmit nothing) or ARMED (transmit iff the full gate passes).
# ----------------------------------------------------------------------------------------
MODE_PREVIEW = "PREVIEW"
MODE_ARMED = "ARMED"

# Terminal ExecutionResult statuses.
STATUS_PREVIEW_ONLY = "PREVIEW_ONLY"
STATUS_COMPLETE = "COMPLETE"
STATUS_PARTIAL_LOUD = "PARTIAL_LOUD"
STATUS_BLOCKED = "BLOCKED"

# The CLI-token hints reproduced in the caller-flag block reasons below. The wording is the
# deploy executor's (s0_live_deploy is the only caller today). TODO(safe-execute): parameterize
# these when the unified arm gate lands (spec §2.2) so other callers can supply their own.
_ARM_TOKEN_HINT = "--arm-i-understand"
_CONFORM_FLAG_HINT = "--conform"

# NOTIONAL SANITY CAP default (sized for a real deploy, not a tiny test): no single order's
# notional may exceed this fraction of NetLiq (fat-finger / bad-price backstop).
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


# ========================================================================================
# EXECUTION REQUEST / RESULT / CAPS — the engine's pure, broker-agnostic contract (§2.3).
# ========================================================================================
@dataclass
class ExecutionCaps:
    """The per-run notional sanity caps. `per_order_notional_pct_nlv`: no single order's
    notional may exceed this fraction of NetLiq. `total_buy_le_investable`: total BUY notional
    must not exceed the plan's investable. `max_total_notional`: an optional absolute ceiling
    on total notional (None = not enforced; the investable cap already bounds deployment)."""
    per_order_notional_pct_nlv: float = MAX_ORDER_NOTIONAL_PCT_NLV
    total_buy_le_investable: bool = True
    max_total_notional: float | None = None


@dataclass
class ExecutionRequest:
    """Everything the engine needs to size, gate, and (in ARMED mode) transmit a rebalance —
    pure and broker-agnostic (the broker `ib` is passed to execute_plan separately and is
    required only for the ARMED transmit + realized-cash re-read + gateway probe).

    `plan` is an AccountPlan from rebalance_engine.plan_account (signed integer deltas +
    alien_lines); `target` is the strategy Target (weights/prices/as_of). `allowed_accounts`
    is the account wall (any account not in it is refused). `armed`/`kill` are the raw gate
    inputs the caller measured; `mode` (ARMED vs PREVIEW) declares the connection lane."""
    account: str
    strategy_version: str
    plan: object                       # rebalance_engine AccountPlan
    target: object                     # strategy_target.Target
    quotes: dict
    prices: dict
    allowed_accounts: list
    caps: ExecutionCaps
    conform: bool
    run_id: str | None
    net_liq: float
    summary: list = field(default_factory=list)   # filtered accountSummary rows (BuyingPower gate)
    armed: bool = False
    kill: bool = False


@dataclass
class ExecutionResult:
    """The engine's outputs. `status` is one of PREVIEW_ONLY / COMPLETE / PARTIAL_LOUD /
    BLOCKED. `legs` is the ordered candidate leg list (as previewed); `sell_results`/
    `buy_results` are the per-leg transmit results (requested/filled/status/reprices/
    skipped/reason). `realized_cash` is the fresh TotalCashValue re-read between phases;
    `reconcile_residual` is the leftover gap (None until a post-run reconcile is wired)."""
    status: str
    legs: list
    reasons: list = field(default_factory=list)
    sell_results: list = field(default_factory=list)
    buy_results: list = field(default_factory=list)
    aliens_left: list = field(default_factory=list)
    unpriceable: list = field(default_factory=list)
    realized_cash: float | None = None
    reconcile_residual: object = None
    run_id: str | None = None
    rc: int = 0


# ========================================================================================
# ORDER-REF + RUN-ID (moved verbatim from s0_live_deploy).
# ========================================================================================
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


# ========================================================================================
# ACCOUNT WALL (moved from s0_live_deploy._account_safety_ok, generalized to allowed_accounts).
# ========================================================================================
def account_wall_ok(account: str, allowed_accounts) -> tuple[bool, str]:
    """Hard account wall: `account` must be one of `allowed_accounts` and no other. For the
    single-account deploy wall this is EXACTLY the s0_live_deploy check (and reason string).
    Read at call time so a test/monkeypatch of the account is honored."""
    allowed = list(allowed_accounts)
    if account not in allowed:
        allowed_str = allowed[0] if len(allowed) == 1 else "/".join(map(str, allowed))
        return False, (f"target account {account} is not the single allowed account "
                       f"{allowed_str} — refusing.")
    return True, ""


# ========================================================================================
# GATEWAY READ-ONLY PROBE (moved verbatim). TODO(safe-execute): consolidate with arming.probe
# into ONE port-parameterized probe (spec §2.2) — arming.py is 4002, this is the 4003 gate.
# ========================================================================================
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


# ========================================================================================
# ACCOUNT-VALUE READS (moved verbatim) — realized cash + buying power gate.
# ========================================================================================
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


# ========================================================================================
# LEG PRICING (moved verbatim).
# ========================================================================================
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


# ========================================================================================
# ORDERED LEG CONSTRUCTION (moved verbatim).
# ========================================================================================
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


# ========================================================================================
# TWO-PHASE TRANSMIT + REPORTING (moved verbatim).
# ========================================================================================
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


# ========================================================================================
# THE PRIMITIVE — execute_plan: leg build [7], pre-flight gate [8], two-phase transmit [9].
# ========================================================================================
def execute_plan(request: ExecutionRequest, *, mode: str = MODE_PREVIEW,
                 ib=None) -> ExecutionResult:
    """Size + build the ordered leg list, run the full fail-closed pre-flight gate collecting
    EVERY blocking reason, and — in ARMED mode only, and only if the gate passes — transmit the
    two-phase cash-gated deploy. PREVIEW transmits nothing.

    This is the extracted [7]/[8]/[9] body of s0_live_deploy._run_session, moved verbatim. The
    printed order-list / totals / blocked-reasons / two-phase logs are byte-for-byte identical.

    `ib` is required only for the ARMED transmit (gateway probe, buying-power gate, realized-cash
    re-read, placeOrder). `mode` is PREVIEW or ARMED — there is no third mode."""
    if mode not in (MODE_PREVIEW, MODE_ARMED):
        raise ValueError(f"mode must be {MODE_PREVIEW!r} or {MODE_ARMED!r}, got {mode!r}")

    req = request
    armed = req.armed
    conform = req.conform
    kill = req.kill
    # armed_conn: connected on the armed (transmit-capable) lane. By construction the caller
    # picks the ARMED lane iff permit_intent (armed AND conform AND not kill) held, so mode
    # ARMED <=> armed_conn (identical to s0_live_deploy's main()).
    armed_conn = (mode == MODE_ARMED)
    account = req.account
    plan = req.plan
    target = req.target
    quotes = req.quotes
    prices = req.prices
    net_liq = req.net_liq
    summary = req.summary
    caps = req.caps
    per_order_pct = caps.per_order_notional_pct_nlv

    # [7] Build the full ordered DEPLOY order list (sells first, then buys; conform adds the
    # ALIEN liquidations). Whole-share, price-guarded caps.
    legs, aliens_left, unpriceable = build_deploy_legs(plan, quotes, prices, conform=conform)
    total_buy = sum(l.notional for l in legs if l.side == "BUY")
    total_sell = sum(l.notional for l in legs if l.side == "SELL")
    per_order_cap = per_order_pct * net_liq

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
        reasons.append(f"conform intent absent (pass {_CONFORM_FLAG_HINT}) — this DEPLOY "
                       f"executor requires it to liquidate + transmit")
    if kill:
        reasons.append(f"KILL_SWITCH sentinel present ({_kill_switch_label()})")
    acct_ok, acct_reason = account_wall_ok(account, req.allowed_accounts)
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
                           f"> {per_order_pct*100:.0f}% of NetLiq "
                           f"({per_order_cap:,.2f})")
    # Total-notional sanity cap: total BUY notional must not exceed investable. (The two-phase
    # cash gate re-sizes buys to realized cash at transmit time; this is the plan-level cap.)
    if caps.total_buy_le_investable and total_buy > plan.investable:
        reasons.append(f"total BUY notional {total_buy:,.2f} > investable "
                       f"{plan.investable:,.2f} — would over-deploy / use margin")
    # Optional absolute total-notional ceiling (None by default — investable already bounds it).
    if caps.max_total_notional is not None and (total_buy + total_sell) > caps.max_total_notional:
        reasons.append(f"total notional {total_buy + total_sell:,.2f} > max_total_notional "
                       f"{caps.max_total_notional:,.2f}")

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

    result = ExecutionResult(status=STATUS_PREVIEW_ONLY, legs=legs, reasons=reasons,
                             aliens_left=aliens_left, unpriceable=unpriceable, rc=0)

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
        # PREVIEW_ONLY when this was never an armed-intent run; BLOCKED when an ARMED-mode run
        # was refused by a gate. (Terminal status for the ExecutionResult; does not affect
        # stdout or rc.)
        result.status = (STATUS_BLOCKED if mode == MODE_ARMED else STATUS_PREVIEW_ONLY)
        result.rc = 0
        return result

    # --- ARMED + CONFORM + every gate passed: TWO-PHASE cash-gated transmit. ---
    run_id = req.run_id or _run_id()
    result.run_id = run_id
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

    result.sell_results = sell_results
    result.buy_results = buy_results
    result.realized_cash = available_cash
    # Terminal status: PARTIAL_LOUD if any leg was unfilled/skipped, else COMPLETE.
    any_short = any(r.get("skipped") or r["filled"] < r["requested"]
                    for r in (sell_results + buy_results))
    result.status = STATUS_PARTIAL_LOUD if any_short else STATUS_COMPLETE
    result.rc = 0
    return result


# The kill-switch label is owned by the caller (s0_live_deploy). Overridden at import time by
# s0_live_deploy so the "KILL_SWITCH sentinel present (<path>)" reason reads identically to the
# pre-extraction message. Default is a generic label if no caller registers one.
_KILL_SWITCH_LABEL = "AUTOTRADE_DISABLED"


def register_kill_switch_label(label: str) -> None:
    """Let the caller register the exact kill-switch sentinel path for the block-reason string
    (byte-for-byte parity with s0_live_deploy's pre-extraction message)."""
    global _KILL_SWITCH_LABEL
    _KILL_SWITCH_LABEL = label


def _kill_switch_label() -> str:
    return _KILL_SWITCH_LABEL
