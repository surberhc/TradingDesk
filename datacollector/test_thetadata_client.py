"""test_thetadata_client.py — regression tests for thetadata_client.connected().

CENTERPIECE: 2026-07-09, eod_daily.py aborted an entire day's EOD grab because its
single 5s connected() check happened to land while the Terminal was momentarily busy
serving the concurrent UniverseDownloadEod backfill (4 parallel shards, ~1 week job) —
the Terminal was fine, just slow to answer that one probe. The fix adds an OPT-IN,
BOUNDED retry (retries/backoff_s params) so eod_daily.py can tolerate one transient
busy moment without giving up on a whole day.

Explicitly NOT wanted (and pinned here): a continuous/background poll of the Terminal
while the backfill runs. These tests confirm the retry is small and bounded (at most
`retries` attempts, at most `retries - 1` sleeps) and that every OTHER existing caller
(who calls connected() with no args) is completely unaffected — still one attempt,
no sleep, immediate False on failure.

Run from datacollector/:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest test_thetadata_client.py -q
"""

from __future__ import annotations

import requests

import thetadata_client as td


def test_default_args_single_attempt_no_sleep_on_failure(monkeypatch):
    """Default connected() (used by every other caller) must be UNCHANGED: one
    attempt, no sleep, return False immediately when the GET raises."""
    calls = {"get": 0}

    def fake_get(*args, **kwargs):
        calls["get"] += 1
        raise requests.RequestException("boom")

    sleeps = []
    monkeypatch.setattr(td.requests, "get", fake_get)
    monkeypatch.setattr(td.time, "sleep", lambda s: sleeps.append(s))

    assert td.connected() is False
    assert calls["get"] == 1
    assert sleeps == []


def test_default_args_single_attempt_success(monkeypatch):
    """Default connected() still returns True immediately on a normal success."""
    def fake_get(*args, **kwargs):
        class _Resp:
            pass
        return _Resp()

    sleeps = []
    monkeypatch.setattr(td.requests, "get", fake_get)
    monkeypatch.setattr(td.time, "sleep", lambda s: sleeps.append(s))

    assert td.connected() is True
    assert sleeps == []


def test_retries_succeeds_on_second_attempt_with_one_sleep(monkeypatch):
    """connected(retries=3, ...): 1st attempt fails, 2nd succeeds -> True, and exactly
    one sleep happened (bounded backoff, not a continuous loop)."""
    calls = {"get": 0}

    def fake_get(*args, **kwargs):
        calls["get"] += 1
        if calls["get"] == 1:
            raise requests.RequestException("busy")
        class _Resp:
            pass
        return _Resp()

    sleeps = []
    monkeypatch.setattr(td.requests, "get", fake_get)
    monkeypatch.setattr(td.time, "sleep", lambda s: sleeps.append(s))

    assert td.connected(retries=3, backoff_s=5.0) is True
    assert calls["get"] == 2
    assert sleeps == [5.0]


def test_retries_all_fail_returns_false_bounded_sleeps(monkeypatch):
    """connected(retries=3, ...): all 3 attempts fail -> False, and it slept at most
    retries-1 times (the key regression guard against unbounded/continuous polling)."""
    calls = {"get": 0}

    def fake_get(*args, **kwargs):
        calls["get"] += 1
        raise requests.RequestException("still busy")

    sleeps = []
    monkeypatch.setattr(td.requests, "get", fake_get)
    monkeypatch.setattr(td.time, "sleep", lambda s: sleeps.append(s))

    assert td.connected(retries=3, backoff_s=5.0) is False
    assert calls["get"] == 3
    assert len(sleeps) == 2  # retries - 1, never more
    assert sleeps == [5.0, 5.0]
