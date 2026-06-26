"""
defensive.py — Defensive Engine (PRIMARY). SPEC.md §7.

Ranks the defensive candidates (T-bills, short/floating/intermediate/long
Treasuries) on six weighted factors and returns a 0-100 composite score per asset.
T-bills are always eligible and are the fallback when nothing else earns its slot;
the portfolio layer (SPEC §11) does the actual slot-filling. We do NOT force
diversification into weak assets — a poor candidate simply ranks low.

Factors and weights (config.DEFENSIVE_SCORE_WEIGHTS, sums to 100):
    return_3m (25), return_6m (20), abs_trend (20), rel_vs_tbill (15),
    volatility_penalty (10), drawdown_penalty (10).

Scoring is CROSS-SECTIONAL: on each date every factor is turned into a
"goodness" value (higher = better, so the two penalties are negated) and ranked
into a [0,1] percentile across the candidates trading that day. The weighted sum
of percentiles is the 0-100 score. This keeps factors with different units
(returns vs volatility vs drawdown) comparable and is naturally inception-aware —
a candidate not yet trading is NaN and drops out of that day's ranking.

Correctness (SPEC §3): all factor windows are trailing, so a date-T score uses
only data on/before T. No look-ahead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies import config
from strategies.parts import _gates as gates

TRADING_DAYS_PER_MONTH = 21  # units conversion (see regime.py), not a tunable


def _factor_goodness(
    prices: pd.DataFrame, candidates: list[str], tbill: str
) -> dict[str, pd.DataFrame]:
    """
    Build each factor as a 'higher is better' frame (date x candidate).

    Penalties (volatility, drawdown) are negated so that, like the others, a
    higher value is always better and a single ascending percentile rank applies.
    """
    px = prices[candidates]
    m = TRADING_DAYS_PER_MONTH

    ret_3m = px.pct_change(3 * m)
    ret_6m = px.pct_change(6 * m)
    trend = gates.distance(px)  # distance above the long MA (mode-aware: sma/ensemble/ema)
    rel_vs_tbill = ret_3m.sub(prices[tbill].pct_change(3 * m), axis=0)

    daily_ret = px.pct_change()
    volatility = daily_ret.rolling(config.VOL_LOOKBACK_DAYS).std() * np.sqrt(252)
    drawdown = px / px.rolling(config.LONG_TSY_DRAWDOWN_LOOKBACK_DAYS).max() - 1.0

    return {
        "return_3m": ret_3m,
        "return_6m": ret_6m,
        "abs_trend": trend,
        "rel_vs_tbill": rel_vs_tbill,
        "volatility_penalty": -volatility,   # lower vol -> higher goodness
        "drawdown_penalty": drawdown,        # shallower (closer to 0) -> higher goodness
    }


def defensive_scores(
    prices: pd.DataFrame,
    candidates: list[str] | None = None,
    tbill: str = config.BENCHMARK_TBILL,
) -> pd.DataFrame:
    """
    Daily 0-100 composite defensive score per candidate (date x candidate).

    Candidates default to config.DEFENSIVE_ASSETS (those present in `prices`);
    `tbill` is always included so T-bills remain eligible (SPEC §7). Each date's
    score is the config-weighted sum of cross-sectional factor percentiles, so it
    lies in [0, 100]. Dates before a candidate's inception are NaN.
    """
    if candidates is None:
        candidates = [c for c in config.DEFENSIVE_ASSETS if c in prices.columns]
    if tbill in prices.columns and tbill not in candidates:
        candidates = candidates + [tbill]  # T-bills always eligible
    missing = [c for c in candidates if c not in prices.columns]
    if missing:
        raise KeyError(f"defensive candidates not in prices: {missing}")

    weights = config.DEFENSIVE_SCORE_WEIGHTS
    factors = _factor_goodness(prices, candidates, tbill)

    score = pd.DataFrame(0.0, index=prices.index, columns=candidates)
    any_valid = pd.DataFrame(False, index=prices.index, columns=candidates)
    for name, weight in weights.items():
        # Percentile across candidates trading that day; NaN candidates excluded.
        pct = factors[name].rank(axis=1, pct=True)
        score = score.add(pct.fillna(0.0) * weight, fill_value=0.0)
        any_valid |= pct.notna()

    # Keep NaN where a candidate had no defined factors that day (pre-inception).
    return score.where(any_valid)


def rank_defensives(
    prices: pd.DataFrame,
    asof,
    candidates: list[str] | None = None,
    tbill: str = config.BENCHMARK_TBILL,
) -> pd.Series:
    """
    Rank defensive candidates for a single signal date, best first.

    Returns a Series of composite scores indexed by ticker, sorted descending,
    with not-yet-trading candidates dropped. T-bills are always present (the
    fallback when nothing else earns its slot).
    """
    scores = defensive_scores(prices, candidates, tbill)
    asof = pd.Timestamp(asof)
    if asof not in scores.index:
        # Use the most recent score on/before the requested date (causal).
        scores = scores.loc[:asof]
        if scores.empty:
            raise KeyError(f"no defensive scores on/before {asof.date()}")
        row = scores.iloc[-1]
    else:
        row = scores.loc[asof]
    return row.dropna().sort_values(ascending=False)
