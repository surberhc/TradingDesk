"""
analytics.py
============
Drift-robust evaluation helpers shared by the reproduction script.

The two methodological pillars (see FLOW_VERDICT.md sec. 3):

1. circular_shift_pvalue — forward returns overlap heavily (consecutive days
   share most of an h-day window), so naive t-stats are wildly overstated. We
   test a label/return association by CIRCULARLY SHIFTING the label series
   against the returns: this preserves the autocorrelation of BOTH series and
   the run-length structure of the labels, and breaks only their alignment.
   p = fraction of shifts whose |spread| >= |observed spread|.

2. strategy_perf — Sharpe and max-drawdown are drift-normalized, so a long/flat
   or sized overlay driven by the signal is the cleanest test of risk value
   independent of the bull drift.
"""

import numpy as np
import pandas as pd


def circular_shift_pvalue(ret: pd.Series, mask: pd.Series):
    """Two-sided perm p for mean(ret|mask) - mean(ret|~mask) via circular shift.

    ret, mask must be aligned, NaNs already dropped. Returns (observed, p).
    Uses every non-trivial integer shift (exact over the rotation group).
    """
    r = np.asarray(ret, float)
    m = np.asarray(mask).astype(int)
    n = len(r)

    def spread(mm):
        a, b = r[mm == 1], r[mm == 0]
        return a.mean() - b.mean() if len(a) and len(b) else np.nan

    obs = spread(m)
    null = np.array([spread(np.roll(m, s)) for s in range(1, n)])
    null = null[~np.isnan(null)]
    p = float((np.abs(null) >= abs(obs)).mean())
    return float(obs), p


def strategy_perf(daily_ret: pd.Series, position: pd.Series, label: str,
                  ann: int = 252):
    """Equity-curve stats for a position series acting on NEXT-day returns.

    position is the state-derived exposure known at close t; it is shifted one
    day so the return earned is t -> t+1 (no look-ahead). rf assumed 0.
    Returns a dict of CAGR%, vol%, Sharpe, maxDD%, time_in_mkt%.
    """
    r = (daily_ret * position.shift(1)).dropna()
    eq = (1 + r).cumprod()
    yrs = (r.index[-1] - r.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    vol = r.std() * np.sqrt(ann)
    sharpe = (r.mean() * ann) / vol if vol else np.nan
    maxdd = (eq / eq.cummax() - 1).min()
    return dict(label=label, CAGR_pct=cagr * 100, vol_pct=vol * 100,
                Sharpe=sharpe, maxDD_pct=maxdd * 100,
                time_in_mkt_pct=float(position.mean()) * 100)


def by_state_table(states: pd.Series, fwd: pd.Series):
    """Mean/demeaned/hit% of a forward-return series grouped by state label."""
    d = pd.DataFrame({"s": states, "f": fwd}).dropna()
    base = d["f"].mean()
    rows = []
    for g, sub in d.groupby("s"):
        rows.append(dict(state=g, n=len(sub), raw=sub["f"].mean(),
                         demean=sub["f"].mean() - base,
                         hit_pct=(sub["f"] > 0).mean() * 100))
    return base, pd.DataFrame(rows).set_index("state")


def episodes(labels: pd.Series):
    """List of (label, start_iloc, end_iloc) maximal runs — for independence."""
    lab = labels.values
    runs, cur, start = [], None, 0
    for i, x in enumerate(lab):
        if x != cur:
            if cur is not None:
                runs.append((cur, start, i - 1))
            cur, start = x, i
    runs.append((cur, start, len(lab) - 1))
    return runs
