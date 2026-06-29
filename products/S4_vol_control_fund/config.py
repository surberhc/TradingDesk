"""
config.py — S4 SPX Volatility-Control Fund: pinned DEPLOY parameters.

This is the single source of truth for the parameters this PRODUCT ships with. It does
NOT contain any strategy logic — the decision engine lives in the shared `strategies`
package (`strategies/spx_vol_control.py`, class SpxVolControl, the "shared brain" both
the backtester and the paperbot import). This file only pins the validated dials and
hands back a configured strategy instance.

PAPER / research scope only. There is no real-money configuration anywhere in this
project (see memory: paper-only-language).

------------------------------------------------------------------------------------
WHY THESE NUMBERS (every default traces to a validated report — see VALIDATION.md):

  target_vol = 0.10, leverage_cap = 1.50
    The live-retail / FIA-RILA standard cell ("10% target at 150% cap"; Lincoln S&P
    500 10% DRC participation 150-170%). On our own SPY history 2007-06-28..2026-06-26
    this cell delivered (gross, total-return):
        CAGR 7.51%  |  realized vol 9.86% (on target)  |  max DD -20.94%
        2008 -12.74%  |  Sharpe 0.65  |  vs SPY B&H: CAGR 10.70%, vol 19.79%, DD -55.20%
    Net of costs (1bp/turnover + 50bp/yr borrow spread) the give-up is ~5bp/yr ->
    7.45% net. Source: backtester/output/s4_vol_control_20260628.md (row "10% / 1.50x")
    and s4_vol_control_net_of_costs_20260628.md.

    This is the DEFAULT deploy cell. It is the "balanced" dial: SPX-like exposure scaled
    to ~10% vol, levering toward the target only in calm markets.

  CONSERVATIVE_PROFILE (target_vol = 0.10 is already moderate; for a bond-ALTERNATIVE
    role dial target_vol DOWN). The 5% cell is the published SEC anchor and the calmest:
        5% / 1.50x:  CAGR 4.56% TR  |  realized vol 4.94%  |  max DD -9.51%  |  2008 -5.71%
    Use this when the role is "smoother than bonds, equity-sourced." Note: at 5% the
    leverage cap never binds (avg exposure ~0.35x), so 1.0x and 1.5x are identical there.

  estimator = "simple"  (asymmetric max(20d, 60d) realized vol)
    The required S4 default (de-risk fast on the 20d window, re-risk slow on the sticky
    60d window — the FIA "headline steal"). An EWMA variant (lambda 0.94/0.97) exists
    via estimator="ewma" but is NOT the shipped default.

  cash_ticker = "BIL"
    1-3mo T-bill ETF; the cash/risk-free leg. Covers 2007-05-30+, ~the full SPY history.
    SGOV is an alternative but only starts 2020. Residual (1 - exposure) sits here
    earning RF; exposure > 1.0 borrows the excess (negative cash weight).

  obs_lag = 0
    Act on vol as-of the close that produced it (the validated causal default; exposure
    decided from vol through T's close earns T+1's return inside the runner).
------------------------------------------------------------------------------------
"""
from __future__ import annotations

from strategies.spx_vol_control import SpxVolControl

# --- PINNED DEPLOY DEFAULTS (the FIA/RILA-standard balanced cell) ------------------
TARGET_VOL = 0.10        # annualized vol the fund holds constant (risk dial)
LEVERAGE_CAP = 1.50      # max exposure (upside dial); 1.5x = FIA/RILA standard
ESTIMATOR = "simple"     # asymmetric max(fast, slow) realized vol (required default)
FAST_WINDOW = 20         # fast realized-vol window (trading days)
SLOW_WINDOW = 60         # slow realized-vol window (trading days)
OBS_LAG = 0              # extra observation lag (trading days); 0 = validated default
RISK_TICKER = "SPY"      # the single risk asset (SPX total-return proxy)
CASH_TICKER = "BIL"      # cash / risk-free leg (1-3mo T-bill ETF)

# Realistic friction defaults used by the runner's net-of-cost accounting. These are
# NOT part of the strategy decision (the shared brain is frictionless) — they belong to
# the runner / execution layer. Pinned here so the product reports net numbers too.
COST_BPS = 1.0           # transaction cost in bps per unit of daily turnover (liquid SPY)
BORROW_SPREAD_BPS = 50.0 # annualized financing spread in bps OVER RF on the borrowed part

# --- Alternative shipped profile: conservative bond-alternative -------------------
# Same engine, lower target vol. Use build_strategy(profile="conservative") to get it.
CONSERVATIVE_TARGET_VOL = 0.05
CONSERVATIVE_LEVERAGE_CAP = 1.50   # cap never binds at 5% (avg exposure ~0.35x)

# Data the product needs (read-only parquet, adjusted/total-return close per ticker).
DATA_DIR = r"C:\TradingDesk-Local\bt_data"
REQUIRED_DATA = [f"{RISK_TICKER}.parquet", f"{CASH_TICKER}.parquet"]


def build_strategy(profile: str = "balanced") -> SpxVolControl:
    """Return a SpxVolControl configured with this product's pinned deploy defaults.

    profile:
      "balanced"     -> 10% target / 1.5x cap (the default deploy cell)
      "conservative" -> 5% target / 1.5x cap (bond-alternative role)

    No logic lives here — this is a thin factory over the shared-brain class so the
    backtester, the paperbot, and any deploy harness all instantiate the SAME engine
    with the SAME validated dials.
    """
    if profile == "conservative":
        target_vol = CONSERVATIVE_TARGET_VOL
        leverage_cap = CONSERVATIVE_LEVERAGE_CAP
    elif profile == "balanced":
        target_vol = TARGET_VOL
        leverage_cap = LEVERAGE_CAP
    else:
        raise ValueError(f"unknown profile {profile!r} (use 'balanced' or 'conservative')")

    return SpxVolControl(
        target_vol=target_vol,
        leverage_cap=leverage_cap,
        fast_window=FAST_WINDOW,
        slow_window=SLOW_WINDOW,
        estimator=ESTIMATOR,
        obs_lag=OBS_LAG,
        risk_ticker=RISK_TICKER,
        cash_ticker=CASH_TICKER,
    )
