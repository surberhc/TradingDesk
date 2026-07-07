"""
fred.py — the one way to pull FRED (Federal Reserve Economic Data) series.

Free, keyed API. The key is read from the `FRED_API_KEY` Windows user env var, falling back
to the desk-wide secrets file `C:\\TradingDesk-Local\\secrets\\.env` (needed in scheduled-task
contexts that don't inherit the interactive user's env vars) — never hard-coded, never
printed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import requests

_BASE = "https://api.stlouisfed.org/fred/series/observations"
_SECRETS_ENV = Path(r"C:\TradingDesk-Local\secrets\.env")


def _load_secrets_env() -> None:
    """Backfill FRED_API_KEY from the desk-wide secrets .env if not already in the env."""
    if os.environ.get("FRED_API_KEY") or not _SECRETS_ENV.exists():
        return
    for line in _SECRETS_ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("FRED_API_KEY="):
            os.environ["FRED_API_KEY"] = line.split("=", 1)[1].strip()
            return


def _api_key() -> str:
    _load_secrets_env()
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise RuntimeError(
            "FRED_API_KEY is not set (Windows user env var, or "
            f"{_SECRETS_ENV}, kept off Drive)."
        )
    return key


def fetch_series(series_id: str, start: str | None = None, end: str | None = None) -> pd.Series:
    """Return one FRED series as a date-indexed float Series (missing/blank values dropped)."""
    params = {"series_id": series_id, "api_key": _api_key(), "file_type": "json"}
    if start:
        params["observation_start"] = start
    if end:
        params["observation_end"] = end
    resp = requests.get(_BASE, params=params, timeout=30)
    resp.raise_for_status()
    obs = resp.json().get("observations", [])
    rows = [(o["date"], o["value"]) for o in obs if o.get("value") not in (".", "", None)]
    if not rows:
        return pd.Series(dtype=float, name=series_id)
    idx, vals = zip(*rows)
    s = pd.Series([float(v) for v in vals], index=pd.to_datetime(idx), name=series_id)
    return s.sort_index()
