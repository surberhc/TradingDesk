"""
account_monitor.py — the per-account MONITOR BRAIN: a pure, PROPOSE-ONLY decision core.

Slice 5 of the account-cashflow build. Given one account's state (NetLiq, cash, positions,
its tier model target, and its client distribution schedule), `decide()` returns a single
`Verdict` — HOLD, REBALANCE, or ALERT — and NOTHING else. It composes the existing pure
pieces (reconcile drift, the shared no-trade band test, and the cashflow reserve policy)
into one verdict per account.

CORE PRINCIPLE — PROPOSE, NEVER DISPOSE
---------------------------------------
This module is a hard, test-enforced boundary (test_account_monitor.py asserts it). It
imports NOTHING that can transmit, arm, or build an order:
  * NO order_router (build / build_fa_block / transmit / arming)
  * NO connections.ibkr / gateway / broker session
  * NO Task Scheduler, NO state files
It mirrors reconcile/rebalance_engine's read-only posture: it stops at producing a
reviewable verdict. If composing a verdict ever required something that can transmit, that
is a design error — STOP, do not import it.

DEPOSIT DETECTION (Slice 6a — PURE core, built here; live shell deferred to 6b)
-------------------------------------------------------------------------------
`decide()` now also emits a `REBALANCE` verdict with reason `DEPOSIT_ARRIVED` when an
EXTERNAL deposit has landed in the account. It does this from PURE inputs only — a prior
settled-cash BASELINE plus today's settled cash and today's executions, all carried on
`AccountState`. It still builds and transmits NOTHING; the live shell (Slice 6b) owns
fetching the baseline / executions off the broker and persisting the baseline. This slice
makes NO broker connection, NO reqExecutions call, and writes NO state file.

A cash increase is classified three ways and only ONE is a deposit:
  * EXTERNAL DEPOSIT — cash up beyond both guards, with NO sell execution that explains it.
  * SALE-RAISED      — cash up because a position was SOLD today (a 'SLD' fill whose
                       proceeds ≈ the cash delta). NOT a deposit; the rebalance engine
                       already accounts for it. No DEPOSIT_ARRIVED.
  * DIVIDEND/INTEREST/ROUNDING — a small increase below the guards, no fill. Never trips.
See `classify_cash_increase` for the exact rules and the three guards.

REAL CLIENT DATA: the real per-account distribution schedule is personal client data and
is NOT in committed code (cashflows.SCHEDULE stays empty). `decide()` reads the schedule
from the AccountState passed in, so it is exercised only with SYNTHETIC fixtures in tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import cashflows
import config
import rebalance_engine
import reconcile
import strategy_target


# --- verdict + state ----------------------------------------------------------
@dataclass(frozen=True)
class Verdict:
    """The monitor's PROPOSE-ONLY output for one account. Carries an action and the
    machine-readable reason behind it; it never carries an order or a transmit instruction.

      action  : "HOLD"      — in-band, no flow issues, no stray positions. Do nothing.
                "REBALANCE"  — the no-trade band is breached; the engine WOULD true the
                               account back to model (the monitor only proposes it).
                "ALERT"      — a human-attention condition (an upcoming distribution that
                               available cash/reserve does not cover, or an untracked held
                               position). Never auto-acted.
      reason  : a stable reason CODE string (see the constants below).
      detail  : context for the reason (numbers, symbols) — for the report, not for trading.
    """
    account: str
    action: str
    reason: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Execution:
    """One of today's fills for the account, in the REAL `reqExecutions` shape (the fields
    a live read-only probe confirmed 2026-06-30). PURE data — no broker handle.

      symbol  : contract.symbol of the filled instrument.
      side    : execution side as IBKR reports it — 'BOT' (bought) or 'SLD' (sold).
      shares  : filled quantity for this execution.
      price   : fill price per share.

    Proceeds of a sale = shares * price; that is what `classify_cash_increase` matches a
    settled-cash jump against to recognise a SALE-RAISED (not external) cash increase.
    The live shell (Slice 6b) maps ib.reqExecutions rows (acctNumber, side, shares, price,
    contract.symbol, …) onto these; the pure core never calls reqExecutions itself.
    """
    symbol: str
    side: str
    shares: float
    price: float

    def proceeds(self) -> float:
        """Cash value of this fill (shares * price). For a 'SLD' fill this is the cash the
        sale RAISED into the account; for 'BOT' it is cash spent."""
        return float(self.shares) * float(self.price)


@dataclass(frozen=True)
class AccountState:
    """Everything `decide` needs for ONE account — a plain data bundle, no broker handle.

    The live caller (a future, separate Slice that owns the read-only broker read) fills
    this from ib.accountSummary/positions; the pure core never reaches a broker itself.

      account   : full account number (e.g. "DU8922142").
      version   : risk tier / strategy version (Conservative | Balanced | Growth).
      net_liq   : account NetLiquidation.
      cash      : settled/available cash currently in the account.
      positions : symbol -> shares actually held (risk positions; nonzero).
      schedule  : list[cashflows.Flow] — this account's distribution/contribution flows.
                  SYNTHETIC in tests; sourced from real client data later (not committed).
      target    : strategy_target.Target for this account's tier (model weights + prices).

    DEPOSIT-DETECTION inputs (Slice 6a — accepted as inputs, never fetched/persisted here):
      settled_cash         : TODAY's settled cash (the amount from the SettledCashByDate
                             tag, decoded by accounts.parse_settled_cash_by_date). None when
                             unavailable -> deposit detection is skipped (no false positive).
      baseline_settled_cash: the PRIOR settled-cash baseline this is diffed against. None on
                             a cold start (first observation) -> no deposit can be claimed.
      baseline_date        : the date the baseline was captured (for the verdict detail /
                             debounce reasoning). None when there is no baseline.
      as_of_date           : the date `settled_cash` is observed for (today). Used with
                             `deposit_already_flagged_today` to debounce to one per day.
      fills                : list[Execution] — TODAY's executions for this account (often
                             EMPTY; an empty set is normal). Used to tell a SALE-RAISED cash
                             jump apart from an external deposit.
      deposit_already_flagged_today : DEBOUNCE flag the live shell sets True once a
                             DEPOSIT_ARRIVED has already fired for this account today, so a
                             second same-day evaluation cannot re-flag the same deposit.
    """
    account: str
    version: str
    net_liq: float
    cash: float
    positions: dict
    schedule: list
    target: strategy_target.Target
    # --- Slice 6a deposit-detection inputs (defaulted so non-deposit callers are unchanged)
    settled_cash: float | None = None
    baseline_settled_cash: float | None = None
    baseline_date: date | None = None
    as_of_date: date | None = None
    fills: list = field(default_factory=list)
    deposit_already_flagged_today: bool = False


# --- reason codes (stable strings) --------------------------------------------
REASON_IN_BAND = "IN_BAND"                              # HOLD
REASON_DRIFT_BAND_BREACH = "DRIFT_BAND_BREACH"          # REBALANCE
REASON_WITHDRAWAL_DUE_UNRESERVED = "WITHDRAWAL_DUE_UNRESERVED"   # ALERT
REASON_UNTRACKED_POSITION = "UNTRACKED_POSITION"        # ALERT
REASON_DEPOSIT_ARRIVED = "DEPOSIT_ARRIVED"             # REBALANCE (Slice 6a)


# --- deposit-detection guards --------------------------------------------------
# A cash increase fires a deposit ONLY when it clears BOTH a NAV-fraction floor AND an
# absolute-dollar floor — and is not explained by a sale. These keep dividends, interest,
# and rounding from ever tripping a (re)deployment proposal (over-trading guards).
#
# Guard 1 (NAV-FRACTION FLOOR): the deposit must be at least this fraction of NAV. Tied to
# the rebalance no-trade band — a deposit smaller than the band would not move any holding
# past its band anyway, so re-deploying it would be churn. Same single source of truth
# (config.REBALANCE_BAND_PCT) the drift test uses, so the two policies can't drift apart.
DEPOSIT_NAV_FRACTION = config.REBALANCE_BAND_PCT       # 0.03 of NAV

# Guard 2 (ABSOLUTE-DOLLAR FLOOR): a hard minimum so that on a tiny account a sub-band
# fraction can't still be a meaningful deposit, and dividend/interest dollars are well
# under it. Belt to the NAV-fraction's suspenders.
DEPOSIT_ABS_FLOOR = 1_000.0                            # $ minimum to be a deposit

# SALE-MATCH TOLERANCE: a same-day SLD fill is treated as EXPLAINING the cash jump when its
# proceeds are within this relative tolerance of the cash delta (covers commissions /
# partial-settlement rounding). Within tolerance -> SALE-RAISED, not a deposit.
DEPOSIT_SALE_MATCH_TOL = 0.02                          # 2% of the delta


def _required_reserve(schedule: list, nav: float) -> float:
    """Dollars that should be held liquid for upcoming DISTRIBUTIONS on this account =
    RESERVE_MONTHS of monthly distributions. Reuses the cashflows reserve POLICY
    (_occurrence_amount + RESERVE_MONTHS) so the monitor and the engine agree on what a
    reserve is — but operates on the state-provided schedule, so it is pure and testable
    with synthetic flows (the global cashflows.SCHEDULE stays empty in committed code)."""
    monthly_dist = sum(cashflows._occurrence_amount(f, nav)
                       for f in schedule if f.kind == "distribution")
    return cashflows.RESERVE_MONTHS * monthly_dist


# --- deposit detection (PURE) -------------------------------------------------
def _sale_proceeds_today(fills: list) -> float:
    """Total cash RAISED by today's sales = sum of proceeds over 'SLD' executions. PURE.
    A side string is matched case-insensitively; non-sell fills contribute nothing."""
    total = 0.0
    for ex in fills:
        side = getattr(ex, "side", "")
        if isinstance(side, str) and side.upper() == "SLD":
            total += ex.proceeds()
    return total


def classify_cash_increase(state: AccountState) -> dict:
    """Classify the settled-cash change since baseline for ONE account. PURE — reads only
    the state bundle, builds/transmits nothing.

    Returns a dict the verdict layer (and the report) can read:
      {"classification": one of "NONE" | "EXTERNAL_DEPOSIT" | "SALE_RAISED"
                               | "BELOW_GUARDS" | "INSUFFICIENT_DATA" | "DEBOUNCED",
       "delta": cash increase since baseline (float, when computable),
       ...context fields per classification...}

    Decision order (only EXTERNAL_DEPOSIT is actionable):
      INSUFFICIENT_DATA — no current settled_cash or no baseline (cold start). Can't claim
                          a deposit without something to diff against. (Never a false
                          positive: a missing/garbled SettledCashByDate decodes to None.)
      NONE              — cash did not increase (flat or down; a decrease is a withdrawal,
                          handled by the withdrawal-coverage path, never a deposit here).
      DEBOUNCED         — a deposit was already flagged for this account today.
      BELOW_GUARDS      — increase is real but under the NAV-fraction OR the absolute floor
                          (dividend / interest / rounding) -> never trips a deposit.
      SALE_RAISED       — increase clears the guards BUT a same-day SLD fill's proceeds ≈
                          the delta -> the cash came from a SALE, not an external deposit.
      EXTERNAL_DEPOSIT  — clears both guards AND is not explained by a sale -> a real
                          external deposit; the only classification that proposes action.
    """
    cur = state.settled_cash
    base = state.baseline_settled_cash
    if cur is None or base is None:
        return {"classification": "INSUFFICIENT_DATA",
                "settled_cash": cur, "baseline_settled_cash": base}

    delta = float(cur) - float(base)
    if delta <= 0.0:
        return {"classification": "NONE", "delta": delta}

    if state.deposit_already_flagged_today:
        return {"classification": "DEBOUNCED", "delta": delta}

    # --- the two over-trading guards (BOTH must clear) ---
    nav = state.net_liq
    nav_floor = DEPOSIT_NAV_FRACTION * nav if nav and nav > 0 else float("inf")
    if delta < DEPOSIT_ABS_FLOOR or delta < nav_floor:
        return {"classification": "BELOW_GUARDS", "delta": delta,
                "abs_floor": DEPOSIT_ABS_FLOOR, "nav_floor": nav_floor}

    # --- sale cross-check: did a same-day SLD fill raise this cash? ---
    sale_proceeds = _sale_proceeds_today(state.fills)
    if sale_proceeds > 0.0 and abs(sale_proceeds - delta) <= DEPOSIT_SALE_MATCH_TOL * delta:
        return {"classification": "SALE_RAISED", "delta": delta,
                "sale_proceeds": sale_proceeds}

    return {"classification": "EXTERNAL_DEPOSIT", "delta": delta,
            "baseline_settled_cash": float(base), "settled_cash": float(cur),
            "baseline_date": state.baseline_date, "as_of_date": state.as_of_date,
            "sale_proceeds_today": sale_proceeds,
            "abs_floor": DEPOSIT_ABS_FLOOR, "nav_floor": nav_floor}


def decide(state: AccountState) -> Verdict:
    """Return ONE Verdict for the account. PURE: reads state, composes existing pure
    pieces (reconcile.reconcile, rebalance_engine.band_breached, the cashflow reserve
    policy), transmits NOTHING.

    Precedence (most-urgent human-attention condition first):
      1. ALERT  WITHDRAWAL_DUE_UNRESERVED — an upcoming distribution exists but available
                cash does not cover the required reserve. (Liquidity safety: surfaced
                before a rebalance so cash earmarked for a client is never traded away.)
      2. ALERT  UNTRACKED_POSITION        — a held symbol the model does not know about.
      3. REBALANCE DEPOSIT_ARRIVED        — (Slice 6a) a confirmed EXTERNAL deposit landed
                (settled cash up past both guards, not explained by a sale). Proposes
                putting the new cash to work. Ranked above the generic drift code so a
                fresh deposit reports the specific reason (a deposit raises cash and would
                also trip drift, but DEPOSIT_ARRIVED is the actionable explanation). Still
                a PROPOSAL only — routes to the human review->arm->transmit gate.
      4. REBALANCE DRIFT_BAND_BREACH      — the no-trade band is breached (engine WOULD
                true the account back to model). Uses the SHARED band test, so the
                monitor's REBALANCE proposal exactly matches what rebalance_engine does.
      5. HOLD   IN_BAND                   — none of the above.

    The deposit path is liquidity-safety-subordinate by construction: the withdrawal
    ALERT is checked FIRST and returns before deposit detection runs, so an account that
    both took a deposit AND has an uncovered upcoming distribution still surfaces the
    withdrawal ALERT (cash earmarked for a client is never proposed for redeployment).
    """
    band_pct = config.REBALANCE_BAND_PCT

    # Reconcile against the tier model at band tolerance — same call the engine makes, so
    # the line statuses (UNTRACKED) and the band test below see identical inputs. Reserve
    # is carved out of investable exactly as the engine does, so target shares match.
    reserve = _required_reserve(state.schedule, state.net_liq)
    investable = reconcile._investable.compute_investable(state.net_liq, reserve)
    lines = reconcile.reconcile(state.target, state.net_liq, state.positions,
                                tolerance_w=band_pct, investable=investable)

    # 1. Withdrawal coverage — is an upcoming distribution covered by available cash?
    #    The reserve is what we MUST keep liquid for the next distribution(s); if the
    #    account's actual cash can't cover it, a distribution would force a sale. ALERT.
    if reserve > 0 and state.cash < reserve:
        return Verdict(
            state.account, "ALERT", REASON_WITHDRAWAL_DUE_UNRESERVED,
            {"required_reserve": reserve, "available_cash": state.cash,
             "shortfall": reserve - state.cash})

    # 2. Untracked / unknown held position — a symbol held but not in the model. Surface
    #    for human review (it must be investigated; the engine would clear it on rebalance,
    #    but the monitor's job is to PROPOSE/flag, not dispose).
    untracked = [ln.symbol for ln in lines
                 if ln.status == "UNTRACKED" and ln.symbol != reconcile._investable.CASH_SYMBOL]
    if untracked:
        return Verdict(
            state.account, "ALERT", REASON_UNTRACKED_POSITION,
            {"symbols": untracked})

    # 3. DEPOSIT detection (Slice 6a) — a CONFIRMED external deposit. PURE: diffs today's
    #    settled cash against a prior baseline carried on the state, cross-checks today's
    #    executions to rule out a sale, and applies the over-trading guards + per-day
    #    debounce. Only EXTERNAL_DEPOSIT proposes action; everything else (sale-raised,
    #    dividend/interest below guards, cold start, already-flagged) falls through. This is
    #    a PROPOSAL only — it builds and transmits nothing; it routes to the existing human
    #    review->arm->transmit gate exactly like any other REBALANCE verdict.
    deposit = classify_cash_increase(state)
    if deposit["classification"] == "EXTERNAL_DEPOSIT":
        return Verdict(
            state.account, "REBALANCE", REASON_DEPOSIT_ARRIVED, deposit)

    # 4. Drift band — the SHARED, single-source-of-truth account-level band test. If it
    #    breaches, the engine would rebalance the whole account back to model; the monitor
    #    PROPOSES that as a REBALANCE verdict (it builds and transmits nothing).
    if rebalance_engine.band_breached(lines, state.net_liq, state.target,
                                      band_pct=band_pct):
        n_drift = sum(1 for ln in lines
                      if ln.status in ("DRIFTED", "MISSING", "UNTRACKED"))
        return Verdict(
            state.account, "REBALANCE", REASON_DRIFT_BAND_BREACH,
            {"drifted_lines": n_drift})

    # 5. In-band, no flow issues, no stray positions -> HOLD.
    return Verdict(state.account, "HOLD", REASON_IN_BAND, {})
