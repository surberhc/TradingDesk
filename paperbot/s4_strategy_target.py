"""
s4_strategy_target.py — what strategy S4 (SPX vol-control fund) wants to hold RIGHT NOW,
packed into the paperbot's existing Target shape.

SIBLING to strategy_target.py (which serves S0). This file is S4-specific: S0 runs the
backtester's month-by-month engine; S4 is a single-asset daily vol-control fund whose
target is a pure function of the SPY/BIL price history through T. We do NOT re-derive any
exposure math here — we instantiate the SHARED-BRAIN engine
(strategies.spx_vol_control.SpxVolControl) with the S4 product's pinned deploy config and
run warmup()+on_data() EXACTLY as products/S4_vol_control_fund/run_s4.py:current_target()
does, then repackage its {SPY, BIL} weights into the existing paperbot Target dataclass
(imported from strategy_target, NOT forked). So paper == the validated engine by
construction.

Profile is a RUNTIME dial (not a hardcoded cell): the two named product profiles
(balanced = target_vol 0.10 / cap 1.5; conservative = 0.05 / 1.5) plus arbitrary
target_vol / leverage_cap overrides. It fails LOUD if you neither name a profile nor
supply both overrides — no silent default that could accidentally lever. When it must
choose, it defaults to CONSERVATIVE (the unlevered 5% cell that never borrows), documented
below.

STALE-DATA GUARD: refuses to produce a tradeable target if the latest price date is older
than the most recent expected US trading session (per the shared market calendar). Fails
closed with a clear error rather than trading on stale prices.

READ-ONLY: loads price data and computes. Touches no broker and places no order.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from connections import market_calendar as mc

# Reuse the EXISTING Target dataclass (do not fork the target seam).
from strategy_target import Target

# The S4 product config (pinned deploy dials + build_strategy factory) lives at
# TradingDesk/products/S4_vol_control_fund/config.py and is imported there as a bare
# `import config`. The paperbot's OWN `config` is a different module, so we load the S4
# product config under a DISTINCT module name to avoid any collision on sys.modules.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_S4_PRODUCT_DIR = _REPO_ROOT / "products" / "S4_vol_control_fund"
if str(_S4_PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(_S4_PRODUCT_DIR))

import importlib.util as _ilu

_s4_cfg_path = _S4_PRODUCT_DIR / "config.py"
_spec = _ilu.spec_from_file_location("s4_product_config", str(_s4_cfg_path))
s4_config = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(s4_config)  # type: ignore[union-attr]


# Named product profiles -> (target_vol, leverage_cap). Kept in lockstep with the S4
# product config's own build_strategy so there is a single source of truth for the dials.
PROFILES = {
    "balanced": (s4_config.TARGET_VOL, s4_config.LEVERAGE_CAP),               # 0.10 / 1.5x
    "conservative": (s4_config.CONSERVATIVE_TARGET_VOL,
                     s4_config.CONSERVATIVE_LEVERAGE_CAP),                     # 0.05 / 1.5x
}

# The safe default when the caller supplies neither a named profile nor explicit overrides.
# CONSERVATIVE (5% target) never levers — avg exposure ~0.35x, the cap never binds — so it
# can never accidentally borrow. Choosing it as the default makes "forgot to pick" fail to
# the un-levered path, not the 1.5x one.
DEFAULT_PROFILE = "conservative"


def _load_prices() -> pd.DataFrame:
    """Load the SPY + BIL adjusted-close (total-return) series the strategy needs.

    Byte-for-byte the same loader as run_s4.py: read the two product parquet files, take
    the first column, normalize the index. Read-only."""
    frames = {}
    for ticker in (s4_config.RISK_TICKER, s4_config.CASH_TICKER):
        path = os.path.join(s4_config.DATA_DIR, f"{ticker}.parquet")
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"missing data file: {path}\n"
                f"  -> S4 needs {s4_config.REQUIRED_DATA} in {s4_config.DATA_DIR}"
            )
        df = pd.read_parquet(path)
        s = df.iloc[:, 0]
        s.index = pd.to_datetime(s.index).normalize()
        frames[ticker] = s.sort_index()
    return pd.concat(frames, axis=1)


def _resolve_params(profile: str | None, target_vol: float | None,
                    leverage_cap: float | None) -> tuple[float, float, str]:
    """Resolve (target_vol, leverage_cap, label) from a named profile and/or overrides.

    Rules (fail-loud, no silent lever):
      * explicit target_vol AND leverage_cap given -> use them (label "custom"); a named
        profile, if also given, must not contradict — but overrides win and are labelled.
      * a named profile alone -> its pinned (target_vol, cap).
      * partial overrides (exactly one of the two) -> ERROR: ambiguous, refuse.
      * nothing at all -> DEFAULT_PROFILE (conservative), documented above.
    """
    has_tv = target_vol is not None
    has_cap = leverage_cap is not None
    if has_tv ^ has_cap:
        raise ValueError(
            "S4 target: supply BOTH target_vol and leverage_cap for a custom cell, or "
            "neither (use a named profile). Got only one — refusing to guess the other "
            "(a wrong leverage_cap is order-affecting).")
    if has_tv and has_cap:
        return float(target_vol), float(leverage_cap), "custom"
    # No overrides: a named profile (explicit) or the documented safe default.
    name = profile if profile is not None else DEFAULT_PROFILE
    if name not in PROFILES:
        raise ValueError(
            f"unknown S4 profile {name!r}; use one of {sorted(PROFILES)} "
            f"or pass explicit target_vol + leverage_cap.")
    tv, cap = PROFILES[name]
    return float(tv), float(cap), name


def _assert_fresh(price_date: pd.Timestamp, *, today: dt.date | None = None) -> None:
    """STALE-DATA GUARD: refuse a tradeable target if the latest price is older than the
    most recent expected US trading session.

    The freshness anchor is market_calendar.last_trading_day(today): after any session, its
    EOD data is what SHOULD be present. If the price store's newest date is strictly before
    that anchor, we are missing at least one completed session -> fail CLOSED. (Running
    intraday on a trading day before that day's close legitimately has yesterday as the
    latest close; the anchor is the last COMPLETED session, so that is not flagged until the
    session it needs has actually closed and gone un-ingested.)"""
    today = dt.date.today() if today is None else today
    pdate = price_date.date() if hasattr(price_date, "date") else price_date
    # last_trading_day(inclusive=False): the last session STRICTLY before today, i.e. the
    # most recent close whose EOD data must already exist. On a trading day this is
    # yesterday's session until today closes; on a weekend/holiday it is the prior Friday.
    try:
        anchor = mc.last_trading_day(today, inclusive=False)
    except mc.CalendarYearMissing as exc:
        # Unknown calendar year -> fail loud rather than guess freshness.
        raise RuntimeError(
            f"S4 stale-data guard cannot verify freshness: {exc}") from exc
    if pdate < anchor:
        raise RuntimeError(
            f"S4 STALE DATA: latest price date {pdate} is older than the last completed "
            f"trading session {anchor} (today={today}). Refusing to produce a tradeable "
            f"target on stale prices — re-run the forward data pull first. FAILING CLOSED.")


def current_target(account: str | None = None, *, profile: str | None = None,
                   target_vol: float | None = None, leverage_cap: float | None = None,
                   check_stale: bool = True, today: dt.date | None = None) -> Target:
    """Today's S4 target book, packed into the paperbot Target dataclass.

    Parameters
    ----------
    account : str | None
        Accepted for signature symmetry with the driver / future multi-account use. The S4
        target is account-INDEPENDENT (it is a set of weights, sized per-account later), so
        this is not used to compute weights; it is recorded by the caller.
    profile : str | None
        "balanced" (0.10/1.5x) or "conservative" (0.05/1.5x). If None and no overrides are
        given, DEFAULT_PROFILE (conservative) is used (documented, un-levered).
    target_vol, leverage_cap : float | None
        Explicit dial overrides. Supply BOTH or NEITHER (partial raises). Both given ->
        a custom cell that takes precedence over any named profile.
    check_stale : bool
        Run the stale-data guard (default True). The driver may pass a fixed `today`.

    Returns a Target(weights={SPY, BIL}, prices, as_of, price_date, version) where version
    encodes the S4 engine + resolved dials for the audit trail. ZERO exposure math here.
    """
    tv, cap, label = _resolve_params(profile, target_vol, leverage_cap)

    # Build the SHARED-BRAIN engine with the resolved dials via the product factory when the
    # cell is a named profile, else construct directly with the overrides (still the same
    # SpxVolControl class + pinned window/estimator defaults). No exposure math is written
    # in either branch — SpxVolControl owns it.
    if label in PROFILES:
        strat = s4_config.build_strategy(label)
    else:
        strat = s4_config.SpxVolControl(
            target_vol=tv, leverage_cap=cap,
            fast_window=s4_config.FAST_WINDOW, slow_window=s4_config.SLOW_WINDOW,
            estimator=s4_config.ESTIMATOR, obs_lag=s4_config.OBS_LAG,
            risk_ticker=s4_config.RISK_TICKER, cash_ticker=s4_config.CASH_TICKER,
        )

    prices = _load_prices().dropna()
    strat.warmup(prices, macro={}, start=str(prices.index.min().date()), end=None)
    if not strat.signal_dates:
        raise RuntimeError("S4: no warm signal dates — not enough history to decide")
    as_of = strat.signal_dates[-1]

    from strategies.base import MarketState
    # Causal: on_data sees ONLY prices up to and including as_of (<= T).
    state = MarketState(prices=prices.loc[:as_of], macro={}, as_of=as_of)
    tw = strat.on_data(state)

    price_date = prices.index[-1]
    if check_stale:
        _assert_fresh(price_date, today=today)

    # Latest available close per ticker for order sizing (ffill covers a ticker that didn't
    # print on the very last date). adjClose is anchored to the latest real price.
    latest = prices.ffill().loc[price_date]

    version = (f"S4/spx_vol_control/{label} "
               f"tv={tv:.4f} cap={cap:.2f} "
               f"est={s4_config.ESTIMATOR} win={s4_config.FAST_WINDOW}/{s4_config.SLOW_WINDOW}")

    return Target(
        weights=tw.weights.astype("float64"),
        prices=latest.astype("float64"),
        as_of=pd.Timestamp(as_of),
        price_date=pd.Timestamp(price_date),
        version=version,
    )
