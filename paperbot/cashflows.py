"""
cashflows.py — scheduled client contributions & distributions, and the liquidity
RESERVE the rebalancer must hold so a distribution is never funded by a fire sale.

Integrated by design (NOT a separate program): the engine subtracts reserve_for()
from each account's investable capital BEFORE sizing any buy, so cash earmarked for
an upcoming distribution is never invested in the first place — no buy-today /
sell-tomorrow churn. Because the All-Weather model already holds a cash-equivalent
sleeve (SGOV / T-bills / floating-rate), that reserve naturally lives in a yield-
bearing holding rather than sitting idle; only the part of a client's distribution
that exceeds the model's natural cash sleeve is a deliberate, consistent, documented
extra-cash deviation for that account.

This module owns the SCHEDULE (data you maintain) and the reserve POLICY. It places
no orders and touches no broker.
"""
from __future__ import annotations

from dataclasses import dataclass

# How many months of an upcoming distribution to hold liquid at all times. 1 month is
# the never-caught-short minimum for regular monthly distributions; 2 months is now the
# deliberate default (Andrew's call, 2026-09-04) to hold a thicker buffer so fewer
# rebalance trades are forced to fund a distribution and less gets paid in fees, at the
# cost of some idle cash. A future "ramp" (reserve grows only as the date approaches)
# remains a possible later refinement.
RESERVE_MONTHS = 2


@dataclass(frozen=True)
class Flow:
    """One recurring monthly cash flow for a client account."""
    kind: str          # "distribution" (cash OUT) | "contribution" (cash IN)
    amount: float      # fixed dollars per occurrence (use 0.0 if using pct_nav)
    pct_nav: float     # OR fraction of NAV per occurrence (0.0 if using fixed amount)
    day: int           # day-of-month the flow occurs (1-28 recommended)
    note: str = ""


# Per-account monthly flows. EDIT with each client's real schedule. An account with no
# entry has no scheduled flows (reserve = 0). Amounts are PER OCCURRENCE. Placeholders
# are commented out so nothing is assumed until you fill in real numbers.
SCHEDULE: dict[str, list[Flow]] = {
    # "DU8922142": [Flow("distribution", amount=2500.0, pct_nav=0.0, day=1,  note="monthly income")],
    # "DU8922145": [Flow("contribution", amount=1000.0, pct_nav=0.0, day=15, note="monthly add")],
}


def _occurrence_amount(flow: Flow, nav: float) -> float:
    """Dollar size of one occurrence (pct flows resolve against current NAV)."""
    return flow.pct_nav * nav if flow.pct_nav > 0 else flow.amount


def reserve_for(account: str, nav: float) -> float:
    """Dollars to hold liquid for this account = RESERVE_MONTHS of upcoming
    distributions. Contributions add nothing (incoming cash needs no reserve)."""
    flows = SCHEDULE.get(account, [])
    monthly_dist = sum(_occurrence_amount(f, nav) for f in flows if f.kind == "distribution")
    return RESERVE_MONTHS * monthly_dist


def monthly_net_flow(account: str, nav: float) -> float:
    """Signed monthly cash flow for reporting: contributions +, distributions −."""
    total = 0.0
    for f in SCHEDULE.get(account, []):
        amt = _occurrence_amount(f, nav)
        total += amt if f.kind == "contribution" else -amt
    return total


def describe(account: str, nav: float) -> str:
    """One-line human summary of an account's schedule (for the report)."""
    flows = SCHEDULE.get(account, [])
    if not flows:
        return "no scheduled flows"
    parts = []
    for f in flows:
        sign = "+" if f.kind == "contribution" else "−"
        parts.append(f"{sign}${_occurrence_amount(f, nav):,.0f}/mo (day {f.day})")
    return ", ".join(parts)
