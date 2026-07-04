"""
test_s4_runner.py — S4 single-account runner preview + daily calendar gate (offline).

Proves, with NO broker and nothing transmitted:
  * build_preview (PURE) wires preflight + sizing + guard together correctly, refusing the
    levered path on a cash account and passing an un-levered book, and marks the borrow leg.
  * s4_daily_run gates on the market calendar: NO-OP (exit 0, no delegation) on a weekend /
    holiday; delegates on a trading day; requires an account.

Run:
  cd paperbot
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s4_runner.py -q
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

import s4_daily_run
import s4_rebalance_run
from strategy_target import Target


def _target(spy_w: float):
    return Target(
        weights=pd.Series({"SPY": spy_w, "BIL": 1.0 - spy_w}, dtype="float64"),
        prices=pd.Series({"SPY": 600.0, "BIL": 91.5}, dtype="float64"),
        as_of=pd.Timestamp("2026-07-02"),
        price_date=pd.Timestamp("2026-07-02"),
        version="S4/test",
    )


def _summary(account_type, bp, xl):
    return {"AccountType": account_type, "BuyingPower": bp, "ExcessLiquidity": xl}


# --- build_preview (PURE) ----------------------------------------------------------
def test_preview_unlevered_passes_on_cash_account():
    out = s4_rebalance_run.build_preview(
        1_000_000.0, {}, _target(0.30), leverage_cap=1.5,
        summary=_summary("CASH", 0, 0))
    assert out["preflight"].ok            # un-levered allowed on cash
    assert out["verdict"].ok
    assert out["exposure"] == 0.30


def test_preview_levered_refused_on_cash_account():
    out = s4_rebalance_run.build_preview(
        1_000_000.0, {}, _target(1.30), leverage_cap=1.5,
        summary=_summary("CASH", 9_000_000, 9_000_000))
    assert not out["preflight"].ok        # levered path refused on cash account
    assert any(i.is_borrow_leg for i in out["intents"])   # borrow leg present, not dropped


def test_preview_levered_passes_on_healthy_margin():
    out = s4_rebalance_run.build_preview(
        1_000_000.0, {}, _target(1.30), leverage_cap=1.5,
        summary=_summary("REG T MARGIN", 3_000_000, 800_000))
    assert out["preflight"].ok
    assert out["verdict"].ok


def test_preview_guard_vetoes_over_cap():
    out = s4_rebalance_run.build_preview(
        1_000_000.0, {}, _target(1.80), leverage_cap=1.5,
        summary=_summary("PORTFOLIO MARGIN", 9_000_000, 9_000_000))
    assert not out["verdict"].ok


# --- daily calendar gate -----------------------------------------------------------
def test_daily_no_op_on_holiday():
    # 2026-07-03 is Independence Day (observed) — a full closure.
    run, reason = s4_daily_run.is_trading_today(dt.date(2026, 7, 3))
    assert not run and "not a trading session" in reason
    # main() must NO-OP (exit 0) without touching the runner, even with an account.
    rc = s4_daily_run.main(account="DU8922142", today=dt.date(2026, 7, 3))
    assert rc == 0


def test_daily_no_op_on_weekend():
    run, _ = s4_daily_run.is_trading_today(dt.date(2026, 7, 4))   # Saturday
    assert not run
    assert s4_daily_run.main(account="DU8922142", today=dt.date(2026, 7, 4)) == 0


def test_daily_trading_day_detected():
    run, reason = s4_daily_run.is_trading_today(dt.date(2026, 7, 2))  # Thursday, open
    assert run and "is a trading session" in reason


def test_daily_requires_account_on_trading_day():
    # On a trading day with no account -> SAFETY STOP (rc 2), never delegates/connects.
    rc = s4_daily_run.main(account=None, today=dt.date(2026, 7, 2))
    assert rc == 2
