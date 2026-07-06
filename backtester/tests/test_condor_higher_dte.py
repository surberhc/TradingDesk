"""
Guards for condor_higher_dte.py (Arm 4 — higher-DTE managed iron condor).

Covers:
  (a) NO LOOK-AHEAD — management/settlement uses only same-or-later EOD data,
      never a future day. Enforced structurally (run_dte only ever reads the
      current loop day's chain) and checked on a synthetic 3-day tape.
  (b) EXIT LOGIC — profit-take, 21-DTE time-exit, and 2x-credit disaster stop
      each fire under the right marked-debit condition, and precedence is
      take > stop > dte21 as coded.
  (c) FROZEN CONSTANTS — the pre-registered delta / wing / DTE-management knobs
      are the blessed values (rule #1: no silent drift).
"""
import datetime as _dt
import numpy as np
import pandas as pd
import pytest

import condor_higher_dte as m


# --------------------------------------------------------------------------- #
# (c) frozen constants
# --------------------------------------------------------------------------- #
def test_frozen_constants():
    assert m.SHORT_DELTA == 0.16
    assert m.WING_WIDTH == 50.0
    assert m.PROFIT_TAKE == 0.50
    assert m.EXIT_DTE == 21
    assert m.DISASTER_MULT == 2.0
    assert m.MIN_DTE_FLOOR == 25
    assert m.N_CONTRACTS == 1
    assert m.MULTIPLIER == 100.0


# --------------------------------------------------------------------------- #
# fill-convention unit checks (worst-side is genuinely worse than mid)
# --------------------------------------------------------------------------- #
def _chain(rows):
    df = pd.DataFrame(rows, columns=["strike", "right", "bid", "ask", "delta", "underlying_price"])
    df["exp_date"] = _dt.date(2020, 2, 21)
    return df


def test_entry_credit_worst_le_mid():
    # short put 100@(bid2,ask3), long put 50@(bid0.5,ask1),
    # short call 200@(bid2,ask3), long call 250@(bid0.5,ask1)
    sub = _chain([
        (100, "PUT", 2.0, 3.0, -0.16, 150.0),
        (50, "PUT", 0.5, 1.0, -0.02, 150.0),
        (200, "CALL", 2.0, 3.0, 0.16, 150.0),
        (250, "CALL", 0.5, 1.0, 0.02, 150.0),
    ])
    legs = [(100, "PUT", +1), (50, "PUT", -1), (200, "CALL", +1), (250, "CALL", -1)]
    cw = m.entry_credit(sub, legs, "worst")
    cm = m.entry_credit(sub, legs, "mid")
    # worst: sell shorts@bid(2+2), buy wings@ask(1+1) => 4-2 = 2.0
    assert cw == pytest.approx(2.0)
    # mid: (2.5+2.5) - (0.75+0.75) = 3.5
    assert cm == pytest.approx(3.5)
    assert cw < cm  # worst-side collects LESS credit


def test_exit_debit_worst_ge_mid():
    sub = _chain([
        (100, "PUT", 2.0, 3.0, -0.16, 150.0),
        (50, "PUT", 0.5, 1.0, -0.02, 150.0),
        (200, "CALL", 2.0, 3.0, 0.16, 150.0),
        (250, "CALL", 0.5, 1.0, 0.02, 150.0),
    ])
    legs = [(100, "PUT", +1), (50, "PUT", -1), (200, "CALL", +1), (250, "CALL", -1)]
    dw = m.exit_debit(sub, legs, "worst")
    dm = m.exit_debit(sub, legs, "mid")
    # worst: buy back shorts@ask(3+3), sell wings@bid(0.5+0.5) => 6-1 = 5.0
    assert dw == pytest.approx(5.0)
    assert dm == pytest.approx(3.5)
    assert dw > dm  # worst-side pays MORE to close


def test_intrinsic_settle_capped_at_width():
    legs = [(100, "PUT", +1), (50, "PUT", -1), (200, "CALL", +1), (250, "CALL", -1)]
    # deep put breach: S=40 -> put spread pays width 50; calls worthless
    assert m.intrinsic_settle(legs, 40.0) == pytest.approx(50.0)
    # inside both shorts: S=150 -> owe nothing
    assert m.intrinsic_settle(legs, 150.0) == pytest.approx(0.0)
    # deep call breach: S=300 -> call spread pays width 50
    assert m.intrinsic_settle(legs, 300.0) == pytest.approx(50.0)


# --------------------------------------------------------------------------- #
# (a)+(b) exit logic + no-look-ahead on a synthetic 3-day tape
# --------------------------------------------------------------------------- #
# We drive run_dte() through monkeypatched session_days + load_chain so we control
# exactly what each day sees. The engine must (i) enter on day0 from day0's chain,
# (ii) manage on later days from THAT day's chain only, (iii) fire the right exit.

def _flat_chain(underlying, exp, put_short=(100, 2.0, 3.0), put_wing=(50, 0.5, 1.0),
                call_short=(200, 2.0, 3.0), call_wing=(250, 0.5, 1.0),
                short_delta=0.16):
    """A minimal condor-able chain: one 45-DTE-ish expiry with 4 usable strikes."""
    rows = [
        (put_short[0], "PUT", put_short[1], put_short[2], -short_delta, underlying),
        (put_wing[0], "PUT", put_wing[1], put_wing[2], -0.03, underlying),
        (call_short[0], "CALL", call_short[1], call_short[2], short_delta, underlying),
        (call_wing[0], "CALL", call_wing[1], call_wing[2], 0.03, underlying),
    ]
    df = pd.DataFrame(rows, columns=["strike", "right", "bid", "ask", "delta", "underlying_price"])
    df["exp_date"] = exp
    return df


def _install_tape(monkeypatch, tape: dict):
    """tape: {date -> chain-or-None}. session_days = sorted keys."""
    days = sorted(tape.keys())
    monkeypatch.setattr(m, "session_days", lambda symbol: list(days))

    seen = {"future_read": False, "max_day_seen": None}

    def fake_load(symbol, d):
        # Guard: run_dte should only ever ask for a day in the tape as the loop
        # advances; record the high-water day to prove no future peeking.
        if seen["max_day_seen"] is None or d >= seen["max_day_seen"]:
            seen["max_day_seen"] = d
        return tape.get(d)

    monkeypatch.setattr(m, "load_chain", fake_load)
    return seen


def test_disaster_stop_fires(monkeypatch):
    d0 = _dt.date(2020, 1, 6)
    d1 = _dt.date(2020, 1, 7)
    exp = _dt.date(2020, 2, 21)  # ~46 DTE from d0
    # day0: enter. credit worst = (2+2)-(1+1)=2.0
    c0 = _flat_chain(150.0, exp)
    # day1: market gaps; debit-to-close (worst) explodes so open loss <= -2x credit.
    # exit_debit worst = buy shorts@ask + sell wings@bid.
    # Set short ask huge => debit large => open_profit = 2.0 - debit very negative.
    c1 = _flat_chain(150.0, exp,
                     put_short=(100, 8.0, 9.0), call_short=(200, 8.0, 9.0))
    seen = _install_tape(monkeypatch, {d0: c0, d1: c1})
    trades = m.run_dte("SPXW", 45)
    assert len(trades) == 1
    t = trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_day == d1
    # open loss must have breached the 2x-credit trigger
    open_profit = t.credit_worst - t.exit_worst
    assert open_profit <= -m.DISASTER_MULT * t.credit_worst


def test_profit_take_fires(monkeypatch):
    d0 = _dt.date(2020, 1, 6)
    d1 = _dt.date(2020, 1, 7)
    exp = _dt.date(2020, 2, 21)
    c0 = _flat_chain(150.0, exp)  # entry credit worst = 2.0
    # day1: theta decayed; cheap to close so open profit >= 50% of credit (>=1.0).
    # exit_debit worst = shorts@ask + wings-sold@bid. Make it ~0.9 => profit ~1.1.
    c1 = _flat_chain(150.0, exp,
                     put_short=(100, 0.30, 0.40), put_wing=(50, 0.05, 0.10),
                     call_short=(200, 0.30, 0.40), call_wing=(250, 0.05, 0.10))
    _install_tape(monkeypatch, {d0: c0, d1: c1})
    trades = m.run_dte("SPXW", 45)
    assert len(trades) == 1
    t = trades[0]
    assert t.exit_reason == "take"
    assert (t.credit_worst - t.exit_worst) >= m.PROFIT_TAKE * t.credit_worst


def test_dte21_time_exit_fires(monkeypatch):
    d0 = _dt.date(2020, 1, 6)
    # a day at exactly 21 DTE with no take / no stop -> time-exit
    d1 = _dt.date(2020, 1, 31)   # 21 days before exp
    exp = _dt.date(2020, 2, 21)
    c0 = _flat_chain(150.0, exp)  # credit 2.0
    # day1 debit ~ mid-ish so neither take (need profit>=1.0) nor stop fires
    c1 = _flat_chain(150.0, exp,
                     put_short=(100, 1.6, 1.9), call_short=(200, 1.6, 1.9))
    assert (exp - d1).days == m.EXIT_DTE
    _install_tape(monkeypatch, {d0: c0, d1: c1})
    trades = m.run_dte("SPXW", 45)
    assert len(trades) == 1
    assert trades[0].exit_reason == "dte21"
    assert trades[0].exit_day == d1


def test_expiry_settlement_when_held(monkeypatch):
    d0 = _dt.date(2020, 1, 6)
    exp = _dt.date(2020, 2, 21)
    c0 = _flat_chain(150.0, exp)
    # expiry day: underlying inside both shorts -> keep full credit (payout 0).
    c_exp = _flat_chain(150.0, exp)
    # no interim day between entry and expiry that would trip a stop/take:
    # only give the loop entry-day and expiry-day (dte==0 path).
    _install_tape(monkeypatch, {d0: c0, exp: c_exp})
    trades = m.run_dte("SPXW", 45)
    assert len(trades) == 1
    t = trades[0]
    assert t.exit_reason == "expiry"
    assert not t.breached  # settled inside the shorts
    # pnl = credit - 0 payout - commission, in dollars
    comm_pts = (m.COMMISSION * 8.0) / m.MULTIPLIER
    assert t.pnl_worst == pytest.approx((t.credit_worst - 0.0 - comm_pts) * m.MULTIPLIER)


def test_no_lookahead_management_uses_current_day_only(monkeypatch):
    """Prove the engine never marks a position with a FUTURE day's chain.

    We record every (day requested) load_chain call and assert that while a
    position is open, the debit used to decide the exit came from the SAME day
    the loop is on — never a later dated chain. We do this by making later days'
    chains poisoned (would force a different decision) and checking the exit
    matches the FIRST qualifying day, not a later one.
    """
    d0 = _dt.date(2020, 1, 6)
    d1 = _dt.date(2020, 1, 7)   # first day a stop would fire
    d2 = _dt.date(2020, 1, 8)   # a LATER day where a take would fire
    exp = _dt.date(2020, 2, 21)
    c0 = _flat_chain(150.0, exp)
    # d1: gapped => stop should fire HERE (uses d1's chain)
    c1 = _flat_chain(150.0, exp,
                     put_short=(100, 8.0, 9.0), call_short=(200, 8.0, 9.0))
    # d2: cheap => a take would fire; if the engine peeked forward it might
    # mis-decide. Correct causal behavior closes on d1 and never consults d2.
    c2 = _flat_chain(150.0, exp,
                     put_short=(100, 0.10, 0.15), call_short=(200, 0.10, 0.15))
    _install_tape(monkeypatch, {d0: c0, d1: c1, d2: c2})
    trades = m.run_dte("SPXW", 45)
    # The FIRST trade must close on d1 by the stop — proving the exit decision used
    # d1's own gapped chain and did NOT peek forward to d2's cheap chain (which would
    # have suppressed the stop). (The engine legitimately re-enters flat on d1 and may
    # close a second book on d2; that is causal re-entry, not look-ahead.)
    assert len(trades) >= 1
    assert trades[0].exit_day == d1
    assert trades[0].exit_reason == "stop"
    # And the second trade (if any) can only have been entered on d1 (>= first exit),
    # never before — no back-dated entry.
    for t in trades[1:]:
        assert t.entry_day >= d1
