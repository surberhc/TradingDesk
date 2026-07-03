"""Tests for canslim/dsr_report.py — the ADDITIVE Deflated/Probabilistic Sharpe
report over the CAN SLIM backtest outputs.

Load-bearing guarantees:
  1. PSR significance behaves correctly (strong positive series -> high PSR;
     zero/weak series -> ~0.5 or below; negative -> < 0.5).
  2. The N=1 path is genuinely PSR-only: DSR with n_trials=1 must EQUAL PSR-vs-zero
     (sr0 == 0, no deflation) — the honest single-frozen-config case.
  3. The N>1 path applies a real haircut: the same observed Sharpe is deflated
     (DSR <= PSR) once a multi-trial search is declared, and more trials / wider
     trial-Sharpe spread deflate harder.
  4. The selection loader parses the real results CSV's trade rows into a return
     series (skipped gracefully if the file is absent).
  5. The synthetic demo runs end-to-end and returns both paths.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dsr_report as dr  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. PSR significance behavior.                                                #
# --------------------------------------------------------------------------- #
def test_psr_high_for_strong_positive_series():
    rng = np.random.default_rng(1)
    # strong, long, near-normal positive book -> PSR should be ~1.
    returns = rng.normal(0.02, 0.05, size=1500)
    res = dr.report_single_config(returns, "strong")
    assert res["mode"] == "psr_only"
    assert res["psr"] > 0.99
    assert res["sharpe"] > 0


def test_psr_not_significant_for_zero_edge_series():
    # A true-zero-edge book must NOT clear the 95% significance bar (whichever way
    # the finite sample's mean happens to lean), i.e. PSR is not a confident "yes".
    rng = np.random.default_rng(2)
    returns = rng.normal(0.0, 0.05, size=800)   # true edge ~ 0
    res = dr.report_single_config(returns, "zero-edge")
    assert res["psr"] < 0.95                     # never a confident positive edge


def test_psr_below_half_for_negative_series():
    rng = np.random.default_rng(3)
    returns = rng.normal(-0.02, 0.05, size=800)
    res = dr.report_single_config(returns, "negative")
    assert res["psr"] < 0.5


# --------------------------------------------------------------------------- #
# 2. N=1 path is PSR-only (DSR collapses to PSR, no deflation).                #
# --------------------------------------------------------------------------- #
def test_n1_dsr_equals_psr_vs_zero():
    rng = np.random.default_rng(4)
    returns = rng.normal(0.01, 0.06, size=500)
    res = dr.report_single_config(returns, "single")
    # by construction sr0 == 0 and dsr == psr when N=1
    assert res["sr0"] == 0.0
    assert res["dsr"] == pytest.approx(res["psr"], abs=1e-12)


# --------------------------------------------------------------------------- #
# 3. N>1 path applies a real multiple-comparisons haircut.                     #
# --------------------------------------------------------------------------- #
def test_multi_trial_deflates_below_psr():
    rng = np.random.default_rng(5)
    best = rng.normal(0.012, 0.06, size=250)
    # a spread of trial Sharpes (a real search) -> positive sr0 -> DSR < PSR
    trial_sharpes = rng.normal(0.05, 0.04, size=88)
    # ensure the best cell's own Sharpe is among the trial sharpes' upper range
    m = dr.ds.sharpe_and_moments(best)
    trial_sharpes = np.append(trial_sharpes, m["sharpe"])
    psr = dr.ds.probabilistic_sharpe_ratio(m["sharpe"], m["T"], m["skew"], m["kurtosis"], 0.0)
    res = dr.report_multi_trial(best, trial_sharpes, "multi")
    assert res["mode"] == "dsr"
    assert res["sr0"] > 0.0                 # a real haircut benchmark
    assert res["dsr"] <= psr + 1e-9         # deflated at or below plain PSR


def test_more_trials_deflate_harder():
    rng = np.random.default_rng(6)
    best = rng.normal(0.012, 0.06, size=250)
    small_search = rng.normal(0.05, 0.04, size=5)
    big_search = rng.normal(0.05, 0.04, size=5000)
    r_small = dr.report_multi_trial(best, small_search, "small")
    r_big = dr.report_multi_trial(best, big_search, "big")
    # more trials -> higher E[max SR] haircut -> lower DSR
    assert r_big["sr0"] > r_small["sr0"]
    assert r_big["dsr"] < r_small["dsr"]


def test_zero_var_trials_no_deflation():
    """If every trial has the SAME Sharpe (var_trials==0), there is no dispersion to
    exploit and DSR must collapse back to PSR-vs-zero (sr0==0)."""
    rng = np.random.default_rng(7)
    best = rng.normal(0.012, 0.06, size=250)
    identical = np.full(88, 0.05)           # zero variance across trials
    res = dr.report_multi_trial(best, identical, "flat-search")
    # np.var of identical values is ~1e-17 float noise, not literally 0, so the
    # E[max] haircut is negligible rather than exactly zero — assert it collapses.
    assert res["sr0"] < 1e-6


# --------------------------------------------------------------------------- #
# 4. Selection loader parses the real CSV (or skips if absent).               #
# --------------------------------------------------------------------------- #
def test_selection_loader_on_real_csv_if_present():
    path = os.path.join(dr.RESEARCH, "selection_backtest_results.csv")
    if not os.path.exists(path):
        pytest.skip("selection_backtest_results.csv not present")
    rets = dr.load_selection_trade_returns(path)
    assert rets.ndim == 1
    assert rets.size >= 2
    assert np.isfinite(rets).all()


def test_selection_loader_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        dr.load_selection_trade_returns(str(tmp_path / "nope.csv"))


def test_selection_loader_too_few_rows_raises(tmp_path):
    p = tmp_path / "thin.csv"
    p.write_text("machine_trade,symbol,entry_date\ntrade,AAA,2020-01-01,1,1,,2020-02-01,0.05\n")
    with pytest.raises(ValueError):
        dr.load_selection_trade_returns(str(p))


# --------------------------------------------------------------------------- #
# 5. Synthetic demo runs end-to-end.                                          #
# --------------------------------------------------------------------------- #
def test_demo_runs_both_paths():
    lines: list[str] = []
    res = dr.run_demo(lines)
    assert "single" in res and "multi" in res
    assert res["single"]["mode"] == "psr_only"
    assert res["multi"]["mode"] == "dsr"
    assert any("SYNTHETIC DEMONSTRATION" in ln for ln in lines)


def test_build_report_demo_smoke():
    lines, res = dr.build_report("demo")
    assert lines and any("Deflated / Probabilistic Sharpe" in ln for ln in lines)
