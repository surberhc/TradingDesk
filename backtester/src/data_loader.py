"""
data_loader.py — Load and validate local price data; handle inception dates.

Built to DATA.md and SPEC.md §2-§3. Loads the read-only files in data/ and
returns a wide adjusted-close (total-return) price frame: rows are trading
dates, columns are tickers. It is inception-aware — a column is simply NaN on
every date before that ETF began trading. We NEVER forward-fill across the
inception boundary or fabricate pre-inception data; the backtest's
inception-aware logic relies on these NaNs being truthful.

Reads only; treats data/ as read-only (CLAUDE.md rule 3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from strategies import config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = PROJECT_ROOT / config.DATA_DIR
MANIFEST_PATH = PROJECT_ROOT / config.MANIFEST_FILE

# Files in data/ that are not per-ticker price series.
_NON_TICKER_FILES = {
    "_manifest.json", "_treasury_10y.parquet", "_vix.parquet", "_hy_oas.parquet", ".gitkeep",
}


def _read_one(path: Path) -> pd.Series:
    """Read a single saved ticker file into a price Series named by its ticker."""
    if path.suffix == ".parquet":
        frame = pd.read_parquet(path)
    else:
        frame = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    # Each file holds exactly one price column named for the ticker.
    series = frame.iloc[:, 0]
    series.index = pd.to_datetime(series.index).normalize()
    series.name = path.stem
    return series.sort_index()


def load_prices(tickers: list[str] | None = None) -> pd.DataFrame:
    """
    Load adjusted-close price series for the universe from data/.

    Parameters
    ----------
    tickers : optional list to restrict the load. Defaults to every ticker
        present in data/ that also appears in config.ALL_TICKERS (macro-proxy
        extras like HYG are loaded only if explicitly requested).

    Returns
    -------
    A DataFrame indexed by trading date (ascending), one column per ticker,
    holding the adjusted close. Pre-inception cells are NaN by construction —
    not forward-filled. Columns are ordered to match config.ALL_TICKERS where
    possible for stable, readable output.
    """
    if not DATA_PATH.exists():
        raise FileNotFoundError(
            f"No data/ directory at {DATA_PATH}. Run `python -m src.download_data` first."
        )

    available: dict[str, pd.Series] = {}
    for path in sorted(DATA_PATH.iterdir()):
        if path.name in _NON_TICKER_FILES or path.is_dir():
            continue
        if path.suffix not in (".parquet", ".csv"):
            continue
        series = _read_one(path)
        available[series.name] = series

    if not available:
        raise FileNotFoundError(
            f"data/ has no price files at {DATA_PATH}. Run the downloader first."
        )

    if tickers is None:
        # Default universe load: config order, only what's on disk.
        wanted = [t for t in config.ALL_TICKERS if t in available]
    else:
        missing = [t for t in tickers if t not in available]
        if missing:
            raise KeyError(
                f"Requested tickers not found in data/: {missing}. "
                "Was the downloader run for them?"
            )
        wanted = list(tickers)

    # Outer-join on the union of all dates; pre-inception stays NaN (no fill).
    frame = pd.concat({t: available[t] for t in wanted}, axis=1)
    frame = frame[wanted].sort_index()
    return frame


def load_treasury_10y() -> tuple[pd.Series | None, str]:
    """
    Load the 10-year Treasury yield series and its source label.

    Returns (series, source). If only a proxy was recorded at download time,
    returns (None, "PROXY:...") so callers fall back to the labeled price-trend
    proxy (config.YIELD_PROXY_TICKER) and surface the proxy in the report.
    """
    info = load_manifest().get("tickers", {}).get("_treasury_10y", {})
    source = info.get("source", "unknown")
    path = DATA_PATH / "_treasury_10y.parquet"
    if path.exists():
        series = pd.read_parquet(path).iloc[:, 0]
        series.index = pd.to_datetime(series.index).normalize()
        series.name = "us_treasury_10y"
        return series.sort_index(), source
    return None, source


def _load_macro(filename: str, name: str) -> tuple[pd.Series | None, str]:
    """Load a saved macro series (VIX / HY OAS) and its recorded source label."""
    info = load_manifest().get("tickers", {}).get(f"_{name}", {})
    source = info.get("source", "unknown")
    path = DATA_PATH / filename
    if path.exists():
        series = pd.read_parquet(path).iloc[:, 0]
        series.index = pd.to_datetime(series.index).normalize()
        series.name = name
        return series.sort_index(), source
    return None, source


def load_vix() -> tuple[pd.Series | None, str]:
    """Real VIX close (CBOE) and source; (None, proxy-label) if not downloaded."""
    return _load_macro("_vix.parquet", "vix")


def load_hy_oas() -> tuple[pd.Series | None, str]:
    """Real HY credit spread (FRED) and source; (None, proxy-label) if absent."""
    return _load_macro("_hy_oas.parquet", "hy_oas")


def load_manifest() -> dict:
    """Load data/_manifest.json (download record). Empty dict if absent."""
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {}


def inception_dates(frame: pd.DataFrame | None = None) -> pd.Series:
    """
    First date each ticker has a real price (its inception, as seen in data/).

    Built from the loaded frame so the backtest can exclude an asset from any
    month before it began trading (SPEC.md §2-§3).
    """
    if frame is None:
        frame = load_prices()
    return frame.apply(lambda col: col.first_valid_index())
