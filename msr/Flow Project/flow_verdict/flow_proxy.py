"""
flow_proxy.py
=============
Transparent vol-control + CTA-trend "systematic flow positioning" proxy.

Reconstructs a Bullish / Neutral / Bearish daily positioning state from PRICE
ALONE (Tier A/B: realized vol + moving average + momentum). It is the free,
reproducible stand-in for the newsletter's proprietary `regime_flow_risk`
("Systematic Flow Risk") label, used to test whether the *mechanism* the vendor
claims to capture carries edge across multiple market regimes (2008-2026).

WHY THESE COMPONENTS (mechanism the vendor describes):
  - vol-control / risk-parity funds target a constant portfolio volatility, so
    their equity exposure scales ~ 1 / realized_vol. When realized vol spikes
    they mechanically de-risk  -> captured here by `volpct` (trailing-year rank
    of 21d realized vol); a high rank => "flows are selling".
  - CTA / trend funds are long when price trends up, short/flat when it trends
    down -> captured by price vs the 200d MA and 12m-minus-1m momentum.

POINT-IN-TIME: every feature uses only data up to and including day t (rolling
windows, causal percentile rank). No look-ahead. Forward returns are computed
separately by the caller for evaluation only.

See FLOW_VERDICT.md for the full derivation, results, and the keep/drop verdict.
"""

import numpy as np
import pandas as pd

# --- default parameters (the knobs the backtester will want to sweep) ---------
DEFAULTS = dict(
    ma_len=200,        # trend filter: simple MA length (days)
    mom_long=252,      # momentum lookback start (days back) ~ 12 months
    mom_skip=21,       # momentum lookback end (days back)   ~ skip last 1 month
    rvol_win=21,       # realized-vol window (days) ~ 1 month
    vol_rank_win=252,  # trailing window for the realized-vol percentile rank
    vol_top=0.80,      # rvol rank above this => "vol spike" => de-risk -> Bearish
    vol_calm=0.70,     # rvol rank below this => "calm" (required for Bullish)
)


def compute_features(px: pd.Series, p: dict = DEFAULTS) -> pd.DataFrame:
    """Causal feature frame from a price series `px` (DatetimeIndex, adj close)."""
    px = px.sort_index().astype(float)
    logret = np.log(px / px.shift(1))
    rvol = logret.rolling(p["rvol_win"]).std() * np.sqrt(252) * 100.0      # ann %
    ma = px.rolling(p["ma_len"]).mean()
    mom = px.shift(p["mom_skip"]) / px.shift(p["mom_long"]) - 1.0          # 12m-1m
    # causal percentile rank of today's rvol within the trailing window:
    volpct = rvol.rolling(p["vol_rank_win"]).apply(
        lambda s: (s.iloc[-1] >= s).mean(), raw=False)
    return pd.DataFrame({"px": px, "rvol": rvol, "ma": ma, "mom": mom,
                         "volpct": volpct})


def classify(feat: pd.DataFrame, p: dict = DEFAULTS) -> pd.Series:
    """Map features -> {'Bullish','Neutral','Bearish'} positioning state.

    Bearish  : downtrend (px < MA) OR vol spike (rvol rank > vol_top)
    Bullish  : uptrend (px > MA AND momentum > 0) AND calm (rvol rank < vol_calm)
    Neutral  : everything in between
    Bearish is evaluated first (de-risk dominates).
    """
    up = (feat["px"] > feat["ma"]) & (feat["mom"] > 0)
    dn = feat["px"] < feat["ma"]
    spike = feat["volpct"] > p["vol_top"]
    calm = feat["volpct"] < p["vol_calm"]
    state = np.where(dn | spike, "Bearish",
            np.where(up & calm, "Bullish", "Neutral"))
    return pd.Series(state, index=feat.index, name="proxy")


def build(px: pd.Series, p: dict = DEFAULTS) -> pd.DataFrame:
    """Convenience: features + 'proxy' column, warm-up rows dropped."""
    feat = compute_features(px, p)
    feat["proxy"] = classify(feat, p)
    return feat.dropna()


def forward_returns(px: pd.Series, horizons=(1, 5, 10, 20)) -> pd.DataFrame:
    """Simple % forward returns for evaluation (NOT a model input)."""
    px = px.sort_index().astype(float)
    out = {f"f{h}": (px.shift(-h) / px - 1.0) * 100.0 for h in horizons}
    return pd.DataFrame(out)
