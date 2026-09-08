"""Tests for the GEX incremental build's FRESHNESS VERDICT (conductor #81, 2026-09-08).

The bug being guarded: build_features.py --latest is an INCREMENTAL build. When its
upstream (the nightly option-chain pull) stops landing data it does no work, raises
nothing, and reported "ok". gex.json therefore read status "ok" with newest_date
20260901 for four trading days while the feed underneath it was dead, and the heartbeat
alarm — which only treated "fail" as outstanding — stayed quiet.

A build that succeeds at doing nothing is not a healthy build.
"""
import datetime

import build_features as bf


class _Now:
    """Stand-in for datetime.datetime.now() pinned to a chosen moment."""

    def __init__(self, d: datetime.date):
        self._d = d

    def date(self) -> datetime.date:
        return self._d


# Tue 2026-09-08 is an ordinary trading day; Sat 2026-09-05 is a weekend.
TRADING_DAY = _Now(datetime.date(2026, 9, 8))
WEEKEND = _Now(datetime.date(2026, 9, 5))


def test_current_data_scores_ok():
    st, msg = bf._score_freshness({"newest_date": "20260908", "rows": 8620}, now=TRADING_DAY)
    assert st == "ok"
    assert "ok" in msg


def test_the_real_incident_scores_stale():
    """The exact shape observed: build fine, data four trading days behind."""
    st, msg = bf._score_freshness({"newest_date": "20260901", "rows": 8620}, now=TRADING_DAY)
    assert st == "stale", "a build with nothing new to add must not report ok"
    assert "20260901" in msg and "20260908" in msg
    # The message must point at the upstream, not at this build — that is where to look.
    assert "UPSTREAM" in msg


def test_no_data_at_all_is_a_failure():
    st, _ = bf._score_freshness({"newest_date": None, "rows": 0}, now=TRADING_DAY)
    assert st == "fail"


def test_weekend_expects_the_last_trading_day_not_today():
    """Saturday must not be scored stale for lacking Saturday data."""
    st, _ = bf._score_freshness({"newest_date": "20260904", "rows": 10}, now=WEEKEND)
    assert st == "ok"


def test_unknown_calendar_never_manufactures_a_false_alarm(monkeypatch):
    monkeypatch.setattr(bf, "_expected_newest_date", lambda *a, **k: None)
    st, msg = bf._score_freshness({"newest_date": "20200101", "rows": 5})
    assert st == "ok"
    assert "freshness unchecked" in msg
