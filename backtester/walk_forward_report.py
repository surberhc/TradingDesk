"""
walk_forward_report.py — standalone demo runner for the multi-window walk-forward harness.

OPT-IN ONLY. This is its own script; it is NOT wired into `python -m src.run` and changes
no default output. It exists to demonstrate calling src/walk_forward.rolling_walk_forward()
against the real backtest and to print the stitched out-of-sample verdict.

Run from the backtester folder with the local venv:
    "C:/TradingDesk-Local/venv/Scripts/python.exe" walk_forward_report.py

The window count / split ratio / mode are DESIGN CHOICES (not frozen) — the CLI flags below
make that explicit. Defaults are the harness's proposed defaults, which still need Andrew's
blessing before being treated as a standing measurement.
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    p = argparse.ArgumentParser(description="Rolling walk-forward report (opt-in).")
    p.add_argument("--n-windows", type=int, default=5, help="number of sequential windows")
    p.add_argument("--is-frac", type=float, default=0.70, help="in-sample fraction per window")
    p.add_argument("--mode", choices=["rolling", "anchored"], default="rolling")
    args = p.parse_args()

    # Import here so merely importing this module (or running the test suite) never triggers
    # a backtest or touches data.
    from src import backtest, data_loader, walk_forward
    from strategies import config

    if not data_loader.DATA_PATH.exists() or not any(data_loader.DATA_PATH.glob("*.parquet")):
        print("No price data found in data/. Run:  python -m src.download_data")
        return

    print(f"Running backtest ({config.ACTIVE_VERSION}) to build the NAV series ...")
    result = backtest.run_backtest(version=config.ACTIVE_VERSION)
    navs = result["benchmark_navs"]  # DataFrame with 'strategy' + benchmarks

    # Passthrough run_fn: the NAV path is already computed, so each window's OOS is that
    # window's OOS slice. (A future caller could pass a run_fn that RE-RUNS the backtest
    # constrained to each window's data; the harness supports that unchanged.)
    wf_result = walk_forward.rolling_walk_forward(
        navs,
        run_fn=None,
        n_windows=args.n_windows,
        is_frac=args.is_frac,
        mode=args.mode,
    )

    print()
    print(walk_forward.format_report(wf_result))
    print()
    print(
        "NOTE: n_windows / is_frac / mode are DESIGN CHOICES pending Andrew's blessing. "
        "Slow strategies (e.g. S0) will trip the thin-sample guard — treat any [THIN] "
        "window as noise, not a finding."
    )


if __name__ == "__main__":
    main()
