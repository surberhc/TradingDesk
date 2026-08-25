"""
investable.py — the ONE shared "investable capital / cash buffer" formula.

LEAF module (Slice 1 of the account-cashflow build). Before this module, the same two
carve-outs were re-derived inline in five places (rebalance_engine, reconcile,
recon_report, execution_engine, and the risk_manager reserve threshold). That is five
chances for the buffer to silently disagree with itself. This collapses them into one
function and one accessor, so the buffer lives in exactly one place.

IMPORT DISCIPLINE: this module imports ONLY `config` (the single source of the
cash_reserve_pct knob). It deliberately does NOT import reconcile or rebalance_engine —
rebalance_engine imports reconcile, and reconcile/rebalance_engine will call into here,
so importing them back would create a cycle. Keeping this a leaf keeps it cycle-free.

Slice 1 was a pure consolidation: ZERO behavior change. Because the buffer now lives in
exactly one place (config.RISK_LIMITS["cash_reserve_pct"]), Slice 2 re-based it 0.05 ->
0.015 by editing that single knob — every site below reads the new value automatically.

PER-MODEL RESERVE (2026-08-25)
------------------------------
The buffer stopped being ONE number and became a number PER MODEL, defaulting to the
global for anything that does not name its own. Today exactly one model family names its
own: an Andrew-authored ("custom") allocation reserves 1% instead of 1.5%. S0 is validated
at 1.5% and does not move.

Two rules keep this from becoming five disagreeing numbers again:
  * the VALUES still live only in config.RISK_LIMITS, read only through the accessors
    below — no site hardcodes a percentage;
  * this module never decides WHICH model an account is on. It takes a boolean the caller
    resolved SOURCE-based (does the label have rows in the CRM custom-allocation view —
    custom_target.is_custom_allocation), never a label spelling, and never a CRM read of
    its own. That keeps this a pure leaf and keeps a CRM rename from being able to change
    an account's reserve.

The value must reach EVERY consumer, not just the sizing site. If a plan SIZES against 1%
while its drift/CASH line is MEASURED against 1.5%, the account carries a permanent 0.5%
phantom drift on its cash bucket, reads as out-of-spec forever, and churns. Hence both
compute_investable() and cash_line() take the same override, and every caller in the chain
(rebalance_engine.plan_account -> reconcile.reconcile -> cash_line) threads one value.
"""
from __future__ import annotations

import config

# Key of the per-model override in config.RISK_LIMITS. Named here so no consumer spells
# the string itself.
CUSTOM_RESERVE_KEY = "custom_allocation_cash_reserve_pct"


def buffer_pct() -> float:
    """The single standing cash-reserve buffer fraction (config.RISK_LIMITS).

    One accessor so every site that needs the buffer reads the SAME config value, and a
    future change to where/how the buffer is stored is a one-line edit here.

    This is the DEFAULT / S0 value. For a model that names its own, use
    :func:`buffer_pct_for`."""
    return config.RISK_LIMITS["cash_reserve_pct"]


def custom_buffer_pct() -> float:
    """The reserve an Andrew-authored (custom) allocation holds: 1%.

    Falls back to the global default if the key is ever removed from config, so a missing
    override degrades to today's behavior rather than to zero reserve (a 0% reserve is the
    fully-invested account that the fee deduction overdraws)."""
    return float(config.RISK_LIMITS.get(CUSTOM_RESERVE_KEY, buffer_pct()))


def buffer_pct_for(is_custom: bool = False) -> float:
    """The cash-reserve buffer for ONE model, resolved from a SOURCE-based flag.

    ``is_custom`` must come from the allocation's source (does the label have rows in the
    CRM custom-allocation view — ``custom_target.is_custom_allocation`` /
    ``custom_target.split_labels``), NEVER from the label's spelling. This module does not
    and must not read the CRM; it is a leaf over config.

    False (the default) -> the global buffer, which is S0's 1.5% and is unchanged."""
    return custom_buffer_pct() if is_custom else buffer_pct()


def compute_investable(net_liq: float, reserve: float,
                       cash_reserve_pct: float | None = None) -> float:
    """Capital the engine is allowed to deploy for one account.

    Two carve-outs, applied in this order (memory: ibkr-model-portfolio-api-limit):
      * the distribution RESERVE is removed FIRST — cash earmarked for an upcoming
        client distribution is never invested in the first place (no buy-today/
        sell-tomorrow churn);
      * the standing cash_reserve_pct buffer is then held back on what remains.

    investable = (net_liq - reserve) * (1 - cash_reserve_pct)

    Never returns a negative (a reserve larger than NetLiq -> 0 investable, not a
    negative target that would manufacture phantom sells)."""
    if cash_reserve_pct is None:
        cash_reserve_pct = buffer_pct()
    investable = (net_liq - reserve) * (1.0 - cash_reserve_pct)
    return max(investable, 0.0)


# --- Slice 3: explicit execution-side CASH bucket ------------------------------
# The CASH "holding" — the deliberate, uninvested fraction of the account.
#
# THE PROBLEM IT FIXES (execution-side only; strategy/backtester untouched): every risk
# holding is SIZED against reduced investable (NAV*(1-buffer)) but the strategy model
# weights sum to ~100% of NAV with no cash line. So a correctly-invested account holds
# slightly less of each risk asset than its raw model weight, and the book visibly does
# NOT sum to 100%. Without a cash line to absorb the buffer, the readout looks "light".
#
# The fix is purely a READOUT/MEASUREMENT line: reconcile the buffer itself as its own
# bucket. Its TARGET weight is exactly the standing buffer (what we deliberately keep in
# cash); its ACTUAL weight is the account's real uninvested cash fraction. A correctly
# invested account therefore reads ~0 drift on CASH, and the whole book (risk lines at
# their true model weights + this CASH line) sums to ~100%.
#
# This places NO order and sizes NO shares — it is downstream of sizing entirely.
CASH_SYMBOL = "CASH"


def cash_line(net_liq: float, risk_positions_value: float,
              buffer: float | None = None) -> tuple[float, float]:
    """Synthetic CASH bucket for one account: (target_weight, actual_weight).

    Pure function — no broker, no config beyond the single buffer knob.

      target_weight = the standing cash buffer (what we INTEND to keep uninvested).
                      Defaults to buffer_pct() (config.RISK_LIMITS["cash_reserve_pct"]).
      actual_weight = the account's REAL uninvested cash fraction
                    = (NetLiq - value_of_risk_positions) / NetLiq.

    `risk_positions_value` is the total market value of the account's RISK holdings
    (sum of actual_shares * price over the reconciled risk lines). Whatever NAV is not
    in risk assets is, by definition, cash.

    A NetLiq of 0 (or non-positive) yields actual_weight 0.0 rather than dividing by
    zero. actual_weight is NOT clamped negative: if the book is somehow levered past
    NetLiq (risk value > NetLiq), a negative cash fraction is the honest reading and the
    drift will flag it — better a visible breach than a hidden one."""
    if buffer is None:
        buffer = buffer_pct()
    actual = (net_liq - risk_positions_value) / net_liq if net_liq else 0.0
    return float(buffer), float(actual)
