"""
test_nav_history.py — proves nav_history.py's append/load contract offline: file
creation with correct header/row, same-day overwrite (not duplicate), multiple
accounts on the same day both persist, and a graceful empty-shape cold start when
the CSV doesn't exist. No broker, no real STATE_DIR — NAV_HISTORY_CSV is
monkeypatched to a tmp_path file per test, same isolation pattern as
test_gateway_lock.py's tmp_path-based lock file.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_nav_history.py -v
"""
from __future__ import annotations

from datetime import date

import pandas as pd

import nav_history as nh


def _csv_path(tmp_path):
    return tmp_path / "nav_history.csv"


def test_append_creates_file_with_header_and_row(tmp_path, monkeypatch):
    p = _csv_path(tmp_path)
    monkeypatch.setattr(nh, "NAV_HISTORY_CSV", p)

    today = date(2026, 7, 7)
    snapshots = [{"account": "DU8922142", "net_liq": 1_100_000.0}]
    nh.append_snapshot(today, snapshots)

    assert p.exists()
    df = pd.read_csv(p)
    assert list(df.columns) == nh.COLUMNS
    assert len(df) == 1
    row = df.iloc[0]
    assert row["date"] == "2026-07-07"
    assert row["account"] == "DU8922142"
    assert row["version"] == "Conservative"
    assert row["net_liq"] == 1_100_000.0


def test_append_same_day_overwrites_not_duplicates(tmp_path, monkeypatch):
    p = _csv_path(tmp_path)
    monkeypatch.setattr(nh, "NAV_HISTORY_CSV", p)

    today = date(2026, 7, 7)
    nh.append_snapshot(today, [{"account": "DU8922142", "net_liq": 1_100_000.0}])
    # Re-run same day (e.g. manual re-run of the monitor) with an updated NetLiq.
    nh.append_snapshot(today, [{"account": "DU8922142", "net_liq": 1_105_000.0}])

    df = nh.load_history()
    rows = df[(df["date"] == "2026-07-07") & (df["account"] == "DU8922142")]
    assert len(rows) == 1
    assert rows.iloc[0]["net_liq"] == 1_105_000.0


def test_append_different_accounts_same_day_both_persist(tmp_path, monkeypatch):
    p = _csv_path(tmp_path)
    monkeypatch.setattr(nh, "NAV_HISTORY_CSV", p)

    today = date(2026, 7, 7)
    nh.append_snapshot(today, [
        {"account": "DU8922142", "net_liq": 1_100_000.0},
        {"account": "DU8922143", "net_liq": 1_100_500.0},
    ])

    df = nh.load_history()
    assert len(df) == 2
    accts = set(df["account"])
    assert accts == {"DU8922142", "DU8922143"}
    ver = dict(zip(df["account"], df["version"]))
    assert ver["DU8922142"] == "Conservative"
    assert ver["DU8922143"] == "Balanced"


def test_append_across_multiple_days_keeps_history(tmp_path, monkeypatch):
    p = _csv_path(tmp_path)
    monkeypatch.setattr(nh, "NAV_HISTORY_CSV", p)

    nh.append_snapshot(date(2026, 7, 7), [{"account": "DU8922142", "net_liq": 1_100_000.0}])
    nh.append_snapshot(date(2026, 7, 8), [{"account": "DU8922142", "net_liq": 1_101_000.0}])

    df = nh.load_history()
    assert len(df) == 2
    assert set(df["date"]) == {"2026-07-07", "2026-07-08"}


def test_load_history_missing_file_returns_empty_shaped_dataframe(tmp_path, monkeypatch):
    p = _csv_path(tmp_path)  # never created
    monkeypatch.setattr(nh, "NAV_HISTORY_CSV", p)

    df = nh.load_history()
    assert list(df.columns) == nh.COLUMNS
    assert len(df) == 0


def test_append_skips_snapshot_with_none_net_liq(tmp_path, monkeypatch):
    p = _csv_path(tmp_path)
    monkeypatch.setattr(nh, "NAV_HISTORY_CSV", p)

    nh.append_snapshot(date(2026, 7, 7), [{"account": "DU8922142", "net_liq": None}])

    # No usable rows -> file is never created (nothing to log).
    assert not p.exists()
    df = nh.load_history()
    assert len(df) == 0


def test_append_skips_unenrolled_account(tmp_path, monkeypatch):
    p = _csv_path(tmp_path)
    monkeypatch.setattr(nh, "NAV_HISTORY_CSV", p)

    nh.append_snapshot(date(2026, 7, 7), [{"account": "DU9999999", "net_liq": 500_000.0}])

    assert not p.exists()
