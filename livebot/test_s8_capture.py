"""
test_s8_capture.py — OFFLINE unit tests for the S8 live-pilot rich entry capture
(Phase 1). NO broker, NO gateway, NO network, NO real sleeps.

Covers the two PURE, offline-testable pieces of s8_capture:
  * leg_grab_from_ticker — greeks populated + complete=True when modelGreeks present;
    all-None greeks + complete=False when absent; NaN normalised to None.
  * build_entry_trade_record — a well-formed status="open" TradeRecord whose
    greeks_complete reflects both legs, round-tripping through s8_store (with
    S8_PILOT_ROOT redirected to a tmp dir so the real off-Drive tree is never touched).

The LIVE pieces (grab_leg_live / grab_vix_live / capture_and_persist_entry) are exercised
by the separate live smoke, not here — they need a real gateway.

Run:
  cd livebot
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s8_capture.py -q
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import s8_capture
import s8_schema
import s8_store


# --------------------------------------------------------------------------- #
# Fakes (no ib_async, no IB) — a Ticker-like object and a SpreadPick-like object
# --------------------------------------------------------------------------- #
@dataclass
class _FakeGreeks:
    delta: Optional[float] = None
    gamma: Optional[float] = None
    vega: Optional[float] = None
    theta: Optional[float] = None
    impliedVol: Optional[float] = None
    undPrice: Optional[float] = None


class _FakeTicker:
    """Mimics the fields s8_capture.leg_grab_from_ticker reads off an ib_async Ticker."""
    def __init__(self, *, bid=None, ask=None, last=None, bidSize=None, askSize=None,
                 volume=None, callOpenInterest=None, putOpenInterest=None,
                 modelGreeks=None):
        self.bid = bid
        self.ask = ask
        self.last = last
        self.bidSize = bidSize
        self.askSize = askSize
        self.volume = volume
        self.callOpenInterest = callOpenInterest
        self.putOpenInterest = putOpenInterest
        self.modelGreeks = modelGreeks


@dataclass
class _FakePick:
    template_name: str = "Puts-80-$4"
    side: str = "PUT"
    short_strike: float = 7480.0
    long_strike: float = 7445.0
    width: float = 35.0
    realized_credit: float = 4.05


# --------------------------------------------------------------------------- #
# leg_grab_from_ticker
# --------------------------------------------------------------------------- #
def test_leg_grab_populates_greeks_and_complete_when_modelgreeks_present():
    greeks = _FakeGreeks(delta=-0.24, gamma=0.001, vega=0.11, theta=-0.9,
                         impliedVol=0.18, undPrice=7500.5)
    t = _FakeTicker(bid=4.10, ask=4.30, last=4.20, bidSize=12, askSize=8,
                    volume=1500, putOpenInterest=4200, callOpenInterest=999,
                    modelGreeks=greeks)

    leg = s8_capture.leg_grab_from_ticker(t, "P", 7480.0)

    assert leg.complete is True
    assert leg.right == "P"
    assert leg.strike == 7480.0
    assert leg.bid == 4.10 and leg.ask == 4.30 and leg.last == 4.20
    assert leg.bid_size == 12 and leg.ask_size == 8 and leg.volume == 1500
    # Put leg reads putOpenInterest (not callOpenInterest).
    assert leg.open_interest == 4200
    assert leg.delta == -0.24 and leg.gamma == 0.001
    assert leg.vega == 0.11 and leg.theta == -0.9
    assert leg.iv == 0.18 and leg.underlying_spot == 7500.5
    assert leg.grab_ts is not None


def test_leg_grab_call_reads_call_open_interest():
    greeks = _FakeGreeks(delta=0.22)
    t = _FakeTicker(bid=1.0, ask=1.2, putOpenInterest=1, callOpenInterest=3333,
                    modelGreeks=greeks)
    leg = s8_capture.leg_grab_from_ticker(t, "C", 7600.0)
    assert leg.complete is True
    assert leg.open_interest == 3333   # call leg reads callOpenInterest


def test_leg_grab_incomplete_and_null_greeks_when_modelgreeks_absent():
    # modelGreeks is None (the settle-delay case that caused the observed short_delta=null).
    t = _FakeTicker(bid=4.10, ask=4.30, modelGreeks=None)
    leg = s8_capture.leg_grab_from_ticker(t, "P", 7480.0)

    assert leg.complete is False
    # Quotes still recorded...
    assert leg.bid == 4.10 and leg.ask == 4.30
    # ...but every greek is honestly None, not a fabricated value.
    assert leg.delta is None and leg.gamma is None and leg.vega is None
    assert leg.theta is None and leg.iv is None and leg.underlying_spot is None


def test_leg_grab_normalises_nan_to_none():
    nan = float("nan")
    greeks = _FakeGreeks(delta=nan, gamma=0.002, vega=nan, theta=-0.5,
                         impliedVol=nan, undPrice=7500.0)
    t = _FakeTicker(bid=nan, ask=4.30, last=nan, bidSize=nan, askSize=5,
                    volume=nan, putOpenInterest=nan, modelGreeks=greeks)

    leg = s8_capture.leg_grab_from_ticker(t, "P", 7480.0)

    assert leg.complete is True          # modelGreeks object WAS present
    assert leg.bid is None               # NaN -> None
    assert leg.ask == 4.30
    assert leg.last is None
    assert leg.bid_size is None and leg.ask_size == 5
    assert leg.volume is None
    assert leg.open_interest is None
    assert leg.delta is None             # NaN greek -> None
    assert leg.gamma == 0.002
    assert leg.vega is None and leg.theta == -0.5
    assert leg.iv is None and leg.underlying_spot == 7500.0
    # Nothing NaN survives into the record.
    for v in leg.to_dict().values():
        assert not (isinstance(v, float) and math.isnan(v))


# --------------------------------------------------------------------------- #
# build_entry_trade_record
# --------------------------------------------------------------------------- #
def _complete_leg(right, strike, delta):
    greeks = _FakeGreeks(delta=delta, gamma=0.001, vega=0.1, theta=-0.8,
                         impliedVol=0.18, undPrice=7500.0)
    t = _FakeTicker(bid=1.0, ask=1.1, modelGreeks=greeks)
    return s8_capture.leg_grab_from_ticker(t, right, strike)


def _incomplete_leg(right, strike):
    t = _FakeTicker(bid=1.0, ask=1.1, modelGreeks=None)
    return s8_capture.leg_grab_from_ticker(t, right, strike)


def test_build_entry_trade_record_wellformed_open_record():
    pick = _FakePick()
    cfg = {"side": "Puts", "target_credit": 4.0, "stop_multiple": 3.3}
    short_leg = _complete_leg("P", 7480.0, -0.24)
    long_leg = _complete_leg("P", 7445.0, -0.16)

    rec = s8_capture.build_entry_trade_record(
        pick=pick, template_cfg=cfg, account="U14438624", qty=1,
        entry_ts="2026-07-17T13:30:45.123-05:00",
        entry_spot=7500.0, entry_vix=14.2, entry_realized_vol=None,
        short_leg=short_leg, long_leg=long_leg, stop_price=7.3,
        paperbot_version="0.16.0", pilot_mode=True,
    )

    assert isinstance(rec, s8_schema.TradeRecord)
    assert rec.status == "open"
    assert rec.exit is None
    assert rec.account == "U14438624"
    assert rec.template == "Puts-80-$4"
    assert rec.side == "PUT"
    assert rec.qty == 1
    # date/slot derived from the CT entry timestamp.
    assert rec.date == "20260717"
    assert rec.slot == "13:30"
    assert rec.trade_id == "20260717:Puts-80-$4:13:30:7480:7445"
    # entry group filled.
    assert rec.entry.entry_spot == 7500.0
    assert rec.entry.entry_vix == 14.2
    assert rec.entry.entry_realized_vol is None
    assert rec.entry.short_strike == 7480.0 and rec.entry.long_strike == 7445.0
    assert rec.entry.width == 35.0
    assert rec.entry.realized_credit == 4.05
    assert rec.entry.stop_multiple == 3.3
    assert rec.entry.stop_price == 7.3
    assert rec.entry.short_leg is short_leg and rec.entry.long_leg is long_leg
    # both legs complete -> greeks_complete True.
    assert rec.entry.greeks_complete is True
    # provenance.
    assert rec.provenance.paperbot_version == "0.16.0"
    assert rec.provenance.pilot_mode is True


def test_build_entry_trade_record_greeks_complete_false_if_a_leg_incomplete():
    pick = _FakePick()
    cfg = {"stop_multiple": 3.3}
    short_leg = _complete_leg("P", 7480.0, -0.24)
    long_leg = _incomplete_leg("P", 7445.0)   # greeks never arrived

    rec = s8_capture.build_entry_trade_record(
        pick=pick, template_cfg=cfg, account="U14438624", qty=1,
        entry_ts="2026-07-17T13:30:45.123-05:00",
        entry_spot=7500.0, entry_vix=None, entry_realized_vol=None,
        short_leg=short_leg, long_leg=long_leg, stop_price=7.3,
        paperbot_version="0.16.0", pilot_mode=True,
    )

    assert rec.entry.greeks_complete is False
    assert rec.status == "open"


# --------------------------------------------------------------------------- #
# Round-trip through s8_store (S8_PILOT_ROOT redirected to a tmp dir)
# --------------------------------------------------------------------------- #
def test_trade_record_round_trips_through_store(tmp_path, monkeypatch):
    # Redirect the store root so the real off-Drive tree is never touched.
    monkeypatch.setenv("S8_PILOT_ROOT", str(tmp_path))
    assert s8_store.get_root() == tmp_path

    pick = _FakePick()
    cfg = {"stop_multiple": 3.3}
    rec = s8_capture.build_entry_trade_record(
        pick=pick, template_cfg=cfg, account="U14438624", qty=1,
        entry_ts="2026-07-17T13:30:45.123-05:00",
        entry_spot=7500.0, entry_vix=14.2, entry_realized_vol=None,
        short_leg=_complete_leg("P", 7480.0, -0.24),
        long_leg=_complete_leg("P", 7445.0, -0.16),
        stop_price=7.3, paperbot_version="0.16.0", pilot_mode=True,
    )
    rec.expiration = "20260717"

    s8_store.upsert_trade_record(rec)
    out = s8_store.read_trade_records()

    assert len(out) == 1
    got = out[0]
    assert got.trade_id == rec.trade_id
    assert got.status == "open"
    assert got.expiration == "20260717"
    assert got.entry.greeks_complete is True
    assert got.entry.short_leg.delta == -0.24
    assert got.entry.long_leg.strike == 7445.0
    # Latest-wins upsert: a second write with the same trade_id supersedes the first.
    rec.status = "closed"
    s8_store.upsert_trade_record(rec)
    out2 = s8_store.read_trade_records()
    assert len(out2) == 1
    assert out2[0].status == "closed"
