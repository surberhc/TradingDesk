"""
test_gamma_overlay.py — unit tests for the GEX gamma risk-sizing overlay.

Covers the pure transform (apply_overlay) and the causal as-of lookup. The
overlay-OFF byte-identity and no-look-ahead-in-the-backtest checks are exercised
end-to-end by src.gamma_compare; here we pin the building blocks.
"""

from __future__ import annotations

import pandas as pd

from strategies import config
from src import gamma_overlay


def _book() -> pd.Series:
    # A representative S0 book: equity beta + a real asset + a Treasury + cash.
    return pd.Series({"SPY": 0.50, "GLDM": 0.10, "IEF": 0.20, "SGOV": 0.20})


def test_positive_and_neutral_pass_through_unchanged():
    w = _book()
    for state in ("Positive", "Neutral", None):
        out = gamma_overlay.apply_overlay(w, state, negative_risk_scale=0.5)
        pd.testing.assert_series_equal(out, w)


def test_negative_trims_risk_to_cash_and_stays_invested():
    w = _book()
    out = gamma_overlay.apply_overlay(w, "Negative", negative_risk_scale=0.5)
    # SPY and GLDM are risk assets -> halved; IEF (Treasury) untouched; trimmed
    # weight (0.25 + 0.05 = 0.30) parked in cash (SGOV). Book renormalizes to 1.
    assert abs(out.sum() - 1.0) < 1e-12
    assert out["SPY"] < w["SPY"] and out["GLDM"] < w["GLDM"]
    assert abs(out["IEF"] - w["IEF"]) < 1e-12          # Treasury is defensive, untouched
    assert out["SGOV"] > w["SGOV"]                     # cash absorbed the trim
    # Risk share dropped from 0.60 to ~0.30 before renorm (sums already == 1 here).
    risk = sum(out[t] for t in ("SPY", "GLDM"))
    assert abs(risk - 0.30) < 1e-9


def test_scale_zero_fully_derisks():
    w = _book()
    out = gamma_overlay.apply_overlay(w, "Negative", negative_risk_scale=0.0)
    assert "SPY" not in out.index and "GLDM" not in out.index
    assert abs(out.sum() - 1.0) < 1e-12


def test_scale_one_is_noop_even_when_negative():
    w = _book()
    out = gamma_overlay.apply_overlay(w, "Negative", negative_risk_scale=1.0)
    pd.testing.assert_series_equal(out, w)


def test_asof_lookup_is_causal():
    idx = pd.to_datetime(["2020-01-02", "2020-01-15", "2020-02-03"])
    gs = pd.Series(["Positive", "Negative", "Positive"], index=idx)
    # On a date between readings, use the most recent PRIOR reading.
    assert gamma_overlay.gamma_state_asof(gs, pd.Timestamp("2020-01-20")) == "Negative"
    # Exactly on a reading date, use that reading.
    assert gamma_overlay.gamma_state_asof(gs, pd.Timestamp("2020-01-02")) == "Positive"
    # Before any reading -> None (caller leaves S0 unchanged).
    assert gamma_overlay.gamma_state_asof(gs, pd.Timestamp("2019-12-31")) is None


def test_load_gamma_state_parses_yyyymmdd():
    gs = gamma_overlay.load_gamma_state(config.GAMMA_OVERLAY_GEX_FILE)
    assert isinstance(gs.index, pd.DatetimeIndex)
    assert gs.index.is_monotonic_increasing
    assert set(gs.dropna().unique()) <= {"Positive", "Neutral", "Negative"}
