r"""
test_s6_zones.py — unit tests for the FROZEN leg-base zone detector (s6_zones).

These pin the MECHANICS and the ANTI-LOOK-AHEAD contract of Brandon's documented zone
method, NOT any strategy outcome:
  * base vs leg candle classification = close inside/outside the prior candle's range;
  * a demand zone forms on drop/rally-base-RALLY with 2+ follow-through + swing break;
  * a supply zone forms on rally/drop-base-DROP likewise;
  * boundaries = full wick range of the base (frozen boundary model A);
  * freshness flips to False once price re-enters the band;
  * NO LOOK-AHEAD: a zone whose confirmation closes AFTER the query time is not admissible,
    and freshness only considers bars that closed at/before the query time;
  * the frozen constants are the documented values (not tuned).

All bars are built in-memory with exact arithmetic — no warehouse needed.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s6_zones as z  # noqa: E402


DAY = dt.date(2024, 6, 3)


def _series_from_bars(bars: list[dict], tf: int) -> pd.Series:
    """Build a 1-min spot series that resamples EXACTLY to the given OHLC bars.

    For each target bar we emit 1-min points at the bar's open minute range so that
    resample_ohlc reproduces open/high/low/close. We place open, then high, then low, then
    close at consecutive minutes inside the bar window (tf>=4 minutes guarantees room).
    """
    idx = []
    vals = []
    start = dt.datetime.combine(DAY, dt.time(9, 30))
    for i, b in enumerate(bars):
        bar_open = start + dt.timedelta(minutes=i * tf)
        pts = [b["open"], b["high"], b["low"], b["close"]]
        for k, v in enumerate(pts):
            idx.append(pd.Timestamp(bar_open + dt.timedelta(minutes=k)))
            vals.append(float(v))
    return pd.Series(vals, index=pd.DatetimeIndex(idx)).sort_index()


# --------------------------------------------------------------------------- #
# Frozen constants are the documented values.
# --------------------------------------------------------------------------- #
def test_frozen_constants_are_documented_values():
    assert z.BASE_MIN_CANDLES == 1
    assert z.BASE_MAX_CANDLES == 6
    assert z.DEPARTURE_FOLLOWTHROUGH == 2
    assert z.DEPARTURE_MIN_RANGE_MULT == 2.0
    assert z.PREFER_15M is True
    assert z.TIMEFRAMES == (5, 15)


# --------------------------------------------------------------------------- #
# Candle classification.
# --------------------------------------------------------------------------- #
def test_base_and_leg_classification():
    # bar0 baseline; bar1 close inside bar0 range -> base; bar2 close above bar1 high -> leg_up;
    # bar3 close below bar2 low -> leg_down.
    bars = [
        {"open": 100, "high": 105, "low": 95, "close": 100},   # base (first)
        {"open": 100, "high": 104, "low": 96, "close": 101},   # close 101 inside [95,105] -> base
        {"open": 101, "high": 110, "low": 100, "close": 109},  # close 109 > prior high 104 -> leg_up
        {"open": 109, "high": 110, "low": 90, "close": 92},    # close 92 < prior low 100 -> leg_down
    ]
    ohlc = z.resample_ohlc(_series_from_bars(bars, 5), 5, DAY)
    labels = z.classify_candles(ohlc).tolist()
    assert labels == ["base", "base", "leg_up", "leg_down"]


# --------------------------------------------------------------------------- #
# Demand zone (rally-base-RALLY) with follow-through + swing break.
# --------------------------------------------------------------------------- #
def test_demand_zone_detected_rbr():
    # leg-in UP, then a 1-candle base, then a strong UP leg-out with 2 follow-through bars
    # each making new highs, traveling >= 2x base range (base range = 2).
    bars = [
        {"open": 100, "high": 101, "low": 99, "close": 100},   # base(first)
        {"open": 100, "high": 106, "low": 100, "close": 105},  # leg_up (close 105 > prior high 101)
        {"open": 105, "high": 106, "low": 104, "close": 105.5},  # base: close inside [100,106]; range=2
        {"open": 105.5, "high": 112, "low": 105, "close": 111},  # leg_up: close>base_high(106); new high
        {"open": 111, "high": 118, "low": 111, "close": 117},  # follow-through new high
        {"open": 117, "high": 124, "low": 117, "close": 123},  # follow-through new high (2nd)
    ]
    ohlc = z.resample_ohlc(_series_from_bars(bars, 5), 5, DAY)
    zs = z.detect_zones_timeframe(ohlc, 5)
    demand = [x for x in zs if x.kind == "demand"]
    assert len(demand) >= 1
    d0 = demand[0]
    assert d0.pattern in ("RBR", "DBR")
    # boundary model A = full wick range of the base candle (bar index 2: high 106, low 104).
    assert d0.zone_high == pytest.approx(106.0)
    assert d0.zone_low == pytest.approx(104.0)
    assert d0.proximal == pytest.approx(106.0)  # demand proximal = zone high


def test_supply_zone_detected_rbd():
    # leg-in UP into a base, then a strong DOWN leg-out breaking below base low, 2 follow-through.
    bars = [
        {"open": 100, "high": 101, "low": 99, "close": 100},    # base(first)
        {"open": 100, "high": 106, "low": 100, "close": 105},   # leg_up
        {"open": 105, "high": 107, "low": 105, "close": 106},   # base: close inside [100,106]; range=2
        {"open": 106, "high": 106, "low": 100, "close": 101},   # leg_down: close<base_low(105)? base_low=105 -> 101<105 ok
        {"open": 101, "high": 101, "low": 96, "close": 97},     # follow-through new low
        {"open": 97, "high": 97, "low": 90, "close": 91},       # follow-through new low (2nd)
    ]
    ohlc = z.resample_ohlc(_series_from_bars(bars, 5), 5, DAY)
    zs = z.detect_zones_timeframe(ohlc, 5)
    supply = [x for x in zs if x.kind == "supply"]
    assert len(supply) >= 1
    s0 = supply[0]
    assert s0.pattern in ("RBD", "DBD")
    assert s0.zone_high == pytest.approx(107.0)   # base high
    assert s0.zone_low == pytest.approx(105.0)    # base low
    assert s0.proximal == pytest.approx(105.0)    # supply proximal = zone low


# --------------------------------------------------------------------------- #
# Departure magnitude gate: too-small a departure is NOT a zone.
# --------------------------------------------------------------------------- #
def test_weak_departure_is_not_a_zone():
    # base range 2; leg-out travels only ~1 point (< 2x=4) -> no zone even with 2 up bars.
    bars = [
        {"open": 100, "high": 101, "low": 99, "close": 100},
        {"open": 100, "high": 106, "low": 100, "close": 105},   # leg_up
        {"open": 105, "high": 106, "low": 104, "close": 105.5},  # base range=2, high 106
        {"open": 105.5, "high": 106.5, "low": 105, "close": 106.3},  # weak leg_up: close 106.3 barely > 106
        {"open": 106.3, "high": 106.8, "low": 106, "close": 106.6},  # tiny new high
        {"open": 106.6, "high": 107.0, "low": 106.3, "close": 106.9},  # tiny new high
    ]
    ohlc = z.resample_ohlc(_series_from_bars(bars, 5), 5, DAY)
    zs = z.detect_zones_timeframe(ohlc, 5)
    # travel from proximal(106) to run high ~107 = 1 pt < 4 -> rejected.
    assert all(x.kind != "demand" or x.zone_high != 106.0 for x in zs) or len(zs) == 0


# --------------------------------------------------------------------------- #
# Freshness flips once price re-enters the zone band.
# --------------------------------------------------------------------------- #
def test_freshness_flips_on_reentry():
    bars = [
        {"open": 100, "high": 101, "low": 99, "close": 100},   # base(first)
        {"open": 100, "high": 106, "low": 100, "close": 105},  # leg_up
        {"open": 105, "high": 106, "low": 104, "close": 105.5},  # base high106 low104
        {"open": 105.5, "high": 112, "low": 105, "close": 111},  # leg_up out
        {"open": 111, "high": 118, "low": 111, "close": 117},  # ft new high
        {"open": 117, "high": 124, "low": 117, "close": 123},  # ft new high (confirm here)
        {"open": 123, "high": 124, "low": 105, "close": 106},  # dips back INTO band [104,106]
    ]
    ohlc = z.resample_ohlc(_series_from_bars(bars, 5), 5, DAY)
    zs = z.detect_zones_timeframe(ohlc, 5)
    demand = [x for x in zs if x.kind == "demand"][0]
    # As-of the confirm bar's close: fresh (no re-entry yet).
    assert z.is_fresh(demand, ohlc, demand.confirm_time) is True
    # As-of the last bar's close: price re-entered [104,106] -> not fresh.
    last_close = ohlc["close_time"].iloc[-1]
    assert z.is_fresh(demand, ohlc, last_close) is False


# --------------------------------------------------------------------------- #
# NO LOOK-AHEAD: a zone confirmed after the query time is not admissible.
# --------------------------------------------------------------------------- #
def test_no_lookahead_zone_not_admissible_before_confirm():
    bars = [
        {"open": 100, "high": 101, "low": 99, "close": 100},
        {"open": 100, "high": 106, "low": 100, "close": 105},   # leg_up
        {"open": 105, "high": 106, "low": 104, "close": 105.5},  # base
        {"open": 105.5, "high": 112, "low": 105, "close": 111},  # leg_up out
        {"open": 111, "high": 118, "low": 111, "close": 117},   # ft
        {"open": 117, "high": 124, "low": 117, "close": 123},   # ft (confirm)
    ]
    spot = _series_from_bars(bars, 5)
    ohlc = z.resample_ohlc(spot, 5, DAY)
    zs = z.detect_zones_timeframe(ohlc, 5)
    demand = [x for x in zs if x.kind == "demand"][0]
    confirm = demand.confirm_time
    # Query one minute BEFORE the zone is confirmed -> universe must NOT contain it.
    before = confirm - pd.Timedelta(minutes=1)
    uni_before = z.build_zone_universe(spot, DAY, before)
    assert all(x.confirm_time <= before for x in uni_before.zones)
    assert not any(abs(x.zone_high - demand.zone_high) < 1e-9
                   and x.kind == "demand" for x in uni_before.zones)
    # Query AT/after confirm -> present.
    uni_after = z.build_zone_universe(spot, DAY, confirm)
    assert any(abs(x.zone_high - demand.zone_high) < 1e-9
               and x.kind == "demand" for x in uni_after.zones)


# --------------------------------------------------------------------------- #
# Selection: nearest fresh demand below spot / supply above spot.
# --------------------------------------------------------------------------- #
def test_select_nearest_side():
    # Build one fresh demand below spot; ensure select_zone returns it for 'demand' and
    # None for 'supply' (no supply present).
    bars = [
        {"open": 100, "high": 101, "low": 99, "close": 100},
        {"open": 100, "high": 106, "low": 100, "close": 105},
        {"open": 105, "high": 106, "low": 104, "close": 105.5},  # base high106 low104
        {"open": 105.5, "high": 112, "low": 105, "close": 111},
        {"open": 111, "high": 118, "low": 111, "close": 117},
        {"open": 117, "high": 124, "low": 117, "close": 123},
    ]
    spot = _series_from_bars(bars, 5)
    as_of = z.resample_ohlc(spot, 5, DAY)["close_time"].iloc[-1]
    uni = z.build_zone_universe(spot, DAY, as_of)
    picked = z.select_zone(uni, spot=123.0, side="demand")
    assert picked is not None and picked.kind == "demand" and picked.zone_high <= 123.0
    assert z.select_zone(uni, spot=123.0, side="supply") is None
