"""test_s8_collector.py — OFFLINE tests for the S8 intraday ATM-band market collector
(Phase 3). NO broker, NO gateway, NO network, NO real sleeps.

Covers the PURE, offline-testable seams of s8_collector:
  * clamp_max_strikes / band_line_count — the market-data-line budget arithmetic (Risk #1):
      the clamp actually clamps a too-wide request down to the line budget.
  * compute_atm_band — centred on ATM, respects the max-strikes cap, covers the expected
      near-money range, and refuses an unresolved spot / empty grid.
  * market_row_from_ticker / build_market_frame — assembly from fake tickers: greeks + quotes
      populate, NaN -> None, and the frame carries exactly MARKET_COLUMNS.
  * write_market round-trip — the assembled frame lands under the date partition with the
      MARKET_COLUMNS schema (S8_PILOT_ROOT redirected to a tmp dir; real tree untouched).

The LIVE pieces (S8Collector.run / _subscribe_band / _resolve_chain) need a real gateway
and are exercised by the live smoke, not here.

Run (from C:\\TradingDesk\\livebot):
    powershell -Command "$env:PYTHONPATH=''; C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s8_collector.py -q"
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd
import pytest

import s8_collector
import s8_store
from s8_collector import (
    StartupDataTimeout,
    band_line_count,
    build_market_frame,
    clamp_max_strikes,
    compute_atm_band,
    market_row_from_ticker,
    wait_for_live_spot,
)
from s8_schema import MARKET_COLUMNS


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path, monkeypatch):
    """Point the store at a throwaway root for every test (real tree untouched)."""
    monkeypatch.setenv("S8_PILOT_ROOT", str(tmp_path))
    assert s8_store.get_root() == tmp_path
    return tmp_path


# --------------------------------------------------------------------------- #
# Fakes — a Ticker-like object (mirrors what leg_grab_from_ticker reads)
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
    def __init__(self, *, bid=None, ask=None, last=None, bidSize=None, askSize=None,
                 volume=None, callOpenInterest=None, putOpenInterest=None, modelGreeks=None):
        self.bid = bid
        self.ask = ask
        self.last = last
        self.bidSize = bidSize
        self.askSize = askSize
        self.volume = volume
        self.callOpenInterest = callOpenInterest
        self.putOpenInterest = putOpenInterest
        self.modelGreeks = modelGreeks


# A 5-point SPXW-style grid around ~7400 (mirrors real near-money spacing).
GRID = [7000.0 + 5 * i for i in range(161)]  # 7000..7800 inclusive


# =========================================================================== #
# Line-budget arithmetic (Risk #1)
# =========================================================================== #

def test_band_line_count_counts_both_rights_plus_underlyings():
    # N strikes * 2 rights + 2 underlyings (SPX, VIX).
    assert band_line_count(24) == 24 * 2 + 2   # 50
    assert band_line_count(0) == 2
    assert band_line_count(10, underlying_lines=0) == 20


def test_clamp_max_strikes_actually_clamps_to_the_budget():
    # A too-wide request (60 strikes -> 122 lines) must clamp to fit a 100-line cap:
    # (100 - 2) // 2 = 49 strikes.
    assert clamp_max_strikes(60, max_lines=100) == 49
    # A request already under budget passes through unchanged.
    assert clamp_max_strikes(24, max_lines=100) == 24
    # A tiny budget still keeps at least the token floor (never returns 0/negative).
    assert clamp_max_strikes(24, max_lines=4) == s8_collector._MIN_MAX_STRIKES
    # The default band fits the default cap with headroom to spare (Risk #1 intent).
    assert band_line_count(clamp_max_strikes(s8_collector.DEFAULT_MAX_STRIKES)) <= 60


# =========================================================================== #
# compute_atm_band
# =========================================================================== #

def test_compute_atm_band_centered_and_capped():
    band = compute_atm_band(7401.0, GRID, max_strikes=24)
    # Respects the cap.
    assert len(band) == 24
    # Sorted ascending, all real grid strikes.
    assert band == sorted(band)
    assert set(band).issubset(set(GRID))
    # Centred on ATM: the nearest strike to spot (7400) is inside the band, roughly central.
    assert 7400.0 in band
    idx = band.index(7400.0)
    assert 8 <= idx <= 15  # near the middle of a 24-wide window
    # Covers the near-money range the templates' short legs live in (~+/-60 pts at 5-pt spacing).
    assert band[0] <= 7360.0 and band[-1] >= 7440.0


def test_compute_atm_band_respects_smaller_cap_and_odd_sizes():
    assert len(compute_atm_band(7400.0, GRID, max_strikes=4)) == 4
    assert len(compute_atm_band(7400.0, GRID, max_strikes=7)) == 7
    # Cap larger than the grid returns the whole grid, not an error.
    small = [7395.0, 7400.0, 7405.0]
    assert compute_atm_band(7400.0, small, max_strikes=50) == small


def test_compute_atm_band_edge_spot_still_returns_cap_strikes():
    # Spot near the top edge of the grid: still returns a full cap-sized band, clamped to grid.
    band = compute_atm_band(7795.0, GRID, max_strikes=24)
    assert len(band) == 24
    assert band[-1] == GRID[-1]  # runs up to the grid's top edge


def test_compute_atm_band_rejects_bad_inputs():
    with pytest.raises(ValueError):
        compute_atm_band(7400.0, [], max_strikes=24)          # empty grid
    with pytest.raises(ValueError):
        compute_atm_band(None, GRID, max_strikes=24)          # unresolved spot
    with pytest.raises(ValueError):
        compute_atm_band(float("nan"), GRID, max_strikes=24)  # NaN spot


# =========================================================================== #
# market_row_from_ticker / build_market_frame
# =========================================================================== #

def test_market_row_populates_quotes_and_greeks():
    greeks = _FakeGreeks(delta=-0.22, gamma=0.002, vega=0.12, theta=-0.85,
                         impliedVol=0.19, undPrice=7401.2)
    tk = _FakeTicker(bid=4.10, ask=4.30, last=4.20, bidSize=12, askSize=8,
                     volume=1500, putOpenInterest=4200, callOpenInterest=999,
                     modelGreeks=greeks)
    row = market_row_from_ticker(tk, "P", 7390.0, expiration="20260717",
                                 underlying_spot=7400.5, vix=14.2, ts="2026-07-17T12:35:00.000-05:00")
    assert set(row.keys()) == set(MARKET_COLUMNS)
    assert row["strike"] == 7390.0 and row["right"] == "P"
    assert row["bid"] == 4.10 and row["ask"] == 4.30 and row["last"] == 4.20
    assert row["bid_size"] == 12 and row["ask_size"] == 8 and row["volume"] == 1500
    assert row["open_interest"] == 4200          # put reads putOpenInterest
    assert row["delta"] == -0.22 and row["gamma"] == 0.002
    assert row["vega"] == 0.12 and row["theta"] == -0.85 and row["iv"] == 0.19
    assert row["expiration"] == "20260717"
    assert row["underlying_spot"] == 7400.5      # explicit snapshot spot preferred
    assert row["vix"] == 14.2


def test_market_row_nan_becomes_none_and_missing_greeks_are_none():
    tk = _FakeTicker(bid=float("nan"), ask=4.30, modelGreeks=None)  # no greeks yet
    row = market_row_from_ticker(tk, "C", 7410.0, expiration="20260717",
                                 underlying_spot=None, vix=None, ts="t")
    assert row["bid"] is None            # NaN normalised to None
    assert row["ask"] == 4.30
    assert row["delta"] is None and row["iv"] is None   # greeks absent -> None (not faked)
    assert row["underlying_spot"] is None and row["vix"] is None


def test_market_row_falls_back_to_greeks_undprice_when_no_snapshot_spot():
    greeks = _FakeGreeks(delta=-0.2, undPrice=7402.7)
    tk = _FakeTicker(bid=1.0, ask=1.2, modelGreeks=greeks)
    row = market_row_from_ticker(tk, "P", 7380.0, expiration="20260717",
                                 underlying_spot=None, vix=None, ts="t")
    assert row["underlying_spot"] == 7402.7   # falls back to the leg's model undPrice


def test_build_market_frame_has_exact_market_columns():
    greeks = _FakeGreeks(delta=-0.2, gamma=0.001, vega=0.1, theta=-0.8,
                         impliedVol=0.18, undPrice=7400.0)
    specs = [
        (_FakeTicker(bid=1.0, ask=1.2, putOpenInterest=10, modelGreeks=greeks), "P", 7390.0),
        (_FakeTicker(bid=2.0, ask=2.2, callOpenInterest=20, modelGreeks=greeks), "C", 7410.0),
    ]
    df = build_market_frame(specs, expiration="20260717", underlying_spot=7400.0, vix=14.0)
    assert list(df.columns) == MARKET_COLUMNS
    assert len(df) == 2
    assert set(df["right"]) == {"P", "C"}
    assert df["vix"].tolist() == [14.0, 14.0]


# =========================================================================== #
# write_market round-trip (lands under the date partition, MARKET_COLUMNS schema)
# =========================================================================== #

def test_build_frame_write_market_round_trip(_isolated_root):
    greeks = _FakeGreeks(delta=-0.21, gamma=0.001, vega=0.1, theta=-0.8,
                         impliedVol=0.18, undPrice=7400.0)
    specs = [
        (_FakeTicker(bid=1.0, ask=1.2, putOpenInterest=10, modelGreeks=greeks), "P", 7390.0),
        (_FakeTicker(bid=2.0, ask=2.2, callOpenInterest=20, modelGreeks=greeks), "C", 7410.0),
    ]
    df = build_market_frame(specs, expiration="20260717", underlying_spot=7400.0, vix=14.0)

    out = s8_store.write_market(df, "20260717")
    # Landed under the date partition.
    assert out.exists()
    assert "date=20260717" in str(out).replace("\\", "/")

    back = pd.read_parquet(out)
    assert list(back.columns) == MARKET_COLUMNS
    assert len(back) == 2
    assert sorted(back["strike"].tolist()) == [7390.0, 7410.0]
    assert set(back["vix"]) == {14.0}
    # Greeks survived the parquet round-trip.
    assert back.loc[back["right"] == "P", "delta"].iloc[0] == -0.21


# =========================================================================== #
# Startup data-wait (Phase 4b boot robustness) — PURE, instant, fake clock.
# An all-day scheduled collector must not crash on boot just because live SPX
# data isn't flowing yet; the bounded wait retries then gives up CLEANLY.
# =========================================================================== #

class _FakeClock:
    """Injectable monotonic clock + sleep: sleep advances virtual time. Records sleeps so a
    test can assert prompt returns (no sleeps) vs. bounded retry (sleeps then a clean give-up).
    Makes wait_for_live_spot instant — no real waiting, no hang."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, secs):
        self.sleeps.append(secs)
        self.t += secs


def test_wait_for_live_spot_returns_promptly_when_data_present():
    clk = _FakeClock()
    resolver_calls = []

    def resolver():
        resolver_calls.append(1)
        return 7412.5  # valid on the very first poll

    spot = wait_for_live_spot(resolver, timeout_secs=600.0, poll_secs=15.0,
                              clock=clk.now, sleep=clk.sleep)
    assert spot == 7412.5
    assert len(resolver_calls) == 1     # resolved on the first attempt
    assert clk.sleeps == []             # returned without ever sleeping/waiting


def test_wait_for_live_spot_retries_without_raising_until_data_arrives():
    clk = _FakeClock()
    # None (no mark), NaN (bad tick), 0.0/None again, then a real spot on the 5th poll.
    seq = [None, float("nan"), 0.0, None, 7400.0]
    it = iter(seq)

    spot = wait_for_live_spot(lambda: next(it), timeout_secs=600.0, poll_secs=15.0,
                              clock=clk.now, sleep=clk.sleep)
    assert spot == 7400.0
    assert len(clk.sleeps) == 4                 # slept once between each of the 4 misses
    assert all(s == 15.0 for s in clk.sleeps)   # polled at the configured cadence


def test_wait_for_live_spot_treats_a_raising_resolver_as_not_ready():
    clk = _FakeClock()
    calls = {"n": 0}

    def resolver():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("SPX ticker not available yet")  # pre-open: no ticker at all
        return 7399.0

    spot = wait_for_live_spot(resolver, timeout_secs=600.0, poll_secs=15.0,
                              clock=clk.now, sleep=clk.sleep)
    assert spot == 7399.0
    assert calls["n"] == 3          # a raising resolver is retried, not crashed
    assert len(clk.sleeps) == 2


def test_wait_for_live_spot_gives_up_cleanly_after_bounded_window():
    clk = _FakeClock()
    calls = {"n": 0}

    def resolver():
        calls["n"] += 1
        return None  # data never arrives

    # Bounded: it must RAISE the caught StartupDataTimeout (not hang, not crash raw).
    with pytest.raises(StartupDataTimeout):
        wait_for_live_spot(resolver, timeout_secs=600.0, poll_secs=15.0,
                           clock=clk.now, sleep=clk.sleep)
    # Gave up within the bounded window: virtual time did not exceed the budget unboundedly.
    assert clk.now() <= 600.0 + 15.0
    assert calls["n"] >= 2          # actually polled more than once before giving up
    # ~600s / 15s ≈ 40 polls, never an unbounded loop.
    assert calls["n"] <= 45


def test_wait_for_live_spot_rejects_nonpositive_and_nan_spots():
    # The validity predicate rejects exactly the values that crashed the old boot path.
    assert s8_collector._spot_is_valid(7400.0) is True
    assert s8_collector._spot_is_valid(None) is False
    assert s8_collector._spot_is_valid(float("nan")) is False
    assert s8_collector._spot_is_valid(0.0) is False
    assert s8_collector._spot_is_valid(-1.0) is False
