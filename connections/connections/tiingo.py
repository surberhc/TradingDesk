"""
tiingo.py — the one way to pull Tiingo daily prices.

Tiingo's FREE tier serves daily split+dividend-adjusted EOD prices for our ETF
universe (~28 tickers). The API key is the `TIINGO_API_KEY` Windows user env var —
read here, never hard-coded, never printed.

NOTE (consolidation TODO): the backtester's `src/download_data.py` is the fuller,
validated Tiingo downloader (manifest + quality checks). It should be migrated to call
this shared helper so there is a single Tiingo code path. Until then, treat that
downloader as canonical for the backtester's data pulls and this as the shared entry.
"""
from __future__ import annotations

import os

import pandas as pd
import requests

_BASE = "https://api.tiingo.com/tiingo/daily"


def _api_key() -> str:
    key = os.environ.get("TIINGO_API_KEY")
    if not key:
        raise RuntimeError(
            "TIINGO_API_KEY env var is not set (it is a Windows user env var, kept off Drive)."
        )
    return key


def fetch_daily(ticker: str, start: str = "2010-01-01", end: str | None = None) -> pd.Series:
    """Return one ticker's daily ADJUSTED close as a date-indexed Series (free tier)."""
    params = {"startDate": start, "format": "json", "token": _api_key()}
    if end:
        params["endDate"] = end
    resp = requests.get(f"{_BASE}/{ticker}/prices", params=params, timeout=30)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return pd.Series(dtype=float, name=ticker)
    df = pd.DataFrame(rows)
    s = pd.Series(df["adjClose"].values,
                  index=pd.to_datetime(df["date"]).dt.tz_localize(None),
                  name=ticker)
    return s.sort_index()


def fetch_many(tickers, start: str = "2010-01-01", end: str | None = None) -> pd.DataFrame:
    """Wide frame of daily adjusted closes (one column per ticker)."""
    return pd.DataFrame({t: fetch_daily(t, start, end) for t in tickers})
