"""
tiingo.py — the one way to pull Tiingo daily prices.

Tiingo's FREE tier serves daily split+dividend-adjusted EOD prices. The API key is read
from the `TIINGO_API_KEY` Windows user env var, falling back to the desk-wide secrets file
`C:\\TradingDesk-Local\\secrets\\.env` (needed in scheduled-task contexts that don't inherit
the interactive user's env vars) — never hard-coded, never printed.

fetch_daily / fetch_many return adjusted-close-only Series/DataFrame (the original, minimal
shape — kept for existing callers). fetch_ohlcv returns the full row set (open/high/low/
close/volume/adjClose/adjOpen/adjHigh/adjLow/adjVolume/divCash/splitFactor) for callers that
need more than adjClose. All three raise TiingoRateLimited on a 429 so callers with a large,
resumable pull can implement their own stop-and-resume policy instead of losing progress.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pandas as pd
import requests

_BASE = "https://api.tiingo.com/tiingo/daily"
_SECRETS_ENV = Path(r"C:\TradingDesk-Local\secrets\.env")


class TiingoRateLimited(Exception):
    """Raised when Tiingo signals the free-tier cap (HTTP 429) or a cap message in the body."""


def _load_secrets_env() -> None:
    """Backfill TIINGO_API_KEY from the desk-wide secrets .env if not already in the env."""
    if os.environ.get("TIINGO_API_KEY") or not _SECRETS_ENV.exists():
        return
    for line in _SECRETS_ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("TIINGO_API_KEY="):
            os.environ["TIINGO_API_KEY"] = line.split("=", 1)[1].strip()
            return


def _api_key() -> str:
    _load_secrets_env()
    key = os.environ.get("TIINGO_API_KEY")
    if not key:
        raise RuntimeError(
            "TIINGO_API_KEY is not set (Windows user env var, or "
            f"{_SECRETS_ENV}, kept off Drive)."
        )
    return key


def _get(symbol: str, params: dict, max_retries: int = 3) -> list | None:
    """
    One paced GET against /{symbol}/prices. Returns the raw JSON row list, or None for an
    honest miss (404 / empty body). Raises TiingoRateLimited on the free-tier cap, or
    RuntimeError after exhausting retries on repeated network/5xx errors.
    """
    url = f"{_BASE}/{symbol}/prices"
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            last_exc = e
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 429:
            raise TiingoRateLimited(f"429 on {symbol}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 500:
            last_exc = RuntimeError(f"{resp.status_code} on {symbol}")
            time.sleep(2 * (attempt + 1))
            continue
        if not resp.ok:
            txt = resp.text.lower()
            if "limit" in txt or "exceeded" in txt:
                raise TiingoRateLimited(f"cap text on {symbol}: {resp.text[:80]}")
            return None  # other 4xx (bad symbol etc.) — an honest miss, not a retry
        return resp.json()
    raise RuntimeError(f"Tiingo request failed for {symbol}: {last_exc}")


def fetch_daily(ticker: str, start: str = "2010-01-01", end: str | None = None) -> pd.Series:
    """Return one ticker's daily ADJUSTED close as a date-indexed Series (free tier)."""
    params = {"startDate": start, "format": "json", "token": _api_key()}
    if end:
        params["endDate"] = end
    rows = _get(ticker, params)
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


def fetch_ohlcv(ticker: str, start: str = "2010-01-01", end: str | None = None) -> pd.DataFrame | None:
    """
    Full daily OHLCV + adjusted fields for one ticker (for callers that need more than
    adjClose — volume, raw OHLC, split factors). Returns None on an honest miss (unlisted /
    no data in window). Raises TiingoRateLimited on the free-tier cap.
    """
    params = {"startDate": start, "format": "json", "token": _api_key()}
    if end:
        params["endDate"] = end
    rows = _get(ticker, params)
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    return df.sort_values("date").reset_index(drop=True)
