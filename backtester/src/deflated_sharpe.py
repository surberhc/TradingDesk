"""
deflated_sharpe.py — Multiple-comparisons statistics for Sharpe ratios.

Additive MEASUREMENT instrumentation only — this module computes nothing that
feeds a strategy, sizing, or order. It answers one question honestly: given that
an observed Sharpe was SELECTED as the best (or one of many) out of N trials, and
given the return series is non-normal (skewed, fat-tailed) and finite-length, how
likely is the true Sharpe actually positive? That is the anti-curve-fit yardstick.

REVIEWED / ACCEPTED 2026-07-03 by Andrew as the project's formal
multiple-comparisons statistic. There are no free thresholds to bless here — the
DSR/PSR/E[max SR] formulas are principled (Bailey/López de Prado), not tuned
knobs — so the acceptance is of the method itself, not of any parameter value.

Reference
---------
Bailey, D. H., & López de Prado, M. (2014). "The Deflated Sharpe Ratio:
Correcting for Selection Bias, Backtest Overfitting, and Non-Normality."
Journal of Portfolio Management, 40(5), 94-107.

The three pieces (all probabilities/ratios, no free knobs):

1. expected_max_sharpe(...)  — E[max SR] over N independent trials (the "haircut"
   a maximum Sharpe deserves purely from having searched N candidates). This is
   the benchmark SR* used to deflate.

2. probabilistic_sharpe_ratio(...)  — PSR: P(true SR > SR*) for a single track
   record, adjusting for sample length T and the return series' skew & kurtosis.

3. deflated_sharpe_ratio(...)  — DSR: PSR evaluated at SR* = E[max SR], i.e. the
   observed Sharpe deflated for BOTH the number of trials it was picked from AND
   non-normal moments. Returns the DSR probability plus intermediate pieces.

All Sharpe inputs are expressed PER OBSERVATION (non-annualized) and the
kurtosis is the FULL (non-excess) kurtosis, kurt=3 for a Gaussian — matching the
Bailey/López de Prado formulas. Helper `annualization` notes are in the docstrings.

scipy is not a dependency of this venv, so the standard-normal CDF and its inverse
(PPF) are implemented here in closed form (Abramowitz & Stegun rational
approximations, ~1e-7 accuracy) — no new heavy dependencies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Euler-Mascheroni constant — used in the E[max] Gaussian-maximum approximation.
EULER_MASCHERONI = 0.5772156649015329


# --------------------------------------------------------------------------- #
# Standard-normal CDF and inverse CDF (PPF), no scipy.                          #
# --------------------------------------------------------------------------- #
def norm_cdf(x: float) -> float:
    """Standard-normal CDF Phi(x) via the error function (exact to fp precision)."""
    return 0.5 * math.erfc(-float(x) / math.sqrt(2.0))


def norm_ppf(p: float) -> float:
    """
    Inverse standard-normal CDF (quantile / PPF): the z such that Phi(z) = p.

    Peter Acklam's rational approximation (relative error < 1.15e-9 in the central
    region), refined by one Halley step against `norm_cdf` for full accuracy.
    Returns -inf / +inf at the p=0 / p=1 boundaries.
    """
    p = float(p)
    if not (0.0 <= p <= 1.0) or math.isnan(p):
        raise ValueError(f"norm_ppf requires 0 <= p <= 1, got {p!r}")
    if p == 0.0:
        return -math.inf
    if p == 1.0:
        return math.inf

    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1.0 - 0.02425

    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    elif p <= phigh:
        q = p - 0.5
        r = q * q
        x = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
            (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)

    # One Halley refinement step against the exact CDF.
    e = norm_cdf(x) - p
    u = e * math.sqrt(2.0 * math.pi) * math.exp(x * x / 2.0)
    x = x - u / (1.0 + x * u / 2.0)
    return x


# --------------------------------------------------------------------------- #
# Return-series moment helper.                                                 #
# --------------------------------------------------------------------------- #
def sharpe_and_moments(returns: pd.Series | np.ndarray) -> dict:
    """
    Compute the per-observation Sharpe, skew, and FULL kurtosis of a return
    series — the exact inputs PSR/DSR need. Uses population moments (ddof=0),
    consistent with `metrics.sharpe`. Returns a dict:
        {'sharpe', 'skew', 'kurtosis', 'T'}
    kurtosis is non-excess (Gaussian = 3). Raises on T < 2 or zero variance.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    T = r.size
    if T < 2:
        raise ValueError(f"need >= 2 finite returns, got {T}")
    mu = r.mean()
    sd = r.std(ddof=0)
    if sd == 0:
        raise ValueError("return series has zero variance; Sharpe undefined")
    z = (r - mu) / sd
    return {
        "sharpe": float(mu / sd),
        "skew": float((z ** 3).mean()),
        "kurtosis": float((z ** 4).mean()),  # non-excess: Gaussian = 3
        "T": int(T),
    }


# --------------------------------------------------------------------------- #
# 1. Expected maximum Sharpe over N independent trials.                        #
# --------------------------------------------------------------------------- #
def expected_max_sharpe(n_trials: int, var_trials: float) -> float:
    """
    E[max SR] — the Sharpe a *maximum* over N independent trials would reach by
    chance alone, when every trial's true Sharpe is zero and trial Sharpes vary
    with variance `var_trials`.

    Bailey/López de Prado (2014), eq. for the expected maximum of N i.i.d. normals:

        SR0 = sqrt(var_trials) * [ (1 - gamma) * Z^-1(1 - 1/N)
                                   + gamma * Z^-1(1 - 1/(N*e)) ]

    where gamma is the Euler-Mascheroni constant, Z^-1 is `norm_ppf`, and e is
    Euler's number. This is the benchmark SR* that a selected maximum Sharpe must
    beat to be considered non-spurious.

    Inputs
    ------
    n_trials : int  — N, the number of independent strategy configurations tried
        (grid points, arms, variants). More trials => a higher chance-maximum bar.
    var_trials : float  — the variance (spread) of the Sharpe estimates ACROSS the
        N trials, in the SAME per-observation units as the observed Sharpe. Wider
        spread => a higher chance-maximum. Must be >= 0.

    Edge cases
    ----------
    N <= 1  => 0.0 (no selection took place; nothing to deflate for).
    var_trials == 0 => 0.0 (all trials identical; no dispersion to exploit).
    """
    N = int(n_trials)
    v = float(var_trials)
    if v < 0:
        raise ValueError(f"var_trials must be >= 0, got {v}")
    if N <= 1 or v == 0.0:
        return 0.0
    q1 = norm_ppf(1.0 - 1.0 / N)
    q2 = norm_ppf(1.0 - 1.0 / (N * math.e))
    return float(math.sqrt(v) * ((1.0 - EULER_MASCHERONI) * q1 + EULER_MASCHERONI * q2))


# --------------------------------------------------------------------------- #
# 2. Probabilistic Sharpe Ratio.                                              #
# --------------------------------------------------------------------------- #
def probabilistic_sharpe_ratio(
    observed_sharpe: float,
    T: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    benchmark_sharpe: float = 0.0,
) -> float:
    """
    PSR = P(true SR > benchmark_sharpe) for a single track record, given the
    estimation error of a Sharpe computed from T observations of a series with the
    supplied skew and (full, non-excess) kurtosis.

    Bailey/López de Prado (2014):

        PSR(SR*) = Phi( (SR_hat - SR*) * sqrt(T - 1)
                        / sqrt(1 - g1*SR_hat + (g2-1)/4 * SR_hat^2) )

    where SR_hat = observed_sharpe (per-observation), SR* = benchmark_sharpe,
    g1 = skew, g2 = kurtosis (Gaussian g2 = 3, so the (g2-1)/4 term = 1/2 there),
    and Phi = `norm_cdf`.

    Interpretation: higher T tightens the estimate (PSR -> 0/1 faster); NEGATIVE
    skew and EXCESS kurtosis inflate the denominator (more estimation risk) and
    push PSR toward 0.5, i.e. LOWER confidence for a positive observed Sharpe.

    All Sharpe arguments must be in the SAME units (per-observation, not
    annualized). Returns a probability in (0, 1).

    Edge cases
    ----------
    T < 2  => raises (no estimation-error notion).
    Non-positive denominator (pathological moment combos) => raises.
    """
    T = int(T)
    if T < 2:
        raise ValueError(f"PSR needs T >= 2, got {T}")
    sr = float(observed_sharpe)
    var_term = 1.0 - float(skew) * sr + (float(kurtosis) - 1.0) / 4.0 * sr * sr
    if var_term <= 0.0:
        raise ValueError(
            f"non-positive PSR variance term ({var_term:.4g}); "
            "moment inputs are inconsistent (check skew/kurtosis)"
        )
    z = (sr - float(benchmark_sharpe)) * math.sqrt(T - 1) / math.sqrt(var_term)
    return norm_cdf(z)


# --------------------------------------------------------------------------- #
# 3. Deflated Sharpe Ratio.                                                    #
# --------------------------------------------------------------------------- #
@dataclass
class DSRResult:
    """Deflated Sharpe result plus the intermediate pieces that produced it."""
    dsr: float                 # P(true SR > E[max SR]); the headline number in [0,1]
    sr0: float                 # E[max SR] benchmark used (expected_max_sharpe)
    observed_sharpe: float     # per-observation SR fed in
    T: int
    n_trials: int
    var_trials: float
    skew: float
    kurtosis: float

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (f"DSRResult(dsr={self.dsr:.4f}, sr0={self.sr0:.4f}, "
                f"SR={self.observed_sharpe:.4f}, T={self.T}, N={self.n_trials})")


def deflated_sharpe_ratio(
    observed_sharpe: float,
    T: int,
    n_trials: int,
    var_trials: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> DSRResult:
    """
    DSR — the probability that the true Sharpe exceeds what a MAXIMUM over
    `n_trials` independent trials would reach by chance, i.e. PSR evaluated at
    SR* = E[max SR]. This deflates an observed Sharpe for the number of trials it
    was selected from AND for non-normal moments.

    A high DSR (say > 0.95) means the Sharpe survives the multiple-comparisons and
    non-normality haircut. A DSR near 0.5 or below means the Sharpe is consistent
    with selection luck — the anti-curve-fit red flag.

    Inputs
    ------
    observed_sharpe : per-observation Sharpe of the SELECTED strategy.
    T               : number of return observations behind that Sharpe.
    n_trials        : N, how many configurations were searched to find it.
    var_trials      : variance of Sharpe estimates across those N trials
                      (per-observation units). If unknown, a common conservative
                      proxy is the variance of the trial Sharpes you actually ran.
    skew, kurtosis  : moments of the selected strategy's return series (full,
                      non-excess kurtosis; Gaussian = 3).

    Returns a `DSRResult`. When n_trials <= 1 or var_trials == 0, SR* collapses to
    0 and DSR == PSR against zero (no deflation applied — nothing was selected).

    Units: annualized Sharpes must be de-annualized first (divide by sqrt(periods
    per year)) so `observed_sharpe`, the SR0 benchmark, and `var_trials` all share
    per-observation units.
    """
    sr0 = expected_max_sharpe(n_trials, var_trials)
    dsr = probabilistic_sharpe_ratio(
        observed_sharpe=observed_sharpe,
        T=T,
        skew=skew,
        kurtosis=kurtosis,
        benchmark_sharpe=sr0,
    )
    return DSRResult(
        dsr=float(dsr),
        sr0=float(sr0),
        observed_sharpe=float(observed_sharpe),
        T=int(T),
        n_trials=int(n_trials),
        var_trials=float(var_trials),
        skew=float(skew),
        kurtosis=float(kurtosis),
    )


def deflated_sharpe_from_returns(
    returns: pd.Series | np.ndarray,
    n_trials: int,
    var_trials: float,
) -> DSRResult:
    """
    Convenience wrapper: compute Sharpe/skew/kurtosis/T from a raw (per-observation)
    return series, then call `deflated_sharpe_ratio`. `var_trials` and `n_trials`
    still come from the SEARCH that produced this series and cannot be inferred
    from a single track record.
    """
    m = sharpe_and_moments(returns)
    return deflated_sharpe_ratio(
        observed_sharpe=m["sharpe"],
        T=m["T"],
        n_trials=n_trials,
        var_trials=var_trials,
        skew=m["skew"],
        kurtosis=m["kurtosis"],
    )


# --------------------------------------------------------------------------- #
# EXPERIMENTAL — White's Reality Check / Hansen SPA stub.                       #
# --------------------------------------------------------------------------- #
def reality_check_pvalue(
    trial_returns: np.ndarray,
    n_bootstrap: int = 1000,
    block: int = 10,
    benchmark_returns: np.ndarray | None = None,
    seed: int = 0,
) -> float:
    """
    EXPERIMENTAL — White's Reality Check (2000) bootstrap p-value for "is the BEST
    of these trials better than the benchmark, after accounting for the search?"

    This is a lightweight stationary/moving-block-bootstrap stub, provided for
    convenience alongside the DSR (which is the priority instrument). It does NOT
    implement Hansen's (2005) SPA studentization/recentering refinements — treat
    the number as indicative only.

    Parameters
    ----------
    trial_returns : 2-D array, shape (T, N) — per-observation excess returns of
        each of the N trials over the SAME T periods (already net of the
        benchmark if `benchmark_returns` is None).
    benchmark_returns : optional (T,) benchmark; subtracted from every column.
    n_bootstrap, block, seed : moving-block-bootstrap controls.

    Returns a p-value in [0, 1]: small => the best trial's outperformance is
    unlikely to be pure luck given the search.
    """
    X = np.asarray(trial_returns, dtype=float)
    if X.ndim != 2:
        raise ValueError("trial_returns must be 2-D (T, N)")
    if benchmark_returns is not None:
        X = X - np.asarray(benchmark_returns, dtype=float).reshape(-1, 1)
    T, N = X.shape
    if T < 2 or N < 1:
        raise ValueError("need T >= 2 and N >= 1")
    block = max(1, min(int(block), T))

    means = X.mean(axis=0)
    V = math.sqrt(T) * means.max()            # observed test statistic
    centered = X - means                       # recenter under the null

    rng = np.random.default_rng(seed)
    count = 0
    for _ in range(int(n_bootstrap)):
        idx: list[int] = []
        while len(idx) < T:
            s = int(rng.integers(0, T))
            idx.extend((s + k) % T for k in range(block))
        idx = np.array(idx[:T])
        boot = centered[idx]
        Vb = math.sqrt(T) * boot.mean(axis=0).max()
        if Vb >= V:
            count += 1
    return float(count) / float(n_bootstrap)
