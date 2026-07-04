r"""
test_s5fh_management.py -- the MANAGEMENT / exit-trigger logic of s5_financing_harness.

Pins that EACH exit rule fires on the correct condition and NEVER earlier:
  * hold          : never exits early (returns None regardless of P&L/DTE);
  * profit_target : fires when open P&L >= target * entry_credit, not before;
  * time_exit     : fires when governing DTE <= time_exit_dte, not before;
  * target_or_time: whichever binds first;
  * stop_mult     : an N x-credit LOSS stop fires in any mode; risk control binds before
                    a same-day profit target.

Pure-logic tests on _check_management with a stub Position -- no warehouse, exact.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s5_financing_harness as h  # noqa: E402


def _pos(entry_credit=200.0):
    """Minimal stub Position (only entry_credit is read by _check_management)."""
    return h.Position(
        name="stub", entry_date=dt.date(2022, 1, 3), legs=[],
        entry_credit=entry_credit, entry_commission=1.30,
        entry_underlying=4000.0, last_expiration=dt.date(2022, 2, 18),
    )


def test_hold_never_exits_early():
    m = h.Management(mode="hold")
    assert h._check_management(m, _pos(), open_pnl=180.0, gov_dte=1) is None
    assert h._check_management(m, _pos(), open_pnl=-500.0, gov_dte=1) is None


def test_profit_target_fires_at_threshold_not_before():
    m = h.Management(mode="profit_target", profit_target=0.50)
    p = _pos(entry_credit=200.0)   # 50% target = +$100 open profit
    assert h._check_management(m, p, open_pnl=99.0, gov_dte=30) is None
    assert h._check_management(m, p, open_pnl=100.0, gov_dte=30) == "profit_target"
    assert h._check_management(m, p, open_pnl=150.0, gov_dte=30) == "profit_target"


def test_time_exit_fires_at_dte_not_before():
    m = h.Management(mode="time_exit", time_exit_dte=21)
    p = _pos()
    assert h._check_management(m, p, open_pnl=10.0, gov_dte=22) is None
    assert h._check_management(m, p, open_pnl=10.0, gov_dte=21) == "time_exit"
    assert h._check_management(m, p, open_pnl=10.0, gov_dte=5) == "time_exit"


def test_target_or_time_whichever_first():
    m = h.Management(mode="target_or_time", profit_target=0.50, time_exit_dte=21)
    p = _pos(entry_credit=200.0)
    # target hit, plenty of DTE left -> profit_target
    assert h._check_management(m, p, open_pnl=120.0, gov_dte=40) == "profit_target"
    # target not hit, but DTE reached -> time_exit
    assert h._check_management(m, p, open_pnl=10.0, gov_dte=21) == "time_exit"
    # neither -> hold
    assert h._check_management(m, p, open_pnl=10.0, gov_dte=40) is None


def test_stop_loss_fires_on_loss_in_any_mode():
    p = _pos(entry_credit=200.0)   # 2x stop = -$400 open loss
    for mode_kw in (
        dict(mode="hold"),
        dict(mode="profit_target", profit_target=0.50),
        dict(mode="target_or_time", profit_target=0.50, time_exit_dte=21),
    ):
        m = h.Management(stop_mult=2.0, **mode_kw)
        assert h._check_management(m, p, open_pnl=-399.0, gov_dte=30) is None
        assert h._check_management(m, p, open_pnl=-400.0, gov_dte=30) == "stop"


def test_stop_binds_before_same_day_profit_target():
    # A degenerate same-day check: if both a huge loss and (impossibly) a target were true,
    # the stop is evaluated first. We assert the stop wins when loss condition holds.
    m = h.Management(mode="profit_target", profit_target=0.50, stop_mult=2.0)
    p = _pos(entry_credit=200.0)
    assert h._check_management(m, p, open_pnl=-500.0, gov_dte=30) == "stop"
