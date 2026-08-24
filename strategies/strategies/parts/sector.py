"""
sector.py — Equity Leadership / Sector Engine (SATELLITE, optional). SPEC.md §5.

Broad beta (SPY/VTI/RSP, chosen above their 200-day/10-month trend) is the equity
core. Sector tilt is an OPTIONAL small overlay (config.SECTOR_TILT_PCT, default 0;
allowable 0-30% of the equity sleeve). When on, sectors are scored on a SIMPLE
basis only — 3-month and 6-month relative strength vs SPY — behind a 200-day trend
gate. No 8-factor score. Max single sector 15%; 3-4 sectors when used.

This engine returns how the EQUITY SLEEVE is split (fractions summing to 1):
broad beta plus, optionally, the selected sector tilts. The regime/volatility
engines decide how big the equity sleeve is; this only decides its internal mix.

Correctness (SPEC §3): relative-strength and trend windows are trailing, so a
date-T selection uses only data on/before T. No look-ahead.
"""

from __future__ import annotations

import pandas as pd

from strategies import config
from strategies.parts import _gates as gates

TRADING_DAYS_PER_MONTH = 21  # units conversion (see regime.py), not a tunable


def _trailing_return(series: pd.Series, months: int, asof: pd.Timestamp) -> float:
    """Total return over the trailing N months as of `asof` (causal)."""
    window = series.loc[:asof]
    lag = months * TRADING_DAYS_PER_MONTH
    if len(window) <= lag:
        return float("nan")
    return window.iloc[-1] / window.iloc[-1 - lag] - 1.0


def _above_trend(series: pd.Series, asof: pd.Timestamp, window: int) -> bool:
    """True if price is above its trailing `window`-day MA as of `asof`."""
    hist = series.loc[:asof]
    if hist.notna().sum() < window:
        return False
    return bool(hist.iloc[-1] > hist.tail(window).mean())


def _core_weights(prices: pd.DataFrame, asof: pd.Timestamp) -> pd.Series:
    """Weight the broad-beta members above their 200d trend; SPY fallback.

    Members are weighted by ``config.EQUITY_CORE_WEIGHTS``, RENORMALIZED over whichever
    members actually pass the trend gate on this date (so dropping a member re-splits its
    weight across the survivors in proportion, not equally). A core ticker with no entry in
    that map — or the map being absent/empty — falls back to EQUAL weight, which reproduces
    the pre-2026-08-24 behaviour exactly. See config.EQUITY_CORE for why the weights are
    not equal: they preserve the old three-fund sleeve's effective cap/equal mix after VTI
    was removed as a duplicate of SPY.
    """
    core = [t for t in config.EQUITY_CORE if t in prices.columns]
    above = [t for t in core if gates.is_above_asof(prices[t], asof, buffer=config.trend_margin("sector"))]
    chosen = above or (["SPY"] if "SPY" in prices.columns else core[:1])
    raw = getattr(config, "EQUITY_CORE_WEIGHTS", None) or {}
    weights = pd.Series([float(raw.get(t, 1.0 / len(chosen))) for t in chosen], index=chosen)
    total = float(weights.sum())
    if total <= 0:  # degenerate map -> fall back to equal weight rather than divide by zero
        return pd.Series(1.0 / len(chosen), index=chosen)
    return weights / total


def select_sectors(
    prices: pd.DataFrame,
    asof,
    tilt_pct: float = config.SECTOR_TILT_PCT,
) -> pd.Series:
    """
    Build the equity-sleeve weights for a signal date (fractions summing to 1).

    With tilt_pct <= 0 (default), returns broad beta only. Otherwise allocates
    `tilt_pct` (clamped to 0-30%) of the sleeve across the top 3-4 sectors that
    (a) pass the 200-day trend gate and (b) rank highest on combined 3m+6m
    relative strength vs SPY, each capped at config.SECTOR_MAX_WEIGHT. If no
    sector passes the gate, the tilt reverts to broad beta.
    """
    asof = pd.Timestamp(asof)
    tilt_pct = max(0.0, min(tilt_pct, 0.30))
    core = _core_weights(prices, asof)

    if tilt_pct <= 0.0 or "SPY" not in prices.columns:
        return core

    spy = prices["SPY"]
    lb_short, lb_long = config.SECTOR_RS_LOOKBACKS_MONTHS
    spy_short = _trailing_return(spy, lb_short, asof)
    spy_long = _trailing_return(spy, lb_long, asof)

    # Score sectors that are trading and above the 200-day trend gate.
    scores: dict[str, float] = {}
    for sec in config.SECTORS:
        if sec not in prices.columns:
            continue
        if not _above_trend(prices[sec], asof, config.SECTOR_TREND_GATE_DAYS):
            continue
        rs_short = _trailing_return(prices[sec], lb_short, asof) - spy_short
        rs_long = _trailing_return(prices[sec], lb_long, asof) - spy_long
        if pd.isna(rs_short) or pd.isna(rs_long):
            continue
        scores[sec] = (rs_short + rs_long) / 2.0

    if not scores:
        return core  # nothing leads -> stay in broad beta

    count = config.SECTOR_COUNT_WHEN_USED[1]  # use the high end (4) when available
    ranked = sorted(scores, key=scores.get, reverse=True)[:count]
    per_sector = min(tilt_pct / len(ranked), config.SECTOR_MAX_WEIGHT)

    sleeve = (core * (1.0 - tilt_pct)).to_dict()
    for sec in ranked:
        sleeve[sec] = sleeve.get(sec, 0.0) + per_sector

    weights = pd.Series(sleeve)
    return weights / weights.sum()  # renormalize (cap may leave a small residual)
