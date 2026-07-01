r"""
s6_recon.py — intraday spot / forward / per-strike delta recovery for 0DTE SPXW.

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

The 1-minute SPXW warehouse (read via s5_intraday_data) gives us option NBBO per
minute but NO underlying spot and NO greeks intraday. This module recovers them from
the quotes themselves, with NO tuning of any free parameter:

  1. SYNTHETIC FORWARD via put-call parity.  For a given minute and a single
     expiration, same-strike call/put mids satisfy
            C - P = (F - K) * e^{-rT}
     so  F = K + (C - P) * e^{rT}.  We solve F from several NEAR-ATM strikes (where
     C - P is most reliable) and take the median, which is robust to a few stale legs.
     Spot S = F * e^{-(r - q) T}  (q = SPX dividend yield carry).

  2. PER-STRIKE IMPLIED VOL by Black-Scholes inversion of each option's mid (bisection),
     using the recovered forward.  No smile model is fit — each strike stands alone, so
     nothing is curve-fit across strikes.

  3. PER-STRIKE DELTA from Black-Scholes (spot delta) using the recovered spot, the
     per-strike IV, the strike, and the time-to-expiry.

Day-count / rates (declared constants, not tuned):
  * RISK_FREE_RATE / DIVIDEND_YIELD are fixed, plainly-stated assumptions.  For 0DTE the
    discount factor e^{-rT} over a few hours is ~1.000, so the answer is almost entirely
    insensitive to these — we still carry them for correctness.
  * Time-to-expiry T is measured to the 16:00 ET PM-settlement minute in CALENDAR-year
    fraction (hours-to-expiry / (365.25*24)).  0DTE SPXW settle on the PM print; we use
    16:00 as the settlement instant.  This is the documented convention, not a knob.

Everything is plain numpy/pandas with comments on the "why".
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Declared assumptions (NOT tuned). Stated here so they are auditable in one place.
# --------------------------------------------------------------------------- #
RISK_FREE_RATE = 0.04        # flat annualized; 0DTE discount factor ~ 1, so ~irrelevant.
DIVIDEND_YIELD = 0.013       # approx SPX dividend yield carry; likewise near-irrelevant 0DTE.
SETTLEMENT_TIME = _dt.time(16, 0)   # PM-settlement instant for 0DTE SPXW.
_HOURS_PER_YEAR = 365.25 * 24.0

# Near-ATM half-window (in strikes) used to recover the forward. A small fixed window of
# strikes around the rough ATM is most reliable for C - P (deep ITM/OTM legs are noisy).
# This is a robustness choice, not a fitted parameter — widening or narrowing it does not
# change which trades the strategy takes; it only affects spot-recon precision.
_FWD_HALF_WINDOW_STRIKES = 10


# --------------------------------------------------------------------------- #
# Time-to-expiry
# --------------------------------------------------------------------------- #
def time_to_expiry_years(minute: pd.Timestamp, expiration: _dt.date) -> float:
    """Calendar-year fraction from `minute` to the PM-settlement instant on `expiration`.

    0DTE => same-day; T is a few hours expressed as a year fraction. Floored at a tiny
    positive epsilon so BS does not divide by zero at/after the settlement minute.
    """
    settle = _dt.datetime.combine(expiration, SETTLEMENT_TIME)
    m = minute.to_pydatetime() if isinstance(minute, pd.Timestamp) else minute
    hours = (settle - m).total_seconds() / 3600.0
    return max(hours, 1e-6) / _HOURS_PER_YEAR


# --------------------------------------------------------------------------- #
# Black-Scholes (forward form), used both ways: price->IV and IV->delta.
# --------------------------------------------------------------------------- #
def _norm_cdf(x: float) -> float:
    # Abramowitz-Stegun-free: use erf via math for a single scalar.
    import math
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    import math
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(
    spot: float, strike: float, t_years: float, vol: float, is_call: bool,
    r: float = RISK_FREE_RATE, q: float = DIVIDEND_YIELD,
) -> float:
    """Black-Scholes-Merton price (continuous dividend yield q)."""
    import math
    if t_years <= 0 or vol <= 0 or spot <= 0:
        # Intrinsic at/after expiry or degenerate inputs.
        fwd = spot * math.exp((r - q) * max(t_years, 0.0))
        intrinsic = max(fwd - strike, 0.0) if is_call else max(strike - fwd, 0.0)
        return intrinsic * math.exp(-r * max(t_years, 0.0))
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * vol * vol) * t_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    disc_q = math.exp(-q * t_years)
    disc_r = math.exp(-r * t_years)
    if is_call:
        return spot * disc_q * _norm_cdf(d1) - strike * disc_r * _norm_cdf(d2)
    return strike * disc_r * _norm_cdf(-d2) - spot * disc_q * _norm_cdf(-d1)


def bs_delta(
    spot: float, strike: float, t_years: float, vol: float, is_call: bool,
    r: float = RISK_FREE_RATE, q: float = DIVIDEND_YIELD,
) -> float:
    """Black-Scholes spot delta (dividend-adjusted)."""
    import math
    if t_years <= 0 or vol <= 0 or spot <= 0:
        # Degenerate at expiry: delta is a step (0/1 for calls, 0/-1 for puts).
        itm = (spot > strike) if is_call else (spot < strike)
        if not itm:
            return 0.0
        return 1.0 if is_call else -1.0
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * vol * vol) * t_years) / (vol * sqrt_t)
    disc_q = math.exp(-q * t_years)
    if is_call:
        return disc_q * _norm_cdf(d1)
    return disc_q * (_norm_cdf(d1) - 1.0)


def implied_vol_from_mid(
    mid: float, spot: float, strike: float, t_years: float, is_call: bool,
    r: float = RISK_FREE_RATE, q: float = DIVIDEND_YIELD,
    lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-5, max_iter: int = 100,
) -> float:
    """Invert BS for implied vol by bisection on a single option's mid.

    Returns NaN if the mid is not arbitrage-consistent (below intrinsic or above the
    no-vol-bounded max), which is common for deep OTM 0DTE legs quoted at $0.025.
    """
    import math
    if not np.isfinite(mid) or mid <= 0 or t_years <= 0 or spot <= 0:
        return float("nan")
    # Price is monotincreasing in vol; check the bracket actually contains the target.
    p_lo = bs_price(spot, strike, t_years, lo, is_call, r, q)
    p_hi = bs_price(spot, strike, t_years, hi, is_call, r, q)
    if mid <= p_lo or mid >= p_hi:
        return float("nan")
    a, b = lo, hi
    for _ in range(max_iter):
        m = 0.5 * (a + b)
        pm = bs_price(spot, strike, t_years, m, is_call, r, q)
        if abs(pm - mid) < tol:
            return m
        if pm < mid:
            a = m
        else:
            b = m
    return 0.5 * (a + b)


# --------------------------------------------------------------------------- #
# Forward / spot recovery via put-call parity
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SpotRecon:
    """Recovered forward & spot for one minute, plus diagnostics."""

    minute: pd.Timestamp
    forward: float          # implied forward F
    spot: float             # F discounted by (r - q) carry
    n_strikes_used: int     # how many near-ATM C-P pairs went into the median
    fwd_dispersion: float   # IQR of per-strike F estimates (lower = more consistent)


def _mid(bid: pd.Series, ask: pd.Series) -> pd.Series:
    """NBBO mid, NaN where the quote is not a usable two-sided quote.

    A cheap option legitimately quotes bid=0 / ask=0.05 — that IS a real quote, so we
    require only a positive ask and a non-negative bid no greater than the ask. Filtering
    on bid>0 (as a naive guard would) wrongly discards every deep-OTM 0DTE leg and starved
    the put-call-parity spot recovery on quiet days.
    """
    m = (bid + ask) / 2.0
    m = m.where((ask > 0) & (bid >= 0) & (ask >= bid))
    return m


def recover_forward_spot(
    minute_snap: pd.DataFrame,
    minute: pd.Timestamp,
    expiration: _dt.date,
    r: float = RISK_FREE_RATE,
    q: float = DIVIDEND_YIELD,
) -> SpotRecon | None:
    """Recover the implied forward & spot for one minute from a same-expiry chain snap.

    `minute_snap` must contain rows for ONE minute & ONE expiration with columns
    strike, right ('CALL'/'PUT'), bid, ask. Returns None if there are not enough
    valid near-ATM C-P pairs to recover the forward.

    Method (no tuning):
      C - P = (F - K) e^{-rT}  =>  F = K + (C - P) e^{rT}.
      We compute F at every strike where BOTH legs have a valid mid, take a rough ATM
      from where |C - P| is smallest, keep a fixed window of strikes around it, and
      return the MEDIAN F (robust to a few stale legs). Spot = F e^{-(r-q)T}.
    """
    snap = minute_snap.copy()
    snap["mid"] = _mid(snap["bid"], snap["ask"])
    calls = snap[snap["right"] == "CALL"].set_index("strike")["mid"]
    puts = snap[snap["right"] == "PUT"].set_index("strike")["mid"]
    common = calls.index.intersection(puts.index)
    if len(common) < 3:
        return None

    t = time_to_expiry_years(minute, expiration)
    disc = np.exp(r * t)  # e^{rT}: pulls the discounted (F-K) back to undiscounted.

    cp = pd.DataFrame({"C": calls[common], "P": puts[common]}).dropna()
    if len(cp) < 3:
        return None
    cp["CmP"] = cp["C"] - cp["P"]
    cp["F"] = cp.index.to_numpy(dtype=float) + cp["CmP"].to_numpy() * disc

    # Rough ATM = strike where |C - P| is smallest (parity crosses zero at the forward).
    atm_strike = cp["CmP"].abs().idxmin()
    strikes_sorted = np.sort(cp.index.to_numpy(dtype=float))
    step = np.median(np.diff(strikes_sorted)) if len(strikes_sorted) > 1 else 5.0
    half = _FWD_HALF_WINDOW_STRIKES * step
    window = cp[(cp.index >= atm_strike - half) & (cp.index <= atm_strike + half)]
    if window.empty:
        window = cp

    f_vals = window["F"].to_numpy()
    forward = float(np.median(f_vals))
    spot = forward * float(np.exp(-(r - q) * t))
    q75, q25 = np.percentile(f_vals, [75, 25]) if len(f_vals) >= 2 else (forward, forward)
    return SpotRecon(
        minute=minute,
        forward=forward,
        spot=spot,
        n_strikes_used=int(len(window)),
        fwd_dispersion=float(q75 - q25),
    )


# --------------------------------------------------------------------------- #
# Per-strike delta for a whole minute snapshot
# --------------------------------------------------------------------------- #
def per_strike_delta(
    minute_snap: pd.DataFrame,
    minute: pd.Timestamp,
    expiration: _dt.date,
    spot: float,
    r: float = RISK_FREE_RATE,
    q: float = DIVIDEND_YIELD,
) -> pd.DataFrame:
    """Per-strike recovered IV & delta for one minute snapshot.

    Returns a frame indexed like `minute_snap` rows (strike, right) with columns
    mid, iv, delta. IV is per-strike BS inversion of the mid; delta is BS spot delta.
    Rows whose mid is not arb-consistent get NaN iv & a degenerate-at-expiry delta only
    if T<=0; otherwise NaN delta (we refuse to invent a delta without an IV).
    """
    snap = minute_snap.copy()
    snap["mid"] = _mid(snap["bid"], snap["ask"])
    t = time_to_expiry_years(minute, expiration)

    ivs, deltas = [], []
    for _, row in snap.iterrows():
        is_call = row["right"] == "CALL"
        strike = float(row["strike"])
        mid = float(row["mid"]) if np.isfinite(row["mid"]) else float("nan")
        iv = implied_vol_from_mid(mid, spot, strike, t, is_call, r, q)
        ivs.append(iv)
        if np.isfinite(iv):
            deltas.append(bs_delta(spot, strike, t, iv, is_call, r, q))
        else:
            deltas.append(float("nan"))
    out = snap[["strike", "right", "mid"]].copy()
    out["iv"] = ivs
    out["delta"] = deltas
    return out
