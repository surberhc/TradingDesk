r"""
test_condor_management_experiment.py — unit tests for the 0DTE condor MANAGEMENT/exit
experiment (condor_management_experiment).

These pin the MECHANICS and the ANTI-LOOK-AHEAD / ANTI-CURVE-FIT contract, NOT any strategy
outcome:
  * the entry chassis constants are the control's own (a silent retune must fail a test);
  * the management-exit scan is CAUSAL: a future-minute price cannot change an earlier arm's
    exit decision (the no-lookahead guard);
  * every management exit is CHARGED an honest 4-leg debit -- an early close pays the real
    round-trip cost, so P&L = entry_credit - honest_exit_debit (the cost-is-charged guard);
  * A_hold reproduces the control's own hold-to-settle iron-condor P&L byte-for-byte
    (structural agreement -- it calls the control's own callables);
  * the profit-target / time-exit / combo rules resolve at the first minute they bind;
  * the matched random-exit placebo fires (arm no better than random) on a no-edge book and
    passes when the arm systematically beats the random-exit distribution.

The scan/placebo tests build tiny in-memory NBBO frames -- no warehouse needed, exact
arithmetic. The A_hold=control agreement is asserted by callable identity + a synthetic-day
reproduction.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import condor_management_experiment as cm  # noqa: E402
import s6_control as ctrl  # noqa: E402


# --------------------------------------------------------------------------- #
# Frozen-constant / structural-agreement guards (rule #1).
# --------------------------------------------------------------------------- #
def test_entry_chassis_constants_inherited_verbatim_from_control():
    assert cm.ENTRY_TIME == ctrl.ENTRY_TIME
    assert cm.SETTLEMENT_TIME == ctrl.SETTLEMENT_TIME
    assert cm.SPREAD_WIDTH == ctrl.SPREAD_WIDTH
    assert cm.TARGET_SHORT_DELTA == ctrl.TARGET_SHORT_DELTA
    assert cm.MIN_ENTRY_CREDIT == ctrl.MIN_ENTRY_CREDIT
    assert cm.WINNER_DEBIT == ctrl.WINNER_DEBIT
    assert cm.STOP_MULTIPLE == ctrl.STOP_MULTIPLE
    # The fill engine callables are the control's own (structural agreement, no re-impl).
    assert cm.ctrl._spread_debit_to_close is ctrl._spread_debit_to_close
    assert cm.ctrl._build_iron_condor is ctrl._build_iron_condor


def test_management_dial_constants_are_frozen_preregistered_choices():
    assert cm.PROFIT_TARGET_FRACS == (0.25, 0.50, 0.75)      # the plateau curve, not a best cell
    assert cm.TIME_EXIT_TIMES == (dt.time(15, 0), dt.time(15, 30))
    assert cm.COMBO_PROFIT_FRAC == 0.50
    assert cm.COMBO_TIME_EXIT == dt.time(15, 30)


# --------------------------------------------------------------------------- #
# Build a tiny synthetic one-day NBBO with a CONTROLLABLE debit-to-close path.
# --------------------------------------------------------------------------- #
def _synthetic_nbbo(entry_minute, debit_path, legs):
    """Return an NBBO frame such that _spread_debit_to_close(snap, legs) == debit_path[i] at
    minute entry_minute + (i+1). We use a 4-leg condor where only the SHORT put carries the
    whole debit (its ask = target debit; every other leg quoted at 0/0 so it contributes 0),
    which lets us script the debit path exactly. legs is the standard condor leg list.
    """
    (spk, _, _), (lpk, _, _), (sck, _, _), (lck, _, _) = legs
    rows = []
    for i, deb in enumerate(debit_path):
        m = entry_minute + pd.Timedelta(minutes=i + 1)
        # short put: buy back at ask=deb (side +1 pays ask). Others: 0 bid / 0 ask contribute 0.
        rows += [
            {"minute": m, "strike": spk, "right": "PUT", "bid": 0.0, "ask": deb},
            {"minute": m, "strike": lpk, "right": "PUT", "bid": 0.0, "ask": 0.0},
            {"minute": m, "strike": sck, "right": "CALL", "bid": 0.0, "ask": 0.0},
            {"minute": m, "strike": lck, "right": "CALL", "bid": 0.0, "ask": 0.0},
        ]
    return pd.DataFrame(rows)


_LEGS = [(5000.0, "PUT", +1), (4995.0, "PUT", -1), (5100.0, "CALL", +1), (5105.0, "CALL", -1)]


def test_profit_target_fires_at_first_touch_and_is_charged_the_honest_debit():
    """Credit 1.00; debit path 0.90,0.70,0.40,0.20. 25% target => open P&L>=0.25 => debit<=0.75:
    first touched at the 0.70 minute (index 1). The booked debit must be that honest 0.70 -- the
    real round-trip cost, not a mid or a modeled fill -- and the hold must be 2 minutes."""
    entry = pd.Timestamp(dt.datetime(2024, 1, 2, 14, 0))
    settle = pd.Timestamp(dt.datetime(2024, 1, 2, 16, 0))
    nbbo = _synthetic_nbbo(entry, [0.90, 0.70, 0.40, 0.20], _LEGS)
    credit = 1.00
    res, path = cm._scan_managed_exits(nbbo, _LEGS, credit, entry, settle)
    b25 = res["B_pt25"]
    assert b25["exit_reason"] == "target"
    assert b25["exit_debit"] == pytest.approx(0.70)     # HONEST debit charged, first-touch
    assert b25["hold_min"] == pytest.approx(2.0)
    # 75% target => debit<=0.25 => first at 0.20 (index 3), later than 25% -> proves ordering.
    b75 = res["B_pt75"]
    assert b75["exit_debit"] == pytest.approx(0.20)
    assert b75["hold_min"] == pytest.approx(4.0)


def test_no_lookahead_future_minute_cannot_change_an_earlier_exit():
    """A profit-target that binds at minute 2 must be UNAFFECTED by anything at minute 3+.
    Run once with a benign tail and once with a catastrophic tail (huge debit spike after the
    target minute); the B_pt25 exit debit + minute must be identical -- the scan froze at the
    firing minute and never peeked forward."""
    entry = pd.Timestamp(dt.datetime(2024, 1, 2, 14, 0))
    settle = pd.Timestamp(dt.datetime(2024, 1, 2, 16, 0))
    credit = 1.00
    benign = cm._scan_managed_exits(
        _synthetic_nbbo(entry, [0.90, 0.70, 0.60, 0.60], _LEGS), _LEGS, credit, entry, settle)[0]
    catastrophe = cm._scan_managed_exits(
        _synthetic_nbbo(entry, [0.90, 0.70, 9.99, 9.99], _LEGS), _LEGS, credit, entry, settle)[0]
    for arm in ("B_pt25",):
        assert benign[arm]["exit_debit"] == catastrophe[arm]["exit_debit"] == pytest.approx(0.70)
        assert benign[arm]["hold_min"] == catastrophe[arm]["hold_min"] == pytest.approx(2.0)


def test_time_exit_binds_at_the_target_clock_and_charges_that_minute_debit():
    """C_t1500 must close at the FIRST minute >= 15:00 (never a mid), regardless of P&L."""
    entry = pd.Timestamp(dt.datetime(2024, 1, 2, 14, 0))
    settle = pd.Timestamp(dt.datetime(2024, 1, 2, 16, 0))
    # 120 minutes of debit=0.60 (never a winner, never a stop, never a target at 25/50/75 for
    # credit 1.0 since 0.60 > 0.75? no: 0.60<=0.75 -> 25% target WOULD fire at minute 1). Use a
    # credit where 0.60 is above the 25% target so only the time rule differentiates C.
    credit = 0.50   # 25% target => debit<=0.375; 0.60 never triggers a target -> isolates time
    debit_path = [0.60] * 120
    nbbo = cm._synthetic_nbbo_full(entry, debit_path, _LEGS) if hasattr(cm, "_synthetic_nbbo_full") \
        else _synthetic_nbbo(entry, debit_path, _LEGS)
    res, _ = cm._scan_managed_exits(nbbo, _LEGS, credit, entry, settle)
    c15 = res["C_t1500"]
    assert c15["exit_reason"] == "time"
    assert c15["hold_min"] == pytest.approx(60.0)     # 14:00 + 60m = 15:00
    assert c15["exit_debit"] == pytest.approx(0.60)   # that minute's honest debit
    # A_hold (no time rule) holds to settle on this benign flat path.
    assert res["A_hold"]["exit_reason"] == "settle"


def test_a_hold_reproduces_the_control_scan_on_a_scripted_path():
    """A_hold must equal the control's OWN _scan_exit on the identical NBBO -- structural
    agreement, not a parallel re-implementation. Script a path that settles (no winner/stop)."""
    entry = pd.Timestamp(dt.datetime(2024, 1, 2, 14, 0))
    settle = pd.Timestamp(dt.datetime(2024, 1, 2, 16, 0))
    credit = 1.00
    path = [0.90, 0.85, 0.80, 0.80]      # never <=0.05, never >=3.00 -> control settles
    nbbo = _synthetic_nbbo(entry, path, _LEGS)
    a_hold = cm._scan_managed_exits(nbbo, _LEGS, credit, entry, settle)[0]["A_hold"]
    ctrl_reason, ctrl_min, ctrl_debit = ctrl._scan_exit(nbbo, _LEGS, credit, entry, settle)
    assert a_hold["exit_reason"] == ctrl_reason
    assert a_hold["exit_debit"] == pytest.approx(ctrl_debit)


def test_stop_and_winner_bind_on_every_arm_before_the_arm_rule():
    """The 2x stop is the disaster brake on EVERY arm; a stop before any target must book 'stop'
    on all arms (no arm can early-take past a blown stop)."""
    entry = pd.Timestamp(dt.datetime(2024, 1, 2, 14, 0))
    settle = pd.Timestamp(dt.datetime(2024, 1, 2, 16, 0))
    credit = 1.00                        # stop at debit>=3.00
    path = [3.50, 0.10, 0.10, 0.10]      # minute 1 blows the stop before any target
    res, _ = cm._scan_managed_exits(_synthetic_nbbo(entry, path, _LEGS), _LEGS, credit, entry, settle)
    for arm in cm.ARM_NAMES:
        assert res[arm]["exit_reason"] == "stop"
        assert res[arm]["exit_debit"] == pytest.approx(3.50)


# --------------------------------------------------------------------------- #
# Matched random-exit placebo: fires on a no-edge book, passes on a real edge.
# --------------------------------------------------------------------------- #
def test_placebo_fires_when_arm_is_no_better_than_random_exit_timing():
    """Build a book of days whose debit path is a random walk with NO time-of-exit edge: the
    per-minute debit is i.i.d. around a constant, so exiting earlier/later carries no signal.
    An arm total equal to the random-exit MEAN must NOT be flagged as beating the placebo."""
    rng = np.random.default_rng(0)
    paths = {}
    credit_by_day = {}
    for k in range(60):
        d = dt.date(2024, 1, 1) + dt.timedelta(days=k)
        offs = np.arange(1, 61, dtype=float)
        debit = 0.5 + rng.normal(0, 0.05, 60)      # no drift, no time edge
        paths[d] = np.column_stack([offs, debit])
        credit_by_day[d] = 0.5
    # An "arm" that exits at the mean hold with a total drawn to equal the placebo mean.
    # First compute the placebo mean by handing it an arm_total of +inf-ish so nothing beats it,
    # then re-run with that mean as the arm_total (arm == placebo => ~50% of draws >= arm).
    probe = cm.random_exit_placebo_from_paths(paths, credit_by_day, 30.0, 1e12, n_draws=500)
    at_mean = cm.random_exit_placebo_from_paths(
        paths, credit_by_day, 30.0, probe["placebo_mean_$"], n_draws=500)
    assert not at_mean["arm_beats_placebo"]        # equal-to-random must FAIL the 5% bar
    assert at_mean["frac_placebo_ge_arm"] > 0.05


def test_placebo_passes_when_arm_exit_timing_has_a_real_edge():
    """If early exit is genuinely superior -- the debit RISES monotonically each minute (so
    exiting early is strictly better) and the arm always exits at minute 1 -- the arm total must
    beat essentially every random-exit draw (which on average exits later, at a higher debit)."""
    paths = {}
    credit_by_day = {}
    for k in range(60):
        d = dt.date(2024, 1, 1) + dt.timedelta(days=k)
        offs = np.arange(1, 61, dtype=float)
        debit = 0.2 + 0.02 * offs                  # debit rises with time -> early is better
        paths[d] = np.column_stack([offs, debit])
        credit_by_day[d] = 1.0
    # Arm total = exit at minute 1 (offset 1) every day: credit - debit(1) = 1.0 - 0.22 = 0.78/pt.
    arm_total = sum((1.0 - (0.2 + 0.02 * 1)) * cm.CONTRACT_MULTIPLIER * cm.N_CONTRACTS
                    for _ in range(60))
    res = cm.random_exit_placebo_from_paths(paths, credit_by_day, 30.0, arm_total, n_draws=500)
    assert res["arm_beats_placebo"]                # early-exit edge must clear the 5% bar
    assert res["frac_placebo_ge_arm"] < 0.05


# --------------------------------------------------------------------------- #
# FILL BAND (pre-registered net-combo execution axis) guards.
# --------------------------------------------------------------------------- #
def _band_snap(legs):
    """One-minute snapshot with a real bid-ask on every leg (so mid != worst-side)."""
    (spk, _, _), (lpk, _, _), (sck, _, _), (lck, _, _) = legs
    return pd.DataFrame([
        {"strike": spk, "right": "PUT", "bid": 1.00, "ask": 1.20},
        {"strike": lpk, "right": "PUT", "bid": 0.40, "ask": 0.55},
        {"strike": sck, "right": "CALL", "bid": 0.90, "ask": 1.10},
        {"strike": lck, "right": "CALL", "bid": 0.30, "ask": 0.45},
    ])


def test_fill_fracs_are_the_preregistered_band():
    assert cm.FILL_FRACS == (0.0, 0.25, 0.50, 1.0)   # mid / 25 / 50(headline) / full-cross
    assert cm.HEADLINE_FILL == 0.50


def test_blended_fill_f1_reproduces_the_honest_control_fills_byte_for_byte():
    """f=1 (worst side on every leg) must equal the control's own honest credit/debit exactly,
    and f=0 must equal the pure-mid marks -- so the band's endpoints are anchored, not modeled."""
    snap = _band_snap(_LEGS)
    honest_credit = (ctrl._credit_to_open(1.00, 0.55) + ctrl._credit_to_open(0.90, 0.45))
    assert cm._blended_credit_to_open(snap, _LEGS, 1.0) == pytest.approx(honest_credit)
    assert cm._blended_credit_to_open(snap, _LEGS, 0.0) == pytest.approx(cm._credit_mid(snap, _LEGS))
    assert cm._blended_debit_to_close(snap, _LEGS, 1.0) == pytest.approx(
        ctrl._spread_debit_to_close(snap, _LEGS))
    assert cm._blended_debit_to_close(snap, _LEGS, 0.0) == pytest.approx(cm._debit_mid(snap, _LEGS))


def test_fill_fraction_is_monotone_friendlier_fill_helps():
    """A smaller fill fraction must give MORE entry credit and a CHEAPER close -- otherwise the
    band is mis-signed and 'mid' would not be the optimistic bound."""
    snap = _band_snap(_LEGS)
    c0, c1 = cm._blended_credit_to_open(snap, _LEGS, 0.0), cm._blended_credit_to_open(snap, _LEGS, 1.0)
    d0, d1 = cm._blended_debit_to_close(snap, _LEGS, 0.0), cm._blended_debit_to_close(snap, _LEGS, 1.0)
    assert c0 > c1        # mid collects more credit than worst-side
    assert d0 < d1        # mid closes cheaper than worst-side


def test_fill_fraction_propagates_through_the_profit_target_trigger():
    """The pre-reg requires f to move WHEN the target is touched, not just scale final P&L.
    Script a debit path where the 50% target is reachable only under the friendlier (mid) fill
    but NOT under full-cross within the window, so the two fills exit at DIFFERENT minutes."""
    entry = pd.Timestamp(dt.datetime(2024, 1, 2, 14, 0))
    settle = pd.Timestamp(dt.datetime(2024, 1, 2, 16, 0))
    # Build a per-minute NBBO where the short put's bid/ask straddle so mid-close is materially
    # cheaper than worst-close. Short put ask = worst close; mid = (bid+ask)/2. Set bid so the
    # 50% target binds at mid but not at full-cross.
    rows = []
    # entry snapshot at 14:00 so blended credit is well-defined at both fills.
    for (m, spb, spa) in [(0, 1.00, 1.20), (1, 0.20, 1.00), (2, 0.10, 0.90)]:
        mm = entry + pd.Timedelta(minutes=m)
        rows += [
            {"minute": mm, "strike": 5000.0, "right": "PUT", "bid": spb, "ask": spa},
            {"minute": mm, "strike": 4995.0, "right": "PUT", "bid": 0.0, "ask": 0.0},
            {"minute": mm, "strike": 5100.0, "right": "CALL", "bid": 0.0, "ask": 0.0},
            {"minute": mm, "strike": 5105.0, "right": "CALL", "bid": 0.0, "ask": 0.0},
        ]
    nbbo = pd.DataFrame(rows)
    entry_snap = ctrl._snap_at(nbbo, entry)
    cred_mid = cm._blended_credit_to_open(entry_snap, _LEGS, 0.0)
    cred_full = cm._blended_credit_to_open(entry_snap, _LEGS, 1.0)
    res_mid, _ = cm._scan_managed_exits_at_fill(nbbo, _LEGS, cred_mid, 0.0, entry, settle)
    res_full, _ = cm._scan_managed_exits_at_fill(nbbo, _LEGS, cred_full, 1.0, entry, settle)
    # Under mid, the close debit is cheaper -> 50% target binds; under full-cross it may bind at a
    # later minute or settle. The exit MINUTE (hold_min) must differ between the two fills, proving
    # the fraction propagated through the trigger rather than merely rescaling P&L.
    assert res_mid["B_pt50"]["hold_min"] != res_full["B_pt50"]["hold_min"] \
        or res_mid["B_pt50"]["exit_reason"] != res_full["B_pt50"]["exit_reason"]
