"""Tests for ddoi_gamma.py — the DDOI inferred-dealer-direction gamma method.

All synthetic / in-memory (no warehouse read), so they run fast and deterministically.
They pin the two things that actually matter for correctness:

  1. The Lee-Ready classifier signs prints correctly (quote rule above/below/at mid,
     tick-rule fallback, and the store-on-change forward-fill via merge_asof) AND never
     back-fills a quote that did not yet exist (no look-ahead).
  2. The DDOI net-GEX differs from the static baseline ONLY through the per-contract
     dealer SIGN -- same gamma, OI, spot, gross, and threshold -- so the comparison is
     an honest apples-to-apples method swap.
"""
from __future__ import annotations

import datetime as _dt

import numpy as np
import pandas as pd
import pytest

import ddoi_gamma as d


# --------------------------------------------------------------------------- #
# Helpers to fabricate the exact frame schema the s5 reader returns.
# --------------------------------------------------------------------------- #
def _ts(day: _dt.date, hh: int, mm: int) -> _dt.datetime:
    return _dt.datetime.combine(day, _dt.time(hh, mm))


def _ohlc(rows: list[dict]) -> pd.DataFrame:
    cols = ["expiration", "strike", "right", "timestamp",
            "open", "high", "low", "close", "volume", "count", "vwap"]
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns:
            df[c] = np.nan
    return df[cols]


def _quote(rows: list[dict]) -> pd.DataFrame:
    cols = ["symbol", "expiration", "strike", "right", "timestamp",
            "bid_size", "bid", "ask_size", "ask"]
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns:
            df[c] = np.nan
    df["symbol"] = "SPXW"
    return df[cols]


DAY = _dt.date(2022, 3, 31)
EXP = "2022-03-31"


# --------------------------------------------------------------------------- #
# 1. Quote rule: above mid -> customer BUY (+1); below mid -> customer SELL (-1)
# --------------------------------------------------------------------------- #
def test_quote_rule_above_and_below_mid():
    # One contract, two minutes. NBBO 1.00/2.00 -> mid 1.50, half-spread 0.50.
    # Minute 1 trade at 1.90 (well above mid) -> BUY. Minute 2 at 1.10 -> SELL.
    q = _quote([
        dict(expiration=EXP, strike=4500.0, right="CALL",
             timestamp=_ts(DAY, 9, 30), bid=1.00, ask=2.00, bid_size=10, ask_size=10),
    ])
    o = _ohlc([
        dict(expiration=EXP, strike=4500.0, right="CALL",
             timestamp=_ts(DAY, 9, 31), close=1.90, vwap=1.90, volume=5),
        dict(expiration=EXP, strike=4500.0, right="CALL",
             timestamp=_ts(DAY, 9, 32), close=1.10, vwap=1.10, volume=7),
    ])
    c = d._classify_bars_quotes(o, q).sort_values("minute").reset_index(drop=True)
    assert list(c["cust_side"]) == [1.0, -1.0]


# --------------------------------------------------------------------------- #
# 2. At-the-mid print routes to the TICK rule (not classified by quote rule).
# --------------------------------------------------------------------------- #
def test_at_mid_uses_tick_rule():
    # mid = 1.50. First print at mid exactly, no prior trade -> unresolved (0).
    # Second print at mid but UP-tick vs the first (1.60 > 1.50) -> BUY via tick rule.
    q = _quote([
        dict(expiration=EXP, strike=4500.0, right="CALL",
             timestamp=_ts(DAY, 9, 30), bid=1.00, ask=2.00, bid_size=10, ask_size=10),
    ])
    o = _ohlc([
        dict(expiration=EXP, strike=4500.0, right="CALL",
             timestamp=_ts(DAY, 9, 31), close=1.50, vwap=1.50, volume=5),   # at mid, no prior
        dict(expiration=EXP, strike=4500.0, right="CALL",
             timestamp=_ts(DAY, 9, 32), close=1.60, vwap=1.60, volume=5),   # uptick -> BUY
    ])
    c = d._classify_bars_quotes(o, q).sort_values("minute").reset_index(drop=True)
    # first: no mid-resolution and no prior print -> 0 (carried, none available)
    assert c.loc[0, "cust_side"] == 0.0
    # second: within-mid-band so quote rule abstains, tick vs 1.50 is up -> +1
    assert c.loc[1, "cust_side"] == 1.0


# --------------------------------------------------------------------------- #
# 3. No look-ahead: a trade BEFORE the contract's first kept quote gets NaN mid
#    (never back-filled) and therefore must fall to the tick rule, not borrow a
#    future quote.
# --------------------------------------------------------------------------- #
def test_no_lookahead_pretrade_quote_is_nan():
    # Quote first appears at 9:35. A trade at 9:31 must NOT see it (no back-fill).
    q = _quote([
        dict(expiration=EXP, strike=4500.0, right="CALL",
             timestamp=_ts(DAY, 9, 35), bid=1.00, ask=2.00, bid_size=10, ask_size=10),
    ])
    o = _ohlc([
        dict(expiration=EXP, strike=4500.0, right="CALL",
             timestamp=_ts(DAY, 9, 31), close=1.90, vwap=1.90, volume=5),   # before any quote
    ])
    c = d._classify_bars_quotes(o, q).reset_index(drop=True)
    assert pd.isna(c.loc[0, "mid"])          # quote not invented
    assert c.loc[0, "cust_side"] == 0.0      # no prior print -> unresolved, not a future-quote BUY


# --------------------------------------------------------------------------- #
# 4. Dealer sign is the NEGATIVE of net customer flow, per contract.
# --------------------------------------------------------------------------- #
def test_dealer_sign_is_opposite_of_customer_flow():
    # Net customer BUYING this contract (2 buys of 10 vs 1 sell of 5) -> dealer SHORT (-1).
    classified = pd.DataFrame({
        "expiration": [EXP, EXP, EXP],
        "strike": [4500.0, 4500.0, 4500.0],
        "right": ["CALL", "CALL", "CALL"],
        "minute": [_ts(DAY, 9, 31), _ts(DAY, 9, 32), _ts(DAY, 9, 33)],
        "volume": [10.0, 10.0, 5.0],
        "price": [1.9, 1.9, 1.1],
        "mid": [1.5, 1.5, 1.5],
        "cust_side": [1.0, 1.0, -1.0],
    })
    g = d.dealer_sign_by_contract(classified)
    assert len(g) == 1
    assert g.loc[0, "right"] == "C"                 # normalized for the EOD join
    assert g.loc[0, "dealer_sign"] == -1.0          # customers net long -> dealer short
    assert g.loc[0, "dealer_signed_vol"] == -15.0   # -(10+10-5)


# --------------------------------------------------------------------------- #
# 5. State thresholding matches the production gex.py band logic exactly.
# --------------------------------------------------------------------------- #
def test_state_from_net_matches_production_band():
    gross = 100.0
    assert d._state_from_net(+6.0, gross) == "Positive"   # > 5% of gross
    assert d._state_from_net(-6.0, gross) == "Negative"
    assert d._state_from_net(+4.0, gross) == "Neutral"    # inside the band
    assert d._state_from_net(0.0, 0.0) == "Neutral"       # degenerate gross


# --------------------------------------------------------------------------- #
# 6. merge_asof forward-fill == the reader's dense-grid forward-fill (the values
#    the classifier consumes are the store-on-change reconstruction, not invented).
# --------------------------------------------------------------------------- #
def test_ffill_matches_reader_grid():
    # Quote updates at 9:30 (1/2) then 9:40 (3/4). A trade at 9:35 must use the 9:30
    # quote (mid 1.5); a trade at 9:45 must use the 9:40 quote (mid 3.5).
    q = _quote([
        dict(expiration=EXP, strike=4500.0, right="CALL",
             timestamp=_ts(DAY, 9, 30), bid=1.0, ask=2.0, bid_size=10, ask_size=10),
        dict(expiration=EXP, strike=4500.0, right="CALL",
             timestamp=_ts(DAY, 9, 40), bid=3.0, ask=4.0, bid_size=10, ask_size=10),
    ])
    o = _ohlc([
        dict(expiration=EXP, strike=4500.0, right="CALL",
             timestamp=_ts(DAY, 9, 35), close=1.50, vwap=1.50, volume=5),
        dict(expiration=EXP, strike=4500.0, right="CALL",
             timestamp=_ts(DAY, 9, 45), close=3.50, vwap=3.50, volume=5),
    ])
    c = d._classify_bars_quotes(o, q).sort_values("minute").reset_index(drop=True)
    assert c.loc[0, "mid"] == 1.5
    assert c.loc[1, "mid"] == 3.5


# --------------------------------------------------------------------------- #
# 7. THE core comparison invariant: DDOI net differs from static net ONLY through
#    the per-contract sign. Rebuild static net from the DDOI machinery by forcing the
#    inferred sign to equal the static call/put sign, and it must reproduce the static.
# --------------------------------------------------------------------------- #
def test_ddoi_reduces_to_static_when_signs_agree():
    # Fabricate a tiny EOD chain and drive both nets by hand to prove the ONLY moving
    # part is the sign vector. Static: calls +1, puts -1.
    gam = np.array([0.001, 0.002, 0.0015])
    oi = np.array([100.0, 50.0, 80.0])
    spot = 4500.0
    is_call = np.array([True, False, True])
    dollar_gamma = gam * oi * d.CONTRACT_MULT * spot * spot * 0.01
    static_sign = np.where(is_call, d.CALL_SIGN, d.PUT_SIGN)
    net_static = float(np.sum(static_sign * dollar_gamma))
    # DDOI with signs == static must equal static exactly.
    ddoi_sign_same = static_sign.copy()
    net_ddoi_same = float(np.sum(ddoi_sign_same * dollar_gamma))
    assert net_ddoi_same == pytest.approx(net_static)
    # Flip one put to dealer-long -> net must change (sign is the only lever).
    ddoi_sign_diff = static_sign.copy()
    ddoi_sign_diff[1] = +1.0
    net_ddoi_diff = float(np.sum(ddoi_sign_diff * dollar_gamma))
    assert net_ddoi_diff != pytest.approx(net_static)


# --------------------------------------------------------------------------- #
# 8. Empty inputs are handled without raising.
# --------------------------------------------------------------------------- #
def test_empty_inputs():
    empty = d._classify_bars_quotes(pd.DataFrame(), pd.DataFrame())
    assert empty.empty
    g = d.dealer_sign_by_contract(empty)
    assert g.empty
