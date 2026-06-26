"""
real_assets.py — Real-asset sleeve: a DIVERSIFIED inflation-hedge basket.
SPEC.md §1 (revised), §6, §12.

The real-asset sleeve is now a small basket of genuine real-asset return legs —
GOLD and BROAD COMMODITIES — not a single standalone gold pick. Diagnostics:
gold and commodities are ~uncorrelated (~0.05), so a gold+commodity basket has
LOWER volatility (~13.5%) than either leg alone (~16-18%) — it diversifies rather
than adds risk. TIPS is excluded from this sleeve (it behaves like Treasuries; it
lives in the defensive sleeve).

Each leg is filled ONLY when it independently passes the trend+momentum gate
(above 200-day MA AND positive 3m AND positive 6m return), so a falling commodity
is never held. The present legs are weighted by INVERSE VOLATILITY within the
sleeve, so the more-volatile leg can't dominate the sleeve's risk. The §12 category
caps (gold 25%, commodities 20%) remain hard ceilings. The sleeve's overall SIZE is
set by config.REAL_ASSET_SLEEVE_TARGET (in portfolio.py), not here.

Correctness (SPEC §3): trailing windows only -> a date-T basket uses only data
on/before T. No look-ahead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies import config
from strategies.parts import _gates as gates

TRADING_DAYS_PER_MONTH = 21  # units conversion (see regime.py), not a tunable

# Portfolio cap per real-asset category (SPEC §12).
_CATEGORY_CAP = {
    "gold": config.CAP_MAX_GOLD,
    "commodities": config.CAP_MAX_COMMODITIES,
    "tips": config.CAP_MAX_TIPS,
}


def _trailing_return(series: pd.Series, months: int, asof: pd.Timestamp) -> float:
    hist = series.loc[:asof]
    lag = months * TRADING_DAYS_PER_MONTH
    if len(hist) <= lag or pd.isna(hist.iloc[-1]):
        return float("nan")
    base = hist.iloc[-1 - lag]
    if pd.isna(base):
        return float("nan")
    return hist.iloc[-1] / base - 1.0


def _above_trend(series: pd.Series, asof: pd.Timestamp, window: int) -> bool:
    hist = series.loc[:asof]
    if hist.notna().sum() < window:
        return False
    return bool(hist.iloc[-1] > hist.tail(window).mean())


def _trailing_vol(series: pd.Series, asof: pd.Timestamp, lookback: int) -> float:
    """Annualized trailing volatility of daily returns up to `asof` (causal)."""
    daily = series.loc[:asof].pct_change().dropna()
    if len(daily) < 2:
        return float("nan")
    window = daily.tail(lookback) if len(daily) >= lookback else daily
    return float(window.std() * np.sqrt(252))


def _best_in_category(prices: pd.DataFrame, tickers, asof: pd.Timestamp) -> dict | None:
    """Best trend-gated ETF in a category (strongest 3m/6m momentum), or None."""
    lb_short, lb_long = 3, config.TREND_RETURN_MONTHS  # 3m and 6m momentum
    best: dict | None = None
    for ticker in tickers:
        if ticker not in prices.columns:
            continue
        if not gates.is_above_asof(prices[ticker], asof, buffer=config.trend_margin("realasset")):
            continue
        r_short = _trailing_return(prices[ticker], lb_short, asof)
        r_long = _trailing_return(prices[ticker], lb_long, asof)
        if pd.isna(r_short) or pd.isna(r_long) or r_short <= 0 or r_long <= 0:
            continue
        vol = _trailing_vol(prices[ticker], asof, config.REAL_ASSET_VOL_LOOKBACK)
        if pd.isna(vol) or vol <= 0:
            continue
        score = (r_short + r_long) / 2.0
        if best is None or score > best["score"]:
            best = {"ticker": ticker, "score": score, "vol": vol}
    return best


def select_real_basket(prices: pd.DataFrame, asof) -> dict | None:
    """
    Build the diversified real-asset basket for a signal date, or None if empty.

    Returns {"legs": [{ticker, category, cap, weight, vol, score}, ...]} where the
    leg weights are inverse-volatility, normalized across the legs that passed the
    trend gate (summing to 1). None when no real asset is trending.
    """
    asof = pd.Timestamp(asof)
    legs: list[dict] = []
    for category, tickers in config.REAL_ASSET_BASKET.items():
        pick = _best_in_category(prices, tickers, asof)
        if pick is not None:
            pick["category"] = category
            pick["cap"] = _CATEGORY_CAP[category]
            legs.append(pick)

    if not legs:
        return None

    inv = [1.0 / leg["vol"] for leg in legs]
    total = sum(inv)
    for leg, w in zip(legs, inv):
        leg["weight"] = w / total
    return {"legs": legs}
