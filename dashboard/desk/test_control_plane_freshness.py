"""Tests for the Control Plane reviewed-preview freshness/expiry decision (pure)."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import page_control_plane as cp  # noqa: E402

_NOW = datetime(2026, 7, 31, 12, 0, 0)


def test_fresh_preview_is_fresh():
    age, fresh = cp._freshness_of(_NOW - timedelta(seconds=10), _NOW)
    assert fresh and 9 < age < 11


def test_exactly_at_window_is_fresh():
    age, fresh = cp._freshness_of(_NOW - timedelta(seconds=cp.PREVIEW_FRESHNESS_SECS), _NOW)
    assert fresh


def test_one_second_past_window_is_stale():
    age, fresh = cp._freshness_of(
        _NOW - timedelta(seconds=cp.PREVIEW_FRESHNESS_SECS + 1), _NOW)
    assert not fresh and age > cp.PREVIEW_FRESHNESS_SECS


def test_none_built_at_not_fresh():
    age, fresh = cp._freshness_of(None, _NOW)
    assert age is None and not fresh


def test_future_built_at_treated_fresh():
    age, fresh = cp._freshness_of(_NOW + timedelta(seconds=5), _NOW)
    assert fresh
