r"""Tests for s6_vrp_experiment -- signal correctness, no-look-ahead, quantile/verdict logic.

The load-bearing test is test_signal_ignores_post_1400_data: it proves the 14:00 VRP signal
is UNCHANGED when all post-14:00 minutes are corrupted/removed -- i.e. strictly causal.
ASCII only.
"""

from __future__ import annotations

import datetime as _dt

import numpy as np
import pandas as pd
import pytest

import s6_vrp_experiment as vrp


# --------------------------------------------------------------------------- #
# Synthetic 0DTE chain builder for deterministic causality tests.
# --------------------------------------------------------------------------- #
def _synthetic_nbbo(day, spot_path):
    """Build a minimal per-minute NBBO frame from a {minute: spot} path.

    For each minute and a grid of strikes around spot, we price call/put with BS at a fixed
    vol and set bid=ask=mid so recon recovers the spot exactly and IV ~ the fixed vol. This
    lets us assert exact causality without warehouse data.
    """
    import s6_recon as recon
    rows = []
    strikes = None
    for minute, spot in spot_path.items():
        if strikes is None:
            base = round(spot / 5.0) * 5.0
            strikes = [base + 5.0 * k for k in range(-8, 9)]
        t = recon.time_to_expiry_years(pd.Timestamp(minute), day)
        for k in strikes:
            for right, is_call in (("CALL", True), ("PUT", False)):
                px = recon.bs_price(spot, k, t, 0.20, is_call)
                px = max(px, 0.0)
                rows.append({"minute": pd.Timestamp(minute), "strike": k,
                             "right": right, "bid": px, "ask": px})
    return pd.DataFrame(rows)


def _spot_path(day, start=(9, 30), end=(16, 0), base=4500.0, drift=0.0):
    path = {}
    t = _dt.datetime.combine(day, _dt.time(*start))
    end_t = _dt.datetime.combine(day, _dt.time(*end))
    i = 0
    while t <= end_t:
        path[pd.Timestamp(t)] = base + drift * i
        t += _dt.timedelta(minutes=1)
        i += 1
    return path


# --------------------------------------------------------------------------- #
# NO-LOOK-AHEAD -- the load-bearing test.
# --------------------------------------------------------------------------- #
def test_signal_ignores_post_1400_data(monkeypatch):
    """The 14:00 VRP inputs must be identical whether or not post-14:00 minutes exist/are
    corrupted. Proves impl_2pm and rv_morning use only <=14:00 data."""
    day = _dt.date(2023, 5, 17)
    full_path = _spot_path(day, drift=0.5)          # gentle morning drift
    nbbo_full = _synthetic_nbbo(day, full_path)

    # Corrupt every post-14:00 minute wildly + also a truncated version with them removed.
    entry = pd.Timestamp(_dt.datetime.combine(day, vrp.ENTRY_TIME))
    nbbo_corrupt = nbbo_full.copy()
    post = nbbo_corrupt["minute"] > entry
    nbbo_corrupt.loc[post, ["bid", "ask"]] = nbbo_corrupt.loc[post, ["bid", "ask"]] * 99.0
    nbbo_trunc = nbbo_full[nbbo_full["minute"] <= entry].copy()

    class _Chain:
        def __init__(self, nbbo):
            self.nbbo = nbbo

    def make_stub(nbbo):
        def _load_day(d):
            return object()
        def _chain(d, day_data=None):
            return _Chain(nbbo)
        return _load_day, _chain

    results = {}
    for tag, nbbo in (("full", nbbo_full), ("corrupt", nbbo_corrupt),
                      ("trunc", nbbo_trunc)):
        ld, ch = make_stub(nbbo)
        monkeypatch.setattr(vrp.s5, "load_day", ld)
        monkeypatch.setattr(vrp.s5, "zero_dte_chain", ch)
        sig = vrp.compute_day_signal(day)
        assert sig.ok, f"{tag}: {sig.skip_reason}"
        results[tag] = (sig.impl_2pm, sig.rv_morning, sig.spot_1400)

    # impl_2pm, rv_morning, spot_1400 must be identical across full/corrupt/trunc.
    for a in ("full", "corrupt", "trunc"):
        assert results[a][0] == pytest.approx(results["full"][0], rel=1e-9), "impl_2pm leaked"
        assert results[a][1] == pytest.approx(results["full"][1], rel=1e-9), "rv_morning leaked"
        assert results[a][2] == pytest.approx(results["full"][2], rel=1e-9), "spot_1400 leaked"


def test_impl_2pm_recovers_input_vol(monkeypatch):
    """With a flat-vol synthetic chain, recovered ATM IV should be ~ the 0.20 input vol."""
    day = _dt.date(2023, 6, 1)
    path = _spot_path(day, drift=0.0)
    nbbo = _synthetic_nbbo(day, path)

    class _Chain:
        nbbo = None
    ch = _Chain(); ch.nbbo = nbbo
    monkeypatch.setattr(vrp.s5, "load_day", lambda d: object())
    monkeypatch.setattr(vrp.s5, "zero_dte_chain", lambda d, day_data=None: ch)
    sig = vrp.compute_day_signal(day)
    assert sig.ok
    assert sig.impl_2pm == pytest.approx(0.20, abs=0.02)


# --------------------------------------------------------------------------- #
# Trailing RV -- no look-ahead (today's close excluded).
# --------------------------------------------------------------------------- #
def test_trailing_rv_excludes_today():
    """rv_trail5 for a day must use closes STRICTLY before that day. Changing today's close
    must not change today's rv_trail5."""
    days = [_dt.date(2023, 1, d) for d in range(3, 20)]  # 17 weekdays-ish
    closes = np.linspace(4000, 4100, len(days))
    base = pd.DataFrame({"day": days, "close_spot": closes})
    out = vrp.add_trailing_rv(base)
    # perturb only the LAST day's close and recompute
    base2 = base.copy()
    base2.loc[base2.index[-1], "close_spot"] = 9999.0
    out2 = vrp.add_trailing_rv(base2)
    # every rv_trail5 except (potentially none, since last day's own value doesn't use itself)
    merged = out.merge(out2, on="day", suffixes=("_a", "_b"))
    # all rv_trail5 must be identical -- today's close never feeds today's trailing value
    a = merged["rv_trail5_a"].to_numpy()
    b = merged["rv_trail5_b"].to_numpy()
    both_nan = np.isnan(a) & np.isnan(b)
    assert np.all(both_nan | np.isclose(a, b, equal_nan=True))


def test_trailing_rv_needs_enough_history():
    days = [_dt.date(2023, 2, d) for d in range(1, 10)]
    base = pd.DataFrame({"day": days, "close_spot": np.linspace(100, 110, len(days))})
    out = vrp.add_trailing_rv(base)
    # first TRAIL_DAYS rows cannot have a full trailing window
    assert out["rv_trail5"].iloc[:vrp.TRAIL_DAYS].isna().all()
    assert np.isfinite(out["rv_trail5"].iloc[-1])


# --------------------------------------------------------------------------- #
# Quantile / monotonicity / verdict logic.
# --------------------------------------------------------------------------- #
def _fake_joined(n=300, seed=0):
    """Build a joined frame where P&L RISES and breach FALLS monotonically with vrp_primary,
    identically in both halves -- a synthetic ROBUST gradient to exercise the verdict."""
    rng = np.random.default_rng(seed)
    sig = np.linspace(-0.1, 0.1, n)
    rng.shuffle(sig)
    # deterministic monotone mapping + tiny noise that preserves tercile order in each half
    pnl = sig * 1000.0 + rng.normal(0, 0.5, n)
    # breach: a deterministic, strictly-decreasing function of the SIGNAL VALUE, so within
    # ANY subset (all/train/test) the low-signal tercile breaches more than the high one.
    # Use a fine alternating pattern whose local density falls as the signal rises.
    breach_prob = np.clip(0.5 - sig * 4.0, 0.05, 0.95)  # higher signal -> lower prob
    # convert to a deterministic breach flag by comparing to a fixed per-row phase in [0,1)
    phase = (np.arange(n) % 10) / 10.0
    breached = phase < breach_prob
    # Span the train/test boundary (2024-06-30) so BOTH halves are populated.
    days = pd.date_range("2023-01-02", periods=n, freq="B").date
    df = pd.DataFrame({
        "day": days, "structure": "bull_put",
        "entry_credit": 0.4, "pnl_dollars": pnl, "breached": breached,
        "impl_2pm": 0.2 + sig, "rv_morning": 0.2, "rv_trail5": 0.2,
        "vix_ts_prior": 0.95, "vrp_primary": sig, "vrp_trail": sig,
    })
    df["half"] = np.where(pd.to_datetime(df["day"]) <= pd.Timestamp(vrp.TRAIN_END),
                          "train", "test")
    return df


def test_quantile_table_equal_count():
    j = _fake_joined()
    qt = vrp.quantile_table(j, "vrp_primary", "bull_put", 3, "all")
    assert len(qt) == 3
    # equal-count bins: sizes within 1 of each other
    ns = qt["n"].to_numpy()
    assert ns.max() - ns.min() <= 1


def test_monotonicity_detects_gradient():
    j = _fake_joined()
    qt = vrp.quantile_table(j, "vrp_primary", "bull_put", 3, "all")
    assert "MONOTONIC" in vrp.monotonicity_call(qt, "avg_pnl")
    assert "MONOTONIC" in vrp.monotonicity_call(qt, "breach_rate")


def test_verdict_robust_on_synthetic_gradient():
    j = _fake_joined(n=400)
    v = vrp.structure_verdict(j, "vrp_primary", "bull_put")
    assert "ROBUST" in v


def test_verdict_dead_end_on_noise():
    rng = np.random.default_rng(3)
    n = 400
    days = pd.date_range("2022-05-01", periods=n, freq="B").date
    df = pd.DataFrame({
        "day": days, "structure": "bull_put",
        "entry_credit": 0.4, "pnl_dollars": rng.normal(0, 100, n),
        "breached": rng.random(n) < 0.4,
        "impl_2pm": 0.2, "rv_morning": 0.2, "rv_trail5": 0.2, "vix_ts_prior": 1.0,
        "vrp_primary": rng.normal(0, 0.05, n), "vrp_trail": rng.normal(0, 0.05, n),
    })
    df["half"] = np.where(pd.to_datetime(df["day"]) <= pd.Timestamp(vrp.TRAIN_END),
                          "train", "test")
    v = vrp.structure_verdict(df, "vrp_primary", "bull_put")
    assert ("DEAD END" in v) or ("PEAK" in v) or ("PARTIAL" in v)


def test_realized_vol_positive():
    s = pd.Series(np.cumprod(1 + np.random.default_rng(1).normal(0, 0.001, 100)) * 4000)
    rv, nmin = vrp.realized_vol_from_spot(s)
    assert np.isfinite(rv) and rv > 0 and nmin == 99
