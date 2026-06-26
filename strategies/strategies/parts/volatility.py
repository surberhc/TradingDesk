"""
volatility.py — Volatility Multiplier (SUBORDINATE). SPEC.md §8.

A TRIM, not a cutter. Within the regime's equity band it picks WHERE in the band
the equity allocation sits, using realized volatility. As realized vol rises past
the active client version's target-vol range, the multiplier steps down through
gentle buckets (config.VOL_BUCKET_MULTIPLIERS, default 100% / 85% / 70%) applied
to the band's HIGH point, with a hard FLOOR at the band's bottom.

It can NEVER set equity below the regime band floor and is not an independent
de-risking lever — the regime engine owns de-risking; this only chooses a point
inside the band it already set.

Correctness (SPEC §3): realized vol uses a trailing 63-day window, so a date-T
trim uses only data on/before T. No look-ahead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies import config


def realized_vol(spy: pd.Series, lookback: int = config.VOL_LOOKBACK_DAYS) -> pd.Series:
    """Trailing annualized realized volatility of daily returns (causal)."""
    return spy.pct_change().rolling(lookback).std() * np.sqrt(252)


def _target_band(version: str) -> tuple[float, float]:
    """The (low, high) target-vol range for a client version (config §8/§10)."""
    if version not in config.TARGET_VOL_BY_VERSION:
        raise KeyError(f"unknown client version: {version}")
    return config.TARGET_VOL_BY_VERSION[version]


def volatility_multiplier(vol: float, version: str = config.ACTIVE_VERSION) -> float:
    """
    Map a realized-vol level to a band multiplier (config.VOL_BUCKET_MULTIPLIERS).

    Buckets relative to the version's target-vol range (lo, hi):
      vol <= lo            -> calm     -> multipliers[0] (default 1.00)
      lo < vol <= hi       -> moderate -> multipliers[1] (default 0.85)
      vol > hi             -> elevated -> multipliers[2] (default 0.70)
    NaN vol (warm-up) is treated as calm so early dates are not spuriously trimmed.
    """
    calm, moderate, elevated = config.VOL_BUCKET_MULTIPLIERS
    lo, hi = _target_band(version)
    if pd.isna(vol) or vol <= lo:
        return calm
    if vol <= hi:
        return moderate
    return elevated


def equity_target(
    band: tuple[float, float], vol: float, version: str = config.ACTIVE_VERSION
) -> float:
    """
    Pick the equity allocation point inside the regime band (SPEC §8).

    equity = max(band_low, multiplier * band_high) — the multiplier trims down
    from the band's high as vol rises, but the band floor is a hard minimum so the
    trim can never de-risk below what the regime engine permitted.
    """
    band_low, band_high = band
    target = volatility_multiplier(vol, version) * band_high
    return max(band_low, target)
