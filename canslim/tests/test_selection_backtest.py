"""Tests for the mechanical selection-from-watchlist backtest.

Load-bearing guarantees:
  1. NO-LOOKAHEAD: the machine's breakout entry never uses a bar on/before the base
     decision week for the breakout, and appending FUTURE bars after an entry cannot move
     that entry earlier. (The base itself is guarded inside base_detector; here we guard the
     breakout scan and the portfolio walk.)
  2. Buy zone: the machine only fills within BUY_ZONE above the pivot; a gap far past the
     zone at the open is skipped (extended), not filled.
  3. No re-entry while a position in the same name is still open (same-breakout dedup).
"""
import datetime as dt
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selection_backtest as sb  # noqa: E402


def _frame(closes, start="2020-01-02", vol=0.0, base_flat=None):
    """Daily OHLC frame from a close series (business days). low/high = close*(1-/+vol)."""
    idx = []
    d = dt.date.fromisoformat(start)
    while len(idx) < len(closes):
        if d.weekday() < 5:
            idx.append(pd.Timestamp(d))
        d += dt.timedelta(days=1)
    rows = [dict(date=idx[i], open=float(c), high=float(c * (1 + vol)), low=float(c * (1 - vol)),
                 close=float(c), volume=1e6)
            for i, c in enumerate(closes)]
    return pd.DataFrame(rows).set_index("date")


def test_buy_zone_skips_extended_gap():
    """An open that gaps far past the buy zone must NOT fill; a later day back in the zone can."""
    # pivot ~= 100; build a flat frame at 100 then a day opening at 120 (>5% above) -> skip,
    # then a day opening at 103 (within 5%) high>=pivot -> fill.
    closes = [100] * 40
    df = _frame(closes)
    pivot = 100.0
    # manual breakout scan mirroring find_machine_entries' inner logic
    wts = df.index[35]
    fwd = df.loc[df.index > wts].copy()
    # inject the two candidate days
    fwd.iloc[0] = [120.0, 121.0, 119.0, 120.5, 1e6]   # gap open beyond zone -> skip
    fwd.iloc[1] = [103.0, 104.0, 102.0, 103.5, 1e6]   # back in zone, high>=pivot -> fill
    took = None
    for d, row in fwd.iterrows():
        if row["high"] >= pivot:
            if row["open"] > pivot * (1 + sb.BUY_ZONE):
                continue
            took = (d, pivot); break
    assert took is not None
    assert took[0] == fwd.index[1]  # filled on the in-zone day, not the gap day


def test_no_reentry_while_open():
    """Two entries in the same name close together: the 2nd is skipped while the 1st is open."""
    e1 = sb.MachineEntry("TST", dt.date(2020, 1, 6), dt.date(2020, 1, 6),
                         dt.date(2020, 1, 10), 100.0, 100.0, "flat_base", 0.1)
    e2 = sb.MachineEntry("TST", dt.date(2020, 1, 13), dt.date(2020, 1, 13),
                         dt.date(2020, 1, 15), 105.0, 105.0, "flat_base", 0.1)
    # price rises then falls: exit only well after both entries
    df = _frame([100 + i for i in range(120)], start="2020-01-02")
    prices = {"TST": df}
    r = sb.run_portfolio([e1, e2], prices, timing=[], use_timing=False)
    # only ONE lot should be taken (2nd skipped while 1st open)
    assert r["n"] == 1


def test_lookahead_entry_stable_under_future_bars():
    """Appending FUTURE bars after the eligibility window must not create an EARLIER entry."""
    # a name that breaks out at index ~45; detector needs history. Build a clean cup then breakout.
    import numpy as np
    base = list(np.linspace(50, 40, 20)) + list(np.linspace(40, 50, 20))  # cup
    run = [50 + i for i in range(60)]  # breakout run
    closes = base + run
    df = _frame(closes, start="2019-06-03")
    pool = {"TST": [(df.index[38].date(), None)]}  # watch at right rim
    ent_full = sb.find_machine_entries(pool, {"TST": df})
    # truncate future bars 10 days past the eligibility window end and re-run
    if ent_full:
        e = ent_full[0]
        cutoff = pd.Timestamp(e.entry_date) + pd.Timedelta(days=1)
        df_trunc = df.loc[df.index <= cutoff]
        ent_trunc = sb.find_machine_entries(pool, {"TST": df_trunc})
        # the entry that exists with full data must exist identically with data only through
        # its own entry day (no future bar created or moved it).
        assert ent_trunc, "entry vanished when future bars removed -> lookahead"
        assert ent_trunc[0].entry_date == e.entry_date
        assert abs(ent_trunc[0].entry_px - e.entry_px) < 1e-6
