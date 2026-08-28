"""test_no_daily_loss_halt.py — the desk has NO automated daily-loss halt.

OWNER DECISION (Andrew, 2026-08-25). A persisted -2%-of-NAV "kill switch" used to live in
risk_manager: it tripped on the day's P&L, wrote a flag file, and halted every account
until a human deleted that file by hand. It was never authorized — it entered in the
pre-git baseline snapshot and no decision record for it ever existed — and it is the wrong
construct for a monthly-rebalance allocation desk, where it would refuse to rebalance the
whole book on exactly the down day you most want to rebalance.

This test is the standing guard against it coming back. It asserts the ABSENCE of the
mechanism, not just that it is currently switched off.

NOTE — this is NOT about the file-based operator stop. The MANUAL AUTOTRADE_DISABLED
sentinel / KILL_SWITCH label (safe_execute.register_kill_switch_label, the live-deploy
rails' _kill_switch_present, dashboard/desk/kill_switch.py) is a deliberate human control
and STAYS. Nothing here touches it.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config          # noqa: E402
import risk_manager    # noqa: E402


@pytest.mark.parametrize("gone", ["check_kill_switch", "trip_killswitch",
                                  "killswitch_state", "clear_killswitch", "_KILL_FILE"])
def test_the_automated_daily_loss_breaker_is_gone_from_risk_manager(gone):
    assert not hasattr(risk_manager, gone), (
        f"risk_manager.{gone} is back. There is NO automated daily-loss halt on this desk "
        f"(owner decision 2026-08-25) and it must not be re-added.")


def test_the_daily_loss_limit_is_gone_from_the_risk_limits():
    assert "max_daily_loss_pct_nav" not in config.RISK_LIMITS, (
        "config.RISK_LIMITS carries a daily-loss limit again. It was removed 2026-08-25 by "
        "owner decision and must not be re-added.")


def _target():
    import pandas as pd

    class _T:
        prices = pd.Series({"SPY": 100.0})
        weights = pd.Series({"SPY": 1.0})

    return _T()


def test_a_catastrophic_daily_loss_does_not_halt_the_evaluation():
    """-50% of NAV on the day. Nothing halts: `daily_pnl` gates nothing at all now — it is
    accepted for call-compatibility and recorded in the ledger, and that is the whole job."""
    nav = 1_000_000.0
    report = risk_manager.evaluate(nav, -0.50 * nav, {}, [], _target())
    assert report.halted is False
    assert report.halt_reason == ""


# --------------------------------------------------------------------------------------
# THE PER-POSITION CAP IS GONE TOO (same owner decision, Andrew, 2026-08-25).
#
# config.RISK_LIMITS["max_position_pct_nav"] = 0.35 capped any single RISK position at 35%
# of NAV. It was never authorized — it entered in the pre-git baseline snapshot with no
# decision record — and its own comment admitted the earlier 5% value would have VETOED
# the strategy itself, so the number had already been retuned once to fit the book it was
# supposed to police. Of the three unauthorized invented numbers only the drift band
# survives.
#
# These tests assert the ABSENCE of the cap AND that the guard that DID survive — the
# single-order-notional sanity check — still works. Both fail against the old code.
# --------------------------------------------------------------------------------------
class _Order:
    """Duck-typed stand-in for execution_engine.IntendedOrder."""

    def __init__(self, symbol, side, quantity, limit_price):
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.limit_price = limit_price
        self.legs = 1


def _priced_target(prices):
    import pandas as pd

    class _T:
        pass

    t = _T()
    t.prices = pd.Series(prices)
    t.weights = pd.Series({k: 0.0 for k in prices})
    return t


def test_the_per_position_cap_is_gone_from_the_risk_limits():
    assert "max_position_pct_nav" not in config.RISK_LIMITS, (
        "config.RISK_LIMITS carries a per-position cap again. It was removed 2026-08-25 by "
        "owner decision and must not be re-added.")


def test_a_ninety_percent_of_nav_position_is_not_vetoed():
    """The proof the cap is gone. NAV 1,000,000; buy 9,000 shares at $100 = $900,000, so
    the resulting single risk position is ~90% of NAV — nearly triple the old 35% ceiling.

    Against the UNMODIFIED code this order was vetoed with
    'resulting 90.0% > per-position cap 35%'. It must now pass.
    """
    nav = 1_000_000.0
    orders = [_Order("SPY", "BUY", 9_000, 100.0)]
    report = risk_manager.evaluate(nav, 0.0, {}, orders, _priced_target({"SPY": 100.0}))

    verdict = report.order_verdicts[0]
    assert verdict.ok is True, verdict.reasons
    assert not any("per-position cap" in r for r in verdict.reasons), verdict.reasons
    assert [o.symbol for o in report.approved] == ["SPY"]


def test_the_surviving_order_notional_guard_still_vetoes_an_order_bigger_than_nav():
    """The control: removing the cap must not have removed ORDER SANITY. A single order
    whose notional exceeds NAV is still refused, and the reason is the notional one."""
    nav = 1_000_000.0
    orders = [_Order("SPY", "BUY", 20_000, 100.0)]   # $2,000,000 notional on a $1M account
    report = risk_manager.evaluate(nav, 0.0, {}, orders, _priced_target({"SPY": 100.0}))

    verdict = report.order_verdicts[0]
    assert verdict.ok is False
    assert any("exceeds NAV" in r for r in verdict.reasons), verdict.reasons
    assert report.approved == []
