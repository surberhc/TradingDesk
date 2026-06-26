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

from ib_async import LimitOrder, Stock

import config


@dataclass
class BuiltOrder:
    symbol: str
    contract: object       # ib_async Contract
    order: object          # ib_async Order (transmit forced False)
    order_ref: str


def _order_ref(account: str, as_of, side: str, symbol: str) -> str:
    return f"paperbot:{account}:{as_of}:{side}:{symbol}"


def build(approved, account: str, as_of, ib=None) -> list[BuiltOrder]:
    """Construct (contract, LIMIT order) for each approved intent. transmit stays
    False. If an ib session is given, qualify the contracts (read-only) so we know
    they resolve on IBKR before we would ever route them."""
    built: list[BuiltOrder] = []
    for o in approved:
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
    one. transmit stays False (this module never arms)."""
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
