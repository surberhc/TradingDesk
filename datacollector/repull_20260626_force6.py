"""
repull_20260626_force6.py — UNCONDITIONAL re-pull of 6/26 for exactly 6 symbols.

These 6 still hold last night's low-quality IBKR-forward 6/26 file (gamma mostly
NaN, ~1200-1900 rows instead of ThetaData's ~13,990). A prior worker wrongly
judged them "fine" and skipped. This script ALWAYS overwrites them from
ThetaData — no "skip if it looks fine" gate of any kind.

Reuses existing functions only (config/storage/download/thetadata_client).
"""

from __future__ import annotations

import pandas as pd

import config
import storage
import download
import thetadata_client as td

DAY = "20260626"
SYMBOLS = ["VXX", "SPY", "QQQ", "IWM", "META", "TSLA"]


def stats(p) -> tuple[int, int]:
    """(rows, gamma_nonnull) for a parquet path, or (0, 0) if absent/unreadable."""
    try:
        if not p.exists():
            return 0, 0
        df = pd.read_parquet(p)
        rows = len(df)
        g = int(df["gamma"].notna().sum()) if "gamma" in df.columns else 0
        return rows, g
    except Exception:
        return 0, 0


def main() -> None:
    assert td.connected(), "ThetaTerminal not reachable — aborting."
    print(f"FORCE re-pull {DAY} (unconditional overwrite) for: {SYMBOLS}\n", flush=True)

    results, errors = [], []

    for sym in SYMBOLS:
        p = storage.partition_path(sym, DAY)
        before = stats(p)
        try:
            df = download.pull_day(sym, DAY)        # greeks ⨝ OI from ThetaData
            storage.write_day(sym, DAY, df)         # atomic OVERWRITE, no have_day
            after = stats(p)                        # read back to confirm
            full = after[0] > 0 and after[1] >= after[0] * 0.99
            results.append((sym, before, after, full))
            print(f"{sym:5s} BEFORE rows={before[0]} gamma_nonnull={before[1]}  ->  "
                  f"AFTER rows={after[0]} gamma_nonnull={after[1]}  "
                  f"{'FULLY POPULATED' if full else 'CHECK'}", flush=True)
        except Exception as e:
            errors.append((sym, str(e)))
            print(f"{sym:5s} FAILED: {e}", flush=True)

    catalog_status = "not attempted"
    try:
        print("\nRebuilding catalog (slow over ~102k files)...", flush=True)
        storage.rebuild_catalog()
        catalog_status = "OK"
    except Exception as e:
        catalog_status = f"ERROR (parquet writes still stand): {e}"

    full_count = sum(1 for _, _, _, full in results if full)
    print("\n===== FORCE6 SUMMARY =====", flush=True)
    for sym, before, after, full in results:
        print(f"  {sym:5s} {before} -> {after}  {'OK' if full else 'CHECK'}", flush=True)
    if errors:
        print(f"  ERRORS: {errors}", flush=True)
    print(f"  Catalog rebuild: {catalog_status}", flush=True)
    print(f"\n{full_count}/6 now fully populated", flush=True)


if __name__ == "__main__":
    main()
