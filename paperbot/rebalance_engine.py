"""
rebalance_engine.py — the PURE, offline multi-account block-rebalance brain.

This is the computation core of Option B (multi-account / FA). Given, per account, its
NetLiq, its current positions, its tier's target weights (from the shared `strategies`
brain, surfaced via strategy_target.Target), current prices, its distribution reserve
(from cashflows), and the no-trade band, it answers two questions with NO broker contact:

  1) Per account: the EXPLICIT integer target SHARE count per ticker, and the signed
     share DELTA vs current holdings — with an ACCOUNT-LEVEL no-trade band: the account
     is left untouched unless SOME holding breaches +/-band, in which case the whole
     account is rebalanced back to model (so ordinary market noise never trips a trade).

  2) Across a tier's accounts: same-symbol, same-direction deltas aggregated into a
     single BLOCK quantity, plus the per-account ContractsOrShares split that sums back
     to that block quantity. A block that touches only one account falls back to a
     per-account DIRECT order.

WHY explicit per-account shares (not an order-level faMethod): proven on the paper
gateway (memory: fa-block-order-allocation) — in this post-build-983 unified FA config,
allocation is governed by the GROUP's stored ContractsOrShares config; an order-level
faMethod="NetLiq" is REJECTED with Error 10226. So the engine must compute each
account's exact shares itself and the group is set to those amounts. to_fa_block_inputs()
therefore emits fa_method="" (empty) on purpose — never "NetLiq".

HARD BOUNDARY: this module is pure computation. It connects to nothing, qualifies
nothing, builds no ib_async order object, and transmits nothing. It STOPS at producing
the inputs that order_router.build_fa_block (group orders) / order_router.build (direct
orders) expect, packaged as a reviewable what-if plan. Arming/transmission stays behind
the independent READONLY + DRY_RUN + armed gates, untouched here.

Reuses, rather than duplicates: reconcile.reconcile (share math + drift/status),
cashflows.reserve_for (distribution carve-out), and the AccountPlan / BlockOrder
dataclasses defined in recon_report.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cashflows
import config
import holding_class
import investable as _investable
import reconcile
import strategy_target
from recon_report import AccountPlan, BlockOrder


# --- 1. reserve carve-out ------------------------------------------------------
# The reserve carve-out math now lives in the shared leaf module `investable` (Slice 1
# of the account-cashflow consolidation). This thin wrapper is kept so existing imports
# and tests (rebalance_engine.compute_investable) keep working unchanged — it is a pure
# re-export, behavior-identical to the previous inline body.
def compute_investable(net_liq: float, reserve: float,
                       cash_reserve_pct: float | None = None) -> float:
    """Capital the engine may deploy for one account. See investable.compute_investable
    for the canonical logic; this is a thin pass-through re-export."""
    return _investable.compute_investable(net_liq, reserve, cash_reserve_pct)


# --- 1b. held-aside carve-out --------------------------------------------------
# The SAME shape as the reserve carve-out above, one step earlier in the chain: a set-aside
# amount is removed from NetLiq and everything downstream (targets, drift, the band) is
# measured against what remains. The difference is only where the number comes from —
# the reserve is a dollar figure from the cashflow schedule, the held-aside amount is
# VALUED FROM POSITIONS by holding_class.
#
# The two COMPOSE, in this order (each carve-out is removed from what the previous one
# left, never from the raw NetLiq twice):
#
#     managed_net_liq = net_liq - held_aside_value          (not ours to trade at all)
#     investable      = (managed_net_liq - reserve) * (1 - cash_reserve_pct)
#
# The distribution reserve is still computed on the WHOLE account (a client's distribution
# obligation does not shrink because part of their money sits in bonds) but is carved out
# of the sleeve we can actually trade — which is the only place cash can be raised.
#
# With no held-aside holdings, held_aside_value is 0.0, managed_net_liq == net_liq, and
# every number below is bit-for-bit what it was before this existed.
def carve_out_held_aside(net_liq: float, positions: dict,
                         sec_types: dict | None = None,
                         prices: dict | None = None,
                         values: dict | None = None):
    """Split one account into its MANAGED sleeve and its HELD-ASIDE block (thin re-export
    of holding_class.carve_out, kept here so the engine's carve-out chain reads in one
    place). ``sec_types=None`` -> nothing is held aside (today's behavior exactly)."""
    return holding_class.carve_out(net_liq, positions, sec_types=sec_types,
                                   prices=prices, values=values)


# --- shared no-trade band test -------------------------------------------------
# The account-level breach decision is needed in two places that MUST agree byte-for-byte:
# plan_account (which then emits the deltas) and the propose-only account_monitor (which
# only emits a REBALANCE verdict). Factored here as ONE function so there is a single
# definition of "does this account breach the band?" — never a copy that can drift.
def _trade_weight(ln, net_liq: float, target: strategy_target.Target,
                  prices: dict | None = None, strict_prices: bool = False) -> float:
    """Size of the trade THIS line would require, as a fraction of NetLiq:
    |target_shares - actual_shares| * price / NetLiq. `prices` (symbol->price) overrides
    the strategy-data close; `strict_prices` makes it the ONLY source (execution rails).
    A missing/non-positive/NaN price or zero NetLiq -> 0.0 (no trade weight contributed)."""
    raw = (prices or {}).get(ln.symbol, None)
    if raw is None and not strict_prices:
        raw = target.prices.get(ln.symbol, None)
    price = reconcile.usable_price(raw)
    if price is None or not net_liq:
        return 0.0
    return abs(ln.target_shares - int(ln.actual_shares)) * price / net_liq


# Statuses the engine must NEVER auto-trade or breach the band on (S0 corp-action guard):
# an ALIEN (corp-action/manual) holding is surfaced for human review, a FRACTIONAL DRIP
# stub is recorded but too small to trade, a SWEEP is a whitelisted cash/money-market
# fund. None of them may (a) count toward the trade-size band or (b) produce a delta.
# UNPRICED joins them (v0.42.0): a symbol the model wants but we could not price has an
# UNKNOWABLE trade size, and _trade_weight would score it 0.0 — a permissive guess. It is
# never traded and never counted; it is SURFACED, via blocked_reasons / unpriced_reasons.
_NO_AUTOTRADE_STATUSES = frozenset({"ALIEN", "FRACTIONAL", "SWEEP", reconcile.UNPRICED})
# Statuses that ALWAYS breach regardless of trade size — a KNOWN held symbol the model
# dropped to 0% must be cleared. UNTRACKED is the legacy (universe=None) equivalent;
# ROTATE_OUT is its refined form when a universe is supplied. Both mean "sell it".
# FRACTIONAL was added here in v0.50.0 and REMOVED in v0.57.0. The intent was right -- a
# stub of a dropped holding should go -- but IBKR will not accept ANY fractional order via
# the API (error 10243, see config.BLOCK_ORDERS_WHOLE_SHARES_ONLY). So a stub is not
# actionable, and making it always-breach created a permanent loop: the stub forces the
# account to breach, the account is pulled into the run, it cannot clear the stub, and it
# breaches again on the next run, forever. Measured 2026-09-04 after a clean run of two
# models: 41 of 43 accounts would have re-traded, ALL 41 driven by a stub alone, ZERO
# breaching on trade size. A stub is REPORTED by verify_in_sync and never traded on;
# clearing it needs the desktop platform.
_ALWAYS_BREACH_STATUSES = frozenset({"UNTRACKED", "ROTATE_OUT"})


def band_breached(lines, net_liq: float, target: strategy_target.Target,
                  prices: dict | None = None, band_pct: float | None = None,
                  strict_prices: bool = False) -> bool:
    """ACCOUNT-LEVEL, all-or-nothing no-trade band test (the single source of truth).

    Returns True iff the account needs work: some holding's required TRADE SIZE exceeds
    band_pct of NetLiq, OR a stray UNTRACKED/ROTATE_OUT/FRACTIONAL position is held (a
    dropped ticker, always cleared regardless of size -- a stub included). The breach test keys on trade size, NOT
    raw weight-vs-model drift, so the cash-reserve gap (a fully-invested account sits
    ~reserve% under raw model weight by construction) can never falsely trip it.

    An ALIEN / SWEEP line NEVER breaches by itself — an alien holding is reviewed by a
    human, not auto-swept, and a whitelisted sweep is held by design. So an alien-only
    cycle is 'needs review', not a false 'band breach' page. A FRACTIONAL stub DOES
    breach (v0.50.0): it is a holding the model dropped, and it is cleared on sight.
    Pure — reads `lines` only (statuses are set by reconcile with the universe)."""
    if band_pct is None:
        band_pct = config.REBALANCE_BAND_PCT
    return (any(ln.status in _ALWAYS_BREACH_STATUSES for ln in lines)
            or any(_trade_weight(ln, net_liq, target, prices, strict_prices) > band_pct
                   for ln in lines if ln.status not in _NO_AUTOTRADE_STATUSES))


# --- 2. per-account target shares, deltas, band suppression --------------------
# The statuses a FULL EXIT may clear outright, fraction included. ROTATE_OUT is a KNOWN
# ticker the model dropped; FRACTIONAL is the sub-1-share stub a previous truncated exit left
# behind. ALIEN is deliberately ABSENT - an unrecognised holding (a spinoff, a rename, a
# client's own position) is still never auto-traded - and so is a SWEEP held by design.
_FULL_EXIT_STATUSES = frozenset({reconcile.ROTATE_OUT, reconcile.FRACTIONAL})


def plan_account(account: str, version: str, net_liq: float, positions: dict,
                 target: strategy_target.Target,
                 prices: dict | None = None,
                 band_pct: float | None = None,
                 universe: set[str] | None = None,
                 sec_types: dict | None = None,
                 values: dict | None = None,
                 cash_reserve_pct: float | None = None,
                 strict_prices: bool = False) -> AccountPlan:
    """Reconcile ONE account against its tier model and emit the share deltas to fix it.

    Steps (all pure):
      * carve     = carve_out_held_aside(net_liq, positions, sec_types, ...)
                    (held-aside carve-out: instruments the desk never trades — individual
                    bonds first among them — are valued and removed from the account BEFORE
                    anything else. Their value sits OUTSIDE the target allocation.)
      * reserve  = cashflows.reserve_for(account, net_liq)        (distribution carve-out)
      * investable = compute_investable(managed_net_liq, reserve, cash_reserve_pct)
      * lines    = reconcile.reconcile(...) — gives, per symbol, the integer target_shares
                   (= int(weight * investable / price)) and the drift weight vs the model.
      * NO-TRADE BAND (account-level, all-or-nothing): the account is left exactly as-is
        unless some holding needs a trade larger than band_pct of NetLiq (or a stray
        UNTRACKED position is held). When that happens the WHOLE account is rebalanced back
        to model — every holding with a >=1 share delta moves, in-band ones included. The
        band is measured on the required trade size, not raw weight-vs-model drift (so the
        cash-reserve gap can't trip it); applied identically to every account.

    `prices` (symbol->price) overrides the strategy-data close for sizing/valuation
    (e.g. live quotes at order time). `orders` maps symbol -> signed share delta
    (target - actual); positive = BUY, negative = SELL. Empty if the whole account is
    in-band.

    HELD-ASIDE (`sec_types`, `values`) — the no-trade list, 2026-08-19
    -----------------------------------------------------------------
    `sec_types` maps symbol -> instrument type (IBKR `contract.secType` on the live lane,
    `asset_category` on the CRM lane). It is the ONLY classification input — never a
    symbol-string guess. `values` (symbol -> broker-reported market value) is an optional
    pricing fallback for a held-aside holding with no usable quote.

    When supplied, holding_class splits the account: HELD-ASIDE holdings are valued,
    removed from NetLiq, and reported on the plan — and they NEVER reach reconcile, so they
    can never become a drift gap, an UNTRACKED/ALIEN line, or a leg of any kind (not a buy,
    not a sell, not an ALIEN liquidation). The model's weights then apply to the REMAINING
    sleeve as its own 100%, and both the band test and the drift measurement use that
    sleeve's NetLiq. An account holding bonds therefore rebalances its non-bond sleeve
    normally instead of being benched.

    `sec_types=None` (the default, and every pre-existing caller) carves out NOTHING and
    every number below is exactly what it was before this existed.

    PER-MODEL CASH RESERVE (`cash_reserve_pct`, 2026-08-25)
    ------------------------------------------------------
    THIS model's standing cash reserve as a fraction of NAV — 1% for an Andrew-authored
    custom allocation, 1.5% (the global default) for S0 and everything else. None keeps
    the global, so every pre-existing caller is byte-identical.

    The caller resolves it SOURCE-based (does the label have rows in the CRM
    custom-allocation view) and never from the label's spelling; the engine stays pure and
    reads no CRM. It is threaded to BOTH sites that must agree:
      * compute_investable(), which SIZES the book, and
      * reconcile(), which MEASURES the CASH bucket's drift.
    Sizing at 1% while measuring at 1.5% would leave the account permanently 0.5% adrift
    on cash by construction — the account would never read in-spec. The risk lines' own
    drift is unaffected (they are always measured against the raw model weight), and the
    no-trade band keys on trade SIZE, not drift, so this cannot change a breach decision
    for a correctly-sized account — it changes how much gets deployed and what the cash
    readout claims to want."""
    if band_pct is None:
        band_pct = config.REBALANCE_BAND_PCT

    # Carve-out 1 of 2: instruments we never trade leave the account entirely. With no
    # sec_types this is the identity (managed_net_liq == net_liq, nothing held aside).
    carve = carve_out_held_aside(net_liq, positions, sec_types=sec_types,
                                 prices=prices, values=values)
    managed_positions = carve.managed_positions
    managed_net_liq = carve.managed_net_liq

    # The distribution reserve is an obligation of the WHOLE account (bonds do not shrink
    # a client's scheduled distribution), so it is still computed on net_liq — but it is
    # carved out of the managed sleeve, the only place cash can actually be raised.
    reserve = cashflows.reserve_for(account, net_liq)
    investable = compute_investable(managed_net_liq, reserve, cash_reserve_pct)

    # tolerance_w=band_pct so reconcile classifies a holding MATCHED iff it's inside the
    # band; DRIFTED/MISSING/ROTATE_OUT mean it breached and is eligible for a delta. When
    # `universe` is supplied, reconcile splits the old UNTRACKED bucket into
    # ROTATE_OUT (sell) / ALIEN (review) / FRACTIONAL / SWEEP (all no-autotrade); when
    # None, it stays UNTRACKED (behavior-preserving default — backtester untouched).
    # Reconcile the MANAGED sleeve against the model as its own 100%: managed_net_liq is
    # the denominator for every weight/drift, and only managed positions are lines at all.
    # The SAME cash_reserve_pct goes to the measurement side as went to the sizing side
    # above — one value per account, never two (phantom-drift guard).
    lines = reconcile.reconcile(target, managed_net_liq, managed_positions, prices=prices,
                               tolerance_w=band_pct, investable=investable,
                               universe=universe, cash_reserve_pct=cash_reserve_pct,
                               strict_prices=strict_prices)

    # NO-TRADE BAND — ACCOUNT-LEVEL, all-or-nothing (Andrew's decision 2026-06-27): leave
    # the whole account alone unless it genuinely needs work; if it does, rebalance EVERY
    # holding back to model in one pass (in-band siblings included). One band rule for all
    # accounts (compliance: no per-client discretion).
    #
    # The breach test is on the SIZE OF THE TRADE the rebalance would make
    # (|target_shares - actual_shares| valued vs NetLiq), NOT raw weight-vs-model drift.
    # Why: the cash-reserve means a fully-invested account sits ~reserve% under its raw
    # model weight by construction, so keying off raw drift (as reconcile's status does)
    # would falsely flag a correctly-invested account and, for any holding >~60% weight,
    # defeat the band entirely. A stray UNTRACKED position always breaches (it must be
    # cleared regardless of size). The breach decision lives in band_breached() above so
    # the propose-only account_monitor uses the EXACT same test (no copy-paste).
    # Band measured against the MANAGED sleeve (== net_liq when nothing is held aside): a
    # 4% drift in the tradeable half of a half-bond account is a real 4% breach, not a
    # diluted 2% that would never trip.
    breached = band_breached(lines, managed_net_liq, target, prices=prices,
                             band_pct=band_pct, strict_prices=strict_prices)

    # ALIEN / FRACTIONAL / SWEEP lines are NEVER auto-traded (corp-action guard): an alien
    # holding is left in place and surfaced for human review, a fractional DRIP stub is too
    # small to trade (int==0 -> delta 0 anyway), a whitelisted sweep is held by design. Only
    # KNOWN symbols (a real BUY/SELL/ROTATE_OUT delta) move.
    orders: dict = {}
    if breached:
        for ln in lines:
            # FULL EXIT SELLS THE WHOLE POSITION, FRACTION INCLUDED (owner decision
            # 2026-09-04). The model wants NONE of this symbol, so the order is the entire
            # holding, not int() of it.
            #
            # WHY. delta = target - int(actual) sold 13 of a 13.8499 holding and stranded
            # 0.8499 forever: on the next run int(0.8499) is 0, the line classifies
            # FRACTIONAL, FRACTIONAL is in _NO_AUTOTRADE_STATUSES, and no order is ever
            # produced for it again. The desk manufactured the stub and then refused to clear
            # it; account U7586137 was found holding eleven of them. The mutual-fund path
            # already follows this rule for the same stated reason: selling out means selling
            # the whole position INCLUDING the fraction, or the account never actually closes
            # the holding.
            #
            # ONLY a full exit. A drift trade still moves whole shares, so ordinary
            # rebalancing is unchanged, and a fractional holding the model WANTS is left be.
            full_exit = (ln.target_shares == 0 and ln.actual_shares
                         and ln.status in _FULL_EXIT_STATUSES)
            if full_exit and config.SELL_WHOLE_POSITION_ON_EXIT:
                orders[ln.symbol] = -float(ln.actual_shares)
                continue
            if ln.status in _NO_AUTOTRADE_STATUSES:
                continue
            delta = ln.target_shares - int(ln.actual_shares)
            if abs(delta) >= 1:
                orders[ln.symbol] = delta

    # UNPRICED MODEL HOLDINGS (v0.42.0) — the same fail-closed road the held-aside carve-out
    # already takes, with the isolate-vs-block split documented in reconcile.split_unpriced:
    #   HELD + unpriced -> BLOCK the account (its value is inside NetLiq but unaccounted for,
    #                      so every sibling target is sized off a base we cannot break down).
    #   NOT HELD        -> ISOLATE: that sleeve stays in cash, the rest of the account
    #                      rebalances normally, and the reason travels on the plan so the
    #                      account can never be reported "in spec, nothing to trade".
    held_unpriced, wanted_unpriced = reconcile.split_unpriced(lines)
    blocked_reasons = list(carve.blocked_reasons)
    blocked_reasons += [reconcile.UNPRICED_HELD_REASON.format(
        symbol=ln.symbol, shares=ln.actual_shares) for ln in held_unpriced]
    unpriced_reasons = [reconcile.UNPRICED_WANTED_REASON.format(
        symbol=ln.symbol, weight=ln.target_weight) for ln in wanted_unpriced]

    # FAIL CLOSED: a held-aside holding we could not price (or one worth more than the whole
    # account) makes the managed sleeve's size a guess. Report everything, emit nothing —
    # the account still surfaces with its reason instead of silently sizing off a bad base.
    if blocked_reasons:
        orders = {}
        breached = False

    alien_lines = [ln for ln in lines if ln.status == "ALIEN"]
    return AccountPlan(account, version, net_liq, reserve, investable,
                       lines, breached, orders, alien_lines,
                       managed_net_liq=carve.managed_net_liq,
                       held_aside_value=carve.held_aside_value,
                       held_aside=list(carve.held_aside),
                       blocked_reasons=blocked_reasons,
                       unpriced_reasons=unpriced_reasons,
                       cash_reserve_pct=(_investable.buffer_pct()
                                         if cash_reserve_pct is None
                                         else float(cash_reserve_pct)))


def plan_accounts(account_inputs: list[dict],
                  targets: dict,
                  band_pct: float | None = None,
                  universe: set[str] | None = None,
                  cash_reserve_pct_by_version: dict | None = None) -> list[AccountPlan]:
    """Plan many accounts at once. `account_inputs` is a list of dicts, each:
        {account, version, net_liq, positions, prices(optional),
         sec_types(optional), values(optional)}
    `targets` maps version -> strategy_target.Target (one model per risk tier, run once).

    `sec_types` (symbol -> instrument type) and `values` (symbol -> broker-reported market
    value) are the OPTIONAL held-aside inputs — see plan_account. An account_input without
    them behaves exactly as it always has (nothing held aside).

    `universe` (the strategy's tradeable symbols) is threaded into every account's
    reconcile so a held symbol the model dropped is classified ROTATE_OUT (sell) vs an
    alien corp-action holding ALIEN (review). None (default) preserves legacy UNTRACKED
    behavior for every account — nothing changes for callers that don't pass it.

    `cash_reserve_pct_by_version` maps version -> that model's standing cash reserve, for
    the models that name their own (today: Andrew-authored custom allocations at 1%). A
    version ABSENT from the map — or a map of None — gets the global default (1.5%, S0's
    value). Per-VERSION, not per-account, because the reserve is a property of the model,
    and looked up with .get() so a mixed batch of S0 and custom accounts resolves each
    account independently: adding a custom model to a batch cannot move an S0 account's
    reserve, and a missing entry fails toward today's behavior rather than toward 0.

    This is the seam the live recon_report fills from accounts.discover()+ib.positions();
    keeping it dict-driven means the engine is testable with synthetic data and never
    has to touch a broker itself."""
    reserves = dict(cash_reserve_pct_by_version or {})
    plans: list[AccountPlan] = []
    for a in sorted(account_inputs, key=lambda x: x["account"]):
        plans.append(plan_account(
            a["account"], a["version"], a["net_liq"], a["positions"],
            targets[a["version"]], prices=a.get("prices"), band_pct=band_pct,
            universe=universe, sec_types=a.get("sec_types"), values=a.get("values"),
            cash_reserve_pct=reserves.get(a["version"]),
            # EXECUTION RAILS pass strict_prices=True per account_input: their `prices` dict
            # is the broker's live quotes and is the ONLY price source. Absent -> False, so
            # every offline/backtest caller is byte-identical.
            strict_prices=bool(a.get("strict_prices", False))))
    return plans


# --- 3. block aggregation + per-account split ----------------------------------
def aggregate_blocks(plans: list[AccountPlan]) -> list[BlockOrder]:
    """Aggregate same-tier, same-symbol, same-DIRECTION deltas into block orders.

    A block executes ONE side at one average price, so BUYs and SELLs of the same symbol
    in the same tier stay separate blocks. per_account records the EXACT share split,
    fixed here at build time and never reallocated after fills (allocation-fairness /
    recordkeeping). By construction sum(per_account.values()) == total_qty."""
    blocks: dict[tuple, BlockOrder] = {}
    for p in plans:
        for sym, delta in p.orders.items():
            side = "BUY" if delta > 0 else "SELL"
            key = (p.version, sym, side)
            blk = blocks.get(key)
            if blk is None:
                blk = blocks[key] = BlockOrder(p.version, sym, side, 0)
            qty = abs(delta)
            blk.total_qty += qty
            blk.per_account[p.account] = blk.per_account.get(p.account, 0) + qty
    return sorted(blocks.values(), key=lambda b: (b.version, b.symbol, b.side))


CROSS_MODEL_VERSION = "ALL MODELS"


def aggregate_blocks_by_ticker(plans: list[AccountPlan]) -> list[BlockOrder]:
    """Aggregate same-symbol, same-DIRECTION deltas across EVERY model into one block each.

    The cross-model counterpart to :func:`aggregate_blocks`, which keys on
    ``(version, symbol, side)`` and therefore produces one block per model per ticker.

    WHY THIS EXISTS (owner decision 2026-09-03). The model decides HOW MANY SHARES each
    account needs; it has no business reaching the order layer. An IBKR API order is always
    for ONE CONTRACT, and IBKR exposes no verb that rebalances a group to target percentages
    (verified against docs/MODEL_PORTFOLIO_RESEARCH.md and the TWS API FA page: the allocation
    methods are EqualQuantity / NetLiq / AvailableEquity / PctChange / ContractsOrShares, and
    there is no rebalance call). So a rebalance IS N block orders whichever way it is sliced -
    and slicing per MODEL would mean six separate XLE orders at six different average prices
    for the same ETF on the same day. Slicing per TICKER means ONE XLE order, and every
    account across every model fills at the SAME average price. On the 2026-09-03 book that is
    the difference between roughly 96 orders and roughly 17.

    A block is one side at one price, so BUY and SELL of the same symbol stay separate blocks.
    ``per_account`` records the EXACT share split, fixed at build time and never reallocated
    after fills (allocation fairness and recordkeeping), and by construction
    ``sum(per_account.values()) == total_qty``.

    ``version`` on the returned blocks is :data:`CROSS_MODEL_VERSION`, not any real model
    name: these blocks deliberately span models, and putting a real version there would invite
    a caller to look up a per-model group or a per-model cash reserve from a block that has
    neither.

    RAISES ValueError if one account ends up on BOTH sides of the same symbol. That cannot
    happen from plan_account (one net delta per symbol per account) so it means the caller
    mixed plans from two different runs of the same account - which would double-trade it.
    """
    blocks: dict[tuple, BlockOrder] = {}
    sides_seen: dict[tuple, str] = {}
    for p in plans:
        for sym, delta in p.orders.items():
            if not delta:
                continue
            side = "BUY" if delta > 0 else "SELL"
            prior = sides_seen.get((p.account, sym))
            if prior is not None and prior != side:
                raise ValueError(
                    f"aggregate_blocks_by_ticker: account {p.account} appears on BOTH sides of "
                    f"{sym} ({prior} and {side}). One account has one net delta per symbol, so "
                    f"this means plans from two different runs were mixed - refusing to build a "
                    f"split that would double-trade it.")
            sides_seen[(p.account, sym)] = side
            key = (sym, side)
            blk = blocks.get(key)
            if blk is None:
                blk = blocks[key] = BlockOrder(CROSS_MODEL_VERSION, sym, side, 0)
            qty = abs(delta)
            blk.total_qty += qty
            blk.per_account[p.account] = blk.per_account.get(p.account, 0) + qty
    return sorted(blocks.values(), key=lambda b: (b.symbol, b.side))


# --- 4. order-router inputs (group block vs single-account direct) -------------
@dataclass
class RoutePlan:
    """A reviewable, transmit-free routing plan for ONE block. It carries exactly the
    inputs order_router would consume — but builds no order object and sends nothing.

      route == "fa_block"  -> order_router.build_fa_block(symbol, side, total_qty,
                                  limit_price, fa_group, fa_method, as_of, ib)
                              with fa_method="" (group's ContractsOrShares governs; an
                              order-level faMethod="NetLiq" is rejected — Error 10226).
                              per_account_split is the explicit ContractsOrShares the
                              tier GROUP must be set to before this block is placed.
      route == "direct"    -> a single-account true-up; order_router.build(...) for that
                              one account (no group; account field set on the order)."""
    route: str                         # "fa_block" | "direct"
    version: str                       # risk tier
    symbol: str
    side: str                          # BUY | SELL
    total_qty: int
    fa_group: str | None               # tier group name for a block; None for direct
    fa_method: str                     # "" for a block (ContractsOrShares group); "" for direct
    account: str | None                # the single account for a direct order; None for a block
    per_account_split: dict = field(default_factory=dict)  # account -> shares (sums to total_qty)
    reason: str = "REBALANCE_TO_MODEL"


# Per-tier FA groups already defined on the paper gateway (memory: verified state). The
# engine names the group; it never creates or edits one here (that is a serialized
# replaceFA admin step the human conductor runs).
TIER_GROUPS = {
    "Conservative": "tier_conservative",
    "Balanced": "tier_balanced",
    "Growth": "tier_growth",
}


def route_blocks(blocks: list[BlockOrder],
                 tier_groups: dict | None = None) -> list[RoutePlan]:
    """Decide, per block, whether it is a true multi-account BLOCK (FA group order) or a
    single-account true-up that falls back to a DIRECT order.

      * >= 2 accounts in the split  -> fa_block (faGroup set, faMethod="" so the group's
        explicit ContractsOrShares allocation governs).
      * exactly 1 account           -> direct (the group machinery buys nothing for a lone
        account; a plain single-account order is simpler and equally auditable).

    fa_group/per_account_split are returned for the conductor to (a) set the group's
    ContractsOrShares to per_account_split, then (b) place the block — both serialized,
    live steps OUTSIDE this module."""
    if tier_groups is None:
        tier_groups = TIER_GROUPS
    routes: list[RoutePlan] = []
    for b in blocks:
        accts = b.per_account
        if len(accts) >= 2:
            routes.append(RoutePlan(
                route="fa_block", version=b.version, symbol=b.symbol, side=b.side,
                total_qty=b.total_qty, fa_group=tier_groups.get(b.version),
                fa_method="", account=None, per_account_split=dict(accts),
                reason=b.reason))
        else:
            # exactly one account -> direct true-up
            (only_acct, qty), = accts.items()
            routes.append(RoutePlan(
                route="direct", version=b.version, symbol=b.symbol, side=b.side,
                total_qty=qty, fa_group=None, fa_method="", account=only_acct,
                per_account_split={only_acct: qty}, reason=b.reason))
    return routes


def build_plan(account_inputs: list[dict], targets: dict,
               band_pct: float | None = None,
               tier_groups: dict | None = None,
               universe: set[str] | None = None,
               cash_reserve_pct_by_version: dict | None = None) -> dict:
    """End-to-end PURE pipeline: per-account plans -> blocks -> route plans.

    Returns {"plans", "blocks", "routes"} — a complete, reviewable what-if of every
    order the engine WOULD route, with nothing built and nothing transmitted. The live
    caller (recon_report / execution_engine) attaches a limit price and hands each
    RoutePlan to order_router, still behind the arming gate.

    `universe` is threaded into per-account reconcile (see plan_accounts). ALIEN holdings
    produce NO route (they are collected on each AccountPlan.alien_lines for human review);
    only ROTATE_OUT/DRIFTED/MISSING deltas aggregate into blocks. None -> legacy behavior.

    HELD-ASIDE holdings (per-account `sec_types`) produce NO route either, and by a stronger
    mechanism than ALIEN: they never become reconcile lines at all, so there is no delta to
    aggregate and no path by which a block could ever contain one. They are carried on each
    AccountPlan.held_aside for the reporting surfaces.

    `cash_reserve_pct_by_version` is threaded straight into plan_accounts (see there): the
    per-model cash reserve, keyed by version, defaulting to the global for any version not
    named. It changes how much each account deploys and what its CASH line targets; it
    changes nothing about block aggregation or routing."""
    plans = plan_accounts(account_inputs, targets, band_pct=band_pct, universe=universe,
                          cash_reserve_pct_by_version=cash_reserve_pct_by_version)
    blocks = aggregate_blocks(plans)
    routes = route_blocks(blocks, tier_groups=tier_groups)
    return {"plans": plans, "blocks": blocks, "routes": routes}
