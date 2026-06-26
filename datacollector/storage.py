"""
storage.py — local parquet warehouse + manifest + DuckDB catalog.

Layout (all under config.DATA_ROOT, LOCAL on C:, never synced to Drive):
    raw/options/{SYMBOL}/{YYYYMMDD}.parquet   one file per root per trading day
    raw/options/_manifest.json                {SYMBOL: {YYYYMMDD: rows}}
    catalog.duckdb                            a view over all the parquet

One file per (symbol, day) because the ThetaData EOD endpoints require requesting
expiration=* a single day at a time — so the natural unit of work, of resumability,
and of the forward IBKR collector is one trading day. A present file (even 0-row,
for a market holiday) means "done — skip". Parquet is zstd-compressed and DuckDB
reads the whole tree with one glob.
"""

from __future__ import annotations

import json
import os

import pandas as pd

import config


def _manifest() -> dict:
    if not config.MANIFEST.exists():
        return {}
    try:
        return json.loads(config.MANIFEST.read_text())
    except (json.JSONDecodeError, OSError):
        # A truncated/corrupt manifest (e.g. a kill mid-write before atomic writes
        # existed) must NOT brick the grab. Treat as empty — have_day() keys off
        # file presence, not the manifest, so nothing gets needlessly re-pulled.
        return {}


def _save_manifest(m: dict) -> None:
    config.MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: write to a temp file then replace, so a kill mid-write can never
    # leave a torn manifest that would fail json.loads on every later write_day.
    tmp = config.MANIFEST.with_name(config.MANIFEST.name + ".tmp")
    tmp.write_text(json.dumps(m, indent=2, sort_keys=True))
    os.replace(tmp, config.MANIFEST)


def partition_path(symbol: str, daystr: str):
    return config.RAW_OPTIONS / symbol / f"{daystr}.parquet"


def have_day(symbol: str, daystr: str) -> bool:
    """True if this (symbol, day) is already on disk (file present = done)."""
    return partition_path(symbol, daystr).exists()


def write_day(symbol: str, daystr: str, df: pd.DataFrame) -> int:
    """Write one (symbol, day) parquet (zstd) and record the row count. 0-row OK.

    The parquet is written atomically (temp file + os.replace). The supervisor
    kills a stalled download with terminate()->kill(); without atomicity a kill
    landing mid-write would leave a torn .parquet that have_day() still counts as
    "done" — a permanent, silently-corrupt hole. The .tmp name does not match the
    have_day()/catalog globs, so an orphaned temp (kill between write and replace)
    is harmless and is overwritten when that same day is re-pulled.
    """
    p = partition_path(symbol, daystr)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    df.to_parquet(tmp, engine="pyarrow", compression="zstd", index=False)
    os.replace(tmp, p)
    m = _manifest()
    m.setdefault(symbol, {})[daystr] = int(len(df))
    _save_manifest(m)
    return len(df)


def _nonempty_parquets() -> list[str]:
    """Forward-slashed paths of every parquet that actually has columns.

    Many files are zero-column "no-data-day" markers (a day the EOD endpoint
    returned nothing). DuckDB 1.5.4's read_parquet(union_by_name=true) REFUSES to
    scan a zero-column file, so the catalog view must be built over only the files
    that carry a schema. We keep the empty markers on disk untouched — have_day()
    relies on them so the collector won't re-pull those days — we just exclude them
    from the view. Reading only the parquet footer (num_columns) is cheap.
    """
    import pyarrow.parquet as pq

    kept: list[str] = []
    for f in config.RAW_OPTIONS.glob("*/*.parquet"):
        try:
            if pq.read_metadata(f).num_columns > 0:
                kept.append(str(f).replace("\\", "/"))
        except Exception:
            # An unreadable/corrupt footer is treated as "not includable" rather
            # than blowing up the whole rebuild.
            continue
    return kept


def rebuild_catalog() -> None:
    """(Re)build a DuckDB view over the non-empty parquet for ad-hoc querying.

    Builds the view from an explicit file list (the non-empty parquets) rather
    than a glob, because a glob would sweep in the zero-column markers that break
    union_by_name. The list is embedded as a literal because a VIEW definition
    cannot reference bind parameters.
    """
    import duckdb

    files = _nonempty_parquets()
    con = duckdb.connect(str(config.CATALOG_DB))
    if not files:
        # Nothing to catalog yet — leave a queryable empty view rather than crash.
        con.execute("CREATE OR REPLACE VIEW options_eod AS SELECT NULL WHERE false")
        con.close()
        return
    # DuckDB list literal: ['a','b',...]. Paths are local, contain no quotes;
    # escape defensively anyway.
    lit = "[" + ",".join("'" + p.replace("'", "''") + "'" for p in files) + "]"
    con.execute("CREATE OR REPLACE VIEW options_eod AS "
                f"SELECT * FROM read_parquet({lit}, union_by_name=true, filename=true)")
    con.close()
