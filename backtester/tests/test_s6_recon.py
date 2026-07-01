r"""
test_s6_recon.py — unit tests for the intraday recon (s6_recon).

Pin the math, not any market outcome:
  * Black-Scholes price/delta sanity (ATM call delta ~0.5, put-call parity of prices);
  * IV round-trip: price a known vol, invert the mid, recover the vol;
  * forward recovery from a synthetic parity-consistent chain returns the true forward;
  * recovered per-strike delta matches the generating BS delta on synthetic data
    (this is the engine the EOD validation exercises against real data).

Synthetic chains are built so the answer is known exactly — no warehouse needed.
"""

from __future__ import annotations

import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s6_recon as recon  # noqa: E402


def _t():
    """A ~30-DTE world for the BS math tests.

    The recon engine's 0DTE day-count is validated against REAL EOD data in s6_validate;
    here we exercise the pure BS price/IV/delta math where OTM strikes have non-trivial,
    invertible values. (At true 0DTE a 5-point-OTM option is worth a sub-penny, so a
    synthetic ±spread would swamp it and the round-trip would be meaningless — that is a
    property of 0DTE, not a recon bug.) The expiry-date trick: put expiry 30 days after
    the trade date and price at 13:00, so time_to_expiry_years ~ 30 days.
    """
    day = dt.date(2024, 6, 3)
    expiration = dt.date(2024, 7, 3)
    minute = pd.Timestamp(dt.datetime.combine(day, dt.time(13, 0)))
    return expiration, minute, recon.time_to_expiry_years(minute, expiration)


def test_bs_atm_call_delta_near_half():
    day, minute, t = _t()
    # ATM forward-ish: pick spot=strike, small carry => delta a touch above 0.5.
    d = recon.bs_delta(spot=100.0, strike=100.0, t_years=t, vol=0.20, is_call=True)
    assert 0.45 < d < 0.60


def test_bs_put_call_parity_of_prices():
    day, minute, t = _t()
    s, k, vol = 100.0, 100.0, 0.20
    call = recon.bs_price(s, k, t, vol, is_call=True)
    put = recon.bs_price(s, k, t, vol, is_call=False)
    r, q = recon.RISK_FREE_RATE, recon.DIVIDEND_YIELD
    lhs = call - put
    rhs = s * math.exp(-q * t) - k * math.exp(-r * t)
    assert lhs == pytest.approx(rhs, abs=1e-6)


def test_iv_round_trip():
    day, minute, t = _t()
    s, k, true_vol = 100.0, 102.0, 0.25
    mid = recon.bs_price(s, k, t, true_vol, is_call=True)
    iv = recon.implied_vol_from_mid(mid, s, k, t, is_call=True)
    assert iv == pytest.approx(true_vol, abs=1e-3)


def test_iv_returns_nan_below_intrinsic():
    day, minute, t = _t()
    # A mid below intrinsic is arb-inconsistent -> NaN, not a fabricated vol.
    s, k = 100.0, 90.0  # deep ITM call, intrinsic ~10
    iv = recon.implied_vol_from_mid(0.50, s, k, t, is_call=True)
    assert math.isnan(iv)


def _synthetic_chain(day, minute, t, spot, vol, strikes):
    """Build a parity-consistent NBBO snapshot from a known BS world (tight 0.10 spread)."""
    rows = []
    for k in strikes:
        for right, is_call in (("CALL", True), ("PUT", False)):
            px = recon.bs_price(spot, k, t, vol, is_call=is_call)
            rows.append({"strike": float(k), "right": right,
                         "bid": max(px - 0.05, 0.0), "ask": px + 0.05})
    return pd.DataFrame(rows)


def test_forward_recovery_matches_truth():
    day, minute, t = _t()
    spot, vol = 100.0, 0.20
    strikes = np.arange(80, 121, 1.0)
    snap = _synthetic_chain(day, minute, t, spot, vol, strikes)
    sr = recon.recover_forward_spot(snap, minute, day)
    assert sr is not None
    # True forward = spot * e^{(r-q)t}.
    true_fwd = spot * math.exp((recon.RISK_FREE_RATE - recon.DIVIDEND_YIELD) * t)
    assert sr.forward == pytest.approx(true_fwd, abs=0.05)
    assert sr.spot == pytest.approx(spot, abs=0.05)


def test_per_strike_delta_recovers_generating_delta():
    day, minute, t = _t()
    spot, vol = 100.0, 0.20
    strikes = np.arange(85, 116, 1.0)
    snap = _synthetic_chain(day, minute, t, spot, vol, strikes)
    sr = recon.recover_forward_spot(snap, minute, day)
    tbl = recon.per_strike_delta(snap, minute, day, sr.spot)
    # Compare recovered delta to the TRUE BS delta for a few strikes, both sides.
    for k, right, is_call in [(95.0, "PUT", False), (100.0, "CALL", True),
                              (110.0, "CALL", True)]:
        truth = recon.bs_delta(spot, k, t, vol, is_call=is_call)
        got = tbl[(tbl["strike"] == k) & (tbl["right"] == right)]["delta"].iloc[0]
        assert got == pytest.approx(truth, abs=0.01), f"{k}{right}: {got} vs {truth}"


def test_recover_returns_none_on_thin_chain():
    day, minute, t = _t()
    # Only two strikes -> fewer than the 3 required C-P pairs -> None (no fabrication).
    snap = _synthetic_chain(day, minute, t, 100.0, 0.20, [99.0, 101.0])
    snap = snap[snap["right"] == "CALL"]  # strip puts so no pairs exist
    assert recon.recover_forward_spot(snap, minute, day) is None
