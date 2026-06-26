"""
backtest.py — The month-by-month simulation loop. SPEC.md §3 (timing), §13 (frictions).

The single most important rule (SPEC §16): NO look-ahead. A signal for month-end T
may use only data on/before T; the resulting trades EXECUTE on the first trading
day of the next month (T+1). This module enforces that by computing every engine
signal causally up to T and applying the target weights starting at T+1.

Flow per month:
  * month-end T  -> confirmed regime (hysteresis), equity target (band x vol trim),
    equity sleeve, duration caps, defensive ranking, real-asset slot -> target
    weights (portfolio.build_target_weights).
  * first trading day after T -> rebalance to those weights, charging a per-trade
    cost on the traded turnover (config.PER_TRADE_COST_BPS). Weights then drift with
    returns until the next rebalance.

Returns a result dict with the strategy NAV + daily returns, the monthly target
weights, regimes, sleeves, turnover, reason codes, and benchmark NAVs (SPY, a
daily-rebalanced 60/40, and T-bills) over the same window — everything report.py
and metrics.py need.
"""

from __future__ import annotations

import pandas as pd

from strategies import config
from strategies.base import MarketState
from strategies.all_weather import AdaptiveAllWeather

from src import data_loader


def _next_trading_day(index: pd.DatetimeIndex, after) -> pd.Timestamp | None:
    """First trading day strictly after `after` (the T+1 execution date)."""
    later = index[index > after]
    return later[0] if len(later) else None


def run_backtest(
    prices: pd.DataFrame | None = None,
    yield_10y: pd.Series | None = None,
    hyg: pd.Series | None = None,
    vix: pd.Series | None = None,
    hy_oas: pd.Series | None = None,
    version: str = config.ACTIVE_VERSION,
    start: str = config.BACKTEST_START,
    end: str | None = config.BACKTEST_END,
    execution_lag_days: int = config.EXECUTION_LAG_DAYS,
    taxable_mode: bool = config.TAXABLE_MODE,
    turnover_band: float = config.TURNOVER_BAND,
    rebalance_frequency: str = config.REBALANCE_FREQUENCY,
    adaptive_fast_regimes=None,
    use_real_assets: bool = True,
) -> dict:
    """
    Run the month-by-month simulation and return the result time series.

    execution_lag_days: 1 (default, SPEC §3) trades on the first trading day after
    the month-end signal. 0 means same-day execution and is for the no-look-ahead
    test only (it lets a signal capture its own signal-day return) — never use it
    for real results.
    """
    # --- Load data if not supplied (full history incl. pre-start warm-up) ---
    if prices is None:
        prices = data_loader.load_prices()
        # Add the 60/40 benchmark bond ticker (benchmark-only, not traded by any
        # engine) so the realistic 60/40 (e.g. SPY/AGG) can be computed.
        bond_t = config.BENCHMARK_6040[1]
        if bond_t not in prices.columns:
            try:
                prices = prices.join(data_loader.load_prices([bond_t]))
            except (KeyError, FileNotFoundError):
                pass
    # Credit proxy pair = config.CREDIT_PROXY (HYG / IEF; denominator configurable).
    # The numerator (HYG) is benchmark-only; the denominator may live in the universe
    # (IEF) or be benchmark-only. Load whichever isn't already passed in.
    if hyg is None:
        try:
            hyg = data_loader.load_prices([config.CREDIT_PROXY[0]])[config.CREDIT_PROXY[0]]
        except (KeyError, FileNotFoundError):
            hyg = None
    denom_t = config.CREDIT_PROXY[1]
    if denom_t in prices.columns:           # present in the (possibly synthetic) panel, e.g. MC
        credit_denom = prices[denom_t]
    else:
        try:                                # single-history: load the real series from disk
            credit_denom = data_loader.load_prices([denom_t])[denom_t]
        except (KeyError, FileNotFoundError):
            credit_denom = prices["IEF"] if "IEF" in prices.columns else None
    if yield_10y is None:
        yield_10y, _ = data_loader.load_treasury_10y()
    # Real macro upgrades when downloaded; else engines fall back to labeled proxies.
    vix_src = credit_src = None
    if vix is None:
        vix, vix_src = data_loader.load_vix()
    if hy_oas is None:
        hy_oas, credit_src = data_loader.load_hy_oas()

    returns = prices.pct_change().fillna(0.0)

    # --- The strategy 'brain' (shared with the paperbot). warmup() precomputes every
    # engine signal causally, picks the rebalance dates, and builds the re-entry
    # ladder; the loop below just asks it for the target weights at each date. The
    # paperbot runs this exact same AdaptiveAllWeather code. ---
    macro = {"hyg": hyg, "credit_denom": credit_denom,
             "yield_10y": yield_10y, "vix": vix, "hy_oas": hy_oas}
    strategy = AdaptiveAllWeather(
        version=version, rebalance_frequency=rebalance_frequency,
        adaptive_fast_regimes=adaptive_fast_regimes, use_real_assets=use_real_assets,
    )
    strategy.warmup(prices, macro, start, end)
    signal_dates = strategy.signal_dates

    # --- Build target weights at each rebalance date, to execute the next day ---
    targets: dict[pd.Timestamp, pd.Series] = {}
    monthly_rows: list[dict] = []
    prev_weights: pd.Series | None = None

    for t in signal_dates:
        exec_date = t if execution_lag_days == 0 else _next_trading_day(prices.index, t)
        if exec_date is None:
            break  # no next-period execution available; stop

        state = MarketState(prices=prices, macro=macro, as_of=t, positions=prev_weights)
        decision = strategy.on_data(state)
        weights = decision.weights
        targets[exec_date] = weights
        prev_weights = weights
        ex = decision.extras
        monthly_rows.append(
            {
                "signal_date": t, "exec_date": exec_date, "regime": ex["regime"],
                "score": ex["score"], "equity_target": ex["equity_target"],
                "ladder_stage": ex["ladder_stage"],
                # decision.sleeves supplies numeric equity/defense/real_asset
                # fractions; keep the held hedge's TICKER under a distinct key so
                # it does not overwrite the real_asset sleeve fraction.
                **decision.sleeves, "real_asset_ticker": decision.real_asset,
                "reasons": "; ".join(decision.reasons),
            }
        )

    if not targets:
        raise RuntimeError("no rebalances generated — check the date window vs data")

    # --- Daily simulation from the first execution date onward ---
    first_exec = min(targets)
    sim_index = prices.index[prices.index >= first_exec]
    cost_per_unit = config.PER_TRADE_COST_BPS / 10_000.0

    nav, daily_ret, turnover_log = 1.0, [], []
    nav_series, ret_series = [], []
    current = pd.Series(dtype=float)

    for d in sim_index:
        # Rebalance at the start of an execution day, charging turnover cost.
        if d in targets:
            new_w = targets[d]
            all_keys = current.index.union(new_w.index)
            drift = current.reindex(all_keys, fill_value=0.0)
            tgt = new_w.reindex(all_keys, fill_value=0.0)
            if taxable_mode:
                # No-trade band: keep the drifted weight where the move is small,
                # then renormalize (SPEC §11 step 6 — suppress small taxable trades).
                small = (tgt - drift).abs() < turnover_band
                tgt = tgt.where(~small, drift)
                total = tgt.sum()
                if total > 0:
                    tgt = tgt / total
            one_way_turnover = (tgt - drift).abs().sum() / 2.0
            nav *= (1.0 - one_way_turnover * cost_per_unit)
            turnover_log.append({"date": d, "turnover": one_way_turnover})
            current = tgt[tgt > 1e-9].copy()

        # Accrue the day's portfolio return on the held weights, then let drift.
        day_r = float((current * returns.loc[d, current.index]).sum()) if len(current) else 0.0
        nav *= (1.0 + day_r)
        nav_series.append(nav)
        ret_series.append(day_r)
        if len(current):
            grown = current * (1.0 + returns.loc[d, current.index])
            current = grown / grown.sum()

    nav_s = pd.Series(nav_series, index=sim_index, name="strategy")
    ret_s = pd.Series(ret_series, index=sim_index, name="strategy")

    # --- Benchmarks over the same window (SPEC §14) ---
    benchmarks = _benchmark_navs(returns, sim_index)
    benchmarks["strategy"] = nav_s

    weights_df = pd.DataFrame(
        {ex: w for ex, w in targets.items()}
    ).T.sort_index().fillna(0.0)

    return {
        "nav": nav_s,
        "returns": ret_s,
        "benchmark_navs": benchmarks,
        "weights": weights_df,
        "monthly": pd.DataFrame(monthly_rows).set_index("signal_date"),
        "turnover": pd.DataFrame(turnover_log).set_index("date")["turnover"],
        "version": version,
        "yield_is_real": getattr(yield_10y, "name", None) == "us_treasury_10y",
        "vix_is_real": vix is not None,
        "credit_is_real": hy_oas is not None,
    }


def _benchmark_navs(returns: pd.DataFrame, sim_index: pd.DatetimeIndex) -> pd.DataFrame:
    """SPY, daily-rebalanced 60/40 (SPY/IEF), and T-bill NAVs over the window."""
    cols = {}
    if "SPY" in returns:
        cols["SPY"] = (1.0 + returns.loc[sim_index, "SPY"]).cumprod()
    eq_t, bond_t = config.BENCHMARK_6040          # e.g. ("SPY", "AGG")
    if {eq_t, bond_t} <= set(returns.columns):
        w_eq, w_bond = config.BENCHMARK_6040_WEIGHTS
        blend = w_eq * returns.loc[sim_index, eq_t] + w_bond * returns.loc[sim_index, bond_t]
        cols["60/40"] = (1.0 + blend).cumprod()
    tbill = config.BENCHMARK_TBILL
    if tbill in returns:
        cols["T-bills"] = (1.0 + returns.loc[sim_index, tbill]).cumprod()
    return pd.DataFrame(cols)
