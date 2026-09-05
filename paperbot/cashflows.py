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
# entry has no scheduled flows (reserve = 0). Amounts are PER OCCURRENCE.
#
# Populated 2026-09-05 from the CRM's IBKR-configured recurring withdrawal instructions
# (accounts.recurring_transactions, ingested nightly by ibkr-flex-sync -- see
# SYSTEM_MAP.md and paperbot/crm_cashflows.py). These are the 9 accounts with an ACTIVE,
# MONTHLY, USD withdrawal instruction as of that date, per Andrew's explicit go-ahead to
# merge them. crm_cashflows.build_draft() also derived 35 deposit-only accounts that same
# run -- deliberately NOT merged here (a contribution adds no reserve; see
# cashflows.reserve_for) -- and correctly excluded 2 accounts whose instruction had already
# expired. This dict does NOT auto-update: re-run `python crm_cashflows.py` periodically
# (or when a new withdrawal client is onboarded) and merge any changes by hand, the same
# deliberate way this batch was added.
SCHEDULE: dict[str, list[Flow]] = {
    "U10555316": [Flow("distribution", amount=8500.0, pct_nav=0.0, day=15,
                       note="CRM recurring_transactions (ACH, since 2025-06-15)")],
    "U13221397": [Flow("distribution", amount=5359.57, pct_nav=0.0, day=24,
                       note="CRM recurring_transactions (ACH, since 2026-04-24)"),
                  Flow("distribution", amount=5359.57, pct_nav=0.0, day=16,
                       note="CRM recurring_transactions (ACH, since 2026-05-16)")],
    "U15715611": [Flow("distribution", amount=1765.0, pct_nav=0.0, day=12,
                       note="CRM recurring_transactions (ACH, since 2025-02-12)")],
    "U22011673": [Flow("distribution", amount=1000.0, pct_nav=0.0, day=3,
                       note="CRM recurring_transactions (ACH, since 2026-08-03)")],
    "U22848377": [Flow("distribution", amount=3846.0, pct_nav=0.0, day=24,
                       note="CRM recurring_transactions (ACH, since 2026-09-24)")],
    "U7349619": [Flow("distribution", amount=5000.0, pct_nav=0.0, day=15,
                      note="CRM recurring_transactions (ACH, since 2024-09-15)")],
    "U7349974": [Flow("distribution", amount=2500.0, pct_nav=0.0, day=27,
                      note="CRM recurring_transactions (ACH, since 2023-01-27)")],
    "U7355827": [Flow("distribution", amount=2941.18, pct_nav=0.0, day=5,
                      note="CRM recurring_transactions (ACH, since 2022-11-05)")],
    "U8147914": [Flow("distribution", amount=2000.0, pct_nav=0.0, day=23,
                      note="CRM recurring_transactions (ACH, since 2026-09-23)")],
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
