"""
test_s4_strategy_target.py — PARITY GATE for the S4 paperbot adapter.

The whole safety argument for S4 in paper is "the paperbot does not re-implement the
exposure math — it runs the shared-brain SpxVolControl and repackages its output." These
tests PROVE that:

  * PARITY: s4_strategy_target.current_target(profile).weights equals, bit-for-bit, the
    weights SpxVolControl.on_data() produces on the SAME data as-of the SAME date — for
    BOTH named profiles (balanced 0.10/1.5x AND conservative 0.05/1.5x) and a custom cell.
    If the adapter ever computes exposure itself, these fail.

  * CAUSALITY / no-lookahead: the adapter's as_of/price_date use only prices <= T (the
    MarketState it feeds on_data is sliced to prices.loc[:as_of]), and the exposure held on
    as_of is identical whether the engine sees the full history or only history through
    as_of (i.e. no future bar changes today's decision).

  * PROFILE is a runtime dial: named profiles, explicit overrides, partial-override refusal,
    and the documented conservative default.

  * STALE-DATA GUARD fails closed on an old price date and passes on a fresh one.

SYNTHETIC where possible; the parity/causality tests use the real product data (read-only)
because the point is byte-identity with the exact engine+data run_s4 uses. No broker, no
gateway, nothing transmitted.

Run:
  cd paperbot
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s4_strategy_target.py -q
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

import s4_strategy_target as s4t
from strategies.base import MarketState


def _engine_weights(profile: str | None = None, *, target_vol=None, leverage_cap=None):
    """Independently run the shared-brain engine the SAME way the adapter should, and
    return (weights, as_of, price_date). This is the reference the adapter must match."""
    if target_vol is not None and leverage_cap is not None:
        strat = s4t.s4_config.SpxVolControl(
            target_vol=target_vol, leverage_cap=leverage_cap,
            fast_window=s4t.s4_config.FAST_WINDOW, slow_window=s4t.s4_config.SLOW_WINDOW,
            estimator=s4t.s4_config.ESTIMATOR, obs_lag=s4t.s4_config.OBS_LAG,
            risk_ticker=s4t.s4_config.RISK_TICKER, cash_ticker=s4t.s4_config.CASH_TICKER,
        )
    else:
        strat = s4t.s4_config.build_strategy(profile)
    prices = s4t._load_prices().dropna()
    strat.warmup(prices, macro={}, start=str(prices.index.min().date()), end=None)
    as_of = strat.signal_dates[-1]
    state = MarketState(prices=prices.loc[:as_of], macro={}, as_of=as_of)
    tw = strat.on_data(state)
    return tw.weights, as_of, prices.index[-1]


# --- 1. PARITY: adapter weights == engine weights, bit-for-bit, both profiles ------
@pytest.mark.parametrize("profile", ["balanced", "conservative"])
def test_parity_named_profile(profile):
    ref_w, ref_as_of, ref_pdate = _engine_weights(profile)
    t = s4t.current_target(profile=profile)
    # Same as_of and price_date.
    assert pd.Timestamp(t.as_of) == pd.Timestamp(ref_as_of)
    assert pd.Timestamp(t.price_date) == pd.Timestamp(ref_pdate)
    # Same symbols and BIT-FOR-BIT identical weights (no re-derivation, no rounding).
    assert set(t.weights.index) == set(ref_w.index)
    for sym in ref_w.index:
        assert t.weights[sym] == ref_w[sym], f"{profile}: {sym} weight differs from engine"
    # Weights sum to 1.0 (SPY + BIL), the vol-control invariant.
    assert float(t.weights.sum()) == pytest.approx(1.0, abs=1e-12)


def test_parity_custom_override():
    ref_w, ref_as_of, _ = _engine_weights(target_vol=0.10, leverage_cap=1.5)
    t = s4t.current_target(target_vol=0.10, leverage_cap=1.5)
    for sym in ref_w.index:
        assert t.weights[sym] == ref_w[sym]
    # A custom cell equal to the balanced dials must equal the balanced profile exactly.
    bal, _, _ = _engine_weights("balanced")
    for sym in bal.index:
        assert t.weights[sym] == bal[sym]


# --- 2. CAUSALITY / no-lookahead ----------------------------------------------------
def test_causality_only_uses_prices_through_T():
    """The exposure held on as_of must not depend on any bar AFTER as_of. Re-run the engine
    on the data TRUNCATED at as_of and confirm the on_data weight is identical — proving the
    decision uses only prices <= T (the MarketState the adapter feeds is sliced to :as_of)."""
    strat_full = s4t.s4_config.build_strategy("balanced")
    prices = s4t._load_prices().dropna()
    strat_full.warmup(prices, macro={}, start=str(prices.index.min().date()), end=None)
    as_of = strat_full.signal_dates[-1]
    w_full = strat_full.on_data(MarketState(prices=prices.loc[:as_of], macro={}, as_of=as_of)).weights

    # Truncate the data at as_of and re-warm: the value at T uses only trailing windows, so
    # the exposure for as_of must be unchanged.
    truncated = prices.loc[:as_of]
    strat_trunc = s4t.s4_config.build_strategy("balanced")
    strat_trunc.warmup(truncated, macro={}, start=str(truncated.index.min().date()), end=None)
    w_trunc = strat_trunc.on_data(MarketState(prices=truncated, macro={}, as_of=as_of)).weights
    for sym in w_full.index:
        assert w_full[sym] == w_trunc[sym], "future bars changed today's exposure (lookahead!)"

    # And the adapter's as_of is the last signal date, whose price_date is the last data row.
    t = s4t.current_target(profile="balanced")
    assert pd.Timestamp(t.as_of) <= pd.Timestamp(t.price_date)


# --- 3. PROFILE is a runtime dial ---------------------------------------------------
def test_profiles_differ_and_conservative_is_lighter():
    bal = s4t.current_target(profile="balanced")
    con = s4t.current_target(profile="conservative")
    # Lower target vol -> lower SPY exposure (strictly, given the same realized vol today).
    assert con.weights["SPY"] < bal.weights["SPY"]


def test_partial_override_refuses():
    with pytest.raises(ValueError):
        s4t.current_target(target_vol=0.10)          # cap missing
    with pytest.raises(ValueError):
        s4t.current_target(leverage_cap=1.5)         # target_vol missing


def test_unknown_profile_refuses():
    with pytest.raises(ValueError):
        s4t.current_target(profile="aggressive")


def test_default_is_conservative_when_nothing_given():
    """No profile, no overrides -> the documented safe default (conservative). Proven by
    equality with an explicit conservative call (same as_of/data)."""
    d = s4t.current_target()
    c = s4t.current_target(profile="conservative")
    for sym in c.weights.index:
        assert d.weights[sym] == c.weights[sym]
    assert "conservative" in d.version


# --- 4. STALE-DATA GUARD ------------------------------------------------------------
def test_stale_guard_fails_closed_on_old_price_date():
    # A price_date well before the last completed session must raise (fail closed).
    with pytest.raises(RuntimeError, match="STALE"):
        s4t._assert_fresh(pd.Timestamp("2020-01-02"), today=dt.date(2026, 7, 6))


def test_stale_guard_passes_when_fresh():
    # price_date == the last completed session before `today` -> fresh, no raise.
    # 2026-07-06 is a Monday; last completed session before it is Thu 2026-07-02
    # (Fri 7-03 observed holiday, weekend), so a 7-02 price date is fresh.
    s4t._assert_fresh(pd.Timestamp("2026-07-02"), today=dt.date(2026, 7, 6))


def test_current_target_stale_guard_wired():
    # With check_stale=True and a today far in the future, real (older) data must trip the
    # guard, proving the guard is actually wired into current_target (not just the helper).
    with pytest.raises(RuntimeError, match="STALE"):
        s4t.current_target(profile="conservative", today=dt.date(2027, 6, 1))
