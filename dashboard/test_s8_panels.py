"""Offline tests for the S8 dashboard tab's PURE helpers (dashboard/app.py).

These exercise the store-record selection and the distance-to-stop math WITHOUT a
Streamlit runtime, without IBKR, and without touching the real capture store (the temp
store test points S8_PILOT_ROOT at a throwaway dir). Importing app.py self-bootstraps
sys.path (paperbot/backtester/connections/strategies/dailyreport/livebot) and runs a few
Streamlit calls in "bare mode" (harmless warnings) — nothing connects to any Gateway.

Run from dashboard/:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest -q
"""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402
import app  # noqa: E402  (import triggers the sys.path bootstrap the helpers rely on)
import s8_monitor_core  # noqa: E402
import s8_schema  # noqa: E402
import s8_store  # noqa: E402


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _open_rec(trade_id="t1", date="20260720", template="Puts-80-$4", slot="08:43",
              side="PUT", short=7495.0, long=7470.0, credit=4.0, stop=7.3, qty=1):
    return s8_schema.TradeRecord(
        trade_id=trade_id, date=date, template=template, slot=slot, side=side,
        expiration=date, qty=qty, status="open",
        entry=s8_schema.EntryInfo(
            short_strike=short, long_strike=long, width=abs(short - long),
            realized_credit=credit, stop_price=stop, entry_spot=7508.0, entry_vix=17.6,
            short_leg=s8_schema.LegGrab(right="P", strike=short, delta=-0.35, gamma=0.01,
                                        theta=-8.4, iv=0.17),
            long_leg=s8_schema.LegGrab(right="P", strike=long),
        ),
    )


def _closed_rec(trade_id="c1", date="20260720", pnl=-20.0, mae=-25.0, reason="stop_hit",
                dur=60.0, exit_spot=7509.0):
    r = _open_rec(trade_id=trade_id, date=date)
    r.status = "closed"
    r.exit = s8_schema.ExitInfo(
        exit_reason=reason, exit_spot=exit_spot, pnl=pnl, max_adverse_excursion=mae,
        duration_secs=dur,
        short_leg_exit=s8_schema.LegGrab(right="P", strike=7495.0, delta=-0.42),
    )
    return r


# --------------------------------------------------------------------------- #
# distance-to-stop (the load-bearing pure computation)
# --------------------------------------------------------------------------- #

def test_distance_open_not_stopped():
    rec = _open_rec(credit=4.0, stop=7.3)
    d = app.s8_distance_to_stop(rec, short_ask=5.0, long_bid=1.0)
    assert d["spread_cost"] == 4.0
    assert d["stop_price"] == 7.3
    assert abs(d["distance_to_stop"] - 3.3) < 1e-9
    assert d["running_pnl"] == 0.0          # (4.0 - 4.0) * 100 * 1
    assert d["stopped"] is False


def test_distance_stopped_when_cost_reaches_stop():
    rec = _open_rec(credit=4.0, stop=7.3, qty=1)
    d = app.s8_distance_to_stop(rec, short_ask=9.0, long_bid=1.5)  # cost 7.5 >= 7.3
    assert d["spread_cost"] == 7.5
    assert d["distance_to_stop"] < 0
    assert d["running_pnl"] == -350.0       # (4.0 - 7.5) * 100
    assert d["stopped"] is True


def test_distance_missing_quote_is_none_safe():
    rec = _open_rec()
    d = app.s8_distance_to_stop(rec, short_ask=None, long_bid=1.0)
    assert d["spread_cost"] is None
    assert d["distance_to_stop"] is None
    assert d["running_pnl"] is None
    assert d["stopped"] is False            # unknown cost never fires a stop


def test_distance_respects_qty():
    rec = _open_rec(credit=4.0, stop=7.3, qty=3)
    d = app.s8_distance_to_stop(rec, short_ask=9.0, long_bid=1.5)
    assert d["running_pnl"] == -1050.0      # -3.5 * 100 * 3


def test_distance_matches_monitor_core_directly():
    """The tab must never drift from the frozen monitor-core stop/P&L math."""
    rec = _open_rec(credit=4.15, stop=7.4, qty=1)
    short_ask, long_bid = 6.2, 1.1
    d = app.s8_distance_to_stop(rec, short_ask, long_bid)
    pos = s8_monitor_core.MonitorPosition(
        trade_id=rec.trade_id, side=rec.side, short_strike=rec.entry.short_strike,
        long_strike=rec.entry.long_strike, qty=1,
        realized_credit=rec.entry.realized_credit, stop_price=rec.entry.stop_price)
    sample = s8_monitor_core.Sample(short_ask=short_ask, long_bid=long_bid)
    assert d["spread_cost"] == s8_monitor_core.spread_close_value(sample)
    assert d["running_pnl"] == s8_monitor_core.pnl_at(pos, sample)


# --------------------------------------------------------------------------- #
# store-record selection / formatting helpers
# --------------------------------------------------------------------------- #

def test_available_dates_sorted_desc():
    recs = [_open_rec(trade_id="a", date="20260717"),
            _open_rec(trade_id="b", date="20260720"),
            _open_rec(trade_id="c", date="20260718")]
    assert app._s8_available_dates(recs) == ["20260720", "20260718", "20260717"]


def test_records_for_date_filters():
    recs = [_open_rec(trade_id="a", date="20260717"),
            _open_rec(trade_id="b", date="20260720")]
    got = app._s8_records_for_date(recs, "20260720")
    assert [r.trade_id for r in got] == ["b"]
    # None date -> all records
    assert len(app._s8_records_for_date(recs, None)) == 2


# --------------------------------------------------------------------------- #
# recorded-tick overlay: latest-tick-per-leg selection + freshness (PURE)
# --------------------------------------------------------------------------- #

def _tick_rows(rows):
    """Build a TICK_COLUMNS-shaped DataFrame from partial (trade_id/ts/leg/...) dicts —
    missing tick columns default to None, mirroring a real capture row."""
    base = {c: None for c in s8_schema.TICK_COLUMNS}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_latest_tick_per_leg_picks_newest_by_ts():
    df = _tick_rows([
        {"trade_id": "t1", "ts": "2026-07-20T14:00:00.000-05:00", "leg": "short",
         "bid": 3.0, "ask": 3.2, "delta": -0.30, "iv": 0.17},
        {"trade_id": "t1", "ts": "2026-07-20T14:00:05.000-05:00", "leg": "short",
         "bid": 3.4, "ask": 3.5, "delta": -0.31, "iv": 0.18},   # newest short
        {"trade_id": "t1", "ts": "2026-07-20T14:00:03.000-05:00", "leg": "long",
         "bid": 0.9, "ask": 1.0},
    ])
    latest = app._s8_latest_tick_per_leg(df)
    assert latest["t1"]["short"]["ask"] == 3.5      # from the newest short row
    assert latest["t1"]["short"]["bid"] == 3.4
    assert latest["t1"]["short"]["delta"] == -0.31
    assert latest["t1"]["long"]["bid"] == 0.9
    assert latest["t1"]["short"]["ts"].endswith("14:00:05.000-05:00")


def test_latest_tick_per_leg_empty_and_none():
    assert app._s8_latest_tick_per_leg(pd.DataFrame()) == {}
    assert app._s8_latest_tick_per_leg(None) == {}


def test_latest_tick_nan_coerced_to_none():
    df = _tick_rows([
        {"trade_id": "t1", "ts": "2026-07-20T14:00:00.000-05:00", "leg": "short",
         "bid": float("nan"), "ask": 3.2, "iv": float("nan")},
    ])
    q = app._s8_latest_tick_per_leg(df)["t1"]["short"]
    assert q["bid"] is None
    assert q["ask"] == 3.2
    assert q["iv"] is None


def test_overlay_distance_from_latest_recorded_tick():
    """distance-to-stop populates from the latest recorded short_ask / long_bid."""
    rec = _open_rec(credit=4.0, stop=7.3)
    df = _tick_rows([
        {"trade_id": rec.trade_id, "ts": "2026-07-20T14:00:05.000-05:00",
         "leg": "short", "bid": 4.8, "ask": 5.0},
        {"trade_id": rec.trade_id, "ts": "2026-07-20T14:00:05.000-05:00",
         "leg": "long", "bid": 1.0, "ask": 1.2},
    ])
    latest = app._s8_latest_tick_per_leg(df)[rec.trade_id]
    d = app.s8_distance_to_stop(rec, latest["short"]["ask"], latest["long"]["bid"])
    assert d["spread_cost"] == 4.0                  # 5.0 - 1.0
    assert abs(d["distance_to_stop"] - 3.3) < 1e-9  # 7.3 - 4.0
    assert d["running_pnl"] == 0.0                  # (4.0 - 4.0) * 100
    assert d["stopped"] is False


def test_tick_age_and_stale_threshold():
    from datetime import datetime as dt, timezone, timedelta
    ct = timezone(timedelta(hours=-5))
    now = dt(2026, 7, 20, 14, 0, 40, tzinfo=ct)
    fresh = app._s8_tick_age_secs("2026-07-20T14:00:00-05:00", now=now)
    assert abs(fresh - 40.0) < 1e-6
    assert fresh <= app.S8_TICK_STALE_SECS          # 40s is fresh
    stale = app._s8_tick_age_secs("2026-07-20T13:00:00-05:00", now=now)
    assert stale > app.S8_TICK_STALE_SECS           # 1h old -> stale
    assert app._s8_tick_age_secs(None) is None
    assert app._s8_tick_age_secs("not-a-timestamp") is None


def test_fmt_age_labels():
    assert app._s8_fmt_age(None) == "—"
    assert app._s8_fmt_age(4) == "4s ago"
    assert app._s8_fmt_age(123) == "2m03s ago"
    assert app._s8_fmt_age(3660) == "1h01m ago"


def test_ticks_dataframe_reads_partition(tmp_path, monkeypatch):
    """End-to-end (files only, no Gateway): write a synthetic tick parquet through the
    store, then read it back via the in-memory DuckDB helper + latest-per-leg selection."""
    monkeypatch.setenv("S8_PILOT_ROOT", str(tmp_path))
    s8_store.write_ticks(_tick_rows([
        {"trade_id": "o1", "ts": "2026-07-20T14:00:00.000-05:00", "leg": "short",
         "bid": 3.0, "ask": 3.5},
        {"trade_id": "o1", "ts": "2026-07-20T14:00:00.000-05:00", "leg": "long",
         "bid": 1.0, "ask": 1.2},
        {"trade_id": "o2", "ts": "2026-07-20T14:00:00.000-05:00", "leg": "short",
         "bid": 2.0, "ask": 2.4},
    ]), "20260720")
    got = app._s8_ticks_dataframe("20260720", ["o1"])
    latest = app._s8_latest_tick_per_leg(got)
    assert set(latest) == {"o1"}                    # filtered to requested trade_ids
    assert latest["o1"]["short"]["ask"] == 3.5
    assert latest["o1"]["long"]["bid"] == 1.0
    # a date with no partition -> empty frame -> empty selection
    assert len(app._s8_ticks_dataframe("20250101", ["o1"])) == 0
    assert app._s8_latest_tick_per_leg(app._s8_ticks_dataframe("20250101")) == {}


# --------------------------------------------------------------------------- #
# temp-store read (synthetic trades.jsonl; never touches the real store)
# --------------------------------------------------------------------------- #

def test_store_roundtrip_and_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("S8_PILOT_ROOT", str(tmp_path))
    s8_store.upsert_trade_record(_open_rec(trade_id="o1", date="20260720"))
    s8_store.upsert_trade_record(_closed_rec(trade_id="c1", date="20260720"))
    s8_store.upsert_trade_record(_open_rec(trade_id="o2", date="20260717"))

    recs = s8_store.read_trade_records()
    assert {r.trade_id for r in recs} == {"o1", "c1", "o2"}

    today = app._s8_records_for_date(recs, "20260720")
    assert {r.trade_id for r in today} == {"o1", "c1"}
    opens = [r for r in today if app.s8_report.is_open(r)]
    closed = [r for r in today if not app.s8_report.is_open(r)]
    assert [r.trade_id for r in opens] == ["o1"]
    assert [r.trade_id for r in closed] == ["c1"]


def test_compute_aggregates_over_closed():
    recs = [_closed_rec(trade_id="c1", pnl=10.0, mae=-5.0),
            _closed_rec(trade_id="c2", pnl=-20.0, mae=-25.0),
            _open_rec(trade_id="o1")]
    agg = app.s8_report.compute_aggregates(recs)
    assert agg["closed_count"] == 2
    assert agg["open_count"] == 1
    assert agg["win_count"] == 1            # only c1 positive
    assert abs(agg["win_rate"] - 0.5) < 1e-9
    assert agg["total_pnl"] == -10.0


# --------------------------------------------------------------------------- #
# offline render smoke (bare-mode Streamlit; no Gateway, no crash)
# --------------------------------------------------------------------------- #

def test_render_helpers_do_not_crash_offline():
    opens = [_open_rec(trade_id="o1"), _open_rec(trade_id="o2", side="CALL")]
    closed = [_closed_rec(trade_id="c1")]
    app._render_s8_open_positions(opens)
    app._render_s8_open_positions([])                 # empty path
    # No session_date -> no tick read attempted; overlay fail-softs to "no tick"/"—".
    app._render_s8_live_monitor(opens, is_today=True)         # today, no ticks -> "—"
    app._render_s8_live_monitor(opens, is_today=False)        # past session, no overlay
    app._render_s8_live_monitor([], is_today=True)            # empty path
    app.render_s8_closed(opens + closed, "20260720")
    app.render_s8_closed([], "20260720")              # empty path
