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

DEFERRED TO SLICE 6 (documented seam, NOT built here)
-----------------------------------------------------
Deposit detection — DEPOSIT_ARRIVED and the cash-baseline that powers it — is explicitly
out of scope for this slice. `AccountState` deliberately OMITS the deposit/baseline fields;
see the TODO seam in `decide` where a DEPOSIT_ARRIVED verdict would slot in once a prior
cash baseline is tracked.

REAL CLIENT DATA: the real per-account distribution schedule is personal client data and
is NOT in committed code (cashflows.SCHEDULE stays empty). `decide()` reads the schedule
from the AccountState passed in, so it is exercised only with SYNTHETIC fixtures in tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field

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

    DEFERRED (Slice 6): deposit-baseline fields are intentionally absent — deposit
    detection needs a prior cash baseline to diff against, which this slice does not track.
    """
    account: str
    version: str
    net_liq: float
    cash: float
    positions: dict
    schedule: list
    target: strategy_target.Target


# --- reason codes (stable strings) --------------------------------------------
REASON_IN_BAND = "IN_BAND"                              # HOLD
REASON_DRIFT_BAND_BREACH = "DRIFT_BAND_BREACH"          # REBALANCE
REASON_WITHDRAWAL_DUE_UNRESERVED = "WITHDRAWAL_DUE_UNRESERVED"   # ALERT
REASON_UNTRACKED_POSITION = "UNTRACKED_POSITION"        # ALERT


def _required_reserve(schedule: list, nav: float) -> float:
    """Dollars that should be held liquid for upcoming DISTRIBUTIONS on this account =
    RESERVE_MONTHS of monthly distributions. Reuses the cashflows reserve POLICY
    (_occurrence_amount + RESERVE_MONTHS) so the monitor and the engine agree on what a
    reserve is — but operates on the state-provided schedule, so it is pure and testable
    with synthetic flows (the global cashflows.SCHEDULE stays empty in committed code)."""
    monthly_dist = sum(cashflows._occurrence_amount(f, nav)
                       for f in schedule if f.kind == "distribution")
    return cashflows.RESERVE_MONTHS * monthly_dist


def decide(state: AccountState) -> Verdict:
    """Return ONE Verdict for the account. PURE: reads state, composes existing pure
    pieces (reconcile.reconcile, rebalance_engine.band_breached, the cashflow reserve
    policy), transmits NOTHING.

    Precedence (most-urgent human-attention condition first):
      1. ALERT  WITHDRAWAL_DUE_UNRESERVED — an upcoming distribution exists but available
                cash does not cover the required reserve. (Liquidity safety: surfaced
                before a rebalance so cash earmarked for a client is never traded away.)
      2. ALERT  UNTRACKED_POSITION        — a held symbol the model does not know about.
      3. REBALANCE DRIFT_BAND_BREACH      — the no-trade band is breached (engine WOULD
                true the account back to model). Uses the SHARED band test, so the
                monitor's REBALANCE proposal exactly matches what rebalance_engine does.
      4. HOLD   IN_BAND                   — none of the above.
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

    # 3. Drift band — the SHARED, single-source-of-truth account-level band test. If it
    #    breaches, the engine would rebalance the whole account back to model; the monitor
    #    PROPOSES that as a REBALANCE verdict (it builds and transmits nothing).
    if rebalance_engine.band_breached(lines, state.net_liq, state.target,
                                      band_pct=band_pct):
        n_drift = sum(1 for ln in lines
                      if ln.status in ("DRIFTED", "MISSING", "UNTRACKED"))
        return Verdict(
            state.account, "REBALANCE", REASON_DRIFT_BAND_BREACH,
            {"drifted_lines": n_drift})

    # TODO (Slice 6 — DEPOSIT DETECTION SEAM): a DEPOSIT_ARRIVED verdict would slot in here,
    # comparing current cash against a tracked prior cash BASELINE (not part of AccountState
    # in this slice). When cash jumps beyond a threshold above baseline with no scheduled
    # contribution explaining it, propose putting the new cash to work. Deferred — needs the
    # baseline-tracking substrate this slice deliberately does not build.

    # 4. In-band, no flow issues, no stray positions -> HOLD.
    return Verdict(state.account, "HOLD", REASON_IN_BAND, {})
