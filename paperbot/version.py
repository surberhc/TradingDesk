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
VERSION = "0.14.0"

# Newest first. Keep terse; for an examiner the "why" matters as much as the "what".
CHANGELOG = [
    ("0.14.0", "2026-07-09", "Automated nightly-monitor + morning-execute PILOT for "
                             "S0 (Andrew-approved, deliberate exception to the normally "
                             "sacred manual review->arm->transmit gate, discussed and "
                             "scoped in advance): nightly_monitor_run.py (~9:15 PM CT, "
                             "after EodReport) does one bounded-retry (3 attempts / "
                             "~10 min cap, always try/finally-disconnected) gateway "
                             "touch that folds in the old AccountMonitorDaily drift/"
                             "cashflow check, computes S0's current per-tier target, "
                             "and -- only if some enrolled account's plan needs "
                             "rebalancing (rebalance_engine's own band-breach test) AND "
                             "the new rebalance_guard.py passes (ticker allow-list "
                             "against strategies.config.ALL_TICKERS, a named "
                             "MAX_SINGLE_ACCOUNT_TURNOVER_PCT_NAV=0.50 notional cap per "
                             "account, and a regime cross-check against the exact "
                             "market_health_score()/apply_hysteresis() call eod_report's "
                             "9PM email uses) -- STAGES the route list as JSON under "
                             "C:\\TradingDesk-Local\\pending_trades\\YYYY-MM-DD.json. A "
                             "guard failure alerts by email and stages nothing. "
                             "morning_execute_run.py (~8:50 AM CT) makes ZERO gateway "
                             "contact on the common no-staged-file day; otherwise "
                             "checks a kill-switch sentinel "
                             "(C:\\TradingDesk-Local\\AUTOTRADE_DISABLED), re-runs the "
                             "guard defensively against fresh prices/regime (never "
                             "trusts the staged file blindly), and -- because "
                             "PILOT_MODE=True (the hardcoded default; only Andrew may "
                             "flip it to False after reviewing pilot cycles) -- logs and "
                             "emails 'WOULD HAVE TRANSMITTED: ...' instead of calling "
                             "any order-placing code, then archives the staged file. "
                             "The PILOT_MODE=False path (not exercised by this release) "
                             "reuses rebalance_execute.py's own arm/build/place/ladder "
                             "functions verbatim rather than reimplementing them. New "
                             "clientIds 46 (paperbot_nightly_monitor) / 47 "
                             "(paperbot_morning_execute). Existing manual "
                             "rebalance_run.py / rebalance_execute.py CLIs are "
                             "untouched and remain the human-driven path. Two new "
                             "scheduled tasks (nightly monitor retimed onto the "
                             "AccountMonitorDaily slot at 9:15 PM CT; a new "
                             "MorningExecuteDaily task at 8:50 AM CT). Order-affecting: "
                             "introduces a real, if pilot-gated, path to unattended "
                             "paper trade staging; actual transmission remains fully "
                             "gated off by PILOT_MODE=True until a deliberate future "
                             "flip."),
    ("0.13.1", "2026-07-09", "Fixed NaN-as-bearish bug in S0 regime scoring: a "
                             "missing daily price (Tiingo hasn't published yet) "
                             "previously read as a false bearish trend/vol/duration "
                             "break via bare pandas `>` comparisons (NaN>NaN/NaN>0 "
                             "== False, not NaN) instead of unknown/hold-prior-"
                             "state, and pct_change's legacy pad-fill silently "
                             "turned a missing print into a fake flat 0% return. "
                             "Fixed in 4 places: regime.py's trend component "
                             "(_ratio_above_trend, _trend_component's "
                             "trend_above_10m/trend_ret_6m_pos, and the SPY-"
                             "realized-vol stress fallback), regime.py's "
                             "_rolling_slope_positive boolean cast (the docstring's "
                             "NaN-safety promise was undone by the final `> 0` "
                             "cast), volatility.py's realized_vol (was silently "
                             "reading a data-gap night as falsely calm, nudging "
                             "toward MORE equity exposure), and duration.py's "
                             "_above_ma (feeds the live Duration Filter's long-"
                             "Treasury permission/ban rules; now NaN-aware with an "
                             "explicit conservative .fillna(False) at each call "
                             "site, matching gates.is_above's existing convention "
                             "for permission gates). Also fixed eod_report.py: "
                             "'Data as-of' now reports the OLDEST last-real-value "
                             "date across S0's required inputs instead of the "
                             "newest date ANY ticker reached (was able to read "
                             "'fresh' on a night a required ticker's row was NaN), "
                             "and s0_regime's status now inherits s0_data's status "
                             "when it's worse (ok<stale<warn<fail) so the two "
                             "sections can no longer disagree with s0_regime "
                             "looking healthier. Order-affecting: changes S0's "
                             "regime/duration computation on any date with a "
                             "missing input price. Byte-identical when all inputs "
                             "are present (pure NaN-handling fix); backtester + "
                             "paperbot both pick this up automatically via the "
                             "shared strategies package, no separate paperbot "
                             "change needed."),
    ("0.13.0", "2026-07-04", "S4 (SPX vol-control fund) single-account daily REVIEW path, "
                             "shelf-ready. New S4-only siblings (frozen S0 paths untouched): "
                             "s4_strategy_target (shared-brain adapter -> Target, profile "
                             "runtime dial + stale-data guard), s4_sizing (REAL-margin "
                             "leverage sizing: SPY notional = NAV*exposure, exposure may "
                             "exceed 1.0x, borrow leg carried NOT dropped), s4_risk (guard "
                             "permits up to the profile leverage_cap + fail-closed margin "
                             "preflight refusing the >1.0 path on a cash/insufficient-BP "
                             "account), s4_rebalance_run (single-account, gateway-locked, "
                             "place(armed=False)), s4_daily_run (calendar-gated entry point). "
                             "clientIds 44 (paperbot_s4) + 45 (paperbot_s4_exec, reserved). "
                             "Order-affecting: introduces the levered S4 sizing/risk path. "
                             "Transmits nothing; no scheduled task registered; not armed."),
    ("0.12.0", "2026-06-30", "Rebalance run/execute acquire the gateway lock for the whole "
                             "session and wait-then-refuse (naming the holder) if it's held; "
                             "monitor skips on busy. Single-process gateway interlock now "
                             "active (clientIds + lock)."),
    ("0.11.0", "2026-06-30", "Withdrawal earmark fence + sale-raised nudge in monitor core; "
                             "live read-only monitor shell (account_monitor_run.py, clientId "
                             "40) reads SettledCashByDate/fills, persists baselines, runs "
                             "decide(), prints propose-only verdicts. No scheduler yet (held)."),
    ("0.10.0", "2026-06-30", "Deposit-detection core (pure): SettledCashByDate baseline + "
                             "executions cross-check distinguishes external deposit from "
                             "sale-raised/dividend; over-trading guards + per-day debounce; "
                             "emits propose-only DEPOSIT_ARRIVED. No broker connection / no "
                             "scheduler yet (Slice 6b)."),
    ("0.9.0", "2026-06-30", "Per-account monitor brain (account_monitor.py): pure "
                            "Verdict/decide() core — HOLD/REBALANCE/ALERT, propose-only, "
                            "transmits nothing. Withdrawal-coverage + drift + "
                            "untracked-position verdicts; deposit detection deferred to "
                            "Slice 6. Real client schedule data not included (SCHEDULE "
                            "stays empty)."),
    ("0.8.0", "2026-06-30", "Explicit execution-side CASH bucket (~1.5%); risk lines "
                            "reconcile against true model weights; phantom drift removed. "
                            "Backtester untouched; order sizing unchanged."),
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
