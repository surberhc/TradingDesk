"""holding_class.py — the ONE place that decides whether a holding is MANAGED or HELD ASIDE.

THE DECISION (owner, 2026-08-19)
--------------------------------
Individual bonds are to be treated like the cash-that-is-not-to-be-traded: on a NO-TRADE
list, NOT a reason to bench the whole account. And they must be accounted for like
professionals — priced, counted, reported — not labelled and thrown in a closet.

So every holding falls into exactly one of two classes:

  MANAGED     — the model owns it. It is reconciled against the target, it can drift, and
                it can produce a BUY/SELL leg. This is every holding the desk trades today.
  HELD_ASIDE  — we PRICE it, we COUNT it, we REPORT it, and we NEVER emit an order for it.
                Not a buy, not a sell, not an ALIEN liquidation. It is simply not part of
                the managed sleeve.

AND THE ALLOCATION DECISION: held-aside holdings sit OUTSIDE the target allocation. Their
market value is carved out of the account's NetLiq FIRST; the model's weights then apply to
the REMAINING (managed) sleeve as its own 100%. A client's individual bonds are therefore
NOT counted as that client's fixed-income allocation — the managed sleeve is a complete
model portfolio in its own right.

WHY THIS IS NOT A BOND-ONLY HACK
--------------------------------
Individual bonds are the FIRST member of the held-aside class, not the definition of it.
Adding another instrument type (an option position we do not manage, a legacy warrant, a
foreign-currency balance) is a one-line edit to ``HELD_ASIDE_TYPES`` — no other module
changes, because every consumer keys off the CLASS, never off "is it a bond".

CLASSIFICATION IS DRIVEN BY INSTRUMENT DATA, NEVER BY A SYMBOL-STRING GUESS
--------------------------------------------------------------------------
The authoritative signal is the instrument's own type: an IBKR position carries
``contract.secType`` ("STK", "BOND", "OPT", ...) and the CRM holdings view carries the
same vocabulary in ``asset_category`` (IBKR Flex ``assetCategory``). Both are passed in
EXPLICITLY as a symbol -> type mapping, so this module stays pure and unit-testable and
never has to sniff a CUSIP out of a ticker string.

FAIL CLOSED ON UNKNOWN
----------------------
An instrument whose type cannot be determined — absent from the mapping, blank, or a type
this module has never been taught — is HELD ASIDE and flagged ``needs_classification``.
It is never silently assumed tradeable. The desk would rather leave a holding untouched
and ask a human what it is than trade something it cannot name.

FAIL CLOSED ON UNPRICEABLE
--------------------------
A held-aside holding we cannot value is worse than one we cannot name: the carve-out
arithmetic (managed sleeve = NetLiq - held-aside value) silently under-carves, and the
managed sleeve would be sized as if that money were available to invest. So an unpriceable
held-aside position BLOCKS order emission for that account (``CarveOut.blocked_reasons``)
while still reporting everything it knows. That is the one case where an account is held
back — and it is a data problem with a named reason, not a standing "bond account = skip".

PRICING CONVENTION (why bonds need one)
---------------------------------------
IBKR carries an individual bond with a FACE / PAR quantity (e.g. 10000) and quotes its
price as a PERCENT OF PAR per 100 (a mark of 100.146 means 100.146% of face). So a bond's
true value is ``qty * mark / 100``; valuing it as ``qty * mark`` overstates it ~100x.
Confirmed against live IBKR data 2026-08-05 (docs/IBKR_API_CURRENCY.md): for every BOND
row the broker's own reported market_value equals ``qty * mark / 100`` exactly, while
equities on the same accounts satisfy ``qty * mark == market_value``. That per-type
convention lives in ``PRICE_MULTIPLIER`` so valuation and order-exclusion can never
disagree — they read the same table.

LEAF MODULE: imports nothing from the desk (stdlib only), so it can be imported by
rebalance_engine, recon_report and the CRM adapters without any cycle.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


# --- the two classes -----------------------------------------------------------
MANAGED = "MANAGED"
HELD_ASIDE = "HELD_ASIDE"

# The placeholder type for "we could not determine what this instrument is".
UNKNOWN = "UNKNOWN"


# --- the type tables (the whole policy, in two literals) -----------------------
# Instrument types the model MANAGES. Vocabulary is IBKR's: `contract.secType` on a live
# position and `assetCategory` on a Flex/CRM holdings row use the same words. A couple of
# spelled-out synonyms are accepted so an upstream feed that says "STOCK" instead of "STK"
# is not wrongly benched.
MANAGED_TYPES = frozenset({"STK", "STOCK", "EQUITY", "ETF", "FUND", "MF"})

# Instrument types explicitly HELD ASIDE, each with the plain-English reason that is
# reported to a human. Adding a member here is the whole cost of adding a new held-aside
# instrument class — nothing else in the desk changes.
HELD_ASIDE_TYPES = {
    "BOND": ("individual bond — held aside outside the model allocation: priced and "
             "counted, never traded by the desk"),
}

# Reason used when the type could not be determined at all (fail-closed bucket).
UNKNOWN_REASON = ("instrument type could not be determined — held aside pending "
                  "classification (fail-closed: never traded on a guess)")

# Reason used for a type we recognise as real but have not been taught to manage
# (e.g. OPT, FUT, WAR, CFD, CASH/forex). Also fail-closed.
UNMANAGED_TYPE_REASON = ("instrument type {sec_type} is not a managed instrument type — "
                         "held aside pending classification (fail-closed)")

# Price conventions that differ from the plain `quantity * price`. Anything absent is 1.0.
# BOND: IBKR quotes percent-of-par per 100 against a FACE-amount quantity -> value/100.
PRICE_MULTIPLIER = {"BOND": 0.01}


# --- pure predicates -----------------------------------------------------------
def normalize_type(sec_type) -> str:
    """Canonical upper-case instrument type. None/blank/non-string -> ``UNKNOWN``.

    One normalization point so "bond", " BOND ", None and a missing mapping entry can never
    be handled three different ways."""
    if sec_type is None:
        return UNKNOWN
    text = str(sec_type).strip().upper()
    return text or UNKNOWN


def classify(sec_type) -> str:
    """``MANAGED`` or ``HELD_ASIDE`` for one instrument type. FAIL CLOSED: anything not
    explicitly in ``MANAGED_TYPES`` — including UNKNOWN — is HELD_ASIDE."""
    return MANAGED if normalize_type(sec_type) in MANAGED_TYPES else HELD_ASIDE


def is_held_aside(sec_type) -> bool:
    """True iff this instrument type is never traded by the desk."""
    return classify(sec_type) == HELD_ASIDE


def needs_classification(sec_type) -> bool:
    """True iff a HUMAN must tell us what this instrument is: it is held aside but NOT
    because of a deliberate policy decision — we simply do not recognise the type. A BOND
    is held aside on purpose and does NOT need classification; a blank/unknown/OPT does."""
    t = normalize_type(sec_type)
    return t not in MANAGED_TYPES and t not in HELD_ASIDE_TYPES


def reason_for(sec_type) -> str:
    """The plain-English reason a holding of this type is held aside ("" when managed)."""
    t = normalize_type(sec_type)
    if t in MANAGED_TYPES:
        return ""
    if t in HELD_ASIDE_TYPES:
        return HELD_ASIDE_TYPES[t]
    if t == UNKNOWN:
        return UNKNOWN_REASON
    return UNMANAGED_TYPE_REASON.format(sec_type=t)


def price_multiplier(sec_type) -> float:
    """Multiplier turning ``quantity * price`` into a real dollar value for this type
    (1.0 for equities; 0.01 for a BOND's percent-of-par-per-100 quote)."""
    return float(PRICE_MULTIPLIER.get(normalize_type(sec_type), 1.0))


def _finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def position_value(quantity, price, sec_type, reported_value=None):
    """Dollar market value of one position, or ``None`` when it cannot be priced.

    Precedence (deliberate, and identical to the pre-existing bond valuation so numbers do
    not move): a real POSITIVE price wins, using this type's price convention; otherwise
    the broker's own reported market value is used if it is a real non-zero number;
    otherwise ``None`` — which callers must treat as "cannot value, fail closed", never
    as zero."""
    if _finite(price) and float(price) > 0 and _finite(quantity):
        return float(quantity) * float(price) * price_multiplier(sec_type)
    if reported_value is not None and _finite(reported_value) and float(reported_value) != 0.0:
        return float(reported_value)
    return None


# --- the held-aside record (the reporting half) --------------------------------
@dataclass(frozen=True)
class HeldAsidePosition:
    """ONE held-aside holding, fully accounted for: what it is, how much of it, what it is
    worth, and WHY it is not traded. This is the record every downstream surface renders —
    a held-aside holding is never a bare number that vanishes into NAV."""
    symbol: str
    sec_type: str                 # normalized instrument type (UNKNOWN when undeterminable)
    quantity: float
    price: float | None           # the per-unit quote used (None when unpriced)
    market_value: float | None    # dollars (None when it could not be priced)
    reason: str                   # plain English, shown to a human
    needs_classification: bool    # True -> a human must say what this instrument is

    @property
    def priced(self) -> bool:
        """True iff this position carries a usable dollar value."""
        return self.market_value is not None

    def as_dict(self) -> dict:
        """Plain dict for JSON/dataframe surfaces (dashboard, daily report)."""
        return {"symbol": self.symbol, "sec_type": self.sec_type,
                "quantity": self.quantity, "price": self.price,
                "market_value": self.market_value, "reason": self.reason,
                "needs_classification": self.needs_classification}


# --- the carve-out -------------------------------------------------------------
UNPRICED_BLOCK_REASON = ("held-aside holding {symbol} could not be priced — the managed "
                         "sleeve cannot be sized safely, so NO orders are emitted for this "
                         "account (fail-closed; human review)")
OVERVALUED_BLOCK_REASON = ("held-aside value {held:,.2f} exceeds the account NetLiq "
                           "{net_liq:,.2f} — the two disagree too much to size the managed "
                           "sleeve, so NO orders are emitted (fail-closed; human review)")


@dataclass
class CarveOut:
    """The result of splitting one account into its MANAGED sleeve and its HELD-ASIDE block.

    ``managed_net_liq + held_aside_value == net_liq`` by construction (both priced), which
    is exactly the "total = managed sleeve + held aside" statement a professional readout
    has to be able to make."""
    net_liq: float                                   # the whole account, unchanged
    managed_net_liq: float                           # what the model's 100% applies to
    held_aside_value: float                          # priced total of the held-aside block
    managed_positions: dict = field(default_factory=dict)   # symbol -> qty (engine input)
    held_aside: list = field(default_factory=list)          # HeldAsidePosition records
    blocked_reasons: list = field(default_factory=list)     # non-empty -> emit NO orders

    @property
    def unclassified(self) -> list:
        """Held-aside positions a human must classify (unknown/unrecognised type)."""
        return [h for h in self.held_aside if h.needs_classification]

    @property
    def unpriced(self) -> list:
        """Held-aside positions that could not be valued (the fail-closed blockers)."""
        return [h for h in self.held_aside if not h.priced]


def carve_out(net_liq: float, positions: dict,
              sec_types: dict | None = None,
              prices: dict | None = None,
              values: dict | None = None) -> CarveOut:
    """Split one account's positions into the MANAGED sleeve and the HELD-ASIDE block.

    ``sec_types`` maps symbol -> instrument type (IBKR ``contract.secType`` on the live
    lane, ``asset_category`` on the CRM lane). It is the ONLY classification input.

      * ``sec_types is None`` (the default, and every pre-existing caller) — NOTHING is
        held aside. ``managed_positions`` is ``positions`` unchanged, ``managed_net_liq``
        is ``net_liq``, and there is no held-aside block. This is the behavior-preserving
        path: an account with no classification data plans exactly as it does today.
      * ``sec_types`` supplied — it must cover every held symbol. A symbol NOT in the
        mapping is UNKNOWN and therefore held aside and flagged (fail-closed).

    ``prices`` (symbol -> quote) and ``values`` (symbol -> broker-reported market value)
    price the held-aside block; see ``position_value`` for the precedence. A held-aside
    position that cannot be priced adds a blocked reason — the account still REPORTS
    everything, but emits no orders.

    Zero-quantity held-aside entries are dropped (nothing to price or report); zero-quantity
    MANAGED entries are passed through untouched so the reconcile input is unchanged.
    PURE — no broker, no config, no I/O."""
    net_liq = float(net_liq or 0.0)
    positions = positions or {}

    if sec_types is None:
        # Behavior-preserving default: no classification data -> no carve-out at all.
        return CarveOut(net_liq=net_liq, managed_net_liq=net_liq, held_aside_value=0.0,
                        managed_positions=dict(positions))

    prices = prices or {}
    values = values or {}

    managed: dict = {}
    held: list[HeldAsidePosition] = []
    blocked: list[str] = []

    for symbol in positions:
        qty = float(positions[symbol] or 0.0)
        raw_type = sec_types.get(symbol)
        sec_type = normalize_type(raw_type)
        if classify(sec_type) == MANAGED:
            managed[symbol] = positions[symbol]
            continue
        if qty == 0:
            continue                       # nothing held -> nothing to price or report
        price = prices.get(symbol)
        mv = position_value(qty, price, sec_type, reported_value=values.get(symbol))
        held.append(HeldAsidePosition(
            symbol=symbol, sec_type=sec_type, quantity=qty,
            price=float(price) if _finite(price) else None,
            market_value=mv, reason=reason_for(sec_type),
            needs_classification=needs_classification(sec_type)))
        if mv is None:
            blocked.append(UNPRICED_BLOCK_REASON.format(symbol=symbol))

    held.sort(key=lambda h: h.symbol)
    held_value = sum(h.market_value for h in held if h.market_value is not None)

    # A held-aside block worth more than the whole account means the two data sources
    # disagree (a stale snapshot, a mis-scaled mark). Sizing the managed sleeve off the
    # difference would be sizing off a number we do not believe -> fail closed.
    if held_value > net_liq:
        blocked.append(OVERVALUED_BLOCK_REASON.format(held=held_value, net_liq=net_liq))

    managed_net_liq = max(net_liq - held_value, 0.0)
    return CarveOut(net_liq=net_liq, managed_net_liq=managed_net_liq,
                    held_aside_value=held_value, managed_positions=managed,
                    held_aside=held, blocked_reasons=blocked)
