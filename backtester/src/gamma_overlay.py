"""
gamma_overlay.py — GEX gamma-regime risk-SIZING overlay (backtester-level).

A research overlay that sits on TOP of S0's target weights inside the backtester.
It does NOT edit the shared strategy brain (strategies/) and is a no-op unless
config.GAMMA_OVERLAY_ENABLED is True — so S0 stays BYTE-IDENTICAL when off.

What it does (and only this):
  * Loads the daily market-wide SPX gamma signal (SPX_gex_daily.parquet), whose
    `gamma_state` column is one of {Positive, Neutral, Negative}.
  * At each monthly rebalance it looks up the most-recent gamma_state AS-OF the
    SIGNAL date (strictly on/before — never the execution date), so there is no
    look-ahead.
  * When that state is NEGATIVE (dealers short gamma -> they amplify moves -> the
    tape is fragile), it SCALES DOWN S0's risk-asset weights by a factor and parks
    the trimmed weight in the book's existing cash sleeve. Positive / Neutral /
    unknown states pass S0's weights through UNCHANGED.

Design principle (MSR / S1 verdict): gamma's edge is SIZING + hedge-timing, NOT
direction. The overlay therefore only resizes risk exposure; it never changes which
assets S0 chose, the equity/defense/real split's composition, or any direction. The
re-weighted Series is renormalized to sum to 1.0 (fully invested), exactly like S0.
"""

from __future__ import annotations

import pandas as pd

from strategies import config


def load_gamma_state(gex_file: str | None = None) -> pd.Series:
    """Daily SPX `gamma_state` indexed by a parsed DatetimeIndex (sorted, de-duped).

    The parquet's `date` column is an int in YYYYMMDD form (e.g. 20180102); convert
    it to a proper datetime so it aligns with the price calendar. Returns a string
    Series of {Positive, Neutral, Negative}.
    """
    path = gex_file or config.GAMMA_OVERLAY_GEX_FILE
    df = pd.read_parquet(path)
    dates = pd.to_datetime(df["date"].astype(int).astype(str), format="%Y%m%d")
    state = pd.Series(df["gamma_state"].values, index=dates, name="gamma_state")
    state = state[~state.index.duplicated(keep="last")].sort_index()
    return state


def gamma_state_asof(gamma_state: pd.Series, as_of: pd.Timestamp) -> str | None:
    """Most-recent gamma_state on/before `as_of` (causal as-of lookup).

    Returns None when no gamma reading exists on/before the date (e.g. before the
    GEX history begins) — the caller treats None as "leave S0 unchanged".
    """
    prior = gamma_state.loc[:as_of]
    if len(prior) == 0:
        return None
    val = prior.iloc[-1]
    return None if pd.isna(val) else str(val)


def apply_overlay(
    weights: pd.Series,
    state: str | None,
    *,
    negative_risk_scale: float = None,
    risk_assets: tuple[str, ...] = None,
    cash_ticker: str = None,
) -> pd.Series:
    """Return S0's `weights` resized for the gamma `state` (a pure transform).

    Only a NEGATIVE state changes anything: risk-asset weights are multiplied by
    `negative_risk_scale` and the freed weight is added to the cash sleeve. Any other
    state (Positive / Neutral / None) returns `weights` unchanged. The result is
    renormalized to sum to 1.0, matching S0's fully-invested convention.

    Pure and side-effect-free: it reads only the passed-in weights + state, so it is
    trivially unit-testable and can never introduce look-ahead on its own.
    """
    scale = (config.GAMMA_OVERLAY_NEGATIVE_RISK_SCALE
             if negative_risk_scale is None else negative_risk_scale)
    risk = set(risk_assets if risk_assets is not None
               else config.GAMMA_OVERLAY_RISK_ASSETS)
    fallback_cash = cash_ticker or config.GAMMA_OVERLAY_CASH_TICKER

    if state != "Negative" or scale >= 1.0:
        return weights  # Positive / Neutral / unknown -> S0 untouched

    w = weights.copy()
    held_risk = [t for t in w.index if t in risk]
    if not held_risk:
        return weights  # nothing risky to trim (already fully defensive)

    trimmed = 0.0
    for t in held_risk:
        keep = w[t] * scale
        trimmed += w[t] - keep
        w[t] = keep

    # Park the trimmed weight in the book's existing cash sleeve. Prefer the
    # largest-weight cash-like holding S0 already chose; else the benchmark T-bill.
    cash_like = [t for t in w.index
                 if t in (config.TBILLS + config.FLOATING_RATE)]
    park = max(cash_like, key=lambda t: w[t]) if cash_like else fallback_cash
    w[park] = w.get(park, 0.0) + trimmed

    w = w[w > 1e-9]
    total = w.sum()
    if total > 0:
        w = w / total
    return w.sort_values(ascending=False)
