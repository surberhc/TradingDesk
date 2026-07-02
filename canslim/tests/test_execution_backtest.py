"""Tests for the integrated execution backtest.

The load-bearing guarantees:
  1. NO-LOOKAHEAD (desk causality rule): an exit decision on day D depends only on bars
     up to D. Extending the path with FUTURE bars must not change an exit that already
     triggered on or before its original date.
  2. E1 reproduces his realized book (the sanity check the whole test rests on).
  3. Each exit rule behaves as specified on controlled synthetic paths:
       E2 = fixed -7% stop; E3 = -7% then let-it-run via the 50-day line; E4 = E3 + cap.
  4. Portfolio path-dependence: cash freed by an exit funds a later entry.
"""
import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import execution_backtest as eb  # noqa: E402


def _path(closes, start="2024-01-02", vol=0.0):
    """Synthetic daily OHLCV path from a close series (business days).
    low = close*(1-vol), high = close*(1+vol) so we can force/avoid stop touches."""
    idx = []
    d = dt.date.fromisoformat(start)
    while len(idx) < len(closes):
        if d.weekday() < 5:
            idx.append(d)
        d += dt.timedelta(days=1)
    return [(idx[i], c, c * (1 + vol), c * (1 - vol), c) for i, c in enumerate(closes)]


def _trade(entry, buy, sell, actual_ret, cost=52000.0):
    return dict(symbol="TST", year=2024, entry_px=entry, exit_px=entry * (1 + actual_ret),
                buy=dt.date.fromisoformat(buy), sell=dt.date.fromisoformat(sell),
                actual_ret=actual_ret, cost=cost, pl=cost * actual_ret, split_flag=False)


# --------------------------------------------------------------------------- 1. no-lookahead
def test_no_lookahead_exit_is_causal():
    """A -7% stop that triggers on day D must return the SAME (date,ret) whether or not the
    path contains bars after D. Appending a huge future rally cannot un-trigger the stop."""
    entry = 100.0
    # day 0 entry bar, then a drop whose LOW pierces -7% on day 3 (but open stays above the
    # stop, so the fill is AT the stop = -7%), then (in the extended path) a moonshot.
    base = [100, 99, 97, 94.5, 90]
    t = _trade(entry, "2024-01-02", "2024-01-31", -0.10)
    short = _path(base, vol=0.03)                # widen lows so day-3 low <= 93 but open ~94.5
    long = _path(base + [200, 300, 400, 500], vol=0.03)  # future rally must NOT matter
    xd_s, xr_s = eb.simulate_exit(t, short, "E2")
    xd_l, xr_l = eb.simulate_exit(t, long, "E2")
    assert xd_s == xd_l, "exit date changed when future bars were added (lookahead!)"
    assert abs(xr_s - xr_l) < 1e-9, "exit return changed with future bars (lookahead!)"
    assert xr_s == pytest.approx(-0.07, abs=1e-9), "intraday-touch fixed stop should fill at -7%"


def test_decision_horizon_guard_raises_on_future_bar():
    """The engine's decision_max_date guard must fire if any decision bar is past the horizon."""
    t = _trade(100.0, "2024-01-02", "2024-01-31", 0.0)
    path = _path([100, 101, 102, 103])
    with pytest.raises(eb.NoLookaheadError):
        eb.simulate_exit(t, path, "E2", decision_max_date=path[1][0])  # bar[2] is past horizon


# --------------------------------------------------------------------------- 3. exit-rule behavior
def test_e2_fixed_stop_fills_at_7pct_on_intraday_touch():
    # low pierces the -7% stop but the open is above it -> fill AT the stop = -7%.
    t = _trade(100.0, "2024-01-02", "2024-02-28", -0.20)
    path = _path([100, 98, 95, 94.5, 85], vol=0.03)  # day-3 low ~91.7 <= 93, open ~94.5 > 93
    xd, xr = eb.simulate_exit(t, path, "E2")
    assert xr == pytest.approx(-0.07, abs=1e-9)


def test_e2_gap_through_fills_at_open():
    # bar opens BELOW the stop (gap-through) -> fill at the open, worse than -7%.
    t = _trade(100.0, "2024-01-02", "2024-02-28", -0.20)
    path = _path([100, 98, 95, 90, 85])          # day-3 open==90 < 93 -> fill at 90 = -10%
    xd, xr = eb.simulate_exit(t, path, "E2")
    assert xr == pytest.approx(-0.10, abs=1e-9)


def test_e2_no_stop_falls_back_to_his_exit():
    """If the -7% stop is never hit, E2 has no upside management -> exit at HIS sell/return."""
    # path must extend THROUGH his sell date so the loop reaches d >= sell before running out.
    t = _trade(100.0, "2024-01-02", "2024-01-18", 0.05)   # sell falls on a bar in the path
    path = _path([100, 101, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113])  # 13 bdays
    xd, xr = eb.simulate_exit(t, path, "E2")
    assert xd == t["sell"]
    assert xr == pytest.approx(0.05)


def test_e3_lets_a_winner_run_past_his_exit():
    """E3 must hold a strong uptrend well past his (early) sell as long as it stays above a
    rising 50-day line, capturing MORE than his actual return."""
    entry = 100.0
    # 60 up-days building a rising 50-SMA, uptrend intact -> never a decisive break.
    closes = [100 + i * 2 for i in range(70)]     # steady climb 100 -> 238
    t = _trade(entry, "2024-01-02", "2024-01-15", 0.06)  # he sold early at +6%
    path = _path(closes, vol=0.005)
    xd, xr = eb.simulate_exit(t, path, "E3")
    assert xr > 0.06, "E3 should hold a runner well past his early exit"
    assert xd > t["sell"], "E3 exit should be later than his sell (or mark-to-last)"


def test_e3_sells_on_decisive_break_below_50sma():
    """After the 50-line rule engages, a decisive close below it exits."""
    entry = 100.0
    up = [100 + i for i in range(60)]             # rising, builds 50-SMA, closes above it
    crash = [160, 150, 120, 100, 90]              # decisive break below the 50-line
    t = _trade(entry, "2024-01-02", "2024-12-31", 0.5)
    path = _path(up + crash, vol=0.002)
    xd, xr = eb.simulate_exit(t, path, "E3")
    # should have exited on the break, not ridden it all the way down to 90
    assert xr < (up[-1] / entry - 1.0)
    assert xr > (100 / entry - 1.0) - 0.15


def test_e4_caps_a_big_winner():
    """E4 must clip a big winner at the profit cap; E3 on the same path must keep more."""
    entry = 100.0
    closes = [100 + i * 3 for i in range(60)]     # rockets up
    t = _trade(entry, "2024-01-02", "2024-06-30", 1.0)
    path = _path(closes, vol=0.005)
    _, xr_e4 = eb.simulate_exit(t, path, "E4")
    _, xr_e3 = eb.simulate_exit(t, path, "E3")
    assert xr_e4 == pytest.approx(eb.PROFIT_CAP, abs=1e-6), "E4 should fill at the cap"
    assert xr_e3 > xr_e4, "E3 (no cap) should keep more than E4 on a runner"


# --------------------------------------------------------------------------- split-exclusion (regression)
def test_split_flagged_trade_falls_back_to_his_actual_under_every_rule():
    """A split-flagged trade must NOT be simulated on its (untrustworthy) forward path: a split
    mid-hold breaks the entry-day rescale ratio and injects a phantom ~split-ratio drop the
    engine would read as a catastrophic stop. Under EVERY non-E1 rule the trade must instead
    fall back to HIS actual exit + return (same treatment as the committed stop-analysis).
    Regression guard for the NVDA phantom -87% bug."""
    entry = 100.0
    # forward path that CRASHES to a 10th of entry (simulates the raw post-split drop). Without
    # the split guard, E2/E3/E4 would read this as ~-90% and return a phantom catastrophic loss.
    path = _path([100, 100, 10, 10, 10, 10], vol=0.0)
    t = _trade(entry, "2024-01-02", "2024-01-05", 0.25)   # his actual: he made +25%
    t["split_flag"] = True
    for rule in ("E2", "E3", "E4"):
        xd, xr = eb.simulate_exit(t, path, rule)
        assert xd == t["sell"], f"{rule}: split-flagged should exit at his sell, not the phantom crash"
        assert xr == pytest.approx(0.25, abs=1e-9), f"{rule}: split-flagged should keep his +25%, got {xr}"
    # sanity: with the flag OFF the same path DOES trigger the catastrophic stop (proves the
    # guard is what protects it, not the path being benign).
    t["split_flag"] = False
    _, xr_unflagged = eb.simulate_exit(t, path, "E3")
    assert xr_unflagged < -0.5, "unflagged trade on a crashing path SHOULD take the big loss"


# --------------------------------------------------------------------------- 2 + 4. portfolio
def test_e1_reproduces_his_realized_book():
    """The whole comparison rests on this: E1 (his actual exits) + his sizing must reproduce
    his journal P&L. Uses the REAL ledger + REAL price cache."""
    trades = eb.load_ledger()
    paths = eb.load_paths()
    timing = eb.load_timing()
    his_pl = sum(t["pl"] for t in trades if t["pl"] is not None)
    r = eb.run_portfolio(trades, paths, timing, "E1", use_timing=False, sizing="his")
    sim_pl = sum(p.pl for p in r["positions"])
    assert r["n_skipped"] == 0, "his-sizing E1 should fit inside START_CAPITAL with no skips"
    assert abs(sim_pl - his_pl) < 1500, f"E1 sim P&L {sim_pl:.0f} != his book {his_pl:.0f}"


def test_cash_frees_and_funds_later_entries():
    """Path-dependence: two trades that can't both fit at once, where the first exits before
    the second's entry, must BOTH be taken (the first's cash funds the second)."""
    paths = {"A": _path([100] * 400), "B": _path([100] * 400)}
    # entry A, exit A (his), then entry B after A's exit
    tA = _trade(100.0, "2024-01-02", "2024-02-01", 0.10, cost=eb.START_CAPITAL * 0.9)
    tA["symbol"] = "A"
    tB = _trade(100.0, "2024-03-01", "2024-04-01", 0.10, cost=eb.START_CAPITAL * 0.9)
    tB["symbol"] = "B"
    r = eb.run_portfolio([tA, tB], paths, [], "E1", use_timing=False, sizing="his")
    syms = {p.symbol for p in r["positions"]}
    assert syms == {"A", "B"}, "second entry should be funded by the first's freed cash"
    assert r["n_skipped"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
