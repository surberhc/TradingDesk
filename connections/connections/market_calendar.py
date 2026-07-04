"""market_calendar.py — the one shared US equity market (NYSE/NASDAQ) trading calendar.

Why this exists
---------------
Nightly jobs and the EOD report ask "should there be fresh data by now?" The honest
answer is not "is it a weekday" — it's "was the last session a real trading session."
On a holiday (or weekend) there is NO new EOD data, so a "stale" flag is a FALSE alarm.
This module is the single source of truth for which calendar days the US stock market
is open, closed, or closes early, so freshness/expectation logic everywhere measures
against the real last session instead of the literal calendar day.

Design contract (deliberate, per the desk's "verify, don't claim" rule)
-----------------------------------------------------------------------
The holiday tables are HAND-VERIFIED per year against NYSE's official published
schedule (nyse.com/markets/hours-calendars + the ICE "NYSE Group Announces …
Holiday and Early Closings Calendar" press release). They are NOT computed from
weekday rules, because the observance edge cases (a Saturday holiday observed the
prior Friday, a Sunday holiday observed the following Monday, Good Friday, and
one-off national days of mourning) are exactly where a clever algorithm quietly
gets it wrong.

Consequence: a year that is not in the table is UNKNOWN, not "assumed open." Every
lookup for an un-tabled year raises `CalendarYearMissing` so the gap fails LOUD
(a visible alarm telling you to add the year) instead of silently green-lighting
stale data. Add each new year from the NYSE source before it starts.

Verified years: 2025, 2026, 2027.

Quick use
---------
    from connections import market_calendar as mc
    mc.is_trading_day(date(2026, 7, 3))     # False — Independence Day (observed)
    mc.holiday_name(date(2026, 7, 3))       # "Independence Day (observed)"
    mc.last_trading_day(date(2026, 7, 4))   # date(2026, 7, 2) — Fri/Sat were closed
    mc.is_early_close(date(2026, 11, 27))   # True — 1:00pm close (day after Thanksgiving)
"""
from __future__ import annotations

import datetime as dt

__all__ = [
    "CalendarYearMissing", "KNOWN_YEARS",
    "is_weekend", "is_holiday", "holiday_name", "is_early_close", "early_close_name",
    "is_trading_day", "last_trading_day", "next_trading_day",
]


class CalendarYearMissing(KeyError):
    """Raised when a lookup touches a year with no verified holiday table.

    Callers that must never crash (e.g. the EOD report) should catch this and
    degrade to a weekend-only rule WITH a visible note, so the gap is loud."""


def _d(y: int, m: int, day: int) -> dt.date:
    return dt.date(y, m, day)


# --------------------------------------------------------------------------- #
# Verified full-day closures — date -> holiday name. NYSE/NASDAQ, no trading.
# Observance rule (already applied below): a holiday on Saturday is observed the
# preceding Friday; on Sunday, the following Monday.
# --------------------------------------------------------------------------- #
_HOLIDAYS: dict[int, dict[dt.date, str]] = {
    2025: {
        _d(2025, 1, 1): "New Year's Day",
        _d(2025, 1, 9): "National Day of Mourning (Jimmy Carter)",  # one-off, market closed
        _d(2025, 1, 20): "Martin Luther King, Jr. Day",
        _d(2025, 2, 17): "Washington's Birthday",
        _d(2025, 4, 18): "Good Friday",
        _d(2025, 5, 26): "Memorial Day",
        _d(2025, 6, 19): "Juneteenth National Independence Day",
        _d(2025, 7, 4): "Independence Day",
        _d(2025, 9, 1): "Labor Day",
        _d(2025, 11, 27): "Thanksgiving Day",
        _d(2025, 12, 25): "Christmas Day",
    },
    2026: {
        _d(2026, 1, 1): "New Year's Day",
        _d(2026, 1, 19): "Martin Luther King, Jr. Day",
        _d(2026, 2, 16): "Washington's Birthday",
        _d(2026, 4, 3): "Good Friday",
        _d(2026, 5, 25): "Memorial Day",
        _d(2026, 6, 19): "Juneteenth National Independence Day",
        _d(2026, 7, 3): "Independence Day (observed)",  # Jul 4 is a Saturday
        _d(2026, 9, 7): "Labor Day",
        _d(2026, 11, 26): "Thanksgiving Day",
        _d(2026, 12, 25): "Christmas Day",
    },
    2027: {
        _d(2027, 1, 1): "New Year's Day",
        _d(2027, 1, 18): "Martin Luther King, Jr. Day",
        _d(2027, 2, 15): "Washington's Birthday",
        _d(2027, 3, 26): "Good Friday",
        _d(2027, 5, 31): "Memorial Day",
        _d(2027, 6, 18): "Juneteenth National Independence Day (observed)",  # Jun 19 is a Saturday
        _d(2027, 7, 5): "Independence Day (observed)",  # Jul 4 is a Sunday
        _d(2027, 9, 6): "Labor Day",
        _d(2027, 11, 25): "Thanksgiving Day",
        _d(2027, 12, 24): "Christmas Day (observed)",  # Dec 25 is a Saturday
    },
}

# --------------------------------------------------------------------------- #
# Verified early closes — date -> name. Market closes 1:00pm ET (options 1:15pm).
# These are NORMAL trading days (is_trading_day is True); they just close early.
# --------------------------------------------------------------------------- #
_EARLY_CLOSES: dict[int, dict[dt.date, str]] = {
    2025: {
        _d(2025, 7, 3): "Day before Independence Day",
        _d(2025, 11, 28): "Day after Thanksgiving",
        _d(2025, 12, 24): "Christmas Eve",
    },
    2026: {
        _d(2026, 11, 27): "Day after Thanksgiving",
        _d(2026, 12, 24): "Christmas Eve",
        # No Jul-3 early close: Jul 3 is the full-closure observed holiday this year.
    },
    2027: {
        _d(2027, 11, 26): "Day after Thanksgiving",
        # No Christmas-Eve early close: Dec 24 is the full-closure observed holiday.
        # No Jul-3 early close: Jul 3, 2027 is a Saturday.
    },
}

KNOWN_YEARS = frozenset(_HOLIDAYS)


def _require_year(year: int) -> None:
    if year not in _HOLIDAYS:
        raise CalendarYearMissing(
            f"No verified NYSE market calendar for {year}. Add it to "
            f"connections/connections/market_calendar.py from nyse.com/markets/hours-calendars. "
            f"Known years: {sorted(KNOWN_YEARS)}."
        )


def is_weekend(d: dt.date) -> bool:
    """Saturday or Sunday. Never raises (no table needed)."""
    return d.weekday() >= 5


def is_holiday(d: dt.date) -> bool:
    """True if `d` is a full-day market closure. Raises CalendarYearMissing if the
    year has no verified table."""
    _require_year(d.year)
    return d in _HOLIDAYS[d.year]


def holiday_name(d: dt.date) -> str | None:
    """Holiday name for `d`, or None if it is not a full-day closure."""
    _require_year(d.year)
    return _HOLIDAYS[d.year].get(d)


def is_early_close(d: dt.date) -> bool:
    """True if `d` is a normal trading day that closes early (1:00pm ET)."""
    _require_year(d.year)
    return d in _EARLY_CLOSES[d.year]


def early_close_name(d: dt.date) -> str | None:
    """Reason for the early close on `d`, or None if it is a full session."""
    _require_year(d.year)
    return _EARLY_CLOSES[d.year].get(d)


def is_trading_day(d: dt.date) -> bool:
    """True if the market is open at all on `d` (a normal OR early-close session).
    False on weekends and full-day holidays. Raises CalendarYearMissing if the
    year has no verified table."""
    return not is_weekend(d) and not is_holiday(d)


def last_trading_day(asof: dt.date, *, inclusive: bool = True) -> dt.date:
    """The most recent trading session on or before `asof` (or strictly before it
    when inclusive=False). This is the freshness anchor: after the close, the last
    session's EOD data is what *should* be present.

    Raises CalendarYearMissing if the walk-back leaves the verified year range
    (so an unknown boundary fails loud rather than guessing)."""
    d = asof if inclusive else asof - dt.timedelta(days=1)
    for _ in range(370):  # generous bound; the longest US market gap is a few days
        if is_trading_day(d):  # may raise CalendarYearMissing — intended
            return d
        d -= dt.timedelta(days=1)
    raise RuntimeError(f"no trading day found within 370 days before {asof!r}")


def next_trading_day(asof: dt.date, *, inclusive: bool = False) -> dt.date:
    """The next trading session on or after `asof` (strictly after by default)."""
    d = asof if inclusive else asof + dt.timedelta(days=1)
    for _ in range(370):
        if is_trading_day(d):
            return d
        d += dt.timedelta(days=1)
    raise RuntimeError(f"no trading day found within 370 days after {asof!r}")
