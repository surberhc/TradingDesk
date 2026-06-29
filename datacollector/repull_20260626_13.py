"""
repull_20260626_13.py — one-off: re-pull 6/26 from ThetaData for the 13 symbols
that last night's IBKR forward run wrote with ALL-NaN (low-quality) greeks.

Reuses existing functions only (config/storage/download/thetadata_client).
For each symbol: check the on-disk 6/26 file; if gamma is all-NaN (bad), re-pull
from ThetaData and force-overwrite (storage.write_day -> atomic temp+os.replace).
If gamma is already populated (good), SKIP — never overwrite good data.
"""

from __future__ import annotations

import sys

import pandas as pd

import config
import storage
import download
import thetadata_client as td

DAY = "20260626"
SYMBOLS = ["VXX", "VIXY", "SPY", "QQQ", "IWM", "UNG",
           "NVDA", "AAPL", "MSFT", "AMZN", "META", "AVGO", "TSLA"]


def gamma_state(df: pd.DataFrame):
    """(rows, has_gamma_col, gamma_all_nan) for a chain DataFrame."""
    rows = len(df)
    has_col = "gamma" in df.columns
    all_nan = has_col and df["gamma"].isna().all()
    return rows, has_col, all_nan


def main() -> None:
    assert td.connected(), "ThetaTerminal not reachable — aborting."
    print(f"ThetaTerminal connected. Re-pull {DAY} for {len(SYMBOLS)} symbols.\n",
          flush=True)

    overwrote, skipped, errors = [], [], []
    results = {}

    for sym in SYMBOLS:
        line = {"sym": sym}
        # --- BEFORE: read current file, confirm greeks are actually bad ---
        p = storage.partition_path(sym, DAY)
        try:
            if not p.exists():
                before = (0, False, True)
                line["before"] = "NO FILE on disk"
            else:
                cur = pd.read_parquet(p)
                before = gamma_state(cur)
                rows, has_col, all_nan = before
                if not has_col:
                    line["before"] = f"rows={rows}, NO gamma column"
                else:
                    line["before"] = (f"rows={rows}, gamma_all_NaN={all_nan}, "
                                      f"gamma_nonnull={int(cur['gamma'].notna().sum())}")
        except Exception as e:
            before = (0, False, True)
            line["before"] = f"READ ERROR: {e}"

        before_rows, before_has_col, before_all_nan = before
        is_bad = (not before_has_col) or before_all_nan

        if not is_bad:
            line["action"] = "SKIP (greeks already good)"
            skipped.append(sym)
            results[sym] = line
            print(f"{sym:5s} BEFORE: {line['before']}  ->  {line['action']}", flush=True)
            continue

        # --- OVERWRITE: pull from ThetaData and force-write ---
        try:
            df = download.pull_day(sym, DAY)
            n = storage.write_day(sym, DAY, df)  # atomic temp + os.replace
            # read back and confirm
            back = pd.read_parquet(p)
            rows, has_col, all_nan = gamma_state(back)
            gamma_ok = has_col and not all_nan and rows > 0
            line["after"] = (f"rows={rows}, gamma_populated="
                             f"{'YES' if gamma_ok else 'NO'}, "
                             f"gamma_nonnull="
                             f"{int(back['gamma'].notna().sum()) if has_col else 0}")
            line["action"] = "OVERWROTE"
            overwrote.append(sym)
        except Exception as e:
            line["after"] = f"ERROR: {e}"
            line["action"] = "FAILED"
            errors.append((sym, str(e)))

        results[sym] = line
        print(f"{sym:5s} BEFORE: {line['before']}  ->  {line['action']}  "
              f"AFTER: {line.get('after','-')}", flush=True)

    # --- catalog rebuild (once, at the end; slow over ~102k files) ---
    catalog_status = "not attempted"
    try:
        print("\nRebuilding catalog (slow over the full parquet tree)...", flush=True)
        storage.rebuild_catalog()
        catalog_status = "OK"
    except Exception as e:
        catalog_status = f"ERROR (parquet writes still stand): {e}"
    print(f"Catalog rebuild: {catalog_status}", flush=True)

    # --- summary ---
    print("\n===== SUMMARY =====", flush=True)
    print(f"Overwrote ({len(overwrote)}): {overwrote}", flush=True)
    print(f"Skipped   ({len(skipped)}): {skipped}", flush=True)
    print(f"Errors    ({len(errors)}): {errors}", flush=True)
    print(f"Catalog: {catalog_status}", flush=True)


if __name__ == "__main__":
    main()
