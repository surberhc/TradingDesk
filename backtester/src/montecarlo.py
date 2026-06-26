"""
montecarlo.py — Block-bootstrap Monte Carlo for the strategy. SPEC.md §16.

The question: was our single historical result lucky, and how bad can the ride get?
Naive return-shuffling is wrong here — it destroys the trends and correlations the
strategy trades on. Instead we use a BLOCK bootstrap:

  * Resample contiguous BLOCKS of trading days (default ~63 = a quarter), so
    short-horizon momentum/mean-reversion survives.
  * Apply the SAME block sequence to EVERY series (all ETFs + the macro yield/VIX),
    so cross-asset correlations (stocks vs bonds, etc.) are preserved.
  * Reconstruct synthetic price/level paths, then re-run the FULL strategy on each.

We collect the distribution of CAGR / max drawdown / Sortino / Calmar across many
synthetic pasts, and compare the strategy to SPY on the SAME synthetic paths.

Scope note: the bootstrap scrambles the timeline, which would break inception-aware
logic, so it runs on the FULL-UNIVERSE window (from 2018-07, when the youngest kept
ETFs already trade) and drops SGOV (2020 inception; BIL covers T-bills). Synthetic
paths are ~8 years; the backtest within each starts after a ~1-year warm-up.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies import config
from src import backtest, data_loader, metrics

MC_WINDOW_START = "2018-07-01"      # full kept-universe present from here
MC_BACKTEST_START = "2019-07-01"    # ~1y warm-up inside each synthetic path
DROP_TICKERS = ["SGOV"]             # 2020 inception; redundant with BIL
_REPORT_METRICS = ["CAGR", "Max drawdown", "Worst rolling 12m", "Sortino", "Calmar"]


def _load_panel():
    """Full-universe daily panel over the MC window: prices(+HYG), yield, VIX."""
    prices = data_loader.load_prices()
    keep = [c for c in prices.columns if c not in DROP_TICKERS]
    prices = prices[keep].loc[MC_WINDOW_START:].dropna(axis=1, how="any")
    hyg = data_loader.load_prices(["HYG"])["HYG"].reindex(prices.index).ffill()
    prices = prices.assign(HYG=hyg)  # carry HYG through the same reorder, split later
    # Benchmark-/proxy-only tickers, bootstrapped with everything else: the 60/40
    # bond leg (AGG) and the credit-proxy denominator (config.CREDIT_PROXY[1]). When
    # the denominator is already in the universe panel (e.g. IEF) this is a no-op;
    # the loop only matters if it's an outside ticker (e.g. LQD).
    for extra in (config.BENCHMARK_6040[1], config.CREDIT_PROXY[1]):
        if extra not in prices.columns:
            try:
                prices = prices.assign(**{extra: data_loader.load_prices([extra])[extra]
                                          .reindex(prices.index).ffill()})
            except (KeyError, FileNotFoundError):
                pass
    yld, _ = data_loader.load_treasury_10y()
    vix, _ = data_loader.load_vix()
    yld = yld.reindex(prices.index).ffill().bfill() if yld is not None else None
    vix = vix.reindex(prices.index).ffill().bfill() if vix is not None else None
    return prices, yld, vix


def _block_positions(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """A length-n sequence of positional indices built from random day-blocks."""
    pos: list[int] = []
    while len(pos) < n:
        start = int(rng.integers(0, n - block))
        pos.extend(range(start, start + block))
    return np.array(pos[:n])


def _synthesize(prices: pd.DataFrame, yld, vix, pos: np.ndarray):
    """Reorder returns (prices/HYG) and level-diffs (yield/VIX) by `pos`; rebuild."""
    cal = prices.index
    rets = prices.pct_change().fillna(0.0).to_numpy()[pos]
    synth_px = prices.iloc[0].to_numpy() * np.cumprod(1.0 + rets, axis=0)
    px = pd.DataFrame(synth_px, index=cal, columns=prices.columns)

    def level(series, floor=None):
        if series is None:
            return None
        diffs = series.diff().fillna(0.0).to_numpy()[pos]
        out = float(series.iloc[0]) + np.cumsum(diffs)
        if floor is not None:
            out = np.maximum(out, floor)
        return pd.Series(out, index=cal, name=series.name)

    return px, level(yld), level(vix, floor=1.0)  # VIX floored >0 to stay realistic


def run_mc(
    n_paths: int = 150,
    block: int = 63,
    version: str = "Balanced",
    seed: int = 0,
    use_real_assets: bool = True,
) -> pd.DataFrame:
    """
    Run the strategy on `n_paths` block-bootstrapped histories. Returns one row per
    path with the strategy's metrics plus SPY's metrics on the SAME synthetic path.
    """
    prices, yld, vix = _load_panel()
    rng = np.random.default_rng(seed)
    n = len(prices)
    records = []
    for _ in range(n_paths):
        pos = _block_positions(n, block, rng)
        spx, syld, svix = _synthesize(prices, yld, vix, pos)
        shyg = spx["HYG"]
        sprices = spx.drop(columns="HYG")
        r = backtest.run_backtest(
            sprices, syld, shyg, svix, None,
            version=version, start=MC_BACKTEST_START, use_real_assets=use_real_assets,
        )
        t = metrics.compute_metrics(r["benchmark_navs"])
        rec = {m: t.loc[m, "strategy"] for m in _REPORT_METRICS}
        rec["SPY_CAGR"] = t.loc["CAGR", "SPY"]
        rec["SPY_MaxDD"] = t.loc["Max drawdown", "SPY"]
        if "60/40" in t.columns:
            rec["6040_CAGR"] = t.loc["CAGR", "60/40"]
            rec["6040_MaxDD"] = t.loc["Max drawdown", "60/40"]
        records.append(rec)
    return pd.DataFrame(records)


def summarize(df: pd.DataFrame) -> None:
    """Print percentile bands for the strategy and the SPY-vs-strategy comparison."""
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    pct = [5, 25, 50, 75, 95]
    print(f"Monte Carlo: {len(df)} block-bootstrapped paths\n")
    print(f"{'metric':12s}" + "".join(f"p{p:<6}" for p in pct))
    for m in _REPORT_METRICS:
        vals = df[m].quantile([p / 100 for p in pct]).values
        fmt = (lambda v: f"{v:>6.1%}") if m != "Sortino" and m != "Calmar" else (lambda v: f"{v:>6.2f}")
        print(f"{m:12s}" + "".join(fmt(v) for v in vals))
    print()
    def vs(label, dd_col, cagr_col):
        shallower = (df["Max drawdown"] > df[dd_col]).mean()  # less negative = shallower
        print(f"  vs {label}: strategy drawdown SHALLOWER on {shallower:.0%} of paths; "
              f"median maxDD {df['Max drawdown'].median():.1%} vs {df[dd_col].median():.1%}; "
              f"median CAGR {df['CAGR'].median():.1%} vs {df[cagr_col].median():.1%}.")

    print("Head-to-head across paths:")
    vs("SPY", "SPY_MaxDD", "SPY_CAGR")
    if "6040_MaxDD" in df.columns:
        vs("60/40", "6040_MaxDD", "6040_CAGR")
    print(f"Worst single-path strategy maxDD: {df['Max drawdown'].min():.1%} "
          f"(SPY {df['SPY_MaxDD'].min():.1%}"
          + (f", 60/40 {df['6040_MaxDD'].min():.1%}" if '6040_MaxDD' in df.columns else "") + ").")
