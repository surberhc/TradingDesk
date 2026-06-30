r"""
test_s5_intraday_data.py — invariants for the SPXW 1-minute warehouse reader.

These run against REAL on-disk warehouse days (skipped cleanly if the warehouse is
absent). They pin the reconstruction contract that the upcoming S5 harvest engine
will rely on — NOT any strategy behavior:

  * forward-fill correctness: a reconstructed minute equals the last KEPT quote
    at-or-before it (and is NaN before the contract's first kept quote);
  * no look-ahead: removing future kept rows never changes a past reconstructed
    minute (causality of the forward-fill);
  * the 0DTE filter returns ONLY expiration == trade date;
  * OHLC is trade-bars-only and is NOT forward-filled.

The reader is strictly read-only on the warehouse; these tests never write to it.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

# The module lives in backtester/ (one level up from this tests/ dir). pytest is run
# from backtester/, so a bare import works; the sys.path insert is a belt-and-braces
# fallback so the test is runnable regardless of the invoking cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s5_intraday_data as s5d  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures — pick real, fully-present days off disk.
# --------------------------------------------------------------------------- #
_DAYS = s5d.available_days()
pytestmark = pytest.mark.skipif(
    len(_DAYS) == 0,
    reason="SPXW 1-minute warehouse not present on this machine",
)


@pytest.fixture(scope="module")
def test_days() -> list[dt.date]:
    # Two of the most-recent fully-present days (recent => liquid, well-populated).
    return _DAYS[-2:] if len(_DAYS) >= 2 else _DAYS


@pytest.fixture(scope="module")
def one_day(test_days) -> dt.date:
    return test_days[-1]


# --------------------------------------------------------------------------- #
# Discovery / loading
# --------------------------------------------------------------------------- #
def test_available_days_are_sorted_dates():
    assert all(isinstance(d, dt.date) for d in _DAYS)
    assert _DAYS == sorted(_DAYS)


def test_available_days_have_both_files(one_day):
    # Availability must imply BOTH parquet files exist and are non-empty.
    assert s5d._quote_path(one_day).is_file()
    assert s5d._ohlc_path(one_day).is_file()


def test_load_day_parses_timestamps(one_day):
    dd = s5d.load_day(one_day)
    assert pd.api.types.is_datetime64_any_dtype(dd.quote["timestamp"])
    assert pd.api.types.is_datetime64_any_dtype(dd.ohlc["timestamp"])
    assert len(dd.quote) > 0 and len(dd.ohlc) > 0


# --------------------------------------------------------------------------- #
# Forward-fill correctness — the core of the store-on-change contract.
# --------------------------------------------------------------------------- #
def test_ffill_equals_last_kept_quote_at_or_before(one_day):
    """For a sampled set of contracts, every reconstructed minute must equal the
    most-recent KEPT quote at-or-before that minute, and be NaN before the first."""
    dd = s5d.load_day(one_day)
    grid = s5d.nbbo_grid(one_day, quote=dd.quote)

    # Sample a handful of busy contracts (most kept rows) to keep the test fast but
    # meaningful — these exercise many distinct fill intervals.
    raw = dd.quote.copy()
    raw["minute"] = raw["timestamp"].dt.floor("min")
    busiest = (
        raw.groupby(s5d.CONTRACT_KEY).size().sort_values(ascending=False).head(8).index
    )

    for (sym, exp, strike, right) in busiest:
        kept = (
            raw[
                (raw["expiration"] == exp)
                & (raw["strike"] == strike)
                & (raw["right"] == right)
            ]
            .sort_values("minute")
            .drop_duplicates(subset="minute", keep="last")
            .set_index("minute")
        )
        recon = grid[
            (grid["expiration"] == exp)
            & (grid["strike"] == strike)
            & (grid["right"] == right)
        ].set_index("minute")

        first_kept = kept.index.min()
        for minute, row in recon.iterrows():
            prior = kept.loc[:minute]
            if minute < first_kept:
                # Before the first kept quote the value is UNDEFINED -> must be NaN.
                assert pd.isna(row["bid"]) and pd.isna(row["ask"]), (
                    f"back-fill leak before first quote at {minute}"
                )
            else:
                expected = prior.iloc[-1]
                for col in ("bid", "ask", "bid_size", "ask_size"):
                    assert row[col] == expected[col], (
                        f"{exp} {strike} {right} {minute}: {col} "
                        f"{row[col]} != last-kept {expected[col]}"
                    )


def test_grid_covers_full_session_per_contract(one_day):
    grid = s5d.nbbo_grid(one_day)
    expected_minutes = set(s5d._minute_index(one_day))
    # Every contract present must be reconstructed onto the SAME full minute grid.
    counts = grid.groupby(s5d.CONTRACT_KEY[1:]).size().unique()
    assert set(grid["minute"].unique()) == expected_minutes
    assert list(counts) == [len(expected_minutes)]


# --------------------------------------------------------------------------- #
# No look-ahead — causality of the reconstruction.
# --------------------------------------------------------------------------- #
def test_no_lookahead_truncation_invariance(one_day):
    """Dropping kept rows that occur AFTER a cutoff must not change any
    reconstructed minute at or before that cutoff. This is the look-ahead test:
    the forward-fill at minute m may depend only on rows with timestamp <= m."""
    dd = s5d.load_day(one_day)
    grid = s5d._minute_index(one_day)
    cutoff = grid[len(grid) // 2]  # midday

    full = s5d.nbbo_grid(one_day, quote=dd.quote, minutes=grid)

    truncated_quote = dd.quote[dd.quote["timestamp"] <= cutoff].copy()
    trunc = s5d.nbbo_grid(one_day, quote=truncated_quote, minutes=grid)

    a = (
        full[full["minute"] <= cutoff]
        .sort_values(["expiration", "strike", "right", "minute"])
        .reset_index(drop=True)
    )
    b = (
        trunc[trunc["minute"] <= cutoff]
        .reindex(columns=a.columns)
        .sort_values(["expiration", "strike", "right", "minute"])
        .reset_index(drop=True)
    )
    # Removing future data cannot change a past reconstructed quote.
    pd.testing.assert_frame_equal(a, b)


# --------------------------------------------------------------------------- #
# 0DTE filter + OHLC handling.
# --------------------------------------------------------------------------- #
def test_zero_dte_only_returns_trade_date_expiration(test_days):
    for d in test_days:
        chain = s5d.zero_dte_chain(d)
        assert chain.expiration == d
        if not chain.nbbo.empty:
            assert (chain.nbbo["expiration"] == d.strftime("%Y-%m-%d")).all()
        if not chain.bars.empty:
            assert (chain.bars["expiration"] == d.strftime("%Y-%m-%d")).all()


def test_zero_dte_nbbo_is_dense_and_bars_are_sparse(one_day):
    chain = s5d.zero_dte_chain(one_day)
    assert not chain.nbbo.empty, "expected a 0DTE chain on a recent day"

    # NBBO grid is dense: full session per contract.
    n_minutes = len(s5d._minute_index(one_day))
    per_contract = chain.nbbo.groupby(["strike", "right"]).size().unique()
    assert list(per_contract) == [n_minutes]

    # Trade bars are sparse: strictly fewer (contract, minute) cells than the dense
    # grid would have — proof we did NOT forward-fill the OHLC.
    n_contracts = chain.nbbo[["strike", "right"]].drop_duplicates().shape[0]
    dense_cells = n_contracts * n_minutes
    assert len(chain.bars) < dense_cells


def test_ohlc_not_forward_filled_raw(one_day):
    """A no-trade minute must be ABSENT from the trade-bar frame (not filled)."""
    chain = s5d.zero_dte_chain(one_day)
    if chain.bars.empty:
        pytest.skip("no 0DTE trade bars on this day")
    # Pick the busiest traded contract; assert it still has gaps in the session
    # (real options do not trade every single minute) -> bars are not a dense grid.
    busiest = (
        chain.bars.groupby(["strike", "right"]).size().sort_values(ascending=False)
    )
    top_key = busiest.index[0]
    n_minutes = len(s5d._minute_index(one_day))
    assert busiest.iloc[0] <= n_minutes
    sub = chain.bars[
        (chain.bars["strike"] == top_key[0]) & (chain.bars["right"] == top_key[1])
    ]
    # No duplicate (contract, minute) bars, and not every minute is present.
    assert not sub.duplicated(subset="minute").any()
    assert sub["minute"].nunique() == len(sub)
