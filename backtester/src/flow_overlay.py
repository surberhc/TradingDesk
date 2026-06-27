"""
flow_overlay.py — Free price-only FLOW de-risk overlay (backtester-level).

A research overlay that sits on TOP of S0's target weights inside the backtester,
the SAME pattern as gamma_overlay.py. It does NOT edit the shared strategy brain
(strategies/) and is a no-op unless config.FLOW_OVERLAY_ENABLED is True — so S0
stays BYTE-IDENTICAL when off.

What it does (and only this):
  * Reconstructs a daily Bullish/Neutral/Bearish "systematic flow positioning"
    state from PRICE ALONE (SPY adjClose in bt_data), using the exact rule the
    Flow Project verdict (msr/Flow Project/flow_verdict/) settled on — the free,
    reproducible stand-in for the vendor's proprietary regime_flow_risk label:

        Bearish : px < MA200            OR  rvol_rank > vol_top   (downtrend / vol spike)
        Bullish : px > MA200 AND mom>0  AND rvol_rank < vol_calm  (uptrend AND calm)
        Neutral : otherwise        (Bearish evaluated first; de-risk dominates)

    Features are point-in-time: trailing MA, 12m-minus-1m momentum, and a CAUSAL
    trailing-252d percentile rank of 21d realized vol. No look-ahead.
  * At each monthly rebalance it looks up the most-recent flow state AS-OF the
    SIGNAL date (strictly on/before — never the execution date).
  * When that state warrants de-risking it SCALES DOWN S0's risk-asset weights by
    a factor and parks the trimmed weight in the book's existing cash sleeve.

Two pre-specified variants from the verdict (no extra grid):
  G1 "flat"  : risk x0.0 when Bearish; x1.0 Bullish/Neutral.
  G2 "sized" : risk x1.0 / x0.5 / x0.0 for Bullish / Neutral / Bearish.

Design principle (Flow verdict): the flow signal's only real, robust value is
DE-RISKING (drawdown protection); it is DROPPED for direction. The overlay
therefore only resizes risk exposure; it never changes which assets S0 chose or
adds any direction. The re-weighted Series is renormalized to sum to 1.0.

This intentionally mirrors gamma_overlay.py so the two overlays are comparable
and the byte-identical-when-off guarantee is enforced the same way.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies import config

from src import data_loader


# --- per-variant risk multipliers by flow state ------------------------------
# Anything not listed for a variant passes through at 1.0 (no change).
_VARIANT_SCALES = {
    "G1": {"Bearish": 0.0, "Neutral": 1.0, "Bullish": 1.0},   # flat-when-bearish
    "G2": {"Bearish": 0.0, "Neutral": 0.5, "Bullish": 1.0},   # 1 / 0.5 / 0 sizing
}


def _proxy_params() -> dict:
    return dict(
        ma_len=config.FLOW_OVERLAY_MA_LEN,
        mom_long=config.FLOW_OVERLAY_MOM_LONG,
        mom_skip=config.FLOW_OVERLAY_MOM_SKIP,
        rvol_win=config.FLOW_OVERLAY_RVOL_WIN,
        vol_rank_win=config.FLOW_OVERLAY_VOL_RANK_WIN,
        vol_top=config.FLOW_OVERLAY_VOL_TOP,
        vol_calm=config.FLOW_OVERLAY_VOL_CALM,
    )


def compute_flow_state(px: pd.Series | None = None, p: dict | None = None) -> pd.Series:
    """Daily {'Bullish','Neutral','Bearish'} flow state from a price series.

    This is a faithful inline of flow_verdict/flow_proxy.py (compute_features +
    classify) so the backtester has no fragile dependency on the "Flow Project"
    folder path, while staying byte-for-byte the same signal logic. Every feature
    is strictly causal (rolling windows + a trailing percentile rank), so the
    returned state on date t uses only data on/before t. Warm-up rows are NaN ->
    treated as 'unknown' (no de-risk) by the caller via the as-of lookup.
    """
    p = p or _proxy_params()
    if px is None:
        px = data_loader.load_prices([config.FLOW_OVERLAY_PRICE_TICKER])[
            config.FLOW_OVERLAY_PRICE_TICKER
        ]
    px = px.dropna().sort_index().astype(float)

    logret = np.log(px / px.shift(1))
    rvol = logret.rolling(p["rvol_win"]).std() * np.sqrt(252) * 100.0
    ma = px.rolling(p["ma_len"]).mean()
    mom = px.shift(p["mom_skip"]) / px.shift(p["mom_long"]) - 1.0
    # Causal percentile rank of today's rvol within the trailing window.
    volpct = rvol.rolling(p["vol_rank_win"]).apply(
        lambda s: (s.iloc[-1] >= s).mean(), raw=False)

    up = (px > ma) & (mom > 0)
    dn = px < ma
    spike = volpct > p["vol_top"]
    calm = volpct < p["vol_calm"]
    state = np.where(dn | spike, "Bearish",
             np.where(up & calm, "Bullish", "Neutral"))
    out = pd.Series(state, index=px.index, name="flow")
    # Mask warm-up rows where any feature is undefined -> 'unknown' (NaN).
    warm = ma.isna() | mom.isna() | volpct.isna()
    out = out.where(~warm, other=np.nan)
    return out


def flow_state_asof(flow_state: pd.Series, as_of: pd.Timestamp) -> str | None:
    """Most-recent flow state on/before `as_of` (causal as-of lookup).

    Returns None when no (non-NaN) reading exists on/before the date (e.g. during
    warm-up) — the caller treats None as "leave S0 unchanged".
    """
    prior = flow_state.loc[:as_of].dropna()
    if len(prior) == 0:
        return None
    return str(prior.iloc[-1])


def apply_overlay(
    weights: pd.Series,
    state: str | None,
    *,
    variant: str | None = None,
    risk_assets: tuple[str, ...] | None = None,
    cash_ticker: str | None = None,
) -> pd.Series:
    """Return S0's `weights` resized for the flow `state` (a pure transform).

    The risk-asset multiplier comes from the variant's state->scale map (G1 flat
    or G2 sized). A scale of 1.0 (Bullish, or unknown state) returns `weights`
    unchanged. Otherwise risk-asset weights are multiplied by the scale and the
    freed weight is added to the cash sleeve; the result is renormalized to 1.0,
    matching S0's fully-invested convention.

    Pure and side-effect-free: reads only the passed-in weights + state, so it is
    trivially unit-testable and can never introduce look-ahead on its own.
    """
    variant = (variant or config.FLOW_OVERLAY_VARIANT).upper()
    scales = _VARIANT_SCALES[variant]
    scale = scales.get(state, 1.0) if state is not None else 1.0

    risk = set(risk_assets if risk_assets is not None
               else config.FLOW_OVERLAY_RISK_ASSETS)
    fallback_cash = cash_ticker or config.FLOW_OVERLAY_CASH_TICKER

    if scale >= 1.0:
        return weights  # Bullish / unknown / no-op -> S0 untouched

    w = weights.copy()
    held_risk = [t for t in w.index if t in risk]
    if not held_risk:
        return weights  # nothing risky to trim (already fully defensive)

    trimmed = 0.0
    for t in held_risk:
        keep = w[t] * scale
        trimmed += w[t] - keep
        w[t] = keep

    cash_like = [t for t in w.index
                 if t in (config.TBILLS + config.FLOATING_RATE)]
    park = max(cash_like, key=lambda t: w[t]) if cash_like else fallback_cash
    w[park] = w.get(park, 0.0) + trimmed

    w = w[w > 1e-9]
    total = w.sum()
    if total > 0:
        w = w / total
    return w.sort_values(ascending=False)
