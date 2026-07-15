r"""
test_condor_alpha_beta.py — guards for ARM 6 (condor_alpha_beta).

These pin the MECHANICS that make the alpha-vs-beta verdict honest, not any outcome:
  * the regression's exposure factor is the INTRADAY 14:00->16:00 move
    (settle_spot - entry_spot)/entry_spot, NOT close-to-close daily returns;
  * the alpha detector recovers intercept ~= 0 on a pure move-driven (short-gamma) series
    with no premium, and recovers a KNOWN positive intercept when one is injected;
  * the passive-straddle benchmark takes HONEST fills (SELL premium toward the BID, so a
    higher fill fraction yields a SMALLER credit) and settles at COSTLESS cash intrinsic;
  * NO LOOK-AHEAD: the straddle's settlement uses only the (given) 16:00 S* and the 14:00
    entry snapshot — strike is picked from the 14:00 snapshot alone.

All tests are pure arithmetic on tiny synthetic inputs — no warehouse needed.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import condor_alpha_beta as ab  # noqa: E402


# --------------------------------------------------------------------------- #
# Exposure window: the move is intraday 14:00->16:00, signed
# --------------------------------------------------------------------------- #
def test_load_arm5_move_is_intraday_1400_to_1600_signed(tmp_path, monkeypatch):
    # load_arm5 must derive move = (settle_spot - entry_spot)/entry_spot (intraday 14->16),
    # signed, and split OOS at 2024-06-30 — NOT a close-to-close daily return.
    csv = tmp_path / "days.csv"
    pd.DataFrame({
        "day": ["2022-03-01", "2024-12-02"],
        "traded": [True, True],
        "entry_spot": [4000.0, 5000.0],
        "settle_spot": [4040.0, 4950.0],       # +1.0% and -1.0% intraday
        "gamma_regime": ["neutral", "neutral"],
        "pnl_w50_f50": [100.0, -50.0],
        "entry_credit_w50_f50": [1.5, 1.5],
        "short_put_k_w50": [3950.0, 4900.0],
    }).to_csv(csv, index=False)
    monkeypatch.setattr(ab, "ARM5_DAYS_CSV", csv)
    out = ab.load_arm5()
    assert out.loc[0, "move"] == pytest.approx(0.01)
    assert out.loc[1, "move"] == pytest.approx(-0.01)
    assert out.loc[1, "move"] < 0 < out.loc[0, "move"]   # sign preserved (down move negative)
    # OOS split: 2022 day is train, 2024-12 (> 2024-06-30) is test
    assert out.loc[0, "half"] == "train"
    assert out.loc[1, "half"] == "test"


def _make_regression_df(n, alpha_dollars, move_beta, gamma_beta, seed=0, noise=1.0):
    """Synthetic Arm-5-shaped frame: pnl = alpha + move_beta*move + gamma_beta*move^2 + noise.
    Returns a frame with the columns regress_width needs at a given (width, f)."""
    rng = np.random.default_rng(seed)
    move = rng.normal(0, 0.008, n)                 # ~0.8% intraday moves
    pnl = (alpha_dollars + move_beta * move + gamma_beta * (move ** 2)
           + rng.normal(0, noise, n))
    days = pd.date_range("2022-01-03", periods=n, freq="B")
    return pd.DataFrame({
        "day": days,
        "move": move,
        "abs_move": np.abs(move),
        "move_sq": move ** 2,
        "half": np.where(days <= pd.Timestamp(ab.OOS_SPLIT), "train", "test"),
        "year": days.year,
        "gamma_regime": "neutral",
        "pnl_w50_f50": pnl,
        "entry_credit_w50_f50": np.full(n, 1.5),
        "short_put_k_w50": 3950.0,
        "entry_spot": 4000.0,
    })


# --------------------------------------------------------------------------- #
# Alpha detector: recovers ~0 intercept on pure move-driven series
# --------------------------------------------------------------------------- #
def test_detector_recovers_zero_alpha_on_pure_move_series():
    # P&L driven ONLY by the move (short-gamma), NO premium intercept -> alpha ~= 0.
    df = _make_regression_df(600, alpha_dollars=0.0, move_beta=-5000.0,
                             gamma_beta=-2e6, seed=1, noise=0.5)
    r = ab.regress_width(df, 50, "f50", "all", do_boot=True)
    assert abs(r["alpha"]) < 0.5, f"expected ~0 intercept, got {r['alpha']}"
    # CI should span 0 when there is no true intercept
    assert r["ci_lo"] < 0 < r["ci_hi"], (r["ci_lo"], r["ci_hi"])


def test_detector_recovers_known_positive_alpha():
    # Inject a real +$40/day premium intercept on top of the move exposure.
    df = _make_regression_df(600, alpha_dollars=40.0, move_beta=-5000.0,
                             gamma_beta=-2e6, seed=2, noise=1.0)
    r = ab.regress_width(df, 50, "f50", "all", do_boot=True)
    assert r["alpha"] == pytest.approx(40.0, abs=1.0), r["alpha"]
    assert r["t_alpha"] > 5.0, r["t_alpha"]
    # CI excludes 0 when the intercept is real
    assert r["ci_lo"] > 0, r["ci_lo"]


def test_move_factor_absorbs_beta_not_intercept():
    # A series that is pure short-gamma loss on big moves must load on move^2 (negative),
    # and must NOT masquerade as a negative intercept.
    df = _make_regression_df(800, alpha_dollars=0.0, move_beta=0.0,
                             gamma_beta=-3e6, seed=3, noise=0.3)
    r = ab.regress_width(df, 50, "f50", "all", do_boot=False)
    assert r["beta_movesq"] < 0, r["beta_movesq"]         # short gamma => negative move^2 load
    assert abs(r["alpha"]) < 0.3, r["alpha"]              # not smeared into the intercept


# --------------------------------------------------------------------------- #
# VRP identity: total P&L == premium sold - realized cost
# --------------------------------------------------------------------------- #
def test_vrp_identity_holds():
    df = _make_regression_df(200, alpha_dollars=10.0, move_beta=-3000.0,
                             gamma_beta=-1e6, seed=4)
    d = ab.vrp_decompose(df, 50, "f50")
    assert d["vrp"] == pytest.approx(df["pnl_w50_f50"].sum(), rel=1e-9)
    # premium - cost identity
    assert d["premium_sold"] - d["realized_cost"] == pytest.approx(d["vrp"], rel=1e-9)


# --------------------------------------------------------------------------- #
# Passive straddle: honest fills + costless cash settlement
# --------------------------------------------------------------------------- #
def test_straddle_fill_is_honest_sell_toward_bid():
    # SELL premium: mid at frac=0, less credit as frac->1 (toward the bid). Never mid-only.
    mid = ab._fill_credit(1.0, 2.0, 0.0)      # bid=1, ask=2 -> mid 1.5
    f50 = ab._fill_credit(1.0, 2.0, 0.5)      # 1.5 - 0.5*0.5 = 1.25
    full = ab._fill_credit(1.0, 2.0, 1.0)     # 1.5 - 0.5    = 1.0 (= bid)
    assert mid == pytest.approx(1.5)
    assert f50 == pytest.approx(1.25)
    assert full == pytest.approx(1.0)
    assert full < f50 < mid   # higher fill fraction => less credit (worse for the seller)


def _kept_quote_frame(day, minute, atm_center=4000):
    """A store-on-change kept-quote frame (the shape s5.load_day().quote returns): symbol,
    expiration, strike, right, bid, ask, bid_size, ask_size, timestamp. Symmetric ATM-ish chain
    around atm_center so put-call parity recovers spot ~ atm_center."""
    rows = []
    for k in range(atm_center - 100, atm_center + 101, 20):
        c_mid = max(atm_center - k, 0) / 100.0 + 5.0
        p_mid = max(k - atm_center, 0) / 100.0 + 5.0
        for right, mid in (("CALL", c_mid), ("PUT", p_mid)):
            rows.append({"symbol": "SPXW", "expiration": day.strftime("%Y-%m-%d"),
                         "strike": float(k), "right": right,
                         "bid": mid - 0.05, "ask": mid + 0.05,
                         "bid_size": 10, "ask_size": 10, "timestamp": minute})
    return pd.DataFrame(rows)


def test_straddle_settles_at_costless_cash_intrinsic():
    # Build a 14:00 synthetic kept-quote chain with a clear ATM, feed a known S*.
    import s5_intraday_data as s5
    day = dt.date(2022, 6, 1)
    minute = pd.Timestamp(dt.datetime.combine(day, dt.time(14, 0)))
    quote = _kept_quote_frame(day, minute)
    orig_load = s5.load_day
    try:
        s5.load_day = lambda d: s5.DayData(day=d, quote=quote, ohlc=pd.DataFrame())
        settle_spot = 4050.0   # index closed +50 above ATM
        tr = ab.run_passive_straddle_day(day, settle_spot)
    finally:
        s5.load_day = orig_load

    assert tr.traded, tr.skip_reason
    # ATM strike is the nearest to recovered spot (~4000); settle intrinsic is costless cash:
    call_i = max(settle_spot - tr.atm_strike, 0.0)
    put_i = max(tr.atm_strike - settle_spot, 0.0)
    settle_pts = call_i + put_i
    # P&L (mid) == (credit_mid - settle_pts) * 100, i.e. NO exit spread crossed at settlement.
    expected_mid = (tr.credit_mid - settle_pts) * ab.CONTRACT_MULTIPLIER
    assert tr.pnl_mid_d == pytest.approx(expected_mid, rel=1e-9)
    # and the f50 P&L is WORSE than mid (less credit collected), never better
    assert tr.pnl_f50_d < tr.pnl_mid_d


def test_straddle_strike_picked_from_entry_snapshot_only():
    # No-look-ahead: the ATM strike depends only on the 14:00 recovered spot, not on S*.
    import s5_intraday_data as s5
    day = dt.date(2022, 6, 2)
    minute = pd.Timestamp(dt.datetime.combine(day, dt.time(14, 0)))
    quote = _kept_quote_frame(day, minute)
    orig_load = s5.load_day
    try:
        s5.load_day = lambda d: s5.DayData(day=d, quote=quote, ohlc=pd.DataFrame())
        # two very different settle levels must yield the SAME ATM strike (picked at entry)
        tr_a = ab.run_passive_straddle_day(day, 3800.0)
        tr_b = ab.run_passive_straddle_day(day, 4200.0)
    finally:
        s5.load_day = orig_load
    assert tr_a.atm_strike == tr_b.atm_strike


def test_straddle_ignores_quotes_after_1400_no_lookahead():
    # A quote stamped AFTER 14:00 must NOT be used (no look-ahead): the 14:00 snapshot is the
    # last kept row at-or-before 14:00. Inject a bogus post-14:00 quote and confirm it's ignored.
    import s5_intraday_data as s5
    day = dt.date(2022, 6, 3)
    minute = pd.Timestamp(dt.datetime.combine(day, dt.time(14, 0)))
    quote = _kept_quote_frame(day, minute)
    later = _kept_quote_frame(day, minute + pd.Timedelta(minutes=30)).copy()
    later["bid"] = 999.0   # absurd quote that would blow up the credit if (wrongly) used
    later["ask"] = 1000.0
    combined = pd.concat([quote, later], ignore_index=True)
    orig_load = s5.load_day
    try:
        s5.load_day = lambda d: s5.DayData(day=d, quote=combined, ohlc=pd.DataFrame())
        tr = ab.run_passive_straddle_day(day, 4000.0)
    finally:
        s5.load_day = orig_load
    assert tr.traded, tr.skip_reason
    # credit reflects the 14:00 quotes (~$10), NOT the poisoned 14:30 quotes (~$2000)
    assert tr.credit_mid < 100.0, tr.credit_mid


# --------------------------------------------------------------------------- #
# Defined-risk stress: loss is CAPPED at width beyond the far wing
# --------------------------------------------------------------------------- #
def test_stress_loss_capped_at_width():
    df = _make_regression_df(300, alpha_dollars=5.0, move_beta=0.0, gamma_beta=0.0, seed=6)
    # a huge crash and a moderate one beyond the wing cost the SAME capped amount
    s_big = ab.stress_defined_risk(df, 50, "f50", -0.50)
    s_med = ab.stress_defined_risk(df, 50, "f50", -0.20)
    assert s_big["capped_loss_d"] == pytest.approx(s_big["max_capped_loss_d"])
    assert s_med["capped_loss_d"] == pytest.approx(s_med["max_capped_loss_d"])
    assert s_big["capped_loss_d"] == pytest.approx(s_med["capped_loss_d"])
    # and the cap equals width*100 - credit
    med_credit = df["entry_credit_w50_f50"].median() * ab.CONTRACT_MULTIPLIER
    assert s_big["max_capped_loss_d"] == pytest.approx(50 * 100 - med_credit)
