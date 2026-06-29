"""
order_router.py — turn risk-approved intents into IBKR paper LIMIT orders.

ARM-GATED, dry-run by default. It CONSTRUCTS the exact orders that would be sent and
LOGS them, but it refuses to transmit unless ALL of these hold together:
  config.DRY_RUN is False  AND  config.READONLY is False  AND  the caller passes
  armed=True (a deliberate, per-session human action).
Under the current safety config (READONLY + DRY_RUN, plus ReadOnlyApi=yes on the
gateway) transmission is physically impossible. The guard fails CLOSED.

Idempotency: every order carries a deterministic orderRef
  paperbot:<account>:<as_of>:<side>:<symbol>
so a restart re-derives the SAME id and a duplicate can be detected, not double-sent.
"""
from __future__ import annotations

from dataclasses import dataclass

from ib_async import (LimitOrder, Order, PriceCondition, Stock, TagValue,
                      TimeCondition)

import config


@dataclass
class BuiltOrder:
    symbol: str
    contract: object       # ib_async Contract
    order: object          # ib_async Order (transmit forced False)
    order_ref: str


def _order_ref(account: str, as_of, side: str, symbol: str) -> str:
    return f"paperbot:{account}:{as_of}:{side}:{symbol}"


def _check_limit_price(symbol: str, limit_price) -> float:
    """HARD PRICE GUARD: refuse to build any order whose limit price is NaN, None, or
    <= 0. A missing quote that silently became a 0.0/NaN limit is the "NaN limit price"
    footgun a review found — caught here so no such order is ever constructed (let alone
    transmitted). Raises ValueError with a clear reason; the caller logs/skips it.

    Returns the validated float so callers can use the cleaned value."""
    try:
        px = float(limit_price)
    except (TypeError, ValueError):
        raise ValueError(
            f"refusing to build order for {symbol}: limit price {limit_price!r} is not a "
            f"number (missing/failed quote). PRICE GUARD — no order built.")
    # NaN is the only float that is not equal to itself.
    if px != px:
        raise ValueError(
            f"refusing to build order for {symbol}: limit price is NaN "
            f"(missing/failed quote). PRICE GUARD — no order built.")
    if px <= 0:
        raise ValueError(
            f"refusing to build order for {symbol}: limit price {px} is <= 0. "
            f"PRICE GUARD — no order built.")
    return px


# =============================================================================
# LADDERED EXECUTION ROUTER — instrument classifier + dynamic order builders +
# the place->watch->cancel->escalate loop.  See docs/IBKR_ORDER_TYPES_RESEARCH.md.
#
# ib_async 2.1.0 has NO MidpriceOrder/Adaptive convenience class, so every dynamic
# order is built from the base `Order` with explicit fields. The HARD PRICE GUARD
# (_check_limit_price) is applied to the cap on EVERY rung. whatIfOrder is never
# called anywhere in this path (known hang).
# =============================================================================

def classify_instrument(symbol: str, *, sec_type: str | None = None,
                        relative_spread: float | None = None) -> str:
    """Classify a symbol into an instrument class for ladder selection. Data-driven:
      1. An option security type (sec_type starting "OPT"/"FOP", or symbol flagged) ->
         INDEX_OPTION (capped LMT / REL only; NEVER MIDPRICE or scheduler algos).
      2. A seed-set membership (config.LIQUID_ETF_SYMBOLS / ILLIQUID_ETF_SYMBOLS).
      3. Otherwise the live spread-width heuristic: relative spread above
         config.ILLIQUID_SPREAD_THRESHOLD -> ILLIQUID, at/under -> LIQUID. With no
         spread available, default to ILLIQUID (the safe full ladder — better to start
         passive and escalate than to cross blindly on an unknown, possibly-thin name)."""
    if sec_type and sec_type.upper() in ("OPT", "FOP"):
        return config.INSTRUMENT_CLASS_INDEX_OPTION
    sym = symbol.upper()
    if sym in {s.upper() for s in config.LIQUID_ETF_SYMBOLS}:
        return config.INSTRUMENT_CLASS_LIQUID_ETF
    if sym in {s.upper() for s in config.ILLIQUID_ETF_SYMBOLS}:
        return config.INSTRUMENT_CLASS_ILLIQUID_ETF
    if relative_spread is not None and relative_spread == relative_spread:  # not NaN
        if relative_spread > config.ILLIQUID_SPREAD_THRESHOLD:
            return config.INSTRUMENT_CLASS_ILLIQUID_ETF
        return config.INSTRUMENT_CLASS_LIQUID_ETF
    return config.INSTRUMENT_CLASS_ILLIQUID_ETF


def ladder_for(instrument_class: str) -> list[dict]:
    """The ordered list of ladder rungs for an instrument class, from the config recipe.
    Falls back to the illiquid (full) ladder for an unknown class — never empty, so the
    ladder always has a terminal marketable rung."""
    return list(config.ORDER_LADDER.get(
        instrument_class,
        config.ORDER_LADDER[config.INSTRUMENT_CLASS_ILLIQUID_ETF]))


def _base_fields(order, account, fa_group, fa_method, order_ref):
    """Common fields applied to every built order. transmit stays False here (placement
    flips it only behind the gate).

    outsideRth is left at ib_async's default (False). BUG FIX (live DU142 run): a previous
    version hard-set outsideRth=True on EVERY builder. IBKR rejects MIDPRICE outside RTH
    (Warning 321: "Midprice orders are not supported outside of regular trading hours"),
    and an outsideRth=True flag makes the order eligible outside RTH even when placed
    DURING RTH — so TFLO/VGSH MIDPRICE rung-1 orders were rejected. We operate during
    regular trading hours, so outsideRth=False is correct for ALL order types here."""
    order.transmit = False
    if order_ref:
        order.orderRef = order_ref
    if account:
        order.account = account
    if fa_group:
        order.faGroup = fa_group
        order.faMethod = fa_method or ""   # an order-level faMethod is rejected (Err 10226)
    return order


def build_marketable_limit(symbol, side, qty, cap, *, account=None, fa_group=None,
                           fa_method="", order_ref=None) -> Order:
    """A capped marketable LMT that crosses the spread now. The cap IS the limit price."""
    px = _check_limit_price(symbol, cap)
    o = LimitOrder(side, qty, px)
    o.tif = "DAY"
    return _base_fields(o, account, fa_group, fa_method, order_ref)


def build_gtc_limit(symbol, side, qty, cap, *, account=None, fa_group=None,
                    fa_method="", order_ref=None) -> Order:
    """A RESTING plain LMT at the cap with tif="GTC" — the disconnect-survival remainder
    (docs/IBKR_RESTING_CONDITIONAL_ORDERS.md §6). It MUST be a plain LMT: Adaptive forbids
    GTC and MIDPRICE is DAY-only, so neither can carry the resting tif. Left at IB so the
    leg survives session disconnect / session end (the thing that killed TFLO/VGSH). The
    cap is guarded like any limit."""
    px = _check_limit_price(symbol, cap)
    o = LimitOrder(side, qty, px)
    o.tif = "GTC"            # the whole point: survive session end / disconnect
    return _base_fields(o, account, fa_group, fa_method, order_ref)


def build_midprice(symbol, side, qty, cap, *, account=None, fa_group=None,
                   fa_method="", order_ref=None) -> Order:
    """MIDPRICE built from the base Order (no convenience class in 2.1.0): pegs to the
    NBBO midpoint or better and pays UP TO the cap to complete. lmtPrice is the cap, the
    worst-case price — guarded like any limit. US stocks/ETFs ONLY (never options)."""
    px = _check_limit_price(symbol, cap)
    o = Order(orderType="MIDPRICE", action=side, totalQuantity=qty, lmtPrice=px)
    o.tif = "DAY"
    return _base_fields(o, account, fa_group, fa_method, order_ref)


def build_adaptive(symbol, side, qty, cap, *, priority="Patient", account=None,
                   fa_group=None, fa_method="", order_ref=None) -> Order:
    """Adaptive algo wrapped on a capped LMT: algoStrategy="Adaptive" with a single
    TagValue("adaptivePriority", Patient|Normal|Urgent). Adaptive FORBIDS GTC — TIF is
    forced to DAY. The cap is the limit price (guarded)."""
    px = _check_limit_price(symbol, cap)
    if priority not in ("Patient", "Normal", "Urgent"):
        raise ValueError(f"adaptivePriority must be Patient|Normal|Urgent, got {priority!r}")
    o = Order(orderType="LMT", action=side, totalQuantity=qty, lmtPrice=px)
    o.algoStrategy = "Adaptive"
    o.algoParams = [TagValue("adaptivePriority", priority)]
    o.tif = "DAY"            # Adaptive forbids GTC
    return _base_fields(o, account, fa_group, fa_method, order_ref)


def build_rel(symbol, side, qty, cap, *, account=None, fa_group=None,
              fa_method="", order_ref=None) -> Order:
    """Relative / Pegged-to-Primary (REL): pegs to our-side NBBO and reprices toward
    marketable, with the cap as a HARD limit (auxPrice). Options-safe (used for S5 SPXW)
    where MIDPRICE is unsupported. The cap is guarded like any price."""
    px = _check_limit_price(symbol, cap)
    o = Order(orderType="REL", action=side, totalQuantity=qty)
    o.auxPrice = px          # hard cap on a REL peg
    o.tif = "DAY"
    return _base_fields(o, account, fa_group, fa_method, order_ref)


_RUNG_BUILDERS = {
    "marketable_limit": build_marketable_limit,
    "midprice": build_midprice,
    "adaptive": build_adaptive,
    "rel": build_rel,
}


def build_rung(rung: dict, symbol, side, qty, cap, *, account=None, fa_group=None,
               fa_method="", order_ref=None) -> Order:
    """Build the Order for a single ladder rung from its recipe dict. The cap is passed
    through the per-builder PRICE GUARD as the worst-case limit on EVERY rung."""
    kind = rung.get("order_type")
    builder = _RUNG_BUILDERS.get(kind)
    if builder is None:
        raise ValueError(f"unknown ladder rung order_type {kind!r}")
    kwargs = dict(account=account, fa_group=fa_group, fa_method=fa_method,
                  order_ref=order_ref)
    if kind == "adaptive":
        kwargs["priority"] = rung.get("priority", "Patient")
    return builder(symbol, side, qty, cap, **kwargs)


def build(approved, account: str, as_of, ib=None) -> list[BuiltOrder]:
    """Construct (contract, LIMIT order) for each approved intent. transmit stays
    False. If an ib session is given, qualify the contracts (read-only) so we know
    they resolve on IBKR before we would ever route them.

    HARD PRICE GUARD: each intent's limit price is validated (NaN/None/<=0 rejected)
    BEFORE any order object is built — a bad price raises rather than producing a
    $0/NaN order."""
    built: list[BuiltOrder] = []
    for o in approved:
        _check_limit_price(o.symbol, o.limit_price)
        contract = Stock(o.symbol, "SMART", "USD")
        order = LimitOrder(o.side, o.quantity, o.limit_price)
        order.account = account
        order.tif = "DAY"
        order.orderRef = _order_ref(account, as_of, o.side, o.symbol)
        order.transmit = False          # never armed in this module
        built.append(BuiltOrder(o.symbol, contract, order, order.orderRef))
    if ib is not None and built:
        try:
            ib.qualifyContracts(*[b.contract for b in built])  # read-only validation
        except Exception:
            pass  # qualification is a dry-run nicety; never fail the run on it
    return built


def build_fa_block(symbol: str, side: str, quantity: int, limit_price: float,
                   fa_group: str, fa_method: str, as_of, ib=None) -> BuiltOrder:
    """Construct ONE FA group (block) order: the master executes it as a single block
    at one average price and allocates across the group's accounts by fa_method. No
    single `account` is set — that is what makes it a group order rather than a direct
    one. transmit stays False (this module never arms).

    HARD PRICE GUARD: the block's limit price is validated (NaN/None/<=0 rejected)
    BEFORE the order object is built — a missing quote can never become a $0/NaN block
    order that the master would then split across a whole tier."""
    _check_limit_price(symbol, limit_price)
    contract = Stock(symbol, "SMART", "USD")
    order = LimitOrder(side, quantity, limit_price)
    order.faGroup = fa_group        # the allocation group defined on the gateway
    order.faMethod = fa_method      # e.g. "NetLiq" (proportional to each acct's net liq)
    order.tif = "DAY"
    order.orderRef = f"paperbot:{fa_group}:{as_of}:{side}:{symbol}"
    order.transmit = False
    if ib is not None:
        try:
            ib.qualifyContracts(contract)   # read-only validation
        except Exception:
            pass
    return BuiltOrder(symbol, contract, order, order.orderRef)


def transmit_guard(armed: bool) -> tuple[bool, str]:
    """Whether transmission is permitted. Fails CLOSED (any reason -> blocked)."""
    if config.DRY_RUN:
        return False, "DRY_RUN=True"
    if config.READONLY:
        return False, "READONLY=True"
    if not armed:
        return False, "session not armed by a human"
    return True, "ARMED"


def place(ib, built, armed: bool = False, fill_timeout: int = 60) -> dict:
    """Log every constructed order; transmit ONLY if the guard fully permits, then
    watch each order up to fill_timeout seconds for fills."""
    permit, why = transmit_guard(armed)
    print(f"\n    OrderRouter: transmission {'PERMITTED' if permit else 'BLOCKED'} ({why}).")
    if not built:
        print("    no approved orders to route.")
        return {"transmitted": 0, "logged": 0}
    print("    Orders constructed (transmit flag forced False):")
    for b in built:
        conid = getattr(b.contract, "conId", 0) or "unqualified"
        print(f"      {b.order.action:4s} {b.symbol:6s} x{b.order.totalQuantity:<7g} "
              f"LMT {b.order.lmtPrice:>10,.2f}  TIF={b.order.tif}  "
              f"conId={conid}  ref={b.order_ref}")
    if not permit:
        print("    -> NOTHING TRANSMITTED (dry run / not armed).")
        return {"transmitted": 0, "logged": len(built), "fills": []}

    # --- ARMED + permitted: transmit to the PAPER account and watch fills. ---
    print("    *** ARMED: transmitting LIMIT orders to the PAPER account ***")
    trades = []
    for b in built:
        b.order.transmit = True                 # actually send
        trades.append(ib.placeOrder(b.contract, b.order))

    waited = 0
    while waited < fill_timeout and not all(t.isDone() for t in trades):
        ib.sleep(1.0)
        waited += 1

    fills = []
    for t in trades:
        st = t.orderStatus
        fills.append({"symbol": t.contract.symbol, "status": st.status,
                      "filled": float(st.filled), "remaining": float(st.remaining),
                      "avgFillPrice": float(st.avgFillPrice or 0.0)})
        print(f"      {t.contract.symbol:6s} {st.status:12s} filled={st.filled:g} "
              f"remaining={st.remaining:g} @ {st.avgFillPrice or 0.0:,.2f}")
    return {"transmitted": len(trades), "logged": len(built), "fills": fills}


def _shown_cap(order) -> float:
    """The cap to display for a built rung: lmtPrice for LMT/MIDPRICE/Adaptive, auxPrice
    for REL. ib_async leaves the unused price field at a huge UNSET sentinel (~1.8e308),
    so pick the one this order type actually uses."""
    return order.auxPrice if order.orderType == "REL" else order.lmtPrice


# Terminal NON-FILL order statuses: the order is DONE but did NOT fill (rejected, errored,
# or cancelled). A rejected/validation-errored order reports filled=0 AND remaining=0, so we
# must NEVER infer completion from remaining==0 — only from cumulative filled >= target. Any
# of these on a rung means: that rung FAILED, cancel any residual and escalate the unfilled
# remainder to the next rung. (Statuses per ib_async OrderStatus.DoneStates + IBKR errors.)
_TERMINAL_NONFILL_STATUSES = frozenset({
    "ValidationError", "Rejected", "Cancelled", "ApiCancelled", "Inactive",
})


def _watch_trade(ib, trade, rung_seconds: float, poll: float, label: str) -> tuple[float, str]:
    """Watch ONE trade for up to rung_seconds, polling every `poll` s with FLUSHED progress
    (supervise-long-ops rule: tight timeout, flushed prints). Returns (filled, status) read
    off the live orderStatus — the caller derives the unfilled remainder from CUMULATIVE
    filled vs target, never from orderStatus.remaining (which is 0 on a rejected order, the
    exact false-success footgun). Never calls whatIfOrder."""
    # Guard the step so the watch ALWAYS terminates even if poll is mis-set to 0 (a 0 step
    # would otherwise spin forever on an unfilled rung). Minimum effective step 0.001s.
    step = max(float(poll), 0.001)
    waited = 0.0
    while waited < rung_seconds and not trade.isDone():
        ib.sleep(poll)
        waited += step
        st = trade.orderStatus
        print(f"        [{label}] watching… {waited:.0f}/{rung_seconds:.0f}s  "
              f"status={st.status} filled={float(st.filled):g} "
              f"remaining={float(st.remaining):g}", flush=True)
    st = trade.orderStatus
    return float(st.filled), str(st.status or "")


def place_laddered(ib, *, symbol, side, total_qty, caps, instrument_class,
                   account=None, fa_group=None, fa_method="", order_ref=None,
                   armed: bool = False, rung_seconds: float | None = None,
                   poll: float | None = None) -> dict:
    """Place ONE leg through its instrument-class ladder: place rung 1 for the full qty,
    watch fills for a bounded window, and if not fully filled CANCEL the residual and
    escalate the UNFILLED REMAINDER to the next rung — until filled or the terminal
    (marketable-cap) rung. The ladder ALWAYS terminates (final rung is marketable).

    `caps` is a dict mapping each rung's order_type -> its worst-case cap price for this
    side (computed by the caller from the live quote, e.g. live_quotes.marketable_cap and
    live_quotes.marketable_cap for the MIDPRICE/Adaptive cap too). Every cap is re-guarded
    inside the per-rung builder. A missing/NaN/<=0 cap raises before the rung is built.

    Behind the gate: if transmit_guard blocks, this builds + logs each rung's order object
    for the would-be rung-1 and transmits NOTHING (dry preview), mirroring place().

    whatIfOrder is NEVER called here (known hang)."""
    rung_seconds = config.LADDER_RUNG_SECONDS if rung_seconds is None else rung_seconds
    poll = config.LADDER_POLL_SECONDS if poll is None else poll
    permit, why = transmit_guard(armed)
    ladder = ladder_for(instrument_class)
    print(f"\n    Laddered placement: {side} {symbol} x{total_qty:g} "
          f"[{instrument_class}]  rungs={[r['order_type'] for r in ladder]}")
    print(f"    transmission {'PERMITTED' if permit else 'BLOCKED'} ({why}).", flush=True)

    if not permit:
        # DRY: build rung-1 to prove the recipe + cap pass the guard, transmit nothing.
        r0 = ladder[0]
        cap0 = caps.get(r0["order_type"])
        o = build_rung(r0, symbol, side, total_qty, cap0, account=account,
                       fa_group=fa_group, fa_method=fa_method, order_ref=order_ref)
        print(f"      [dry] rung-1 {r0['order_type']:16s} {side} {symbol} x{total_qty:g} "
              f"cap={_shown_cap(o)}  -> NOTHING TRANSMITTED.", flush=True)
        return {"transmitted": 0, "filled": 0.0, "remaining": float(total_qty),
                "rungs_used": 0, "fills": []}

    contract = Stock(symbol, "SMART", "USD")
    if ib is not None:
        try:
            ib.qualifyContracts(contract)
        except Exception:
            pass   # qualification is a nicety; never fail the ladder on it
    target = float(total_qty)
    filled = 0.0                     # CUMULATIVE filled across rungs — the ONLY completion truth
    rungs_used = 0
    avg_px = 0.0
    for i, rung in enumerate(ladder):
        remaining = target - filled
        if remaining <= 0:           # genuinely complete (cumulative fill met the target)
            break
        rungs_used += 1
        is_terminal = (i == len(ladder) - 1)
        cap = caps.get(rung["order_type"])
        qty = int(remaining) if float(remaining).is_integer() else remaining
        # PRICE GUARD runs inside build_rung — a bad cap raises BEFORE any order is sent.
        order = build_rung(rung, symbol, side, qty, cap, account=account,
                           fa_group=fa_group, fa_method=fa_method, order_ref=order_ref)
        order.transmit = True
        label = f"rung{i + 1}/{len(ladder)}:{rung['order_type']}"
        print(f"      placing {label}  {side} {symbol} x{qty:g}  "
              f"cap={_shown_cap(order)}  tif={order.tif}"
              + (f"  algo={order.algoStrategy}" if order.algoStrategy else ""), flush=True)
        trade = ib.placeOrder(contract, order)
        rung_filled, rung_status = _watch_trade(ib, trade, rung_seconds, poll, label)

        st = trade.orderStatus
        if st.avgFillPrice:
            avg_px = float(st.avgFillPrice)
        # Add ONLY this rung's actual fills to the cumulative total. A rejected order
        # contributes rung_filled=0, so it can never masquerade as a completion.
        filled += rung_filled
        remaining = target - filled

        if remaining <= 0:
            # Complete ONLY when cumulative filled met the target — never inferred from a
            # remaining==0 that a rejected order also produces.
            print(f"      {label}: FILLED (cumulative filled={filled:g} >= target={target:g}).",
                  flush=True)
            break

        # The rung did NOT complete the leg. Distinguish a terminal NON-FILL (rejected/
        # errored/cancelled — the live TFLO/VGSH MIDPRICE case) from a partial/timeout: both
        # escalate, but a rejected order needs no residual cancel (nothing is resting).
        rejected = rung_status in _TERMINAL_NONFILL_STATUSES and rung_filled <= 0
        if rejected:
            print(f"      {label}: FAILED (status={rung_status}, filled=0) — NOT a fill; "
                  f"escalating remainder x{remaining:g} to the next rung.", flush=True)
        else:
            # Partial fill or watch-window timeout with filled<target: cancel the residual,
            # then escalate the still-unfilled remainder.
            if not trade.isDone():
                print(f"      {label}: partial/timeout (filled={rung_filled:g}, remaining="
                      f"{remaining:g}) — cancelling residual"
                      f"{' (terminal rung)' if is_terminal else ', escalating'}.", flush=True)
                ib.cancelOrder(order)
                ib.sleep(poll)   # let the cancel settle before re-placing the remainder
            else:
                print(f"      {label}: done but unfilled remainder x{remaining:g} "
                      f"(status={rung_status})"
                      f"{' (terminal rung)' if is_terminal else ' — escalating'}.", flush=True)

    # --- GTC-remainder layer: "ladder while connected, rest when gone." ---------
    # If the terminal rung still left quantity unfilled, DO NOT give up: convert the
    # remainder to a RESTING plain GTC limit at the cap and leave it at IB, so the leg
    # survives session disconnect/end (the failure that killed TFLO/VGSH). The rest is a
    # plain LMT — Adaptive/MIDPRICE cannot be GTC. The deterministic orderRef is preserved
    # so a reconnect detects the resting order rather than double-sending. This is a single
    # place() with no watch loop, so no new hang path is introduced.
    # Recompute the genuinely-unfilled remainder from CUMULATIVE filled (not from any single
    # order's orderStatus.remaining) so a rejected rung can never poison the GTC-rest amount.
    remaining = target - filled
    rested = False
    if remaining > 0 and config.LADDER_REST_REMAINDER:
        rest_cap = caps.get("marketable_limit")
        # PRICE GUARD runs inside build_gtc_limit — a bad cap raises before any send.
        rest_qty = int(remaining) if float(remaining).is_integer() else remaining
        rest_order = build_gtc_limit(symbol, side, rest_qty, rest_cap, account=account,
                                     fa_group=fa_group, fa_method=fa_method,
                                     order_ref=order_ref)
        rest_order.transmit = True
        print(f"      RESTING remainder: GTC LMT {side} {symbol} x{rest_qty:g} "
              f"cap={_shown_cap(rest_order)} tif=GTC — left at IB (survives disconnect).",
              flush=True)
        ib.placeOrder(contract, rest_order)
        rested = True

    result = {"transmitted": rungs_used + (1 if rested else 0),
              "filled": filled, "remaining": remaining, "rungs_used": rungs_used,
              "avgFillPrice": avg_px, "rested": rested, "resting_qty": remaining if rested else 0.0,
              "fills": [{"symbol": symbol, "filled": filled, "remaining": remaining,
                         "avgFillPrice": avg_px, "rungs_used": rungs_used,
                         "rested_gtc": rested}]}
    if remaining <= 0:
        state = "FILLED"
    elif rested:
        state = f"RESTING (GTC) x{remaining:g}"   # NOT failed — left working at IB
    else:
        state = "INCOMPLETE (ladder exhausted)"
    print(f"    Laddered result: {symbol} {state} filled={filled:g} "
          f"remaining={remaining:g} after {rungs_used} rung(s).", flush=True)
    return result


def what_if(ib, built) -> list:
    """Validate each order WITHOUT transmitting: IBKR returns margin + commission and,
    crucially, whether it would accept the order at all (e.g. an FA master may reject a
    direct, unallocated order). Sends a what-if message only — never a live order."""
    print("    What-if validation (no transmission):")
    states = []
    for b in built:
        try:
            state = ib.whatIfOrder(b.contract, b.order)
        except Exception as exc:
            print(f"      {b.symbol:6s} REJECTED by what-if: {exc}")
            states.append(None)
            continue
        init_m = getattr(state, "initMarginChange", "?")
        comm = getattr(state, "commission", "?")
        ccy = getattr(state, "commissionCurrency", "")
        print(f"      {b.symbol:6s} accepted: initMargin={init_m}  commission={comm} {ccy}")
        states.append(state)
    return states


# =============================================================================
# S5 SERVER-SIDE RISK/COVER SEAM — conditional + OCA builders.  SCAFFOLDING ONLY.
#
# These build IB server-side constructs (see docs/IBKR_RESTING_CONDITIONAL_ORDERS.md
# §4-§5) that survive client disconnect: a cross-instrument Conditional Order (e.g.
# "if SPX <= X then send this SPXW cover") and a One-Cancels-All basket ("one fills ->
# IB cancels the rest"). They prove ib_async 2.1.0 can construct the fields/objects.
#
# *** NOT YET WIRED INTO THE REBALANCE FLOW. *** Nothing in rebalance_execute.py /
# rebalance_run.py calls these. They are the staging seam for the future S5 0DTE/tail
# strategy and stay behind the same review->arm->transmit gate as everything else.
# transmit stays False on every order built here.
# =============================================================================

# Trigger-method codes for a simulated PriceCondition (TWS API trigger_method ref):
#   0=default, 1=double bid/ask, 2=last, 3=double last, 4=bid/ask, 7=last-or-bid/ask,
#   8=mid-point. Default (0) is fine for an index trigger.
def build_price_condition(*, trigger_conid: int, price: float, is_more: bool,
                          exchange: str, trigger_method: int = 0,
                          conjunction: str = "a") -> PriceCondition:
    """A cross-instrument PriceCondition: "<trigger_conid on exchange> price >|<= price".
    For S5 this references the SPX index conId (a DIFFERENT contract than the SPXW option
    the order trades). is_more=False => "price <= X" (a downside trigger). conjunction
    'a'=AND / 'o'=OR when chaining multiple conditions on one order."""
    if trigger_conid is None or int(trigger_conid) <= 0:
        raise ValueError(f"trigger_conid must be a positive conId, got {trigger_conid!r}")
    _check_limit_price("<condition trigger>", price)   # PRICE GUARD on the trigger level
    c = PriceCondition()
    c.conId = int(trigger_conid)
    c.exch = exchange
    c.price = float(price)
    c.isMore = bool(is_more)
    c.triggerMethod = int(trigger_method)
    c.conjunction = conjunction
    return c


def build_time_condition(*, time: str, is_more: bool = True,
                         conjunction: str = "a") -> TimeCondition:
    """A TimeCondition: act when the server clock passes (is_more=True) / is before
    (is_more=False) `time` (TWS "YYYYMMDD HH:MM:SS" format). Server-evaluated."""
    if not time:
        raise ValueError("time condition requires a non-empty 'YYYYMMDD HH:MM:SS' time")
    c = TimeCondition()
    c.time = time
    c.isMore = bool(is_more)
    c.conjunction = conjunction
    return c


def build_conditional_order(symbol, side, qty, cap, conditions, *,
                            conditions_cancel_order: bool = False, tif: str = "GTC",
                            account=None, order_ref=None) -> Order:
    """A server-side Conditional Order: a capped LMT carrying one or more IB conditions
    (Price/Time/...) on `order.conditions`. IB evaluates the conditions on its servers and
    (with conditions_cancel_order=False) ACTIVATES the order when they are met — surviving
    our disconnect. Set conditions_cancel_order=True to instead CANCEL a working order when
    the condition fires. tif defaults to GTC so the staged order rests until triggered.

    NOT wired into the live flow — the S5 risk/cover seam. The cap is guarded; transmit
    stays False."""
    if not conditions:
        raise ValueError("a conditional order needs at least one condition")
    px = _check_limit_price(symbol, cap)
    o = Order(orderType="LMT", action=side, totalQuantity=qty, lmtPrice=px)
    o.conditions = list(conditions)
    o.conditionsCancelOrder = bool(conditions_cancel_order)
    o.tif = tif                       # GTC so the staged trigger rests at IB
    return _base_fields(o, account, None, "", order_ref)


def apply_oca_group(orders, oca_group: str, oca_type: int = 1):
    """Tag a basket of orders as One-Cancels-All: when one fills, IB cancels the rest
    (server-enforced, survives our disconnect). oca_type: 1 = cancel-remaining WITH block
    (overfill-protected, the safe default), 2 = reduce-with-block, 3 = no-block. Mutates
    and returns the orders. The S5 mutually-exclusive cover/roll seam — NOT wired in."""
    if not oca_group:
        raise ValueError("oca_group must be a non-empty group name")
    if oca_type not in (1, 2, 3):
        raise ValueError(f"oca_type must be 1, 2, or 3, got {oca_type!r}")
    for o in orders:
        o.ocaGroup = oca_group
        o.ocaType = oca_type
    return orders
