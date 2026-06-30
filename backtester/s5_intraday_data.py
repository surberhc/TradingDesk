r"""
s5_intraday_data.py — clean reader / reconstructor for the SPXW 1-minute warehouse.

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

This module is pure data plumbing for the upcoming S5 "real harvest engine": it gives
that engine a tested, typed intraday-chain API. It makes NO strategy or parameter
decisions and curve-fits nothing — it only honors the LOSSLESS storage contract that
`datacollector\collect_spxw_1m.py` writes, and reconstructs the per-minute chain from it.

THE STORAGE CONTRACT WE HONOR ON READ (verbatim from the collector docstring):

  Data tree (we never write/modify/delete any of it):
    C:\TradingDesk-Local\warehouse\raw\options_1m\SPXW\quote\{YYYYMMDD}.parquet
    C:\TradingDesk-Local\warehouse\raw\options_1m\SPXW\ohlc\{YYYYMMDD}.parquet

  QUOTE (NBBO) = STORE-ON-CHANGE per contract key (symbol, expiration, strike, right):
    A row is kept only when (bid, ask, bid_size, ask_size) differs from the previously
    kept row for that key; each key's first row of the day is the baseline. To get the
    NBBO for ANY minute, FORWARD-FILL the last kept row within each contract key. A
    contract's quote is UNDEFINED before its first kept timestamp (we leave it NaN — we
    never back-fill, because that would invent a quote that did not exist => look-ahead).

  OHLC = TRADE BARS ONLY: minutes that did not trade are intentionally ABSENT. A missing
    minute means "no trade", NOT missing data. We do NOT forward-fill OHLC (a trade bar
    is point-in-time, not a state that persists).

PUBLIC API
  available_days() -> list[date]
      Days where BOTH the quote and ohlc parquet exist and are non-empty.
  load_day(d) -> DayData
      Raw (kept-rows) quote + ohlc frames for day d, timestamps parsed to datetime.
  nbbo_grid(d, expiration=None, minutes=None) -> pd.DataFrame
      Per-minute NBBO reconstructed by forward-filling within each contract key.
  zero_dte_chain(d, minutes=None) -> ZeroDteChain
      The 0DTE slice (expiration == trade date d): reconstructed minute-grid NBBO joined
      with that day's traded OHLC bars (bars present only where a trade occurred).

Everything is plain pandas with comments on the "why". Timestamps are tz-naive datetimes
representing exchange local minutes (09:30 .. 16:00).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------- #
# Paths. Mirrors datacollector\config.py (DATA_ROOT) + collect_spxw_1m.py layout.
# Hardcoded here to match the standalone s5_*.py convention in this folder and to
# keep this reader importable without the collector's `config` module on sys.path.
# --------------------------------------------------------------------------- #
WAREHOUSE_ROOT = Path(r"C:\TradingDesk-Local\warehouse\raw\options_1m\SPXW")
QUOTE_DIR = WAREHOUSE_ROOT / "quote"
OHLC_DIR = WAREHOUSE_ROOT / "ohlc"

SYMBOL = "SPXW"

# The four columns that identify a single option contract (the "contract key").
CONTRACT_KEY = ["symbol", "expiration", "strike", "right"]

# The NBBO payload that is valid-until-next-kept-row for each contract key. The
# change test in the collector is on (bid, ask, bid_size, ask_size); the ancillary
# exchange/condition columns ride along on the kept rows and are forward-filled too.
_QUOTE_PAYLOAD = [
    "bid",
    "ask",
    "bid_size",
    "ask_size",
    "bid_exchange",
    "bid_condition",
    "ask_exchange",
    "ask_condition",
]


# --------------------------------------------------------------------------- #
# Typed containers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DayData:
    """One trading day's raw, kept-rows warehouse frames (no reconstruction yet)."""

    day: _dt.date
    quote: pd.DataFrame  # store-on-change NBBO rows; `timestamp` is datetime64
    ohlc: pd.DataFrame   # trade-bars-only; `timestamp` is datetime64


@dataclass(frozen=True)
class ZeroDteChain:
    """The reconstructed 0DTE chain for one day (expiration == that trade date).

    `nbbo` is the dense per-minute forward-filled NBBO grid; `bars` are the day's
    actual trade bars (sparse — present only where a trade occurred that minute).
    """

    day: _dt.date
    expiration: _dt.date
    nbbo: pd.DataFrame   # minute grid: strike, right, minute, bid, ask, sizes, ...
    bars: pd.DataFrame   # trade bars: strike, right, minute, open, high, low, close, volume, ...


# --------------------------------------------------------------------------- #
# Filename <-> date helpers
# --------------------------------------------------------------------------- #
def _day_to_stem(d: _dt.date) -> str:
    """date -> 'YYYYMMDD' filename stem used by the collector."""
    return d.strftime("%Y%m%d")


def _stem_to_day(stem: str) -> _dt.date:
    """'YYYYMMDD' filename stem -> date."""
    return _dt.datetime.strptime(stem, "%Y%m%d").date()


def _quote_path(d: _dt.date) -> Path:
    return QUOTE_DIR / f"{_day_to_stem(d)}.parquet"


def _ohlc_path(d: _dt.date) -> Path:
    return OHLC_DIR / f"{_day_to_stem(d)}.parquet"


def _nonempty_parquet(p: Path) -> bool:
    """True iff the parquet exists and has at least one row.

    We check the row count via the parquet metadata (no full read) so that
    `available_days()` stays cheap even across ~880 days.
    """
    if not p.is_file():
        return False
    try:
        import pyarrow.parquet as pq

        return pq.ParquetFile(p).metadata.num_rows > 0
    except Exception:
        # Fallback: a present-but-unreadable file is treated as not-available
        # rather than raising, so one corrupt day can't break discovery.
        try:
            return len(pd.read_parquet(p, columns=["symbol"])) > 0
        except Exception:
            return False


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def available_days() -> list[_dt.date]:
    """All days where BOTH the quote and ohlc parquet exist and are non-empty.

    The collector's "a day is DONE only when BOTH files exist and are non-empty"
    rule is exactly our availability rule. Returned sorted ascending (oldest first).
    """
    quote_days = {
        _stem_to_day(p.stem)
        for p in QUOTE_DIR.glob("*.parquet")
        if _nonempty_parquet(p)
    }
    ohlc_days = {
        _stem_to_day(p.stem)
        for p in OHLC_DIR.glob("*.parquet")
        if _nonempty_parquet(p)
    }
    return sorted(quote_days & ohlc_days)


# --------------------------------------------------------------------------- #
# Loading (raw kept rows)
# --------------------------------------------------------------------------- #
def _parse_timestamps(df: pd.DataFrame) -> pd.DataFrame:
    """Parse the ISO-string `timestamp` column to tz-naive datetime64.

    The warehouse stores `timestamp` as e.g. '2022-08-26T09:30:00.000' (a string).
    We parse once on load so every downstream comparison is real datetime ordering,
    not string ordering.
    """
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"])
    return out


def load_day(d: _dt.date) -> DayData:
    """Load day `d`'s raw quote + ohlc frames (kept rows only, timestamps parsed).

    Raises FileNotFoundError if either file is missing — call `available_days()`
    first to pick days that are fully present.
    """
    qp, op = _quote_path(d), _ohlc_path(d)
    if not qp.is_file():
        raise FileNotFoundError(f"no quote parquet for {d}: {qp}")
    if not op.is_file():
        raise FileNotFoundError(f"no ohlc parquet for {d}: {op}")
    quote = _parse_timestamps(pd.read_parquet(qp))
    ohlc = _parse_timestamps(pd.read_parquet(op))
    return DayData(day=d, quote=quote, ohlc=ohlc)


# --------------------------------------------------------------------------- #
# NBBO reconstruction (the heart of the contract)
# --------------------------------------------------------------------------- #
def _minute_index(day: _dt.date) -> pd.DatetimeIndex:
    """The regular-session minute grid 09:30 .. 16:00 inclusive (391 minutes).

    The warehouse's dense grid is exactly these minutes (verified on disk). The
    reconstruction uses this as the target index for forward-filling.
    """
    start = _dt.datetime.combine(day, _dt.time(9, 30))
    end = _dt.datetime.combine(day, _dt.time(16, 0))
    return pd.date_range(start, end, freq="1min")


def nbbo_grid(
    d: _dt.date,
    expiration: _dt.date | str | None = None,
    minutes: pd.DatetimeIndex | None = None,
    quote: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Reconstruct the per-minute NBBO grid for day `d` by forward-filling within
    each contract key, exactly per the store-on-change storage contract.

    Parameters
    ----------
    d           the trade date to reconstruct.
    expiration  optional filter to one expiration (date or 'YYYY-MM-DD' string).
    minutes     optional target minute grid; defaults to the full 09:30..16:00 session.
    quote       optional pre-loaded kept-rows quote frame (skips the parquet read);
                must be the SAME schema `load_day` returns (timestamp parsed).

    Returns
    -------
    Tidy DataFrame, one row per (expiration, strike, right, minute):
        expiration, strike, right, minute,
        bid, ask, bid_size, ask_size, bid_exchange, bid_condition,
        ask_exchange, ask_condition
    For a (contract, minute) BEFORE that contract's first kept quote, every payload
    field is NaN — we never back-fill, because a quote that did not yet exist must
    not be invented (that would be look-ahead). After the first kept quote, the
    value is the most recent kept quote at-or-before that minute.

    Why this is the correct reconstruction
    --------------------------------------
    Store-on-change means each kept row is the START of an interval that persists
    until the next kept row for the same contract. Reindexing each contract onto the
    minute grid and forward-filling reproduces, for every minute, "the most recent
    kept row at or before it" — which is the contract's definition of recovery.
    """
    q = quote if quote is not None else load_day(d).quote

    if expiration is not None:
        exp_str = expiration if isinstance(expiration, str) else expiration.strftime("%Y-%m-%d")
        q = q[q["expiration"] == exp_str]

    grid = minutes if minutes is not None else _minute_index(d)

    if q.empty:
        # Return an empty, correctly-typed frame so callers don't special-case it.
        cols = ["expiration", "strike", "right", "minute", *_QUOTE_PAYLOAD]
        return pd.DataFrame(columns=cols)

    # Floor each kept timestamp to its minute. Within a (key, minute) the store-on-
    # change rule already guarantees at most one kept row per minute, but flooring +
    # keeping the LAST makes us robust and gives a clean DatetimeIndex to reindex on.
    q = q.copy()
    q["minute"] = q["timestamp"].dt.floor("min")

    out_frames: list[pd.DataFrame] = []
    # Group by the full contract key; each group is one option's kept quotes.
    for (sym, exp, strike, right), g in q.groupby(CONTRACT_KEY, sort=False):
        g = g.sort_values("minute")
        # Collapse any (extremely rare) multiple kept rows in the same floored minute
        # to the last one — that is the freshest quote effective at that minute.
        g = g.drop_duplicates(subset="minute", keep="last").set_index("minute")
        # Reindex onto the full target grid, then forward-fill the payload. Minutes
        # before the first kept quote remain NaN (no back-fill => no look-ahead).
        ff = g[_QUOTE_PAYLOAD].reindex(grid).ffill()
        ff.index.name = "minute"
        ff = ff.reset_index()
        ff.insert(0, "right", right)
        ff.insert(0, "strike", strike)
        ff.insert(0, "expiration", exp)
        out_frames.append(ff)

    out = pd.concat(out_frames, ignore_index=True)
    # Stable, readable ordering.
    out = out.sort_values(["expiration", "strike", "right", "minute"]).reset_index(drop=True)
    return out


# --------------------------------------------------------------------------- #
# 0DTE chain
# --------------------------------------------------------------------------- #
def zero_dte_chain(
    d: _dt.date,
    minutes: pd.DatetimeIndex | None = None,
    day_data: DayData | None = None,
) -> ZeroDteChain:
    """Reconstruct the 0DTE chain for day `d` (expiration == the trade date).

    Returns a `ZeroDteChain` whose `nbbo` is the dense forward-filled minute grid for
    every 0DTE contract, and whose `bars` are that day's actual trade bars for the
    same 0DTE contracts (sparse — only minutes that traded). The two are kept as
    SEPARATE frames on purpose: forward-filling a quote (a persistent state) is correct,
    but forward-filling a trade bar (a point-in-time event) is NOT — so we never merge
    them in a way that would smear bars across no-trade minutes.

    Parameters
    ----------
    d         trade date; the 0DTE expiration is exactly this date.
    minutes   optional target minute grid (defaults to the full session).
    day_data  optional pre-loaded DayData (skips the parquet read).
    """
    dd = day_data if day_data is not None else load_day(d)
    exp_str = d.strftime("%Y-%m-%d")

    # --- NBBO side: filter to 0DTE first, then reconstruct on the minute grid. ---
    nbbo = nbbo_grid(d, expiration=d, minutes=minutes, quote=dd.quote)

    # --- Trade-bar side: 0DTE bars only, floored to the minute, NO forward-fill. ---
    bars = dd.ohlc[dd.ohlc["expiration"] == exp_str].copy()
    if not bars.empty:
        bars["minute"] = bars["timestamp"].dt.floor("min")
        keep = [
            "expiration", "strike", "right", "minute",
            "open", "high", "low", "close", "volume", "count", "vwap",
        ]
        bars = bars[keep].sort_values(["strike", "right", "minute"]).reset_index(drop=True)
    else:
        bars = pd.DataFrame(
            columns=[
                "expiration", "strike", "right", "minute",
                "open", "high", "low", "close", "volume", "count", "vwap",
            ]
        )

    return ZeroDteChain(day=d, expiration=d, nbbo=nbbo, bars=bars)
