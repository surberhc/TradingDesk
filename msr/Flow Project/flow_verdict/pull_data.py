"""
pull_data.py  (optional)
========================
Refresh data/spy_hist_2008_2026.csv from Tiingo.

The API token is read at runtime from a local .env (default the backtester's,
kept OUTSIDE Drive by design) and is NEVER written to disk or printed. The
reproduction script does not need this — it uses the cached CSV.

Usage:
    py pull_data.py                       # uses default ENV_PATH + symbol SPY
    set TIINGO_ENV=C:\\path\\to\\.env & py pull_data.py
"""

import os
import csv
import json
import urllib.request

ENV_PATH = os.environ.get("TIINGO_ENV", r"C:\Users\andre\backtester\.env")
SYMBOL = os.environ.get("TIINGO_SYMBOL", "SPY")
START = os.environ.get("TIINGO_START", "2008-01-01")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "spy_hist_2008_2026.csv")


def _token(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("TIINGO_API_KEY="):
                return line.strip().split("=", 1)[1]
    raise SystemExit(f"TIINGO_API_KEY not found in {path}")


def main():
    tok = _token(ENV_PATH)
    url = (f"https://api.tiingo.com/tiingo/daily/{SYMBOL}/prices"
           f"?startDate={START}&format=json&token={tok}")
    req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "close", "adjClose", "volume"])
        for d in data:
            w.writerow([d["date"][:10], d["close"], d["adjClose"], d["volume"]])
    print(f"{SYMBOL}: {len(data)} rows  {data[0]['date'][:10]} -> "
          f"{data[-1]['date'][:10]}  -> {OUT}")


if __name__ == "__main__":
    main()
