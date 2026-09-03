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
This increment MOVES the s0_live_deploy execution logic into the shared engine. The
rebalance_execute arm-gate unification (armed_session) and the arming.probe consolidation
(the gateway read-only probe now lives once in connections.gateway_probe; this module keeps
a thin same-named wrapper) are DONE (spec §2.2).
"""
from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace

# NOTE: `Stock` is deliberately NOT imported here any more. This module no longer
# reconstructs a contract from a ticker string — the broker's own qualified contract is used
# for a held symbol, and the fallback construction lives in ONE place (live_quotes).
from connections import gateway_probe

import config
import investable as _investable   # the SHARED price-validity helper (usable_price)
import live_quotes
import order_router
import pdt_guard
import s0_live
import s4_risk

# The live-trade Gateway port this executor transmits on. Contextual only — passed to the
# shared probe for its log/error messages; the actual connection is opened by the caller.
LIVE_TRADE_PORT = 4003

# ----------------------------------------------------------------------------------------
# EXECUTION MODES — PREVIEW (transmit nothing) or ARMED (transmit iff the full gate passes).
# ----------------------------------------------------------------------------------------
MODE_PREVIEW = "PREVIEW"
MODE_ARMED = "ARMED"

# ----------------------------------------------------------------------------------------
# EXECUTION PURPOSE — DEPLOY (first-deploy: liquidate aliens + fully conform) vs REBALANCE
# (ongoing lane: trim to target, leave non-target holdings). DEPLOY requires `conform` to
# transmit; REBALANCE does not. There is no third purpose.
# ----------------------------------------------------------------------------------------
PURPOSE_DEPLOY = "DEPLOY"
PURPOSE_REBALANCE = "REBALANCE"

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

# PER-ORDER FAT-FINGER RAIL (2026-08-25 rebuild — owner decision D3, 2026-08-19: "NO ORDER
# CAPS. %NLV and max-notional caps just create issues").
#
# WHAT WAS HERE: MAX_ORDER_NOTIONAL_PCT_NLV = 0.50 — no single order's notional could exceed
# 50% of NetLiq. That rail is STRUCTURALLY wrong for this desk: a never-invested account's
# FIRST deploy buys the model's largest leg at its full model weight, which is ~85% of NetLiq
# BY CONSTRUCTION (Growth (Small) = SCHB 85% + USFR 15%). It therefore blocked the initial
# deploy of EVERY never-invested account even when fully armed — observed 2026-08-25 in the
# read-only whole-roster preview on U5721712 (NetLiq 957.10, zero positions):
#     order BUY SCHB x27 notional 800.55 > 50% of NetLiq (478.55)
#
# WHAT REPLACES IT: a rail measured against what the PLAN ITSELF calls for, not against an
# arbitrary slice of NetLiq. It cannot false-block a legitimate order of any size, and it is
# strictly TIGHTER than the old rail in the direction that can actually hurt:
#   * BUY  — notional may not exceed MAX_ORDER_MODEL_MULTIPLE x the model's own target dollars
#            for that symbol (weight x managed NetLiq), floored at one share so a single share
#            is always permitted. Catches BOTH fat-finger classes on the only side that can
#            overspend: a 10x quantity and a 10x limit price both blow through the multiple.
#   * SELL — quantity may not exceed the shares the account ACTUALLY HOLDS. This desk never
#            shorts, and by construction |delta| <= int(actual_shares) for a sell (an alien
#            liquidation is exactly int(actual_shares)), so this is an exact invariant check:
#            zero false blocks, and it catches a corrupted quantity the %NLV cap would have
#            waved through whenever the position was under half of NetLiq. A too-high SELL
#            limit simply does not fill (limit orders protect), so notional is not the risk
#            on that side and never was.
# The plan-level total-BUY-<=-investable cap (ExecutionCaps.total_buy_le_investable) is
# untouched: it is an arithmetic consistency check against over-deploying, not a "cap" in the
# sense D3 refused, and it already bounds every BUY leg by the account's investable capital.
MAX_ORDER_MODEL_MULTIPLE = 2.0
# Sell legs on a plan carrying no per-symbol holding record (a synthetic/partial plan) have no
# share count to check against; they fall back to this multiple of managed NetLiq as a pure
# implausibility backstop. A real AccountPlan always carries `lines` / `alien_lines`.
MAX_SELL_NOTIONAL_NLV_MULTIPLE = 2.0

# TWO-PHASE / RE-PRICE / CASH-GATE tuning (2026-07-28 rebuild). All bounded so a run always
# terminates and never blocks on the wire.
PHASE_TERMINAL_TIMEOUT_SEC = 90.0    # max wait for one phase's legs to reach terminal state
REPRICE_AFTER_SEC = 18.0             # unfilled longer than this -> cancel + re-price (chase)
REPRICE_MAX_ATTEMPTS = 3             # cap on cancel-replace re-prices per leg
POLL_SEC = 1.0                       # phase poll cadence
CASH_SETTLE_SEC = 3.0                # let streaming account values update after the sells fill
POSITION_SETTLE_SEC = 3.0            # let the position stream catch up before reconciling
# Keep a small slice of realized cash UNSPENT so rounding / a late fill can never tip negative.
CASH_SAFETY_BUFFER_PCT = 0.01
# Terminal order statuses (filled OR done-without-fill). Mirrors ib_async DoneStates + the
# reject/inactive set order_router already treats as terminal.
_TERMINAL_STATUSES = frozenset({
    "Filled", "Cancelled", "ApiCancelled", "Inactive", "Rejected", "ValidationError",
})

# The subset of terminal statuses that mean THE BROKER REFUSED THE ORDER, as opposed to the
# order having worked and then been cancelled by us at the phase timeout. The distinction is
# load-bearing for the operator: a refusal never sat on the book, so it was never chased, and
# reporting it as "chased to cap, gave up" sends the reader looking for a market that moved
# away when in fact IBKR declined the order outright. Found live 2026-09-02: JAAA came back
# Inactive with reprices=0 in two accounts and was reported as a failed chase.
_BROKER_REFUSED_STATUSES = frozenset({"Inactive", "Rejected", "ValidationError"})

# CODES IBKR SENDS AS "ERRORS" THAT ARE PURELY INFORMATIONAL — THE ORDER IS STILL LIVE.
# ib_async's wrapper.error() keeps its own `warningCodes` set and treats anything OUTSIDE it as
# a real failure, which sets `trade.orderStatus.status = Cancelled` on a LIVE order (site-
# packages/ib_async/wrapper.py, the `elif trade:` branch). Neither of these is in that set:
#   10311 "This order will be directly routed to <EXCH>. Direct routed orders may result in
#          higher trade fees. Restriction is specified in Precautionary Settings..."
#   10349 "Order TIF was set to DAY based on order preset."
# MEASURED 2026-09-03: all 36 legs of an armed run came back locally "Cancelled / filled 0"
# on 10311 while every one of them was live at IBKR and filled — positions moved, the desk
# reported nothing had happened, and the re-price ladder never engaged because a "cancelled"
# order is terminal. Known upstream (ib-api-reloaded/ib_async discussion #190, worse on TWS
# 10.45+; this gateway is build 1045).
_BENIGN_BROKER_WARNING_CODES = frozenset({10311, 10349})


def _spuriously_cancelled(trade) -> bool:
    """True when the ONLY thing that marked this trade terminal was one of the benign
    informational codes above — i.e. the order is still live at the broker and our local copy
    is wrong. Reads the trade log backwards to the entry that set the terminal status.

    Conservative by construction: returns False unless the status is one ib_async writes for
    this failure (Cancelled / ValidationError), the trade reports nothing filled, and the most
    recent log entry carrying a code carries a BENIGN one. Any real cancel, rejection or fill
    lands a different status or a different code, and is left alone. PURE and defensive."""
    try:
        status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
        if status not in ("Cancelled", "ValidationError"):
            return False
        if float(getattr(getattr(trade, "orderStatus", None), "filled", 0.0) or 0.0) > 0:
            return False
        for entry in reversed(list(getattr(trade, "log", None) or ())):
            code = getattr(entry, "errorCode", None)
            if code in (None, 0, "0"):
                continue
            try:
                return int(code) in _BENIGN_BROKER_WARNING_CODES
            except (TypeError, ValueError):
                return False
    except Exception:  # noqa: BLE001 — never break a transmit on a reporting helper
        return False
    return False

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
    """The per-run sanity rails. `per_order_model_multiple`: a BUY leg's notional may not
    exceed this multiple of the model's own target dollars for that symbol (weight x managed
    NetLiq) — the fat-finger rail, measured against the plan rather than against a flat slice
    of NetLiq (see MAX_ORDER_MODEL_MULTIPLE for why the old %NLV cap was removed, owner
    decision D3 2026-08-19). `total_buy_le_investable`: total BUY notional AT THE PLAN'S OWN
    PRICE BASIS (qty x the reference price the engine sized on) must not exceed the plan's
    investable — NOT the worst-case-at-the-marketable-cap total, which is a different basis and
    refused whole accounts for an overspend that cannot happen (see the check in execute_plan).
    `max_total_notional`: an optional absolute ceiling on total notional (None = not enforced;
    the investable cap already bounds deployment).

    SELL legs are rails-checked against the shares actually held, which needs no knob."""
    per_order_model_multiple: float = MAX_ORDER_MODEL_MULTIPLE
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
    # DEPLOY (default) requires `conform` to transmit — the first-deploy lane that liquidates
    # aliens and fully conforms the book. REBALANCE is the ongoing lane that does NOT require
    # conform: it trims to target and leaves non-target holdings (reported in aliens_left).
    # LAST field with a default so every existing positional/keyword construction is unchanged.
    purpose: str = PURPOSE_DEPLOY
    # {symbol: the BROKER'S OWN already-qualified contract}, from ib.positions(). Threaded
    # exactly like `quotes`/`prices` — the caller measured it, the engine uses it. A leg for a
    # symbol in here is placed against the broker's contract (its real conId and real secType,
    # which is how IBKR identifies the instrument), which is what makes a SELL of a MUTUAL FUND
    # an order that exists rather than an order against a US-stock contract that does not have
    # a conId at all. Anything absent falls back to
    # the historic Stock(symbol, "SMART", "USD"), qualified before use. Defaulted and LAST so
    # every existing construction (crm_execute's included) is unchanged.
    contracts: dict = field(default_factory=dict)


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
    single-account deploy wall this is EXACTLY the s0_live_deploy check (and reason string);
    for a multi-account roster the reason names the enrolled execution roster instead. Only
    the WORDING generalizes — the logic/return contract is unchanged. Read at call time so a
    test/monkeypatch of the account is honored."""
    allowed = list(allowed_accounts)
    if account not in allowed:
        if len(allowed) == 1:
            return False, (f"target account {account} is not the single allowed account "
                           f"{allowed[0]} — refusing.")
        roster_str = "{" + ", ".join(map(str, allowed)) + "}"
        return False, (f"target account {account} is not in the enrolled execution roster "
                       f"{roster_str} — refusing.")
    return True, ""


# ========================================================================================
# GATEWAY READ-ONLY PROBE — now the ONE shared, port-parameterized probe (consolidated with
# arming's, spec §2.2 item 2, conductor #64). Kept as a thin, same-named wrapper so execute_plan
# and the Control-Plane probe (dashboard/desk/gateway_arm_probe.py, via s0_live_deploy's
# re-export) keep importing `_probe_gateway_readonly` unchanged; the ZERO-TRANSMISSION technique
# lives once in connections.gateway_probe.
# ========================================================================================
def _probe_gateway_readonly(ib, timeout: int = 15) -> bool:
    """Return True if the OPEN live-trade (4003) connection's Gateway is READ-ONLY
    (transmission physically BLOCKED), False if it is WRITE-ENABLED (armed). Thin wrapper over
    the shared connections.gateway_probe.probe_api_readonly — same zero-transmission cancel-a-
    fabricated-order technique, same FAIL-CLOSED default (no decisive signal -> True/refuse)."""
    return gateway_probe.probe_api_readonly(ib, port=LIVE_TRADE_PORT, timeout=timeout)


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


def _margin_preflight_ok(summary, net_liq, total_buy, plan, target) -> tuple[bool, str]:
    """Self-computed per-account MARGIN pre-flight (#57), reusing s4_risk.margin_preflight.

    Computes the account's intended POST-TRADE risk exposure as a FRACTION of NAV from the
    plan's investable — the strategy's OWN risk-deployment ceiling, (NAV - reserve) *
    (1 - cash_reserve_pct). For an UNLEVERED S0 book that is ~0.985 (fully invested minus the
    ~1.5% buffer) and is STRUCTURALLY <= 1.0 (investable can never exceed NAV without borrow).
    Using the ceiling is the conservative choice: it never UNDER-states exposure, so a genuinely
    levered book can't slip through, while an unlevered book stays at/below 1.0.

    leverage_cap is 1.0: every account routed here (S0 / ongoing rebalance) is UNLEVERED — no
    borrowing permitted. (A future per-account profile could raise this cap for a deliberately
    levered strategy; for now all such accounts are held to 1.0.)

    HARD INVARIANT: for exposure <= 1.0 this returns (True, "") on ANY account type. margin_
    preflight's unlevered branch never reads BuyingPower/AccountType, so a thin/empty summary
    AND the trust account U14438624 (AccountType='TRUST', BuyingPower > NetLiq) both pass with
    ZERO reasons added — matching _buying_power_ok's fail-open-on-unreadable stance. It fails
    CLOSED only on a genuinely levered (exposure > 1.0) request that cannot confirm margin
    capacity (cash/unknown account, thin BP), exactly like s4_risk.margin_preflight.

    Returns (True, "") when the run may proceed; (False, reason) when it must be refused.
    `total_buy`/`target` are accepted for signature completeness / future per-account profiles."""
    nav = float(net_liq or 0.0)
    investable = float(getattr(plan, "investable", 0.0) or 0.0)
    # Intended post-trade risk exposure as a fraction of NAV. nav<=0 is already blocked upstream
    # by the per-order cap (which runs before this gate), so this guard only avoids a divide-by-
    # zero on a pathological summary; a 0.0 exposure then trivially clears the unlevered branch.
    exposure = (investable / nav) if nav > 0 else 0.0
    pf = s4_risk.margin_preflight(summary, nav=nav, exposure=exposure, leverage_cap=1.0)
    if not pf.ok:
        return False, (f"margin pre-flight REFUSED (intended exposure {exposure:.4f}x of NAV, "
                       f"leverage_cap 1.0): " + "; ".join(pf.reasons))
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


def _leg_plan_notional(leg, prices: dict) -> float:
    """One leg's notional at the PLAN's OWN price basis — `qty * prices[symbol]`, the same
    REFERENCE price rebalance_engine sized the position on (target_shares = int(weight *
    investable / reference_price)).

    This is deliberately NOT the leg's `notional`, which is `qty * cap` where cap is the
    WORST-CASE marketable crossing price (ask * (1 + ORDER_CAP_K)) — a price the order is
    permitted to pay but, in the ordinary case, does not. Comparing a cap-priced total to an
    investable figure computed on the reference basis compares two different bases; see the
    total_buy_le_investable check in execute_plan for the incident that produced this.

    FAILS CLOSED PER LEG: when the symbol carries no USABLE price (None / NaN / non-positive
    — judged by the ONE shared rule, investable.usable_price, never a fresh one written here),
    this falls back to the leg's own cap-priced `notional`, which is strictly the LARGER of the
    two. An unreadable price therefore makes the gate STRICTER, never looser. PURE."""
    px = _investable.usable_price(prices.get(leg.symbol))
    if px is None:
        return float(leg.notional)
    return float(leg.qty) * px


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
def leg_sec_type(leg, contracts: dict | None = None) -> str:
    """The instrument type ONE leg is for, as the BROKER names it. Prefers the type stamped on
    the leg at build time, falling back to the broker's own contract map. "" when unknown —
    which every fund-specific branch treats as "not a fund", i.e. the ordinary path. PURE."""
    st = getattr(leg, "sec_type", None)
    if not st:
        c = (contracts or {}).get(getattr(leg, "symbol", None))
        st = getattr(c, "secType", None)
    return str(st or "")


def is_fund_leg(leg, contracts: dict | None = None) -> bool:
    """True iff this leg is for a MUTUAL FUND — the ONE predicate every fund-specific branch
    on this rail keys off (pricing, order type, fractional quantity, settlement). Delegates to
    live_quotes.is_fund so there is a single definition of "is it a fund" on the desk."""
    return live_quotes.is_fund(leg_sec_type(leg, contracts))


def _qty_text(qty) -> str:
    """A leg quantity for display. An int renders EXACTLY as it always has (`{qty:<8d}`) so an
    all-ETF preview is byte-identical; a fractional fund quantity renders with its fraction
    intact instead of raising on the int-only format."""
    return f"{qty:<8d}" if isinstance(qty, int) else f"{float(qty):<8,.4f}"


def build_deploy_legs(plan, quotes: dict, prices: dict, *, conform: bool,
                      contracts: dict | None = None):
    """PURE order-list construction from an already-sized AccountPlan. Builds and transmits
    NOTHING — returns the ordered candidate legs plus review metadata.

    Legs:
      * the engine's plan.orders (signed deltas -> BUY/SELL to reach the target), PLUS
      * CONFORM mode (opt-in): for each plan.alien_lines entry, a SELL of its full
        whole-share count (liquidate to 0). When conform is False, ALIEN holdings produce NO
        leg and are returned in `aliens_left` for the preview to list as "would remain".

    SELLS ARE SEQUENCED BEFORE BUYS (raise cash before buying): the returned `legs` list is
    plan-sells + alien-liquidations + plan-buys, so every SELL precedes every BUY.

    WHOLE-SHARE, WITH EXACTLY ONE EXCEPTION: every quantity is an int (deltas are already
    integer; an alien's fractional share count is truncated toward 0 — a sub-1-share alien
    can't be whole-share liquidated and is returned in `aliens_left`). The exception is a
    MUTUAL FUND being sold OUT COMPLETELY — see below.

    `contracts` maps symbol -> the BROKER'S OWN contract (from ib.positions()), and is used
    for ONE thing: reading `secType` so a mutual fund can be recognised as one. Absent (the
    default, and every pre-existing caller) nothing is treated as a fund and every leg is
    built exactly as before.

    SELLING A MUTUAL FUND OUT — WHY THE QUANTITY IS FRACTIONAL
    ----------------------------------------------------------
    Mutual-fund positions are fractional by nature (Stevens holds 123.73 AFMBX, 17.393 MFEKX).
    The engine works in whole shares, so its delta to exit a 123.73-share position is -123 —
    which would sell 123 shares and leave a 0.73-share stub behind, and the account would
    never actually close the holding or be convertible to its model. So when, and ONLY when,
    a leg is (a) for a FUND, (b) a SELL, and (c) the plan's whole-share delta is exactly the
    truncation of the ENTIRE holding — i.e. the plan's intent is a complete exit — the
    quantity is restored to the FULL fractional share count off the plan's own reconciliation.
    A fund the plan intends to only PARTIALLY sell (which this desk never does; it does not
    buy funds either) keeps its whole-share quantity, unchanged.

    Returns (legs, aliens_left, unpriceable):
      legs        : ordered list of SimpleNamespace(symbol, side, qty, limit, notional,
                    source, sec_type)
      aliens_left : alien lines NOT liquidated (conform False, or a sub-1-share alien)
      unpriceable : list of (symbol, side, qty) with no usable price -> a blocking reason
    """
    sells: list = []
    buys: list = []
    unpriceable: list = []
    contracts = contracts or {}

    def _sec_type(sym) -> str:
        return str(getattr(contracts.get(sym), "secType", None) or "")

    for sym in sorted(plan.orders):
        qty = int(plan.orders[sym])          # whole-share; engine deltas are already integer
        if qty == 0:
            continue
        side = "BUY" if qty > 0 else "SELL"
        qty = abs(qty)
        sec_type = _sec_type(sym)
        if live_quotes.is_fund(sec_type) and side == "SELL":
            # SELL THE WHOLE FUND POSITION, FRACTION INCLUDED — but only when the plan's
            # whole-share delta IS the complete exit. held is the account's real (fractional)
            # holding off the plan's own reconciliation; int(held) == qty is precisely the
            # "plan wants this position gone" test.
            held = _held_shares(plan, sym)
            if held is not None and held > 0 and int(held) == qty:
                qty = float(held)
        cap = _leg_cap(side, sym, quotes, prices)
        if cap is None:
            unpriceable.append((sym, side, qty))
            continue
        leg = SimpleNamespace(symbol=sym, side=side, qty=qty, limit=cap,
                              notional=qty * cap, source="plan", sec_type=sec_type)
        (buys if side == "BUY" else sells).append(leg)

    alien_sells: list = []
    aliens_left: list = []
    if conform:
        for ln in plan.alien_lines:
            qty = int(ln.actual_shares)      # truncate toward 0 — never fractional
            if qty < 1:
                aliens_left.append(ln)       # sub-1-share alien: can't whole-share liquidate
                continue
            sec_type = _sec_type(ln.symbol)
            if live_quotes.is_fund(sec_type):
                # Same rule as the plan-sell branch above: liquidating a fund means the WHOLE
                # position, fraction included, or the holding never actually closes.
                qty = float(ln.actual_shares)
            cap = _leg_cap("SELL", ln.symbol, quotes, prices)
            if cap is None:
                unpriceable.append((ln.symbol, "SELL", qty))
                continue
            alien_sells.append(SimpleNamespace(
                symbol=ln.symbol, side="SELL", qty=qty, limit=cap,
                notional=qty * cap, source="alien_liquidation", sec_type=sec_type))
    else:
        aliens_left = list(plan.alien_lines)

    # SELLS (plan sells + alien liquidations) BEFORE BUYS — raise cash first.
    legs = sells + alien_sells + buys
    return legs, aliens_left, unpriceable


# ========================================================================================
# PER-ORDER FAT-FINGER RAIL — measured against the PLAN, not a flat slice of NetLiq.
# ========================================================================================
def _held_shares(plan, symbol: str) -> float | None:
    """The shares of `symbol` the account actually holds, off the plan's own reconciliation
    (`lines` for managed holdings, `alien_lines` for review-only ones). None when the plan
    carries no record for that symbol — a synthetic/partial plan; the caller then falls back
    to an implausibility backstop rather than pretending to know. PURE."""
    best: float | None = None
    for ln in list(getattr(plan, "lines", None) or []) + \
            list(getattr(plan, "alien_lines", None) or []):
        if getattr(ln, "symbol", None) != symbol:
            continue
        try:
            shares = float(getattr(ln, "actual_shares", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        best = shares if best is None else max(best, shares)
    return best


def per_order_rail_reasons(legs, plan, target, *, managed_net_liq: float,
                           model_multiple: float = MAX_ORDER_MODEL_MULTIPLE) -> list[str]:
    """PURE. The per-order fat-finger rail: return one blocking reason per leg that fails it
    (empty list = every leg is plausible). Builds and transmits nothing.

    BUY  — notional <= model_multiple x (target weight x managed NetLiq), floored at one
           share's limit so a single share is never refused. A first deploy into a full model
           weight (85% of NetLiq for Growth (Small)) passes by construction; a 10x quantity or
           a 10x limit price does not.
    SELL — quantity <= the shares actually held (this desk never shorts). Falls back to
           notional <= MAX_SELL_NOTIONAL_NLV_MULTIPLE x managed NetLiq only when the plan
           carries no holding record for the symbol.
    """
    weights = getattr(target, "weights", None)
    reasons: list[str] = []
    for l in legs:
        if l.side == "BUY":
            try:
                w = float(weights.get(l.symbol, 0.0)) if weights is not None else 0.0
            except (TypeError, ValueError):
                w = 0.0
            if w != w:                       # NaN weight -> treat as no allocation
                w = 0.0
            allowance = max(w * managed_net_liq * model_multiple, float(l.limit))
            if l.notional > allowance:
                reasons.append(
                    f"order BUY {l.symbol} x{l.qty} notional {l.notional:,.2f} exceeds "
                    f"{model_multiple:g}x the model's target for {l.symbol} "
                    f"({w*100:.2f}% of managed NetLiq {managed_net_liq:,.2f} = "
                    f"{w*managed_net_liq:,.2f}; allowed {allowance:,.2f}) — "
                    f"fat-finger / bad-price rail")
            continue
        held = _held_shares(plan, l.symbol)
        if held is None:
            ceiling = MAX_SELL_NOTIONAL_NLV_MULTIPLE * managed_net_liq
            if l.notional > ceiling:
                reasons.append(
                    f"order SELL {l.symbol} x{l.qty} notional {l.notional:,.2f} exceeds "
                    f"{MAX_SELL_NOTIONAL_NLV_MULTIPLE:g}x managed NetLiq "
                    f"({ceiling:,.2f}) and the plan carries no holding record for "
                    f"{l.symbol} to size against — fat-finger rail")
        elif l.qty > held + 1e-6:
            reasons.append(
                f"order SELL {l.symbol} x{l.qty} exceeds the {held:,.4f} share(s) actually "
                f"held — this desk never shorts; fat-finger rail")
    return reasons


def _unsettled_fund_proceeds(sell_results, sell_legs, contracts: dict | None = None) -> float:
    """PURE. Dollars of MUTUAL-FUND sale proceeds that this run must NOT let anything buy
    against, measured from what actually filled.

    Mutual-fund proceeds do not settle the same day, so no fund sale — however much of it
    filled — is spendable on the run that placed it. This returns `filled shares x the last
    NAV` summed over the fund SELL legs, which is 0 in the ordinary case (a fund order placed
    during the day has not filled: it fills at tonight's NAV) and a real figure only when a
    run happens to see a fill. The caller subtracts it from the realized-cash budget.

    Measured, never assumed: a leg the broker did not fill contributes nothing, so an
    all-ETF run — and a run whose fund orders are simply working — is arithmetically
    unchanged. Non-fund legs are ignored entirely; ordinary equity settlement is untouched by
    this and is governed, as before, by the broker's own cash figure."""
    by_symbol = {l.symbol: l for l in (sell_legs or []) if is_fund_leg(l, contracts)}
    total = 0.0
    for r in sell_results or []:
        leg = by_symbol.get(r.get("symbol"))
        if leg is None:
            continue
        try:
            filled = float(r.get("filled") or 0.0)
        except (TypeError, ValueError):
            filled = 0.0
        if filled > 0:
            total += filled * float(leg.limit)
    return total


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
    the orderStatus.status against the terminal set.

    EXCEPTION: a trade our client marked terminal ONLY because of a benign informational code
    (:func:`_spuriously_cancelled`) is NOT done — the order is still working at IBKR. Treating
    it as done is what stopped the re-price ladder and made an armed run report 36 phantom
    cancellations on 2026-09-03. Keep watching it; a genuine fill or cancel will land a real
    status, and if nothing does, the phase timeout cancels it like any other straggler."""
    if _spuriously_cancelled(trade):
        return False
    try:
        return bool(trade.isDone())
    except Exception:
        st = getattr(trade, "orderStatus", None)
        return getattr(st, "status", "") in _TERMINAL_STATUSES


def _broker_message(trade) -> tuple[str, str]:
    """(message, errorCode) IBKR attached to this order, or ("", "").

    ib_async appends every status change AND every error the gateway sends for an order to
    ``Trade.log`` as TradeLogEntry(time, status, message, errorCode). That text is the ONLY
    record of WHY the broker refused an order, and the desk used to discard it. Reads the log
    backwards and returns the most recent entry carrying either a message or an error code.
    PURE and defensive: any missing/renamed attribute yields ("", "") rather than raising —
    this runs on the reporting path of a real-money transmit and must never be the thing that
    breaks a run."""
    try:
        entries = list(getattr(trade, "log", None) or ())
    except Exception:  # noqa: BLE001
        return ("", "")
    for entry in reversed(entries):
        msg = str(getattr(entry, "message", "") or "").strip()
        code = getattr(entry, "errorCode", None)
        code = "" if code in (None, 0, "0") else str(code)
        if msg or code:
            return (msg, code)
    return ("", "")


def _unfilled_reason(trade, status: str, attempts: int, cancelled_by_us: bool) -> str:
    """Plain-English reason ONE leg did not fully fill, naming what actually happened.

    Replaces a hardcoded "UNFILLED remainder (chased to cap, gave up)" that was emitted for
    every unfilled leg regardless of cause — including orders the broker refused outright and
    which were therefore never chased at all. Three distinct outcomes, each said differently:

      * the BROKER REFUSED it  -> say so, and carry IBKR's own error code and text verbatim
      * WE cancelled it at the phase timeout -> say so, with the true re-price count
      * anything else          -> report the raw status rather than inventing a story

    PURE."""
    msg, code = _broker_message(trade)
    if status in _BROKER_REFUSED_STATUSES:
        out = f"BROKER REFUSED THE ORDER (status {status}; it never worked, so it was never re-priced)"
        if code:
            out += f" — IBKR error {code}"
        if msg:
            out += f": {msg}"
        return out
    if cancelled_by_us:
        tail = ("after " + str(attempts) + " re-price(s)") if attempts else "without re-pricing"
        out = f"UNFILLED remainder — we cancelled it at the phase timeout {tail}"
        if code or msg:
            out += f" (last broker message{' ' + code if code else ''}: {msg})" if msg else                    f" (last broker code: {code})"
        return out
    out = f"UNFILLED remainder — ended {status or 'UNKNOWN'} after {attempts} re-price(s)"
    if code:
        out += f" — IBKR error {code}"
    if msg:
        out += f": {msg}"
    return out


def _cum_filled(active: dict) -> float:
    """Cumulative filled shares for a leg across any cancel/replace re-prices (fills on a
    cancelled order are carried in filled_prior; the live trade's own fill is added on top)."""
    st = getattr(active["trade"], "orderStatus", None)
    live = float(getattr(st, "filled", 0.0) or 0.0)
    return float(active.get("filled_prior", 0.0)) + live


def _leg_contract(ib, symbol, contracts=None):
    """THE contract one leg is placed against, or None when IBKR will not resolve the symbol.

    Prefers the BROKER'S OWN qualified contract when the account holds the symbol (ib.positions()
    carries it — real conId, real secType, real exchange). That is the half that makes a SELL of
    a MUTUAL FUND a real order: rebuilding it as Stock(symbol, "SMART", "USD") produces a
    contract IBKR does not know, so the order is wrong (or unplaceable) at transmit time even
    though the quote path succeeded. Anything NOT held falls back to that same historic Stock
    construction, qualified before use.

    Both cases go through the ONE shared chooser, live_quotes.qualified_contracts — no second
    rule, no lookup table, no exchange hardcoded here. None means: DO NOT PLACE THIS LEG."""
    picked, unqualified = live_quotes.qualified_contracts(ib, [symbol], known=contracts)
    if unqualified:
        return None
    return picked.get(symbol)


def _transmit_phase(ib, legs, *, account, as_of, run_id, phase_label, quotes, prices,
                    contracts=None):
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
        # THE CONTRACT, BEFORE THE ORDER. Fail closed: a symbol IBKR will not resolve has
        # nothing to place an order against, so the leg is NOT placed and IS reported —
        # exactly the posture an unpriceable leg already gets. One unrecognisable holding in
        # one account must never take the run down or send a wrong-contract order.
        contract = _leg_contract(ib, l.symbol, contracts)
        if contract is None:
            print(f"    [{phase_label}] NOT PLACED {l.side} {l.symbol} x{l.qty}: IBKR would "
                  f"not resolve a contract for this symbol, so there is nothing to place an "
                  f"order against. Reported, not traded.")
            results.append({"symbol": l.symbol, "side": l.side, "requested": l.qty,
                            "filled": 0.0, "status": "SKIPPED_UNQUALIFIED", "reprices": 0,
                            "skipped": True,
                            "reason": "IBKR would not resolve a contract for this symbol"})
            continue
        ref = _deploy_ref(account, as_of, l.side, l.symbol, run_id)
        # THE ORDER TYPE IS DECIDED BY WHAT THE INSTRUMENT IS, off the contract IBKR just
        # resolved — the broker's own identity for it, never a ticker-string guess.
        # A MUTUAL FUND takes a MARKET order with no limit price (it has no intraday price to
        # limit; it fills at tonight's NAV, and so does every other order entered today).
        # Everything else takes the capped marketable LMT it always has.
        fund = live_quotes.is_fund(getattr(contract, "secType", None))
        if fund:
            order = order_router.build_mutual_fund_market(
                l.symbol, l.side, l.qty, account=account, order_ref=ref)
        else:
            order = order_router.build_marketable_limit(
                l.symbol, l.side, l.qty, l.limit, account=account, order_ref=ref)
        order.transmit = True
        trade = ib.placeOrder(contract, order)
        px_text = ("MARKET (fills at tonight's NAV; last NAV "
                   f"{l.limit:,.2f})" if fund else f"LIMIT {l.limit:,.2f}")
        print(f"    [{phase_label}] SENT {l.side} {l.symbol} x{_qty_text(l.qty)} {px_text} "
              f"ref={ref}")
        if fund:
            # A FUND ORDER MUST BE LEFT WORKING — DO NOT ADD IT TO `active`.
            # `active` is the set this phase waits on, re-prices, and CANCELS at the
            # PHASE_TERMINAL_TIMEOUT_SEC deadline. A fund order cannot reach a terminal state
            # inside that window by construction: it does not fill until the fund strikes its
            # NAV after the close, hours later. Waiting on it would burn the whole phase
            # timeout and then CANCEL the very order we came to place — defeating the entire
            # purpose. Re-pricing is meaningless for the same reason (there is no price to
            # chase, and every order today fills at the same NAV).
            # It is reported here, honestly, as WORKING and unfilled: this run raised no cash
            # from it, which is exactly what the settlement rule downstream relies on.
            print(f"    [{phase_label}] {l.symbol} is a MUTUAL FUND: the order is now WORKING "
                  f"and will fill at tonight's NAV. This run does NOT wait for it, does NOT "
                  f"re-price it, and does NOT cancel it — and counts NONE of its proceeds as "
                  f"cash available to buy with today.")
            # ONE immediate, non-blocking status read so the report states what the broker
            # actually says rather than an assumption. In the ordinary case that is
            # PreSubmitted/Submitted with 0 filled; if the broker HAS already filled it (a run
            # after the NAV strike), the real fill is reported — and the settlement rule below
            # then excludes exactly those proceeds from this run's buying power.
            _st = getattr(trade, "orderStatus", None)
            _filled = float(getattr(_st, "filled", 0.0) or 0.0)
            results.append({"symbol": l.symbol, "side": l.side, "requested": l.qty,
                            "filled": _filled,
                            "status": str(getattr(_st, "status", "") or "") or "WORKING",
                            "reprices": 0, "skipped": False, "sec_type": "FUND",
                            "reason": ("mutual fund — left working, fills at tonight's NAV; "
                                       "proceeds do NOT settle today")})
            continue
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
        cancelled_by_us = not _trade_done(a["trade"])
        if cancelled_by_us:
            try:
                ib.cancelOrder(a["order"])    # give up: cancel the straggler, report loudly
            except Exception:
                pass
        st = getattr(a["trade"], "orderStatus", None)
        filled = _cum_filled(a)
        status = str(getattr(st, "status", "") or "")
        # WHY it did not fill, from the broker's own words where there are any — never a
        # hardcoded story. `cancelled_by_us` is captured BEFORE the cancel above, so a leg the
        # broker refused is never described as one we gave up chasing.
        broker_msg, broker_code = _broker_message(a["trade"])
        # IBKR's STRUCTURED rejection payload (advancedOrderRejectJson). ib_async parks it on
        # the trade as `advancedError`; it is the only place the broker explains a refusal in
        # machine-readable detail, and the desk used to discard it.
        advanced = str(getattr(a["trade"], "advancedError", "") or "")
        reason = ("" if filled >= l.qty
                  else _unfilled_reason(a["trade"], status, a["attempts"], cancelled_by_us))
        results.append({"symbol": l.symbol, "side": l.side, "requested": l.qty,
                        "filled": filled, "status": status, "reprices": a["attempts"],
                        "skipped": False, "reason": reason,
                        "broker_message": broker_msg, "broker_error_code": broker_code,
                        "broker_refused": status in _BROKER_REFUSED_STATUSES,
                        "broker_advanced_reject": advanced})
    return results


def _positions_for(ib, account: str) -> dict:
    """{symbol: shares} the BROKER says this account holds, right now. Never raises: a
    reconcile that cannot read positions must degrade to UNKNOWN, never take a run down."""
    out: dict = {}
    try:
        ib.reqPositions()
        ib.sleep(POSITION_SETTLE_SEC)
        for pos in ib.positions():
            if getattr(pos, "account", None) != account:
                continue
            qty = float(getattr(pos, "position", 0.0) or 0.0)
            if qty:
                out[str(pos.contract.symbol)] = qty
    except Exception as exc:  # noqa: BLE001
        print(f"    !! could not read positions for {account} ({type(exc).__name__}: {exc}) - "
              f"the position reconcile is UNAVAILABLE for this run.")
        return {}
    return out


def _reconcile_by_position(before: dict, after: dict, sell_results, buy_results) -> dict:
    """POSITIONS ARE THE SOURCE OF TRUTH. Compare what the account actually holds now against
    what it held before the phases, and set each leg filled_by_position from that delta rather
    than from the order status this client happens to be holding.

    WHY THIS EXISTS. On 2026-09-03 an armed run reported 36 legs as Cancelled with filled 0
    while every one of them had filled at IBKR - the client had marked live orders cancelled
    off a benign informational code (see _BENIGN_BROKER_WARNING_CODES). An order status is a
    CLAIM; the position file is the FACT. Every leg is annotated, disagreements are collected,
    and the caller reports the run outcome from the position side.

    Returns {"available", "legs", "disagreements", "unverifiable"}. PURE apart from mutating
    the result dicts it is handed. Empty before/after means the position read failed, and the
    whole reconcile reports available=False rather than guessing zero."""
    out = {"available": bool(after) or bool(before), "legs": [], "disagreements": [],
           "unverifiable": []}
    if not out["available"]:
        return out
    buy_syms = {x["symbol"] for x in (buy_results or [])}
    sell_syms = {x["symbol"] for x in (sell_results or [])}
    both = buy_syms & sell_syms
    for res, sign in ((sell_results or [], -1.0), (buy_results or [], 1.0)):
        for r in res:
            sym = r["symbol"]
            if sym in both:
                # Traded on BOTH sides in one run: a net delta cannot attribute either leg.
                r["filled_by_position"] = None
                out["unverifiable"].append(sym)
                continue
            delta = float(after.get(sym, 0.0)) - float(before.get(sym, 0.0))
            by_pos = abs(delta) if (delta * sign) > 0 else 0.0
            r["filled_by_position"] = by_pos
            reported = float(r.get("filled", 0.0) or 0.0)
            r["position_disagrees"] = abs(by_pos - reported) > 1e-6
            row = {"symbol": sym, "side": r["side"], "requested": r["requested"],
                   "reported_filled": reported, "position_filled": by_pos,
                   "status": r.get("status", "")}
            out["legs"].append(row)
            if r["position_disagrees"]:
                out["disagreements"].append(row)
    return out


def _effective_filled(r: dict) -> float:
    """How many shares a leg ACTUALLY moved. The position-derived figure when the reconcile
    established one, otherwise the order status. Positions first, always."""
    by_pos = r.get("filled_by_position")
    return float(r.get("filled", 0.0) or 0.0) if by_pos is None else float(by_pos)


def _report_reconcile(rec: dict) -> None:
    """Print the position reconcile, LOUDLY when the broker positions disagree with what the
    order statuses claimed. A silent disagreement is the failure this exists to catch."""
    if not rec.get("available"):
        print("")
        print("    !! POSITION RECONCILE UNAVAILABLE - positions could not be read, so the "
              "fills for this run are UNVERIFIED. Treat the per-leg statuses below as a claim, "
              "not a fact, and check the account before trading it again.")
        return
    if rec.get("unverifiable"):
        print("")
        print("    Position reconcile: {} traded on BOTH sides this run, so a net position "
              "delta cannot attribute them. Not a fault, just not verifiable this way.".format(
                  ", ".join(sorted(set(rec["unverifiable"])))))
    dis = rec.get("disagreements") or []
    if not dis:
        print("")
        print("    POSITION RECONCILE: CLEAN - every leg fill matches the change in the "
              "holdings the account actually reports.")
        return
    print("")
    print("    !!!! POSITION RECONCILE MISMATCH on {} leg(s). THE POSITIONS ARE THE TRUTH; "
          "the order status is only what this client believed. Where they differ the order "
          "status is wrong - this is the 2026-09-03 failure mode.".format(len(dis)))
    for d in dis:
        print("      -> {} {}: order status said filled {:g} [{}], but the position moved by "
              "{:g} of {:g} requested.".format(d["side"], d["symbol"], d["reported_filled"],
                                               d["status"], d["position_filled"],
                                               d["requested"]))


def _report_phase(label: str, results) -> None:
    """Print a phase's per-leg results and a LOUD summary of anything unfilled/skipped."""
    if not results:
        print(f"\n    {label} phase: no legs.")
        return
    print(f"\n    {label} phase results:")
    flagged: list[dict] = []
    working_funds: list[dict] = []
    for r in results:
        line = (f"      {r['side']:4s} {r['symbol']:6s} requested={r['requested']:g} "
                f"filled={r['filled']:g} status={r['status']} reprices={r['reprices']}")
        if r.get("skipped"):
            line += f"  SKIPPED ({r.get('reason', '')})"
        print(line)
        if r.get("skipped") or r["filled"] < r["requested"]:
            # A MUTUAL-FUND leg that is unfilled is NOT a problem to review — it is the
            # instrument working exactly as it must. It is listed separately, in plain
            # English, so it can never be mistaken for a failed order.
            (working_funds if (live_quotes.is_fund(r.get("sec_type"))
                               and not r.get("skipped")) else flagged).append(r)
    if working_funds:
        print(f"    {label} MUTUAL-FUND legs still WORKING (this is normal and expected — a "
              f"mutual fund prices once a day at NAV after the close, so an order placed "
              f"during the day cannot fill yet):")
        for r in working_funds:
            print(f"      -> {r['side']} {r['symbol']}: {r['requested']:g} share(s) working, "
                  f"will fill at tonight's NAV [{r['status']}]. Its proceeds were NOT counted "
                  f"as cash available to buy with on this run.")
    if flagged:
        print(f"    !! {label} UNFILLED / SKIPPED legs (LOUD — needs human review):")
        for r in flagged:
            print(f"      -> {r['side']} {r['symbol']}: requested {r['requested']:g}, filled "
                  f"{r['filled']:g} [{r['status']}] {r.get('reason', '')}")
        # A BROKER REFUSAL is a different animal from a leg that worked and did not fill, and
        # it gets its own banner so it cannot be read past. The desk asked, IBKR said no, and
        # IBKR's own words are the only thing that explains why.
        refused = [r for r in flagged if r.get("broker_refused")]
        if refused:
            print(f"    !! {label} — THE BROKER REFUSED {len(refused)} ORDER(S). These never "
                  f"reached the book and were never re-priced. IBKR's own words:")
            for r in refused:
                code = r.get("broker_error_code") or "(no code)"
                msg = r.get("broker_message") or "(no message returned)"
                print(f"      -> {r['side']} {r['symbol']} x{r['requested']:g} "
                      f"[{r['status']}] IBKR {code}: {msg}")


# ========================================================================================
# THE ONE ARM GATE — armed_session (spec §2.2, conductor #64 Step 1). BOTH the deploy engine
# (execute_plan, now) and rebalance_execute (Step 2) arm through THIS single code path.
# ========================================================================================
@contextmanager
def armed_session(*, purpose, client_id, gateway_lock_on_busy=None, fa_backup_ib=None):
    """The ONE arm gate: flip config.READONLY/DRY_RUN False IN-PROCESS behind the gate and
    RESTORE them in a finally — the enablement can NEVER outlive the block (spec §4 #4).
    Optionally, for the rebalance/FA path: acquire gateway_lock for the whole block
    (gateway_lock_on_busy not None), and expose an FA GROUPS replaceFA XML backup helper.
    Yields a small handle with .fa_backup_path (set only if a backup is taken via
    sess.backup_fa_groups(ib)). On GatewayBusyRefuse the lock context propagates it (the
    caller catches, as today) and the flags are never left flipped."""
    prev_ro, prev_dry = config.READONLY, config.DRY_RUN
    sess = SimpleNamespace(fa_backup_path="")
    stack = ExitStack()
    try:
        if gateway_lock_on_busy is not None:
            from gateway_lock import gateway_lock  # lazy: only the FA/rebalance path needs it
            stack.enter_context(gateway_lock(purpose=purpose, client_id=client_id,
                                             on_busy=gateway_lock_on_busy))
        config.READONLY = False
        config.DRY_RUN = False

        def _do_fa_backup(ib):
            # Take the FA GROUPS backup at the CALLER'S chosen point (byte-identical timing to
            # today's step [7]); records the path on the handle. Import lazily to avoid a hard
            # dep for the deploy path. The actual backup fn lives in rebalance_execute today —
            # in Step 2 the caller passes a bound backup fn; for Step 1 (deploy) it's unused.
            raise NotImplementedError("FA backup is wired by the rebalance caller in Step 2")
        sess.backup_fa_groups = _do_fa_backup

        yield sess
    finally:
        config.READONLY = prev_ro
        config.DRY_RUN = prev_dry
        stack.close()   # releases gateway_lock via its own __exit__ (normal + exception)


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
    # PURPOSE — DEPLOY (default) requires conform to transmit; REBALANCE does not. Validate
    # like `mode` above. `conform_required` is True ONLY for DEPLOY, so `purpose_ok` collapses
    # to exactly `conform` on the DEPLOY path (byte-identical to the pre-change gate) and is
    # unconditionally True on the REBALANCE path (which trims to target without conform).
    purpose = getattr(req, "purpose", PURPOSE_DEPLOY)
    if purpose not in (PURPOSE_DEPLOY, PURPOSE_REBALANCE):
        raise ValueError(f"purpose must be {PURPOSE_DEPLOY!r} or {PURPOSE_REBALANCE!r}, "
                         f"got {purpose!r}")
    conform_required = (purpose == PURPOSE_DEPLOY)
    purpose_ok = (conform if conform_required else True)
    # armed_conn: connected on the armed (transmit-capable) lane. By construction the caller
    # picks the ARMED lane iff permit_intent (armed AND conform AND not kill) held, so mode
    # ARMED <=> armed_conn (identical to s0_live_deploy's main()).
    armed_conn = (mode == MODE_ARMED)
    account = req.account
    plan = req.plan
    target = req.target
    quotes = req.quotes
    prices = req.prices
    # The BROKER'S OWN contracts for this account's held symbols, measured by the caller and
    # carried on the request beside quotes/prices. getattr-with-default so a request built by
    # an older caller (crm_execute, the pilot rails) is unchanged: an empty map means every
    # leg takes the historic Stock(symbol, "SMART", "USD") path, qualified before use.
    contracts = getattr(req, "contracts", None) or {}
    net_liq = req.net_liq
    summary = req.summary
    caps = req.caps
    model_multiple = caps.per_order_model_multiple
    # The model's weights apply to the MANAGED sleeve (net_liq minus any held-aside block),
    # so that — not the whole account — is the denominator the per-order rail sizes against.
    # AccountPlan normalizes managed_net_liq to net_liq when nothing is held aside.
    managed_net_liq = float(getattr(plan, "managed_net_liq", None) or net_liq)

    # [7] Build the full ordered DEPLOY order list (sells first, then buys; conform adds the
    # ALIEN liquidations). Whole-share, price-guarded caps.
    legs, aliens_left, unpriceable = build_deploy_legs(plan, quotes, prices, conform=conform,
                                                       contracts=contracts)
    # TWO TOTALS, TWO PRICE BASES — they are not interchangeable. See the comment on the
    # total_buy_le_investable check below for which one each gate must use.
    #   total_buy      — WORST CASE: each leg at its marketable CAP (ask * (1 + ORDER_CAP_K)),
    #                    the price the order is willing to cross at but will not normally pay.
    #   total_buy_plan — PLAN BASIS: each leg at the same REFERENCE price the engine sized it
    #                    on (`prices`), i.e. what the plan actually intends to spend.
    total_buy = sum(l.notional for l in legs if l.side == "BUY")
    total_buy_plan = sum(_leg_plan_notional(l, prices) for l in legs if l.side == "BUY")
    total_sell = sum(l.notional for l in legs if l.side == "SELL")

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
        # A MUTUAL FUND leg carries NO limit price — it is a market order that fills at
        # tonight's NAV — so saying "LIMIT" for it would be a lie on the operator's screen.
        # A non-fund leg's line is byte-identical to what it has always printed.
        px_text = (f"MARKET at tonight's NAV (last NAV ~{l.limit:,.2f})"
                   if is_fund_leg(l, contracts) else f"LIMIT ~{l.limit:>10,.2f}")
        print(f"    {l.side:4s} {l.symbol:6s} x{_qty_text(l.qty)} {px_text}  "
              f"notional ~{l.notional:>12,.2f}  [{l.source}]  {note}")
    # BOTH buy totals are shown so an operator reading a preview sees exactly what the
    # investable gate compares (buys at plan prices) and what it does NOT (the worst case).
    print(f"    TOTALS   sells ~{total_sell:,.2f}   "
          f"buys at plan prices ~{total_buy_plan:,.2f}   "
          f"buys worst case at the marketable caps ~{total_buy:,.2f}   "
          f"investable ~{plan.investable:,.2f}   NetLiq ~{net_liq:,.2f}")
    print("    NOTE: buys will be RE-SIZED to REALIZED cash after the sells fill (two-phase); "
          "the buy figures above are the pre-cash-gate plan.")
    # THE SETTLEMENT RULE, SAID OUT LOUD BEFORE ANYONE ARMS ANYTHING.
    fund_sell_legs = [l for l in legs if l.side == "SELL" and is_fund_leg(l, contracts)]
    expected_fund_proceeds = sum(l.notional for l in fund_sell_legs)
    if fund_sell_legs:
        print(f"    MUTUAL FUNDS ON THIS RUN: {len(fund_sell_legs)} fund sale(s) worth about "
              f"{expected_fund_proceeds:,.2f} at the last NAV. THAT MONEY IS EXCLUDED FROM "
              f"THIS RUN'S BUYING POWER. A mutual fund prices once a day, at NAV, after the "
              f"close, and its proceeds do NOT settle the same day — so nothing bought today "
              f"can be paid for with it. Today's buys are sized ONLY to cash that has "
              f"actually landed (a fresh reading of the account's real cash balance, which "
              f"cannot contain money a fund has not paid out yet). Expect roughly "
              f"{expected_fund_proceeds:,.2f} to arrive over the next day or two; a LATER RUN "
              f"will deploy it, because every run re-reads the account's real cash. This "
              f"account will therefore be only PARTLY conformed to its model today, on "
              f"purpose — the alternative is buying with money that has not arrived.")
        for l in fund_sell_legs:
            print(f"      SELL {l.symbol:6s} {_qty_text(l.qty)} share(s) — the FULL position "
                  f"including the fraction — about {l.notional:,.2f} at the last NAV")
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
    if conform_required and not conform:
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
    # Per-order fat-finger rail, measured against the PLAN (BUY: <= model_multiple x the
    # model's target dollars for the symbol; SELL: <= the shares actually held). Replaces the
    # flat 50%-of-NetLiq cap, which blocked every never-invested account's first deploy by
    # construction — owner decision D3 (2026-08-19), evidence U5721712 2026-08-25.
    reasons.extend(per_order_rail_reasons(legs, plan, target,
                                          managed_net_liq=managed_net_liq,
                                          model_multiple=model_multiple))
    # Total-notional sanity cap: total BUY must not exceed investable. (The two-phase cash gate
    # re-sizes buys to realized cash at transmit time; this is the plan-level cap.)
    #
    # THE TWO BASES — DO NOT "FIX" THIS BACK TO `total_buy` (v0.44.0, 2026-09-01).
    # `plan.investable` is computed on the REFERENCE-price basis: rebalance_engine sizes every
    # position as target_shares = int(weight * investable / reference_price), and whole-share
    # rounding is DOWN, so the plan's intended spend is STRICTLY BELOW investable by
    # construction. `total_buy` is a DIFFERENT basis: qty * the marketable CAP
    # (ask * (1 + ORDER_CAP_K), ORDER_CAP_K = 0.003) — the deliberately-above-market price the
    # order may cross at so it actually fills. Comparing the cap basis to a reference-basis
    # investable compares two different numbers; the gap is (ask - reference)/reference plus
    # 30bps, which on a larger account exceeds the whole-share rounding slack and refuses the
    # ENTIRE account. MEASURED 2026-09-01 on U25274773: investable 818,504.60, buys at the caps
    # 822,722.51 (blocked) vs buys at plan prices 818,504.60-minus-rounding (fine) — a ~51bps
    # after-hours spread. Three of eight custom-model accounts, $1,194,383 of $1,471,610 (81%
    # of the deployment, including the largest account), were refused for an overspend that
    # cannot happen: the account never pays the cap, it pays the market.
    # The protection that ACTUALLY prevents over-deployment is elsewhere and untouched — the
    # two-phase transmit re-reads TotalCashValue from the BROKER after the sells and floors the
    # buys to it via _scale_buys_to_cash (1% safety buffer, hard assert).
    # So: this arithmetic-consistency gate uses the PLAN basis, and only it. The worst case is
    # reported alongside so nothing is hidden — and it is still the figure the BUYING-POWER and
    # margin gates below use, because "will the broker permit this order" genuinely is a
    # worst-case question.
    if caps.total_buy_le_investable and total_buy_plan > plan.investable:
        reasons.append(f"total BUY {total_buy_plan:,.2f} at plan prices > investable "
                       f"{plan.investable:,.2f} — would over-deploy / use margin "
                       f"(worst case at the marketable caps: {total_buy:,.2f})")
    # Optional absolute total-notional ceiling (None by default — investable already bounds it).
    if caps.max_total_notional is not None and (total_buy + total_sell) > caps.max_total_notional:
        reasons.append(f"total notional {total_buy + total_sell:,.2f} > max_total_notional "
                       f"{caps.max_total_notional:,.2f}")

    # Connection-dependent gates — only meaningful on the armed (4003 transmit) connection,
    # and only worth probing once the code-level gates above are clean.
    if armed and purpose_ok and armed_conn and not reasons:
        if _probe_gateway_readonly(ib):
            reasons.append("Gateway is still READ-ONLY on 4003 (arming.probe idiom) — not "
                           "physically armed; a human must turn the Read-Only API toggle off")
    if armed and purpose_ok and armed_conn and not reasons:
        bp_ok, bp_reason = _buying_power_ok(summary, total_buy)
        if not bp_ok:
            reasons.append(bp_reason)
    # Self-computed per-account MARGIN pre-flight (#57): refuse a genuinely levered request on
    # an account that cannot carry it. Same guard shape as the buying-power check above (only
    # probed once the code-level gates are clean, on the armed transmit lane). For an unlevered
    # S0 plan (exposure <= 1.0) this adds ZERO reasons on ANY account type — see
    # _margin_preflight_ok's HARD invariant.
    if armed and purpose_ok and armed_conn and not reasons:
        mg_ok, mg_reason = _margin_preflight_ok(summary, net_liq, total_buy, plan, target)
        if not mg_ok:
            reasons.append(mg_reason)
    # Per-account PATTERN-DAY-TRADER (PDT) pre-flight. Until now this check existed ONLY on the
    # FA block rail (live_fa_block_execute), while the rail the Control Plane actually shells
    # out to — batch_rebalance_execute -> THIS function, once per account — had none. An account
    # IBKR has already flagged PDT rejects ORDINARY orders, not just day trades (2026-07-28,
    # U5721712: a plain BUY of 1 USFR bounced), so the check must sit on every transmit path.
    #
    # Same guard shape as the buying-power and margin checks above: armed transmit lane only,
    # and only once the code-level gates are already clean. ZERO new broker reads — `summary`
    # is this ONE account's already-filtered accountSummary rows (batch_rebalance_execute puts
    # ib.accountSummary() through s0_live.filter_account_summary per account, and
    # crm_execute.build_batch_requests hands each request its own), and ib_async requests
    # DayTradesRemaining by default.
    #
    # The absent-tag rule is the load-bearing design decision here — several real, tradeable
    # accounts return NO DayTradesRemaining tag at all (measured on 4003, 2026-09-01), so a
    # naive fail-closed would refuse them. pdt_guard's module header states the rule and its
    # trade-off in full; read it before changing this.
    if armed and purpose_ok and armed_conn and not reasons:
        pdt = pdt_guard.pdt_verdict(account, summary)
        # NEVER SILENT — the verdict is printed on a clearance as well as on a refusal, so an
        # armed run always shows which basis each account was let through on.
        print(f"    PDT pre-flight [{pdt.code}]: {pdt.reason}")
        if not pdt.ok:
            reasons.append(pdt.reason)

    permit = (armed and purpose_ok and armed_conn and not kill and not reasons)

    result = ExecutionResult(status=STATUS_PREVIEW_ONLY, legs=legs, reasons=reasons,
                             aliens_left=aliens_left, unpriceable=unpriceable, rc=0)

    # [9] Report + (only if permitted) transmit the two-phase cash-gated deploy.
    if not permit:
        primary = ("not armed" if not armed
                   else "conform off" if (conform_required and not conform)
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
    # (the committed desk-wide defaults). armed_session flips both to False in memory ONLY, runs
    # BOTH phases, then RESTORES in a finally so the flip can never leak past the transmit.
    # UNREACHABLE unless `permit` is True (above the `if not permit: return` guard). Deploy passes
    # gateway_lock_on_busy=None: its branch decision (permit) is computed from explicit inputs
    # ABOVE, and it is serialized by the physical 4003 gateway arming, not the 4002 mutex.
    # POSITIONS BEFORE - the baseline for the post-run reconcile. Read OUTSIDE the armed
    # session so a position-read problem can never delay or disturb a transmit.
    positions_before = _positions_for(ib, account)

    with armed_session(purpose="safe_execute_deploy", client_id=None,
                       gateway_lock_on_busy=None):
        # PHASE 1 — SELLS (raise cash), wait for terminal, re-price stragglers.
        print(f"\n    PHASE 1 — SELLS ({len(sell_legs)} leg(s)): transmit, then WAIT for "
              f"terminal state (fill/cancel) before sizing any buy.")
        sell_results = _transmit_phase(ib, sell_legs, account=account, as_of=target.as_of,
                                       run_id=run_id, phase_label="SELL", quotes=quotes,
                                       prices=prices, contracts=contracts)

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

        # THE SETTLEMENT GATE — MUTUAL-FUND PROCEEDS CAN NEVER FUND A SAME-RUN BUY.
        # Fund proceeds do not settle the same day. Buying against them is the exact failure
        # shape of the 2026-07-28 negative-balance incident: money the account has not got yet
        # counted as money it can spend.
        # TWO INDEPENDENT WALLS, and neither is an assumption:
        #   1. TIMING. A fund order placed during the day cannot fill until the fund strikes
        #      its NAV after the close, so the fresh TotalCashValue read above provably cannot
        #      contain its proceeds. _transmit_phase leaves fund legs WORKING and reports
        #      filled=0 (it never waits on them), which is the direct evidence of that.
        #   2. MEASUREMENT. Whatever a fund leg DID report as filled — normally nothing, but a
        #      run after the NAV strike could see a real fill — is subtracted here, so its
        #      proceeds are removed from the budget even if the broker's cash figure has
        #      already picked them up. Exactly what filled, never an estimate: an unfilled
        #      fund order subtracts 0 and the account deploys its ETF proceeds in full, which
        #      is the intended shape (this run sells the funds and the ETFs and deploys the
        #      ETF cash; a LATER run, after settlement, deploys the fund cash).
        unsettled_fund_cash = _unsettled_fund_proceeds(sell_results, sell_legs, contracts)
        if unsettled_fund_cash > 0:
            available_cash = max(0.0, available_cash - unsettled_fund_cash)
            print(f"    MUTUAL-FUND PROCEEDS EXCLUDED: {unsettled_fund_cash:,.2f} of fund "
                  f"sale proceeds is UNSETTLED and cannot be spent today, so it is removed "
                  f"from this run's buying power. Cash available to buy with: "
                  f"{available_cash:,.2f}.")
        working_fund_legs = [l for l in sell_legs if is_fund_leg(l, contracts)]
        if working_fund_legs and unsettled_fund_cash <= 0:
            print(f"    MUTUAL-FUND PROCEEDS EXCLUDED: {len(working_fund_legs)} fund sale(s) "
                  f"are still WORKING and have paid out nothing, so the cash figure above "
                  f"contains none of their proceeds and no buy today is sized against them.")

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
                                      prices=prices, contracts=contracts)

    # POSITIONS AFTER, and the reconcile. This runs BEFORE the phase reports so the operator
    # reads the truth first and the claims second.
    positions_after = _positions_for(ib, account)
    reconcile = _reconcile_by_position(positions_before, positions_after,
                                       sell_results, buy_results)
    _report_reconcile(reconcile)

    # Consolidated result — LOUD on anything unfilled/skipped in either phase.
    _report_phase("SELL", sell_results)
    _report_phase("BUY", buy_results)
    print("\nDone. Two-phase cash-gated deploy complete — review the fills above and DISARM the "
          "Gateway when finished.")

    result.sell_results = sell_results
    result.buy_results = buy_results
    result.realized_cash = available_cash
    result.reconcile_residual = reconcile
    # Terminal status: PARTIAL_LOUD if any leg was unfilled/skipped, else COMPLETE.
    # THE FILL FIGURE COMES FROM THE POSITIONS wherever the reconcile could establish one; the
    # order status is the fallback, not the authority. A leg this client called cancelled that
    # actually moved the position counts as filled, and a leg it called filled that did NOT
    # move the position counts as short. Both were wrong the other way round before this.
    any_short = any(r.get("skipped") or _effective_filled(r) < r["requested"]
                    for r in (sell_results + buy_results))
    if reconcile.get("disagreements"):
        # A disagreement is never quietly COMPLETE: the operator has to look at it.
        result.status = STATUS_PARTIAL_LOUD
    else:
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
