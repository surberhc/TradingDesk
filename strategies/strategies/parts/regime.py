"""
regime.py — Regime Engine (PRIMARY). SPEC.md §4.

Owns the de-risking decision. Computes the Market Health Score (0-100) from three
EQUAL-WEIGHT components and maps it to a regime that sets the equity BAND:

  1. Broad equity trend (SPY): above 200-day MA, above 10-month MA, positive
     6-month total return, positive 200-day slope. Full marks when all hold.
  2. Breadth: % of sectors above their 200-day MA, plus RSP/SPY (equal- vs
     cap-weight) leadership trend. Inception-aware — a sector not yet trading is
     excluded from the breadth count, never counted as "below".
  3. Stress (LABELED PROXIES per DATA.md): credit = HYG/IEF ratio trend
     (HY-vs-Treasury proxy for credit spreads); volatility = SPY 63-day realized
     vol vs its own 200-day trend (proxy for VIX level-vs-trend). Calm = full marks.

Rate/inflation inputs do NOT belong here — they live in duration.py (SPEC §6).

Correctness (SPEC §3, §16): every window is TRAILING, so the score on date T uses
only data on/before T. There is no look-ahead; test_regime.py asserts this by
recomputing the score on truncated history and matching it.

Each component is scaled to [0, 1]; the score is their mean * 100. classify_regime
maps the score to a regime via config.REGIME_BANDS, and apply_hysteresis adds the
whipsaw controls (confirmation buffer, threshold dead-zone, immediate de-risk).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies import config
from strategies.parts import _gates as gates

# One "month" operationalized in trading days. This is a units conversion for
# turning month-based spec lookbacks (10-month MA, 6-month return) into daily
# windows — not a strategy tunable, so it lives here rather than in config.
TRADING_DAYS_PER_MONTH = 21


# ---------------------------------------------------------------------------
# Small trailing-window helpers (all causal: use only data up to each date)
# ---------------------------------------------------------------------------
def _rolling_slope_positive(price: pd.Series, window: int) -> pd.Series:
    """
    True on each date where the OLS slope of the trailing `window` closes is > 0.

    Vectorized least-squares slope of price against a time index 0..window-1.
    Windows containing any NaN (pre-inception) yield NaN and are treated as
    "unknown", never as a positive or negative signal.
    """
    vals = price.to_numpy(dtype=float)
    n = len(vals)
    slopes = np.full(n, np.nan)
    if n >= window:
        wins = np.lib.stride_tricks.sliding_window_view(vals, window)
        x = np.arange(window, dtype=float)
        x -= x.mean()  # center x so the intercept term drops out
        denom = float((x * x).sum())
        slopes[window - 1:] = (wins * x).sum(axis=1) / denom
    s = pd.Series(slopes, index=price.index)
    # NaN-safe: a NaN slope (pre-inception or a NaN price inside the window) must
    # read as unknown, not a false "not positive" — bare `>` reads NaN>0 as False,
    # not NaN. 2026-07-09 NaN-as-bearish fix (this cast previously undid the
    # NaN-safety the docstring above promises).
    return (s > 0).where(s.notna())


def _ratio_above_trend(numer: pd.Series, denom: pd.Series, window: int) -> pd.Series:
    """True where a price ratio sits above its own trailing moving average."""
    ratio = numer / denom
    # NaN-safe: a NaN ratio (missing today's print) must read as unknown, not a
    # false "not above trend" — bare `>` reads NaN>NaN as False, not NaN.
    # 2026-07-09 NaN-as-bearish fix.
    return (ratio > ratio.rolling(window).mean()).where(ratio.notna())


# ---------------------------------------------------------------------------
# The three components
# ---------------------------------------------------------------------------
def _trend_component(spy: pd.Series) -> pd.DataFrame:
    """Component 1 — broad equity trend on SPY. Returns sub-signals + [0,1] score."""
    ma_10m = spy.rolling(config.MA_MONTHS * TRADING_DAYS_PER_MONTH).mean()
    # fill_method=None: don't let pandas silently forward-fill a missing today's
    # price before computing the return (2026-07-09 NaN-as-bearish fix — the old
    # pad default turned a missing print into a flat 0% return, which then read
    # as bearish below).
    ret_6m = spy.pct_change(config.TREND_RETURN_MONTHS * TRADING_DAYS_PER_MONTH,
                             fill_method=None)

    sub = pd.DataFrame(
        {
            "trend_above_200d": gates.membership(spy, buffer=config.trend_margin("regime")),
            # NaN-safe: a NaN spy/ma_10m/ret_6m (today's print hasn't landed) must
            # read as unknown, not a false "not in trend" — bare `>` reads NaN>NaN
            # as False, not NaN. 2026-07-09 NaN-as-bearish fix.
            "trend_above_10m": (spy > ma_10m).astype(float).where(spy.notna() & ma_10m.notna()),
            "trend_ret_6m_pos": (ret_6m > 0).astype(float).where(ret_6m.notna()),
            "trend_slope_pos": _rolling_slope_positive(
                spy, config.SLOPE_LOOKBACK_DAYS
            ).astype(float),
        }
    )
    # Mask the warm-up period (before the slowest window has data) as undefined.
    warmup = max(config.MA_MONTHS * TRADING_DAYS_PER_MONTH, config.SLOPE_LOOKBACK_DAYS)
    sub.iloc[: warmup - 1] = np.nan
    sub["trend"] = sub.mean(axis=1)
    return sub


def _breadth_component(rsp: pd.Series, spy: pd.Series, sectors: pd.DataFrame) -> pd.DataFrame:
    """Component 2 — breadth. % sectors above 200d MA + RSP/SPY leadership."""
    # Inception-aware breadth: mean graded trend membership across *trading* sectors
    # (NaN sectors — no MA yet — are skipped by mean). sma -> 0/1 per sector = the
    # original "% of sectors above their 200d MA".
    member = gates.membership_frame(sectors, buffer=config.trend_margin("regime"))
    breadth_pct = member.mean(axis=1)

    rsp_leads = gates.membership(rsp / spy, buffer=config.trend_margin("regime"))

    sub = pd.DataFrame({"breadth_pct": breadth_pct, "breadth_rsp_leads": rsp_leads})
    sub["breadth"] = sub[["breadth_pct", "breadth_rsp_leads"]].mean(axis=1)
    return sub


def _stress_component(
    spy: pd.Series,
    hyg: pd.Series | None,
    credit_denom: pd.Series | None,
    vix: pd.Series | None = None,
    hy_oas: pd.Series | None = None,
) -> pd.DataFrame:
    """
    Component 3 — stress: volatility + credit, each calm when below its own trend.

    Volatility: REAL VIX vs its 200d trend if provided, else the SPY 63-day
    realized-vol proxy. Credit: REAL HY OAS vs its 200d trend if provided, else the
    HYG / credit_denom ratio proxy (credit_denom = IEF per config.CREDIT_PROXY, so
    the proxy is HYG/IEF — HY vs Treasury, capturing credit stress + flight-to-quality).
    """
    idx = spy.index

    # --- Volatility sub-signal ---
    if vix is not None:
        v = vix.reindex(idx).ffill()
        vol_calm = (v <= v.rolling(config.stress_ma_days()).mean()).astype(float)
    else:
        # fill_method=None + explicit NaN mask: same 2026-07-09 NaN-as-bearish fix
        # as the trend component — don't let a missing today's price read as calm/
        # not-calm by accident.
        realized = (spy.pct_change(fill_method=None).rolling(config.VOL_LOOKBACK_DAYS).std()
                    * np.sqrt(252))
        vol_calm = ((realized <= realized.rolling(config.stress_ma_days()).mean())
                    .astype(float).where(realized.notna()))
    cols = {"stress_vol_calm": vol_calm}

    # --- Credit sub-signal ---
    if hy_oas is not None:
        o = hy_oas.reindex(idx).ffill()
        # Spread below its own trend = tightening/stable = calm.
        cols["stress_credit_calm"] = (o <= o.rolling(config.stress_ma_days()).mean()).astype(float)
    elif hyg is not None and credit_denom is not None:
        cols["stress_credit_calm"] = _ratio_above_trend(hyg, credit_denom, config.stress_ma_days()).astype(float)

    sub = pd.DataFrame(cols)
    sub["stress"] = sub.mean(axis=1)
    return sub


# ---------------------------------------------------------------------------
# Public: the score
# ---------------------------------------------------------------------------
def market_health_score(
    prices: pd.DataFrame,
    hyg: pd.Series | None = None,
    credit_denom: pd.Series | None = None,
    vix: pd.Series | None = None,
    hy_oas: pd.Series | None = None,
) -> pd.DataFrame:
    """
    Compute the daily Market Health Score (0-100) and its components (SPEC §4).

    Parameters
    ----------
    prices : wide adjusted-close frame; must contain "SPY", "RSP", and the
        config.SECTORS columns. IEF may be taken from here if not passed.
    hyg, credit_denom : optional Series for the credit proxy (HYG / credit_denom,
        where credit_denom = LQD per config.CREDIT_PROXY). If absent, the stress
        component uses the volatility proxy alone (clearly degraded).

    Returns a DataFrame indexed by date with the three component scores, the
    0-100 "score", and every underlying sub-signal (kept for the report/tests).
    Rows in the warm-up window, where the slowest signal has no data, are NaN.
    """
    for col in ("SPY", "RSP"):
        if col not in prices.columns:
            raise KeyError(f"market_health_score needs '{col}' in prices")
    sector_cols = [s for s in config.SECTORS if s in prices.columns]
    if not sector_cols:
        raise KeyError("market_health_score needs sector columns for breadth")

    spy, rsp = prices["SPY"], prices["RSP"]
    if credit_denom is None:  # fall back to the configured denominator, then IEF
        denom_ticker = config.CREDIT_PROXY[1]
        credit_denom = prices.get(denom_ticker, prices.get("IEF"))

    trend = _trend_component(spy)
    breadth = _breadth_component(rsp, spy, prices[sector_cols])
    stress = _stress_component(spy, hyg, credit_denom, vix=vix, hy_oas=hy_oas)

    out = pd.concat([trend, breadth, stress], axis=1)
    # Each component already in [0,1]; equal-weight mean * 100 -> 0-100 score.
    out["score"] = out[["trend", "breadth", "stress"]].mean(axis=1) * 100
    out["regime"] = classify_regime_series(out["score"])
    return out


# ---------------------------------------------------------------------------
# Public: regime classification (config.REGIME_BANDS)
# ---------------------------------------------------------------------------
# Regimes ordered high health -> low health, with each band's lower score bound.
_REGIME_ORDER = sorted(
    config.REGIME_BANDS, key=lambda r: config.REGIME_BANDS[r]["score"][0], reverse=True
)
_LOWER_BOUND = {r: config.REGIME_BANDS[r]["score"][0] for r in config.REGIME_BANDS}


def classify_regime(score: float) -> str:
    """Map a single 0-100 score to a regime name via config.REGIME_BANDS."""
    if pd.isna(score):
        return "Undefined"
    for regime in _REGIME_ORDER:  # high to low; first band whose floor we clear
        if score >= _LOWER_BOUND[regime]:
            return regime
    return _REGIME_ORDER[-1]


def classify_regime_series(score: pd.Series) -> pd.Series:
    """Vectorized classify_regime over a score Series."""
    return score.apply(classify_regime)


def equity_band(regime: str) -> tuple[float, float]:
    """The (low, high) equity allowance band for a regime (SPEC §4)."""
    if regime not in config.REGIME_BANDS:
        return (0.0, 0.0)
    return config.REGIME_BANDS[regime]["equity"]


# ---------------------------------------------------------------------------
# Public: hysteresis / whipsaw control (SPEC §4)
# ---------------------------------------------------------------------------
def _is_decisive(score: float, confirmed: str, raw: str) -> bool:
    """
    Did the score cross the band boundary by at least the minimum (dead-zone)?

    A change is ignored if the score only nicks across a threshold by fewer than
    config.REGIME_MIN_THRESHOLD_CROSS points (SPEC §4).
    """
    ci, ri = _REGIME_ORDER.index(confirmed), _REGIME_ORDER.index(raw)
    margin = config.REGIME_MIN_THRESHOLD_CROSS
    if ri > ci:  # raw is a LOWER-health regime: a de-risk; boundary = confirmed's floor
        return (_LOWER_BOUND[confirmed] - score) >= margin
    # raw is a HIGHER-health regime: a re-risk; boundary = floor of the band just above
    up_regime = _REGIME_ORDER[ci - 1]
    return (score - _LOWER_BOUND[up_regime]) >= margin


def apply_hysteresis(score: pd.Series) -> pd.Series:
    """
    Turn a raw score series into a confirmed-regime series with whipsaw control.

    Rules (SPEC §4):
      * A regime change requires the new regime to hold for
        config.REGIME_CONFIRMATION_DAYS consecutive observations, OR
      * the score to drop more than config.REGIME_IMMEDIATE_DROP_POINTS since the
        last confirmation (then de-risk immediately, no waiting).
      * Threshold crossings smaller than config.REGIME_MIN_THRESHOLD_CROSS points
        are ignored (dead-zone).
      * Re-risking (moving to a healthier regime) never jumps immediately — it
        always serves the confirmation buffer. The staged re-entry ladder (SPEC
        §9, separate engine) governs how exposure rebuilds.

    Operates on whatever cadence the caller samples (daily here; the backtest
    samples month-ends). Returns a regime-label Series aligned to `score`.
    """
    confirmed: str | None = None
    ref_score = np.nan          # score at last confirmation (for the drop test)
    pending: str | None = None
    pending_days = 0
    out: list[str] = []

    for value in score:
        raw = classify_regime(value)
        if confirmed is None or raw == "Undefined":
            if not pd.isna(value):
                confirmed = raw
                ref_score = value
            out.append(confirmed if confirmed is not None else "Undefined")
            continue

        if raw == confirmed:
            pending, pending_days = None, 0
            ref_score = value  # stay anchored to the most recent in-regime score
        elif not _is_decisive(value, confirmed, raw):
            pending, pending_days = None, 0  # within dead-zone: ignore the wiggle
        else:
            ci, ri = _REGIME_ORDER.index(confirmed), _REGIME_ORDER.index(raw)
            big_drop = (ref_score - value) > config.REGIME_IMMEDIATE_DROP_POINTS
            if ri > ci and big_drop:
                confirmed, ref_score = raw, value  # immediate de-risk
                pending, pending_days = None, 0
            else:
                pending_days = pending_days + 1 if pending == raw else 1
                pending = raw
                if pending_days >= config.REGIME_CONFIRMATION_DAYS:
                    confirmed, ref_score = raw, value
                    pending, pending_days = None, 0
        out.append(confirmed)

    return pd.Series(out, index=score.index, name="regime_confirmed")
