"""Offline tests for the S8 live-pilot storage foundation (Phase 0).

Pure, no IBKR, no network. Every test monkeypatches ``S8_PILOT_ROOT`` to a pytest
``tmp_path`` so the real C:\\TradingDesk-Local\\s8_pilot tree is NEVER touched.
"""

from __future__ import annotations

import pandas as pd
import pytest

import s8_store
from s8_schema import (
    MARKET_COLUMNS,
    TICK_COLUMNS,
    EntryInfo,
    ExitInfo,
    LegGrab,
    Provenance,
    TradeRecord,
    make_trade_id,
)


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path, monkeypatch):
    """Point the store at a throwaway root for every test."""
    monkeypatch.setenv("S8_PILOT_ROOT", str(tmp_path))
    assert s8_store.get_root() == tmp_path
    return tmp_path


# --------------------------------------------------------------------------- #
# make_trade_id
# --------------------------------------------------------------------------- #

def test_make_trade_id_stable_and_unique():
    a = make_trade_id("20260717", "Puts-80-$4", "12:35", 7480, 7445)
    assert a == "20260717:Puts-80-$4:12:35:7480:7445"
    # Stable: same inputs -> same id.
    assert a == make_trade_id("20260717", "Puts-80-$4", "12:35", 7480, 7445)
    # Whole-number floats render without a trailing .0 (stable key).
    assert make_trade_id("20260717", "Puts-80-$4", "12:35", 7480.0, 7445.0) == a
    # Unique: any distinguishing field changes the id.
    b = make_trade_id("20260717", "Puts-80-$4", "12:35", 7480, 7440)
    c = make_trade_id("20260717", "Calls-80-$4", "12:35", 7480, 7445)
    d = make_trade_id("20260718", "Puts-80-$4", "12:35", 7480, 7445)
    assert len({a, b, c, d}) == 4


# --------------------------------------------------------------------------- #
# Schema round-trips
# --------------------------------------------------------------------------- #

def _sample_leg(strike, right="P", complete=True):
    return LegGrab(
        right=right, strike=strike, bid=4.0, ask=4.1, last=4.05,
        bid_size=10, ask_size=12, volume=100, open_interest=500,
        delta=-0.18, gamma=0.01, vega=0.5, theta=-0.9, iv=0.22,
        underlying_spot=7500.0, grab_ts="2026-07-17T12:35:00-05:00", complete=complete,
    )


def _entry_only_record(trade_id):
    return TradeRecord(
        trade_id=trade_id,
        date="20260717",
        account="U14438624",
        template="Puts-80-$4",
        slot="12:35",
        side="PUT",
        expiration="20260717",
        qty=1,
        status="open",
        entry=EntryInfo(
            entry_ts="2026-07-17T12:35:00-05:00",
            entry_spot=7500.0, entry_vix=14.2, entry_realized_vol=0.11,
            short_strike=7480, long_strike=7445, width=35,
            realized_credit=4.05, stop_multiple=2.0, stop_price=8.10,
            short_leg=_sample_leg(7480), long_leg=_sample_leg(7445),
            greeks_complete=True,
        ),
        exit=None,
        provenance=Provenance(paperbot_version="0.16.0", pilot_mode=True),
    )


def test_traderecord_roundtrip_exit_none():
    rec = _entry_only_record("t-open")
    back = TradeRecord.from_dict(rec.to_dict())
    assert back == rec
    assert back.exit is None
    assert back.entry.short_leg == rec.entry.short_leg
    assert isinstance(back.entry.short_leg, LegGrab)


def test_traderecord_roundtrip_with_exit_and_none_exit_legs():
    rec = _entry_only_record("t-closed")
    rec.status = "closed"
    rec.exit = ExitInfo(
        exit_ts="2026-07-17T14:05:00-05:00",
        exit_reason="stop_hit",
        exit_spot=7460.0,
        short_leg_exit=_sample_leg(7480, complete=False),
        long_leg_exit=None,  # exercise the None-leg branch
        spread_value_at_exit=8.10,
        pnl=-4.05,
        max_adverse_excursion=-4.05,
        duration_secs=5400,
    )
    back = TradeRecord.from_dict(rec.to_dict())
    assert back == rec
    assert back.exit.exit_reason == "stop_hit"
    assert back.exit.long_leg_exit is None
    assert isinstance(back.exit.short_leg_exit, LegGrab)
    assert back.exit.short_leg_exit.complete is False


# --------------------------------------------------------------------------- #
# Trade records — append + latest-wins
# --------------------------------------------------------------------------- #

def test_upsert_append_and_latest_wins():
    other = _entry_only_record("t-other")
    s8_store.upsert_trade_record(other)

    entry = _entry_only_record("t-main")
    s8_store.upsert_trade_record(entry)

    # Now write the same trade_id again, with an exit filled in.
    closed = _entry_only_record("t-main")
    closed.status = "closed"
    closed.exit = ExitInfo(exit_ts="2026-07-17T14:05:00-05:00", exit_reason="eod", pnl=1.2)
    s8_store.upsert_trade_record(closed)

    # File is append-only: three physical lines.
    with open(s8_store._trades_file(), "r", encoding="utf-8") as fh:
        assert sum(1 for line in fh if line.strip()) == 3

    recs = {r.trade_id: r for r in s8_store.read_trade_records()}
    assert set(recs) == {"t-other", "t-main"}
    # Latest-wins: t-main is the closed/exit version.
    assert recs["t-main"].status == "closed"
    assert recs["t-main"].exit is not None
    assert recs["t-main"].exit.exit_reason == "eod"
    # The other trade is untouched.
    assert recs["t-other"].status == "open"
    assert recs["t-other"].exit is None


def test_read_trade_records_missing_file_is_empty():
    assert s8_store.read_trade_records() == []


# --------------------------------------------------------------------------- #
# Parquet round-trips
# --------------------------------------------------------------------------- #

def _tick_frame():
    row = {c: None for c in TICK_COLUMNS}
    row.update(
        trade_id="t-main", ts="2026-07-17T12:35:01-05:00", leg="short", right="P",
        strike=7480.0, bid=4.0, ask=4.1, last=4.05, bid_size=10, ask_size=12,
        volume=100, open_interest=500, delta=-0.18, gamma=0.01, vega=0.5,
        theta=-0.9, iv=0.22, underlying_spot=7500.0,
    )
    return pd.DataFrame([row], columns=TICK_COLUMNS)


def _market_frame():
    row = {c: None for c in MARKET_COLUMNS}
    row.update(
        ts="2026-07-17T12:35:01-05:00", expiration="20260717", strike=7480.0,
        right="P", bid=4.0, ask=4.1, last=4.05, bid_size=10, ask_size=12,
        volume=100, open_interest=500, delta=-0.18, gamma=0.01, vega=0.5,
        theta=-0.9, iv=0.22, underlying_spot=7500.0, vix=14.2,
    )
    return pd.DataFrame([row], columns=MARKET_COLUMNS)


def test_write_ticks_partition_and_roundtrip(_isolated_root):
    out = s8_store.write_ticks(_tick_frame(), "20260717")
    assert out.exists()
    assert out.parent.name == "date=20260717"
    assert (_isolated_root / "ticks" / "date=20260717") == out.parent
    back = pd.read_parquet(out)
    assert list(back.columns) == TICK_COLUMNS
    assert back.iloc[0]["trade_id"] == "t-main"
    assert back.iloc[0]["leg"] == "short"


def test_write_market_partition_and_roundtrip(_isolated_root):
    out = s8_store.write_market(_market_frame(), "20260717")
    assert out.exists()
    assert out.parent.name == "date=20260717"
    back = pd.read_parquet(out)
    assert list(back.columns) == MARKET_COLUMNS
    assert back.iloc[0]["vix"] == 14.2


def test_write_ticks_appends_new_part_each_call():
    a = s8_store.write_ticks(_tick_frame(), "20260717")
    b = s8_store.write_ticks(_tick_frame(), "20260717")
    assert a != b  # unique part name, no overwrite
    assert a.parent == b.parent


# --------------------------------------------------------------------------- #
# Open-position state — atomic round-trip
# --------------------------------------------------------------------------- #

def test_open_state_missing_reads_empty():
    assert s8_store.read_open_state() == {}


def test_open_state_atomic_roundtrip_and_replace():
    s8_store.write_open_state({"t-main": {"status": "open", "qty": 1}})
    assert s8_store.read_open_state() == {"t-main": {"status": "open", "qty": 1}}

    # A second write cleanly replaces the file (no leftover temp files).
    s8_store.write_open_state({"t-other": {"status": "open"}})
    assert s8_store.read_open_state() == {"t-other": {"status": "open"}}
    leftovers = list(s8_store._state_dir().glob(".*tmp"))
    assert leftovers == []


def test_open_state_corrupted_reads_empty():
    s8_store._state_dir().mkdir(parents=True, exist_ok=True)
    with open(s8_store._state_file(), "w", encoding="utf-8") as fh:
        fh.write("{not valid json")
    assert s8_store.read_open_state() == {}


# --------------------------------------------------------------------------- #
# DuckDB catalog
# --------------------------------------------------------------------------- #

def test_init_catalog_empty_store():
    import duckdb

    catalog = s8_store.init_catalog()
    assert catalog.exists()
    con = duckdb.connect(str(catalog))
    try:
        for view in ("trades", "ticks", "market"):
            # Views exist and are queryable (zero rows on an empty store).
            assert con.execute(f"SELECT count(*) FROM {view}").fetchone()[0] == 0
    finally:
        con.close()


def test_init_catalog_populated_store():
    import duckdb

    s8_store.upsert_trade_record(_entry_only_record("t-main"))
    s8_store.write_ticks(_tick_frame(), "20260717")
    s8_store.write_market(_market_frame(), "20260717")

    catalog = s8_store.init_catalog()
    con = duckdb.connect(str(catalog))
    try:
        assert con.execute("SELECT count(*) FROM trades").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM ticks").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM market").fetchone()[0] == 1
        # Hive partition column surfaces on the parquet views (DuckDB infers int).
        assert str(con.execute("SELECT date FROM ticks LIMIT 1").fetchone()[0]) == "20260717"
    finally:
        con.close()
