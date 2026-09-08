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

This module owns the SCHEDULE and the reserve POLICY. It places no orders and touches
no broker.

THE SCHEDULE IS LIVE, NOT HAND-MAINTAINED (changed 2026-09-08). It used to be a dict
typed in by hand, which meant a newly-onboarded withdrawal client was silently absent
from every reserve check until someone remembered to refresh it. SCHEDULE now reads the
client system (the CRM) through crm_cashflows -- the one existing derivation of IBKR's
own configured recurring instructions, reused, not re-queried. Read-only, lazily on
first use, cached for the life of the process. If that read fails, or comes back with
no withdrawal instructions at all, every access raises ScheduleUnavailable: an
unreadable schedule must never be mistaken for "no client takes a withdrawal".
"""
from __future__ import annotations

from collections.abc import Mapping
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


class ScheduleUnavailable(RuntimeError):
    """The withdrawal schedule could not be read from the client system. Raised INSTEAD of
    returning an empty schedule, because an empty schedule would make every reserve check
    conclude that no client takes a withdrawal and finish clean."""


def load_schedule() -> dict[str, list[Flow]]:
    """Read every account's recurring monthly flows from the client system (the CRM).

    Delegates to crm_cashflows, which is the single derivation of IBKR's own configured
    recurring instructions (accounts.recurring_transactions) -- this is not a second query.
    Read-only. Raises ScheduleUnavailable rather than ever returning nothing."""
    import crm_cashflows  # noqa: PLC0415 — lazy: importing cashflows must do no I/O

    try:
        draft = crm_cashflows.build_draft()
    except crm_cashflows.CrmCashflowsUnavailable as exc:
        raise ScheduleUnavailable(
            f"the scheduled-withdrawal list could not be read from the client system: {exc}"
        ) from exc
    schedule = draft.schedule
    if not any(f.kind == "distribution" for flows in schedule.values() for f in flows):
        raise ScheduleUnavailable(
            "the client system returned no active monthly withdrawal instruction for any "
            "account at all. That is being treated as a failed read, not as a real answer, "
            "because every client who takes a scheduled withdrawal would otherwise go "
            "unchecked without anyone being told.")
    return schedule


class _LiveSchedule(Mapping):
    """Per-account monthly flows, read from the client system on first use and cached for
    the life of the process. Behaves exactly like the dict it replaced (`.get`, `.items`,
    `in`, iteration), so no caller had to change. Reading is LAZY so importing this module
    never touches the network, and every read either yields the client system's real
    records or raises ScheduleUnavailable -- never a quiet empty answer."""

    def __init__(self) -> None:
        self._loaded: dict | None = None

    def _data(self) -> dict:
        if self._loaded is None:
            self._loaded = load_schedule()
        return self._loaded

    def __getitem__(self, account):
        return self._data()[account]

    def __iter__(self):
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())

    def __repr__(self) -> str:
        state = "not read yet" if self._loaded is None else f"{len(self._loaded)} accounts"
        return f"<live withdrawal schedule from the client system ({state})>"


SCHEDULE = _LiveSchedule()


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
