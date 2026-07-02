"""
build_features.py — turn raw EOD chains into a daily GEX feature table per root.

Reads every raw/options/{SYMBOL}/{YYYYMMDD}.parquet, computes one row of GEX
features per day (features/gex.py), and writes a small daily table to
derived/{SYMBOL}_gex_daily.parquet. These derived tables are tiny (one row/day)
and ARE safe to copy back to Drive for backup.

Usage:
    python build_features.py SPX          # one root, FULL rebuild
    python build_features.py              # every root, FULL rebuild (HEAVY, ~30min+)
    python build_features.py --latest     # FAST incremental: append only NEW days for the
                                          #   report symbols (SPX SPXW SPY QQQ) — for nightly cron
    python build_features.py --latest all # incremental for every root that has raw data
    python build_features.py --latest SPX QQQ   # incremental for a chosen subset

Incremental (--latest) reads the existing derived/{SYMBOL}_gex_daily.parquet, finds the
latest date already in it, and processes ONLY the raw daily files newer than that — then
appends and rewrites the (still tiny) derived table. It never recomputes history, so it is
seconds-fast per symbol. If no derived table exists yet for a symbol, it falls back to a
full build_symbol() for that one symbol. Full-rebuild behavior is unchanged.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

import config
import storage
from features import gex

# --------------------------------------------------------------------------- #
# Python-side log — run_gex.bat runs windowless (pythonw, no stdout capture), so
# a small self-contained log keeps gex failures debuggable. Mirrors the _log()
# helper in dailyreport/eod_report.py. Fully exception-wrapped; never raises.
# --------------------------------------------------------------------------- #
_GEX_LOG = Path(r"C:\TradingDesk-Local\warehouse\gex.log")


def _log(msg: str) -> None:
    try:
        line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
        _GEX_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_GEX_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# --------------------------------------------------------------------------- #
# Status artifact — same import trick heartbeat_alarm uses for the mailer: put
# the sibling dailyreport package dir on sys.path and import its `status` module.
# Every status.write here is wrapped so it can NEVER break the GEX build.
# --------------------------------------------------------------------------- #
_DAILYREPORT_DIR = config.CODE_ROOT.parent / "dailyreport"
if str(_DAILYREPORT_DIR) not in sys.path:
    sys.path.insert(0, str(_DAILYREPORT_DIR))
try:
    import status as _status
except Exception:  # never let a missing import break the build
    _status = None


def _write_gex_status(st: str, metrics: dict | None = None, message: str = "") -> None:
    """Write the 'gex' status JSON so the EOD digest + heartbeat_alarm can see it.
    Never raises into the caller."""
    if _status is None:
        return
    try:
        import datetime as _dt
        _status.write("gex", st, metrics=metrics or {}, message=message,
                      day=_dt.datetime.now().strftime("%Y%m%d"))
    except Exception:
        pass


def _gex_table_summary() -> dict:
    """Newest date + row count across the report symbols' derived tables, for the
    status metrics. Best-effort; returns {} on any trouble."""
    newest = None
    rows = 0
    try:
        for sym in REPORT_SYMBOLS:
            p = config.DERIVED / f"{sym}_gex_daily.parquet"
            if not p.exists():
                continue
            df = pd.read_parquet(p, columns=["date"])
            if df.empty:
                continue
            rows += len(df)
            d = str(df["date"].astype(str).max())
            if newest is None or d > newest:
                newest = d
    except Exception:
        pass
    return {"newest_date": newest, "rows": rows}

# Symbols the EOD report's Dealer Gamma section actually reads (SPX->SPXW->SPY),
# plus QQQ. The nightly incremental defaults to these so the report is always fresh.
REPORT_SYMBOLS = ["SPX", "SPXW", "SPY", "QQQ"]


def _day_rows(raw_dir, only_after: str | None = None) -> list[dict]:
    """GEX feature rows for each raw daily parquet in raw_dir.

    If only_after (a 'YYYYMMDD' date string) is given, only files whose stem
    (the date) is strictly greater than it are processed — the incremental path.
    """
    rows = []
    for p in sorted(raw_dir.glob("*.parquet")):
        if only_after is not None and p.stem <= only_after:
            continue
        chain = pd.read_parquet(p)
        if chain.empty:
            continue
        f = gex.day_features(chain)
        if f:
            rows.append(f)
    return rows


def build_symbol(symbol: str) -> int:
    """FULL rebuild: recompute every day's GEX from scratch and overwrite the table."""
    raw_dir = config.RAW_OPTIONS / symbol
    if not raw_dir.exists():
        print(f"  {symbol}: no raw data yet")
        return 0
    rows = _day_rows(raw_dir)
    if not rows:
        print(f"  {symbol}: no usable days")
        return 0
    out = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    config.DERIVED.mkdir(parents=True, exist_ok=True)
    out.to_parquet(config.DERIVED / f"{symbol}_gex_daily.parquet", index=False)
    print(f"  {symbol}: {len(out)} days -> derived/{symbol}_gex_daily.parquet")
    return len(out)


def update_symbol(symbol: str) -> int:
    """INCREMENTAL: append only days newer than what's already in the derived table.

    Returns the number of NEW days appended. Falls back to a full build if no
    derived table exists yet (first run for a symbol).
    """
    raw_dir = config.RAW_OPTIONS / symbol
    if not raw_dir.exists():
        print(f"  {symbol}: no raw data yet")
        return 0
    out_path = config.DERIVED / f"{symbol}_gex_daily.parquet"
    if not out_path.exists():
        print(f"  {symbol}: no derived table yet -> full build")
        return build_symbol(symbol)
    existing = pd.read_parquet(out_path)
    # 'date' is stored as a 'YYYYMMDD' string (str(chain['date'].iloc[0])); compare as str.
    last_date = str(existing["date"].astype(str).max()) if not existing.empty else None
    new_rows = _day_rows(raw_dir, only_after=last_date)
    if not new_rows:
        print(f"  {symbol}: up to date (latest {last_date})")
        return 0
    out = (pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True)
           .drop_duplicates(subset="date", keep="last")
           .sort_values("date")
           .reset_index(drop=True))
    config.DERIVED.mkdir(parents=True, exist_ok=True)
    out.to_parquet(out_path, index=False)
    added = [r["date"] for r in new_rows]
    print(f"  {symbol}: +{len(new_rows)} day(s) {added} -> {len(out)} total")
    return len(new_rows)


def main(roots: list[str]) -> None:
    """FULL rebuild for the given roots (or every root if empty)."""
    if not roots:
        roots = sorted(p.name for p in config.RAW_OPTIONS.glob("*") if p.is_dir())
    for symbol in roots:
        build_symbol(symbol)


def main_incremental(roots: list[str]) -> None:
    """FAST incremental update for the given roots.

    Empty -> the report symbols. ['ALL'] -> every root that has raw data.
    """
    if not roots:
        roots = REPORT_SYMBOLS
    elif roots == ["ALL"]:
        roots = sorted(p.name for p in config.RAW_OPTIONS.glob("*") if p.is_dir())
    print("build_features --latest (incremental):")
    for symbol in roots:
        update_symbol(symbol)


if __name__ == "__main__":
    args = [a.upper() for a in sys.argv[1:]]
    if args and args[0] == "--LATEST":
        # The nightly incremental path (run by run_gex.bat). Write a 'gex' status
        # artifact so the EOD digest + per-job alarm can see whether it ran/failed.
        _log("incremental build start")
        try:
            main_incremental(args[1:])
            summary = _gex_table_summary()
            _write_gex_status("ok", metrics=summary,
                              message=f"incremental build ok "
                                      f"(newest {summary.get('newest_date')}, "
                                      f"{summary.get('rows')} rows)")
            _log(f"incremental build done "
                 f"(newest {summary.get('newest_date')}, {summary.get('rows')} rows)")
        except Exception as e:
            _write_gex_status("fail", message=f"{type(e).__name__}: {e}")
            _log(f"incremental build FAILED: {type(e).__name__}: {e}")
            raise
    else:
        main(args)
