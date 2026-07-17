"""Offline tests for the S8 live-pilot reporting layer (Phase 5).

Pure, no IBKR, no network. Every test monkeypatches ``S8_PILOT_ROOT`` to a
pytest ``tmp_path`` so the real C:\\TradingDesk-Local\\s8_pilot tree is NEVER
touched. Seeds a small synthetic store via the real store writer, then asserts
the rendered report's counts, win rate, P&L math, open-trade handling, template
grouping, and the graceful empty-store path.
"""

from __future__ import annotations

import pandas as pd
import pytest

import s8_report
import s8_store
from s8_schema import (
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
# Synthetic store seeding
# --------------------------------------------------------------------------- #


def _leg(strike, right="P", delta=-0.18, complete=True):
    return LegGrab(
        right=right, strike=strike, bid=4.0, ask=4.1, last=4.05,
        bid_size=10, ask_size=12, volume=100, open_interest=500,
        delta=delta, gamma=0.01, vega=0.5, theta=-0.9, iv=0.22,
        underlying_spot=7500.0, grab_ts="2026-07-17T12:35:00-05:00",
        complete=complete,
    )


def _winner():
    tid = make_trade_id("20260717", "Puts-80-$4", "12:35", 7480, 7445)
    rec = TradeRecord(
        trade_id=tid, date="20260717", account="U14438624",
        template="Puts-80-$4", slot="12:35", side="PUT",
        expiration="20260717", qty=1, status="closed",
        entry=EntryInfo(
            entry_ts="2026-07-17T12:35:00-05:00", entry_spot=7500.0,
            entry_vix=14.2, entry_realized_vol=0.11,
            short_strike=7480, long_strike=7445, width=35,
            realized_credit=4.05, stop_multiple=2.0, stop_price=8.10,
            short_leg=_leg(7480), long_leg=_leg(7445), greeks_complete=True,
        ),
        exit=ExitInfo(
            exit_ts="2026-07-17T15:55:00-05:00", exit_reason="eod",
            exit_spot=7505.0,
            short_leg_exit=_leg(7480, delta=-0.02), long_leg_exit=_leg(7445),
            spread_value_at_exit=1.05, pnl=3.00,
            max_adverse_excursion=-0.80, duration_secs=11700,
        ),
        provenance=Provenance(paperbot_version="0.16.0", pilot_mode=True),
    )
    return tid, rec


def _stopped():
    tid = make_trade_id("20260717", "Calls-80-$4", "12:40", 7560, 7595)
    rec = TradeRecord(
        trade_id=tid, date="20260717", account="U14438624",
        template="Calls-80-$4", slot="12:40", side="CALL",
        expiration="20260717", qty=1, status="closed",
        entry=EntryInfo(
            entry_ts="2026-07-17T12:40:00-05:00", entry_spot=7500.0,
            entry_vix=14.2, entry_realized_vol=0.11,
            short_strike=7560, long_strike=7595, width=35,
            realized_credit=4.00, stop_multiple=2.0, stop_price=8.00,
            short_leg=_leg(7560, right="C", delta=0.18),
            long_leg=_leg(7595, right="C"), greeks_complete=True,
        ),
        exit=ExitInfo(
            exit_ts="2026-07-17T14:05:00-05:00", exit_reason="stop_hit",
            exit_spot=7540.0,
            short_leg_exit=_leg(7560, right="C", delta=0.35, complete=False),
            long_leg_exit=None,
            spread_value_at_exit=8.00, pnl=-4.00,
            max_adverse_excursion=-4.00, duration_secs=5100,
        ),
        provenance=Provenance(paperbot_version="0.16.0", pilot_mode=True),
    )
    return tid, rec


def _open():
    tid = make_trade_id("20260717", "Puts-80-$4", "13:10", 7470, 7435)
    rec = TradeRecord(
        trade_id=tid, date="20260717", account="U14438624",
        template="Puts-80-$4", slot="13:10", side="PUT",
        expiration="20260717", qty=1, status="open",
        entry=EntryInfo(
            entry_ts="2026-07-17T13:10:00-05:00", entry_spot=7498.0,
            entry_vix=14.5, entry_realized_vol=0.12,
            short_strike=7470, long_strike=7435, width=35,
            realized_credit=3.90, stop_multiple=2.0, stop_price=7.80,
            short_leg=_leg(7470), long_leg=_leg(7435), greeks_complete=True,
        ),
        exit=None,
        provenance=Provenance(paperbot_version="0.16.0", pilot_mode=True),
    )
    return tid, rec


def _seed_store():
    """Write two closed (one win, one stop-out) + one open trade, plus ticks."""
    win_id, win = _winner()
    stop_id, stop = _stopped()
    open_id, opn = _open()
    for rec in (win, stop, opn):
        s8_store.upsert_trade_record(rec)

    # A few tick rows on the winner (optional coverage layer).
    rows = []
    for i in range(3):
        row = {c: None for c in TICK_COLUMNS}
        row.update(
            trade_id=win_id, ts=f"2026-07-17T12:3{5 + i}:00-05:00", leg="short",
            right="P", strike=7480.0, bid=4.0, ask=4.1, last=4.05,
            delta=-0.18, gamma=0.01, vega=0.5, theta=-0.9, iv=0.22,
            underlying_spot=7500.0,
        )
        rows.append(row)
    s8_store.write_ticks(pd.DataFrame(rows, columns=TICK_COLUMNS), "20260717")
    return {"win": win_id, "stop": stop_id, "open": open_id}


# --------------------------------------------------------------------------- #
# Empty store — graceful path
# --------------------------------------------------------------------------- #


def test_empty_store_graceful_message():
    report = s8_report.render_report()
    assert s8_report.NO_TRADES_MSG in report
    # Header still renders; no exception, no crash.
    assert "S8 Live-Pilot Report" in report


def test_empty_store_with_date_filter_graceful():
    report = s8_report.render_report(date="20260717")
    assert s8_report.NO_TRADES_MSG in report


# --------------------------------------------------------------------------- #
# Aggregate correctness
# --------------------------------------------------------------------------- #


def test_aggregates_counts_and_pnl_math():
    _seed_store()
    records = s8_store.read_trade_records()
    agg = s8_report.compute_aggregates(records)

    assert agg["total"] == 3
    assert agg["closed_count"] == 2
    assert agg["open_count"] == 1

    # Only closed trades count toward P&L: winner +3.00, stop -4.00.
    assert agg["pnl_count"] == 2
    assert agg["win_count"] == 1
    assert agg["win_rate"] == pytest.approx(0.5)
    # Hand-verified: 3.00 + (-4.00) = -1.00 total; avg -0.50.
    assert agg["total_pnl"] == pytest.approx(-1.00)
    assert agg["avg_pnl"] == pytest.approx(-0.50)
    # MAE avg: (-0.80 + -4.00) / 2 = -2.40.
    assert agg["avg_mae"] == pytest.approx(-2.40)
    # Duration avg: (11700 + 5100) / 2 = 8400 secs.
    assert agg["avg_duration_secs"] == pytest.approx(8400.0)


def test_aggregates_grouping_by_template_and_side():
    _seed_store()
    records = s8_store.read_trade_records()
    agg = s8_report.compute_aggregates(records)
    # Two Puts (winner + open), one Calls (stop).
    assert agg["by_template"] == {"Puts-80-$4": 2, "Calls-80-$4": 1}
    assert agg["by_side"] == {"PUT": 2, "CALL": 1}


def test_open_trade_not_counted_as_win_or_loss():
    _seed_store()
    records = s8_store.read_trade_records()
    ids = _seed_ids = {r.trade_id: r for r in records}
    open_rec = ids[make_trade_id("20260717", "Puts-80-$4", "13:10", 7470, 7435)]
    assert s8_report.is_open(open_rec) is True
    agg = s8_report.compute_aggregates(records)
    # The open trade is excluded from the 2-trade P&L population.
    assert agg["pnl_count"] == 2


# --------------------------------------------------------------------------- #
# Rendered report content
# --------------------------------------------------------------------------- #


def test_report_marks_open_trade_and_groups():
    _seed_store()
    report = s8_report.render_report()

    assert "Trades: 3" in report
    assert "Win rate: 50.0%" in report
    assert "Total P&L: -1.00" in report
    assert "Still open: 1" in report

    # Per-template grouping present.
    assert "Puts-80-$4 2" in report
    assert "Calls-80-$4 1" in report

    # The open trade is clearly flagged and shows no result line.
    assert "[OPEN ]" in report
    assert "still open" in report
    # Closed trades show their exit reasons.
    assert "reason eod" in report
    assert "reason stop_hit" in report
    # Greeks at entry vs exit render for the short leg.
    assert report.count("short-leg d") >= 4  # entry + exit lines per closed trade


def test_tick_coverage_surfaces_in_report():
    ids = _seed_store()
    report = s8_report.render_report()
    # Winner had 3 tick rows seeded.
    assert "ticks captured: 3" in report


# --------------------------------------------------------------------------- #
# Date selection
# --------------------------------------------------------------------------- #


def test_date_filter_selects_only_matching_day():
    _seed_store()
    # Add a trade on a different day.
    other = _open()[1]
    other.trade_id = make_trade_id("20260718", "Puts-80-$4", "12:35", 7400, 7365)
    other.date = "20260718"
    s8_store.upsert_trade_record(other)

    records = s8_store.read_trade_records()
    only_17 = s8_report.select_records(records, date="20260717")
    assert len(only_17) == 3
    assert all(r.date == "20260717" for r in only_17)

    # Dashed form normalizes the same way.
    assert len(s8_report.select_records(records, date="2026-07-17")) == 3


def test_date_range_inclusive():
    _seed_store()
    other = _open()[1]
    other.trade_id = make_trade_id("20260720", "Puts-80-$4", "12:35", 7400, 7365)
    other.date = "20260720"
    s8_store.upsert_trade_record(other)

    records = s8_store.read_trade_records()
    both = s8_report.select_records(records, date_from="20260717", date_to="20260720")
    assert len(both) == 4
    narrow = s8_report.select_records(records, date_from="20260718", date_to="20260719")
    assert narrow == []


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #


def test_cli_main_empty_store(capsys):
    rc = s8_report.main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert s8_report.NO_TRADES_MSG in captured.out


def test_cli_main_seeded(capsys):
    _seed_store()
    rc = s8_report.main(["--date", "20260717"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Trades: 3" in captured.out
    assert "Total P&L: -1.00" in captured.out
