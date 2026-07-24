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


# --- buying_power / excess_liquidity columns (conductor #26) ------------------------
def test_bp_xl_flow_through_into_csv(tmp_path, monkeypatch):
    p = _csv_path(tmp_path)
    monkeypatch.setattr(nh, "NAV_HISTORY_CSV", p)

    nh.append_snapshot(date(2026, 7, 7), [{
        "account": "DU8922142", "net_liq": 1_100_000.0,
        "buying_power": 3_800_000.0, "excess_liquidity": 900_000.0}])

    df = nh.load_history()
    assert list(df.columns) == nh.COLUMNS
    row = df.iloc[0]
    assert row["buying_power"] == 3_800_000.0
    assert row["excess_liquidity"] == 900_000.0


def test_missing_bp_xl_become_nan(tmp_path, monkeypatch):
    p = _csv_path(tmp_path)
    monkeypatch.setattr(nh, "NAV_HISTORY_CSV", p)

    # net_liq present but no BP/XL keys -> columns exist, values NaN (row still written).
    nh.append_snapshot(date(2026, 7, 7), [{"account": "DU8922142", "net_liq": 1_100_000.0}])

    df = nh.load_history()
    assert list(df.columns) == nh.COLUMNS
    assert len(df) == 1
    assert pd.isna(df.iloc[0]["buying_power"])
    assert pd.isna(df.iloc[0]["excess_liquidity"])
    assert df.iloc[0]["net_liq"] == 1_100_000.0


def test_preexisting_csv_without_new_columns_upserts(tmp_path, monkeypatch):
    p = _csv_path(tmp_path)
    monkeypatch.setattr(nh, "NAV_HISTORY_CSV", p)

    # Simulate a legacy CSV written BEFORE the two new columns existed (old 4-col shape).
    legacy = pd.DataFrame([{"date": "2026-07-06", "account": "DU8922142",
                            "version": "Conservative", "net_liq": 1_000_000.0}])
    legacy.to_csv(p, index=False)

    # A new day's snapshot with BP/XL must upsert cleanly against the legacy file.
    nh.append_snapshot(date(2026, 7, 7), [{
        "account": "DU8922142", "net_liq": 1_100_000.0,
        "buying_power": 3_800_000.0, "excess_liquidity": 900_000.0}])

    df = nh.load_history()
    assert list(df.columns) == nh.COLUMNS
    assert len(df) == 2
    old = df[df["date"] == "2026-07-06"].iloc[0]
    assert pd.isna(old["buying_power"])          # legacy row backfilled NaN
    new = df[df["date"] == "2026-07-07"].iloc[0]
    assert new["buying_power"] == 3_800_000.0


def test_load_history_reindexes_legacy_csv_to_full_shape(tmp_path, monkeypatch):
    p = _csv_path(tmp_path)
    monkeypatch.setattr(nh, "NAV_HISTORY_CSV", p)

    legacy = pd.DataFrame([{"date": "2026-07-06", "account": "DU8922142",
                            "version": "Conservative", "net_liq": 1_000_000.0}])
    legacy.to_csv(p, index=False)

    df = nh.load_history()
    assert list(df.columns) == nh.COLUMNS        # full shape even though CSV had 4 cols
    assert pd.isna(df.iloc[0]["buying_power"])
    assert pd.isna(df.iloc[0]["excess_liquidity"])
