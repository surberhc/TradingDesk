"""
test_deflated_sharpe.py — sanity & known-behavior checks for the multiple-
comparisons Sharpe statistics (Bailey & López de Prado 2014).

The point of these tests is direction and monotonicity, not exact table values:
a strong Sharpe from FEW trials stays significant; the SAME Sharpe selected from
THOUSANDS of trials collapses; negative skew / fat tails lower confidence; more
data raises it. Plus input validation.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from src import deflated_sharpe as ds


# --------------------------------------------------------------------------- #
# Normal CDF / PPF plumbing.                                                    #
# --------------------------------------------------------------------------- #
def test_norm_cdf_known_points():
    assert ds.norm_cdf(0.0) == pytest.approx(0.5, abs=1e-12)
    assert ds.norm_cdf(1.959963985) == pytest.approx(0.975, abs=1e-6)
    assert ds.norm_cdf(-1.959963985) == pytest.approx(0.025, abs=1e-6)


def test_norm_ppf_inverts_cdf():
    for p in (0.001, 0.05, 0.25, 0.5, 0.75, 0.95, 0.999):
        z = ds.norm_ppf(p)
        assert ds.norm_cdf(z) == pytest.approx(p, abs=1e-9)


def test_norm_ppf_boundaries_and_validation():
    assert ds.norm_ppf(0.0) == -math.inf
    assert ds.norm_ppf(1.0) == math.inf
    with pytest.raises(ValueError):
        ds.norm_ppf(-0.01)
    with pytest.raises(ValueError):
        ds.norm_ppf(1.01)


# --------------------------------------------------------------------------- #
# Expected maximum Sharpe.                                                      #
# --------------------------------------------------------------------------- #
def test_expected_max_sharpe_edge_cases():
    assert ds.expected_max_sharpe(1, 0.25) == 0.0       # no selection
    assert ds.expected_max_sharpe(0, 0.25) == 0.0
    assert ds.expected_max_sharpe(1000, 0.0) == 0.0     # no dispersion
    with pytest.raises(ValueError):
        ds.expected_max_sharpe(10, -1.0)


def test_expected_max_sharpe_monotone_in_trials():
    v = 0.03 ** 2   # per-observation Sharpe dispersion across trials
    vals = [ds.expected_max_sharpe(n, v) for n in (2, 10, 100, 1000, 10000)]
    # More trials => higher chance-maximum benchmark, strictly increasing.
    assert all(b > a for a, b in zip(vals, vals[1:]))
    assert vals[0] > 0.0


def test_expected_max_sharpe_scales_with_dispersion():
    a = ds.expected_max_sharpe(100, 0.25)
    b = ds.expected_max_sharpe(100, 1.0)
    # sqrt(var) scaling: doubling the std doubles E[max SR].
    assert b == pytest.approx(2.0 * a, rel=1e-9)


# --------------------------------------------------------------------------- #
# Probabilistic Sharpe Ratio.                                                  #
# --------------------------------------------------------------------------- #
def test_psr_monotone_in_T():
    # Same observed Sharpe, more observations => more confidence it beats zero.
    vals = [ds.probabilistic_sharpe_ratio(0.1, T, 0.0, 3.0, 0.0)
            for T in (50, 250, 1000, 5000)]
    assert all(b > a for a, b in zip(vals, vals[1:]))
    assert vals[-1] > 0.5


def test_psr_negative_skew_lowers_confidence():
    base = ds.probabilistic_sharpe_ratio(0.1, 1000, 0.0, 3.0)
    neg = ds.probabilistic_sharpe_ratio(0.1, 1000, -1.0, 3.0)
    assert neg < base


def test_psr_high_kurtosis_lowers_confidence():
    base = ds.probabilistic_sharpe_ratio(0.1, 1000, 0.0, 3.0)
    fat = ds.probabilistic_sharpe_ratio(0.1, 1000, 0.0, 9.0)
    assert fat < base


def test_psr_above_benchmark_gives_majority_confidence():
    # Observed clearly above benchmark => PSR > 0.5.
    assert ds.probabilistic_sharpe_ratio(0.2, 2000, 0.0, 3.0, 0.05) > 0.5
    # Observed below benchmark => PSR < 0.5.
    assert ds.probabilistic_sharpe_ratio(0.02, 2000, 0.0, 3.0, 0.1) < 0.5


def test_psr_validation():
    with pytest.raises(ValueError):
        ds.probabilistic_sharpe_ratio(0.1, 1, 0.0, 3.0)   # T too small


# --------------------------------------------------------------------------- #
# Deflated Sharpe Ratio — the headline behavior.                               #
# --------------------------------------------------------------------------- #
def test_dsr_few_trials_stays_significant():
    # Strong per-observation Sharpe, long sample, only a handful of trials.
    # var_trials is per-OBSERVATION Sharpe dispersion (small), matching the units
    # of observed_sharpe.
    res = ds.deflated_sharpe_ratio(
        observed_sharpe=0.15, T=2000, n_trials=3, var_trials=0.02 ** 2,
    )
    assert res.dsr > 0.95
    assert res.sr0 > 0.0


def test_dsr_collapses_when_selected_from_thousands():
    # A MODEST observed Sharpe (per-obs 0.08 ~= 1.3 annualized): convincing when it
    # is one of a few candidates, spurious once it is the max of a huge search whose
    # chance-maximum bar (sr0) climbs past it. Same observed Sharpe both times.
    strong = ds.deflated_sharpe_ratio(0.08, 1500, n_trials=3, var_trials=0.03 ** 2)
    searched = ds.deflated_sharpe_ratio(0.08, 1500, n_trials=100000, var_trials=0.03 ** 2)
    assert strong.dsr > 0.95
    assert searched.dsr < strong.dsr
    assert searched.dsr < 0.5
    assert searched.sr0 > strong.sr0


def test_dsr_n1_equals_psr_against_zero():
    res = ds.deflated_sharpe_ratio(0.1, 1000, n_trials=1, var_trials=0.4)
    psr0 = ds.probabilistic_sharpe_ratio(0.1, 1000, 0.0, 3.0, 0.0)
    assert res.sr0 == 0.0
    assert res.dsr == pytest.approx(psr0, abs=1e-12)


def test_dsr_zero_var_trials_no_deflation():
    res = ds.deflated_sharpe_ratio(0.1, 1000, n_trials=5000, var_trials=0.0)
    assert res.sr0 == 0.0
    assert res.dsr == pytest.approx(
        ds.probabilistic_sharpe_ratio(0.1, 1000, 0.0, 3.0, 0.0), abs=1e-12
    )


def test_dsr_monotone_decreasing_in_trials():
    vals = [ds.deflated_sharpe_ratio(0.12, 2000, n, 0.02 ** 2).dsr
            for n in (2, 20, 200, 2000, 20000)]
    assert all(b <= a for a, b in zip(vals, vals[1:]))


def test_dsr_negative_skew_lowers_dsr():
    # Choose N so the base DSR sits strictly inside (0,1), leaving room to move.
    base = ds.deflated_sharpe_ratio(0.12, 2000, 2000, 0.02 ** 2, skew=0.0, kurtosis=3.0)
    neg = ds.deflated_sharpe_ratio(0.12, 2000, 2000, 0.02 ** 2, skew=-1.5, kurtosis=6.0)
    assert 0.0 < base.dsr < 1.0
    assert neg.dsr < base.dsr


# --------------------------------------------------------------------------- #
# Moment helper + from-returns wrapper.                                        #
# --------------------------------------------------------------------------- #
def test_sharpe_and_moments_gaussian():
    rng = np.random.default_rng(0)
    r = rng.normal(0.0005, 0.01, size=20000)
    m = ds.sharpe_and_moments(r)
    assert m["T"] == 20000
    assert m["kurtosis"] == pytest.approx(3.0, abs=0.15)   # Gaussian
    assert abs(m["skew"]) < 0.1
    assert m["sharpe"] == pytest.approx(0.05, abs=0.02)


def test_sharpe_and_moments_validation():
    with pytest.raises(ValueError):
        ds.sharpe_and_moments([1.0])                     # too few
    with pytest.raises(ValueError):
        ds.sharpe_and_moments([0.01, 0.01, 0.01])        # zero variance


def test_deflated_sharpe_from_returns_matches_manual():
    rng = np.random.default_rng(1)
    r = rng.normal(0.0008, 0.01, size=3000)
    res = ds.deflated_sharpe_from_returns(r, n_trials=50, var_trials=0.3 ** 2)
    m = ds.sharpe_and_moments(r)
    manual = ds.deflated_sharpe_ratio(
        m["sharpe"], m["T"], 50, 0.3 ** 2, skew=m["skew"], kurtosis=m["kurtosis"]
    )
    assert res.dsr == pytest.approx(manual.dsr, abs=1e-12)


# --------------------------------------------------------------------------- #
# Experimental SPA / Reality Check stub.                                       #
# --------------------------------------------------------------------------- #
def test_reality_check_null_is_not_significant():
    rng = np.random.default_rng(2)
    # 20 pure-noise trials over the benchmark => best one should NOT look special.
    X = rng.normal(0.0, 0.01, size=(500, 20))
    p = ds.reality_check_pvalue(X, n_bootstrap=300, block=10, seed=3)
    assert p > 0.10


def test_reality_check_true_edge_is_significant():
    rng = np.random.default_rng(4)
    X = rng.normal(0.0, 0.01, size=(1000, 10))
    X[:, 0] += 0.004   # one trial has a genuine, large edge
    p = ds.reality_check_pvalue(X, n_bootstrap=300, block=10, seed=5)
    assert p < 0.05


def test_reality_check_validation():
    with pytest.raises(ValueError):
        ds.reality_check_pvalue(np.zeros(10), n_bootstrap=10)   # not 2-D
