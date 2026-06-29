"""
run_s4.py — thin runnable entry point for the S4 SPX Volatility-Control Fund product.

This file owns NO strategy logic. It:
  1. instantiates the shared-brain engine (strategies.spx_vol_control.SpxVolControl)
     with this product's pinned deploy defaults (products/.../config.py), and
  2. prints TODAY's target book — the {SPY: exposure, BIL: 1-exposure} weights the fund
     wants to hold right now, which a paper-execution layer would diff against the
     account and trade toward. (A negative BIL weight legitimately means "borrow".)

It can also hand a full historical backtest/sweep to the VALIDATED runner
(backtester/s4_vol_control.py) without re-deriving anything — `--backtest` simply shells
out to that script so paper == backtest by construction.

PAPER / research scope only. This script touches NO broker and places NO order; it only
loads read-only price data and computes target weights. Wiring it to the paperbot's
execution engine is a separate, deliberate step (see DEPLOY.md).

Run (offline; project venv):
  C:/TradingDesk-Local/venv/Scripts/python.exe products/S4_vol_control_fund/run_s4.py
  flags:
    --profile balanced|conservative   which pinned deploy cell (default balanced)
    --backtest                        delegate a full TR/ER sweep to the validated runner
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Make the shared `strategies` package importable whether or not it is pip-installed.
# The package lives at TradingDesk/strategies (the editable "shared brain"); this file
# is at TradingDesk/products/S4_vol_control_fund, so the repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_STRATEGIES_PKG = _REPO_ROOT / "strategies"
if str(_STRATEGIES_PKG) not in sys.path:
    sys.path.insert(0, str(_STRATEGIES_PKG))

import config  # this product's pinned deploy defaults  # noqa: E402


def _load_prices() -> pd.DataFrame:
    """Load the risk + cash adjusted-close (total-return) series the strategy needs.

    Read-only. Mirrors the backtester runner's loader so the warmup sees the same data.
    """
    frames = {}
    for ticker in (config.RISK_TICKER, config.CASH_TICKER):
        path = os.path.join(config.DATA_DIR, f"{ticker}.parquet")
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"missing data file: {path}\n"
                f"  -> the product needs {config.REQUIRED_DATA} in {config.DATA_DIR}"
            )
        df = pd.read_parquet(path)
        s = df.iloc[:, 0]
        s.index = pd.to_datetime(s.index).normalize()
        frames[ticker] = s.sort_index()
    return pd.concat(frames, axis=1)


def current_target(profile: str = "balanced") -> dict:
    """Compute the target book the S4 fund wants to hold on the latest data date.

    Returns {as_of, weights (SPY/BIL), exposure, realized_vol, params}. This is the
    deploy-shaped output: the portfolio a paper-execution layer would rebalance toward.
    """
    strat = config.build_strategy(profile)
    prices = _load_prices()
    # Drop rows where either leg is missing so warmup's pct_change is clean.
    prices = prices.dropna()
    strat.warmup(prices, macro={}, start=str(prices.index.min().date()), end=None)

    if not strat.signal_dates:
        raise RuntimeError("no warm signal dates — not enough history to decide")
    as_of = strat.signal_dates[-1]

    from strategies.base import MarketState
    state = MarketState(prices=prices.loc[:as_of], macro={}, as_of=as_of)
    tw = strat.on_data(state)
    return {
        "as_of": as_of,
        "weights": tw.weights,
        "exposure": tw.extras.get("exposure"),
        "realized_vol": tw.extras.get("realized_vol"),
        "params": strat.params,
        "price_date": prices.index[-1],
    }


def _print_target(t: dict, profile: str) -> None:
    p = t["params"]
    print("\n" + "=" * 64)
    print(f"  S4 SPX VOL-CONTROL FUND  --  profile: {profile}")
    print("=" * 64)
    print(f"  target_vol     {p['target_vol']:.0%}")
    print(f"  leverage_cap   {p['leverage_cap']:.2f}x")
    print(f"  estimator      {p['estimator']} max({p['fast_window']}d, {p['slow_window']}d)")
    print(f"  cash leg       {p['cash_ticker']}")
    print("-" * 64)
    print(f"  decision date  {t['as_of'].date()}  (data through {t['price_date'].date()})")
    rv = t["realized_vol"]
    print(f"  realized vol   {rv*100:.2f}%" if rv == rv else "  realized vol   —")
    print(f"  exposure       {t['exposure']:.3f}x")
    print("-" * 64)
    print("  TARGET BOOK (the portfolio to hold right now):")
    for ticker, w in t["weights"].items():
        note = "  (BORROW)" if (ticker == p["cash_ticker"] and w < 0) else ""
        print(f"    {ticker:<6} {w*100:>7.2f}%{note}")
    print("=" * 64)
    print("  PAPER / research only — no order placed. See DEPLOY.md to wire execution.")


def _delegate_backtest() -> int:
    """Hand a full validated TR/ER sweep to backtester/s4_vol_control.py (no re-derivation)."""
    runner = _REPO_ROOT / "backtester" / "s4_vol_control.py"
    cmd = [sys.executable, str(runner), "--sweep"]
    print(f"  delegating to validated runner: {runner}\n")
    return subprocess.call(cmd, cwd=str(_REPO_ROOT))


def main() -> None:
    ap = argparse.ArgumentParser(description="S4 SPX vol-control fund — target book / backtest")
    ap.add_argument("--profile", default="balanced",
                    choices=["balanced", "conservative"],
                    help="which pinned deploy cell (default: balanced = 10%/1.5x)")
    ap.add_argument("--backtest", action="store_true",
                    help="delegate a full TR/ER 2-D sweep to the validated runner")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    if args.backtest:
        sys.exit(_delegate_backtest())

    t = current_target(args.profile)
    _print_target(t, args.profile)


if __name__ == "__main__":
    main()
