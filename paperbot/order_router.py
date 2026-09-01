"""
order_router.py — turn risk-approved intents into IBKR paper LIMIT orders.

ARM-GATED, dry-run by default. It CONSTRUCTS the exact orders that would be sent and
LOGS them, but it refuses to transmit unless ALL of these hold together:
  config.DRY_RUN is False  AND  config.READONLY is False  AND  the caller passes
  armed=True (a deliberate, per-session human action).
Under the current safety config (READONLY + DRY_RUN, plus ReadOnlyApi=yes on the
gateway) transmission is physically impossible. The guard fails CLOSED.

Idempotency (ENFORCED, not just aspirational): every order carries a deterministic orderRef
  paperbot:<account>:<as_of>:<side>:<symbol>[:<run_id>]
  paperbot:<fa_group>:<as_of>:<side>:<symbol>[:<run_id>]   (FA blocks — keyed on the GROUP)
so a restart WITHIN the run re-derives the SAME id. The trailing <run_id> is the PER-RUN stamp
(v0.34.0, see _run_stamp) — constant across one run, different on the next, which is what makes
a re-run new work instead of "already done". Before the FIRST placeOrder for a leg, the pre-transmit
dedup gate `already_present()` reads BROKER TRUTH (reqAllOpenOrders + reqExecutions) and the
transmit journal, and lets ONLY a genuinely FRESH leg through — a working/filled/partial/
uncertain leg transmits NOTHING. This is what actually prevents a crash-resume, retry, or a
stacked ladder from double-sending. It fails CLOSED (any broker-read failure -> UNKNOWN ->
skip). whatIfOrder is NEVER used (known hang).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ib_async import (LimitOrder, MarketOrder, Order, PriceCondition, Stock, TagValue,
                      TimeCondition, util)

import config
import margin_monitor   # per-run margin/buying-power observability (conductor #26). Imports
                        # only config + a lazy ledger; neither imports order_router — acyclic.

# HARD TIMEOUT (seconds) for a single what-if request. ib_async's whatIfOrder has NO
# timeout (IB.RequestTimeout defaults to 0 = wait forever), so a what-if that never
# resolves — the FA/transmit-flag hang, see docs/IBKR_API_CURRENCY.md §3.1 and what_if()
# below — would otherwise wedge the loop over `built` indefinitely. 10s is generous for a
# genuine margin/commission round-trip and short enough that a hung request is abandoned.
WHATIF_TIMEOUT_SEC = 10.0


# =============================================================================
# PRE-TRANSMIT DEDUP GATE (S0 order-idempotency, docs/S0_ORDER_IDEMPOTENCY_SPEC.md §3.A).
# Broker-truth-first, with the transmit journal as a crash-window tripwire underneath.
# Both fail CLOSED: if we cannot prove a leg is safe to send, we do NOT send it.
# =============================================================================
class LegState:
    """The per-leg dedup verdict. Only FRESH proceeds to transmit."""
    FRESH = "FRESH"        # ref not open, 0 filled today          -> place normally
    WORKING = "WORKING"    # ref is in open orders                 -> SKIP (already live)
    COMPLETE = "COMPLETE"  # filled today >= target (or SENT)      -> SKIP (already done)
    PARTIAL = "PARTIAL"    # 0 < filled < target, nothing open     -> SKIP + ALERT (human)
    UNKNOWN = "UNKNOWN"    # broker read failed, or mid-transmit   -> SKIP + ALERT (closed)


# Sentinel: "no journal_state supplied — query the transmit journal on disk." Distinct from
# None, which is a valid journal_state meaning "the ref was never journaled".
_JOURNAL_UNSET = object()

# Which LegStates warrant a human alert (vs. a benign already-handled skip).
_ALERTING_STATES = frozenset({LegState.PARTIAL, LegState.UNKNOWN})


def leg_state_needs_alert(state: str) -> bool:
    """PARTIAL / UNKNOWN require a human; WORKING / COMPLETE are benign no-ops."""
    return state in _ALERTING_STATES


def already_present(ib, order_ref, target_qty, *, day=None,
                    journal_state=_JOURNAL_UNSET) -> str:
    """Pre-transmit dedup for ONE leg's orderRef. Returns a LegState. Reads live broker
    state with the SAFE read APIs only — reqAllOpenOrders (working refs) + reqExecutions
    (today's fills per ref); it NEVER calls whatIfOrder (known hang). Fails CLOSED: any
    broker-read exception/timeout -> UNKNOWN.

    Layer B (journal) is consulted first as the crash-window tripwire:
      * journaled SENT today        -> COMPLETE (defense-in-depth with the broker read);
      * journaled ATTEMPTING, no SENT -> the process died mid-transmit -> UNKNOWN (skip +
        alert, never auto-retry — broker truth alone can't tell if the order landed).
    `journal_state` may be passed in by the caller (the snapshot taken BEFORE it wrote its
    own ATTEMPTING, so the gate never trips on this run's own record); when left unset the
    gate queries transmit_journal itself. transmit_journal is imported at leaf level here
    so order_router's import graph stays acyclic."""
    if journal_state is _JOURNAL_UNSET:
        try:
            import transmit_journal
            journal_state = transmit_journal.state_for(order_ref, day)
        except Exception:
            journal_state = None
    if journal_state == "SENT":
        return LegState.COMPLETE
    if journal_state == "ATTEMPTING":
        return LegState.UNKNOWN

    # Layer A: broker truth (authoritative for "is it there?"). Fail closed on any error.
    try:
        from ib_async import ExecutionFilter
        open_trades = ib.reqAllOpenOrders()
        working_refs = set()
        for t in (open_trades or []):
            o = getattr(t, "order", None) or t
            ref = getattr(o, "orderRef", None)
            if ref:
                working_refs.add(ref)
        if order_ref in working_refs:
            return LegState.WORKING       # catches a resting GTC remainder or in-flight order
        fills = ib.reqExecutions(ExecutionFilter())
        filled = 0.0
        for f in (fills or []):
            ex = getattr(f, "execution", None) or f
            if getattr(ex, "orderRef", None) == order_ref:
                filled += float(getattr(ex, "shares", 0.0) or 0.0)
    except Exception as exc:
        print(f"    ! already_present broker read FAILED for {order_ref}: "
              f"{type(exc).__name__}: {exc} -> UNKNOWN (fail closed, transmit nothing).")
        return LegState.UNKNOWN

    target = float(target_qty or 0.0)
    if target > 0 and filled >= target:
        return LegState.COMPLETE
    if filled > 0:
        return LegState.PARTIAL
    return LegState.FRESH


def order_ref_for_route(route, as_of, run_id=None) -> str:
    """The deterministic orderRef for a staged route, matching EXACTLY what build() /
    build_fa_block() stamp — so the dedup gate keys on the same string the transmit path
    uses. FA blocks key on the group; direct legs key on the account. `run_id` must be the
    SAME value the caller passes to build()/build_fa_block() for that run (see _run_stamp)."""
    if getattr(route, "route", None) == "fa_block":
        return _fa_block_ref(route.fa_group, as_of, route.side, route.symbol, run_id)
    return _order_ref(route.account, as_of, route.side, route.symbol, run_id)


@dataclass
class BuiltOrder:
    symbol: str
    contract: object       # ib_async Contract
    order: object          # ib_async Order (transmit forced False)
    order_ref: str


# ---------------------------------------------------------------------------------------
# PER-RUN ORDER-REF STAMP (v0.34.0). WHY: the base ref carries the model `as_of`, which for a
# monthly model is effectively a MONTH stamp. already_present() keys the dedup on the ref, so a
# SECOND run of the same group+symbol+side inside that month read as "already done" and silently
# sent NOTHING — the 2026-07-28 incident (a bought-then-sold symbol needing a re-buy could not
# be re-sent). safe_execute fixed its OWN lane by appending a PER-RUN stamp to the ref
# (_deploy_ref / _run_id); this is the SAME convention, hoisted to the one place every lane
# builds a ref, so the block lane and the paper rebalance lane cannot drift from it.
#
# WHAT IS PRESERVED: WITHIN one run the run_id is constant, so the ref is constant, so
# already_present() still classifies a second submission of the SAME leg as WORKING/COMPLETE and
# transmits nothing. A retry, a straggler re-price, a crash-resume inside the run and a stacked
# ladder are all gated exactly as before.
# WHAT IS DELIBERATELY DROPPED: cross-run suppression. A NEW run gets a NEW ref, so it is
# correctly treated as NEW WORK rather than as "already done". That is the point — the engine's
# delta-vs-current-positions becomes the sole source of truth for what to trade, which is the
# only thing that can be right after a position has already moved.
# SIDE is part of the base ref, so a SELL and a BUY of the same symbol in the SAME run (the
# two-phase cash gate places both) can never collide with or dedup each other.
# TRACEABILITY: run_id is a human-readable wall-clock stamp (YYYYmmddTHHMMSS — safe_execute.
# _run_id) and is ALSO written to the run's ledger record, so an order on the wire joins back to
# the run that produced it from either direction.
# run_id=None yields the UNCHANGED base ref — kept for the lanes that key a DURABLE transmit
# journal on the ref (morning_execute_run), where the cross-day "already sent" tripwire is the
# intended behavior and a per-run ref would silently disable it.
def _run_stamp(base: str, run_id) -> str:
    """Append the per-run stamp to a base orderRef; run_id=None yields the base unchanged."""
    return f"{base}:{run_id}" if run_id else base


def _order_ref(account: str, as_of, side: str, symbol: str, run_id=None) -> str:
    return _run_stamp(f"paperbot:{account}:{as_of}:{side}:{symbol}", run_id)


def _fa_block_ref(fa_group: str, as_of, side: str, symbol: str, run_id=None) -> str:
    """The FA-BLOCK orderRef: keyed on the GROUP (not any one account), same per-run stamp."""
    return _run_stamp(f"paperbot:{fa_group}:{as_of}:{side}:{symbol}", run_id)


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


def build_mutual_fund_market(symbol, side, qty, *, account=None, fa_group=None,
                             fa_method="", order_ref=None) -> Order:
    """A plain MARKET order — the ONLY correct order for a MUTUAL FUND, and built ONLY for one.

    WHY THERE IS NO LIMIT PRICE HERE, AND WHY THAT IS NOT A HOLE IN THE PRICE GUARD.
    Every other builder in this module runs its cap through _check_limit_price, because for a
    continuously-quoted instrument a missing quote silently becoming a 0.0/NaN limit is a real
    footgun. A mutual fund is not continuously quoted: it has NO intraday price at all. It
    prices once a day, at NAV, after the close, and EVERY order entered that day fills at that
    SAME NAV. There is therefore no price to limit and nothing a limit could protect against —
    a limit order on a fund is not a safer order, it is a malformed one. The price guard is not
    bypassed; it is inapplicable, and this builder is scoped so narrowly that it can never be
    reached by an instrument the guard does protect.

    `qty` is deliberately NOT coerced to an int: fund positions are FRACTIONAL by nature
    (123.73 shares of AFMBX), and selling out means selling the whole position INCLUDING the
    fraction or the account never actually closes the holding. This is the one place on the
    desk where a non-whole quantity is correct.

    UNPROVEN UNTIL AN ARMED TEST (2026-09-01): the port-4003 gateway is ReadOnlyApi=yes and
    refuses transmission at the API boundary, so IBKR's ACCEPTANCE of this order — market type,
    fractional quantity, FUND contract — has NOT been demonstrated live. Only the construction
    below is proven."""
    o = MarketOrder(side, qty)
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


def build(approved, account: str, as_of, ib=None, run_id=None) -> list[BuiltOrder]:
    """Construct (contract, LIMIT order) for each approved intent. transmit stays
    False. If an ib session is given, qualify the contracts (read-only) so we know
    they resolve on IBKR before we would ever route them.

    `run_id` stamps the PER-RUN orderRef (see _run_stamp): the caller passes ONE run_id for
    the whole run so within-run dedup still holds while a NEW run is seen as new work.

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
        order.orderRef = _order_ref(account, as_of, o.side, o.symbol, run_id)
        order.transmit = False          # never armed in this module
        built.append(BuiltOrder(o.symbol, contract, order, order.orderRef))
    if ib is not None and built:
        try:
            ib.qualifyContracts(*[b.contract for b in built])  # read-only validation
        except Exception:
            pass  # qualification is a dry-run nicety; never fail the run on it
    return built


def build_fa_block(symbol: str, side: str, quantity: int, limit_price: float,
                   fa_group: str, fa_method: str, as_of, ib=None,
                   run_id=None) -> BuiltOrder:
    """Construct ONE FA group (block) order: the master executes it as a single block
    at one average price and allocates across the group's accounts by fa_method. No
    single `account` is set — that is what makes it a group order rather than a direct
    one. transmit stays False (this module never arms).

    `run_id` stamps the PER-RUN orderRef (see _run_stamp). The two-phase cash gate places a
    SELL block and a BUY block inside the SAME run: they share the run_id and are kept apart
    by the `side` already in the base ref, so they can never dedup each other.

    HARD PRICE GUARD: the block's limit price is validated (NaN/None/<=0 rejected)
    BEFORE the order object is built — a missing quote can never become a $0/NaN block
    order that the master would then split across a whole tier."""
    _check_limit_price(symbol, limit_price)
    contract = Stock(symbol, "SMART", "USD")
    order = LimitOrder(side, quantity, limit_price)
    order.faGroup = fa_group        # the allocation group defined on the gateway
    order.faMethod = fa_method      # e.g. "NetLiq" (proportional to each acct's net liq)
    order.tif = "DAY"
    order.orderRef = _fa_block_ref(fa_group, as_of, side, symbol, run_id)
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


def place(ib, built, armed: bool = False, fill_timeout: int = 60, *,
          day=None, journal_states: dict | None = None, account=None,
          context: str = "") -> dict:
    """Log every constructed order; transmit ONLY if the guard fully permits, then
    watch each order up to fill_timeout seconds for fills.

    Pre-transmit dedup (S0 idempotency): once permitted, EACH leg runs through
    already_present() — only a FRESH leg is sent; a WORKING/COMPLETE/PARTIAL/UNKNOWN leg
    transmits nothing and is reported in `skipped`/`leg_states` for the caller to alert on.
    `journal_states` optionally maps order_ref -> the caller's PRE-ATTEMPTING journal
    snapshot (so the gate never trips on this run's own ATTEMPTING record); absent, the gate
    queries the transmit journal itself.

    Per-RUN margin observability (conductor #26): in the ARMED+permitted branch only, an
    accountSummary snapshot is captured once BEFORE the first transmit and once AFTER the
    fill-watch, diffed, persisted (kind="margin_impact"), and returned as result["margin"].
    NOT per-single-order (a per-leg round-trip would add latency and race fills). Fully
    fail-soft: any capture error degrades to margin=None and never blocks/alters an order."""
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

    # --- ARMED + permitted: dedup PER LEG against broker truth, then transmit FRESH legs. ---
    print("    *** ARMED: transmitting LIMIT orders to the PAPER account ***")
    # Per-RUN margin snapshot BEFORE the first transmit (conductor #26). One read per run,
    # not per leg — see docstring. Fully fail-soft (read_snapshot returns None on any error).
    acct = account or next(
        (b.order.account for b in built if getattr(b.order, "account", None)), None)
    before = margin_monitor.read_snapshot(ib, acct) if acct else None
    trades = []
    leg_states: dict[str, str] = {}
    skipped: list[dict] = []
    for b in built:
        js = journal_states.get(b.order_ref, _JOURNAL_UNSET) if journal_states else _JOURNAL_UNSET
        state = already_present(ib, b.order_ref, b.order.totalQuantity, day=day,
                                journal_state=js)
        leg_states[b.order_ref] = state
        if state != LegState.FRESH:
            print(f"      GATE {state}: {b.symbol} ref={b.order_ref} — SKIP "
                  f"(transmit nothing).")
            skipped.append({"symbol": b.symbol, "order_ref": b.order_ref, "state": state,
                            "alert": leg_state_needs_alert(state)})
            continue
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

    # Per-RUN margin snapshot AFTER the fill-watch, diffed against `before`, persisted, and
    # returned as result["margin"] (conductor #26). Fully fail-soft — any error here degrades
    # to margin=None and NEVER changes the order result already computed above.
    try:
        after = margin_monitor.read_snapshot(ib, acct) if acct else None
        margin_rec = margin_monitor.to_record(before, after, account=acct,
                                               context=context or "place")
        margin_monitor.record_impact(before, after, account=acct,
                                     context=context or "place")
    except Exception:
        margin_rec = None
    return {"transmitted": len(trades), "logged": len(built), "fills": fills,
            "leg_states": leg_states, "skipped": skipped, "margin": margin_rec}


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
                   poll: float | None = None, day=None,
                   journal_state=_JOURNAL_UNSET, context: str = "") -> dict:
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

    # --- PRE-TRANSMIT DEDUP GATE (per leg, before rung 1). Broker truth + journal; only a
    # FRESH leg proceeds. A WORKING (incl. a resting GTC remainder) / COMPLETE / PARTIAL /
    # UNKNOWN leg transmits NOTHING — this is what stops a crash-resume or a re-run from
    # stacking a fresh ladder on top of an order the broker already has. A None order_ref
    # (no dedup key) can't be gated, so it proceeds (only the keyed transmit paths dedup).
    if order_ref:
        gate = already_present(ib, order_ref, total_qty, day=day,
                               journal_state=journal_state)
        if gate != LegState.FRESH:
            print(f"    GATE {gate}: {symbol} ref={order_ref} — SKIP laddered leg "
                  f"(transmit nothing).", flush=True)
            return {"transmitted": 0, "filled": 0.0, "remaining": float(total_qty),
                    "rungs_used": 0, "fills": [], "leg_state": gate, "skipped": True,
                    "alert": leg_state_needs_alert(gate)}

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
    # Per-RUN margin snapshot BEFORE rung 1 (conductor #26). One read per laddered leg, not
    # per rung. Fully fail-soft (read_snapshot returns None on any error).
    before = margin_monitor.read_snapshot(ib, account) if account else None
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
    # AND the pre-transmit gate above reads it back via reqAllOpenOrders on the next run:
    # a still-resting GTC returns WORKING and the leg is skipped, so a reconnect does not
    # double-send. This is a single place() with no watch loop, so no new hang path.
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

    # Per-RUN margin snapshot AFTER the ladder settles, diffed/persisted/returned as
    # result["margin"] (conductor #26). Fully fail-soft — any error degrades to margin=None.
    try:
        after = margin_monitor.read_snapshot(ib, account) if account else None
        margin_rec = margin_monitor.to_record(before, after, account=account,
                                               context=context or "place_laddered")
        margin_monitor.record_impact(before, after, account=account,
                                     context=context or "place_laddered")
    except Exception:
        margin_rec = None
    result = {"transmitted": rungs_used + (1 if rested else 0),
              "filled": filled, "remaining": remaining, "rungs_used": rungs_used,
              "avgFillPrice": avg_px, "rested": rested, "resting_qty": remaining if rested else 0.0,
              "margin": margin_rec,
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


def what_if(ib, built, *, timeout: float | None = None) -> list:
    """Validate each order WITHOUT transmitting: IBKR returns margin + commission and,
    crucially, whether it would accept the order at all (e.g. an FA master may reject a
    direct, unallocated order). Sends a what-if message only — never a live order.

    HARD TIMEOUT (BUG #48): `ib.whatIfOrder` sets whatIf=True but NOT transmit, which trips
    IBKR error 321 ("What-If order should have transmit flag set to TRUE"). IBKR delivers
    321 as an error EVENT that never resolves the request future, and ib_async's whatIfOrder
    has no timeout (IB.RequestTimeout defaults to 0), so the bare call HANGS FOREVER. A hang
    is not an exception, so the try/except below can never catch it. We therefore drive the
    async variant through ib_async's OWN event loop (util.run — the exact loop ib.whatIfOrder
    uses internally, so we are not fighting it with a second loop) but with an explicit
    WHATIF_TIMEOUT_SEC, so one hung request is abandoned and the loop over `built` moves on.

    A timeout is treated as NO STATE RETURNED: None is appended and it is logged plainly. A
    timeout is NEVER recorded as acceptance. Real rejections still arrive as exceptions and
    keep their existing path (also -> None).

    NOTE: setting transmit=True is NOT the fix. Even with transmit=True an FA group (block)
    order returns nothing at all (docs/IBKR_API_CURRENCY.md §3.1; conductor #53), so the
    timeout is the load-bearing backstop regardless of the transmit flag — and we never
    flip transmit on a what-if copy here anyway."""
    timeout = WHATIF_TIMEOUT_SEC if timeout is None else timeout
    print("    What-if validation (no transmission):")
    states = []
    for b in built:
        try:
            # asyncio.wait_for(whatIfOrderAsync(...), timeout) driven through ib_async's loop.
            state = util.run(ib.whatIfOrderAsync(b.contract, b.order), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"      {b.symbol:6s} what-if TIMED OUT after {timeout:g}s — NO STATE "
                  f"RETURNED (recording None; a timeout is NEVER an acceptance).")
            states.append(None)
            continue
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
