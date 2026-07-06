r"""
test_condor_width_sweep.py — guards for ARM 1, the 0DTE iron-condor STRIKE-WIDTH sweep
(condor_width_sweep).

Pins the CONTRACT, not any strategy outcome:
  (a) NO LOOK-AHEAD  — a future-minute price cannot change an earlier width's managed exit.
  (b) COST IS CHARGED — widening the wings buys a real long option at the ASK: the honest
      (worst-side) entry credit is strictly LESS than the mid credit, and the blended-fill
      band is correctly signed, so a wider condor's cheaper-but-nonzero wing is always paid for.
  (c) FROZEN CONSTANTS — the width grid is the pre-registered 5/10/20/30/50, the 5-pt control is
      present, the entry chassis is inherited verbatim from the control, and management = 25% PT.

The scan tests build tiny in-memory NBBO frames — no warehouse needed, exact arithmetic.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import condor_width_sweep as ws  # noqa: E402
import s6_control as ctrl  # noqa: E402
import condor_management_experiment as cm  # noqa: E402


# --------------------------------------------------------------------------- #
# (c) FROZEN CONSTANTS — the grid, the control width, the entry chassis, the management rule.
# --------------------------------------------------------------------------- #
def test_width_grid_is_the_preregistered_frozen_set():
    assert ws.WIDTHS == (5.0, 10.0, 20.0, 30.0, 50.0)   # exactly these five, NOT swept-to-winner
    assert ctrl.SPREAD_WIDTH in ws.WIDTHS               # the 5-pt control baseline is present
    assert ws.WIDTH_TAGS == ("w5", "w10", "w20", "w30", "w50")


def test_entry_chassis_inherited_verbatim_from_control():
    assert ws.ENTRY_TIME == ctrl.ENTRY_TIME
    assert ws.SETTLEMENT_TIME == ctrl.SETTLEMENT_TIME
    assert ws.TARGET_SHORT_DELTA == ctrl.TARGET_SHORT_DELTA
    assert ws.MIN_ENTRY_CREDIT == ctrl.MIN_ENTRY_CREDIT
    assert ws.WINNER_DEBIT == ctrl.WINNER_DEBIT
    assert ws.STOP_MULTIPLE == ctrl.STOP_MULTIPLE
    # The strike-selection + fill math are the control's own callables (structural agreement).
    assert ws.ctrl._pick_short_by_delta is ctrl._pick_short_by_delta
    assert ws.ctrl._credit_to_open is ctrl._credit_to_open
    assert ws.ctrl._spread_debit_to_close is ctrl._spread_debit_to_close
    # The blended-fill band is the management experiment's own (one code path).
    assert ws.cm._blended_debit_to_close is cm._blended_debit_to_close


def test_management_is_the_fixed_pt25_rule():
    assert ws.PROFIT_TARGET_FRAC == 0.25                 # the prior run's best RISK arm, fixed
    assert ws.FILL_FRACS == (0.0, 0.25, 0.50, 1.0)
    assert ws.HEADLINE_FILL == 0.50


# --------------------------------------------------------------------------- #
# Synthetic entry snapshot + per-strike deltas, so build_condor_at_width can be exercised
# with EXACT arithmetic and multiple widths on the same chain.
# --------------------------------------------------------------------------- #
def _delta_tbl():
    """A per-strike delta table with a 0.15-delta short put at 5000 and short call at 5100,
    plus wing strikes for every swept width so any width can build."""
    rows = [
        {"strike": 5000.0, "right": "PUT", "delta": -0.15},
        {"strike": 5100.0, "right": "CALL", "delta": 0.15},
    ]
    for w in ws.WIDTHS:
        rows.append({"strike": 5000.0 - w, "right": "PUT", "delta": -0.05})
        rows.append({"strike": 5100.0 + w, "right": "CALL", "delta": 0.05})
    return pd.DataFrame(rows)


def _entry_snap():
    """Entry NBBO with a real bid-ask on the short strikes and on every wing strike. Wings get
    a POSITIVE (nonzero) price so 'cost is charged' is a real, non-trivial check, and further-OTM
    wings (wider) are priced CHEAPER — the realistic shape."""
    rows = [
        {"strike": 5000.0, "right": "PUT", "bid": 2.00, "ask": 2.20},
        {"strike": 5100.0, "right": "CALL", "bid": 1.80, "ask": 2.00},
    ]
    # wider wing -> further OTM -> cheaper long option (monotone decreasing with width).
    wing_px = {5.0: (1.40, 1.55), 10.0: (1.00, 1.15), 20.0: (0.60, 0.72),
               30.0: (0.35, 0.45), 50.0: (0.15, 0.22)}
    for w, (b, a) in wing_px.items():
        rows.append({"strike": 5000.0 - w, "right": "PUT", "bid": b, "ask": a})
        rows.append({"strike": 5100.0 + w, "right": "CALL", "bid": b, "ask": a})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# (b) COST IS CHARGED — the long wing is bought at the ASK; honest credit < mid credit; and the
#     net credit RISES with width (cheaper wing) while the wing is still fully paid for.
# --------------------------------------------------------------------------- #
def test_wider_wing_costs_real_premium_honest_below_mid():
    snap, dtbl = _entry_snap(), _delta_tbl()
    for w in ws.WIDTHS:
        build = ws.build_condor_at_width(snap, dtbl, w)
        assert build is not None, f"width {w} should build"
        honest = build["entry_credit"]                       # worst-side (buy wing at ASK)
        mid = cm._credit_mid(snap, build["legs"])
        # Honest credit must be STRICTLY less than mid: the long wing's bid/ask spread is paid.
        assert honest < mid, f"width {w}: honest credit {honest} must be below mid {mid}"
        # The blended full-cross credit must equal the honest control credit exactly (anchored).
        assert ws.cm._blended_credit_to_open(snap, build["legs"], 1.0) == pytest.approx(honest)
        # Short strikes are the frozen 0.15-delta picks regardless of width.
        assert build["short_put_k"] == 5000.0
        assert build["short_call_k"] == 5100.0
        # Long wing sits exactly WIDTH away from the short — the ONLY thing that changed.
        assert build["long_put_k"] == pytest.approx(5000.0 - w)
        assert build["long_call_k"] == pytest.approx(5100.0 + w)


def test_credit_rises_with_width_because_the_bought_wing_gets_cheaper():
    """The mechanism under test: a further-OTM (wider) long wing costs less to buy, so the NET
    credit collected rises with width. This is exactly why the sweep might attack the cost drag."""
    snap, dtbl = _entry_snap(), _delta_tbl()
    credits = [ws.build_condor_at_width(snap, dtbl, w)["entry_credit"] for w in ws.WIDTHS]
    assert credits == sorted(credits), "net credit must be non-decreasing as the wing widens"
    assert credits[-1] > credits[0], "50-pt condor must collect strictly more credit than 5-pt"


# --------------------------------------------------------------------------- #
# Synthetic per-minute NBBO with a scriptable debit-to-close path (only the short put carries it).
# --------------------------------------------------------------------------- #
def _synthetic_nbbo(entry_minute, debit_path, legs):
    (spk, _, _), (lpk, _, _), (sck, _, _), (lck, _, _) = legs
    rows = []
    for i, deb in enumerate(debit_path):
        m = entry_minute + pd.Timedelta(minutes=i + 1)
        rows += [
            {"minute": m, "strike": spk, "right": "PUT", "bid": deb, "ask": deb},
            {"minute": m, "strike": lpk, "right": "PUT", "bid": 0.0, "ask": 0.0},
            {"minute": m, "strike": sck, "right": "CALL", "bid": 0.0, "ask": 0.0},
            {"minute": m, "strike": lck, "right": "CALL", "bid": 0.0, "ask": 0.0},
        ]
    return pd.DataFrame(rows)


_LEGS = [(5000.0, "PUT", +1), (4990.0, "PUT", -1), (5100.0, "CALL", +1), (5110.0, "CALL", -1)]


# --------------------------------------------------------------------------- #
# (a) NO LOOK-AHEAD — a catastrophic future minute cannot change an exit that already bound.
# --------------------------------------------------------------------------- #
def test_no_lookahead_future_minute_cannot_change_an_earlier_exit():
    """The 25% profit-target binds at minute 2; anything after must NOT change the booked exit.
    Run with a benign tail and with a catastrophic spike after the target minute — identical."""
    entry = pd.Timestamp(dt.datetime(2024, 1, 2, 14, 0))
    settle = pd.Timestamp(dt.datetime(2024, 1, 2, 16, 0))
    credit = 1.00                       # 25% target => debit <= 0.75
    benign, _ = ws.scan_pt25_exit_at_fill(
        _synthetic_nbbo(entry, [0.90, 0.70, 0.60, 0.60], _LEGS), _LEGS,
        credit, 1.0, entry, settle)
    catastrophe, _ = ws.scan_pt25_exit_at_fill(
        _synthetic_nbbo(entry, [0.90, 0.70, 9.99, 9.99], _LEGS), _LEGS,
        credit, 1.0, entry, settle)
    assert benign["exit_reason"] == catastrophe["exit_reason"] == "target"
    assert benign["exit_debit"] == catastrophe["exit_debit"] == pytest.approx(0.70)
    assert benign["hold_min"] == catastrophe["hold_min"] == pytest.approx(2.0)


def test_pt25_target_fires_at_first_touch_and_is_charged_the_honest_debit():
    """Credit 1.00; path 0.90,0.70,0.40. 25% target => debit<=0.75: first at 0.70 (minute 2).
    The booked debit is that honest 0.70 (first-touch), hold 2 minutes."""
    entry = pd.Timestamp(dt.datetime(2024, 1, 2, 14, 0))
    settle = pd.Timestamp(dt.datetime(2024, 1, 2, 16, 0))
    res, _ = ws.scan_pt25_exit_at_fill(
        _synthetic_nbbo(entry, [0.90, 0.70, 0.40], _LEGS), _LEGS, 1.00, 1.0, entry, settle)
    assert res["exit_reason"] == "target"
    assert res["exit_debit"] == pytest.approx(0.70)
    assert res["hold_min"] == pytest.approx(2.0)


def test_stop_binds_before_the_target():
    """A 2x-credit stop (debit >= 3.00 for credit 1.00) fires at minute 1 before any target."""
    entry = pd.Timestamp(dt.datetime(2024, 1, 2, 14, 0))
    settle = pd.Timestamp(dt.datetime(2024, 1, 2, 16, 0))
    res, _ = ws.scan_pt25_exit_at_fill(
        _synthetic_nbbo(entry, [3.50, 0.10, 0.10], _LEGS), _LEGS, 1.00, 1.0, entry, settle)
    assert res["exit_reason"] == "stop"
    assert res["exit_debit"] == pytest.approx(3.50)


def test_settle_when_no_rule_binds():
    """A flat path that never wins/stops/targets closes at the last mark ('settle')."""
    entry = pd.Timestamp(dt.datetime(2024, 1, 2, 14, 0))
    settle = pd.Timestamp(dt.datetime(2024, 1, 2, 16, 0))
    res, _ = ws.scan_pt25_exit_at_fill(
        _synthetic_nbbo(entry, [0.90, 0.85, 0.80], _LEGS), _LEGS, 1.00, 1.0, entry, settle)
    assert res["exit_reason"] == "settle"
    assert res["exit_debit"] == pytest.approx(0.80)
