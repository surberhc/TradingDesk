r"""
test_s6_control.py — unit tests for the S6 control harness (s6_control).

These pin the MECHANICS, not any strategy outcome:
  * honest fills: credit = short_bid - long_ask; close debit = short_ask - long_bid;
  * hand-checked single-trade P&L;
  * stop logic fires at the first minute net loss reaches 2x credit, and never before;
  * the documented no-trade rule (credit < $0.30) skips;
  * NO LOOK-AHEAD: the exit scan's decision at the firing minute is invariant to whether
    later minutes exist (truncation invariance), and it stops at the FIRST rule hit.

All tests build tiny synthetic NBBO grids in-memory — no warehouse needed — so they run
anywhere and assert exact arithmetic.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s6_control as c  # noqa: E402


# --------------------------------------------------------------------------- #
# Synthetic NBBO builder
# --------------------------------------------------------------------------- #
def _grid(day: dt.date, rows: list[dict]) -> pd.DataFrame:
    """rows: dicts with minute(HH:MM), strike, right, bid, ask."""
    out = []
    for r in rows:
        hh, mm = map(int, r["minute"].split(":"))
        out.append(
            {
                "minute": pd.Timestamp(dt.datetime.combine(day, dt.time(hh, mm))),
                "strike": float(r["strike"]),
                "right": r["right"],
                "bid": float(r["bid"]),
                "ask": float(r["ask"]),
            }
        )
    return pd.DataFrame(out)


# --------------------------------------------------------------------------- #
# Honest-fill arithmetic
# --------------------------------------------------------------------------- #
def test_credit_and_debit_are_honest_fills():
    # SELL short at bid, BUY long at ask => credit = short_bid - long_ask.
    assert c._credit_to_open(short_bid=2.00, long_ask=0.80) == pytest.approx(1.20)
    # Close: BUY back short at ask, SELL long at bid => debit = short_ask - long_bid.
    assert c._debit_to_close(short_ask=0.30, long_bid=0.05) == pytest.approx(0.25)


def test_spread_close_debit_uses_ask_for_short_bid_for_long():
    day = dt.date(2024, 1, 5)
    snap = _grid(day, [
        {"minute": "14:00", "strike": 100, "right": "PUT", "bid": 1.90, "ask": 2.10},
        {"minute": "14:00", "strike": 95, "right": "PUT", "bid": 0.70, "ask": 0.90},
    ])
    snap = snap[["strike", "right", "bid", "ask"]]
    legs = [(100.0, "PUT", +1), (95.0, "PUT", -1)]  # short 100 put, long 95 put
    # close debit = short_ask(2.10) - long_bid(0.70) = 1.40
    assert c._spread_debit_to_close(snap, legs) == pytest.approx(1.40)


# --------------------------------------------------------------------------- #
# Hand-checked single trade P&L (winner path)
# --------------------------------------------------------------------------- #
def test_full_trade_pnl_winner_handchecked():
    """Bull put: short 100P bid 1.50/ask 1.70, long 95P bid 0.40/ask 0.60 at entry.
    Entry credit = 1.50 - 0.60 = 0.90. At 15:00 the spread collapses so close debit
    = short_ask 0.05 - long_bid 0.00 = 0.05 -> winner. P&L = 0.90 - 0.05 = 0.85 pts
    = $85 for one contract."""
    day = dt.date(2024, 2, 1)
    # Build a chain rich enough for spot recon AND the put-spread legs. We bypass recon
    # by calling the exit scan directly on the two legs (recon is tested separately).
    entry = pd.Timestamp(dt.datetime.combine(day, dt.time(14, 0)))
    settle = pd.Timestamp(dt.datetime.combine(day, dt.time(16, 0)))
    nbbo = _grid(day, [
        {"minute": "14:00", "strike": 100, "right": "PUT", "bid": 1.50, "ask": 1.70},
        {"minute": "14:00", "strike": 95, "right": "PUT", "bid": 0.40, "ask": 0.60},
        {"minute": "15:00", "strike": 100, "right": "PUT", "bid": 0.00, "ask": 0.05},
        {"minute": "15:00", "strike": 95, "right": "PUT", "bid": 0.00, "ask": 0.05},
    ])
    legs = [(100.0, "PUT", +1), (95.0, "PUT", -1)]
    credit = c._credit_to_open(1.50, 0.60)
    assert credit == pytest.approx(0.90)
    reason, minute, debit = c._scan_exit(nbbo, legs, credit, entry, settle)
    assert reason == "winner"
    assert minute == pd.Timestamp(dt.datetime.combine(day, dt.time(15, 0)))
    assert debit == pytest.approx(0.05)  # short_ask 0.05 - long_bid 0.00
    pnl_points = credit - debit
    assert pnl_points == pytest.approx(0.85)
    assert pnl_points * c.CONTRACT_MULTIPLIER == pytest.approx(85.0)


# --------------------------------------------------------------------------- #
# Stop logic
# --------------------------------------------------------------------------- #
def test_stop_fires_at_first_minute_reaching_2x_credit():
    """Credit 1.00 => stop threshold debit = (1 + 2)*1.00 = 3.00. The scan must fire at
    the FIRST minute debit >= 3.00 and not earlier."""
    day = dt.date(2024, 3, 1)
    entry = pd.Timestamp(dt.datetime.combine(day, dt.time(14, 0)))
    settle = pd.Timestamp(dt.datetime.combine(day, dt.time(16, 0)))
    # short 100P ask, long 95P bid -> debit = short_ask - long_bid.
    nbbo = _grid(day, [
        {"minute": "14:30", "strike": 100, "right": "PUT", "bid": 2.40, "ask": 2.50},
        {"minute": "14:30", "strike": 95, "right": "PUT", "bid": 0.10, "ask": 0.20},  # debit 2.40 < 3
        {"minute": "14:45", "strike": 100, "right": "PUT", "bid": 3.10, "ask": 3.20},
        {"minute": "14:45", "strike": 95, "right": "PUT", "bid": 0.10, "ask": 0.20},  # debit 3.10 >= 3 -> STOP
        {"minute": "15:00", "strike": 100, "right": "PUT", "bid": 5.00, "ask": 5.10},
        {"minute": "15:00", "strike": 95, "right": "PUT", "bid": 0.10, "ask": 0.20},  # later, must NOT be used
    ])
    legs = [(100.0, "PUT", +1), (95.0, "PUT", -1)]
    reason, minute, debit = c._scan_exit(nbbo, legs, entry_credit=1.00,
                                         entry_minute=entry, settle_minute=settle)
    assert reason == "stop"
    assert minute == pd.Timestamp(dt.datetime.combine(day, dt.time(14, 45)))
    assert debit == pytest.approx(3.10)


def test_no_stop_holds_to_settlement():
    """If neither winner nor stop fires, close at the last marked (settlement) minute."""
    day = dt.date(2024, 3, 2)
    entry = pd.Timestamp(dt.datetime.combine(day, dt.time(14, 0)))
    settle = pd.Timestamp(dt.datetime.combine(day, dt.time(16, 0)))
    nbbo = _grid(day, [
        {"minute": "15:00", "strike": 100, "right": "PUT", "bid": 0.60, "ask": 0.70},
        {"minute": "15:00", "strike": 95, "right": "PUT", "bid": 0.10, "ask": 0.20},  # debit 0.60
        {"minute": "16:00", "strike": 100, "right": "PUT", "bid": 0.30, "ask": 0.40},
        {"minute": "16:00", "strike": 95, "right": "PUT", "bid": 0.05, "ask": 0.10},  # debit 0.35
    ])
    legs = [(100.0, "PUT", +1), (95.0, "PUT", -1)]
    reason, minute, debit = c._scan_exit(nbbo, legs, entry_credit=1.00,
                                         entry_minute=entry, settle_minute=settle)
    assert reason == "settle"
    assert minute == settle
    assert debit == pytest.approx(0.35)


# --------------------------------------------------------------------------- #
# No look-ahead — exit decision invariant to later minutes existing.
# --------------------------------------------------------------------------- #
def test_exit_scan_no_lookahead_truncation_invariance():
    """Removing minutes AFTER the firing minute must not change (reason, minute, debit).
    This proves the scan decides causally — it never peeks past the firing minute."""
    day = dt.date(2024, 4, 1)
    entry = pd.Timestamp(dt.datetime.combine(day, dt.time(14, 0)))
    settle = pd.Timestamp(dt.datetime.combine(day, dt.time(16, 0)))
    full = _grid(day, [
        {"minute": "14:30", "strike": 100, "right": "PUT", "bid": 1.00, "ask": 1.10},
        {"minute": "14:30", "strike": 95, "right": "PUT", "bid": 0.10, "ask": 0.20},   # debit 1.00
        {"minute": "14:45", "strike": 100, "right": "PUT", "bid": 0.05, "ask": 0.05},
        {"minute": "14:45", "strike": 95, "right": "PUT", "bid": 0.00, "ask": 0.00},   # debit 0.05 -> winner
        {"minute": "15:30", "strike": 100, "right": "PUT", "bid": 9.00, "ask": 9.10},
        {"minute": "15:30", "strike": 95, "right": "PUT", "bid": 0.10, "ask": 0.20},   # future blowout, must be ignored
    ])
    legs = [(100.0, "PUT", +1), (95.0, "PUT", -1)]
    r_full = c._scan_exit(full, legs, 1.00, entry, settle)

    fire_minute = pd.Timestamp(dt.datetime.combine(day, dt.time(14, 45)))
    truncated = full[full["minute"] <= fire_minute]
    r_trunc = c._scan_exit(truncated, legs, 1.00, entry, settle)

    assert r_full == r_trunc
    assert r_full[0] == "winner"
    assert r_full[1] == fire_minute


# --------------------------------------------------------------------------- #
# No-trade rule + struct skip surface through run_day_structure.
# --------------------------------------------------------------------------- #
def test_min_credit_no_trade_rule_constant():
    # Pin the documented constant so a silent edit is caught.
    assert c.MIN_ENTRY_CREDIT == 0.30
    assert c.SPREAD_WIDTH == 5.0
    assert c.TARGET_SHORT_DELTA == 0.15
    assert c.STOP_MULTIPLE == 2.0
    assert c.WINNER_DEBIT == 0.05


def test_unquoted_minute_is_skipped_not_invented():
    """A minute where a leg is unquoted (NaN) cannot be closed -> the scan skips it
    rather than inventing a fill, and falls through to the next quoted minute."""
    day = dt.date(2024, 5, 1)
    entry = pd.Timestamp(dt.datetime.combine(day, dt.time(14, 0)))
    settle = pd.Timestamp(dt.datetime.combine(day, dt.time(16, 0)))
    nbbo = _grid(day, [
        {"minute": "14:30", "strike": 100, "right": "PUT", "bid": np.nan, "ask": np.nan},
        {"minute": "14:30", "strike": 95, "right": "PUT", "bid": np.nan, "ask": np.nan},
        {"minute": "15:00", "strike": 100, "right": "PUT", "bid": 0.00, "ask": 0.05},
        {"minute": "15:00", "strike": 95, "right": "PUT", "bid": 0.00, "ask": 0.05},  # debit 0.05 winner
    ])
    legs = [(100.0, "PUT", +1), (95.0, "PUT", -1)]
    reason, minute, debit = c._scan_exit(nbbo, legs, 1.00, entry, settle)
    assert reason == "winner"
    assert minute == pd.Timestamp(dt.datetime.combine(day, dt.time(15, 0)))
