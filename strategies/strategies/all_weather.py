"""
all_weather.py — Adaptive All-Weather Core (S0) as a Strategy.

The SHARED brain for S0: the exact decision logic the backtester validated, wrapped
behind StrategyBase so the paperbot runs the identical code. It owns NO data access
and NO simulation — it turns (prices + macro, an as-of date) into target weights via
the engine signals (parts/) and the portfolio assembler.

Logic mirrors the former inline loop in the backtester (SPEC §3 timing, §9 re-entry
ladder, §11 order of operations):
  * warmup() precomputes every engine signal causally, determines the rebalance
    dates (fixed cadence, or regime-adaptive), and builds the re-entry ladder.
  * on_data(T) derives the equity target (regime band x volatility trim, capped by
    the ladder), picks the sleeves, and assembles final weights.

Causality: every signal is a trailing-window computation, so a value at T uses only
data on/before T. The runner must still feed MarketState frames that end at T.
"""
from __future__ import annotations

import pandas as pd

from strategies import config
from strategies.base import MarketState, StrategyBase, TargetWeights
from strategies.parts import (
    defensive,
    duration,
    portfolio,
    real_assets,
    reentry,
    regime,
    sector,
    volatility,
)

# Rebalance cadences -> approximate rebalance periods per month, to keep the ladder's
# month-denominated MAX-LAG knob calendar-consistent across frequencies (SPEC §9).
_PERIODS_PER_MONTH = {"monthly": 1, "biweekly": 2, "weekly": 4}


def universe() -> set[str]:
    """Every symbol S0 can EVER hold — the union across all sleeves and regimes.

    Self-describing, derived from the SAME config groups the sleeves draw from, so it can
    never silently disagree with what the portfolio assembler actually emits:
      * equity sleeve   -> EQUITY_CORE + SECTORS   (parts/sector.select_sectors)
      * defensive sleeve-> DEFENSIVE_ASSETS         (parts/defensive + portfolio._BUCKET_OF;
                           best-tbill fallback BENCHMARK_TBILL is in TBILLS ⊆ DEFENSIVE_ASSETS)
      * real-asset sleeve-> REAL_ASSETS             (parts/real_assets over REAL_ASSET_BASKET)
    That union is exactly config.ALL_TICKERS (EQUITY_CORE + SECTORS + DEFENSIVE_ASSETS +
    REAL_ASSETS), the same universe the backtester loads prices for and the assembler
    filters every emitted ticker against — so no sleeve can produce a symbol outside this
    set. Verified against parts/sector.py, parts/defensive.py, parts/real_assets.py, and
    parts/portfolio.build_target_weights (2026-07-20). READ-ONLY; decides nothing."""
    return set(config.ALL_TICKERS)


def _signal_dates(index: pd.DatetimeIndex, frequency: str, start: str, end: str | None) -> list:
    """Rebalance signal dates within [start, end] at the requested cadence.

    The signal is taken on the LAST trading day of each period (month, ISO week, or
    every-other ISO week); trades execute T+1. Frequency only controls how often we
    act on the (unchanged, time-based) engine lookbacks.
    """
    s = index.to_series()
    if frequency == "monthly":
        picks = s.groupby([index.year, index.month]).last()
    elif frequency in ("weekly", "biweekly"):
        iso = index.isocalendar()
        picks = s.groupby([iso["year"], iso["week"]]).last().sort_values()
        if frequency == "biweekly":
            picks = picks.iloc[::2]  # every other week
    else:
        raise ValueError(f"unknown rebalance frequency: {frequency!r}")
    floor = pd.Timestamp(start)
    ceil = pd.Timestamp(end) if end else index.max()
    return [d for d in picks if floor <= d <= ceil]


def _adaptive_signal_dates(
    index: pd.DatetimeIndex,
    confirmed_regime: pd.Series,
    fast_regimes: set,
    start: str,
    end: str | None,
) -> list:
    """Regime-adaptive cadence: monthly normally, but weekly while de-risked.

    Every month-end (cheap base cadence) PLUS every week-end whose confirmed regime is
    in `fast_regimes`. Low turnover in healthy markets, weekly checks during de-risked
    stretches so the book re-enters quickly on a bounce. Causal: the regime is computed
    daily from trailing data, so which weeks qualify uses only past information.
    """
    monthly = set(_signal_dates(index, "monthly", start, end))
    weekly = _signal_dates(index, "weekly", start, end)
    reg = confirmed_regime.reindex(index).ffill()
    fast_weekly = [d for d in weekly if reg.get(d, None) in fast_regimes]
    return sorted(monthly.union(fast_weekly))


def _scaled_band(regime_name: str, version: str) -> tuple[float, float]:
    """Regime equity band scaled by the client version's equity allowance (§10)."""
    lo, hi = regime.equity_band(regime_name)
    allow = config.CLIENT_VERSIONS.get(version, {}).get("equity_allowance", 1.0)
    return lo * allow, hi * allow


def _reentry_conditions(
    prices: pd.DataFrame,
    score_df: pd.DataFrame,
    confirmed_regime: pd.Series,
    realized: pd.Series,
    signal_dates: list,
    version: str,
) -> pd.DataFrame:
    """Build the per-rebalance stage-gate conditions for the re-entry ladder (SPEC §9).

    All inputs are causal (trailing-window engine outputs), sampled at the signal
    dates, so the conditions — and the stages derived from them — use only data
    on/before each date.
    """
    idx = pd.DatetimeIndex(signal_dates)
    bool_col = lambda name: score_df.get(name, pd.Series(0.0, index=score_df.index)).reindex(idx) > 0.5  # noqa: E731

    score = score_df["score"].reindex(idx)
    above_200d = bool_col("trend_above_200d")
    above_10m = bool_col("trend_above_10m")
    breadth = score_df["breadth_pct"].reindex(idx)
    vol_calm = bool_col("stress_vol_calm")
    credit_calm = (
        bool_col("stress_credit_calm")
        if "stress_credit_calm" in score_df.columns
        else pd.Series(True, index=idx)
    )
    rvol = realized.reindex(idx)

    ma50 = prices["SPY"].rolling(50).mean()
    spy_above_50d = (prices["SPY"] > ma50).reindex(idx)

    sector_cols = [s for s in config.SECTORS if s in prices.columns]
    sectors = prices[sector_cols]
    sec_above = (sectors > sectors.rolling(config.MA_LONG_DAYS).mean()).sum(axis=1)
    sec_count = sec_above.reindex(idx)

    breadth_prev, rvol_prev = breadth.shift(1), rvol.shift(1)
    vol_not_rising = (rvol <= rvol_prev).fillna(False)
    breadth_improving = (breadth > breadth_prev).fillna(False)
    breadth_material = (breadth > breadth_prev + config.REENTRY_BREADTH_IMPROVE).fillna(False)

    stage4_score = score > config.REENTRY_STAGE4_SCORE.get(version, 65)
    trend_ok = above_200d | above_10m

    conditions = pd.DataFrame(index=idx)
    conditions["stage1"] = spy_above_50d & breadth_improving & vol_not_rising
    conditions["stage2"] = trend_ok | (score > 40)
    conditions["stage3"] = (sec_count >= config.REENTRY_STAGE3_SECTOR_COUNT) | breadth_material
    conditions["stage4"] = stage4_score & credit_calm & vol_calm
    conditions["defensive"] = confirmed_regime.reindex(idx).isin(
        ["Defensive", "CapitalPreservation"]
    )
    conditions["deteriorating"] = (~credit_calm) | (~vol_not_rising)
    conditions["sharp_recovery"] = stage4_score & trend_ok
    return conditions.fillna(False)


class AdaptiveAllWeather(StrategyBase):
    """S0 — rules-based, multi-engine tactical asset allocation."""

    name = "adaptive_all_weather"

    def universe(self) -> set[str]:
        """S0's tradeable universe (union across sleeves/regimes). Delegates to the
        module-level `universe()` so the instance and module accessors can never drift."""
        return universe()

    def __init__(
        self,
        version: str = config.ACTIVE_VERSION,
        rebalance_frequency: str = config.REBALANCE_FREQUENCY,
        adaptive_fast_regimes=None,
        use_real_assets: bool = True,
    ):
        self.version = version
        self.rebalance_frequency = rebalance_frequency
        self.adaptive_fast_regimes = adaptive_fast_regimes
        self.use_real_assets = use_real_assets
        self.params = {
            "version": version,
            "rebalance_frequency": rebalance_frequency,
            "adaptive_fast_regimes": adaptive_fast_regimes,
            "use_real_assets": use_real_assets,
        }
        self.signal_dates: list = []
        self._signals: dict | None = None

    def warmup(self, prices: pd.DataFrame, macro: dict, start: str, end: str | None) -> None:
        """Precompute every engine signal ONCE (causally), pick the rebalance dates,
        and build the re-entry ladder. Mirrors the former precompute block in
        backtest.run_backtest exactly — same functions, inputs, and order."""
        hyg = macro.get("hyg")
        credit_denom = macro.get("credit_denom")
        yield_10y = macro.get("yield_10y")
        vix = macro.get("vix")
        hy_oas = macro.get("hy_oas")

        score_df = regime.market_health_score(
            prices, hyg=hyg, credit_denom=credit_denom, vix=vix, hy_oas=hy_oas)
        confirmed_regime = regime.apply_hysteresis(score_df["score"])
        dur_signals = duration.duration_signals(
            prices, yield_10y=yield_10y, hyg=hyg, hy_oas=hy_oas, credit_denom=credit_denom)
        def_scores = defensive.defensive_scores(prices)
        realized = volatility.realized_vol(prices["SPY"])

        if self.adaptive_fast_regimes:
            signal_dates = _adaptive_signal_dates(
                prices.index, confirmed_regime, set(self.adaptive_fast_regimes), start, end)
        else:
            signal_dates = _signal_dates(prices.index, self.rebalance_frequency, start, end)

        conditions = _reentry_conditions(
            prices, score_df, confirmed_regime, realized, signal_dates, self.version)
        per_month = _PERIODS_PER_MONTH.get(self.rebalance_frequency, 1)
        ladder_stage = reentry.compute_ladder_stages(
            conditions, max_lag_months=config.REENTRY_MAX_LAG_MONTHS * per_month)
        ladder_cap = ladder_stage.map(reentry.ladder_equity_cap)

        self.signal_dates = signal_dates
        self._signals = {
            "prices": prices,
            "score_df": score_df,
            "confirmed_regime": confirmed_regime,
            "dur_signals": dur_signals,
            "def_scores": def_scores,
            "realized": realized,
            "ladder_stage": ladder_stage,
            "ladder_cap": ladder_cap,
        }

    def on_data(self, state: MarketState) -> TargetWeights:
        """Assemble the target weights to hold as of state.as_of (one rebalance)."""
        if self._signals is None:
            raise RuntimeError("call warmup() before on_data()")
        s = self._signals
        t = state.as_of
        prev_weights = state.positions
        version = self.version

        reg = s["confirmed_regime"].loc[t]
        band = _scaled_band(reg, version)
        # Equity target = volatility trim inside the band, then capped by the re-entry
        # ladder (a staged rebuild can only lower it, never raise it).
        eq_target = min(
            volatility.equity_target(band, s["realized"].loc[t], version),
            float(s["ladder_cap"].loc[t]),
        )

        sleeve = sector.select_sectors(s["prices"], t, config.SECTOR_TILT_PCT)
        ddec = duration.duration_decision(s["dur_signals"].loc[t], reg)
        def_rank = s["def_scores"].loc[t].dropna().sort_values(ascending=False)
        real = real_assets.select_real_basket(s["prices"], t) if self.use_real_assets else None

        built = portfolio.build_target_weights(
            regime=reg,
            equity_target=eq_target,
            equity_sleeve=sleeve,
            duration_decision=ddec,
            defensive_ranking=def_rank,
            real_basket=real,
            version=version,
            prev_weights=prev_weights,
        )
        return TargetWeights(
            weights=built["weights"],
            as_of=t,
            sleeves=built["sleeves"],
            real_asset=built["real_asset"],
            reasons=built["reasons"],
            extras={
                "regime": reg,
                "equity_target": eq_target,
                "score": float(s["score_df"].loc[t, "score"]),
                "ladder_stage": int(s["ladder_stage"].loc[t]),
            },
        )
