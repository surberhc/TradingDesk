"""
overfit_gates.py — Overfit-diagnostics instrumentation (anti-curve-fit, rule #1).

ADDITIVE MEASUREMENT ONLY. Nothing here changes a strategy, reads a file, or
touches config. These are pure functions that take metric values a caller has
already computed (Sharpe, CAGR, Calmar, ...) and return a *categorical verdict*
plus the raw numbers behind it — so a human, not a threshold, makes the call.

Two diagnostics:

  1. is_oos_divergence()  — compares an in-sample metric to its out-of-sample
     twin and flags divergence in EITHER direction. Classic overfit is
     IS >> OOS (the fit was inflated on the tuning window). But OOS >> IS is
     *also* a warning ("lucky OOS"): the out-of-sample window happened to be
     kind, which is just as much a reason to distrust the number.

  2. is_too_good()        — flags a single metric that clears a "too-good-to-be-
     true" ceiling (e.g. Sharpe 2.5). At that level the honest read is usually
     "the asset did the work, not the strategy" (or a leak / survivorship /
     look-ahead bug), not genuine edge.

THRESHOLDS ARE PROPOSALS, NOT GATES. Every tolerance below is a documented,
overridable default argument — a starting point for Andrew to bless, tighten,
or reject. None of them live in the frozen config, and none of them decide
pass/fail on their own; they decide what gets a second look.
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Proposed defaults (all overridable per-call; NONE are enshrined gates).
#
#   DEFAULT_DEGRADATION_TOL — how much an OOS metric may fall below its IS value
#       before we flag "overfit-suspect". 0.30 => OOS must retain >= ~70% of IS.
#       Symmetric on the other side: OOS more than this fraction ABOVE IS is
#       flagged "lucky-suspect". A round, defensible starting point; needs
#       Andrew's blessing before it means anything.
#
#   DEFAULT_SHARPE_CEILING — the "too-good-to-be-true" Sharpe line. 2.5 is the
#       value called out in the source video as the point where a long-horizon
#       daily-rebalanced strategy's Sharpe stops being plausibly skill. Also a
#       proposal pending Andrew's blessing.
# ---------------------------------------------------------------------------
DEFAULT_DEGRADATION_TOL: float = 0.30
DEFAULT_SHARPE_CEILING: float = 2.5

# Denominators below this magnitude are treated as "≈ zero": a ratio through
# them is meaningless, so we fall back to the absolute delta and say so.
_NEAR_ZERO = 1e-9


def _is_number(x) -> bool:
    """True only for a real, finite number (rejects None, NaN, ±inf, strings)."""
    try:
        return isinstance(x, (int, float)) and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def is_oos_divergence(
    in_sample: float,
    out_of_sample: float,
    tol: float = DEFAULT_DEGRADATION_TOL,
    higher_is_better: bool = True,
    metric_name: str = "metric",
) -> dict:
    """
    Flag in-sample vs out-of-sample divergence in EITHER direction.

    Compares an in-sample metric value to its out-of-sample twin (same metric,
    two windows — e.g. from metrics.split_walk_forward). Returns the raw gap,
    the retention ratio, and a categorical verdict:

        "clean"           — OOS held within `tol` of IS (both directions).
        "overfit-suspect" — OOS degraded by more than `tol` vs IS (the classic
                            inflated-fit tell: strong on the tuning window,
                            weak after it).
        "lucky-suspect"   — OOS was better than IS by more than `tol` ("lucky
                            OOS": the out-of-sample window was unusually kind;
                            distrust the number, don't celebrate it).
        "undefined"       — inputs missing / non-finite, or IS ≈ 0 so a ratio
                            is meaningless (verdict then leans on the raw delta).

    Direction handling
    ------------------
    `higher_is_better` controls what "degraded" means. For Sharpe / CAGR /
    Calmar (higher = better) a *drop* from IS to OOS is the overfit tell. For a
    loss-style metric (e.g. max drawdown magnitude, higher = worse) set
    higher_is_better=False and "degraded" flips to mean OOS got worse (larger).

    `tol` is a PROPOSED default (0.30 => retain >= ~70% of IS), overridable and
    non-binding — it decides what gets a second look, not pass/fail.

    Pure: no I/O, no globals mutated. Never raises on bad input — returns a
    verdict of "undefined" with a human-readable reason instead.

    Returns
    -------
    dict with keys:
        metric, in_sample, out_of_sample, delta, ratio, retention,
        tol, higher_is_better, verdict, reason
    where:
        delta      = out_of_sample - in_sample (signed, raw units)
        ratio      = out_of_sample / in_sample (None if IS ≈ 0)
        retention  = signed fraction of IS retained OOS, oriented so that
                     >0 means "OOS better than IS" and <0 means "OOS worse"
                     AFTER accounting for higher_is_better (None if IS ≈ 0)
    """
    result = {
        "metric": metric_name,
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
        "delta": None,
        "ratio": None,
        "retention": None,
        "tol": tol,
        "higher_is_better": higher_is_better,
        "verdict": "undefined",
        "reason": "",
    }

    if not _is_number(in_sample) or not _is_number(out_of_sample):
        result["reason"] = "in_sample and/or out_of_sample is missing or non-finite"
        return result

    is_v = float(in_sample)
    oos_v = float(out_of_sample)
    result["delta"] = oos_v - is_v

    # Improvement expressed so that positive == "OOS better than IS", regardless
    # of whether the metric is maximized or minimized.
    signed_improvement = (oos_v - is_v) if higher_is_better else (is_v - oos_v)

    if abs(is_v) < _NEAR_ZERO:
        # A ratio through ≈0 is meaningless; fall back to the absolute delta.
        result["reason"] = (
            "in_sample ≈ 0; ratio undefined — verdict from absolute delta"
        )
        if abs(signed_improvement) <= tol:
            # Both essentially at zero, or a tiny move — call it clean.
            result["verdict"] = "clean"
        elif signed_improvement < 0:
            result["verdict"] = "overfit-suspect"
        else:
            result["verdict"] = "lucky-suspect"
        return result

    ratio = oos_v / is_v
    result["ratio"] = ratio

    # Retention as a signed fraction of the IS magnitude: how much better (+) or
    # worse (-) OOS came in, normalized by |IS| and oriented by direction.
    retention = signed_improvement / abs(is_v)
    result["retention"] = retention

    if retention < -tol:
        result["verdict"] = "overfit-suspect"
        result["reason"] = (
            f"OOS {metric_name} degraded {abs(retention):.0%} vs IS "
            f"(tol {tol:.0%})"
        )
    elif retention > tol:
        result["verdict"] = "lucky-suspect"
        result["reason"] = (
            f"OOS {metric_name} beat IS by {retention:.0%} "
            f"(tol {tol:.0%}) — lucky-OOS warning"
        )
    else:
        result["verdict"] = "clean"
        result["reason"] = (
            f"OOS {metric_name} within {tol:.0%} of IS "
            f"(moved {retention:+.0%})"
        )
    return result


def is_too_good(
    value: float,
    ceiling: float = DEFAULT_SHARPE_CEILING,
    metric_name: str = "Sharpe",
) -> dict:
    """
    Flag a "too-good-to-be-true" metric — the "asset did the work, not the
    strategy" signal.

    When a metric (canonically Sharpe) clears `ceiling`, the honest first read
    is skepticism: at a high enough Sharpe on a long, daily-rebalanced backtest
    the number is more likely a leak (look-ahead / survivorship / an asset on a
    once-in-a-generation run) than durable edge. This does NOT prove a bug — it
    marks the result for scrutiny.

    `ceiling` is a PROPOSED default (2.5, from the source video), overridable
    and non-binding.

    Pure and total: never raises; non-finite / missing input returns a verdict
    of "undefined".

    Returns
    -------
    dict with keys:
        metric, value, ceiling, exceeds_by, verdict, reason
    where:
        exceeds_by = value - ceiling (signed; positive == over the line)
        verdict    = "too-good-suspect" | "plausible" | "undefined"
    """
    result = {
        "metric": metric_name,
        "value": value,
        "ceiling": ceiling,
        "exceeds_by": None,
        "verdict": "undefined",
        "reason": "",
    }

    if not _is_number(value):
        result["reason"] = "value is missing or non-finite"
        return result
    if not _is_number(ceiling):
        result["reason"] = "ceiling is missing or non-finite"
        return result

    v = float(value)
    c = float(ceiling)
    result["exceeds_by"] = v - c

    if v > c:
        result["verdict"] = "too-good-suspect"
        result["reason"] = (
            f"{metric_name} {v:.2f} exceeds ceiling {c:.2f} — "
            f"suspect the asset/leak, not the strategy"
        )
    else:
        result["verdict"] = "plausible"
        result["reason"] = f"{metric_name} {v:.2f} at/under ceiling {c:.2f}"
    return result
