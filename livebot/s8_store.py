"""S8 live-pilot data-capture — storage layer (Phase 0).

Off-Drive, crash-safe persistence for the zero-transmit S8 live pilot. Storage root
is ``S8_PILOT_ROOT`` (env, for tests) or the local default below — NEVER a My Drive
path (Drive corrupts market data; that is the whole reason for the code/data split).

Layout (mirrors the datacollector warehouse convention: parquet + DuckDB catalog):

    <ROOT>/
      trades/trades.jsonl                append-only trade records (latest-wins on read)
      ticks/date=YYYYMMDD/part-*.parquet per-trade full-tick leg time-series
      market/date=YYYYMMDD/part-*.parquet intraday market-context
      state/open_positions.json          live open-position state (atomic write)
      logs/
      catalog.duckdb                     queryable views over the above

All writers ensure their own dirs exist. Paths are derived from ROOT only — never
from cwd.
"""

from __future__ import annotations

import glob
import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from s8_schema import TradeRecord

# NEVER a "My Drive" path. Off-Drive local default; overridable for tests via env.
DEFAULT_ROOT = r"C:\TradingDesk-Local\s8_pilot"


# --------------------------------------------------------------------------- #
# Path helpers (env re-read on every call so tests can monkeypatch S8_PILOT_ROOT)
# --------------------------------------------------------------------------- #

def get_root() -> Path:
    """Storage root: ``$S8_PILOT_ROOT`` if set, else the off-Drive local default."""
    return Path(os.environ.get("S8_PILOT_ROOT", DEFAULT_ROOT))


# Convenience module attribute (import-time snapshot). Internal code uses get_root().
ROOT = get_root()


def _trades_dir() -> Path:
    return get_root() / "trades"


def _trades_file() -> Path:
    return _trades_dir() / "trades.jsonl"


def _ticks_dir() -> Path:
    return get_root() / "ticks"


def _market_dir() -> Path:
    return get_root() / "market"


def _state_dir() -> Path:
    return get_root() / "state"


def _state_file() -> Path:
    return _state_dir() / "open_positions.json"


def _logs_dir() -> Path:
    return get_root() / "logs"


def _catalog_path() -> Path:
    return get_root() / "catalog.duckdb"


def _fmt_date(date: Any) -> str:
    """Normalize a partition date to ``YYYYMMDD`` (accepts str or date/datetime)."""
    if hasattr(date, "strftime"):
        return date.strftime("%Y%m%d")
    s = str(date).replace("-", "")
    return s


def ensure_dirs() -> None:
    """Create the full store skeleton. Safe to call repeatedly."""
    for d in (_trades_dir(), _ticks_dir(), _market_dir(), _state_dir(), _logs_dir()):
        d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Trade records — append-only JSONL, latest-wins on read
# --------------------------------------------------------------------------- #

def upsert_trade_record(rec: TradeRecord) -> None:
    """Append one trade record as a JSON line (append-only, crash-safe).

    "Upsert" is realized on *read* (see read_trade_records): a later line with the
    same ``trade_id`` supersedes an earlier one, so writing an exit-filled record
    overwrites the entry-only version without ever mutating existing bytes.
    """
    d = _trades_dir()
    d.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec.to_dict(), separators=(",", ":"), default=str)
    with open(_trades_file(), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_trade_records() -> List[TradeRecord]:
    """Return trade records with latest-wins by ``trade_id`` (last line per id wins).

    Insertion order is preserved by first-seen id. A missing file reads as empty.
    """
    path = _trades_file()
    if not path.exists():
        return []
    latest: "Dict[str, Dict[str, Any]]" = {}
    order: List[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            tid = obj["trade_id"]
            if tid not in latest:
                order.append(tid)
            latest[tid] = obj
    return [TradeRecord.from_dict(latest[tid]) for tid in order]


# --------------------------------------------------------------------------- #
# Tick / market time-series — date-partitioned parquet
# --------------------------------------------------------------------------- #

def _write_partition(df, base: Path, date: Any) -> Path:
    import pandas as pd  # noqa: F401  (validates dependency; df is a DataFrame)

    part_dir = base / f"date={_fmt_date(date)}"
    part_dir.mkdir(parents=True, exist_ok=True)
    out = part_dir / f"part-{uuid.uuid4().hex}.parquet"
    df.to_parquet(out, engine="pyarrow", index=False)
    return out


def write_ticks(df, date: Any) -> Path:
    """Append a full-tick leg frame as a parquet part under ticks/date=YYYYMMDD/."""
    return _write_partition(df, _ticks_dir(), date)


def write_market(df, date: Any) -> Path:
    """Append a market-context frame as a parquet part under market/date=YYYYMMDD/."""
    return _write_partition(df, _market_dir(), date)


# --------------------------------------------------------------------------- #
# Open-position state — atomic JSON (crash-safe)
# --------------------------------------------------------------------------- #

def read_open_state() -> Dict[str, Any]:
    """Return the open-position state dict. Missing or corrupted file -> {}."""
    path = _state_file()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, ValueError, OSError):
        # A crash mid-write (or otherwise garbled file) must not take the service
        # down — treat an unreadable state file as "no known open positions".
        return {}


def write_open_state(state: Dict[str, Any]) -> None:
    """Atomically persist the open-position state (temp file + os.replace)."""
    d = _state_dir()
    d.mkdir(parents=True, exist_ok=True)
    final = _state_file()
    tmp = final.with_name(f".{final.name}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, separators=(",", ":"), default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)  # atomic on Windows and POSIX


# --------------------------------------------------------------------------- #
# DuckDB catalog — views only, tolerant of empty/missing partitions
# --------------------------------------------------------------------------- #

def init_catalog() -> Path:
    """Create/refresh DuckDB views over the trades jsonl + ticks/market parquet.

    Views only. Tolerates an empty or missing store: if a source has no files yet,
    an empty stub view is created so downstream queries resolve without error.
    """
    import duckdb

    ensure_dirs()
    catalog = _catalog_path()
    con = duckdb.connect(str(catalog))
    try:
        _create_jsonl_view(con, "trades", _trades_file())
        _create_parquet_view(con, "ticks", _ticks_dir())
        _create_parquet_view(con, "market", _market_dir())
    finally:
        con.close()
    return catalog


def _sql_str(p: Path) -> str:
    """Forward-slash, single-quote-escaped path literal for embedding in SQL."""
    return str(p).replace("\\", "/").replace("'", "''")


def _create_jsonl_view(con, name: str, path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT * FROM read_json_auto('{_sql_str(path)}', "
            f"format='newline_delimited', union_by_name=true)"
        )
    else:
        _create_stub_view(con, name)


def _create_parquet_view(con, name: str, base: Path) -> None:
    pattern = str(base / "date=*" / "*.parquet")
    if glob.glob(pattern):
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT * FROM read_parquet('{_sql_str(Path(pattern))}', "
            f"hive_partitioning=true, union_by_name=true)"
        )
    else:
        _create_stub_view(con, name)


def _create_stub_view(con, name: str) -> None:
    # Empty placeholder so the view exists and queries return zero rows cleanly.
    con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT NULL AS placeholder WHERE 1=0")
