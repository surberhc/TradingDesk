"""
Unit tests for overfit_gates.py — the overfit-diagnostics instrumentation.

Covers: clean pass, IS>>OOS overfit flag, OOS>>IS lucky flag, ceiling breach,
and edge cases (zero denominator / NaN / missing metric / non-finite).
"""

import math

import numpy as np
import pytest

from src import overfit_gates as og


# --------------------------------------------------------------------------- #
# is_oos_divergence
# --------------------------------------------------------------------------- #
def test_divergence_clean_when_oos_holds():
    r = og.is_oos_divergence(1.0, 0.9, metric_name="Sharpe")
    assert r["verdict"] == "clean"
    assert r["ratio"] == pytest.approx(0.9)
    assert r["retention"] == pytest.approx(-0.10)
    assert r["delta"] == pytest.approx(-0.10)


def test_divergence_overfit_when_is_dominates():
    # OOS retains only 40% of a strong IS Sharpe -> classic inflated fit.
    r = og.is_oos_divergence(2.0, 0.8, metric_name="Sharpe")
    assert r["verdict"] == "overfit-suspect"
    assert r["retention"] == pytest.approx(-0.60)


def test_divergence_lucky_when_oos_dominates():
    # OOS came in 50% ABOVE IS -> lucky-OOS warning, not a victory.
    r = og.is_oos_divergence(1.0, 1.5, metric_name="CAGR")
    assert r["verdict"] == "lucky-suspect"
    assert r["retention"] == pytest.approx(0.50)


def test_divergence_symmetric_at_tolerance_boundary():
    # Just inside tol (a 29% move) is "clean"; comfortably past it flips, in
    # both directions. (Values kept off the exact float knife-edge of 0.30.)
    assert og.is_oos_divergence(1.0, 0.71)["verdict"] == "clean"
    assert og.is_oos_divergence(1.0, 0.65)["verdict"] == "overfit-suspect"
    assert og.is_oos_divergence(1.0, 1.29)["verdict"] == "clean"
    assert og.is_oos_divergence(1.0, 1.35)["verdict"] == "lucky-suspect"


def test_divergence_custom_tolerance():
    # Loosen tol to 60%: a 50% drop that would normally flag now passes.
    r = og.is_oos_divergence(2.0, 1.0, tol=0.60)
    assert r["verdict"] == "clean"


def test_divergence_higher_is_better_false_flips_direction():
    # Loss-style metric (e.g. drawdown magnitude): OOS bigger == worse == overfit.
    worse = og.is_oos_divergence(0.10, 0.20, higher_is_better=False, metric_name="MaxDD")
    assert worse["verdict"] == "overfit-suspect"
    better = og.is_oos_divergence(0.20, 0.10, higher_is_better=False, metric_name="MaxDD")
    assert better["verdict"] == "lucky-suspect"


def test_divergence_zero_denominator_falls_back_to_delta():
    # IS ≈ 0 -> ratio undefined; verdict must still be produced from the delta.
    r = og.is_oos_divergence(0.0, 0.5)
    assert r["ratio"] is None
    assert r["retention"] is None
    assert r["verdict"] == "lucky-suspect"
    assert "ratio undefined" in r["reason"]

    r_bad = og.is_oos_divergence(0.0, -0.5)
    assert r_bad["verdict"] == "overfit-suspect"

    r_flat = og.is_oos_divergence(0.0, 0.0)
    assert r_flat["verdict"] == "clean"


def test_divergence_nan_and_missing_inputs_are_undefined():
    for bad in (float("nan"), np.nan, float("inf"), None, "1.0"):
        r = og.is_oos_divergence(bad, 1.0)
        assert r["verdict"] == "undefined"
        r2 = og.is_oos_divergence(1.0, bad)
        assert r2["verdict"] == "undefined"


def test_divergence_never_raises_and_reports_reason():
    r = og.is_oos_divergence(None, None)
    assert r["verdict"] == "undefined"
    assert r["reason"]  # non-empty human-readable reason


# --------------------------------------------------------------------------- #
# is_too_good
# --------------------------------------------------------------------------- #
def test_too_good_breach_above_ceiling():
    r = og.is_too_good(3.0)  # default ceiling 2.5
    assert r["verdict"] == "too-good-suspect"
    assert r["exceeds_by"] == pytest.approx(0.5)


def test_too_good_plausible_at_and_under_ceiling():
    assert og.is_too_good(2.5)["verdict"] == "plausible"   # boundary is inclusive
    assert og.is_too_good(1.2)["verdict"] == "plausible"


def test_too_good_custom_ceiling():
    r = og.is_too_good(2.0, ceiling=1.5)
    assert r["verdict"] == "too-good-suspect"
    assert r["exceeds_by"] == pytest.approx(0.5)


def test_too_good_nan_and_missing_are_undefined():
    for bad in (float("nan"), np.nan, float("inf"), None, "x"):
        assert og.is_too_good(bad)["verdict"] == "undefined"
    # A bad ceiling is also handled gracefully.
    assert og.is_too_good(3.0, ceiling=float("nan"))["verdict"] == "undefined"


def test_defaults_are_the_documented_proposals():
    # Guard the proposed defaults so a silent drift is caught.
    assert og.DEFAULT_DEGRADATION_TOL == 0.30
    assert og.DEFAULT_SHARPE_CEILING == 2.5


def test_returns_are_plain_dicts_and_pure():
    # Same inputs -> same output (no hidden state), and a real dict comes back.
    a = og.is_oos_divergence(1.0, 0.5)
    b = og.is_oos_divergence(1.0, 0.5)
    assert isinstance(a, dict) and a == b
    c = og.is_too_good(3.0)
    assert isinstance(c, dict)
    assert not math.isnan(c["exceeds_by"])
