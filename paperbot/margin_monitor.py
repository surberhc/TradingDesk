"""
margin_monitor.py — per-RUN buying-power / margin observability around the ARMED
order-transmit path (conductor #26).

WHY
---
s4_risk.margin_preflight() computes the account's margin posture BEFORE a levered run,
but its result was only PRINTED — never persisted — so after the fact there was no
durable record of what buying power / margin the account actually had when an order
transmitted, nor of how a run moved it. This module closes that gap: it snapshots the
account's accountSummary immediately BEFORE the first transmit and again AFTER the
fill-watch settles, diffs the two, and writes a durable kind="margin_impact" record to
the audit ledger (runs.jsonl) so per-run margin impact is auditable later.

DESIGN (deliberate, do not change)
----------------------------------
* Mirrors s4_risk: a PURE core (snapshot_from_summary / delta / to_record) that is
  testable offline, plus ONE thin broker driver (read_snapshot) that does the single
  accountSummary read. It READS ONLY — it connects nothing, transmits nothing, and
  touches no frozen module.
* Capture is PER-RUN, NOT per-single-order: one accountSummary snapshot before the run's
  first transmit and one after the fill-watch, NOT a round-trip per leg. A per-leg
  accountSummary would add latency to every order and race the fills it is trying to
  measure. One run = one before/after pair.
* Fully FAIL-SOFT: every capture is guarded so any summary-read or ledger error degrades
  to margin=None and NEVER blocks, delays, or alters an order. This is observability
  bolted onto the side of the order path — it can only add a record, never change a trade.
"""
from __future__ import annotations

from dataclasses import dataclass


# --- pure summary helpers (mirror s4_risk._summary_map / _num) ----------------------
def _summary_map(summary) -> dict:
    """Accept either a {tag: value} dict or an ib_async accountSummary() list of rows
    (each with .tag and .value) and return {tag: value_str}. Self-contained so this
    module has no dependency on s4_risk."""
    if isinstance(summary, dict):
        return {str(k): str(v) for k, v in summary.items()}
    out: dict[str, str] = {}
    for row in summary:
        tag = getattr(row, "tag", None)
        val = getattr(row, "value", None)
        if tag is not None:
            out[str(tag)] = str(val)
    return out


def _num(m: dict, tag: str):
    """Numeric value of a tag, distinguishing ABSENT from present-but-zero:
      * tag NOT in the map        -> None   (we simply didn't read it)
      * tag present, parses       -> float  (INCLUDING "0" -> 0.0)
      * tag present, unparseable  -> None
    The absent-vs-zero distinction matters: a real BuyingPower of 0 is a meaningful,
    loggable fact, not a missing read."""
    if tag not in m:
        return None
    try:
        return float(m[tag])
    except (TypeError, ValueError):
        return None


@dataclass
class MarginSnapshot:
    account: str
    account_type: str
    net_liq: float | None
    buying_power: float | None
    excess_liquidity: float | None
    init_margin: float | None
    maint_margin: float | None
    available_funds: float | None

    def as_dict(self) -> dict:
        return {
            "account": self.account,
            "account_type": self.account_type,
            "net_liq": self.net_liq,
            "buying_power": self.buying_power,
            "excess_liquidity": self.excess_liquidity,
            "init_margin": self.init_margin,
            "maint_margin": self.maint_margin,
            "available_funds": self.available_funds,
        }


# The numeric fields we diff between the before/after snapshots.
_NUMERIC_FIELDS = ("net_liq", "buying_power", "excess_liquidity",
                   "init_margin", "maint_margin", "available_funds")


def snapshot_from_summary(summary, account: str) -> MarginSnapshot:
    """PURE: build a MarginSnapshot from an accountSummary (dict OR ib_async row list).
    Tags read: AccountType, NetLiquidation, BuyingPower, ExcessLiquidity, InitMarginReq,
    MaintMarginReq, AvailableFunds. A missing numeric tag stays None (absent != 0)."""
    m = _summary_map(summary)
    return MarginSnapshot(
        account=account,
        account_type=m.get("AccountType", ""),
        net_liq=_num(m, "NetLiquidation"),
        buying_power=_num(m, "BuyingPower"),
        excess_liquidity=_num(m, "ExcessLiquidity"),
        init_margin=_num(m, "InitMarginReq"),
        maint_margin=_num(m, "MaintMarginReq"),
        available_funds=_num(m, "AvailableFunds"),
    )


def read_snapshot(ib, account: str):
    """THE only broker touch: read the account's live accountSummary (the same
    ib.accountSummary(account) call s4_rebalance_run.py:185 uses) and build a snapshot.

    Fully fail-soft: ANY exception (no connection, timeout, bad account) -> None, so a
    failed read can never block or alter the order path around it. Returns a MarginSnapshot
    or None."""
    try:
        summary = ib.accountSummary(account)
        return snapshot_from_summary(summary, account)
    except Exception:
        return None


def delta(before, after) -> dict:
    """Per-field numeric change after-before. Returns {} if EITHER snapshot is None.
    For each numeric field, "<field>_delta" = after.x - before.x when both are not None,
    else None (can't diff a field we didn't read on one side)."""
    if before is None or after is None:
        return {}
    out: dict = {}
    for f in _NUMERIC_FIELDS:
        b = getattr(before, f)
        a = getattr(after, f)
        out[f"{f}_delta"] = (a - b) if (b is not None and a is not None) else None
    return out


def to_record(before, after, *, account: str, context: str = "") -> dict:
    """PURE: assemble the before/after/delta record (the value returned as result["margin"]
    and the body of the persisted ledger record). before/after may be None (fail-soft)."""
    return {
        "account": account,
        "context": context,
        "before": before.as_dict() if before else None,
        "after": after.as_dict() if after else None,
        "delta": delta(before, after),
    }


def record_impact(before, after, *, account: str, context: str = ""):
    """Persist a kind="margin_impact" record to the audit ledger (runs.jsonl). Returns the
    ledger path, or None if nothing was written.

    Skips (returns None) when BOTH snapshots are None — nothing observed, nothing to log.
    ledger is imported at LEAF level (like order_router's transmit_journal import) to keep
    the import graph acyclic. The WHOLE body is guarded: any failure -> None, never raises
    into the order path."""
    try:
        if before is None and after is None:
            return None
        import ledger
        record = {"kind": "margin_impact",
                  **to_record(before, after, account=account, context=context)}
        return ledger.record_run(record)
    except Exception:
        return None
