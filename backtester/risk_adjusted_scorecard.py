"""RISK-ADJUSTED SCORECARD — premium-selling vehicles vs SPX, judged on a RISK lens.

RE-ANALYSIS of already-committed results (this session) through drawdown-avoidance /
risk-adjusted terms. NO new strategy, NO param tuning, NO curve-fit surface.

Andrew's reframe: "market-like return with less volatility / shallower drawdown / smoother
ride is worth the complexity, ESPECIALLY drawdown avoidance." So the decisive question is
NOT "is there alpha" (already answered: no) but "does any vehicle, UNLEVERED and on the same
reserved capital, deliver a genuinely smoother ride than the index — and beat the trivial
alternative of just holding LESS index (de-risked with cash)?"

Vehicles (each a DAILY book-return series on its reserved capital, reused from committed
engine dumps / reconstructed byte-identically via the committed engines' own analyze_cell):
  1. SPX buy-and-hold, full reserved capital 1:1                    (THE BENCHMARK)
  2. CSP 45DTE hold-to-expiry, 16d-ish ATM, f=0.5                   (the +$718k one)
  3. CSP 30DTE hold-to-expiry, f=0.5
  4. Short strangle 16d/45DTE UNGATED, hold-to-expiry, f=0.5
  5. Short strangle 16d/45DTE UNGATED, managed(50%/21DTE), f=0.5
  6. Short strangle 16d/45DTE REGIME-GATED composite, hold, f=0.5   (layer-all-three gate)
  7. S7 condor headline (completeness line only — negative return, risk-adj moot)

METHOD (never trade-level x-sqrt-52; strictly the DAILY book-return series):
  - Each vehicle series is aligned to the SPX daily-return series on COMMON dates and the
    engine's open-book mask (identical to how the committed grid computed its metrics).
  - SPX metrics for a vehicle's row are computed on the SAME aligned dates as that vehicle,
    so every comparison is apples-to-apples on an identical calendar.
  - rf = constant 3.0% annualized (a single-number 3-mo T-bill proxy; the committed strangle
    cash benchmark used ~3%). rf sensitivity is noted in the report; the RANKING is
    rf-robust because all vehicles share the same rf.

RISK-MATCHED VERDICT (leverage OFF the table):
  De-risk the INDEX with cash: blend b(w) = w*SPX + (1-w)*rf_daily. Choose w so the blend's
  annualized vol equals the vehicle's vol (VOL match); separately so the blend's maxDD equals
  the vehicle's maxDD (DRAWDOWN match). The vehicle BEATS "just hold less index" iff its
  annualized return exceeds the matched blend's. Equivalently vehicle Sharpe > SPX Sharpe
  (vol match) and vehicle Calmar > SPX Calmar (drawdown match) — reported both ways.
  If a vehicle's vol/maxDD EXCEEDS SPX's, w>1 would need leverage; we clamp w<=1 and report
  the ratio comparison (Sharpe/Calmar) instead — never introduce leverage.
"""

from __future__ import annotations

import sys
import datetime as _dt

import numpy as np
import pandas as pd

TRADING_DAYS = 252
RF_ANNUAL = 0.03                      # constant 3% rf proxy (stated assumption)
RF_DAILY = RF_ANNUAL / TRADING_DAYS


# --------------------------------------------------------------------------- #
# Reusable risk metrics (hand-checkable; covered by test_risk_adjusted_scorecard.py)
# --------------------------------------------------------------------------- #
def equity_curve(ret: pd.Series) -> pd.Series:
    """Compounded equity from a daily simple-return series, starting at 1.0."""
    return (1.0 + ret.astype(float)).cumprod()


def drawdown_series(ret: pd.Series) -> pd.Series:
    """Underwater series (equity - running peak) / running peak; <= 0 everywhere."""
    eq = equity_curve(ret)
    peak = eq.cummax()
    return (eq - peak) / peak


def max_drawdown(ret: pd.Series) -> float:
    """Worst peak-to-trough fractional drawdown (a negative number)."""
    dd = drawdown_series(ret)
    return float(dd.min()) if len(dd) else float("nan")


def ann_return(ret: pd.Series) -> float:
    """Annualized (mean daily return * 252) — matches the committed daily_metrics basis."""
    r = ret.dropna().astype(float)
    return float(r.mean() * TRADING_DAYS) if len(r) else float("nan")


def ann_vol(ret: pd.Series) -> float:
    r = ret.dropna().astype(float)
    return float(r.std(ddof=1) * np.sqrt(TRADING_DAYS)) if len(r) > 1 else float("nan")


def sharpe(ret: pd.Series, rf_annual: float = RF_ANNUAL) -> float:
    """(ann.ret - rf) / ann.vol."""
    v = ann_vol(ret)
    if not np.isfinite(v) or v <= 0:
        return float("nan")
    return (ann_return(ret) - rf_annual) / v


def downside_dev_ann(ret: pd.Series, mar_daily: float = RF_DAILY) -> float:
    """Annualized downside deviation of returns BELOW the daily minimum-acceptable-return."""
    r = ret.dropna().astype(float)
    short = r[r < mar_daily]
    if len(short) < 2:
        return float("nan")
    # deviations measured from the MAR, per the Sortino definition
    dd = np.sqrt(np.mean((short - mar_daily) ** 2))
    return float(dd * np.sqrt(TRADING_DAYS))


def sortino(ret: pd.Series, rf_annual: float = RF_ANNUAL) -> float:
    """(ann.ret - rf) / (annualized downside deviation below rf)."""
    dd = downside_dev_ann(ret, mar_daily=rf_annual / TRADING_DAYS)
    if not np.isfinite(dd) or dd <= 0:
        return float("nan")
    return (ann_return(ret) - rf_annual) / dd


def calmar(ret: pd.Series) -> float:
    """CAGR / |maxDD|.  CAGR from the equity curve, annualized by trading-day count."""
    r = ret.dropna().astype(float)
    if len(r) < 2:
        return float("nan")
    eq = equity_curve(r)
    years = len(r) / TRADING_DAYS
    cagr = eq.iloc[-1] ** (1.0 / years) - 1.0
    mdd = abs(max_drawdown(r))
    if mdd <= 0:
        return float("nan")
    return float(cagr / mdd)


def cagr(ret: pd.Series) -> float:
    r = ret.dropna().astype(float)
    if len(r) < 2:
        return float("nan")
    eq = equity_curve(r)
    years = len(r) / TRADING_DAYS
    return float(eq.iloc[-1] ** (1.0 / years) - 1.0)


def ulcer_index(ret: pd.Series) -> float:
    """Sqrt of mean squared percentage drawdown (RMS of the underwater curve, in %)."""
    dd = drawdown_series(ret) * 100.0
    if not len(dd):
        return float("nan")
    return float(np.sqrt(np.mean(dd ** 2)))


def realized_beta(ret: pd.Series, spx: pd.Series) -> float:
    """OLS slope of vehicle daily return on SPX daily return (aligned on the index)."""
    common = ret.dropna().index.intersection(spx.dropna().index)
    y = ret.loc[common].to_numpy(dtype=float)
    x = spx.loc[common].to_numpy(dtype=float)
    if len(y) < 3:
        return float("nan")
    vx = np.var(x, ddof=1)
    if vx <= 0:
        return float("nan")
    return float(np.cov(y, x, ddof=1)[0, 1] / vx)


def metric_row(ret: pd.Series, spx: pd.Series | None = None) -> dict:
    """Full metric bundle for one daily-return series."""
    r = ret.dropna().astype(float)
    out = dict(
        n=int(len(r)),
        cagr=cagr(r),
        ann_ret=ann_return(r),
        ann_vol=ann_vol(r),
        max_dd=max_drawdown(r),
        sharpe=sharpe(r),
        sortino=sortino(r),
        calmar=calmar(r),
        ulcer=ulcer_index(r),
        total_ret=float(equity_curve(r).iloc[-1] - 1.0) if len(r) else float("nan"),
    )
    out["beta"] = realized_beta(r, spx) if spx is not None else float("nan")
    return out


# --------------------------------------------------------------------------- #
# Risk-matched de-risked-index blend  w*SPX + (1-w)*rf
# --------------------------------------------------------------------------- #
def blend_return_series(spx: pd.Series, w: float, rf_daily: float = RF_DAILY) -> pd.Series:
    """Daily return of a static w-in-SPX, (1-w)-in-cash blend (rebalanced daily)."""
    return w * spx.astype(float) + (1.0 - w) * rf_daily


def solve_w_for_vol(spx: pd.Series, target_vol: float) -> float:
    """Vol scales linearly in w (cash has zero vol): w = target_vol / spx_vol, clamped [0,1]."""
    sv = ann_vol(spx)
    if not np.isfinite(sv) or sv <= 0:
        return float("nan")
    return float(min(1.0, max(0.0, target_vol / sv)))


def solve_w_for_maxdd(spx: pd.Series, target_dd: float, rf_daily: float = RF_DAILY) -> float:
    """Find w in [0,1] so blend maxDD == target_dd. maxDD shrinks monotonically as w->0.

    Bisection on |maxDD(blend(w))|. target_dd passed as a negative fraction; compared on
    magnitude. If even w=1 (full SPX) has a shallower maxDD than the target, return 1.0
    (can't get DEEPER without leverage — we never lever).
    """
    tgt = abs(target_dd)
    dd_full = abs(max_drawdown(blend_return_series(spx, 1.0, rf_daily)))
    if dd_full <= tgt:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        dd_mid = abs(max_drawdown(blend_return_series(spx, mid, rf_daily)))
        if dd_mid < tgt:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# Crisis windows (drawdown-avoidance detail)
# --------------------------------------------------------------------------- #
CRISES = {
    "2018Q4": (_dt.date(2018, 10, 1), _dt.date(2018, 12, 31)),
    "COVID": (_dt.date(2020, 2, 15), _dt.date(2020, 4, 30)),
    "2022bear": (_dt.date(2022, 1, 1), _dt.date(2022, 12, 31)),
}


def crisis_max_dd(ret: pd.Series, start: _dt.date, end: _dt.date) -> tuple[float, int]:
    """Max drawdown of a return series computed WITHIN [start,end] (equity re-based at window
    start). Returns (maxDD, n_days)."""
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    w = ret[(ret.index >= s) & (ret.index <= e)].dropna()
    if len(w) < 2:
        return float("nan"), int(len(w))
    return max_drawdown(w), int(len(w))
