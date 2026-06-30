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
"""
from __future__ import annotations

import config


def buffer_pct() -> float:
    """The single standing cash-reserve buffer fraction (config.RISK_LIMITS).

    One accessor so every site that needs the buffer reads the SAME config value, and a
    future change to where/how the buffer is stored is a one-line edit here."""
    return config.RISK_LIMITS["cash_reserve_pct"]


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
