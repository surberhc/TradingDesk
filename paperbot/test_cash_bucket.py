"""
test_cash_bucket.py — Slice 3: explicit execution-side CASH bucket + honest drift.

The bug Slice 3 fixes (READOUT/MEASUREMENT only — execution side; strategy/backtester
untouched): every risk holding is SIZED against reduced investable (NAV*(1-buffer)) but
the model weights sum to ~100% with no cash line, so a correctly-invested account looked
"~buffer% light" on the book and a phantom drift flag appeared. Slice 3 reconciles each
risk line against its TRUE model weight (no haircut, unchanged) AND adds a synthetic CASH
line whose target is the standing buffer and whose actual is the real uninvested cash
fraction. A correctly-invested account then reads MATCHED on every risk line AND on CASH,
and the book sums to ~100%.

HARD GUARDRAIL proven here: the CASH line places no order and the risk-line DELTAS are
byte-for-byte the Slice-2 deltas — adding the bucket moves ZERO target share counts.

SYNTHETIC inputs only — no broker, no gateway, nothing transmitted.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_cash_bucket.py -q
"""
from __future__ import annotations

import pandas as pd
import pytest

import config
import investable
import rebalance_engine as eng
import reconcile
import strategy_target


CASH_RESERVE = config.RISK_LIMITS["cash_reserve_pct"]   # 0.015 (Slice 2 re-base)


def make_target(weights: dict, prices: dict, version: str = "Balanced") -> strategy_target.Target:
    return strategy_target.Target(
        weights=pd.Series(weights, dtype="float64"),
        prices=pd.Series(prices, dtype="float64"),
        as_of=pd.Timestamp("2026-06-26"),
        price_date=pd.Timestamp("2026-06-26"),
        version=version,
    )


def _fully_invested_positions(target, nav):
    """The EXACT integer share count Slice-2 sizing wants for each holding, given an
    account holding nothing earmarked but the standing buffer."""
    inv = investable.compute_investable(nav, 0.0)
    out = {}
    for sym, w in target.weights.items():
        price = float(target.prices[sym])
        out[sym] = int(float(w) * inv / price)
    return out


# --- 1. the synthetic CASH-line helper is pure and correct ---------------------
def test_cash_line_target_is_the_buffer():
    tgt, _ = investable.cash_line(1_000_000, 985_000)
    assert tgt == CASH_RESERVE                      # target = standing buffer (1.5%)


def test_cash_line_actual_is_uninvested_fraction():
    # NetLiq 1,000,000, risk positions worth 985,000 -> 15,000 cash -> 1.5%.
    _, act = investable.cash_line(1_000_000, 985_000)
    assert act == pytest.approx(0.015)


def test_cash_line_zero_netliq_does_not_divide_by_zero():
    tgt, act = investable.cash_line(0.0, 0.0)
    assert tgt == CASH_RESERVE and act == 0.0


def test_cash_line_levered_book_reads_negative_cash():
    # risk value > NetLiq (levered) -> honest NEGATIVE cash fraction, not clamped.
    _, act = investable.cash_line(1_000_000, 1_100_000)
    assert act == pytest.approx(-0.10)


# --- 2. THE FIX: a correctly-invested account reads MATCHED everywhere ----------
def test_correctly_invested_account_matched_on_every_risk_line_and_cash():
    # Realistic multi-holding book (4 x 25% @ $100). Hold EXACTLY the Slice-2 target shares.
    # Each risk line's drift is -0.015*weight = -0.38% (inside the 1% tolerance) -> MATCHED,
    # and the CASH line carries the ~1.5% buffer at ~0 drift -> MATCHED. Phantom drift GONE.
    nav = 1_000_000.0
    target = make_target({"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.25},
                         {"A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0})
    positions = _fully_invested_positions(target, nav)   # the correct, Slice-2 holdings
    inv = investable.compute_investable(nav, 0.0)
    lines = reconcile.reconcile(target, nav, positions, tolerance_w=0.01, investable=inv)

    risk = [ln for ln in lines if ln.symbol != investable.CASH_SYMBOL]
    cash = next(ln for ln in lines if ln.symbol == investable.CASH_SYMBOL)

    # every RISK line matched (no phantom under-weight flag)
    assert all(ln.status == "MATCHED" for ln in risk), [(l.symbol, l.status) for l in risk]
    # the synthetic CASH line matched too (actual cash ~= target buffer)
    assert cash.status == "MATCHED"
    assert cash.target_weight == pytest.approx(CASH_RESERVE)
    assert cash.actual_weight == pytest.approx(0.0152, abs=1e-3)
    assert abs(cash.drift_weight) <= 0.01


def test_book_sums_to_about_one_hundred_percent_including_cash():
    # With the CASH bucket the ACTUAL book (risk holdings + real cash) sums to EXACTLY
    # 100% of NAV — that is the honest, identity-true readout (every dollar is either in a
    # risk asset or in cash). The TARGET column sums to 100% + buffer because the strategy
    # model weights sum to 100% with no cash line and the CASH target ADDS the standing
    # buffer on top: that buffer is precisely the deliberate uninvested slice the risk
    # lines were sized to leave behind. Both are correct; the actual-side identity is the
    # one that makes "the book sums to 100%" literally true.
    nav = 1_000_000.0
    target = make_target({"A": 0.6, "B": 0.4}, {"A": 200.0, "B": 100.0})
    positions = _fully_invested_positions(target, nav)
    inv = investable.compute_investable(nav, 0.0)
    lines = reconcile.reconcile(target, nav, positions, tolerance_w=0.01, investable=inv)

    assert any(ln.symbol == investable.CASH_SYMBOL for ln in lines)
    # ACTUAL: risk holdings + real cash == NAV, exactly 100%.
    assert sum(ln.actual_weight for ln in lines) == pytest.approx(1.0, abs=1e-9)
    # TARGET: model (1.0) + buffer bucket -> 1.0 + buffer.
    assert sum(ln.target_weight for ln in lines) == pytest.approx(1.0 + CASH_RESERVE, abs=1e-9)


def test_cash_line_drifts_when_account_holds_too_much_cash():
    # A genuinely UNDER-invested account (holds nothing) -> cash actual ~100%, CASH DRIFTED.
    # This proves the CASH line is an honest readout, not a rubber stamp.
    nav = 1_000_000.0
    target = make_target({"A": 1.0}, {"A": 100.0})
    lines = reconcile.reconcile(target, nav, {}, tolerance_w=0.01,
                                investable=investable.compute_investable(nav, 0.0))
    cash = next(ln for ln in lines if ln.symbol == investable.CASH_SYMBOL)
    assert cash.actual_weight == pytest.approx(1.0)      # all cash
    assert cash.status == "DRIFTED"                       # 100% vs 1.5% target -> flagged


# --- 3. GUARDRAIL: the CASH bucket moves ZERO order quantities ------------------
def test_cash_bucket_adds_no_order_and_changes_no_share_count():
    # Reconcile a flat account: the risk deltas (target_shares) must be exactly what they
    # were before Slice 3, and CASH must contribute target_shares==0 / actual_shares==0 and
    # never appear as an order. We assert against the independently-computed Slice-2 sizing.
    nav = 1_000_000.0
    target = make_target({"SPY": 0.6, "BND": 0.4}, {"SPY": 200.0, "BND": 100.0})
    inv = investable.compute_investable(nav, 0.0)
    lines = reconcile.reconcile(target, nav, {}, tolerance_w=0.03, investable=inv)

    by_sym = {ln.symbol: ln for ln in lines}
    # Slice-2 sizing, recomputed independently:
    assert by_sym["SPY"].target_shares == int(0.6 * inv / 200.0)   # 2955
    assert by_sym["BND"].target_shares == int(0.4 * inv / 100.0)   # 3940
    # CASH carries no shares at all
    cash = by_sym[investable.CASH_SYMBOL]
    assert cash.target_shares == 0 and cash.actual_shares == 0.0


def test_engine_order_quantities_unchanged_by_cash_bucket():
    # End-to-end through rebalance_engine.plan_account: the orders dict (symbol -> signed
    # share delta) must contain ONLY risk symbols with the exact Slice-2 deltas, and must
    # NOT contain a CASH order. This is the load-bearing guardrail: Slice 3 fixes the
    # readout, it must not move a single target share count.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    # Hold 5000 (far below target) so the band breaches and the engine emits the delta.
    plan = eng.plan_account("DU0001", "Balanced", 1_000_000, {"SPY": 5000}, target,
                            band_pct=0.03)
    # target on 985,000 investable @ $100 = 9850 -> delta +4850 (identical to Slice 2)
    assert plan.orders == {"SPY": 9850 - 5000}
    assert investable.CASH_SYMBOL not in plan.orders     # CASH never generates an order


def test_in_band_account_still_emits_nothing_with_cash_line_present():
    # The CASH line must not perturb the no-trade band: an on-target account stays in-band.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    plan = eng.plan_account("DU0002", "Balanced", 1_000_000, {"SPY": 9850}, target,
                            band_pct=0.03)
    assert plan.orders == {}
    assert plan.needs_rebalance is False
    # CASH IS present in the reconciled lines (readout), just not an order.
    assert any(ln.symbol == investable.CASH_SYMBOL for ln in plan.lines)
