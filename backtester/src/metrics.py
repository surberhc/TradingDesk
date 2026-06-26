"""
metrics.py — Performance and risk metrics. SPEC.md §14.

The PRIMARY yardstick is risk, not CAGR (SPEC §0): max drawdown, worst rolling
12-month, downside deviation, worst rolling 3-month/3-year, then Sharpe, Sortino,
Calmar, beta vs SPY, up/down capture, and the LONGEST stretch of underperformance
vs SPY (in months). compute_metrics() returns one tidy table comparing the
strategy against SPY, the 60/40 blend, and T-bills.

Inputs are NAV series (growth of $1). The risk-free rate for Sharpe/Sortino is the
T-bill NAV's own return when present, else zero.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def _returns(nav: pd.Series) -> pd.Series:
    return nav.pct_change().dropna()


def cagr(nav: pd.Series) -> float:
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    return (nav.iloc[-1] / nav.iloc[0]) ** (1.0 / years) - 1.0 if years > 0 else np.nan


def annual_vol(ret: pd.Series) -> float:
    return ret.std(ddof=0) * np.sqrt(TRADING_DAYS)


def max_drawdown(nav: pd.Series) -> float:
    return float((nav / nav.cummax() - 1.0).min())


def worst_rolling(nav: pd.Series, window: int) -> float:
    """Worst total return over any trailing `window`-day span."""
    roll = nav / nav.shift(window) - 1.0
    return float(roll.min()) if roll.notna().any() else np.nan


def downside_deviation(ret: pd.Series, rf_daily: pd.Series | float = 0.0) -> float:
    downside = (ret - rf_daily).clip(upper=0.0)
    return float(np.sqrt((downside ** 2).mean()) * np.sqrt(TRADING_DAYS))


def sharpe(ret: pd.Series, rf_daily: pd.Series | float = 0.0) -> float:
    vol = ret.std(ddof=0)
    if vol == 0:
        return np.nan
    return float((ret - rf_daily).mean() * TRADING_DAYS / (vol * np.sqrt(TRADING_DAYS)))


def sortino(ret: pd.Series, rf_daily: pd.Series | float = 0.0) -> float:
    dd = downside_deviation(ret, rf_daily)
    if dd == 0:
        return np.nan
    return float((ret - rf_daily).mean() * TRADING_DAYS / dd)


def calmar(nav: pd.Series) -> float:
    mdd = abs(max_drawdown(nav))
    return cagr(nav) / mdd if mdd > 0 else np.nan


def beta(ret: pd.Series, market_ret: pd.Series) -> float:
    aligned = pd.concat([ret, market_ret], axis=1).dropna()
    var = aligned.iloc[:, 1].var()  # ddof=1 to match Series.cov() below
    if var == 0:
        return np.nan
    return float(aligned.iloc[:, 0].cov(aligned.iloc[:, 1]) / var)


def capture_ratio(ret: pd.Series, market_ret: pd.Series, up: bool) -> float:
    """
    Up/down capture vs the market, the standard (Morningstar) way: on the MONTHLY
    return series, take the geometric-MEAN return of the strategy over the
    geometric-mean return of the market across the months the market was up
    (up=True) or down (up=False). Down capture < 1 means the strategy fell less
    than the market — the goal here.

    Monthly geometric *means* (not the raw compounded product over hundreds of
    days) keep the ratio stable and interpretable. Inputs are daily returns on a
    DatetimeIndex; they are resampled to month-end compounded returns internally.
    """
    sr = (1.0 + ret).resample("ME").prod() - 1.0
    mr = (1.0 + market_ret).resample("ME").prod() - 1.0
    aligned = pd.concat([sr, mr], axis=1).dropna()
    mask = aligned.iloc[:, 1] > 0 if up else aligned.iloc[:, 1] < 0
    s, m = aligned.iloc[:, 0][mask], aligned.iloc[:, 1][mask]
    if len(m) == 0:
        return np.nan
    geo_strat = (1.0 + s).prod() ** (1.0 / len(s)) - 1.0
    geo_mkt = (1.0 + m).prod() ** (1.0 / len(m)) - 1.0
    return float(geo_strat / geo_mkt) if geo_mkt != 0 else np.nan


def longest_underperformance_months(strategy_nav: pd.Series, spy_nav: pd.Series) -> int:
    """
    Longest stretch (in months) the strategy spent below a prior peak in its
    relative performance vs SPY — i.e. how long a relative-return high took to
    reclaim. SPEC §14 calls this out as a prominent behavior metric.
    """
    rel = (strategy_nav / spy_nav).resample("ME").last().dropna()
    underwater = rel < rel.cummax()
    longest = current = 0
    for flag in underwater:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return int(longest)


def compute_metrics(benchmark_navs: pd.DataFrame) -> pd.DataFrame:
    """
    Full metrics table (rows = metrics, columns = strategy/SPY/60-40/T-bills).

    benchmark_navs must contain a "strategy" column and any benchmarks present
    ("SPY", "60/40", "T-bills"). Risk-free = T-bill return if available.
    """
    navs = benchmark_navs.dropna(how="all")
    rf_daily = _returns(navs["T-bills"]) if "T-bills" in navs else 0.0
    spy_nav = navs["SPY"] if "SPY" in navs else None
    spy_ret = _returns(spy_nav) if spy_nav is not None else None

    rows: dict[str, dict[str, float]] = {}

    def put(label: str, fn):
        rows[label] = {col: fn(col) for col in navs.columns}

    put("CAGR", lambda c: cagr(navs[c]))
    put("Annual volatility", lambda c: annual_vol(_returns(navs[c])))
    put("Max drawdown", lambda c: max_drawdown(navs[c]))
    put("Worst rolling 3m", lambda c: worst_rolling(navs[c], 63))
    put("Worst rolling 12m", lambda c: worst_rolling(navs[c], TRADING_DAYS))
    put("Worst rolling 3y", lambda c: worst_rolling(navs[c], TRADING_DAYS * 3))
    put("Downside deviation", lambda c: downside_deviation(_returns(navs[c]), rf_daily))
    put("Sharpe", lambda c: sharpe(_returns(navs[c]), rf_daily))
    put("Sortino", lambda c: sortino(_returns(navs[c]), rf_daily))
    put("Calmar", lambda c: calmar(navs[c]))

    if spy_ret is not None:
        put("Beta vs SPY", lambda c: beta(_returns(navs[c]), spy_ret))
        put("Up capture vs SPY", lambda c: capture_ratio(_returns(navs[c]), spy_ret, up=True))
        put("Down capture vs SPY", lambda c: capture_ratio(_returns(navs[c]), spy_ret, up=False))
        put(
            "Longest underperf. vs SPY (months)",
            lambda c: longest_underperformance_months(navs[c], spy_nav),
        )

    table = pd.DataFrame(rows).T
    # Stable, readable column order.
    order = [c for c in ("strategy", "SPY", "60/40", "T-bills") if c in table.columns]
    return table[order]


def split_walk_forward(
    benchmark_navs: pd.DataFrame, train_end: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Walk-forward split (SPEC §16): metrics on the in-sample (<= train_end) and the
    out-of-sample (> train_end) periods, evaluated separately. The strategy's rules
    are fixed in config (no fitting), so this shows how the same rules held up on
    data after the period a user would have tuned them on.
    """
    cut = pd.Timestamp(train_end)
    train = compute_metrics(benchmark_navs.loc[:cut])
    test = compute_metrics(benchmark_navs.loc[cut:])
    return train, test
