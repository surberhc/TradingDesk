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
VERSION = "0.4.0"

# Newest first. Keep terse; for an examiner the "why" matters as much as the "what".
CHANGELOG = [
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
