r"""
ddoi_gamma.py — DDOI-style INFERRED-dealer-direction gamma signal from the tape.

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.
ASCII-only console output.

WHY THIS EXISTS
---------------
Our production daily gamma signal (datacollector/features/gex.py) signs every
option's gamma with a STATIC dealer-position assumption:
    dealers are LONG call gamma (CALL_SIGN=+1), SHORT put gamma (PUT_SIGN=-1).
It reproduces the Tier-1-Alpha (vendor) gamma_state ~70% of the time on the SPX
root; the residual gap is a genuine METHOD difference on the NEGATIVE-gamma side:
vendors INFER dealer trade direction (DDOI = dealer directional open interest)
from the tape rather than assuming it.

This module builds that inferred-direction method off the SPXW 1-minute warehouse:

  1. TRADE-DIRECTION CLASSIFIER (Lee-Ready quote rule, tick-rule fallback).
     For every 1-minute TRADE bar on a contract, compare the bar's trade price
     (VWAP) to the prevailing NBBO midpoint that minute:
         price > mid  -> BUYER-initiated   (customer buys  -> dealer SELLS -> dealer SHORT)
         price < mid  -> SELLER-initiated  (customer sells -> dealer BUYS  -> dealer LONG)
         price ~ mid  -> tick rule: compare to the previous trade price on that contract
                         (uptick=buy, downtick=sell); carry the last non-mid sign if flat.
     This is pure OBSERVATION of real historical prints vs the real NBBO. No
     threshold here is tuned to hit a target -- the mid and the tick are the
     textbook Lee-Ready definition.

  2. PER-CONTRACT INFERRED DEALER SIGN (the DDOI estimate).
     Net signed customer volume over the day per contract:
         dealer_signed_vol = -sum(customer_side * volume)
     (customer buy -> dealer short that contract -> NEGATIVE dealer position).
     sign(dealer_signed_vol) is the inferred dealer position sign for that contract:
         +1 dealers net LONG this contract's gamma (they ADD gamma / stabilize)
         -1 dealers net SHORT this contract's gamma (they REMOVE gamma / amplify)
     This REPLACES the static +1 call / -1 put assumption with a per-contract,
     tape-inferred sign.

  3. DDOI NET GEX for the day.
     net_gex_ddoi = sum_over_contracts( dealer_sign * gamma * OI * 100 * S^2 * 0.01 )
     using the EOD chain's supplied per-contract gamma and open interest (the same
     inputs gex.py uses), but with the INFERRED dealer_sign in place of the static one.
     Contracts that never traded intraday have no inferred sign; they FALL BACK to
     the static call/put sign (an explicit, stated fallback -- not a tuned choice).
     Then the SAME thresholding as gex.py maps net_gex -> Positive/Neutral/Negative.

WHAT IS AND IS NOT A KNOB (anti-curve-fit)
------------------------------------------
  * The Lee-Ready mid/tick classification is OBSERVATION -> always allowed.
  * NEUTRAL_BAND_FRAC and the static fallback sign are inherited VERBATIM from
    production gex.py so the ONLY thing that changes between the static baseline and
    the DDOI method is the per-contract dealer SIGN. That isolates the method effect.
  * Nothing here is fit to make the output match the vendor. If a future edit tunes
    a classification threshold to lift the match rate, that is curve-fitting -> STOP.

DATA
----
  * Intraday tape + NBBO: SPXW 1-minute warehouse via s5_intraday_data (read-only).
  * EOD chain (gamma, open_interest, underlying_price): the ThetaData EOD warehouse
    C:/TradingDesk-Local/warehouse/raw/options/SPXW/{YYYYMMDD}.parquet (read-only).
  * We aggregate the tape across ALL expirations that traded on day d and join to the
    full EOD SPXW chain, mirroring gex.py's use of the whole chain's OI.
"""

from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

import s5_intraday_data as s5

# --------------------------------------------------------------------------- #
# Constants inherited VERBATIM from datacollector/features/gex.py so the DDOI
# method differs from the static baseline in ONE place only: the dealer sign.
# --------------------------------------------------------------------------- #
CONTRACT_MULT = 100
CALL_SIGN = +1.0            # static fallback: dealers long call gamma
PUT_SIGN = -1.0             # static fallback: dealers short put gamma
NEUTRAL_BAND_FRAC = 0.05    # |net_gex| within this fraction of gross -> Neutral

EOD_WAREHOUSE = r"C:\TradingDesk-Local\warehouse\raw\options"
SYMBOL = "SPXW"

# Mid-band for the quote rule: a print within this fraction of the half-spread of the
# mid is treated as "at mid" -> tick-rule fallback. This is the standard Lee-Ready
# treatment of at-the-mid prints, NOT a tuned signal knob (it only routes a print to
# the tick rule instead of the quote rule; it does not change any target).
_MID_EPS_FRAC = 0.25        # within 25% of the half-spread of mid == "at the mid"


# --------------------------------------------------------------------------- #
# Typed container
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DdoiDay:
    """One day's DDOI result plus the diagnostics needed to audit it."""

    date: _dt.date
    spot: float
    net_gex_ddoi: float
    net_gex_static: float
    gamma_state_ddoi: str
    gamma_state_static: str
    n_contracts_scored: int      # EOD contracts with OI>0 that we signed
    n_inferred: int              # of those, how many got a TAPE-inferred sign
    frac_inferred: float         # n_inferred / n_contracts_scored
    total_classified_volume: int # intraday contracts we classified buy/sell


# --------------------------------------------------------------------------- #
# 1. Trade-direction classifier (Lee-Ready quote rule + tick fallback)
# --------------------------------------------------------------------------- #
def classify_day_trades(day: _dt.date) -> pd.DataFrame:
    """Sign every intraday trade bar buyer/seller-initiated for day `day`.

    Uses the full-day tape (ALL expirations that traded), each bar's VWAP as the
    trade price, and the per-minute forward-filled NBBO mid at that bar's minute.

    Returns a tidy frame with columns:
        expiration, strike, right, minute, volume, price, mid, cust_side
    where cust_side is +1 (buyer/customer-buy) or -1 (seller/customer-sell) or 0
    (unclassifiable: no NBBO mid AND no prior trade to tick off).

    Method (textbook Lee-Ready):
        * quote rule: price vs NBBO mid (buy if above, sell if below, tie -> tick)
        * tick rule (for at-mid or no-mid prints): vs the previous CLASSIFIED trade
          price on the SAME contract (uptick=buy, downtick=sell; flat -> carry last).
    """
    dd = s5.load_day(day)
    ohlc = dd.ohlc
    if ohlc.empty:
        return _empty_classified()

    # Bars: one row per (contract, minute) that traded. Price = VWAP (the true
    # volume-weighted trade price for that minute); fall back to close if VWAP is NaN.
    bars = ohlc.copy()
    bars["minute"] = bars["timestamp"].dt.floor("min")
    bars["price"] = pd.to_numeric(bars["vwap"], errors="coerce")
    bars["price"] = bars["price"].where(bars["price"].notna(),
                                        pd.to_numeric(bars["close"], errors="coerce"))
    bars["volume"] = pd.to_numeric(bars["volume"], errors="coerce").fillna(0.0)
    bars = bars[bars["volume"] > 0]
    if bars.empty:
        return _empty_classified()

    # NBBO mid per (contract, minute): reconstruct the forward-filled grid for the
    # WHOLE day (all expirations), then merge each bar to the mid at its own minute.
    nbbo = s5.nbbo_grid(day, quote=dd.quote)
    nbbo = nbbo[["expiration", "strike", "right", "minute", "bid", "ask"]].copy()
    nbbo["bid"] = pd.to_numeric(nbbo["bid"], errors="coerce")
    nbbo["ask"] = pd.to_numeric(nbbo["ask"], errors="coerce")
    valid = (nbbo["ask"] > 0) & (nbbo["bid"] >= 0) & (nbbo["ask"] >= nbbo["bid"])
    nbbo["mid"] = np.where(valid, (nbbo["bid"] + nbbo["ask"]) / 2.0, np.nan)
    nbbo["half_spread"] = np.where(valid, (nbbo["ask"] - nbbo["bid"]) / 2.0, np.nan)

    # s5 nbbo_grid stores `right` as CALL/PUT (from the intraday feed). Bars too.
    m = bars.merge(
        nbbo[["expiration", "strike", "right", "minute", "mid", "half_spread"]],
        on=["expiration", "strike", "right", "minute"], how="left",
    )

    # --- Quote rule ---------------------------------------------------------
    # A print is "at the mid" when |price - mid| <= _MID_EPS_FRAC * half_spread.
    px = m["price"].to_numpy(float)
    mid = m["mid"].to_numpy(float)
    hs = m["half_spread"].to_numpy(float)
    eps = np.where(np.isfinite(hs), _MID_EPS_FRAC * hs, np.nan)

    side = np.zeros(len(m), dtype=float)  # 0 = unresolved by the quote rule
    has_mid = np.isfinite(mid)
    above = has_mid & (px > mid + eps)
    below = has_mid & (px < mid - eps)
    side[above] = +1.0
    side[below] = -1.0
    # at-mid or no-mid prints stay 0 here and go to the tick rule below.

    m["cust_side"] = side

    # --- Tick rule fallback (per contract, chronological) -------------------
    # For rows still 0, sign by the tick vs the previous trade price on the SAME
    # contract; if the price is flat, carry the last resolved sign forward.
    m = m.sort_values(["expiration", "strike", "right", "minute"]).reset_index(drop=True)
    m["cust_side"] = _apply_tick_rule(m)

    return m[["expiration", "strike", "right", "minute", "volume", "price", "mid", "cust_side"]]


def _apply_tick_rule(m: pd.DataFrame) -> pd.Series:
    """Resolve remaining unresolved (cust_side==0) prints by the tick rule.

    Grouped per contract and walked in time: uptick vs the previous trade price is a
    buy, downtick a sell, flat carries the last resolved sign. Quote-rule-resolved
    rows are left as-is (they also seed the tick comparison for later flats).
    """
    out = m["cust_side"].to_numpy(float).copy()
    price = m["price"].to_numpy(float)
    keys = m[["expiration", "strike", "right"]].astype(str).agg("|".join, axis=1).to_numpy()

    last_price = {}
    last_sign = {}
    for i in range(len(out)):
        k = keys[i]
        p = price[i]
        if out[i] == 0.0:
            lp = last_price.get(k)
            if lp is not None and np.isfinite(lp) and np.isfinite(p):
                if p > lp:
                    out[i] = +1.0
                elif p < lp:
                    out[i] = -1.0
                else:
                    out[i] = last_sign.get(k, 0.0)  # flat -> carry
            else:
                out[i] = last_sign.get(k, 0.0)       # no prior print -> carry (0 if none)
        # update state with THIS print
        if np.isfinite(p):
            last_price[k] = p
        if out[i] != 0.0:
            last_sign[k] = out[i]
    return pd.Series(out, index=m.index)


def _empty_classified() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["expiration", "strike", "right", "minute", "volume", "price", "mid", "cust_side"]
    )


# --------------------------------------------------------------------------- #
# 2. Per-contract inferred dealer sign (the DDOI estimate)
# --------------------------------------------------------------------------- #
def dealer_sign_by_contract(classified: pd.DataFrame) -> pd.DataFrame:
    """Aggregate classified prints to one inferred DEALER position sign per contract.

    dealer_signed_vol = -sum(cust_side * volume)   (customer buy -> dealer short)
    dealer_sign = sign(dealer_signed_vol) in {-1, 0, +1}.

    Returns columns: expiration, strike, right, dealer_signed_vol, classified_vol,
    dealer_sign.  `right` is normalized to a single upper char (C/P) to match the EOD
    chain join in the GEX step.
    """
    if classified.empty:
        return pd.DataFrame(
            columns=["expiration", "strike", "right", "dealer_signed_vol",
                     "classified_vol", "dealer_sign"]
        )
    c = classified.copy()
    c["signed"] = c["cust_side"] * c["volume"]
    g = c.groupby(["expiration", "strike", "right"], sort=False).agg(
        cust_signed_vol=("signed", "sum"),
        classified_vol=("volume", "sum"),
    ).reset_index()
    # Dealer is the OTHER side of the customer flow.
    g["dealer_signed_vol"] = -g["cust_signed_vol"]
    g["dealer_sign"] = np.sign(g["dealer_signed_vol"])
    g["right"] = g["right"].astype(str).str.upper().str[0]  # CALL->C, PUT->P
    return g[["expiration", "strike", "right", "dealer_signed_vol",
              "classified_vol", "dealer_sign"]]


# --------------------------------------------------------------------------- #
# 3. EOD chain + DDOI net GEX + state
# --------------------------------------------------------------------------- #
def _eod_chain_path(day: _dt.date) -> str:
    return os.path.join(EOD_WAREHOUSE, SYMBOL, day.strftime("%Y%m%d") + ".parquet")


def load_eod_chain(day: _dt.date) -> pd.DataFrame | None:
    """Read the EOD SPXW chain for `day` (gamma, OI, bid/ask, underlying). Read-only.

    Returns a cleaned frame (right=C/P, oi>0 only, T floored) or None if unusable.
    Mirrors the columns datacollector/features/gex.py consumes.
    """
    p = _eod_chain_path(day)
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_parquet(p)
    except Exception:
        return None
    if df.empty:
        return None
    df = df.copy()
    df["right"] = df["right"].astype(str).str.upper().str[0]
    if "open_interest" in df.columns:
        df["oi"] = pd.to_numeric(df["open_interest"], errors="coerce").fillna(0.0)
    else:
        df["oi"] = 0.0
    df["gamma"] = pd.to_numeric(df.get("gamma"), errors="coerce").fillna(0.0)
    df["spot"] = pd.to_numeric(df.get("underlying_price"), errors="coerce")
    df["expiration"] = pd.to_datetime(df["expiration"]).dt.strftime("%Y-%m-%d")
    df = df[df["oi"] > 0]
    if df.empty or df["spot"].dropna().empty:
        return None
    return df


def _state_from_net(net: float, gross: float) -> str:
    if gross == 0:
        return "Neutral"
    if net > NEUTRAL_BAND_FRAC * gross:
        return "Positive"
    if net < -NEUTRAL_BAND_FRAC * gross:
        return "Negative"
    return "Neutral"


def ddoi_day(day: _dt.date) -> DdoiDay | None:
    """Full DDOI pipeline for one day. Returns None if the day is unusable.

    Static-baseline net_gex is computed here too (same chain, static call/put sign)
    so the DDOI-vs-static comparison uses the SAME inputs and differs only in sign.
    """
    chain = load_eod_chain(day)
    if chain is None:
        return None
    spot = float(chain["spot"].dropna().iloc[0])
    if spot <= 0:
        return None

    # Per-contract dollar gamma magnitude (unsigned), exactly as gex.py builds it.
    gam = chain["gamma"].to_numpy(float)
    oi = chain["oi"].to_numpy(float)
    dollar_gamma = gam * oi * CONTRACT_MULT * spot * spot * 0.01  # >= 0 per contract
    is_call = (chain["right"] == "C").to_numpy()

    # --- Static baseline sign (the current production assumption) -----------
    static_sign = np.where(is_call, CALL_SIGN, PUT_SIGN)
    net_static = float(np.sum(static_sign * dollar_gamma))
    gross = float(np.sum(np.abs(dollar_gamma)))
    state_static = _state_from_net(net_static, gross)

    # --- DDOI inferred sign from the tape -----------------------------------
    classified = classify_day_trades(day)
    dsign = dealer_sign_by_contract(classified)
    total_classified_vol = int(dsign["classified_vol"].sum()) if not dsign.empty else 0

    chain_key = chain.copy()
    chain_key["_row"] = np.arange(len(chain_key))
    merged = chain_key.merge(
        dsign[["expiration", "strike", "right", "dealer_sign"]],
        on=["expiration", "strike", "right"], how="left",
    )
    inferred = merged["dealer_sign"].to_numpy(float)  # NaN where no trade
    has_inf = np.isfinite(inferred) & (inferred != 0.0)
    # Fallback to static call/put sign where the tape gave no directional read.
    ddoi_sign = np.where(has_inf, inferred, static_sign)
    net_ddoi = float(np.sum(ddoi_sign * dollar_gamma))
    state_ddoi = _state_from_net(net_ddoi, gross)

    n_scored = int(len(chain))
    n_inf = int(has_inf.sum())
    return DdoiDay(
        date=day,
        spot=round(spot, 2),
        net_gex_ddoi=net_ddoi,
        net_gex_static=net_static,
        gamma_state_ddoi=state_ddoi,
        gamma_state_static=state_static,
        n_contracts_scored=n_scored,
        n_inferred=n_inf,
        frac_inferred=(n_inf / n_scored) if n_scored else 0.0,
        total_classified_volume=total_classified_vol,
    )
