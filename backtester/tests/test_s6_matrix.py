r"""
test_s6_matrix.py — unit tests for the S6 sensitivity matrix (s6_matrix).

These pin the MECHANICS and the ANTI-LOOK-AHEAD contract, not any strategy outcome:
  * the day classifier uses ONLY a STRICTLY-PRIOR EOD row (no same-day, no future);
  * VIX term-structure label uses the standard 1.0 crossover of VIX9D/VIX;
  * the multi-exit re-scan reproduces the control's 2x outcome byte-for-byte and resolves
    3x / hold on the SAME causal minute-walk (higher stop can only help a stopped day);
  * a raised/removed stop never changes a winner/settle day (carried forward untouched);
  * plateau/peak classification demands both-halves + neighbor agreement + n>=THIN_N.

All classifier tests build tiny in-memory EOD frames; exit tests build synthetic NBBO —
no warehouse needed, exact arithmetic.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s6_matrix as mx  # noqa: E402
import s6_control as ctrl  # noqa: E402


# --------------------------------------------------------------------------- #
# Day classifier — NO LOOK-AHEAD (the load-bearing correctness test).
# --------------------------------------------------------------------------- #
def _clf(gamma_rows, vix_rows) -> mx.DayClassifier:
    g = pd.DataFrame(gamma_rows)  # cols: date, gamma_state, net_gex
    v = pd.DataFrame(vix_rows)    # cols: date, ratio
    return mx.DayClassifier(gamma=g.sort_values("date").reset_index(drop=True),
                            vix=v.sort_values("date").reset_index(drop=True))


def test_classifier_uses_strictly_prior_eod_row():
    """For trade-day D the label must come from the last EOD row BEFORE D — never D itself,
    never a future row. We plant a DIFFERENT value ON D and AFTER D and prove they are
    ignored."""
    D = dt.date(2024, 3, 15)
    clf = _clf(
        gamma_rows=[
            {"date": dt.date(2024, 3, 13), "gamma_state": "Positive", "net_gex": 1.0},
            {"date": dt.date(2024, 3, 14), "gamma_state": "Negative", "net_gex": -1.0},  # prior
            {"date": D,                    "gamma_state": "Positive", "net_gex": 9.0},   # same-day: MUST be ignored
            {"date": dt.date(2024, 3, 18), "gamma_state": "Positive", "net_gex": 9.0},   # future: MUST be ignored
        ],
        vix_rows=[
            {"date": dt.date(2024, 3, 14), "ratio": 1.20},   # prior => backwardation
            {"date": D,                    "ratio": 0.50},   # same-day: MUST be ignored
            {"date": dt.date(2024, 3, 18), "ratio": 0.50},   # future: MUST be ignored
        ],
    )
    c = clf.classify(D)
    assert c["gamma_regime"] == "negative"      # from 3/14, not the same-day Positive
    assert c["vix_regime"] == "backwardation"   # from 3/14 ratio 1.20, not same-day 0.50


def test_classifier_unknown_before_history_starts():
    D = dt.date(2020, 1, 1)
    clf = _clf(
        gamma_rows=[{"date": dt.date(2024, 1, 2), "gamma_state": "Positive", "net_gex": 1.0}],
        vix_rows=[{"date": dt.date(2024, 1, 2), "ratio": 0.9}],
    )
    c = clf.classify(D)
    assert c["gamma_regime"] == "unknown"
    assert c["vix_regime"] == "unknown"


def test_vix_regime_uses_standard_1p0_crossover():
    clf = _clf(
        gamma_rows=[{"date": dt.date(2024, 1, 2), "gamma_state": "Positive", "net_gex": 1.0}],
        vix_rows=[
            {"date": dt.date(2024, 1, 2), "ratio": 0.99},   # below 1 -> contango
            {"date": dt.date(2024, 1, 3), "ratio": 1.01},   # above 1 -> backwardation
        ],
    )
    assert clf.vix_regime(dt.date(2024, 1, 3)) == "contango"        # prior row 0.99
    assert clf.vix_regime(dt.date(2024, 1, 4)) == "backwardation"   # prior row 1.01
    assert mx.VIX_CROSSOVER == 1.0                                  # standard, not tuned


# --------------------------------------------------------------------------- #
# Multi-exit re-scan — reproduces control 2x + resolves 3x / hold causally.
# --------------------------------------------------------------------------- #
def _grid(day, rows):
    out = []
    for r in rows:
        hh, mm = map(int, r["minute"].split(":"))
        out.append({"minute": pd.Timestamp(dt.datetime.combine(day, dt.time(hh, mm))),
                    "strike": float(r["strike"]), "right": r["right"],
                    "bid": float(r["bid"]), "ask": float(r["ask"])})
    return pd.DataFrame(out)


def test_multiexit_matches_control_2x_and_orders_stops():
    """Credit 1.00. 2x stop debit=3.00, 3x stop debit=4.00. Build a path that crosses 3.00
    then 4.00 then blows out: 2x stops first (3.10), 3x stops later (4.20), hold rides to
    settlement. The 2x branch must EQUAL the control's own _scan_exit."""
    day = dt.date(2024, 6, 3)
    entry = pd.Timestamp(dt.datetime.combine(day, dt.time(14, 0)))
    settle = pd.Timestamp(dt.datetime.combine(day, dt.time(16, 0)))
    nbbo = _grid(day, [
        {"minute": "14:30", "strike": 100, "right": "PUT", "bid": 3.10, "ask": 3.10},
        {"minute": "14:30", "strike": 95,  "right": "PUT", "bid": 0.00, "ask": 0.00},  # debit 3.10 -> 2x stop
        {"minute": "14:45", "strike": 100, "right": "PUT", "bid": 4.20, "ask": 4.20},
        {"minute": "14:45", "strike": 95,  "right": "PUT", "bid": 0.00, "ask": 0.00},  # debit 4.20 -> 3x stop
        {"minute": "16:00", "strike": 100, "right": "PUT", "bid": 2.00, "ask": 2.00},
        {"minute": "16:00", "strike": 95,  "right": "PUT", "bid": 0.00, "ask": 0.00},  # debit 2.00 settle
    ])
    legs = [(100.0, "PUT", +1), (95.0, "PUT", -1)]
    exits = mx._scan_all_exits(nbbo, legs, 1.00, entry, settle)

    # control's own 2x scan for the same inputs:
    ctrl_2x = ctrl._scan_exit(nbbo, legs, 1.00, entry, settle)
    assert exits["stop_2x"] == ctrl_2x
    assert exits["stop_2x"][0] == "stop" and exits["stop_2x"][2] == pytest.approx(3.10)
    assert exits["stop_3x"][0] == "stop" and exits["stop_3x"][2] == pytest.approx(4.20)
    assert exits["hold"][0] == "settle" and exits["hold"][2] == pytest.approx(2.00)


def test_higher_stop_can_rescue_a_2x_stopped_day_to_a_winner():
    """A day that 2x-stops but then RECOVERS to <=0.05: under 3x/hold it becomes a winner.
    This is the whole point of widening the stop — proven mechanically, not assumed."""
    day = dt.date(2024, 6, 4)
    entry = pd.Timestamp(dt.datetime.combine(day, dt.time(14, 0)))
    settle = pd.Timestamp(dt.datetime.combine(day, dt.time(16, 0)))
    nbbo = _grid(day, [
        {"minute": "14:30", "strike": 100, "right": "PUT", "bid": 3.10, "ask": 3.10},
        {"minute": "14:30", "strike": 95,  "right": "PUT", "bid": 0.00, "ask": 0.00},  # debit 3.10 -> 2x stop
        {"minute": "15:30", "strike": 100, "right": "PUT", "bid": 0.05, "ask": 0.05},
        {"minute": "15:30", "strike": 95,  "right": "PUT", "bid": 0.00, "ask": 0.00},  # debit 0.05 -> winner
    ])
    legs = [(100.0, "PUT", +1), (95.0, "PUT", -1)]
    exits = mx._scan_all_exits(nbbo, legs, 1.00, entry, settle)
    assert exits["stop_2x"][0] == "stop"       # 2x still stops at 3.10
    assert exits["stop_3x"] == ("winner", pd.Timestamp(dt.datetime.combine(day, dt.time(15, 30))), pytest.approx(0.05))
    assert exits["hold"][0] == "winner"


def test_legs_from_row_rebuilds_each_structure():
    bp = mx._legs_from_row(pd.Series({"structure": "bull_put", "short_strike": 100.0,
                                      "long_strike": 95.0, "short_strike_2": np.nan,
                                      "long_strike_2": np.nan}))
    assert bp == [(100.0, "PUT", +1), (95.0, "PUT", -1)]
    bc = mx._legs_from_row(pd.Series({"structure": "bear_call", "short_strike": 100.0,
                                      "long_strike": 105.0, "short_strike_2": np.nan,
                                      "long_strike_2": np.nan}))
    assert bc == [(100.0, "CALL", +1), (105.0, "CALL", -1)]
    ic = mx._legs_from_row(pd.Series({"structure": "iron_condor", "short_strike": 100.0,
                                      "long_strike": 95.0, "short_strike_2": 110.0,
                                      "long_strike_2": 115.0}))
    assert ic == [(100.0, "PUT", +1), (95.0, "PUT", -1),
                  (110.0, "CALL", +1), (115.0, "CALL", -1)]


# --------------------------------------------------------------------------- #
# Plateau / peak classification — demands robustness, rejects isolated peaks.
# --------------------------------------------------------------------------- #
def _matrix_row(exit, total, train, test, n=100, **kw):
    base = {"structure": "bull_put", "gamma": "positive", "vix": "contango",
            "exit": exit, "n": n, "total_pnl_$": total,
            "train_pnl_$": train, "test_pnl_$": test}
    base.update(kw)
    return base


def test_plateau_requires_both_halves_and_neighbor():
    # A cell profitable in both halves AND with a profitable neighbor exit -> PLATEAU.
    m = pd.DataFrame([
        _matrix_row("stop_2x", 500, 200, 300, n=80),
        _matrix_row("stop_3x", 400, 150, 250, n=80),
        _matrix_row("hold",    -50, -20, -30, n=80),
    ])
    out = mx.classify_plateau_peak(m).set_index("exit")["classification"]
    assert out["stop_2x"] == "PLATEAU"     # both halves + neighbor stop_3x profitable
    assert out["stop_3x"] == "PLATEAU"     # both halves + neighbor stop_2x profitable
    assert out["hold"] == "loss"


def test_one_half_only_is_a_peak_not_a_plateau():
    m = pd.DataFrame([
        _matrix_row("stop_2x", 100, 300, -200, n=80),
        _matrix_row("stop_3x", 90,  280, -190, n=80),
        _matrix_row("hold",    -10, -5,  -5,  n=80),
    ])
    out = mx.classify_plateau_peak(m).set_index("exit")["classification"]
    assert out["stop_2x"].startswith("PEAK")   # test half negative -> not robust
    assert "one-half" in out["stop_2x"]


def test_thin_cell_is_unusable_peak():
    m = pd.DataFrame([
        _matrix_row("stop_2x", 500, 200, 300, n=10),
        _matrix_row("stop_3x", 400, 150, 250, n=10),
    ])
    out = mx.classify_plateau_peak(m).set_index("exit")["classification"]
    assert out["stop_2x"] == "PEAK(thin)"
    assert mx.THIN_N == 30
