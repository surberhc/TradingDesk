"""
spx_vol_control.py — S4: SPX Volatility-Control Fund (standalone, single-asset).

A faithful in-house replica of the S&P 500 Daily Risk Control / FIA-RILA vol-control
engine. ONE risk asset (the S&P 500 via SPY adjusted total-return prices) plus a
cash / T-bill leg. NOT diversified — no bonds, no real assets, NO regime engine.
Volatility targeting IS the entire mechanism.

Core formula, rebalanced DAILY:

    exposure_t = min( leverage_cap , target_vol / realized_vol_t )

  * The residual (1 - exposure_t) sits in cash earning the risk-free rate. If
    exposure_t > 1.0 the excess is BORROWED at the risk-free (financing) rate, which
    we represent as a NEGATIVE cash weight (weights still sum to 1.0).
  * realized_vol_t is the asymmetric "max-of-two-horizons" estimator:
        max( fast, slow )
    where fast ~ 20-trading-day and slow ~ 60-trading-day annualized realized vol of
    SPX daily returns (annualized with sqrt(252)). This is the FIA "headline steal":
    de-risk FAST (the short window spikes first in a selloff) and re-risk SLOW (the
    long window is sticky, so exposure rebuilds only gradually). It is the required
    default. An EWMA variant (lambda 0.94 / 0.97 style) is also selectable.

Causality (the single most important correctness rule): the exposure that is HELD
from day T's close — and which therefore earns day T+1's return — must be computed
from realized vol using only returns up to and including day T. We enforce this by
computing realized_vol with trailing rolling windows (mirroring volatility.py /
all_weather.py), so the value at index T uses returns on/before T. An optional
observation lag (default 0) can shift the signal further back.

This module is PURE decision logic: it owns NO data access and NO simulation. The
standalone runner (backtester/s4_vol_control.py) or the paperbot drives it. on_data(T)
returns a TargetWeights of {SPY: exposure_t, <cash_ticker>: 1 - exposure_t}.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.base import MarketState, StrategyBase, TargetWeights

# Annualization factor for daily realized vol (trading days per year).
TRADING_DAYS_PER_YEAR = 252


def realized_vol_simple(
    returns: pd.Series, fast: int = 20, slow: int = 60
) -> pd.Series:
    """Asymmetric max-of-two-horizons annualized realized vol (the S4 default).

    fast/slow are trailing simple standard deviations of daily returns, each
    annualized by sqrt(252). The estimator is max(fast, slow): the fast window
    spikes first when vol rises (de-risk fast), and the slow window stays elevated
    longer when vol falls (re-risk slow). All windows are trailing, so the value at
    date T uses only returns on/before T — no look-ahead.
    """
    ann = np.sqrt(TRADING_DAYS_PER_YEAR)
    fast_vol = returns.rolling(fast).std(ddof=0) * ann
    slow_vol = returns.rolling(slow).std(ddof=0) * ann
    # max() propagates NaN until BOTH windows are warm, which is what we want
    # (no exposure decision until the slow window has filled).
    return pd.concat([fast_vol, slow_vol], axis=1).max(axis=1)


def realized_vol_ewma(
    returns: pd.Series, lam_fast: float = 0.94, lam_slow: float = 0.97
) -> pd.Series:
    """Optional S&P-style EWMA variant: max of two exponentially-weighted vols.

    lambda 0.94 (fast) and 0.97 (slow) are the RiskMetrics-flavored decays the S&P
    Daily Risk Control methodology uses. Each is an EWMA of squared daily returns,
    sqrt-ed and annualized; we take the max for the same de-risk-fast/re-risk-slow
    asymmetry as the simple estimator. Causal: EWMA at T uses only returns on/before T.
    """
    ann = np.sqrt(TRADING_DAYS_PER_YEAR)
    sq = returns**2
    # alpha = 1 - lambda; larger lambda -> longer memory (slower).
    fast_var = sq.ewm(alpha=1 - lam_fast, adjust=False).mean()
    slow_var = sq.ewm(alpha=1 - lam_slow, adjust=False).mean()
    fast_vol = np.sqrt(fast_var) * ann
    slow_vol = np.sqrt(slow_var) * ann
    return pd.concat([fast_vol, slow_vol], axis=1).max(axis=1)


def exposure_from_vol(
    realized: pd.Series, target_vol: float, leverage_cap: float
) -> pd.Series:
    """The universal vol-control exposure: min(cap, target_vol / realized_vol).

    Returns a per-date exposure series (the equity weight). Where realized vol is NaN
    (warm-up) the exposure is NaN; the runner holds 0 exposure (all cash) until the
    estimator is warm. Exposure is non-negative and capped at leverage_cap.
    """
    raw = target_vol / realized
    capped = raw.clip(upper=leverage_cap)
    return capped


class SpxVolControl(StrategyBase):
    """S4 — single-asset SPX volatility-targeting fund.

    Holds SPX (SPY) at a constant TARGET_VOL by scaling daily exposure between a cash
    leg and a leverage cap. Both target_vol and leverage_cap are dials. The decision
    logic is the pure vol-control formula; TR/ER accounting lives in the runner.
    """

    name = "spx_vol_control"

    def __init__(
        self,
        target_vol: float = 0.10,
        leverage_cap: float = 1.50,
        fast_window: int = 20,
        slow_window: int = 60,
        estimator: str = "simple",   # "simple" (default) or "ewma"
        obs_lag: int = 0,            # extra observation lag in trading days (default 0)
        risk_ticker: str = "SPY",
        cash_ticker: str = "BIL",
        ewma_lambdas: tuple[float, float] = (0.94, 0.97),
    ):
        self.target_vol = target_vol
        self.leverage_cap = leverage_cap
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.estimator = estimator
        self.obs_lag = obs_lag
        self.risk_ticker = risk_ticker
        self.cash_ticker = cash_ticker
        self.ewma_lambdas = ewma_lambdas
        self.params = {
            "target_vol": target_vol,
            "leverage_cap": leverage_cap,
            "fast_window": fast_window,
            "slow_window": slow_window,
            "estimator": estimator,
            "obs_lag": obs_lag,
            "risk_ticker": risk_ticker,
            "cash_ticker": cash_ticker,
        }
        self.signal_dates: list = []
        self._realized: pd.Series | None = None
        self._exposure: pd.Series | None = None

    # ------------------------------------------------------------------ warmup
    def warmup(self, prices: pd.DataFrame, macro: dict, start: str, end: str | None) -> None:
        """Precompute the causal realized-vol series, the daily exposure series, and
        the DAILY signal dates (every trading day in the window — vol-control rebalances
        every day, not month-end). All trailing-window, so value at T uses only data
        on/before T; an optional obs_lag shifts the signal further back.
        """
        if self.risk_ticker not in prices.columns:
            raise KeyError(f"risk ticker {self.risk_ticker!r} not in price frame")
        spx = prices[self.risk_ticker].dropna()
        returns = spx.pct_change()

        if self.estimator == "ewma":
            lam_f, lam_s = self.ewma_lambdas
            realized = realized_vol_ewma(returns, lam_fast=lam_f, lam_slow=lam_s)
        elif self.estimator == "simple":
            realized = realized_vol_simple(returns, self.fast_window, self.slow_window)
        else:
            raise ValueError(f"unknown estimator {self.estimator!r}")

        # Optional observation lag: act on vol as-of (T - obs_lag). Default 0.
        if self.obs_lag > 0:
            realized = realized.shift(self.obs_lag)

        exposure = exposure_from_vol(realized, self.target_vol, self.leverage_cap)

        self._realized = realized
        self._exposure = exposure

        # DAILY cadence: every trading day in [start, end] (after the estimator is warm).
        floor = pd.Timestamp(start)
        ceil = pd.Timestamp(end) if end else spx.index.max()
        warm = exposure.dropna().index
        self.signal_dates = [d for d in warm if floor <= d <= ceil]

    # ----------------------------------------------------------------- on_data
    def on_data(self, state: MarketState) -> TargetWeights:
        """Target weights as of T: {SPY: exposure_t, cash: 1 - exposure_t}.

        exposure_t is the causal vol-control exposure computed in warmup (vol through
        T's close). A cash weight below 0 legitimately represents the borrow when
        exposure > 1.0. Weights always sum to 1.0.
        """
        if self._exposure is None:
            raise RuntimeError("call warmup() before on_data()")
        t = state.as_of
        exp = self._exposure.get(t, np.nan)
        if pd.isna(exp):
            exp = 0.0  # not warm yet -> sit in cash
        exp = float(exp)
        weights = pd.Series(
            {self.risk_ticker: exp, self.cash_ticker: 1.0 - exp}
        )
        return TargetWeights(
            weights=weights,
            as_of=t,
            sleeves={"equity": exp, "cash": 1.0 - exp},
            reasons=[f"vol_control exposure={exp:.3f}"],
            extras={
                "exposure": exp,
                "realized_vol": float(self._realized.get(t, np.nan)),
                "target_vol": self.target_vol,
                "leverage_cap": self.leverage_cap,
            },
        )
