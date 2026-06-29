"""
repull_20260626_pass2.py — second pass for 6/26.

Pass 1 ('all-NaN' gate) skipped 6 symbols whose gamma is NOT strictly all-NaN but
is effectively empty: only a tiny handful of populated greek cells (e.g. SPY 8/1922,
VXX 1/1192) out of a full ThetaData 41-col schema. That is still the low-quality
write the task wants replaced. This pass re-pulls any 6/26 file whose gamma fill
fraction is below GAMMA_MIN_FRAC, force-overwriting from ThetaData.

Reuses existing functions only (config/storage/download/thetadata_client).
"""

from __future__ import annotations

import pandas as pd

import config
import storage
import download
import thetadata_client as td

DAY = "20260626"
SYMBOLS = ["VXX", "VIXY", "SPY", "QQQ", "IWM", "UNG",
           "NVDA", "AAPL", "MSFT", "AMZN", "META", "AVGO", "TSLA"]
GAMMA_MIN_FRAC = 0.50  # below this => treat file as bad/empty-greek write


def gamma_frac(df: pd.DataFrame) -> float:
    if "gamma" not in df.columns or len(df) == 0:
        return 0.0
    return float(df["gamma"].notna().mean())


def main() -> None:
    assert td.connected(), "ThetaTerminal not reachable — aborting."
    print(f"PASS 2: re-pull {DAY} for files with gamma fill < {GAMMA_MIN_FRAC:.0%}\n",
          flush=True)

    overwrote, skipped, errors = [], [], []

    for sym in SYMBOLS:
        p = storage.partition_path(sym, DAY)
        try:
            cur = pd.read_parquet(p) if p.exists() else pd.DataFrame()
            frac = gamma_frac(cur)
            rows = len(cur)
        except Exception as e:
            frac, rows = 0.0, 0
            print(f"{sym:5s} read error: {e} -> will attempt pull", flush=True)

        if frac >= GAMMA_MIN_FRAC:
            skipped.append(sym)
            print(f"{sym:5s} BEFORE: rows={rows} gamma_frac={frac:.3f}  ->  "
                  f"SKIP (good)", flush=True)
            continue

        try:
            df = download.pull_day(sym, DAY)
            n = storage.write_day(sym, DAY, df)
            back = pd.read_parquet(p)
            nfrac = gamma_frac(back)
            ok = nfrac > 0 and len(back) > 0
            overwrote.append(sym)
            print(f"{sym:5s} BEFORE: rows={rows} gamma_frac={frac:.3f}  ->  "
                  f"OVERWROTE  AFTER: rows={len(back)} gamma_frac={nfrac:.3f} "
                  f"populated={'YES' if ok else 'NO'}", flush=True)
        except Exception as e:
            errors.append((sym, str(e)))
            print(f"{sym:5s} FAILED: {e}", flush=True)

    catalog_status = "not attempted"
    try:
        print("\nRebuilding catalog (slow)...", flush=True)
        storage.rebuild_catalog()
        catalog_status = "OK"
    except Exception as e:
        catalog_status = f"ERROR (parquet writes still stand): {e}"

    print("\n===== PASS2 SUMMARY =====", flush=True)
    print(f"Overwrote ({len(overwrote)}): {overwrote}", flush=True)
    print(f"Skipped   ({len(skipped)}): {skipped}", flush=True)
    print(f"Errors    ({len(errors)}): {errors}", flush=True)
    print(f"Catalog rebuild: {catalog_status}", flush=True)


if __name__ == "__main__":
    main()
