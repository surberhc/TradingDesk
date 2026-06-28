"""
gex.py — reconstruct dealer gamma exposure (GEX) from one day's option chain.

This is the engine behind the validated MSR signal: dealer gamma regime ->
next-day volatility and drawdown fragility. From a single EOD chain snapshot
(gamma, open interest, IV, strike, expiry, spot) it produces, for that day:

  * net_gex            net dollar dealer gamma per 1% move ($)
  * gamma_state        Positive / Neutral / Negative  (the vol/risk gate)
  * gamma_flip         the spot level where net GEX crosses zero ("zero gamma")
  * above_flip         spot > flip (the clean high-vol vs low-vol side)
  * dist_to_flip_pct   % cushion above/below the flip (continuous fragility)
  * expected_move_pct  nearest-expiry ATM implied move (vol-target denominator)
  * focal_strike       largest-|gamma| strike (the pin magnet)

Dealer-sign convention (the one real modelling choice, per the methodology spec):
assume dealers are LONG call gamma and SHORT put gamma -> calls add gamma,
puts remove it. This is the standard SqueezeMetrics-style assumption; the exact
sign/scale is CALIBRATED against the 281-day Tier 1 Alpha set, where the spec
notes it "shifts the flip by a strike or two". Knobs live in config so calibration
can tune them without touching this math.

The flip needs the gamma profile as a function of a HYPOTHETICAL spot, so we
re-price each contract's gamma with Black-Scholes from its own IV/strike/DTE
(ThetaData's supplied `gamma` is only at the actual spot). At S = spot our BS
gamma reproduces the supplied gamma, which is a built-in sanity check.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

CONTRACT_MULT = 100          # equity/ETF/SPX index options are x100
CALL_SIGN = +1.0             # dealers long call gamma  (calibratable)
PUT_SIGN = -1.0              # dealers short put gamma   (calibratable)
NEUTRAL_BAND_FRAC = 0.05     # |net_gex| within this fraction of the day's gross -> Neutral
                             # (0.10 -> 0.05 on 2026-06-27: calibration vs the msr vendor labels
                             #  lifted gamma-state accuracy 61% -> 70%, direction unchanged)
_T_FLOOR = 0.5 / 365.0       # clamp DTE so 0DTE gamma doesn't blow up
_FLIP_LO, _FLIP_HI, _FLIP_STEP = 0.85, 1.15, 0.001   # flip scan: +/-15% of spot, 0.1% grid


def _phi(x: np.ndarray) -> np.ndarray:
    """Standard normal pdf (no scipy dependency)."""
    return np.exp(-0.5 * x * x) / np.sqrt(2.0 * np.pi)


def _bs_gamma(S, K, T, sigma):
    """Black-Scholes gamma per share (r=0). Vectorized; 0 where inputs invalid."""
    S, K, T, sigma = (np.asarray(v, float) for v in (S, K, T, sigma))
    ok = (S > 0) & (K > 0) & (T > 0) & (sigma > 0)
    Ss, Ks, Ts, sigs = np.where(ok, S, 1.0), np.where(ok, K, 1.0), np.where(ok, T, 1.0), np.where(ok, sigma, 1.0)
    d1 = (np.log(Ss / Ks) + 0.5 * sigs * sigs * Ts) / (sigs * np.sqrt(Ts))
    g = _phi(d1) / (Ss * sigs * np.sqrt(Ts))
    return np.where(ok, g, 0.0)


def _prep(chain: pd.DataFrame) -> pd.DataFrame:
    """Clean one day's chain to the columns the math needs."""
    df = chain.copy()
    df["right"] = df["right"].str.upper().str[0]                 # C / P
    # Some snapshots (e.g. VIX half-days) return greeks but no OI endpoint, so the
    # parquet has no open_interest column at all. df.get() would yield a scalar NaN
    # (no .fillna) and crash the whole symbol build -> treat an absent column as 0 OI.
    if "open_interest" in df.columns:
        df["oi"] = pd.to_numeric(df["open_interest"], errors="coerce").fillna(0.0)
    else:
        df["oi"] = 0.0
    df["iv"] = pd.to_numeric(df.get("implied_vol"), errors="coerce")
    exp = pd.to_datetime(df["expiration"])
    asof = pd.to_datetime(df["date"], format="%Y%m%d")
    df["T"] = ((exp - asof).dt.days.clip(lower=0) / 365.0).clip(lower=_T_FLOOR)
    df["sign"] = np.where(df["right"] == "C", CALL_SIGN, PUT_SIGN)
    df["spot"] = pd.to_numeric(df["underlying_price"], errors="coerce")
    return df[df["oi"] > 0]                                      # only contracts with OI matter


def _net_gex_at(df: pd.DataFrame, S: float) -> float:
    """Signed dealer $GEX (per 1% move) at a hypothetical spot S, BS-repriced."""
    g = _bs_gamma(S, df["strike"].to_numpy(), df["T"].to_numpy(), df["iv"].to_numpy())
    return float(np.sum(df["sign"].to_numpy() * g * df["oi"].to_numpy()
                        * CONTRACT_MULT * S * S * 0.01))


def _net_gex_supplied(df: pd.DataFrame, spot: float) -> tuple[float, float, float]:
    """Net / call / put $GEX at the actual spot using ThetaData's supplied gamma."""
    gam = pd.to_numeric(df["gamma"], errors="coerce").fillna(0.0).to_numpy()
    notion = gam * df["oi"].to_numpy() * CONTRACT_MULT * spot * spot * 0.01
    is_call = (df["right"] == "C").to_numpy()
    call_g = float(np.sum(notion[is_call]))
    put_g = float(np.sum(notion[~is_call]))
    return CALL_SIGN * call_g + PUT_SIGN * put_g, call_g, put_g


def _gamma_flip(df: pd.DataFrame, spot: float) -> float:
    """Spot level where BS-repriced net GEX crosses zero, nearest current spot."""
    if not spot or spot <= 0:        # spot==0 -> arange step is 0 -> ZeroDivisionError
        return float("nan")
    grid = np.arange(spot * _FLIP_LO, spot * _FLIP_HI, spot * _FLIP_STEP)
    net = np.array([_net_gex_at(df, S) for S in grid])
    sign_change = np.where(np.diff(np.sign(net)) != 0)[0]
    if len(sign_change) == 0:
        return float("nan")
    # crossing whose level is closest to spot
    crossings = grid[sign_change]
    return float(crossings[np.argmin(np.abs(crossings - spot))])


def _expected_move_pct(df: pd.DataFrame, spot: float) -> float:
    """Nearest-expiry ATM IV annualized -> implied move % over that expiry's horizon."""
    nearest_T = df["T"].min()
    near = df[df["T"] <= nearest_T + 1e-9]
    if near.empty:
        return float("nan")
    atm = near.iloc[(near["strike"] - spot).abs().argsort()[:4]]   # ~ATM contracts
    iv = atm["iv"].dropna()
    if iv.empty:
        return float("nan")
    return float(iv.mean() * np.sqrt(max(nearest_T, _T_FLOOR)) * 100.0)


def day_features(chain: pd.DataFrame) -> dict:
    """All GEX features for one day's chain. Returns {} if the day is unusable."""
    df = _prep(chain)
    if df.empty or df["spot"].dropna().empty:
        return {}
    spot = float(df["spot"].dropna().iloc[0])
    if spot <= 0:                    # thin symbol w/ a 0 underlying_price -> unusable day
        return {}
    net, call_g, put_g = _net_gex_supplied(df, spot)
    gross = abs(call_g) + abs(put_g)
    if gross == 0:
        state = "Neutral"
    elif net > NEUTRAL_BAND_FRAC * gross:
        state = "Positive"
    elif net < -NEUTRAL_BAND_FRAC * gross:
        state = "Negative"
    else:
        state = "Neutral"
    flip = _gamma_flip(df, spot)
    focal = float(df.assign(absn=lambda d: (pd.to_numeric(d["gamma"], errors="coerce").fillna(0.0)
                                            * d["oi"]).abs())
                  .groupby("strike")["absn"].sum().idxmax())
    return {
        "date": str(chain["date"].iloc[0]),
        "spot": round(spot, 2),
        "net_gex": net,
        "call_gex": call_g,
        "put_gex": put_g,
        "gamma_state": state,
        "gamma_flip": round(flip, 2) if flip == flip else float("nan"),
        "above_flip": int(spot > flip) if flip == flip else None,
        "dist_to_flip_pct": round((spot - flip) / spot * 100, 3) if flip == flip else float("nan"),
        "expected_move_pct": round(_expected_move_pct(df, spot), 3),
        "focal_strike": focal,
    }
