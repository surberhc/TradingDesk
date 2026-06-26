"""
thetadata_client.py — thin client over the local ThetaData v3 Terminal.

No API key is needed here — the Terminal (started by start_terminal.py) holds the
key and serves localhost. We just make HTTP GETs and parse CSV into DataFrames.

The two endpoints we need to reconstruct dealer-gamma (GEX) at end of day:
  * /option/history/greeks/eod  -> gamma, IV, OHLC, and underlying_price (spot!)
  * /option/history/open_interest -> per-strike open interest
Joined on (symbol, expiration, strike, right, date), they give everything the
GEX / gamma-flip / expected-move features are built from.
"""

from __future__ import annotations

import io
import time

import pandas as pd
import requests

import config

_TIMEOUT = 120
_RETRIES = 4


def _get(path: str, params: dict) -> pd.DataFrame:
    """GET a CSV endpoint on the local Terminal -> DataFrame (with light retry)."""
    url = f"{config.THETA_BASE_URL}{path}"
    params = {**params, "format": "csv"}
    last = None
    for attempt in range(_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=_TIMEOUT)
            if r.status_code == 200:
                if not r.text.strip():
                    return pd.DataFrame()
                return pd.read_csv(io.StringIO(r.text))
            # 472 = no data for the request (valid empty); anything else -> retry/raise
            if r.status_code in (472, 404):
                return pd.DataFrame()
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {_RETRIES} tries: {last}")


def connected() -> bool:
    """Is the local Terminal up and serving?"""
    try:
        requests.get(f"{config.THETA_BASE_URL}/system/mdds/status", timeout=5)
        return True
    except requests.RequestException:
        return False


def eod_greeks(symbol: str, start: str, end: str,
               expiration: str = "*", right: str = "both") -> pd.DataFrame:
    """EOD greeks (incl. gamma, implied_vol, underlying_price) for a date range."""
    return _get("/option/history/greeks/eod", {
        "symbol": symbol, "expiration": expiration,
        "start_date": start, "end_date": end,
        "strike": "*", "right": right,
        "rate_type": config.THETA_RATE_TYPE,
    })


def eod_open_interest(symbol: str, start: str, end: str,
                      expiration: str = "*", right: str = "both") -> pd.DataFrame:
    """EOD per-strike open interest for a date range."""
    return _get("/option/history/open_interest", {
        "symbol": symbol, "expiration": expiration,
        "start_date": start, "end_date": end,
        "strike": "*", "right": right,
    })
