"""
Data-layer tests — src/data_loader.py against the real files in data/.

The property that matters most here is inception-awareness (DATA.md, SPEC.md
§2-§3): an ETF must be NaN on every date before it began trading, and we must
never forward-fill or fabricate across that boundary. These tests run against
the actual downloaded data and skip cleanly if the downloader hasn't run yet.
"""

import pandas as pd
import pytest

from strategies import config
from src import data_loader as dl

# Skip the whole module if no data has been downloaded yet.
_prices_available = bool(
    (dl.DATA_PATH.exists())
    and any(dl.DATA_PATH.glob("*.parquet"))
)
pytestmark = pytest.mark.skipif(
    not _prices_available,
    reason="no data in data/ — run `python -m src.download_data` first",
)


@pytest.fixture(scope="module")
def prices() -> pd.DataFrame:
    return dl.load_prices()


def test_universe_columns_present(prices):
    """Every ticker on disk from config.ALL_TICKERS loads as a column."""
    on_disk = [t for t in config.ALL_TICKERS if (dl.DATA_PATH / f"{t}.parquet").exists()]
    assert list(prices.columns) == on_disk
    assert len(prices.columns) > 0


def test_dates_sorted_and_unique(prices):
    """A clean trading-date index: ascending, no duplicates."""
    assert prices.index.is_monotonic_increasing
    assert not prices.index.has_duplicates


def test_no_zero_or_negative_prices(prices):
    """Real prices are strictly positive wherever they exist (NaN allowed)."""
    positive_or_nan = (prices > 0) | prices.isna()
    assert bool(positive_or_nan.all().all())


def test_inception_awareness_no_prefill(prices):
    """
    A late-starting ETF is NaN before inception and has a real price from
    inception onward — never forward-filled backward across the boundary.
    """
    inc = dl.inception_dates(prices)
    # XLC (Communication Services) launched mid-2018 — a clear late starter.
    late = "XLC"
    if late not in prices.columns:
        pytest.skip(f"{late} not in downloaded universe")
    start = inc[late]
    before = prices.loc[prices.index < start, late]
    after = prices.loc[prices.index >= start, late]
    assert before.isna().all(), "pre-inception cells must be NaN, not filled"
    assert pd.notna(after.iloc[0]), "first in-life value must be a real price"
    assert start > prices.index.min(), "late starter should not begin at data floor"


def test_inception_dates_match_first_valid(prices):
    """inception_dates() equals each column's first non-NaN date."""
    inc = dl.inception_dates(prices)
    for ticker in prices.columns:
        assert inc[ticker] == prices[ticker].first_valid_index()


def test_treasury_yield_real_source():
    """The 10y yield loaded from a real Treasury source (not the ETF proxy)."""
    series, source = dl.load_treasury_10y()
    if series is None:
        pytest.skip(f"yield is a proxy this run: {source}")
    assert source == "us_treasury_par_yield"
    assert len(series) > 0
    assert series.index.is_monotonic_increasing
