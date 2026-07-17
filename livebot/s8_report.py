"""S8 live-pilot data-capture — reporting layer (Phase 5).

Read-only analysis over the captured store. Turns the durable trade records
(``trades/trades.jsonl``, read via :func:`s8_store.read_trade_records`) — and,
where useful, the per-trade tick parquet — into a readable Markdown report:
per-trade lines (strikes, credit, greeks at entry vs exit, spot move, exit
reason, P&L, max adverse excursion, duration) plus aggregates (counts by
template/side, win rate, avg/total P&L, avg MAE, avg duration, still-open count).

Non-negotiable: this NEVER writes to the store. It reads the JSONL through the
store API and, for optional tick coverage, builds read-only DuckDB views in an
**in-memory** connection — the on-disk ``catalog.duckdb`` is never touched. A
report must not mutate data.

CLI:
    python s8_report.py                       # every captured trade
    python s8_report.py --date 20260717       # one session
    python s8_report.py --from 20260717 --to 20260731   # inclusive range
"""

from __future__ import annotations

import argparse
import glob
from typing import Dict, List, Optional

import s8_store
from s8_schema import TradeRecord

# --------------------------------------------------------------------------- #
# Record selection
# --------------------------------------------------------------------------- #


def _norm_date(value: Optional[str]) -> Optional[str]:
    """Normalize a YYYYMMDD / YYYY-MM-DD string to bare ``YYYYMMDD`` (or None)."""
    if value is None:
        return None
    return str(value).replace("-", "").strip()


def select_records(
    records: List[TradeRecord],
    date: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[TradeRecord]:
    """Filter records by a single ``date`` or an inclusive ``[from, to]`` range.

    Dates are compared as bare ``YYYYMMDD`` strings, which sort chronologically.
    ``date`` takes precedence over the range if both are supplied. A record with
    no ``date`` is kept only when no date filter is active.
    """
    d = _norm_date(date)
    lo = _norm_date(date_from)
    hi = _norm_date(date_to)

    if d is None and lo is None and hi is None:
        return list(records)

    out: List[TradeRecord] = []
    for rec in records:
        rd = _norm_date(rec.date)
        if rd is None:
            continue
        if d is not None:
            if rd == d:
                out.append(rec)
            continue
        if lo is not None and rd < lo:
            continue
        if hi is not None and rd > hi:
            continue
        out.append(rec)
    return out


def is_open(rec: TradeRecord) -> bool:
    """A trade is still open if it is not closed or has no exit info yet."""
    return rec.status != "closed" or rec.exit is None


# --------------------------------------------------------------------------- #
# Optional tick coverage (read-only DuckDB, in-memory connection only)
# --------------------------------------------------------------------------- #


def tick_counts() -> Dict[str, int]:
    """Return {trade_id: tick-row count} from the ticks parquet, or {} if none.

    Builds an ephemeral in-memory DuckDB view over ``ticks/date=*/**.parquet``.
    Never opens or writes the on-disk catalog. Any failure (missing duckdb,
    unreadable partitions) degrades to an empty mapping — coverage is a nicety,
    not load-bearing.
    """
    ticks_dir = s8_store.get_root() / "ticks"
    pattern = str(ticks_dir / "date=*" / "*.parquet")
    if not glob.glob(pattern):
        return {}
    try:
        import duckdb

        con = duckdb.connect(":memory:")
        try:
            glob_sql = pattern.replace("\\", "/").replace("'", "''")
            con.execute(
                "CREATE VIEW rpt_ticks AS "
                f"SELECT * FROM read_parquet('{glob_sql}', "
                "hive_partitioning=true, union_by_name=true)"
            )
            rows = con.execute(
                "SELECT trade_id, count(*) FROM rpt_ticks GROUP BY trade_id"
            ).fetchall()
            return {str(tid): int(n) for tid, n in rows}
        finally:
            con.close()
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #


def _fmt_num(value, places: int = 2, dash: str = "-") -> str:
    if value is None:
        return dash
    try:
        return f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_strike(value) -> str:
    if value is None:
        return "-"
    try:
        f = float(value)
        return str(int(f)) if f.is_integer() else str(f)
    except (TypeError, ValueError):
        return str(value)


def _fmt_duration(secs) -> str:
    if secs is None:
        return "-"
    try:
        total = int(round(float(secs)))
    except (TypeError, ValueError):
        return str(secs)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fmt_signed(value, places: int = 2) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):+.{places}f}"
    except (TypeError, ValueError):
        return str(value)


def _greek_str(leg) -> str:
    """Compact d/g/v/t/iv string for one leg grab (dashes for missing/None)."""
    if leg is None:
        return "d - g - v - t - iv -"
    return (
        f"d {_fmt_num(leg.delta)} g {_fmt_num(leg.gamma)} "
        f"v {_fmt_num(leg.vega)} t {_fmt_num(leg.theta)} iv {_fmt_num(leg.iv)}"
    )


# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #


def compute_aggregates(records: List[TradeRecord]) -> Dict[str, object]:
    """Summary statistics over a selection of trade records.

    Win rate, avg P&L, total P&L, avg MAE and avg duration are computed over
    CLOSED trades only (an open trade has no realized P&L and is neither a win
    nor a loss). ``by_template`` / ``by_side`` count all selected trades.
    """
    total = len(records)
    open_recs = [r for r in records if is_open(r)]
    closed_recs = [r for r in records if not is_open(r)]

    by_template: Dict[str, int] = {}
    by_side: Dict[str, int] = {}
    for r in records:
        by_template[r.template or "?"] = by_template.get(r.template or "?", 0) + 1
        by_side[r.side or "?"] = by_side.get(r.side or "?", 0) + 1

    pnls = [r.exit.pnl for r in closed_recs if r.exit and r.exit.pnl is not None]
    maes = [
        r.exit.max_adverse_excursion
        for r in closed_recs
        if r.exit and r.exit.max_adverse_excursion is not None
    ]
    durs = [
        r.exit.duration_secs
        for r in closed_recs
        if r.exit and r.exit.duration_secs is not None
    ]
    wins = [p for p in pnls if p > 0]

    return {
        "total": total,
        "open_count": len(open_recs),
        "closed_count": len(closed_recs),
        "by_template": by_template,
        "by_side": by_side,
        "pnl_count": len(pnls),
        "win_count": len(wins),
        "win_rate": (len(wins) / len(pnls)) if pnls else None,
        "total_pnl": sum(pnls) if pnls else None,
        "avg_pnl": (sum(pnls) / len(pnls)) if pnls else None,
        "avg_mae": (sum(maes) / len(maes)) if maes else None,
        "avg_duration_secs": (sum(durs) / len(durs)) if durs else None,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

NO_TRADES_MSG = "No trades captured for the selected period."


def _render_trade_line(rec: TradeRecord, ticks: Dict[str, int]) -> List[str]:
    e = rec.entry
    x = rec.exit
    open_now = is_open(rec)
    flag = "OPEN " if open_now else "close"

    short_k = _fmt_strike(e.short_strike if e else None)
    long_k = _fmt_strike(e.long_strike if e else None)
    width = _fmt_num(e.width if e else None, 0)
    credit = _fmt_num(e.realized_credit if e else None)

    head = (
        f"- [{flag}] {rec.template or '?'} / {rec.side or '?'}  "
        f"short {short_k} / long {long_k}  width {width}  credit {credit}  "
        f"({rec.trade_id})"
    )

    entry_spot = _fmt_num(e.entry_spot if e else None)
    entry_greeks = _greek_str(e.short_leg if e else None)
    lines = [head, f"    entry: spot {entry_spot}  short-leg {entry_greeks}"]

    if open_now:
        lines.append("    exit:  -- still open --")
    else:
        exit_spot = _fmt_num(x.exit_spot if x else None)
        exit_greeks = _greek_str(x.short_leg_exit if x else None)
        lines.append(f"    exit:  spot {exit_spot}  short-leg {exit_greeks}")
        lines.append(
            f"    result: reason {x.exit_reason if x else '-'}  "
            f"pnl {_fmt_signed(x.pnl if x else None)}  "
            f"mae {_fmt_signed(x.max_adverse_excursion if x else None)}  "
            f"duration {_fmt_duration(x.duration_secs if x else None)}"
        )

    n_ticks = ticks.get(rec.trade_id)
    if n_ticks:
        lines.append(f"    ticks captured: {n_ticks}")
    return lines


def _render_scope(date, date_from, date_to) -> str:
    d = _norm_date(date)
    lo = _norm_date(date_from)
    hi = _norm_date(date_to)
    if d is not None:
        return d
    if lo is not None or hi is not None:
        return f"{lo or 'start'} .. {hi or 'end'}"
    return "all captured trades"


def render_report(
    date: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    records: Optional[List[TradeRecord]] = None,
) -> str:
    """Read the store and render the full Markdown report as a string.

    ``records`` may be supplied to render an already-loaded selection (used by
    tests); otherwise the trade records are read fresh from the store. Purely
    read-only — no store writes.
    """
    if records is None:
        records = s8_store.read_trade_records()
    selected = select_records(records, date=date, date_from=date_from, date_to=date_to)

    scope = _render_scope(date, date_from, date_to)
    out: List[str] = [
        "# S8 Live-Pilot Report",
        f"_Scope: {scope}_",
        "",
    ]

    if not selected:
        out.append(NO_TRADES_MSG)
        return "\n".join(out)

    agg = compute_aggregates(selected)
    ticks = tick_counts()

    # ---- Aggregates -------------------------------------------------------- #
    out.append("## Summary")
    out.append("")
    out.append(f"- Trades: {agg['total']}  (closed {agg['closed_count']}, open {agg['open_count']})")
    if agg["pnl_count"]:
        wr = agg["win_rate"]
        out.append(
            f"- Win rate: {wr * 100:.1f}%  "
            f"({agg['win_count']}/{agg['pnl_count']} closed with P&L)"
        )
        out.append(f"- Total P&L: {_fmt_signed(agg['total_pnl'])}")
        out.append(f"- Avg P&L: {_fmt_signed(agg['avg_pnl'])}")
    else:
        out.append("- Win rate: n/a (no closed trades with P&L)")
    out.append(f"- Avg MAE: {_fmt_signed(agg['avg_mae'])}")
    out.append(f"- Avg duration: {_fmt_duration(agg['avg_duration_secs'])}")
    out.append(f"- Still open: {agg['open_count']}")
    out.append("")
    out.append("By template: " + ", ".join(
        f"{k} {v}" for k, v in sorted(agg["by_template"].items())
    ))
    out.append("")
    out.append("By side: " + ", ".join(
        f"{k} {v}" for k, v in sorted(agg["by_side"].items())
    ))
    out.append("")

    # ---- Per-trade --------------------------------------------------------- #
    out.append("## Trades")
    out.append("")
    for rec in selected:
        out.extend(_render_trade_line(rec, ticks))
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="s8_report.py",
        description="Read-only S8 live-pilot report over the captured store.",
    )
    p.add_argument("--date", help="single session, YYYYMMDD (or YYYY-MM-DD)")
    p.add_argument("--from", dest="date_from", help="range start (inclusive), YYYYMMDD")
    p.add_argument("--to", dest="date_to", help="range end (inclusive), YYYYMMDD")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    print(render_report(
        date=args.date, date_from=args.date_from, date_to=args.date_to
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
