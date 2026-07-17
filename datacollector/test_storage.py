"""test_storage.py — regression tests for storage.py's incremental rebuild_catalog().

CENTERPIECE (2026-07-10): rebuild_catalog() used to re-scan pyarrow metadata for
EVERY warehouse parquet AND re-embed every non-empty path as one giant literal
DuckDB list in a single CREATE VIEW on every call. Against the real ~312k-file
warehouse a production run was killed after 52 minutes with no completion (see
storage.py's "Incremental catalog rebuild" module comment for the full diagnosis).
The fix chunks the view into small, mostly-frozen per-chunk views (CATALOG_CHUNK_SIZE
each) UNION ALL BY NAME'd together, and only reclassifies/rebuilds files and chunks
that are new or whose on-disk mtime changed since the last call.

These tests use tiny synthetic parquet files (a handful of rows, plus a genuine
zero-column "no-data-day" marker) in temp directories so they run in well under a
second — they are NOT a substitute for the real-warehouse proof run, just a fast
regression guard for the incremental bookkeeping (new files, rewrites-in-place,
empty<->nonempty flips, chunk sealing) that's cheap to run on every `pytest -q`.

Run from datacollector/:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest test_storage.py -q
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import config
import storage


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    """Point config.RAW_OPTIONS / CATALOG_DB at a scratch temp dir for this test."""
    raw = tmp_path / "raw" / "options"
    raw.mkdir(parents=True)
    catalog_db = tmp_path / "catalog.duckdb"
    monkeypatch.setattr(config, "RAW_OPTIONS", raw)
    monkeypatch.setattr(config, "MANIFEST", raw / "_manifest.json")
    monkeypatch.setattr(config, "CATALOG_DB", catalog_db)
    monkeypatch.setattr(storage, "CATALOG_CHUNK_SIZE", 2)  # force multi-chunk behavior
    return raw, catalog_db


def _write_nonempty(raw, symbol: str, daystr: str, price: float = 1.0) -> None:
    d = raw / symbol
    d.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({"date": [daystr], "symbol": [symbol], "close": [price]})
    df.to_parquet(d / f"{daystr}.parquet", engine="pyarrow")


def _write_empty_marker(raw, symbol: str, daystr: str) -> None:
    """A genuine zero-column marker, matching download.py's pd.DataFrame() no-data
    write (NOT just a zero-row frame with columns — a truly columnless table)."""
    d = raw / symbol
    d.mkdir(parents=True, exist_ok=True)
    table = pa.table({})
    pq.write_table(table, d / f"{daystr}.parquet")


def _query_all(catalog_db) -> pd.DataFrame:
    import duckdb
    con = duckdb.connect(str(catalog_db))
    try:
        return con.execute("SELECT * FROM options_eod ORDER BY date, symbol").fetchdf()
    finally:
        con.close()


def test_empty_warehouse_yields_empty_queryable_view(warehouse):
    raw, catalog_db = warehouse
    storage.rebuild_catalog()
    import duckdb
    con = duckdb.connect(str(catalog_db))
    try:
        df = con.execute("SELECT * FROM options_eod").fetchdf()
    finally:
        con.close()
    assert len(df) == 0


def test_zero_column_marker_excluded_from_view(warehouse):
    raw, catalog_db = warehouse
    _write_nonempty(raw, "SPY", "20260101")
    _write_empty_marker(raw, "SPY", "20260102")   # market holiday marker
    storage.rebuild_catalog()
    df = _query_all(catalog_db)
    assert len(df) == 1
    assert df.iloc[0]["date"] == "20260101"


def test_incremental_add_does_not_lose_prior_rows(warehouse):
    raw, catalog_db = warehouse
    _write_nonempty(raw, "SPY", "20260101")
    storage.rebuild_catalog()
    assert len(_query_all(catalog_db)) == 1

    _write_nonempty(raw, "SPY", "20260102")
    storage.rebuild_catalog()
    df = _query_all(catalog_db)
    assert len(df) == 2
    assert set(df["date"]) == {"20260101", "20260102"}


def test_chunk_sealing_spans_multiple_chunks(warehouse):
    """CATALOG_CHUNK_SIZE is forced to 2 by the fixture — 5 files must span 3
    chunks and all 5 rows must still be visible through the union view."""
    raw, catalog_db = warehouse
    for i in range(5):
        _write_nonempty(raw, "SPY", f"2026010{i+1}")
    storage.rebuild_catalog()
    df = _query_all(catalog_db)
    assert len(df) == 5

    import duckdb
    con = duckdb.connect(str(catalog_db))
    try:
        chunk_ids = [r[0] for r in con.execute(
            "SELECT DISTINCT chunk_id FROM _catalog_manifest WHERE nonempty").fetchall()]
    finally:
        con.close()
    assert len(chunk_ids) >= 3   # 5 files / chunk_size=2 -> at least 3 chunks


def test_rewrite_in_place_is_reflected_without_manual_chunk_rebuild(warehouse):
    """A file that changes CONTENT after being cataloged (e.g. a repull script's
    overwrite) must show the new content on the next query. Since views are not
    materialized, this must be true even without a chunk-list change — but a
    rebuild_catalog() call must still succeed and not misbehave when the mtime
    changes."""
    raw, catalog_db = warehouse
    _write_nonempty(raw, "SPY", "20260101", price=1.0)
    storage.rebuild_catalog()
    df = _query_all(catalog_db)
    assert df.iloc[0]["close"] == 1.0

    _write_nonempty(raw, "SPY", "20260101", price=2.0)   # in-place repull-style rewrite
    storage.rebuild_catalog()
    df = _query_all(catalog_db)
    assert len(df) == 1
    assert df.iloc[0]["close"] == 2.0


def test_flip_nonempty_to_empty_removes_from_view(warehouse):
    """A file that flips from real data back to a zero-column marker (not part of
    any normal flow today, but must not silently corrupt its whole chunk) must be
    excluded from the view after the next rebuild, and the chunk must remain
    queryable (not break) for its other members."""
    raw, catalog_db = warehouse
    _write_nonempty(raw, "SPY", "20260101")
    _write_nonempty(raw, "QQQ", "20260101")
    storage.rebuild_catalog()
    assert len(_query_all(catalog_db)) == 2

    _write_empty_marker(raw, "SPY", "20260101")   # overwrite with a marker
    storage.rebuild_catalog()
    df = _query_all(catalog_db)
    assert len(df) == 1
    assert df.iloc[0]["symbol"] == "QQQ"


def test_out_of_order_backfill_of_a_past_date(warehouse):
    """A backfill writing an OLDER date after newer dates already exist (exactly
    the shape of a real have_day()-driven backfill for a missed day) must still
    show up — membership is keyed off "not yet seen", never a date cutoff."""
    raw, catalog_db = warehouse
    _write_nonempty(raw, "SPY", "20260110")
    storage.rebuild_catalog()

    _write_nonempty(raw, "SPY", "20260105")   # backfilled after the fact, older date
    storage.rebuild_catalog()
    df = _query_all(catalog_db)
    assert set(df["date"]) == {"20260105", "20260110"}


def test_second_rebuild_with_nothing_changed_is_a_noop(warehouse):
    raw, catalog_db = warehouse
    _write_nonempty(raw, "SPY", "20260101")
    storage.rebuild_catalog()
    df1 = _query_all(catalog_db)

    storage.rebuild_catalog()   # nothing changed on disk
    df2 = _query_all(catalog_db)
    assert df1.equals(df2)


def test_base_param_writes_to_isolated_namespace(warehouse, tmp_path):
    """The optional base= redirect (used by ibkr_forward_live for the parallel
    raw/options_ibkr namespace during the ThetaData A/B window) must land the
    parquet + its OWN _manifest.json under the alt base, be visible to have_day/
    partition_path when base= is passed, and be INVISIBLE to the default namespace
    — the two namespaces are fully isolated."""
    raw, _ = warehouse
    alt = tmp_path / "raw" / "options_ibkr"
    df = pd.DataFrame({"date": ["20260101"], "symbol": ["SPX"], "close": [1.23]})

    n = storage.write_day("SPX", "20260101", df, base=alt)
    assert n == 1

    # Landed under the alt base, not the default namespace.
    assert (alt / "SPX" / "20260101.parquet").exists()
    assert not (raw / "SPX" / "20260101.parquet").exists()

    # partition_path / have_day honor base=.
    assert storage.partition_path("SPX", "20260101", base=alt) == alt / "SPX" / "20260101.parquet"
    assert storage.have_day("SPX", "20260101", base=alt) is True

    # Default namespace does NOT see it (isolation).
    assert storage.have_day("SPX", "20260101") is False

    # The alt base got its OWN manifest, recording the row count.
    alt_manifest = alt / "_manifest.json"
    assert alt_manifest.exists()
    import json
    assert json.loads(alt_manifest.read_text())["SPX"]["20260101"] == 1
    # ...and the default manifest was untouched (still absent for this scratch warehouse).
    assert not config.MANIFEST.exists()


def test_default_write_day_behavior_unchanged(warehouse):
    """Regression guard: write_day with NO base= must still write to the default
    RAW_OPTIONS namespace + config.MANIFEST exactly as before."""
    raw, _ = warehouse
    df = pd.DataFrame({"date": ["20260101"], "symbol": ["SPY"], "close": [9.9]})
    storage.write_day("SPY", "20260101", df)
    assert (raw / "SPY" / "20260101.parquet").exists()
    assert storage.have_day("SPY", "20260101") is True
    import json
    assert json.loads(config.MANIFEST.read_text())["SPY"]["20260101"] == 1
