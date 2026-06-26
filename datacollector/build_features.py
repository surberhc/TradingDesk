"""
build_features.py — turn raw EOD chains into a daily GEX feature table per root.

Reads every raw/options/{SYMBOL}/{YYYYMMDD}.parquet, computes one row of GEX
features per day (features/gex.py), and writes a small daily table to
derived/{SYMBOL}_gex_daily.parquet. These derived tables are tiny (one row/day)
and ARE safe to copy back to Drive for backup.

Usage:
    python build_features.py SPX          # one root
    python build_features.py              # every root that has raw data
"""

from __future__ import annotations

import sys

import pandas as pd

import config
import storage
from features import gex


def build_symbol(symbol: str) -> int:
    raw_dir = config.RAW_OPTIONS / symbol
    if not raw_dir.exists():
        print(f"  {symbol}: no raw data yet")
        return 0
    rows = []
    for p in sorted(raw_dir.glob("*.parquet")):
        chain = pd.read_parquet(p)
        if chain.empty:
            continue
        f = gex.day_features(chain)
        if f:
            rows.append(f)
    if not rows:
        print(f"  {symbol}: no usable days")
        return 0
    out = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    config.DERIVED.mkdir(parents=True, exist_ok=True)
    out.to_parquet(config.DERIVED / f"{symbol}_gex_daily.parquet", index=False)
    print(f"  {symbol}: {len(out)} days -> derived/{symbol}_gex_daily.parquet")
    return len(out)


def main(roots: list[str]) -> None:
    if not roots:
        roots = sorted(p.name for p in config.RAW_OPTIONS.glob("*") if p.is_dir())
    for symbol in roots:
        build_symbol(symbol)


if __name__ == "__main__":
    main([r.upper() for r in sys.argv[1:]])
