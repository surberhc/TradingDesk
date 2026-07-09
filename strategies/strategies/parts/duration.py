"""
duration.py — Duration Filter & Inflation/Deflation Engine (PRIMARY, the edge).
SPEC.md §6.

Owns what KIND of defense to hold: cash, short, intermediate, or long Treasuries.
Long Treasuries are NOT default defense — they must EARN exposure:

  * Long-Treasury PERMISSION: pass >= config.LONG_TSY_PERMISSION_MIN_PASSES of 5
    rules (TLT trend, 3m return, vs T-bills, yield flat/falling, drawdown ok).
  * Long-Treasury BAN (any one bans long duration): broken trend, yield rising
    above its average, T-bills winning, stocks+bonds both falling, inflationary
    bear active, or drawdown beyond the tested threshold.
  * long_allowed = (permission met) AND (not banned). The "deflationary character,
    not inflationary" confirmation that SPEC requires is enforced *through the bans*
    — the inflationary-bear filter and "stocks and bonds both down" both ban long,
    so a surviving permission is by construction a non-inflationary, stabilizing
    duration bet. Larger long sizing only unlocks in defensive regimes via the caps.

Also implements the inflationary-bear filter (the 2022 guard) and the
deflationary-panic filter, and resolves per-bucket duration caps by regime
(config.DURATION_CAPS) with those filters applied as overrides.

Correctness (SPEC §3): every window is TRAILING, so signals on date T use only
data on/before T. No look-ahead. Macro yield input is the real 10y par yield when
available; otherwise a labeled IEF-price proxy (falling yield <-> rising IEF).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies import config
from strategies.parts import _gates as gates

# One "month" in trading days — a units conversion (see regime.py), not a tunable.
TRADING_DAYS_PER_MONTH = 21

# Representative tickers (long history) for each role used by the filters.
_LONG, _INT, _TBILL, _STOCK = "TLT", "IEF", "BIL", "SPY"
_GOLD, _COMMODITY, _FLOATING = "IAU", "DBC", "USFR"


# ---------------------------------------------------------------------------
# Trailing-window helpers (causal)
# ---------------------------------------------------------------------------
def _ret(series: pd.Series, months: int) -> pd.Series:
    """Total return over the trailing N months (N * 21 trading days)."""
    return series.pct_change(months * TRADING_DAYS_PER_MONTH)


def _above_ma(series: pd.Series, window: int) -> pd.Series:
    """NaN-aware: True/False where price is defined vs its trailing MA, NaN where
    either is undefined (pre-inception or a missing today's print) — never a false
    "not above" the way a bare `>` would read NaN>NaN. 2026-07-09 NaN-as-bearish
    fix. Every call site below `.fillna(False)`s this explicitly (matching
    gates.is_above's existing conservative collapse-to-not-in-trend convention for
    permission/ban rules) so the conservative default is a visible, deliberate
    choice at the point of use, not a silent side effect of the operator."""
    ma = series.rolling(window).mean()
    valid = series.notna() & ma.notna()
    # boolean (nullable) dtype, not object: avoids a pandas FutureWarning on the
    # .fillna(False) call sites below and is the more correct dtype for a
    # True/False/<NA> signal anyway.
    return (series > ma).where(valid).astype("boolean")


def _drawdown_from_high(series: pd.Series, window: int) -> pd.Series:
    """Drawdown vs the trailing `window`-day high (<= 0)."""
    return series / series.rolling(window).max() - 1.0


def _yield_series(prices: pd.DataFrame, yield_10y: pd.Series | None) -> tuple[pd.Series, bool]:
    """
    Return (yield-like series, is_real). When the real 10y par yield is absent,
    fall back to a labeled proxy: NEGATED IEF price, so that every yield trend
    comparison inverts correctly (falling yield <-> rising bond price).
    """
    if yield_10y is not None:
        return yield_10y.reindex(prices.index).ffill(), True
    return -prices[_INT], False


# ---------------------------------------------------------------------------
# Signals (time series, daily, causal)
# ---------------------------------------------------------------------------
def duration_signals(
    prices: pd.DataFrame,
    yield_10y: pd.Series | None = None,
    hyg: pd.Series | None = None,
    hy_oas: pd.Series | None = None,
    credit_denom: pd.Series | None = None,
) -> pd.DataFrame:
    """
    Compute all duration permission/ban rules and the two macro filters (SPEC §6).

    Returns a DataFrame indexed by date with every sub-rule (bool), the permission
    pass count, long_banned, inflationary_bear, deflationary_panic, and long_allowed.
    Missing inputs degrade gracefully — a sub-rule that needs an absent ticker is
    treated as not-satisfied rather than raising.
    """
    px = prices
    has = lambda t: t in px.columns  # noqa: E731 — terse local guard

    tlt = px[_LONG] if has(_LONG) else None
    ief = px[_INT] if has(_INT) else None
    bil = px[_TBILL] if has(_TBILL) else None
    spy = px[_STOCK] if has(_STOCK) else None
    if tlt is None or bil is None or spy is None:
        raise KeyError("duration_signals needs at least TLT, BIL, SPY in prices")

    yld, yld_is_real = _yield_series(px, yield_10y)
    # TREND gates go through gates.is_above (mode-aware: sma/ensemble/ema). The
    # 10-month MA (ma10m) is a SEPARATE trend window, left as a plain single SMA.
    # stress_ma carries the series-vs-own-MA baselines (yield, credit).
    ma10m = config.MA_MONTHS * TRADING_DAYS_PER_MONTH
    stress_ma = config.stress_ma_days()
    dmargin = config.trend_margin("duration")  # early-exit margin for duration gates

    yld_ma = yld.rolling(stress_ma).mean()
    yld_prev_month = yld.shift(TRADING_DAYS_PER_MONTH)
    yld_rising = yld > yld_prev_month
    yld_flat_or_falling = (yld < yld_ma) | (yld <= yld_prev_month)
    yld_above_avg_and_rising = (yld > yld_ma) & yld_rising

    r3 = config.LONG_TSY_RETURN_MONTHS               # 3 months
    tbill_lb = config.TBILL_VS_TSY_LOOKBACKS_MONTHS  # (3, 6)

    s = pd.DataFrame(index=px.index)

    # --- Long-Treasury PERMISSION rules (need >= MIN of 5) ---
    # .fillna(False): a NaN _above_ma reads as "not above" for this permission
    # rule (conservative-collapse, matching gates.is_above's own convention) —
    # deliberate, not the silent bare-`>` bug. 2026-07-09 NaN-as-bearish fix.
    s["perm_tlt_trend"] = gates.is_above(tlt, buffer=dmargin) | _above_ma(tlt, ma10m).fillna(False)
    s["perm_tlt_pos_3m"] = _ret(tlt, r3) > 0
    s["perm_tlt_beats_tbill_3m"] = _ret(tlt, r3) > _ret(bil, r3)
    s["perm_yield_flat_or_falling"] = yld_flat_or_falling
    s["perm_dd_ok"] = _drawdown_from_high(
        tlt, config.LONG_TSY_DRAWDOWN_LOOKBACK_DAYS
    ) >= config.LONG_TSY_MAX_DRAWDOWN
    perm_cols = [c for c in s.columns if c.startswith("perm_")]
    s["perm_passes"] = s[perm_cols].sum(axis=1)

    # --- Inflationary-bear filter (the 2022 guard): majority of 5 ---
    infl = pd.DataFrame(index=px.index)
    infl["spy_weak"] = ~gates.is_above(spy, buffer=dmargin)
    infl["yield_up_rising"] = yld_above_avg_and_rising
    infl["tlt_weak"] = ~gates.is_above(tlt, buffer=dmargin)
    if ief is not None:
        infl["tbill_beats_dur"] = (_ret(bil, r3) > _ret(ief, r3)) & (_ret(bil, r3) > _ret(tlt, r3))
    # Real/defensive assets outperform long bonds over 3m (majority of available).
    real_reps = [t for t in (_GOLD, _COMMODITY, _TBILL, _FLOATING) if has(t)]
    if real_reps:
        beats = pd.concat([_ret(px[t], r3) > _ret(tlt, r3) for t in real_reps], axis=1)
        infl["reals_beat_long"] = beats.sum(axis=1) >= (len(real_reps) / 2.0)
    s["inflationary_bear"] = infl.sum(axis=1) >= np.ceil(infl.shape[1] / 2.0)

    # --- Deflationary-panic filter: majority of 5 ---
    defl = pd.DataFrame(index=px.index)
    defl["spy_weak"] = ~gates.is_above(spy, buffer=dmargin)
    defl["yield_falling"] = yld < yld_prev_month
    if ief is not None:
        defl["dur_beats_tbill"] = (_ret(ief, r3) > _ret(bil, r3)) | (_ret(tlt, r3) > _ret(bil, r3))
    # Credit-widening (deflationary stress). credit_denom comes from config.CREDIT_PROXY
    # (default IEF) so the proxy is HYG/IEF — HY vs Treasury, which captures both the
    # credit blowout AND the flight-to-quality the deflation filter wants. Falls back
    # to ief if not supplied.
    cd = credit_denom if credit_denom is not None else ief
    if hy_oas is not None:
        o = hy_oas.reindex(px.index).ffill()
        defl["credit_widening"] = o > o.rolling(stress_ma).mean()  # real OAS rising = stress
    elif hyg is not None and cd is not None:
        ratio = hyg / cd.reindex(px.index).ffill()
        defl["credit_widening"] = ratio < ratio.rolling(stress_ma).mean()  # HY underperforming = stress
    if has(_COMMODITY):
        defl["commodities_weak"] = ~gates.is_above(px[_COMMODITY], buffer=dmargin)
    s["deflationary_panic"] = defl.sum(axis=1) >= np.ceil(defl.shape[1] / 2.0)

    # --- Long-Treasury BAN rules (any one bans) ---
    ban = pd.DataFrame(index=px.index)
    # .fillna(False) -> ~False -> True: a NaN _above_ma reads as "not above" here
    # too, so its negation reads as satisfying the ban leg — same conservative-
    # collapse convention as perm_tlt_trend above. 2026-07-09 NaN-as-bearish fix.
    ban["broken_trend"] = (~gates.is_above(tlt, buffer=dmargin)) & (~_above_ma(tlt, ma10m).fillna(False))
    ban["yield_up_rising"] = yld_above_avg_and_rising
    ban["tbill_beats_long"] = (_ret(bil, tbill_lb[0]) > _ret(tlt, tbill_lb[0])) | (
        _ret(bil, tbill_lb[1]) > _ret(tlt, tbill_lb[1])
    )
    ban["stocks_and_bonds_down"] = (~gates.is_above(spy, buffer=dmargin)) & (~gates.is_above(tlt, buffer=dmargin))
    ban["inflationary_bear"] = s["inflationary_bear"]
    ban["dd_beyond"] = _drawdown_from_high(
        tlt, config.LONG_TSY_DRAWDOWN_LOOKBACK_DAYS
    ) < config.LONG_TSY_MAX_DRAWDOWN
    s["long_banned"] = ban.any(axis=1)

    # --- Final permission ---
    s["long_allowed"] = (
        s["perm_passes"] >= config.LONG_TSY_PERMISSION_MIN_PASSES
    ) & (~s["long_banned"])

    # --- Macro regime label (drives the dynamic real-asset cap, SPEC §1/§6) ---
    # Only tilt on an UNAMBIGUOUS regime (one filter on, the other off); conflicting
    # signals -> "neutral". Stagflation = a SUSTAINED inflationary-bear.
    infl = s["inflationary_bear"] & (~s["deflationary_panic"])
    defl = s["deflationary_panic"] & (~s["inflationary_bear"])
    sustained = (
        s["inflationary_bear"].rolling(config.STAGFLATION_LOOKBACK_DAYS).mean()
        >= config.STAGFLATION_PERSISTENCE
    )
    regime = pd.Series("neutral", index=px.index, dtype=object)
    regime[infl] = "inflation"
    regime[infl & sustained] = "stagflation"
    regime[defl] = "deflation"
    s["macro_regime"] = regime

    s.attrs["yield_is_real"] = yld_is_real
    return s


# ---------------------------------------------------------------------------
# Cap resolution (per date + regime)
# ---------------------------------------------------------------------------
def _safe_caps(regime: str) -> dict[str, tuple[float, float]]:
    """Base per-bucket caps for a regime; most-defensive default if unknown."""
    if regime in config.DURATION_CAPS["tbill"]:
        return {bucket: config.DURATION_CAPS[bucket][regime] for bucket in config.DURATION_CAPS}
    # Unknown/Undefined regime -> hold cash-like ballast, no duration bet.
    return {"long": (0.0, 0.0), "intermediate": (0.0, 0.0),
            "short": (0.0, 1.0), "tbill": (0.5, 1.0)}


def duration_decision(signals_row: pd.Series, regime: str) -> dict:
    """
    Resolve duration permission + per-bucket caps for one signal date and regime.

    Applies config.DURATION_CAPS for the regime, then overrides:
      * long capped to 0 unless long_allowed.
      * inflationary-bear active -> long banned AND intermediate held low
        (config.INFLATIONARY_INTERMEDIATE_CAP); default defense becomes T-bills /
        ultra-short / floating-rate (SPEC §6).
    Returns a dict with the flags, caps, and human-readable reason codes.
    """
    caps = _safe_caps(regime)
    reasons: list[str] = []

    long_allowed = bool(signals_row.get("long_allowed", False))
    inflationary = bool(signals_row.get("inflationary_bear", False))
    deflationary = bool(signals_row.get("deflationary_panic", False))

    if not long_allowed:
        lo, _ = caps["long"]
        caps["long"] = (0.0, 0.0) if lo == 0.0 else (0.0, 0.0)
        reasons.append(
            f"long Treasuries disallowed (passes={int(signals_row.get('perm_passes', 0))}"
            f"/{config.LONG_TSY_PERMISSION_MIN_PASSES} needed"
            + (", banned)" if signals_row.get("long_banned", False) else ")")
        )
    else:
        reasons.append("long Treasuries permitted (rate bet earned)")

    if inflationary:
        caps["long"] = (0.0, 0.0)
        lo_i, hi_i = caps["intermediate"]
        caps["intermediate"] = (lo_i, min(hi_i, config.INFLATIONARY_INTERMEDIATE_CAP))
        reasons.append("inflationary-bear filter active: long banned, intermediate capped low, defense = T-bills/floating")
    elif deflationary:
        reasons.append("deflationary-panic filter active: intermediate allowed, long up to cap if permitted")

    return {
        "regime": regime,
        "macro_regime": signals_row.get("macro_regime", "neutral"),
        "long_allowed": long_allowed and not inflationary,
        "perm_passes": int(signals_row.get("perm_passes", 0)),
        "long_banned": bool(signals_row.get("long_banned", False)) or inflationary,
        "inflationary_bear": inflationary,
        "deflationary_panic": deflationary,
        "caps": caps,
        "reasons": reasons,
    }
