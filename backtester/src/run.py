"""
run.py — Top-level entry point the user runs.

Orchestrates: load data -> run backtest -> compute metrics -> build HTML report.
Reads all tunables from config.py. This is the one command that produces a
results report.

From the project root, with the venv active:
    python -m src.run
"""

from __future__ import annotations

import sys

from strategies import config
from src import backtest, data_loader, metrics, report


def main() -> None:
    """Load data, run the backtest, compute metrics, build the report."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # safe on Windows cmd
    except Exception:
        pass

    version = config.ACTIVE_VERSION
    print(f"Adaptive All-Weather Core — running backtest ({version} version)\n")

    if not data_loader.DATA_PATH.exists() or not any(data_loader.DATA_PATH.glob("*.parquet")):
        print("No price data found in data/. Run this first:\n    python -m src.download_data")
        return

    result = backtest.run_backtest(version=version)
    table = metrics.compute_metrics(result["benchmark_navs"])
    report_path = report.build_report(result, table)

    navs = result["benchmark_navs"].iloc[-1]
    start = result["nav"].index.min().date()
    end = result["nav"].index.max().date()

    def pct(metric: str, col: str) -> str:
        return f"{table.loc[metric, col]:.1%}"

    # Plain-English summary, lead with the answer (CLAUDE.md).
    print(f"Backtest window: {start} to {end}, {len(result['weights'])} monthly rebalances.\n")
    print("Headline (the mandate is a smoother ride, not beating SPY):")
    print(f"  Max drawdown   : strategy {pct('Max drawdown','strategy')}"
          f"  vs SPY {pct('Max drawdown','SPY')}")
    print(f"  Worst 12 months: strategy {pct('Worst rolling 12m','strategy')}"
          f"  vs SPY {pct('Worst rolling 12m','SPY')}")
    print(f"  CAGR           : strategy {pct('CAGR','strategy')}"
          f"  vs SPY {pct('CAGR','SPY')}  (lagging in a bull is expected)")
    print(f"  Sortino        : strategy {table.loc['Sortino','strategy']:.2f}"
          f"  vs SPY {table.loc['Sortino','SPY']:.2f}")
    print(f"  Down capture   : {table.loc['Down capture vs SPY','strategy']:.0%} of SPY's downside")

    # Walk-forward (SPEC §16): show out-of-sample behavior on data after the
    # period a user might have tuned the rules on.
    if config.WALK_FORWARD_ENABLED:
        train, test = metrics.split_walk_forward(
            result["benchmark_navs"], config.WALK_FORWARD_TRAIN_END
        )
        print(f"\nWalk-forward (train <= {config.WALK_FORWARD_TRAIN_END}, test after):")
        for label, tbl in (("In-sample ", train), ("Out-sample", test)):
            print(f"  {label}: CAGR {tbl.loc['CAGR','strategy']:.1%}, "
                  f"max DD {tbl.loc['Max drawdown','strategy']:.1%}, "
                  f"Sortino {tbl.loc['Sortino','strategy']:.2f}")

    print(f"\nReport written to: {report_path}")
    print("Open that file in a browser (it is standalone).")


if __name__ == "__main__":
    main()
