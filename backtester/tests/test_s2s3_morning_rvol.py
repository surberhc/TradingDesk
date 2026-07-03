r"""
test_s2s3_morning_rvol.py — unit tests for the S2/S3 morning-realized-vol signal harness
(s2s3_morning_rvol), the pre-registered follow-up to the refuted gap gate.

These pin the MECHANICS and the ANTI-LOOK-AHEAD / ANTI-CURVE-FIT contract, NOT any
strategy outcome:
  * the signal (OLS am_rvol->pm_range) AND the top-third flag cutoff are fit on the TRAIN
    half ONLY and applied unseen to the test half (no look-ahead, no per-half retune);
  * the frozen arm constants are the pre-registered plain choices (top-third flag, 0.5x
    downsize, 0.10 widen delta, 98% placebo bar), pinned so a silent retune breaks a test;
  * each arm's per-day P&L is the exact declared transform of the SAME control (GATE->0 on
    flagged, DOWNSIZE->0.5x on flagged, WIDEN->widen_pnl on flagged);
  * the matched random placebo fires and REFUTES an arm whose flag is random noise;
  * the avoided-losses decomposition splits flagged-day change into losses-avoided vs
    profits-forgone and only credits an arm whose gain is dominated by avoided losses;
  * the plateau check flips negative iff a sub-bucket delta goes negative.

The Arm-C 0.10-delta re-simulation is STRUCTURAL (it calls s6_control's own
_build_iron_condor / _scan_exit unchanged at a different target delta), asserted by identity
of the imported callables, not re-simulated on the warehouse here.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s2s3_morning_rvol as mr  # noqa: E402
import s6_control as ctrl  # noqa: E402


# --------------------------------------------------------------------------- #
# Frozen-constant guards (a silent retune must fail a test — rule #1).
# --------------------------------------------------------------------------- #
def test_frozen_preregistered_constants():
    assert mr.FLAG_TOP_FRACTION == pytest.approx(1.0 / 3.0)   # "top third", not swept
    assert mr.DOWNSIZE_FRACTION == 0.5                        # Arm B, pre-declared
    assert mr.WIDEN_TARGET_DELTA == 0.10                      # Arm C, pre-declared
    assert mr.PLACEBO_PASS_FRAC == 0.98                       # raised bar for 3 arms
    assert mr.N_PLACEBO_DRAWS == 3000                         # inherited draw count
    assert mr.TRAIN_END == dt.date(2024, 6, 30)              # inherited OOS split


def test_widen_engine_is_the_controls_own_callables():
    """Arm C must reuse the control's engine (structural agreement, no re-implementation)."""
    assert mr.ctrl._build_iron_condor is ctrl._build_iron_condor
    assert mr.ctrl._scan_exit is ctrl._scan_exit


# --------------------------------------------------------------------------- #
# Signal fit + flag — TRAIN-ONLY, applied unseen (no look-ahead).
# --------------------------------------------------------------------------- #
def _synth_days(n_train=120, n_test=90, seed=1):
    """Build a synthetic day table with a real am_rvol->pm_range slope + a control P&L that
    is WORSE when am_rvol is high (so a genuine morning-vol edge exists to detect)."""
    rng = np.random.default_rng(seed)
    days, rows = [], []
    d0 = dt.date(2023, 1, 1)
    for i in range(n_train + n_test):
        d = d0 + dt.timedelta(days=i)
        am = float(abs(rng.normal(0.05, 0.02)) + 0.01)          # morning rvol %/min
        pm = 0.8 * am * 100 + float(rng.normal(0, 0.3))          # pm range ~ 0.8*am
        # control pnl: mostly small credits, but big losses concentrated on high-am days.
        base = float(rng.normal(30, 40))
        loss = -600.0 if am > 0.08 and rng.random() < 0.6 else 0.0
        pnl = base + loss
        half = "train" if d <= mr.TRAIN_END else "test"
        rows.append({
            "day": d, "traded": True, "am_rvol_pct": am, "pm_range_pct": pm,
            "pnl_dollars": pnl, "half": half,
            "gamma_regime": "positive" if i % 2 else "negative",
            "vix_regime": "contango" if i % 3 else "backwardation",
        })
    df = pd.DataFrame(rows)
    # Force the split so ~n_train rows are train regardless of the calendar.
    df["half"] = ["train"] * n_train + ["test"] * n_test
    df.loc[:n_train - 1, "day"] = [mr.TRAIN_END - dt.timedelta(days=n_train - k)
                                   for k in range(n_train)]
    df.loc[n_train:, "day"] = [mr.TRAIN_END + dt.timedelta(days=k + 1)
                               for k in range(n_test)]
    return df


def test_flag_cutoff_is_fit_on_train_only_and_applied_unseen():
    """The top-third cutoff must come from TRAIN predictions only; the test half cannot move
    it. We plant a test half with hugely inflated am_rvol; if the cutoff leaked test info it
    would rise, but a train-only cutoff must be unchanged."""
    df = _synth_days()
    out_a = mr.add_flag(df.copy(), verbose=False)
    cutoff_a = out_a["flag_cutoff"].iloc[0]

    # Inflate ONLY the test half's am_rvol massively; refit.
    df2 = df.copy()
    df2.loc[df2["half"] == "test", "am_rvol_pct"] *= 10.0
    out_b = mr.add_flag(df2, verbose=False)
    cutoff_b = out_b["flag_cutoff"].iloc[0]
    assert cutoff_a == pytest.approx(cutoff_b)   # test half cannot move a train-only cutoff


def test_flagged_fraction_on_train_is_top_third():
    df = _synth_days()
    out = mr.add_flag(df, verbose=False)
    tr = out[out["half"] == "train"]
    frac = tr["flagged"].mean()
    assert abs(frac - (1.0 / 3.0)) < 0.05   # top third of train, by construction


def test_missing_am_rvol_is_not_flagged():
    df = _synth_days()
    df.loc[df.index[:5], "am_rvol_pct"] = np.nan
    out = mr.add_flag(df, verbose=False)
    assert not out.loc[out.index[:5], "flagged"].any()   # no prediction -> default = trade


# --------------------------------------------------------------------------- #
# Arm P&L transforms — exact declared transform of the SAME control.
# --------------------------------------------------------------------------- #
def _flagged_df():
    df = pd.DataFrame([
        {"day": dt.date(2024, 3, 1), "traded": True, "flagged": False, "pnl_dollars": 50.0,
         "widen_pnl": 50.0, "half": "train", "gamma_regime": "positive", "vix_regime": "contango"},
        {"day": dt.date(2024, 3, 2), "traded": True, "flagged": True, "pnl_dollars": -500.0,
         "widen_pnl": -200.0, "half": "train", "gamma_regime": "positive", "vix_regime": "contango"},
        {"day": dt.date(2024, 3, 3), "traded": True, "flagged": True, "pnl_dollars": 100.0,
         "widen_pnl": 20.0, "half": "test", "gamma_regime": "negative", "vix_regime": "backwardation"},
    ])
    return df


def test_arm_transforms_condition_the_same_control():
    df = _flagged_df()
    ctrl_p = mr.arm_pnl(df, "control")
    a = mr.arm_pnl(df, "A")
    b = mr.arm_pnl(df, "B")
    c = mr.arm_pnl(df, "C")
    assert list(ctrl_p) == [50.0, -500.0, 100.0]
    assert list(a) == [50.0, 0.0, 0.0]                    # GATE: flagged -> 0
    assert list(b) == [50.0, -250.0, 50.0]                # DOWNSIZE: flagged -> 0.5x
    assert list(c) == [50.0, -200.0, 20.0]                # WIDEN: flagged -> widen_pnl


# --------------------------------------------------------------------------- #
# Plateau check — flips iff a sub-bucket delta goes negative.
# --------------------------------------------------------------------------- #
def test_plateau_flips_when_a_bucket_delta_is_negative():
    bt = pd.DataFrame([
        {"bucket": "ALL", "delta_$": 100.0},
        {"bucket": "half=train", "delta_$": 60.0},
        {"bucket": "half=test", "delta_$": 40.0},
        {"bucket": "pos/cont", "delta_$": 120.0},
        {"bucket": "neg/back", "delta_$": -20.0},   # one bucket flips negative
    ])
    ok, losers = mr.plateau_ok(bt)
    assert ok is False and "neg/back" in losers


def test_plateau_holds_when_all_positive():
    bt = pd.DataFrame([
        {"bucket": "ALL", "delta_$": 100.0},
        {"bucket": "half=train", "delta_$": 60.0},
        {"bucket": "half=test", "delta_$": 40.0},
        {"bucket": "pos/cont", "delta_$": 10.0},
    ])
    ok, losers = mr.plateau_ok(bt)
    assert ok is True and losers == []


# --------------------------------------------------------------------------- #
# Avoided-losses decomposition — credits avoided losses, not trimming.
# --------------------------------------------------------------------------- #
def test_decomp_credits_avoided_losses_when_gate_cuts_a_loser():
    """A GATE that sits out a flagged LOSING day should show the gain as avoided losses."""
    df = pd.DataFrame([
        {"day": dt.date(2024, 3, 1), "traded": True, "flagged": True, "pnl_dollars": -500.0,
         "widen_pnl": -500.0, "half": "train", "gamma_regime": "positive", "vix_regime": "contango"},
        {"day": dt.date(2024, 3, 2), "traded": True, "flagged": False, "pnl_dollars": 30.0,
         "widen_pnl": 30.0, "half": "train", "gamma_regime": "positive", "vix_regime": "contango"},
    ])
    dcp = mr.avoided_losses_decomp(df, "A")
    assert dcp["change_on_losing_days_$"] == pytest.approx(500.0)   # cut a -500 loser -> +500
    assert dcp["change_on_winning_days_$"] == pytest.approx(0.0)
    assert dcp["avoided_losses_dominate"] is True


def test_decomp_flags_trimming_when_gate_mostly_cuts_winners():
    """A GATE that sits out flagged WINNERS is trimming, not avoiding losses -> not credited."""
    df = pd.DataFrame([
        {"day": dt.date(2024, 3, 1), "traded": True, "flagged": True, "pnl_dollars": 200.0,
         "widen_pnl": 200.0, "half": "train", "gamma_regime": "positive", "vix_regime": "contango"},
        {"day": dt.date(2024, 3, 2), "traded": True, "flagged": True, "pnl_dollars": -50.0,
         "widen_pnl": -50.0, "half": "train", "gamma_regime": "positive", "vix_regime": "contango"},
    ])
    dcp = mr.avoided_losses_decomp(df, "A")
    # cuts a +200 winner (change -200) and a -50 loser (change +50): forgone dominates.
    assert dcp["change_on_winning_days_$"] == pytest.approx(-200.0)
    assert dcp["change_on_losing_days_$"] == pytest.approx(50.0)
    assert dcp["avoided_losses_dominate"] is False


# --------------------------------------------------------------------------- #
# Matched placebo — refutes a RANDOM-noise flag.
# --------------------------------------------------------------------------- #
def test_placebo_refutes_a_random_flag_gate():
    """If the flag is random (no gap between flagged-day losses and the book), the arm cannot
    beat random sit-out of the same count in >=98% of draws -> placebo does NOT pass."""
    rng = np.random.default_rng(5)
    n = 200
    pnl = rng.normal(-30, 120, n)                 # losing book, no signal
    flag = np.zeros(n, dtype=bool)
    flag[rng.choice(n, size=n // 3, replace=False)] = True   # RANDOM flag
    df = pd.DataFrame({
        "day": [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(n)],
        "traded": [True] * n, "flagged": flag, "pnl_dollars": pnl,
        "widen_pnl": pnl, "half": ["train"] * (n // 2) + ["test"] * (n - n // 2),
        "gamma_regime": ["positive"] * n, "vix_regime": ["contango"] * n,
    })
    plac = mr.matched_placebo(df, "A", n_draws=1000)
    assert plac["beats_placebo_98"] is False   # a random flag has no edge over random sit-out


def test_placebo_passes_when_flag_targets_the_true_losers():
    """If the flag deterministically picks the worst losers, GATE beats random sit-out in
    ~100% of draws -> placebo passes (the mechanic can register a real edge)."""
    n = 200
    pnl = np.concatenate([np.full(n // 4, -800.0), np.full(3 * n // 4, 40.0)])
    flag = pnl < -100.0                          # flag exactly the big losers
    df = pd.DataFrame({
        "day": [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(n)],
        "traded": [True] * n, "flagged": flag, "pnl_dollars": pnl,
        "widen_pnl": pnl, "half": ["train"] * (n // 2) + ["test"] * (n - n // 2),
        "gamma_regime": ["positive"] * n, "vix_regime": ["contango"] * n,
    })
    plac = mr.matched_placebo(df, "A", n_draws=1000)
    assert plac["beats_placebo_98"] is True
    assert plac["frac_arm_beats_random"] >= 0.98
