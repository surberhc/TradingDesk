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

# ThetaData-RETIRED 2026-07-27 (subscription dead). Retained ONLY as the leaf
# client for the dormant download.py/universe_download.py and CANSLIM
# pull_equity_options.py. Not used by any live/nightly path (EOD feed is on
# IBKR now).

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


def list_expirations(symbol: str) -> list[str]:
    """Every expiration THIS root has ever had, as dashed strings (oldest first).

    Uses the /option/list/expirations catalog endpoint, which — unlike the history
    endpoints — does NOT take a date and never needs expiration=*, so it is the one
    way to enumerate a root's expirations for the CURRENT (unsettled) trading day.
    (The history endpoints reject expiration=* for the current day: HTTP 400
    "Cannot fetch current-day data without specifying an expiration".)
    """
    df = _get("/option/list/expirations", {"symbol": symbol})
    if df.empty or "expiration" not in df.columns:
        return []
    return sorted({str(x) for x in df["expiration"].dropna().unique()})


def connected(retries: int = 1, backoff_s: float = 5.0) -> bool:
    """Is the local Terminal up and serving?

    Default behavior (retries=1) is UNCHANGED from before: a single 5s-timeout GET,
    no sleep, return False immediately on failure. Every existing caller relies on
    this being a cheap one-shot check.

    Callers that expect the Terminal may momentarily be busy (e.g. eod_daily.py,
    which can race a long-running concurrent backfill on the same Terminal) can opt
    in to a small BOUNDED retry by passing retries > 1: on failure, sleep
    `backoff_s` seconds and try again, up to `retries` total attempts, returning
    True on the first success. This is NOT a background/continuous poll — it only
    runs inline, once, at the single call site that asks for it.
    """
    for attempt in range(retries):
        try:
            requests.get(f"{config.THETA_BASE_URL}/system/mdds/status", timeout=5)
            return True
        except requests.RequestException:
            if attempt < retries - 1:
                time.sleep(backoff_s)
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


def _looped_over_expirations(pull, symbol: str, day: str,
                             expirations: list[str]) -> pd.DataFrame:
    """Call `pull(symbol, day, day, expiration=exp)` for each exp, concatenate.

    Shared inner loop for the CURRENT-day path, where expiration=* is rejected so we
    must request one explicit expiration at a time. An empty part (472/no-data) is
    simply skipped; a transport error inside a single expiration propagates (via
    _get's own retry-then-raise) so the day is left un-done and retried next pass —
    we never persist a partial current-day file as if it were complete.
    """
    frames: list[pd.DataFrame] = []
    for exp in expirations:
        part = pull(symbol, day, day, expiration=exp)
        if not part.empty:
            frames.append(part)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def eod_greeks_current_day(symbol: str, day: str,
                           expirations: list[str]) -> pd.DataFrame:
    """Current-day EOD greeks: loop per explicit expiration (expiration=* is 400)."""
    return _looped_over_expirations(eod_greeks, symbol, day, expirations)


def eod_open_interest_current_day(symbol: str, day: str,
                                  expirations: list[str]) -> pd.DataFrame:
    """Current-day EOD open interest: loop per explicit expiration (=* is 400)."""
    return _looped_over_expirations(eod_open_interest, symbol, day, expirations)
