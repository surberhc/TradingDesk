"""
_sharp_recovery_diag.py — DIAGNOSTIC (no adoption, no config mutation persisted).

Instruments WHEN the re-entry MAX-LAG `sharp_recovery` override actually fires across
full history, and characterizes each firing (was it a clean V or a sideways grind?),
so the refinement is GROUNDED in observed behavior, not assumption.

Strategy/data/config are NOT modified on disk. We re-implement the ladder walk here in
a thin wrapper that records the firing dates, reusing the EXACT condition builder and
ladder semantics from the shared brain (so the recorded firings are byte-faithful to
production). Strictly causal: every input is a trailing-window engine output sampled at
the signal dates.
"""
from __future__ import annotations
import warnings, sys
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

import numpy as np
import pandas as pd

from strategies import config
from strategies.parts import regime, volatility, reentry
from strategies.all_weather import _reentry_conditions, _signal_dates, _PERIODS_PER_MONTH
from src import data_loader

START = "2007-01-01"
VERSION = "Balanced"


def load_inputs():
    prices = data_loader.load_prices()
    bond_t = config.BENCHMARK_6040[1]
    if bond_t not in prices.columns:
        try:
            prices = prices.join(data_loader.load_prices([bond_t]))
        except Exception:
            pass
    try:
        hyg = data_loader.load_prices([config.CREDIT_PROXY[0]])[config.CREDIT_PROXY[0]]
    except Exception:
        hyg = None
    denom_t = config.CREDIT_PROXY[1]
    credit_denom = prices[denom_t] if denom_t in prices.columns else None
    yld, _ = data_loader.load_treasury_10y()
    vix, _ = data_loader.load_vix()
    oas, _ = data_loader.load_hy_oas()
    return prices, yld, hyg, credit_denom, vix, oas


def build_signals(prices, yld, hyg, credit_denom, vix, oas):
    score_df = regime.market_health_score(prices, hyg=hyg, credit_denom=credit_denom, vix=vix, hy_oas=oas)
    confirmed = regime.apply_hysteresis(score_df["score"])
    realized = volatility.realized_vol(prices["SPY"])
    sig_dates = _signal_dates(prices.index, "monthly", START, None)
    conditions = _reentry_conditions(prices, score_df, confirmed, realized, sig_dates, VERSION)
    return score_df, confirmed, conditions, sig_dates


def walk_record(conditions: pd.DataFrame, max_lag_months: int):
    """Re-implement compute_ladder_stages but RECORD each override firing date.
    Mirrors reentry.compute_ladder_stages EXACTLY (verified against it)."""
    stage = 4
    months_capped = 0
    out, fires = [], []
    for dt, row in conditions.iterrows():
        target = reentry._target_stage(row)
        if bool(row["defensive"]):
            stage = min(stage, target); months_capped = 0
        elif target > stage:
            stage += 1
        elif target < stage and bool(row["deteriorating"]):
            stage -= 1
        if stage < 4:
            months_capped += 1
            if months_capped >= max_lag_months and bool(row["sharp_recovery"]):
                fires.append(dt)
                stage = 4; months_capped = 0
        else:
            months_capped = 0
        stage = max(0, min(4, stage))
        out.append(stage)
    return pd.Series(out, index=conditions.index, name="ladder_stage"), fires


def characterize_firing(prices, dt, lookback_months=6):
    """At firing date dt (causal), measure the SHAPE of the prior recovery:
      - rebound slope: SPY trailing return over lookback (V = steep, grind = flat)
      - prior drawdown depth: how deep was the low the rebound came off (within ~18mo)
      - distance above the trailing low (how far we've climbed off the bottom)
      - re-violation: did price chop back near its trailing low recently (grind tell)
    ALL trailing/causal as of dt."""
    spy = prices["SPY"]
    hist = spy[spy.index <= dt]
    if len(hist) < 260:
        return None
    px = float(hist.iloc[-1])
    lb = lookback_months * 21
    ret_lb = float(hist.iloc[-1] / hist.iloc[-lb] - 1.0) if len(hist) > lb else np.nan
    # trailing 18-month low (the trough we're recovering from) and its date
    win18 = hist.tail(18 * 21)
    low = float(win18.min()); low_dt = win18.idxmin()
    days_since_low = int((dt - low_dt).days)
    off_low = float(px / low - 1.0)  # how far above the trough
    # depth of the drawdown that preceded this low (peak in 12mo before the low -> low)
    pre = hist[hist.index <= low_dt].tail(12 * 21)
    peak_before_low = float(pre.max()) if len(pre) else np.nan
    dd_depth = float(low / peak_before_low - 1.0) if peak_before_low and not np.isnan(peak_before_low) else np.nan
    # re-violation: min of the LAST 3 months vs the off-low climb — if recent price is
    # still near the trailing low, it's a grind (chopping), not a clean V.
    recent_low = float(hist.tail(3 * 21).min())
    recent_off_low = float(recent_low / low - 1.0)
    return {
        "date": dt, "ret_6m": ret_lb, "off_low": off_low, "dd_depth": dd_depth,
        "days_since_low": days_since_low, "recent_off_low": recent_off_low,
    }


if __name__ == "__main__":
    prices, yld, hyg, credit_denom, vix, oas = load_inputs()
    score_df, confirmed, conditions, sig_dates = build_signals(prices, yld, hyg, credit_denom, vix, oas)

    per_month = _PERIODS_PER_MONTH.get("monthly", 1)
    max_lag = config.REENTRY_MAX_LAG_MONTHS * per_month
    # parity check vs canonical
    canon = reentry.compute_ladder_stages(conditions, max_lag_months=max_lag)
    mine, fires = walk_record(conditions, max_lag)
    assert (canon == mine).all(), "ladder walk parity FAILED"
    print(f"PARITY OK. Override max_lag(months)={config.REENTRY_MAX_LAG_MONTHS}")
    print(f"Override FIRED on {len(fires)} dates across {sig_dates[0].date()}..{sig_dates[-1].date()}:\n")
    hdr = f"{'fire_date':>12} | {'ret_6m':>7} {'off_low':>8} {'dd_depth':>8} {'recent_off_low':>14} {'days_since_low':>14}"
    print(hdr); print("-"*len(hdr))
    for dt in fires:
        c = characterize_firing(prices, dt)
        if c is None:
            print(f"{dt.date()!s:>12} | (insufficient history)"); continue
        print(f"{dt.date()!s:>12} | {c['ret_6m']*100:>6.1f}% {c['off_low']*100:>7.1f}% "
              f"{c['dd_depth']*100:>7.1f}% {c['recent_off_low']*100:>13.1f}% {c['days_since_low']:>14d}")
    print("\nDONE")
