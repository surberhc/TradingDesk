"""
_gates.py — trend-gate primitives (the price > moving-average tests).

Centralizes the "is this price above its long trend?" decision so the TREND role
(the fragile one, per the 200d MA study) can be hardened in ONE place across every
engine. The mode is config-driven:

  * "sma"      — production: a single SMA(N) crossover. Byte-identical to the
                 original inline `price > price.rolling(N).mean()`.
  * "ensemble" — vote across config.MA_ENSEMBLE_LOOKBACKS; graded membership in
                 [0,1] = fraction of member lookbacks the price clears. Removes the
                 knife-edge at a single N. Boolean callers majority-vote (>= 0.5).
  * "ema"      — a single EMA(span=N) crossover.

config.MA_GATE_BUFFER_PCT adds a symmetric deadband (price must clear the MA by
this fraction to count as above) to damp whipsaw. N resolves via
config.trend_ma_days() (the TREND role's lookback).

Everything is trailing/causal: a value at date T uses only data on/before T.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies import config


def _lookbacks() -> tuple[int, ...]:
    if config.MA_GATE_MODE == "ensemble":
        return tuple(config.MA_ENSEMBLE_LOOKBACKS)
    return (config.trend_ma_days(),)


def _ma(series, window: int):
    """Trailing MA of `series` at `window` — EMA in 'ema' mode, else SMA."""
    if config.MA_GATE_MODE == "ema":
        return series.ewm(span=window, adjust=False, min_periods=window).mean()
    return series.rolling(window).mean()


def _resolve_buf(buffer):
    """Resolve the trend-gate margin: explicit arg, else the global default."""
    return float(config.MA_GATE_BUFFER_PCT if buffer is None else buffer)


def _gate(series: pd.Series, ma: pd.Series, buf: float) -> pd.Series:
    """Binary 0/1 trend membership of `series` vs its MA, as a float Series.

    buf == 0 : plain crossing (series > ma) — byte-identical to production.
    buf  > 0 : ONE-SIDED EARLY-EXIT MARGIN — price must clear ma*(1+buf) to count
               as 'in trend'; the instant it slips below ma*(1+buf) the gate reads
               not-in-trend. This de-risks early (aligned with the smoothness
               mandate) and removes the knife-edge whipsaw at the MA — the source of
               the 200d lookback fragility. (A symmetric hold-in-band variant was
               tested and was WORSE: it de-risks late and is itself lookback-
               sensitive.) Causal: only same-day price vs trailing MA.
    """
    thresh = ma if buf <= 0.0 else ma * (1.0 + buf)
    return (series > thresh).astype(float).where(ma.notna())


def membership(series: pd.Series, buffer=None) -> pd.Series:
    """Graded trend membership in [0,1] (NaN until the longest lookback has data).

    sma/ema -> 0/1; ensemble -> fraction of member lookbacks cleared. This is the
    primitive the regime score uses directly (it averages such [0,1] sub-signals).
    `buffer` overrides the global early-exit margin (for per-engine scoping).
    """
    lbs = _lookbacks()
    buf = _resolve_buf(buffer)
    valid = _ma(series, max(lbs)).notna()
    acc = None
    for w in lbs:
        gate = _gate(series, _ma(series, w), buf)
        acc = gate if acc is None else acc + gate
    return (acc / len(lbs)).where(valid)


def membership_frame(df: pd.DataFrame, buffer=None) -> pd.DataFrame:
    """Per-column membership() for a wide frame (e.g. the sector panel for breadth)."""
    return df.apply(lambda s: membership(s, buffer=buffer))


def is_above(series: pd.Series, buffer=None) -> pd.Series:
    """Boolean trend Series (majority vote >= 0.5). Drop-in for `price > SMA(N)`."""
    return membership(series, buffer=buffer).fillna(0.0) >= 0.5


def distance(frame):
    """Mean of (price / MA - 1) across lookbacks — the continuous 'how far above
    trend' factor used by the defensive ranking. sma/ema -> single term."""
    lbs = _lookbacks()
    acc = None
    for w in lbs:
        d = frame / _ma(frame, w) - 1.0
        acc = d if acc is None else acc + d
    return acc / len(lbs)


def is_above_asof(series: pd.Series, asof, window: int | None = None, buffer=None) -> bool:
    """Causal scalar trend gate at a single date (for the sector / real-asset gates).

    With `window` given, forces a single fixed lookback and no margin (the separate
    sector-tilt gate). Otherwise honors the configured mode/lookbacks and the
    early-exit margin (`buffer` overrides the global default).
    """
    lbs = (window,) if window is not None else _lookbacks()
    buf = 0.0 if window is not None else _resolve_buf(buffer)
    hist = series.loc[:asof]
    votes = []
    for w in lbs:
        if hist.notna().sum() < w:
            votes.append(0.0)
            continue
        if config.MA_GATE_MODE == "ema" and window is None:
            ref = hist.ewm(span=w, adjust=False, min_periods=w).mean().iloc[-1]
        else:
            ref = hist.tail(w).mean()
        thresh = ref if buf <= 0.0 else ref * (1.0 + buf)
        votes.append(1.0 if hist.iloc[-1] > thresh else 0.0)
    return (sum(votes) / len(lbs)) >= 0.5
