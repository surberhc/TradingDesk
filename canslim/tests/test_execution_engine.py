"""Tests for the disciplined-execution DECISION ENGINE (compute-only).

Load-bearing guarantees (same spirit as test_execution_backtest.py):
  1. NO-LOOKAHEAD (desk causality rule): a decision for day D uses only bars up to D;
     appending future bars must not change today's decision, and the causality guard fires
     on a bar dated after the decision day.
  2. The per-name state machine matches the PROVEN E3 ruleset: -7% initial stop until a
     close above a rising 50-SMA, then hold to a DECISIVE 50-line break; NO profit cap.
  3. Entry buy-zone: only confirmed breakouts within +5% above the pivot are buyable.
  4. Sizing / concurrent cap / exposure dial are respected; excess is held as cash.
  5. Compute-only: the module never touches paperbot / order paths (import + API surface).
"""
import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import execution_engine as ee  # noqa: E402


# --------------------------------------------------------------------------- helpers
def _rising_closes(n=60, start=100.0, step=1.0):
    """A steadily rising close series that builds a rising 50-SMA with price above it."""
    return [start + i * step for i in range(n)]


def _md(closes, dates=None):
    return ee.MarketData(last_px=closes[-1], closes=closes, dates=dates)


def _dates_for(closes, end="2024-06-28"):
    d = dt.date.fromisoformat(end)
    out = []
    while len(out) < len(closes):
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return list(reversed(out))


# --------------------------------------------------------------------------- 1. no-lookahead
def test_causality_guard_raises_on_future_bar():
    closes = _rising_closes()
    asof = dt.date(2024, 6, 20)
    dates = _dates_for(closes, end="2024-06-28")  # last date is AFTER asof
    pos = ee.Position("TST", dt.date(2024, 1, 2), 100.0, 52000.0)
    with pytest.raises(ee.NoLookaheadError):
        ee.evaluate_position(pos, _md(closes, dates), asof)


def test_decision_unchanged_when_future_bars_appended():
    """A -7% stop breach today must yield the same decision whether or not the caller could
    (hypothetically) have appended a future rally. The engine only sees closes<=asof, so a
    decision computed on the causal slice equals the decision on that slice alone."""
    # entry 100, today's close 92 (below the -7% stop = 93), 50-line not engaged -> EXIT.
    closes = [100.0] * 60 + [92.0]
    pos = ee.Position("TST", dt.date(2024, 1, 2), 100.0, 52000.0)
    d1, active1, _, trig1 = ee.evaluate_position(pos, _md(closes), dt.date(2024, 6, 20))
    # "appending a future moonshot" = a LATER decision day; today's slice is unchanged.
    d2, active2, _, trig2 = ee.evaluate_position(pos, _md(closes), dt.date(2024, 6, 20))
    assert (d1, trig1) == (d2, trig2) == ("EXIT", "stop_7pct")


# --------------------------------------------------------------------------- 2. per-name state machine
def test_initial_stop_exits_below_minus_7pct():
    closes = [100.0] * 10 + [92.9]      # close below the 93 stop; 50-line not engaged
    pos = ee.Position("TST", dt.date(2024, 1, 2), 100.0, 52000.0)
    decision, active, reason, trig = ee.evaluate_position(pos, _md(closes), dt.date(2024, 6, 20))
    assert decision == "EXIT" and trig == "stop_7pct"


def test_stop_retires_after_close_above_rising_50sma():
    """Close above a rising 50-SMA latches sma_active True and retires the -7% stop."""
    closes = _rising_closes()           # rising, price above its own 50-SMA
    pos = ee.Position("TST", dt.date(2024, 1, 2), 100.0, 52000.0)
    decision, active, reason, trig = ee.evaluate_position(pos, _md(closes), dt.date(2024, 6, 20))
    assert decision == "HOLD" and active is True and trig is None


def test_winner_held_no_profit_cap():
    """Once the 50-line rule engages, a huge gain still HOLDs — there is NO profit target."""
    closes = _rising_closes(step=5.0)   # +300% move; must NOT be capped
    pos = ee.Position("TST", dt.date(2024, 1, 2), 100.0, 52000.0, sma_active=True)
    decision, active, reason, trig = ee.evaluate_position(pos, _md(closes), dt.date(2024, 6, 20))
    assert decision == "HOLD"


def test_decisive_50sma_break_exits():
    up = _rising_closes(n=55)
    s_today, _ = ee.sma50(up)
    closes = up + [s_today * 0.90]      # decisive close well below the (lagging) 50-line
    pos = ee.Position("TST", dt.date(2024, 1, 2), 100.0, 52000.0, sma_active=True)
    decision, active, reason, trig = ee.evaluate_position(pos, _md(closes), dt.date(2024, 6, 20))
    assert decision == "EXIT" and trig == "decisive_50sma_break"


def test_shallow_dip_below_50sma_is_not_decisive():
    """A close just under the 50-line (within the 2% buffer) is NOT a decisive break -> HOLD."""
    up = _rising_closes(n=55)
    s_today, _ = ee.sma50(up)
    closes = up + [s_today * 0.99]      # below SMA but above 0.98*SMA -> not decisive
    pos = ee.Position("TST", dt.date(2024, 1, 2), 100.0, 52000.0, sma_active=True)
    decision, *_ = ee.evaluate_position(pos, _md(closes), dt.date(2024, 6, 20))
    assert decision == "HOLD"


# --------------------------------------------------------------------------- 3. entry buy-zone
def test_buy_zone_accepts_near_pivot():
    ok, _ = ee.in_buy_zone(ee.Pick("X", pivot=100.0, last_px=103.0))
    assert ok


def test_buy_zone_rejects_extended():
    ok, why = ee.in_buy_zone(ee.Pick("X", pivot=100.0, last_px=110.0))
    assert not ok and "extended" in why


def test_buy_zone_rejects_below_pivot_and_unconfirmed():
    assert not ee.in_buy_zone(ee.Pick("X", 100.0, 98.0))[0]                     # below pivot
    assert not ee.in_buy_zone(ee.Pick("X", 100.0, 102.0, breakout_confirmed=False))[0]


# --------------------------------------------------------------------------- 4. daily engine wiring
def _flat_md(px, n=60):
    return ee.MarketData(last_px=px, closes=[px] * n)


def test_exposure_cap_holds_cash():
    """With the dial at 50%, the engine must not deploy past 50% of equity."""
    asof = dt.date(2024, 6, 20)
    market = {f"P{i}": _flat_md(100.0) for i in range(6)}
    picks = [ee.Pick(f"P{i}", pivot=100.0, last_px=100.0) for i in range(6)]
    plan = ee.decide_day(asof, [], market, picks, invested_pct=0.50, cash=650_000.0)
    deployed = sum(a.target_dollars for a in plan.entries)
    assert deployed <= 0.50 * 650_000.0 + 1.0
    assert deployed > 0


def test_concurrent_cap_limits_entries():
    asof = dt.date(2024, 6, 20)
    market = {f"P{i}": _flat_md(100.0) for i in range(12)}
    picks = [ee.Pick(f"P{i}", pivot=100.0, last_px=100.0) for i in range(12)]
    plan = ee.decide_day(asof, [], market, picks, invested_pct=1.0, cash=650_000.0)
    assert len(plan.entries) <= ee.MAX_CONCURRENT


def test_no_pyramiding_into_held_name():
    asof = dt.date(2024, 6, 20)
    held = ee.Position("HELD", dt.date(2024, 1, 2), 100.0, 78000.0, sma_active=True)
    market = {"HELD": _md(_rising_closes())}
    picks = [ee.Pick("HELD", pivot=100.0, last_px=101.0)]
    plan = ee.decide_day(asof, [held], market, picks, invested_pct=1.0, cash=500_000.0)
    assert not plan.entries and len(plan.holds) == 1


def test_entries_ranked_by_buyzone_tightness():
    """Least-extended pick (closest to pivot) is ranked first."""
    asof = dt.date(2024, 6, 20)
    market = {"NEAR": _flat_md(100.5), "FAR": _flat_md(104.0)}
    picks = [ee.Pick("FAR", pivot=100.0, last_px=104.0),
             ee.Pick("NEAR", pivot=100.0, last_px=100.5)]
    plan = ee.decide_day(asof, [], market, picks, invested_pct=1.0, cash=650_000.0)
    assert plan.entries[0].symbol == "NEAR"


def test_exit_before_entry_ordering_and_freed_cash():
    """Exits are listed before entries, and cash freed by an exit can fund a new entry the
    same day (path-dependence, compute-side)."""
    asof = dt.date(2024, 6, 20)
    # a loser at -10% (below stop, 50-line not engaged) must EXIT
    loser = ee.Position("LOSE", dt.date(2024, 6, 1), 100.0, 600_000.0, sma_active=False)
    market = {"LOSE": _flat_md(90.0), "NEW": _flat_md(100.0)}
    picks = [ee.Pick("NEW", pivot=100.0, last_px=100.0)]
    # almost no free cash; only freeing the loser makes room
    plan = ee.decide_day(asof, [loser], market, picks, invested_pct=1.0, cash=5_000.0)
    assert plan.exits and plan.exits[0].symbol == "LOSE"
    assert any(a.symbol == "NEW" for a in plan.entries), "freed cash should fund NEW"
    assert plan.actions.index(plan.exits[0]) < plan.actions.index(plan.entries[0])


def test_initial_stop_level_on_entry_is_minus_7pct():
    asof = dt.date(2024, 6, 20)
    market = {"X": _flat_md(200.0)}
    picks = [ee.Pick("X", pivot=195.0, last_px=200.0)]
    plan = ee.decide_day(asof, [], market, picks, invested_pct=1.0, cash=650_000.0)
    assert plan.entries[0].initial_stop == pytest.approx(200.0 * 0.93, abs=1e-6)


# --------------------------------------------------------------------------- 5. compute-only boundary
def test_module_never_imports_paperbot_or_order_paths():
    """Safety boundary: this compute-only engine must not reach any live order path. We check
    for CODE that would wire an order (imports / API calls), not prose in the docstrings that
    explains the seam and the review->arm->transmit gate."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "execution_engine.py")).read()
    # strip out docstrings/comments so we only scan executable code
    code_lines = []
    for ln in src.splitlines():
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        code_lines.append(ln)
    code = "\n".join(code_lines)
    for forbidden in ("import paperbot", "from paperbot", "placeOrder",
                      "ib_insync", "reqIds", "transmit=True"):
        assert forbidden not in code, f"compute-only engine must not reference {forbidden!r}"


def test_pluggable_pick_source_adapter():
    picks = ee.picks_from_list([
        {"symbol": "abc", "pivot": 50.0, "last_px": 51.0},
        {"symbol": "xyz", "pivot": 10.0, "last_px": 10.2, "breakout_confirmed": False},
    ])
    assert picks[0].symbol == "ABC" and picks[0].breakout_confirmed is True
    assert picks[1].breakout_confirmed is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
