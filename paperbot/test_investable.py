"""
test_investable.py — characterization tests pinning the shared investable/buffer math.

Slice 1 of the account-cashflow consolidation moved the "investable capital / cash
buffer" formula out of five inline copies into the single leaf module `investable`.
These tests PIN the numbers at the current buffer (0.05) so the consolidation is proven
behavior-identical: if any repointed call site ever drifts from the shared function, a
test here fails. SYNTHETIC inputs only — no broker, no gateway, nothing transmitted.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest ^
    "C:\\Users\\andre\\My Drive (andrew@surberhc.com)\\TradingDesk\\paperbot\\test_investable.py" -v
"""
from __future__ import annotations

import pytest

import config
import execution_engine
import investable
import rebalance_engine as eng
import reconcile


CASH_RESERVE = config.RISK_LIMITS["cash_reserve_pct"]   # 0.05 by default


# --- 1. the canonical math, pinned at 0.05 -------------------------------------
def test_buffer_pct_is_the_single_config_value():
    assert investable.buffer_pct() == config.RISK_LIMITS["cash_reserve_pct"]
    assert investable.buffer_pct() == 0.05   # pinned for Slice 1 (no behavior change)


def test_investable_no_reserve():
    # (1,000,000 - 0) * (1 - 0.05) = 950,000
    assert investable.compute_investable(1_000_000, 0.0, 0.05) == pytest.approx(950_000.0)


def test_investable_with_reserve_carved_first():
    # reserve removed BEFORE the cash buffer: (1,000,000 - 100,000) * 0.95 = 855,000
    assert investable.compute_investable(1_000_000, 100_000, 0.05) == pytest.approx(855_000.0)


def test_investable_reserve_over_netliq_clamps_to_zero():
    # reserve larger than NetLiq must clamp to 0, not manufacture phantom sells.
    assert investable.compute_investable(50_000, 200_000, 0.05) == 0.0


def test_investable_defaults_pct_from_config():
    # Omitting cash_reserve_pct resolves it from config — same as passing 0.05 today.
    assert (investable.compute_investable(1_000_000, 0.0)
            == investable.compute_investable(1_000_000, 0.0, CASH_RESERVE)
            == pytest.approx(950_000.0))


# --- 2. each repointed call site yields the SAME number it did before -----------
# These reproduce the EXACT pre-refactor inline expression for each site and assert the
# repointed code path now equals it. This is the proof the refactor moved nothing.
def test_rebalance_engine_wrapper_matches_shared():
    # rebalance_engine.compute_investable is now a thin re-export of investable.*
    old_inline = (1_000_000 - 100_000) * (1.0 - CASH_RESERVE)        # (nav-reserve)*(1-r)
    assert eng.compute_investable(1_000_000, 100_000) == pytest.approx(old_inline)
    assert (eng.compute_investable(1_000_000, 100_000)
            == investable.compute_investable(1_000_000, 100_000))


def test_reconcile_default_investable_matches_old_inline():
    # reconcile's old default was nav*(1-cash_reserve_pct). Drive it via the public
    # reconcile() with an empty target/positions and read the value it sized against.
    import pandas as pd
    import strategy_target

    nav = 1_000_000.0
    old_inline = nav * (1.0 - CASH_RESERVE)
    # A one-symbol target priced at $1 so target_shares == int(weight*investable/price)
    # surfaces the investable used. weight 1.0, price 1.0 -> target_shares == int(investable).
    target = strategy_target.Target(
        weights=pd.Series({"AAA": 1.0}, dtype="float64"),
        prices=pd.Series({"AAA": 1.0}, dtype="float64"),
        as_of=pd.Timestamp("2026-06-26"),
        price_date=pd.Timestamp("2026-06-26"),
        version="Balanced",
    )
    lines = reconcile.reconcile(target, nav, {}, tolerance_w=0.03)
    assert lines[0].target_shares == int(old_inline)
    assert int(old_inline) == int(investable.compute_investable(nav, 0.0))


def test_execution_engine_default_investable_matches_old_inline():
    # execution_engine.compute_intended_orders sizes against nav*(1-cash_reserve_pct).
    # Same surfacing trick: weight 1.0 @ $1 -> target_shares == int(investable).
    import pandas as pd
    import strategy_target

    nav = 1_000_000.0
    old_inline = nav * (1.0 - CASH_RESERVE)
    target = strategy_target.Target(
        weights=pd.Series({"AAA": 1.0}, dtype="float64"),
        prices=pd.Series({"AAA": 1.0}, dtype="float64"),
        as_of=pd.Timestamp("2026-06-26"),
        price_date=pd.Timestamp("2026-06-26"),
        version="Balanced",
    )
    orders = execution_engine.compute_intended_orders(nav, {}, target)
    # one BUY of int(investable) shares of AAA from a flat book
    assert len(orders) == 1
    assert orders[0].symbol == "AAA"
    assert orders[0].quantity == int(old_inline)
    assert int(old_inline) == int(investable.compute_investable(nav, 0.0))
