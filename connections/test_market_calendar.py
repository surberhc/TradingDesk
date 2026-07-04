"""Tests for the shared NYSE/NASDAQ trading calendar (connections/market_calendar.py)."""
import datetime as dt

import pytest

from connections import market_calendar as mc


def test_independence_day_2026_observed_friday():
    # Jul 4, 2026 is a Saturday -> observed Friday Jul 3 is a full closure.
    assert mc.is_holiday(dt.date(2026, 7, 3)) is True
    assert mc.holiday_name(dt.date(2026, 7, 3)) == "Independence Day (observed)"
    assert mc.is_trading_day(dt.date(2026, 7, 3)) is False
    assert mc.is_trading_day(dt.date(2026, 7, 2)) is True   # Thursday, open


def test_weekend_is_not_trading():
    assert mc.is_weekend(dt.date(2026, 7, 4)) is True       # Saturday
    assert mc.is_trading_day(dt.date(2026, 7, 4)) is False
    assert mc.is_trading_day(dt.date(2026, 7, 5)) is False  # Sunday


def test_last_trading_day_spans_holiday_weekend():
    # From Sat Jul 4, 2026 the last real session is Thu Jul 2 (Fri Jul 3 was the holiday).
    assert mc.last_trading_day(dt.date(2026, 7, 4)) == dt.date(2026, 7, 2)
    # A normal trading day returns itself.
    assert mc.last_trading_day(dt.date(2026, 7, 6)) == dt.date(2026, 7, 6)  # Monday
    # inclusive=False steps strictly before.
    assert mc.last_trading_day(dt.date(2026, 7, 2), inclusive=False) == dt.date(2026, 7, 1)


def test_next_trading_day_over_holiday():
    # After Fri Jul 3 holiday and the weekend, the next session is Mon Jul 6.
    assert mc.next_trading_day(dt.date(2026, 7, 3)) == dt.date(2026, 7, 6)


def test_last_trading_day_crosses_year_boundary():
    # Jan 1, 2026 is New Year's Day -> last session is Wed Dec 31, 2025 (2025 is tabled).
    assert mc.last_trading_day(dt.date(2026, 1, 1)) == dt.date(2025, 12, 31)


def test_early_closes_2026():
    assert mc.is_early_close(dt.date(2026, 11, 27)) is True   # day after Thanksgiving
    assert mc.is_trading_day(dt.date(2026, 11, 27)) is True   # still a (short) session
    assert mc.is_early_close(dt.date(2026, 12, 24)) is True   # Christmas Eve
    assert mc.is_early_close(dt.date(2026, 11, 26)) is False  # Thanksgiving itself (closed)


def test_2027_sunday_and_saturday_observances():
    assert mc.is_holiday(dt.date(2027, 7, 5)) is True   # Jul 4 Sun -> observed Mon Jul 5
    assert mc.is_holiday(dt.date(2027, 12, 24)) is True # Dec 25 Sat -> observed Fri Dec 24
    assert mc.is_holiday(dt.date(2027, 6, 18)) is True  # Jun 19 Sat -> observed Fri Jun 18


def test_each_verified_year_has_ten_full_closures():
    # Sanity: 2026 and 2027 have exactly the 10 standard federal-market holidays.
    for yr in (2026, 2027):
        n = sum(1 for d in mc._HOLIDAYS[yr])
        assert n == 10, f"{yr} should have 10 full closures, found {n}"


def test_unknown_year_fails_loud():
    with pytest.raises(mc.CalendarYearMissing):
        mc.is_trading_day(dt.date(2030, 1, 1))
    with pytest.raises(mc.CalendarYearMissing):
        mc.is_holiday(dt.date(2024, 12, 25))
