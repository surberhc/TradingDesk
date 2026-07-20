"""test_s8_monitor.py — OFFLINE tests for the S8 live exit-monitor service (Phase 2b).

100% offline: NO real broker, NO network. The crash-safe orchestration seams
(load_open_positions / on_sample / finalize_exit / close_all_eod) are exercised with
synthetic tick streams and a fake IB. Every test monkeypatches ``S8_PILOT_ROOT`` to a
pytest ``tmp_path`` so the real C:\\TradingDesk-Local\\s8_pilot tree is NEVER touched.

Run (from C:\\TradingDesk\\livebot):
    powershell -Command "$env:PYTHONPATH=''; C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s8_monitor.py -q"
"""

from __future__ import annotations

import pandas as pd
import pytest

import s8_monitor
import s8_store
from s8_monitor import S8Monitor, epoch_to_iso, iso_to_epoch
from s8_monitor_core import Sample
from s8_schema import (
    TICK_COLUMNS,
    EntryInfo,
    LegGrab,
    Provenance,
    TradeRecord,
)
from s8_strategy import stop_price as frozen_stop_price

CREDIT = 4.05
STOP_MULTIPLE = 2.0
STOP_PRICE = frozen_stop_price(CREDIT, STOP_MULTIPLE)   # 6.0 (frozen formula)
ENTRY_ISO = "2026-07-17T12:35:00.000-05:00"
ENTRY_EPOCH = iso_to_epoch(ENTRY_ISO)


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path, monkeypatch):
    """Point the store at a throwaway root for every test (real tree untouched)."""
    monkeypatch.setenv("S8_PILOT_ROOT", str(tmp_path))
    assert s8_store.get_root() == tmp_path
    return tmp_path


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _leg(strike, right="P", greeks=True):
    return LegGrab(
        right=right, strike=strike, bid=1.0, ask=1.2, last=1.1,
        bid_size=10, ask_size=12, volume=100, open_interest=500,
        delta=-0.2 if greeks else None, gamma=0.01 if greeks else None,
        vega=0.4 if greeks else None, theta=-0.8 if greeks else None,
        iv=0.22 if greeks else None, underlying_spot=7400.0,
        grab_ts=ENTRY_ISO, complete=greeks,
    )


def _open_record(trade_id, *, short=7480.0, long=7445.0, status="open"):
    return TradeRecord(
        trade_id=trade_id, date="20260717", account="U14438624",
        template="Puts-80-$4", slot="12:35", side="PUT", expiration="20260717",
        qty=1, status=status,
        entry=EntryInfo(
            entry_ts=ENTRY_ISO, entry_spot=7400.0, entry_vix=14.0,
            short_strike=short, long_strike=long, width=short - long,
            realized_credit=CREDIT, stop_multiple=STOP_MULTIPLE, stop_price=STOP_PRICE,
            short_leg=_leg(short), long_leg=_leg(long), greeks_complete=True,
        ),
        provenance=Provenance(paperbot_version="0.16.0", pilot_mode=True),
    )


def _seed(trade_id, **kw):
    rec = _open_record(trade_id, **kw)
    s8_store.upsert_trade_record(rec)
    s8_store.write_open_state({**s8_store.read_open_state(),
                               trade_id: {"status": "open"}})
    return rec


def _sample(ts_offset, short_ask, long_bid, *, short_bid=None, spot=7400.0):
    return Sample(ts=ENTRY_EPOCH + ts_offset, short_ask=short_ask,
                  short_bid=short_bid if short_bid is not None else short_ask - 0.2,
                  short_last=short_ask, long_bid=long_bid, long_ask=long_bid + 0.2,
                  long_last=long_bid, spot=spot)


def _records_by_id():
    return {r.trade_id: r for r in s8_store.read_trade_records()}


# --------------------------------------------------------------------------- #
# ts bridge
# --------------------------------------------------------------------------- #

def test_iso_epoch_roundtrip_and_duration():
    ep = iso_to_epoch(ENTRY_ISO)
    assert ep is not None
    # round-trip back to the same wall-clock instant
    assert iso_to_epoch(epoch_to_iso(ep)) == pytest.approx(ep, abs=1e-3)
    # bad inputs never raise
    assert iso_to_epoch(None) is None
    assert iso_to_epoch("not-a-date") is None
    assert epoch_to_iso(None) is None


# --------------------------------------------------------------------------- #
# stop-out end to end
# --------------------------------------------------------------------------- #

def test_stop_out_end_to_end():
    _seed("t-stop")
    mon = S8Monitor()
    assert mon.load_open_positions() == ["t-stop"]

    # NET-SPREAD stop: the spread cost to close (short_ask - long_bid) crosses the 6.0
    # stop; long_bid steady at 0.5. Full LegGrabs (greeks) attached so the persisted
    # ticks — and the exit legs — carry greeks.
    asks = [2.0, 4.0, 6.0, 6.5, 7.0]   # closes 1.5,3.5,5.5,6.0,6.5 -> first cross at index 3
    for i, a in enumerate(asks):
        s = _sample(60 * (i + 1), a, 0.5)
        mon.on_sample("t-stop", s,
                      short_leg=_leg(7480.0), long_leg=_leg(7445.0))

    rec = _records_by_id()["t-stop"]
    assert rec.status == "closed"
    assert rec.exit is not None
    assert rec.exit.exit_reason == "stop_hit"
    # crossing at short_ask=6.5, long_bid=0.5 -> close 6.0 -> pnl (4.05-6.0)*100 = -195
    assert rec.exit.pnl == pytest.approx(-195.0)
    assert rec.exit.pnl < 0
    # on_sample finalizes AT the crossing and drops the position, so the 5th (worse) tick
    # is never processed — MAE reflects life up to the exit (the crossing is the worst point).
    assert rec.exit.max_adverse_excursion == pytest.approx(-195.0)
    assert rec.exit.duration_secs == pytest.approx(4 * 60)          # crossed at 4th sample
    # exit legs carry greeks harvested from the live LegGrabs
    assert rec.exit.short_leg_exit.delta == pytest.approx(-0.2)
    assert rec.exit.short_leg_exit.complete is True
    # removed from open-state
    assert "t-stop" not in s8_store.read_open_state()
    # ticks were written
    ticks = pd.read_parquet(s8_store._ticks_dir())
    assert len(ticks) > 0
    assert set(ticks["trade_id"]) == {"t-stop"}


def test_stop_out_idempotent_no_double_exit_after_more_ticks():
    _seed("t-stop2")
    mon = S8Monitor()
    mon.load_open_positions()
    mon.on_sample("t-stop2", _sample(60, 6.5, 0.5))   # crosses -> finalized
    n_lines_before = _count_trade_lines()
    # further ticks for the same (now-closed, dropped) position are ignored
    mon.on_sample("t-stop2", _sample(120, 9.0, 0.9))
    mon.on_sample("t-stop2", _sample(180, 12.0, 1.2))
    assert _count_trade_lines() == n_lines_before
    assert _records_by_id()["t-stop2"].exit.exit_reason == "stop_hit"


# --------------------------------------------------------------------------- #
# never-hit -> close_all_eod
# --------------------------------------------------------------------------- #

def test_never_hit_then_close_all_eod():
    _seed("t-eod")
    mon = S8Monitor()
    mon.load_open_positions()
    # short_ask stays under the 6.0 stop all session; winner at the final mark.
    for i, (a, lb) in enumerate([(2.0, 0.5), (2.5, 0.5), (1.5, 0.3)]):
        mon.on_sample("t-eod", _sample(60 * (i + 1), a, lb),
                      short_leg=_leg(7480.0), long_leg=_leg(7445.0))
    assert _records_by_id()["t-eod"].status == "open"   # no stop -> still open

    closed = mon.close_all_eod(reason="eod")
    assert closed == ["t-eod"]
    rec = _records_by_id()["t-eod"]
    assert rec.status == "closed"
    assert rec.exit.exit_reason == "eod"
    # final marks: short_ask 1.5, long_bid 0.3 -> close 1.2 -> pnl (4.05-1.2)*100 = 285
    assert rec.exit.pnl == pytest.approx(285.0)
    assert "t-eod" not in s8_store.read_open_state()


def test_close_all_eod_no_samples_records_reason_with_none_marks():
    _seed("t-quiet")
    mon = S8Monitor()
    mon.load_open_positions()
    closed = mon.close_all_eod(reason="eod")   # never saw a sample
    assert closed == ["t-quiet"]
    rec = _records_by_id()["t-quiet"]
    assert rec.status == "closed"
    assert rec.exit.exit_reason == "eod"
    assert rec.exit.pnl is None                 # honest: no priceable sample
    assert rec.exit.exit_ts is None


# --------------------------------------------------------------------------- #
# CRASH RECOVERY — reconcile-on-load + idempotent resume
# --------------------------------------------------------------------------- #

def test_crash_recovery_reconcile_drops_closed_and_resumes_open():
    # Seed one OPEN and one already-CLOSED trade, with BOTH lingering in open-state
    # (the crash signature: exit written but open-state not yet pruned).
    _seed("t-open", short=7480.0, long=7445.0)
    s8_store.upsert_trade_record(_open_record("t-closed", short=7470.0, long=7435.0,
                                              status="closed"))
    s8_store.write_open_state({"t-open": {"status": "open"},
                               "t-closed": {"status": "open"}})

    mon = S8Monitor()
    monitored = mon.load_open_positions()
    # only the open one is monitored; the closed one is cleaned out (idempotent recovery)
    assert monitored == ["t-open"]
    assert set(s8_store.read_open_state()) == {"t-open"}
    # the already-closed trade was NOT touched / re-closed
    assert _records_by_id()["t-closed"].status == "closed"
    n_lines = _count_trade_lines()

    # Feed a couple of NON-crossing ticks (position stays open through this "life").
    mon.on_sample("t-open", _sample(60, 3.0, 0.5))
    mon.on_sample("t-open", _sample(120, 4.0, 0.5))
    assert _records_by_id()["t-open"].status == "open"

    # Simulate a mid-life RESTART: a fresh service reloads from the durable store.
    mon2 = S8Monitor()
    assert mon2.load_open_positions() == ["t-open"]     # resumes the still-open one
    assert set(s8_store.read_open_state()) == {"t-open"}
    # no double-close occurred across the restart: no new exit line was written
    assert _count_trade_lines() == n_lines
    assert _records_by_id()["t-open"].status == "open"

    # And it can still finalize normally after the restart.
    mon2.on_sample("t-open", _sample(180, 6.5, 0.5))    # crosses the stop now
    assert _records_by_id()["t-open"].status == "closed"
    assert _records_by_id()["t-open"].exit.exit_reason == "stop_hit"


# --------------------------------------------------------------------------- #
# idempotent finalize
# --------------------------------------------------------------------------- #

def test_finalize_exit_twice_is_noop():
    _seed("t-fin")
    mon = S8Monitor()
    mon.load_open_positions()
    mon.on_sample("t-fin", _sample(60, 6.5, 0.5))   # triggers -> finalize #1
    assert mon.finalize_exit("t-fin") is False       # already dropped -> no-op
    n_lines = _count_trade_lines()

    # Even re-seeding the live handle and calling again must not write a 2nd exit,
    # because the durable record is already closed.
    mon.on_sample("t-fin", _sample(120, 9.0, 0.9))   # ignored (not monitored)
    assert _count_trade_lines() == n_lines
    rec = _records_by_id()["t-fin"]
    assert rec.status == "closed"
    assert rec.exit.pnl == pytest.approx((4.05 - (6.5 - 0.5)) * 100)  # -195


# --------------------------------------------------------------------------- #
# tick persistence
# --------------------------------------------------------------------------- #

def test_tick_persistence_schema_and_partition():
    _seed("t-ticks")
    mon = S8Monitor()
    mon.load_open_positions()
    # a handful of non-crossing samples, greeks attached
    for i in range(4):
        mon.on_sample("t-ticks", _sample(60 * (i + 1), 2.0, 0.5),
                      short_leg=_leg(7480.0), long_leg=_leg(7445.0))
    mon.flush_all_ticks()   # buffer below the batch threshold -> force it out

    part_dir = s8_store._ticks_dir() / "date=20260717"
    assert part_dir.exists()
    # read the part file directly (the raw table is exactly TICK_COLUMNS; reading the whole
    # dir would add the hive `date` partition column).
    parts = list(part_dir.glob("*.parquet"))
    assert parts
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    assert list(df.columns) == TICK_COLUMNS
    # 4 samples x 2 legs = 8 rows
    assert len(df) == 8
    assert set(df["leg"]) == {"short", "long"}
    # greeks present on the persisted ticks (came from the LegGrabs)
    shorts = df[df["leg"] == "short"]
    assert shorts["delta"].notna().all()
    assert shorts["delta"].tolist() == pytest.approx([-0.2] * len(shorts))
    # ts written as CT ISO string
    assert all(isinstance(v, str) and v.startswith("2026-07-17T") for v in df["ts"])


def test_price_only_ticks_when_no_leggrabs():
    _seed("t-priceonly")
    mon = S8Monitor()
    mon.load_open_positions()
    mon.on_sample("t-priceonly", _sample(60, 2.0, 0.5))   # no LegGrabs
    mon.flush_all_ticks()
    df = pd.read_parquet(s8_store._ticks_dir())
    assert len(df) == 2
    assert df["delta"].isna().all()          # greeks None when no live grab
    assert df["bid"].notna().any()           # prices still captured


# --------------------------------------------------------------------------- #
# on_sample never raises
# --------------------------------------------------------------------------- #

def test_on_sample_never_raises_on_bad_sample():
    _seed("t-bad")
    mon = S8Monitor()
    mon.load_open_positions()
    # a garbage sample must be swallowed, not raised
    mon.on_sample("t-bad", Sample(ts=None, short_ask=None, long_bid=None))
    mon.on_sample("unknown-trade", _sample(60, 6.5, 0.5))   # stray tick, no such position
    assert _records_by_id()["t-bad"].status == "open"


# --------------------------------------------------------------------------- #
# small util
# --------------------------------------------------------------------------- #

def _count_trade_lines():
    path = s8_store._trades_file()
    if not path.exists():
        return 0
    with open(path, "r", encoding="utf-8") as fh:
        return sum(1 for ln in fh if ln.strip())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
