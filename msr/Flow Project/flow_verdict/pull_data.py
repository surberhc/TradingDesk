"""
pull_data.py  (optional)
========================
Refresh data/spy_hist_2008_2026.csv from Tiingo.

Uses the shared `connections.tiingo` client — the API key is read from the
`TIINGO_API_KEY` Windows user env var, falling back to the desk-wide secrets file
`C:\\TradingDesk-Local\\secrets\\.env`. Never written to disk or printed. The
reproduction script does not need this — it uses the cached CSV.

Usage:
    py pull_data.py                       # uses default symbol SPY, start 2008-01-01
    set TIINGO_SYMBOL=QQQ & set TIINGO_START=2015-01-01 & py pull_data.py
"""

import csv
import os

from connections.tiingo import fetch_ohlcv

SYMBOL = os.environ.get("TIINGO_SYMBOL", "SPY")
START = os.environ.get("TIINGO_START", "2008-01-01")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "spy_hist_2008_2026.csv")


def main():
    df = fetch_ohlcv(SYMBOL, start=START)
    if df is None or df.empty:
        print(f"{SYMBOL}: no data returned from Tiingo for start={START} — nothing written.")
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "close", "adjClose", "volume"])
        for row in df.itertuples(index=False):
            w.writerow([row.date.strftime("%Y-%m-%d"), row.close, row.adjClose, row.volume])
    first = df.iloc[0]["date"].strftime("%Y-%m-%d")
    last = df.iloc[-1]["date"].strftime("%Y-%m-%d")
    print(f"{SYMBOL}: {len(df)} rows  {first} -> {last}  -> {OUT}")


if __name__ == "__main__":
    main()
