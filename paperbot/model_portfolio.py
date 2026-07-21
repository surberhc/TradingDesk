"""
model_portfolio.py — IBKR Model Portfolios "sleeve" system (ADDITIVE, READ-ONLY / COMPUTE-ONLY).

The foundational, purely-additive module for allocating ONE client account across several
strategy SLEEVES using IBKR's Model Portfolios feature — e.g. a client whose account runs
75% S0 (Adaptive All-Weather) and 25% S8 (SPX 0DTE), both under our FA master DF8922141.
Each sleeve is an IBKR "model" (a modelCode); orders tagged with a modelCode are booked
against that model's slice of the account, and IBKR tracks each model's positions
independently even when two models hold the SAME instrument (the "fungibility" case).

Built to go LIVE eventually, but PAPER-FIRST and behind the same review->arm->transmit gate
as the rest of the desk (CLAUDE.md). This module DELIBERATELY does none of the following:
it forms NO connection, transmits NO order, and has NO import-time side effects. It only:
  (a) declares the model registry + per-account allocation policy (config data),
  (b) sizes each sleeve's dollar capital base (pure),
  (c) BUILDS model-tagged order objects with transmit=False (like order_router, never edits it),
  (d) READ-wraps the per-model position/account IBKR calls, and
  (e) computes per-model share deltas to hit targets (pure — the rebalancer brain).

--- WHAT THE CLIENT LIBRARY ACTUALLY SUPPORTS (ib_async 2.1.0, verified, not assumed) ---
We use `ib_async` (the maintained ib_insync fork), NOT native `ibapi`. Findings that shaped
this module:
  * `Order.modelCode` EXISTS  -> model-aware order ROUTING is clean (set order.modelCode).
  * `ib.reqAccountUpdatesMulti(account, modelCode)` EXISTS and `AccountValue.modelCode`
    EXISTS -> per-model ACCOUNT VALUE reads (NetLiq per sleeve) are clean.
  * `reqPositionsMulti` is NOT on the high-level `IB` facade. The low-level
    `ib.client.reqPositionsMulti(reqId, account, modelCode)` sends the correct TWS wire
    message, BUT `ib_async.wrapper.Wrapper.positionMulti` / `positionMultiEnd` are no-op
    STUBS (`pass`) — ib_async drops the responses and exposes no accessor/event/future.
    So per-model POSITION reads are NOT cleanly supported out of the box. `read_model_positions`
    below implements a documented, self-contained WORKAROUND that temporarily installs its
    own collector on those two wrapper callbacks, pumps the event loop, and restores the
    originals. It is flagged as depending on an ib_async internal; the proper fix is to
    upstream (or vendor) real positionMulti handling. See its docstring + MODEL_PORTFOLIO_SPEC.md.

See docs/MODEL_PORTFOLIO_SPEC.md, conductor/ACCOUNT_ALLOCATION.md, conductor/DECISIONS.md.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field

from ib_async import LimitOrder, Stock

import config
import order_router   # reused (never edited): _check_limit_price PRICE GUARD + BuiltOrder


# =============================================================================
# (a) MODEL REGISTRY + PER-ACCOUNT ALLOCATION POLICY  (config data)
# =============================================================================
# The paper FA master (advisor/connection account). Every model order routes UNDER this
# master (it is the API connection point; see conductor/ACCOUNT_ALLOCATION.md). Its suffix
# is config.ACCOUNT_SUFFIX ("141"); the full number is fixed here so the build helpers can
# stamp/guard it. The master itself is not a client sleeve-holder — it is the umbrella.
FA_MASTER_ACCOUNT = "DF8922141"


def is_fa_master(account: str) -> bool:
    """True if `account` is the FA master (starts 'DF' and ends in our configured suffix)."""
    return bool(account) and account.startswith("DF") and account.endswith(config.ACCOUNT_SUFFIX)


# modelCode names. These are the identifiers created in the IBKR UI (model creation is
# UI-only — see the spec) and stamped onto every order/read. Keep them stable: renaming a
# model in the UI orphans every tagged order/position keyed on the old string.
MODEL_S0 = "S0_ALLWEATHER"
MODEL_S8 = "S8_ZERODTE"


@dataclass(frozen=True)
class ModelSleeve:
    """One strategy sleeve <-> IBKR model. Immutable registry entry."""
    model_code: str      # the IBKR modelCode string (stamped on orders + reads)
    strategy: str        # the internal strategy key (matches config.STRATEGY_NAME family)
    description: str


# strategy sleeve registry: modelCode -> ModelSleeve. Config-driven and easy to extend —
# add a row here (and create the matching model in the IBKR UI) to introduce a new sleeve.
# The OPEN design fork (spec): model granularity could later become per-strategy x risk-tier
# (S0 tiers internally as Conservative/Balanced/Growth), which would add e.g.
# "S0_ALLWEATHER_BALANCED" rows here. Left per-strategy for now, pending Andrew's decision.
MODEL_REGISTRY: dict[str, ModelSleeve] = {
    MODEL_S0: ModelSleeve(MODEL_S0, "adaptive_all_weather",
                          "S0 Adaptive All-Weather multi-asset ETF sleeve"),
    MODEL_S8: ModelSleeve(MODEL_S8, "s8_british_ic",
                          "S8 SPX 0DTE scheduled credit-spread sleeve"),
}


def known_model(model_code: str) -> bool:
    """True iff model_code is a registered sleeve. Typo guard for routing/policies."""
    return model_code in MODEL_REGISTRY


def require_known_model(model_code: str) -> str:
    """Return model_code if registered, else raise — no order/policy may name an unknown
    model (a typo would silently route to, or read, the wrong sleeve)."""
    if not known_model(model_code):
        raise ValueError(
            f"unknown modelCode {model_code!r} — not in MODEL_REGISTRY "
            f"({', '.join(sorted(MODEL_REGISTRY))}). Register the sleeve first.")
    return model_code


# The weights of a policy must sum to 1.0 within this absolute tolerance (float slop).
POLICY_WEIGHT_TOL = 1e-6


@dataclass(frozen=True)
class AllocationPolicy:
    """A client account's split across sleeves: account -> {modelCode: weight}. Weights are
    fractions of the account's net-liq that each sleeve targets; they must sum to ~1.0.
    Frozen so a validated policy can't be mutated out from under a sizing/rebalance run."""
    account: str
    weights: dict[str, float]
    label: str = ""

    def validate(self) -> "AllocationPolicy":
        """Validate and return self (so `policy.validate()` reads fluently). See
        validate_policy for the exact rules."""
        validate_policy(self)
        return self


def validate_policy(policy: AllocationPolicy, *, tol: float = POLICY_WEIGHT_TOL) -> None:
    """Raise ValueError unless `policy` is well-formed:
      * a non-empty account and non-empty weights map,
      * every modelCode is a REGISTERED sleeve (typo guard),
      * every weight is a finite number in [0.0, 1.0] (no NaN/inf/negative/>1),
      * the weights sum to 1.0 within `tol`.
    Pure — no broker, no I/O. Mirrors order_router's fail-with-a-clear-reason style."""
    if not policy.account:
        raise ValueError("allocation policy must name an account")
    if not policy.weights:
        raise ValueError(f"allocation policy for {policy.account} has no sleeve weights")
    total = 0.0
    for model_code, weight in policy.weights.items():
        require_known_model(model_code)
        try:
            w = float(weight)
        except (TypeError, ValueError):
            raise ValueError(
                f"{policy.account}/{model_code}: weight {weight!r} is not a number")
        if not math.isfinite(w):
            raise ValueError(f"{policy.account}/{model_code}: weight {w} is not finite")
        if w < 0.0 or w > 1.0:
            raise ValueError(
                f"{policy.account}/{model_code}: weight {w} out of range [0, 1]")
        total += w
    if abs(total - 1.0) > tol:
        raise ValueError(
            f"{policy.account}: sleeve weights sum to {total:.6f}, not 1.0 "
            f"(tol {tol}). Weights: {policy.weights}")


def validate_account_policies(policies: dict[str, AllocationPolicy]) -> None:
    """Validate a whole account->policy map; also enforces the map key matches each
    policy's own .account (a copy-paste guard)."""
    for account, policy in policies.items():
        if account != policy.account:
            raise ValueError(
                f"policy map key {account!r} != policy.account {policy.account!r}")
        validate_policy(policy)


# --- Example policies (illustrative; NOT wired into any runner) ----------------------
# The headline 75% S0 / 25% S8 split the spec is built around, on an illustrative paper
# client sub-account. NOT hardcoded as the only shape — build any per-client policy by
# constructing another AllocationPolicy. All such accounts live UNDER FA_MASTER_ACCOUNT.
EXAMPLE_POLICY_75_25 = AllocationPolicy(
    account="DU8922142",
    weights={MODEL_S0: 0.75, MODEL_S8: 0.25},
    label="75% S0 / 25% S8",
)

# A second shape to prove arbitrary per-client policies (single-sleeve is valid: 100% S0).
EXAMPLE_POLICY_S0_ONLY = AllocationPolicy(
    account="DU8922143",
    weights={MODEL_S0: 1.0},
    label="100% S0",
)

# The account->policy structure. The FA master is included as a known account but holds NO
# client sleeve of its own (like REBALANCE_MASTER=False in config.py — the master is the
# advisor/umbrella, not a traded client). It maps to an empty-but-explicit marker rather
# than a policy so callers can enumerate "every account under the master" and still know
# the master is not itself allocated.
EXAMPLE_ACCOUNT_POLICIES: dict[str, AllocationPolicy] = {
    EXAMPLE_POLICY_75_25.account: EXAMPLE_POLICY_75_25,
    EXAMPLE_POLICY_S0_ONLY.account: EXAMPLE_POLICY_S0_ONLY,
}


# =============================================================================
# (b) PURE SLEEVE SIZING — dollar capital base per sleeve
# =============================================================================
def sleeve_capital(net_liq: float, policy: AllocationPolicy) -> dict[str, float]:
    """Each sleeve's dollar capital base = account net-liq * that sleeve's weight.

    PURE and unit-testable — no broker. Validates the policy first (a bad policy can never
    silently produce a mis-sized sleeve). A negative net_liq is rejected (a real account
    can be near-zero but not negative net-liq in this context)."""
    validate_policy(policy)
    nlv = float(net_liq)
    if not math.isfinite(nlv) or nlv < 0.0:
        raise ValueError(f"net_liq must be a finite, non-negative number, got {net_liq!r}")
    return {model_code: nlv * float(weight)
            for model_code, weight in policy.weights.items()}


# =============================================================================
# (c) MODEL-AWARE ORDER ROUTING HELPERS — build model-tagged orders (transmit=False)
#     Mirrors order_router._base_fields / build_fa_block WITHOUT editing order_router.
# =============================================================================
def model_order_ref(account: str, model_code: str, as_of, side: str, symbol: str) -> str:
    """Deterministic orderRef for a model-tagged leg. Extends order_router's scheme with the
    modelCode so two sleeves holding the SAME symbol in the SAME account (the fungibility
    case, e.g. S0 and S8 both long SPY) get DISTINCT refs and never collide in the dedup
    gate:  paperbot:<account>:<model>:<as_of>:<side>:<symbol>."""
    return f"paperbot:{account}:{model_code}:{as_of}:{side}:{symbol}"


def apply_model_fields(order, *, account: str, model_code: str, order_ref: str | None):
    """Set the model-routing fields on a built order. Mirrors order_router._base_fields:
    transmit stays False here (placement flips it only behind the gate); account carries the
    CLIENT account (the sleeve-holder), and modelCode carries the sleeve. No faGroup/faMethod
    — a model order is NOT an FA-block group order; the model tag is what allocates it."""
    order.transmit = False
    if order_ref:
        order.orderRef = order_ref
    if account:
        order.account = account
    if model_code:
        order.modelCode = model_code
    return order


def build_model_limit_order(symbol: str, side: str, quantity: int, limit_price: float, *,
                            account: str, model_code: str, as_of,
                            order_ref: str | None = None, ib=None):
    """Construct ONE model-tagged LIMIT order (contract + Order), transmit=False, for a
    single client account's sleeve. The order carries account=<client account> AND
    modelCode=<sleeve> so IBKR books it against that model's slice.

    HARD PRICE GUARD reused from order_router: a NaN/None/<=0 limit is rejected BEFORE any
    order object is built. Returns an order_router.BuiltOrder (same shape the transmit path
    and dedup gate already consume). This module NEVER arms — transmit stays False."""
    require_known_model(model_code)
    order_router._check_limit_price(symbol, limit_price)   # PRICE GUARD (shared, not copied)
    contract = Stock(symbol, "SMART", "USD")
    order = LimitOrder(side, quantity, limit_price)
    order.tif = "DAY"
    ref = order_ref or model_order_ref(account, model_code, as_of, side, symbol)
    apply_model_fields(order, account=account, model_code=model_code, order_ref=ref)
    if ib is not None:
        try:
            ib.qualifyContracts(contract)   # read-only validation; never fail the build on it
        except Exception:
            pass
    return order_router.BuiltOrder(symbol, contract, order, ref)


# =============================================================================
# (d) READ WRAPPERS FOR PER-MODEL STATE  (READ-ONLY)
#     Per-model ACCOUNT VALUES: cleanly supported. Per-model POSITIONS: gap workaround.
# =============================================================================
def read_model_account_values(ib, account: str = "", model_code: str = "", *,
                              timeout: float = 6.0) -> list:
    """Per-model account VALUES (NetLiq etc. per sleeve). Subscribes via
    reqAccountUpdatesMulti(account, modelCode), then returns the AccountValue rows filtered to
    (account, model_code). AccountValue carries a real `modelCode` field, so this genuinely
    separates e.g. S0 vs S8 exposure. READ-ONLY.

    TIMEOUT-BOUNDED (verified against the paper gateway 2026-07-20): the bare *sync*
    `ib.reqAccountUpdatesMulti` blocks until IBKR sends the accountUpdateMultiEnd marker — but
    the gateway NEVER sends that End for a broad request (account="") or for a modelCode that
    exists in the UI yet has NO account allocated into it, so the sync form hangs FOREVER.
    We instead drive the async form under asyncio.wait_for; on TimeoutError we simply keep
    whatever values streamed in before the deadline (a partial/empty read, never a hang)."""
    try:
        ib.run(asyncio.wait_for(
            ib.reqAccountUpdatesMultiAsync(account, model_code), timeout))
    except asyncio.TimeoutError:
        pass   # no End marker (empty account or an unallocated model) -> use what we have
    except Exception as exc:
        print(f"    ! reqAccountUpdatesMulti failed ({type(exc).__name__}: {exc})")
    rows = ib.accountValues(account) if account else ib.accountValues()
    out = []
    for v in (rows or []):
        if account and getattr(v, "account", "") != account:
            continue
        if model_code and getattr(v, "modelCode", "") != model_code:
            continue
        out.append(v)
    return out


def net_liq_for_model(ib, account: str, model_code: str) -> float | None:
    """The NetLiquidation for ONE sleeve (account x model), or None if not reported.
    Feeds sleeve_capital / the rebalancer with per-model NLV. READ-ONLY."""
    for v in read_model_account_values(ib, account, model_code):
        if getattr(v, "tag", "") == "NetLiquidation":
            try:
                return float(v.value)
            except (TypeError, ValueError):
                return None
    return None


@dataclass
class ModelPosition:
    """One (account, model) position row — the per-model breakdown ib.positions() lacks."""
    account: str
    model_code: str
    contract: object       # ib_async Contract
    position: float
    avg_cost: float


def parse_model_positions(rows) -> list[ModelPosition]:
    """PURE parser: turn raw positionMulti tuples (account, model_code, contract, position,
    avg_cost) into ModelPosition records. Unit-testable WITHOUT a broker — the read wrapper
    below hands its collected callback tuples through here, and tests exercise it directly."""
    out: list[ModelPosition] = []
    for account, model_code, contract, position, avg_cost in rows:
        out.append(ModelPosition(
            account=account or "",
            model_code=model_code or "",
            contract=contract,
            position=float(position),
            avg_cost=float(avg_cost),
        ))
    return out


def read_model_positions(ib, account: str = "", model_code: str = "", *,
                         timeout: float = 5.0) -> list[ModelPosition]:
    """Per-model POSITIONS — the S0-vs-S8-exposure read the high-level ib_async API does NOT
    provide (see the module header). WORKAROUND, clearly flagged: ib_async wires the request
    (`ib.client.reqPositionsMulti`) and defines the `positionMulti`/`positionMultiEnd`
    wrapper callbacks, but leaves both as no-op stubs, so the responses are otherwise dropped.
    Here we TEMPORARILY install our own collector on those two callbacks (instance-attribute
    shadowing — verified safe on ib_async 2.1.0), fire the request, pump ib.sleep() until the
    End marker or `timeout`, cancel the subscription, and RESTORE the originals in a finally.

    READ-ONLY: reqPositionsMulti is a data request (like reqPositions), never an order. Fails
    soft — any error returns whatever was collected. TODO: replace with a real ib_async
    wrapper method (upstream PR or a thin vendored subclass) so we don't depend on an internal.
    Not exercised in unit tests (needs a live gateway); parse_model_positions IS tested."""
    collected: list[tuple] = []
    done = {"end": False}

    def _on_position_multi(reqId, acct, mc, contract, pos, avgCost):
        collected.append((acct, mc, contract, pos, avgCost))

    def _on_position_multi_end(reqId):
        done["end"] = True

    wrapper = ib.wrapper
    had_pos = "positionMulti" in wrapper.__dict__
    orig_pos = wrapper.__dict__.get("positionMulti")
    had_end = "positionMultiEnd" in wrapper.__dict__
    orig_end = wrapper.__dict__.get("positionMultiEnd")
    req_id = ib.client.getReqId()
    try:
        wrapper.positionMulti = _on_position_multi
        wrapper.positionMultiEnd = _on_position_multi_end
        ib.client.reqPositionsMulti(req_id, account, model_code)
        waited = 0.0
        step = 0.1
        while waited < timeout and not done["end"]:
            ib.sleep(step)
            waited += step
        try:
            ib.client.cancelPositionsMulti(req_id)
        except Exception:
            pass
    except Exception as exc:
        print(f"    ! read_model_positions failed ({type(exc).__name__}: {exc}) "
              f"-> returning {len(collected)} row(s) collected so far.")
    finally:
        # Restore the wrapper's original callbacks EXACTLY (remove our shadow if there was
        # none before, else put the prior instance attribute back). Never leave our collector
        # installed — it would corrupt a later real positionMulti read on the same session.
        if had_pos:
            wrapper.positionMulti = orig_pos
        else:
            wrapper.__dict__.pop("positionMulti", None)
        if had_end:
            wrapper.positionMultiEnd = orig_end
        else:
            wrapper.__dict__.pop("positionMultiEnd", None)
    return parse_model_positions(collected)


# =============================================================================
# (e) PURE DRIFT / REBALANCE-TARGET — per-model share deltas (the rebalancer brain)
#     Reuses the SPIRIT of rebalance_engine.py, but keyed on modelCode. Pure/testable.
# =============================================================================
def _investable_for_sleeve(capital: float, cash_reserve_pct: float | None) -> float:
    """A sleeve's deployable dollars after the cash reserve — mirrors the reserve carve-out
    in rebalance_engine/investable (NAV*(1-reserve)), applied per SLEEVE capital base."""
    cr = config.RISK_LIMITS["cash_reserve_pct"] if cash_reserve_pct is None else cash_reserve_pct
    inv = float(capital) * (1.0 - float(cr))
    return inv if inv > 0.0 else 0.0


def model_share_targets(capital_by_model: dict[str, float],
                        weights_by_model: dict[str, dict[str, float]],
                        prices: dict[str, float], *,
                        cash_reserve_pct: float | None = None) -> dict[str, dict[str, int]]:
    """Per-model integer target SHARE counts. For each model: investable = capital*(1-reserve);
    per symbol target_shares = floor(weight * investable / price). Mirrors reconcile's
    int(weight*investable/price) share math, keyed by modelCode. A missing/non-positive price
    yields 0 target shares for that symbol (never a NaN/negative order). PURE."""
    out: dict[str, dict[str, int]] = {}
    for model_code, capital in capital_by_model.items():
        investable = _investable_for_sleeve(capital, cash_reserve_pct)
        weights = weights_by_model.get(model_code, {})
        model_targets: dict[str, int] = {}
        for symbol, weight in weights.items():
            price = float(prices.get(symbol, float("nan")))
            if not (price == price and price > 0.0):    # NaN/<=0 -> untradeable
                model_targets[symbol] = 0
                continue
            model_targets[symbol] = int(float(weight) * investable / price)
        out[model_code] = model_targets
    return out


def model_share_deltas(capital_by_model: dict[str, float],
                       weights_by_model: dict[str, dict[str, float]],
                       current_by_model: dict[str, dict[str, float]],
                       prices: dict[str, float], *,
                       cash_reserve_pct: float | None = None,
                       band_pct: float | None = None) -> dict[str, dict[str, int]]:
    """Per-model signed share DELTAS (target - current) needed to hit each sleeve's targets.
    Positive = BUY, negative = SELL. This is the rebalancer brain, keyed on modelCode.

    Handles the FUNGIBILITY / overlapping-instrument case natively: because everything is
    keyed by model, S0 and S8 both holding SPY in one account are computed INDEPENDENTLY —
    each model gets its own SPY target and its own SPY delta; they are never netted.

    A held symbol the sleeve's model dropped to 0 weight (present in current, absent/zero in
    targets) is fully SOLD (delta = -current). Only |delta| >= 1 share is emitted.

    Optional per-model no-trade BAND (all-or-nothing, mirroring rebalance_engine.band_breached
    but scoped to a sleeve): when band_pct is set, a model's deltas are SUPPRESSED entirely
    unless some symbol's required trade size (|delta|*price) exceeds band_pct of that model's
    capital base — then the whole sleeve rebalances (in-band siblings included). band_pct=None
    (default) emits every non-zero delta (no suppression). PURE."""
    targets = model_share_targets(capital_by_model, weights_by_model, prices,
                                  cash_reserve_pct=cash_reserve_pct)
    out: dict[str, dict[str, int]] = {}
    for model_code, capital in capital_by_model.items():
        model_targets = targets.get(model_code, {})
        current = current_by_model.get(model_code, {})
        symbols = set(model_targets) | set(current)

        raw: dict[str, int] = {}
        for symbol in symbols:
            delta = int(model_targets.get(symbol, 0)) - int(current.get(symbol, 0))
            if abs(delta) >= 1:
                raw[symbol] = delta

        if band_pct is not None:
            breached = False
            cap = float(capital)
            for symbol, delta in raw.items():
                price = float(prices.get(symbol, float("nan")))
                if not (price == price and price > 0.0) or cap <= 0.0:
                    continue
                if abs(delta) * price / cap > band_pct:
                    breached = True
                    break
            if not breached:
                raw = {}     # whole sleeve inside the band -> leave it exactly as-is

        out[model_code] = raw
    return out


@dataclass
class SleevePlan:
    """A reviewable, transmit-free per-account sleeve plan. Carries the sizing + deltas the
    order builder / dedup gate would consume — but builds no order and sends nothing."""
    account: str
    net_liq: float
    sleeve_capital: dict[str, float]              # modelCode -> dollar base
    deltas: dict[str, dict[str, int]]             # modelCode -> {symbol: signed share delta}


def plan_account_sleeves(account: str, net_liq: float, policy: AllocationPolicy,
                         weights_by_model: dict[str, dict[str, float]],
                         current_by_model: dict[str, dict[str, float]],
                         prices: dict[str, float], *,
                         cash_reserve_pct: float | None = None,
                         band_pct: float | None = None) -> SleevePlan:
    """End-to-end PURE plan for ONE account: policy -> per-sleeve capital -> per-model share
    deltas. Ties (b)+(e) together into a single reviewable what-if with NOTHING transmitted.
    The live caller attaches a limit price and hands each delta to build_model_limit_order,
    still behind the arm gate. `weights_by_model` are the intra-sleeve symbol weights the
    shared strategy brain produces for each model."""
    caps = sleeve_capital(net_liq, policy)
    deltas = model_share_deltas(caps, weights_by_model, current_by_model, prices,
                                cash_reserve_pct=cash_reserve_pct, band_pct=band_pct)
    return SleevePlan(account=account, net_liq=float(net_liq),
                      sleeve_capital=caps, deltas=deltas)
