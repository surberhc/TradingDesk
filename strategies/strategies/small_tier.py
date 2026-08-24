"""
small_tier.py - the whole-share SMALL-ACCOUNT tier for every client version.

DECIDED 2026-08-06 for Growth (SmallAccount_Tier_Proposal_2026-08-05.md); EXTENDED
2026-08-24 to Conservative and Balanced.

Fractional and cash-quantity orders are impossible over the TWS socket API, so a small
account cannot hold the full models on target: SPY's ~$766 share is too coarse a quantum.
Instead of tracking badly, such an account holds a TWO-TICKER PROXY of the SAME engine
output - the equity sleeve collapsed to SCHB (~$29.61/share) and everything else collapsed
to USFR, the floating-rate fund the full models already use.

The key property: this is a RENDERING, not a separate strategy. There is ONE engine and one
set of regime decisions; `collapse()` projects whatever that engine produced onto two
tickers. Conservative (Small), Balanced (Small) and Growth (Small) therefore differ from one
another exactly as their full-size parents do - by the equity/defensive split handed to them -
and each one inherits its parent's dynamic risk-on/risk-off behaviour unchanged.

PURE. Reads no data, contacts no broker, decides no regime.
"""
from __future__ import annotations

import pandas as pd

from strategies import config


# --- labels ---------------------------------------------------------------------
def small_label(version: str) -> str:
    """"Balanced" -> "Balanced (Small)". Idempotent."""
    version = str(version).strip()
    if version.endswith(config.SMALL_TIER_SUFFIX):
        return version
    return f"{version}{config.SMALL_TIER_SUFFIX}"


def is_small(label: str) -> bool:
    return str(label).strip().endswith(config.SMALL_TIER_SUFFIX)


def parent_version(label: str) -> str:
    """"Balanced (Small)" -> "Balanced". The engine only ever runs a PARENT version."""
    label = str(label).strip()
    if label.endswith(config.SMALL_TIER_SUFFIX):
        return label[: -len(config.SMALL_TIER_SUFFIX)].strip()
    return label


# --- the projection -------------------------------------------------------------
def collapse(weights: "pd.Series") -> "pd.Series":
    """Project a full model's target weights onto {SCHB, USFR}.

    Everything the engine classes as EQUITY (broad-beta core + sectors) becomes SCHB; every
    other holding - defensive, real-asset, cash - becomes USFR. Classifying the EMITTED
    TICKERS (rather than trusting a sleeve-fraction dict) means this stays correct if the
    sleeve bookkeeping ever changes, and it handles the sector-neutral arm for free.

    Returns a 2-entry Series summing to 1. An all-defensive target collapses to 100% USFR,
    which is the correct risk-off answer, not a degenerate case.
    """
    equity_tickers = set(config.EQUITY_CORE) | set(config.SECTORS)
    total = float(weights.sum())
    if total <= 0:
        return pd.Series({config.SMALL_TIER_EQUITY: 0.0, config.SMALL_TIER_DEFENSIVE: 1.0})
    eq = float(sum(float(w) for t, w in weights.items() if t in equity_tickers)) / total
    eq = min(max(eq, 0.0), 1.0)
    return pd.Series({config.SMALL_TIER_EQUITY: eq,
                      config.SMALL_TIER_DEFENSIVE: 1.0 - eq})


# --- NAV tiering ----------------------------------------------------------------
def _bounds(version: str):
    over = config.SMALL_TIER_THRESHOLD_BY_VERSION.get(parent_version(version), {})
    return (float(over.get("threshold", config.SMALL_TIER_THRESHOLD)),
            float(over.get("promote_at", config.SMALL_TIER_PROMOTE_AT)),
            float(over.get("demote_at", config.SMALL_TIER_DEMOTE_AT)))


def tier_for(nav: float, version: str, current_label: str | None = None) -> str:
    """Which model label this account should hold, WITH HYSTERESIS.

    `current_label` is what it holds today (None for a new/unassigned account). Hysteresis is
    the point: an account oscillating around the boundary must not switch models every month,
    so promotion and demotion use different, deliberately separated levels. A brand-new
    account has no incumbent to be sticky about and uses the plain threshold.
    """
    parent = parent_version(version)
    threshold, promote_at, demote_at = _bounds(parent)
    nav = float(nav)
    if current_label is None:
        return small_label(parent) if nav < threshold else parent
    if is_small(current_label):
        return parent if nav >= promote_at else small_label(parent)
    return small_label(parent) if nav < demote_at else parent


# --- whole-share feasibility (validation helper) --------------------------------
def whole_share_fit(weights: "pd.Series", nav: float, prices: dict) -> dict:
    """Nearest-whole-share realization of `weights` at `nav`, and the drift it forces.

    Returns shares, realized weights, per-holding drift, `total_drift` (sum of absolute
    deviations) and `feasible` (every non-trivial target gets at least one share). This is
    the measurement behind the tier's threshold - it is a diagnostic, never a decision.
    """
    shares, realized = {}, {}
    for t, w in weights.items():
        px = float(prices[t])
        shares[t] = int((float(w) * nav) // px)
        realized[t] = shares[t] * px / nav if nav else 0.0
    drift = {t: realized[t] - float(weights[t]) for t in weights.index}
    feasible = all(shares[t] >= 1 for t, w in weights.items() if float(w) >= 0.05)
    return {"shares": shares, "realized": realized, "drift": drift,
            "total_drift": float(sum(abs(d) for d in drift.values())),
            "max_drift": float(max((abs(d) for d in drift.values()), default=0.0)),
            "cash_left": float(1.0 - sum(realized.values())), "feasible": feasible}
