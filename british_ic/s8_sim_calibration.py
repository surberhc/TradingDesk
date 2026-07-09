"""
Stage B, Task 2: run the mechanical simulator (s8_mechanical_simulator.py) with
its Task-1-corrected entry-time grids across all 11 templates x every real
trading day 2025-07-09 to 2026-07-07 (236 trading days per docs/S8_SPEC.md),
skipping any date with no SPXW parquet on disk (same convention as Stage A).

Writes one row per simulated trade to s8_sim_calibration_2025_2026.csv
(gitignored per folder convention) and prints an aggregate summary comparable
to docs/S8_SPEC.md section 4's headline (+$138,982 / +108.8%) and the
per-template real-fills numbers in TEMPLATE_FIXED_AND_GRID_ANALYSIS.md /
S8_80_4_ONLY_FULL_BACKTEST.md.

Sizing note: simulate_day() returns pnl_per_spread (dollars for ONE spread).
Real S8 trades size multiple contracts per combo. This script reports on a
PER-SPREAD basis (i.e. treats every simulated entry as 1 contract) -- the
apples-to-apples comparison this enables is percentage-shape and per-template
relative-magnitude, NOT an absolute-dollar match to the real $127,710-balance
headline, which is explicitly sized. This is stated as a caveat, not hidden;
see SIMULATOR_STAGE_B_PROGRESS.md for the sizing discussion.
"""

import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from s8_mechanical_simulator import TEMPLATES, simulate_day, results_to_dataframe, WAREHOUSE

START_DATE = "20250709"
END_DATE = "20260707"


def trading_days_with_data():
    """All YYYYMMDD parquet files on disk in [START_DATE, END_DATE]."""
    files = sorted(p.stem for p in (WAREHOUSE / "ohlc").glob("*.parquet"))
    return [f for f in files if START_DATE <= f <= END_DATE]


def main():
    days = trading_days_with_data()
    print(f"Found {len(days)} trading days with SPXW parquet on disk in "
          f"[{START_DATE}, {END_DATE}] (of 236 real trading days in the window per S8_SPEC.md)")
    if days:
        print(f"  first: {days[0]}, last: {days[-1]}")
    missing_tail_note = "2026-07-02 through 2026-07-07 expected missing (Stage A already found no parquet past 2026-07-01)"
    print(f"  note: {missing_tail_note}")

    all_rows = []
    t0 = time.time()
    n_days_done = 0
    n_days_skipped = 0

    for i, date_str in enumerate(days):
        day_had_data = False
        for tmpl_key, tmpl in TEMPLATES.items():
            if tmpl_key == "Calls-80-$4b":
                continue  # unused alias, explicitly excluded per simulator's own comment
            try:
                trades = simulate_day(date_str, tmpl)
            except Exception as e:
                print(f"  ERROR {date_str} {tmpl_key}: {e}")
                continue
            if trades:
                day_had_data = True
                for t in trades:
                    all_rows.append(t.__dict__)
        if day_had_data:
            n_days_done += 1
        else:
            n_days_skipped += 1

        if (i + 1) % 10 == 0 or (i + 1) == len(days):
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(days) - (i + 1)) / rate if rate > 0 else float("nan")
            print(f"  [{i+1}/{len(days)}] {date_str} done, {len(all_rows)} trades so far, "
                  f"{elapsed:.0f}s elapsed, ~{eta:.0f}s remaining")

    df = pd.DataFrame(all_rows)
    out_name = sys.argv[1] if len(sys.argv) > 1 else "s8_sim_calibration_2025_2026.csv"
    out_path = Path(__file__).parent / out_name
    df.to_csv(out_path, index=False)
    print(f"\nWrote {out_path} ({len(df)} rows)")

    print("\n" + "=" * 78)
    print("AGGREGATE SUMMARY (per-spread basis, dollars for ONE contract)")
    print("=" * 78)
    print(f"Total simulated trades: {len(df)}")
    print(f"Days with >=1 sim trade: {n_days_done} / {len(days)} parquet-available days")
    valid = df.dropna(subset=["pnl_per_spread"])
    print(f"Trades with a valid pnl_per_spread: {len(valid)} / {len(df)}")
    total_pnl = valid["pnl_per_spread"].sum()
    print(f"\nTOTAL simulated P&L (per-spread basis): ${total_pnl:,.2f}")

    print("\nPer-template totals:")
    by_tmpl = valid.groupby("template")["pnl_per_spread"].agg(["sum", "count", "mean"])
    print(by_tmpl.to_string())

    print("\nExit reason breakdown:")
    print(df["exit_reason"].value_counts(dropna=False).to_string())

    return df


if __name__ == "__main__":
    main()
