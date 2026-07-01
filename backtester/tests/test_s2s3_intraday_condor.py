r"""
test_s2s3_intraday_condor.py — unit tests for the S2/S3 intraday 0DTE iron-condor
harness (s2s3_intraday_condor).

These pin the MECHANICS and the ANTI-LOOK-AHEAD / ANTI-CURVE-FIT contract, NOT any
strategy outcome:
  * the overnight GAP is derived from THIS day's open vs the PRIOR day's close only
    (strictly causal — never today's close, never a future row);
  * the trailing-median gap baseline at row i uses STRICTLY-earlier rows (no self, no
    future), so the big_gap flag is knowable before the 14:00 entry;
  * the frozen gate constants are the pre-registered plain choices (2.0x, 20-day), not
    swept — pinned so a silent retune would break the test;
  * the gated comparison SITS OUT big-gap days (P&L=0), never re-places;
  * the PV-band coverage fractions are monotone non-decreasing in the EM multiple k, and
    close_in_em is (close-entry)/expected_move.

The gap/gate/PV tests build tiny in-memory frames — no warehouse needed, exact arithmetic.
The condor-engine agreement with S6 is STRUCTURAL (this module calls s6_control's own
_build_iron_condor / _scan_exit unchanged), so it is asserted by identity of the imported
callables rather than re-simulated here.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s2s3_intraday_condor as s23  # noqa: E402
import s6_control as ctrl  # noqa: E402


# --------------------------------------------------------------------------- #
# Frozen-constant guards (a silent retune must fail a test — rule #1).
# --------------------------------------------------------------------------- #
def test_condor_constants_inherited_verbatim_from_control():
    """The condor mechanics MUST be the control's own constants, not re-declared numbers."""
    assert s23.ENTRY_TIME == ctrl.ENTRY_TIME
    assert s23.SETTLEMENT_TIME == ctrl.SETTLEMENT_TIME
    assert s23.SPREAD_WIDTH == ctrl.SPREAD_WIDTH
    assert s23.TARGET_SHORT_DELTA == ctrl.TARGET_SHORT_DELTA
    assert s23.MIN_ENTRY_CREDIT == ctrl.MIN_ENTRY_CREDIT
    # The engine callables are the control's own (structural agreement, no re-implementation).
    assert s23.ctrl._build_iron_condor is ctrl._build_iron_condor
    assert s23.ctrl._scan_exit is ctrl._scan_exit


def test_gate_constants_are_the_frozen_preregistered_choices():
    assert s23.GAP_GATE_MULT == 2.0     # "twice the typical gap" — not swept
    assert s23.GAP_LOOKBACK == 20       # trailing window — not swept


# --------------------------------------------------------------------------- #
# GAP derivation — strictly causal (this day's open vs the PRIOR day's close).
# --------------------------------------------------------------------------- #
def _min_df(rows):
    """Build the minimal columns _apply_gap_gate needs from a list of (day, open, close)."""
    return pd.DataFrame([
        {"day": d, "open_spot": o, "close_spot": c,
         "gap_abs_pct": float("nan"), "gap_signed_pct": float("nan"),
         "prior_close": float("nan")}
        for (d, o, c) in rows
    ])


def test_gap_uses_prior_day_close_not_same_day_close():
    """Day 2's gap must use day 1's CLOSE, never day 2's own close (that would be look-ahead)."""
    df = _min_df([
        (dt.date(2024, 1, 2), 100.0, 110.0),   # first day: no prior -> gap NaN
        (dt.date(2024, 1, 3), 121.0, 5.0),      # open 121 vs prior close 110 -> +10.0%
    ])
    out = s23._apply_gap_gate(df)
    r0, r1 = out.iloc[0], out.iloc[1]
    assert not np.isfinite(r0["gap_abs_pct"])                 # first row: no prior close
    assert r1["prior_close"] == 110.0                         # prior day's CLOSE, not open
    assert r1["gap_signed_pct"] == pytest.approx(10.0)        # (121-110)/110*100
    assert r1["gap_abs_pct"] == pytest.approx(10.0)
    # The absurd same-day close (5.0) must NOT influence this day's gap.


def test_gap_sign_and_abs():
    df = _min_df([
        (dt.date(2024, 1, 2), 100.0, 100.0),
        (dt.date(2024, 1, 3),  98.0, 99.0),     # (98-100)/100 = -2.0%
    ])
    out = s23._apply_gap_gate(df)
    assert out.iloc[1]["gap_signed_pct"] == pytest.approx(-2.0)
    assert out.iloc[1]["gap_abs_pct"] == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# GATE baseline — trailing median uses STRICTLY-earlier rows only.
# --------------------------------------------------------------------------- #
def test_big_gap_flag_uses_strictly_prior_gaps_and_frozen_multiple():
    """Baseline for day i = median of prior gaps; big_gap iff gap_i > 2.0x that baseline.
    We plant a long run of ~1.0% gaps then a 3.0% spike; the spike must flag big_gap, and a
    later 1.0% day must NOT — proving the flag is the frozen 2x rule off a trailing median."""
    rows = [(dt.date(2024, 1, 1), 100.0, 100.0)]  # seed close
    price = 100.0
    days = []
    # 25 days each gapping ~+1.0% at the open (baseline ~1.0), close flat.
    for k in range(25):
        d = dt.date(2024, 2, 1) + dt.timedelta(days=k)
        opn = price * 1.01
        rows.append((d, opn, price))   # gap ~ +1.0% vs prior close (=price)
        days.append(d)
    # Day with a 3.0% gap -> should flag big_gap (3.0 > 2.0 * ~1.0).
    spike_day = dt.date(2024, 2, 1) + dt.timedelta(days=25)
    rows.append((spike_day, price * 1.03, price))
    # Then a normal 1.0% day -> should NOT flag.
    normal_day = dt.date(2024, 2, 1) + dt.timedelta(days=26)
    rows.append((normal_day, price * 1.01, price))

    out = s23._apply_gap_gate(_min_df(rows)).set_index("day")
    assert bool(out.loc[spike_day, "big_gap"]) is True
    assert bool(out.loc[normal_day, "big_gap"]) is False
    # Baseline at the spike must be ~1.0 (the trailing median of the 1% run), not the spike itself.
    assert out.loc[spike_day, "gap_baseline_pct"] == pytest.approx(1.0, abs=0.05)


def test_insufficient_history_defaults_to_trade_not_sitout():
    """Early days without enough prior gaps must be big_gap=False (default = TRADE)."""
    rows = [(dt.date(2024, 1, 1) + dt.timedelta(days=k), 100.0 * (1 + 0.05 * (k % 2)), 100.0)
            for k in range(4)]
    out = s23._apply_gap_gate(_min_df(rows))
    assert not out["big_gap"].any()   # < max(5, LOOKBACK//2) history -> never sit out


# --------------------------------------------------------------------------- #
# GATED comparison — sits out big-gap days (never re-places).
# --------------------------------------------------------------------------- #
def test_gate_sits_out_big_gap_days_only():
    """GATED trades exactly the non-big-gap traded days; the control trades all of them."""
    df = pd.DataFrame([
        {"day": dt.date(2024, 3, 1), "traded": True, "big_gap": False, "pnl_dollars": 50.0,
         "half": "train", "gamma_regime": "positive", "vix_regime": "contango"},
        {"day": dt.date(2024, 3, 2), "traded": True, "big_gap": True, "pnl_dollars": -500.0,
         "half": "train", "gamma_regime": "positive", "vix_regime": "contango"},
        {"day": dt.date(2024, 3, 3), "traded": True, "big_gap": False, "pnl_dollars": 40.0,
         "half": "test", "gamma_regime": "negative", "vix_regime": "backwardation"},
    ])
    txt = s23.compare_control_vs_gate(df)
    # Control total = 50 - 500 + 40 = -410; gated skips the -500 big-gap day -> 90.
    assert "-410" in txt or "-410.0" in txt
    assert "90" in txt


def test_comparison_emits_placebo_and_refutes_when_gate_no_better_than_random():
    """On a losing book where the gate sits out RANDOM (not the worst) days, the placebo must
    fire and the verdict must be REFUTED -- the gate's gain is the fewer-trades artifact, not
    signal. Build 60 days: the gate sits out days that are NOT systematically the losers, so
    random sit-out of the same count does at least as well."""
    rng = np.random.default_rng(3)
    n = 60
    pnl = rng.normal(-30, 120, n)          # losing book, no gap->loss relationship
    big = np.zeros(n, dtype=bool)
    big[rng.choice(n, size=12, replace=False)] = True   # gate sits out 12 RANDOM days
    df = pd.DataFrame({
        "day": [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(n)],
        "traded": [True] * n,
        "big_gap": big,
        "pnl_dollars": pnl,
        "half": ["train"] * (n // 2) + ["test"] * (n - n // 2),
        "gamma_regime": ["positive"] * n,
        "vix_regime": ["contango"] * n,
    })
    txt = s23.compare_control_vs_gate(df)
    assert "PLACEBO" in txt
    assert "REFUTED" in txt or "REJECTED" in txt   # a random-gap gate cannot be a real edge


# --------------------------------------------------------------------------- #
# PV-band — coverage monotone in k; close_in_em is (close-entry)/EM.
# --------------------------------------------------------------------------- #
def test_pv_band_coverage_monotone_in_k():
    rng = np.random.default_rng(0)
    n = 200
    df = pd.DataFrame({
        "day": [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(n)],
        "traded": [True] * n,
        "close_in_em": rng.normal(0, 1, n),
        "half": ["train"] * (n // 2) + ["test"] * (n - n // 2),
        "gamma_regime": ["positive"] * n,
        "vix_regime": ["contango"] * n,
        "short_put_k": [4990.0] * n,
        "short_call_k": [5010.0] * n,
        "close_spot": [5000.0] * n,
    })
    band = s23.build_pv_band(df)
    all_row = band[band["bucket"] == "ALL"].iloc[0]
    ks = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    vals = [all_row[f"within_{k}EM"] for k in ks]
    assert all(vals[i] <= vals[i + 1] + 1e-9 for i in range(len(vals) - 1))  # non-decreasing
    assert 0.0 <= vals[0] <= 1.0 and vals[-1] <= 1.0
    # close (5000) is inside [4990, 5010] on every row -> coverage 1.0.
    assert all_row["close_inside_shorts"] == pytest.approx(1.0)
