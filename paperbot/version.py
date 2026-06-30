"""
version.py — single source of truth for the paperbot system version + change log.

Compliance/traceability: every run STAMPS this version (plus the config knobs and the
per-account model as_of) into its report header and — once order routing exists — into
each audit-ledger record, so any trade decision can be tied back to the exact logic and
settings that produced it. Bump VERSION whenever behavior that can change an order
changes, and add a CHANGELOG line saying WHAT changed and WHY.

This is the lightweight in-code marker. It is NOT a substitute for source-control
history (git) — it complements it. The CHANGELOG here is the human-readable summary;
the repo (when initialized) is the authoritative line-by-line record.
"""
from __future__ import annotations

# 0.MAJOR.MINOR while pre-trading. Bump on ANY change that can affect a generated order
# (strategy wiring, sizing, reserve/band logic, allocation, routing).
VERSION = "0.7.0"

# Newest first. Keep terse; for an examiner the "why" matters as much as the "what".
CHANGELOG = [
    ("0.7.0", "2026-06-30", "Cash buffer 5%->1.5% via single config knob "
                            "(cash_reserve_pct=0.015); risk positions now size ~3.5% of "
                            "NAV closer to model. Order-affecting."),
    ("0.6.0", "2026-06-30", "Consolidated the investable/buffer math into a single leaf "
                            "module investable.py (compute_investable + buffer_pct). The "
                            "5 sites that re-derived (NetLiq-reserve)*(1-cash_reserve) "
                            "inline — rebalance_engine, reconcile, recon_report, "
                            "execution_engine, and the risk_manager reserve threshold — "
                            "now share that one function (rebalance_engine.compute_investable "
                            "kept as a thin re-export). Buffer stays 0.05; proven "
                            "behavior-identical (full paperbot suite green, +8 "
                            "characterization tests pinning the math at 0.05). Slice 1 of "
                            "the account-cashflow build: consolidation only, NO behavior "
                            "change."),
    ("0.5.0", "2026-06-27", "Pure offline multi-account block REBALANCE ENGINE "
                            "(rebalance_engine.py): per-account reserve carve-out + "
                            "explicit integer target shares + per-holding no-trade band; "
                            "same-tier/symbol/side block aggregation with per-account "
                            "ContractsOrShares split (sums to block qty); single-account "
                            "true-up falls back to a DIRECT order. Emits order_router "
                            "inputs as a transmit-free RoutePlan with fa_method='' (the "
                            "group's ContractsOrShares governs; faMethod='NetLiq' is "
                            "rejected, Err 10226). 14 pytest unit tests, all green. No "
                            "broker contact, no order object built, nothing transmitted."),
    ("0.4.0", "2026-06-26", "FA block-order plumbing: read-only FA-config probe "
                            "(fa_probe.py), order_router.build_fa_block(), and a "
                            "what-if-only block validator (fa_block_test.py). No "
                            "transmission, no config writes."),
    ("0.3.0", "2026-06-26", "Multi-account reconciliation + drift report; distribution "
                            "reserve (cashflows); no-trade band; block aggregation with "
                            "per-account allocation split. Read-only."),
    ("0.2.0", "2026-06-26", "Multi-account discovery (accounts.py) + per-tier enrollment "
                            "map. Read-only."),
    ("0.1.0", "2026-06-26", "Single-account dry-run engine: target / diff / risk / build. "
                            "Read-only, transmits nothing."),
]


def banner() -> str:
    """One-line version marker for report headers and logs."""
    return f"paperbot v{VERSION}"


def stamp() -> dict:
    """Version fields to merge into an audit-ledger record (layer 2 of traceability)."""
    return {"paperbot_version": VERSION}
