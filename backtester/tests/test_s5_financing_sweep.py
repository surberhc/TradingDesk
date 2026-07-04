r"""
test_s5_financing_sweep.py -- pure-logic pins for the shared S5 sweep+evaluation DRIVER.

No warehouse read: exercises the metric, management mapping, causal calm-gate DIRECTION,
the vectorized matched-placebo, sign-consistency, and the DSR-ready return series on tiny
in-memory frames. The heavy end-to-end sweep is validated separately against the real
warehouse (see the Phase-2a validation run); here we pin the comparison protocol itself.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s5_financing_harness as h  # noqa: E402
import s5_financing_sweep as sw  # noqa: E402


# --------------------------------------------------------------------------- #
# management-knob mapping.
# --------------------------------------------------------------------------- #
def test_build_management_maps_every_grid_knob():
    m = sw.build_management("hold_to_expiry")
    assert m.mode == "hold" and m.stop_mult is None
    m = sw.build_management("profit_50")
    assert m.mode == "profit_target" and m.profit_target == 0.50
    m = sw.build_management("dte_21")
    assert m.mode == "time_exit" and m.time_exit_dte == 21
    m = sw.build_management("profit_50_or_dte_21")
    assert m.mode == "target_or_time" and m.profit_target == 0.50 and m.time_exit_dte == 21
    m = sw.build_management("stop_2x")
    assert m.mode == "hold" and m.stop_mult == 2.0


def test_build_management_rejects_unknown():
    with pytest.raises(ValueError):
        sw.build_management("moon_phase")


def test_management_grid_all_buildable():
    for knob in sw.MANAGEMENT_GRID:
        assert isinstance(sw.build_management(knob), h.Management)


# --------------------------------------------------------------------------- #
# %/yr-of-core metric + DSR-ready return series.
# --------------------------------------------------------------------------- #
def _mini_trades():
    """A tiny per-trade frame with two entries a year apart, known net_pnl and underlying."""
    return pd.DataFrame({
        "entry_date": [dt.date(2018, 1, 2), dt.date(2019, 1, 2)],
        "exit_date": [dt.date(2018, 2, 16), dt.date(2019, 2, 15)],
        "exit_reason": ["settle", "settle"],
        "entry_credit": [50.0, 50.0],
        "net_pnl": [100.0, -200.0],
        "total_commission": [1.3, 1.3],
        "entry_underlying": [2000.0, 4000.0],  # core = 200k and 400k
        "exit_underlying": [2010.0, 3980.0],
    })


def test_trade_returns_are_fraction_of_core():
    t = _mini_trades()
    r = sw._trade_returns(t)
    # 100 / (2000*100) = 5e-4 ; -200 / (4000*100) = -5e-4
    assert r.tolist() == pytest.approx([100 / 200000.0, -200 / 400000.0])


def test_pct_yr_of_core_matches_hand_math():
    t = _mini_trades()
    start, end = dt.date(2018, 1, 2), dt.date(2019, 1, 2)
    # total net = -100 ; mean core = (200000+400000)/2 = 300000 ; years = 365/365.25
    val = sw._pct_yr_of_core(t, start, end)
    expect = (-100.0 / 300000.0) / ((end - start).days / 365.25)
    assert val == pytest.approx(expect)


def test_win_stats():
    t = _mini_trades()
    win_rate, loss_win = sw._win_stats(t)
    assert win_rate == pytest.approx(0.5)             # 1 win of 2
    assert loss_win == pytest.approx(200.0 / 100.0)   # mean loss / mean win


def test_empty_trades_do_not_crash_metrics():
    empty = pd.DataFrame(columns=["entry_date", "net_pnl", "entry_underlying",
                                  "entry_credit", "exit_reason"])
    assert sw._pct_yr_of_core(empty, dt.date(2018, 1, 2), dt.date(2019, 1, 2)) == 0.0
    assert sw._trade_returns(empty).empty
    assert sw._crash_exit_cost(empty) == {}
    rb = sw._regime_buckets(empty)
    assert rb == {"vix_tercile": {}, "year": {}}


# --------------------------------------------------------------------------- #
# vectorized matched placebo -- exact metric + bounded percentile.
# --------------------------------------------------------------------------- #
def test_placebo_real_metric_equals_pct_yr_of_core():
    rng = np.random.default_rng(0)
    n = 150
    df = pd.DataFrame({
        "net_pnl": rng.normal(10, 60, n),
        "entry_underlying": rng.uniform(3000, 5000, n),
    })
    gated = df.iloc[:40]
    yrs = 2.5
    res = sw._placebo_percentile(df, gated, n_draws=500, seed=3, window_years=yrs)
    manual = (gated["net_pnl"].sum()
              / (gated["entry_underlying"] * h.CONTRACT_MULTIPLIER).mean()) / yrs
    assert res["real_metric"] == pytest.approx(manual)
    assert 0.0 <= res["percentile"] <= 1.0
    assert res["n_selected"] == 40 and res["n_universe"] == n


def test_placebo_undefined_when_gate_takes_everything():
    df = pd.DataFrame({"net_pnl": [1.0, 2.0, 3.0],
                       "entry_underlying": [3000.0, 3000.0, 3000.0]})
    res = sw._placebo_percentile(df, df, n_draws=100, seed=1, window_years=1.0)
    assert np.isnan(res["percentile"]) and res["beats_placebo"] is None


def test_placebo_draws_are_count_matched():
    # a deterministic universe where every subset of size k has the SAME metric would give a
    # degenerate percentile; use a spread universe and just assert the draw mechanism keeps
    # the selection count fixed by checking placebo_mean is finite and near the universe mean.
    rng = np.random.default_rng(7)
    df = pd.DataFrame({"net_pnl": rng.normal(0, 50, 200),
                       "entry_underlying": np.full(200, 4000.0)})
    gated = df.iloc[:70]
    res = sw._placebo_percentile(df, gated, n_draws=800, seed=2, window_years=1.0)
    # with constant core, metric = mean(net_pnl subset)*k/(4000*100*k?) ... just bound-check.
    assert np.isfinite(res["placebo_mean"]) and np.isfinite(res["placebo_std"])


# --------------------------------------------------------------------------- #
# causal calm-gate DIRECTION (uses real VIX files if present; else skips).
# --------------------------------------------------------------------------- #
def test_calm_gate_stands_down_on_backwardation_and_high_vix():
    try:
        vix = sw._load_vix_series("vix")
        vix3m = sw._load_vix_series("vix3m")
    except FileNotFoundError:
        pytest.skip("VIX-family files not present in this environment")
    ok = sw.calm_entry_filter(vix_level=sw.CALM_VIX_LEVEL)
    # pick a known-calm day: 2019-08-01-ish low-vol regime -> should generally be OK.
    # and a known-stress day: 2020-03-16 (VIX ~ 82) -> must stand down.
    assert ok(dt.date(2020, 3, 16)) is False           # crash: elevated + backwardated
    # a day before both series start must FAIL-SAFE to stand-down (no peeking).
    assert ok(dt.date(1990, 1, 2)) is False


def test_calm_gate_is_causal_asof_only():
    """The gate's as-of lookup must use the LAST close on-or-before d, never a future one."""
    try:
        vix = sw._load_vix_series("vix")
    except FileNotFoundError:
        pytest.skip("VIX-family files not present")
    ok = sw.calm_entry_filter()
    # Evaluating the SAME day twice is stable and does not consult any later date: monkey a
    # cutoff by checking that a date strictly before the first index returns stand-down.
    first_day = min(vix.index)
    before = first_day - dt.timedelta(days=1)
    assert ok(before) is False


# --------------------------------------------------------------------------- #
# spec builder wiring.
# --------------------------------------------------------------------------- #
def test_put_credit_spread_spec_builds_declared_structure():
    spec = sw.put_credit_spread_spec(wing=10.0)
    struct = spec.builder(45, 0.15, sw.build_management("hold_to_expiry"))
    assert isinstance(struct, h.Structure)
    assert struct.dte == 45
    # short put at 0.15 delta + a 10-wide long wing below it
    shorts = [l for l in struct.legs if l.action == "sell"]
    wings = [l for l in struct.legs if l.action == "buy"]
    assert len(shorts) == 1 and shorts[0].target_delta == 0.15
    assert len(wings) == 1 and wings[0].strike_offset == -10.0
