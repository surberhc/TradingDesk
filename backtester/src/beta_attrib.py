"""
beta_attrib.py — SKILL-vs-BETA attribution for the equity-broadening study.

Pre-registration: docs/PREREG_S0_equity_sleeve_broadening_2026-07-20.md §3, §6.

The decisive question the study must answer honestly is whether a broadened
variant's extra return is *selection skill* or *just more equity beta*. This
module holds the regression side of that test: regress a strategy's daily excess
returns on SPY's daily excess returns and report (alpha, beta, R^2). A
higher-beta-but-no-skill series must come back with alpha ~ 0 — that is the
attribution property the study leans on and the tests pin down.

Phase 1 scope: the single-factor CAPM regression + annualized alpha. The
block-bootstrap CI on alpha and the beta-matched broad-beta portfolio construction
(§3 "killer control") are Phase 2 and build on this same alpha estimate; the seed
(np.random.default_rng(20260720)) and block (~20d, >=2000 resamples) are fixed in
the prereg so nothing there is tuned to data.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TRADING_DAYS = 252


@dataclass(frozen=True)
class Attribution:
    """Single-factor (CAPM) attribution of a return series vs SPY."""

    alpha_daily: float      # intercept, per-day
    alpha_annual: float     # intercept annualized (x252) — the headline "skill" term
    beta: float             # slope vs SPY (the "just beta" loading)
    r2: float               # fraction of variance explained by SPY
    n: int                  # observations used


def capm_attribution(
    strat_ret: pd.Series,
    spy_ret: pd.Series,
    rf_daily: pd.Series | float = 0.0,
) -> Attribution:
    """Regress strategy EXCESS daily returns on SPY EXCESS daily returns (OLS).

    r_strat - rf = alpha + beta * (r_spy - rf) + eps.

    alpha_annual is the annualized intercept — the return NOT explained by equity
    beta. If a series is just SPY scaled up (higher beta, no skill), alpha ~ 0 and
    beta reflects the scaling. Inputs are aligned on their common dates; rf may be a
    scalar or a daily Series (e.g. the T-bill return).
    """
    df = pd.concat([strat_ret.rename("s"), spy_ret.rename("m")], axis=1).dropna()
    if isinstance(rf_daily, pd.Series):
        df = df.join(rf_daily.rename("rf"), how="left")
        df["rf"] = df["rf"].fillna(0.0)
    else:
        df["rf"] = float(rf_daily)
    if len(df) < 2:
        raise ValueError("need >= 2 aligned observations for a regression")

    y = (df["s"] - df["rf"]).to_numpy()
    x = (df["m"] - df["rf"]).to_numpy()

    # OLS via the design matrix [1, x]; np.linalg.lstsq is numerically stable.
    design = np.column_stack([np.ones_like(x), x])
    (alpha, beta), *_ = np.linalg.lstsq(design, y, rcond=None)

    resid = y - (alpha + beta * x)
    ss_res = float((resid ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    return Attribution(
        alpha_daily=float(alpha),
        alpha_annual=float(alpha) * TRADING_DAYS,
        beta=float(beta),
        r2=float(r2),
        n=int(len(df)),
    )
