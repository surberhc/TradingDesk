"""
Stage B, Task 3: extended 2022-01-03 through 2026-07-01 run of the S8
mechanical simulator, using the SAME entry-time grids derived in Task 1
(explicitly assumed constant back through 2022 -- no real schedule data
exists pre-2025-07, this is a stated assumption not a validated fact) and the
SAME locked fill model as Task 2.

ONLY run this after Task 2's calibration gap has been judged reasonable
(directionally consistent with real fills, no wrong-sign/order-of-magnitude
red flag) -- per the task's explicit stop-condition.

Chunked by year (1,127 total trading days on disk is too big for one
uninterrupted run): writes british_ic/s8_sim_results_YYYY.csv incrementally,
one file per year, so partial results exist on disk at all times even if the
run is interrupted. Run this script with a single year argument at a time:

    python s8_sim_extended_run.py 2022
    python s8_sim_extended_run.py 2023
    ... etc, or `python s8_sim_extended_run.py all` to run all years
    sequentially in one process (still writes per-year files as each
    completes, so a mid-run interruption still leaves completed years intact).
"""

import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from s8_mechanical_simulator import TEMPLATES, simulate_day, WAREHOUSE

YEARS = ["2022", "2023", "2024", "2025", "2026"]


def trading_days_for_year(year: str):
    files = sorted(p.stem for p in (WAREHOUSE / "ohlc").glob(f"{year}*.parquet"))
    return files


def run_year(year: str):
    days = trading_days_for_year(year)
    print(f"\n{'='*78}\nYEAR {year}: {len(days)} trading days with SPXW parquet on disk\n{'='*78}",
          flush=True)
    if not days:
        print(f"  no data for {year}, skipping")
        return

    all_rows = []
    t0 = time.time()
    for i, date_str in enumerate(days):
        for tmpl_key, tmpl in TEMPLATES.items():
            if tmpl_key == "Calls-80-$4b":
                continue
            try:
                trades = simulate_day(date_str, tmpl)
            except Exception as e:
                print(f"  ERROR {date_str} {tmpl_key}: {e}", flush=True)
                continue
            for t in trades:
                all_rows.append(t.__dict__)

        if (i + 1) % 10 == 0 or (i + 1) == len(days):
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(days) - (i + 1)) / rate if rate > 0 else float("nan")
            print(f"  [{year} {i+1}/{len(days)}] {date_str} done, {len(all_rows)} trades so far, "
                  f"{elapsed:.0f}s elapsed, ~{eta:.0f}s remaining", flush=True)

    df = pd.DataFrame(all_rows)
    out_path = Path(__file__).parent / f"s8_sim_results_{year}.csv"
    df.to_csv(out_path, index=False)
    valid = df.dropna(subset=["pnl_per_spread"]) if len(df) else df
    total = valid["pnl_per_spread"].sum() if len(valid) else 0.0
    print(f"\nYEAR {year} DONE: wrote {out_path} ({len(df)} rows). "
          f"Total simulated P&L (per-spread): ${total:,.2f}. "
          f"Elapsed: {time.time()-t0:.0f}s", flush=True)
    return df


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    years = YEARS if target == "all" else [target]
    summary = {}
    for y in years:
        df = run_year(y)
        if df is not None and len(df):
            valid = df.dropna(subset=["pnl_per_spread"])
            summary[y] = float(valid["pnl_per_spread"].sum())
    print("\n" + "=" * 78)
    print("MULTI-YEAR SUMMARY (per-spread basis)")
    print("=" * 78)
    for y, total in summary.items():
        print(f"  {y}: ${total:,.2f}")
    if summary:
        print(f"  TOTAL across run years: ${sum(summary.values()):,.2f}")


if __name__ == "__main__":
    main()
