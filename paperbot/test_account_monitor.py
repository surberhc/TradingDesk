"""
test_account_monitor.py — Slice 5: the per-account MONITOR BRAIN (pure, propose-only).

Proves `decide()` returns the right Verdict for each trigger, and ENFORCES the propose-
only boundary: the module (and its module-level imports) cannot reach any order-transmit
or arming path. SYNTHETIC fixtures only — no broker, no gateway, nothing transmitted; the
withdrawal path is exercised with a fake in-test distribution schedule (never real client
data — cashflows.SCHEDULE stays empty in committed code).

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_account_monitor.py -q
"""
from __future__ import annotations

import inspect

import pandas as pd
import pytest

import account_monitor as mon
import cashflows
import config


# --- synthetic helpers --------------------------------------------------------
def make_target(weights: dict, prices: dict, version: str = "Balanced"):
    """A strategy_target.Target from plain dicts — no backtester, no data load.
    reconcile reads only .weights (index + get) and .prices (get)."""
    import strategy_target
    return strategy_target.Target(
        weights=pd.Series(weights, dtype="float64"),
        prices=pd.Series(prices, dtype="float64"),
        as_of=pd.Timestamp("2026-06-30"),
        price_date=pd.Timestamp("2026-06-30"),
        version=version,
    )


def make_state(positions: dict, *, net_liq: float = 1_000_000.0, cash: float = 50_000.0,
               schedule: list | None = None, weights=None, prices=None,
               version: str = "Balanced") -> mon.AccountState:
    target = make_target(weights or {"SPY": 1.0}, prices or {"SPY": 100.0}, version)
    return mon.AccountState(
        account="DU0001", version=version, net_liq=net_liq, cash=cash,
        positions=positions, schedule=schedule or [], target=target)


# target shares for SPY 100% @ $100 on NetLiq 1,000,000:
#   reserve 0 -> investable 1,000,000*(1-0.015)=985,000 -> floor(985,000/100)=9850 sh.
ON_TARGET = 9850


# --- 1. HOLD: in-band, no flows, no stray positions ---------------------------
def test_hold_in_band_no_flows():
    state = make_state({"SPY": ON_TARGET}, schedule=[])
    v = mon.decide(state)
    assert v.action == "HOLD"
    assert v.reason == mon.REASON_IN_BAND
    assert v.account == "DU0001"


# --- 2. REBALANCE: a real band breach -----------------------------------------
def test_rebalance_on_band_breach():
    # Hold only 5000 of a 9850 target -> trade ~48% of NetLiq, far past the 3% band.
    state = make_state({"SPY": 5000}, schedule=[])
    v = mon.decide(state)
    assert v.action == "REBALANCE"
    assert v.reason == mon.REASON_DRIFT_BAND_BREACH
    assert v.detail["drifted_lines"] >= 1


def test_small_drift_stays_hold():
    # Hold 9800 of 9850 -> trade 0.5% of NetLiq, inside the band -> HOLD, not REBALANCE.
    state = make_state({"SPY": 9800}, schedule=[])
    assert mon.decide(state).action == "HOLD"


# --- 3. ALERT: an upcoming distribution that available cash does not cover -----
def test_alert_withdrawal_due_unreserved():
    # SYNTHETIC schedule: $20,000/mo distribution; RESERVE_MONTHS(=1) -> reserve $20,000.
    # Account cash is only $5,000 -> cannot cover the upcoming distribution -> ALERT.
    sched = [cashflows.Flow("distribution", amount=20_000.0, pct_nav=0.0, day=1,
                            note="SYNTHETIC test flow")]
    state = make_state({"SPY": ON_TARGET}, cash=5_000.0, schedule=sched)
    v = mon.decide(state)
    assert v.action == "ALERT"
    assert v.reason == mon.REASON_WITHDRAWAL_DUE_UNRESERVED
    assert v.detail["shortfall"] == pytest.approx(15_000.0)


def test_withdrawal_covered_is_not_alerted():
    # Same $20k reserve, but cash $25,000 covers it -> no withdrawal ALERT (HOLD here).
    sched = [cashflows.Flow("distribution", amount=20_000.0, pct_nav=0.0, day=1,
                            note="SYNTHETIC test flow")]
    state = make_state({"SPY": ON_TARGET}, cash=25_000.0, schedule=sched)
    assert mon.decide(state).action == "HOLD"


def test_contribution_only_schedule_needs_no_reserve():
    # A contribution (cash IN) requires no reserve, so low cash does NOT alert.
    sched = [cashflows.Flow("contribution", amount=20_000.0, pct_nav=0.0, day=15,
                            note="SYNTHETIC test flow")]
    state = make_state({"SPY": ON_TARGET}, cash=1.0, schedule=sched)
    assert mon.decide(state).action == "HOLD"


# --- 4. ALERT: an untracked / unknown held position ---------------------------
def test_alert_untracked_position():
    # Hold the SPY target plus a stray GOOG not in the model -> ALERT (untracked), and the
    # untracked symbol is reported. (No distribution schedule, so it isn't the cash alert.)
    state = make_state(
        {"SPY": ON_TARGET, "GOOG": 10},
        weights={"SPY": 1.0}, prices={"SPY": 100.0, "GOOG": 150.0}, schedule=[])
    v = mon.decide(state)
    assert v.action == "ALERT"
    assert v.reason == mon.REASON_UNTRACKED_POSITION
    assert "GOOG" in v.detail["symbols"]


def test_withdrawal_alert_takes_precedence_over_rebalance():
    # Both a band breach AND an uncovered withdrawal -> the liquidity ALERT wins (cash
    # earmarked for a client must surface before any rebalance proposal).
    sched = [cashflows.Flow("distribution", amount=20_000.0, pct_nav=0.0, day=1,
                            note="SYNTHETIC test flow")]
    state = make_state({"SPY": 5000}, cash=1_000.0, schedule=sched)
    v = mon.decide(state)
    assert v.action == "ALERT"
    assert v.reason == mon.REASON_WITHDRAWAL_DUE_UNRESERVED


# --- 5. Verdict is immutable (propose-only data, can't be mutated post-decision) ---
def test_verdict_is_frozen():
    v = mon.decide(make_state({"SPY": ON_TARGET}))
    with pytest.raises(Exception):
        v.action = "REBALANCE"   # frozen dataclass -> FrozenInstanceError


# --- 6. PROPOSE-ONLY BOUNDARY: the module cannot reach a transmit/arm path -----
# Symbols that, if reachable from account_monitor's namespace, would mean it could touch a
# broker / build / transmit / arm an order. The monitor must compose only PURE pieces.
_FORBIDDEN_MODULES = {
    "order_router",          # build / build_fa_block / transmit_guard live here
    "execution_engine",      # the arming + transmit driver
    "live_quotes",           # live broker quotes -> a broker session
    "ibkr",                  # connections.ibkr — the gateway/broker connection
    "ib_async", "ib_insync",
}
_FORBIDDEN_CALLABLE_NAMES = {
    "transmit", "build", "build_fa_block", "transmit_guard", "arm", "place_order",
    "placeOrder", "connect",
}


def test_module_does_not_import_any_transmit_path():
    """account_monitor's module namespace must not contain a transmit-capable module or
    a transmit/arm/connect callable — directly OR via a re-export."""
    ns = vars(mon)
    for name, obj in ns.items():
        if name.startswith("__"):
            continue
        # No forbidden MODULE bound in the namespace.
        modname = getattr(obj, "__name__", None)
        if inspect.ismodule(obj):
            assert modname not in _FORBIDDEN_MODULES, (
                f"account_monitor imports forbidden module {modname!r}")
        # No transmit/arm/connect CALLABLE bound by name.
        if callable(obj):
            assert name not in _FORBIDDEN_CALLABLE_NAMES, (
                f"account_monitor exposes forbidden callable {name!r}")


def test_source_does_not_reference_transmit_symbols():
    """Belt-and-suspenders: the SOURCE text imports none of the transmit modules. Catches a
    forbidden import even if it were aliased so the namespace check missed it."""
    src = inspect.getsource(mon)
    for bad in ("import order_router", "import execution_engine", "import live_quotes",
                "from connections import", "import ib_async", "import ib_insync"):
        assert bad not in src, f"account_monitor source references a transmit path: {bad!r}"


def test_transitive_imports_stay_pure():
    """The modules account_monitor DOES import (cashflows, config, rebalance_engine,
    reconcile, strategy_target) are the same pure pieces the read-only recon path uses.
    Assert none of account_monitor's own imported modules is a forbidden transmit module."""
    imported = {getattr(v, "__name__", None)
                for v in vars(mon).values() if inspect.ismodule(v)}
    assert imported & _FORBIDDEN_MODULES == set(), (
        f"account_monitor reached a transmit module transitively: "
        f"{imported & _FORBIDDEN_MODULES}")
