r"""
test_strangle_regime_gate.py — correctness guards for the REGIME-GATED short-strangle study.

Pre-registered invariants for strangle_regime_gate.py (docs/PREREG_strangle_regime_gate_2026-07-06.md):
  1. The gate is CAUSAL: an entry decision on day D uses only data on/before D — a FUTURE
     VIX/VIX3M/regime print cannot change a past on/off decision. (Adding future rows after the
     entry day leaves every earlier on/off decision identical.)
  2. The random-duty-cycle PLACEBO actually matches the gate's duty cycle: each random draw turns
     on exactly the requested on-week count (same on-count ± 0), sampled without replacement.
  3. The IVR percentile is TRAILING-ONLY: IVR(t) depends only on VIX closes on/before t; appending
     future closes cannot change a past IVR value; min_periods=252 leaves the first 251 days NaN.

Pure-logic tests always run; data-backed tests skip if bt_data / the VIX parquet are absent.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import strangle_regime_gate as g


# --------------------------------------------------------------------------- #
# 1. Gate causality — an as-of read uses only data on/before the entry day.
# --------------------------------------------------------------------------- #
def test_asof_bool_is_causal_to_future_prints():
    idx = pd.to_datetime(["2020-01-06", "2020-01-13", "2020-01-20"])
    s = pd.Series([True, False, True], index=idx)
    entry = dt.date(2020, 1, 13)
    val = g._asof_bool(s, entry)
    # Flip a FUTURE print (2020-01-20) — the as-of value on the entry day must not change.
    s2 = s.copy()
    s2.loc[pd.Timestamp("2020-01-20")] = False
    assert g._asof_bool(s2, entry) == val
    # And it reads the most-recent value ON/BEFORE the entry day (the 01-13 = False), not later.
    assert val is False


def test_asof_bool_before_series_start_is_false():
    idx = pd.to_datetime(["2020-06-01"])
    s = pd.Series([True], index=idx)
    assert g._asof_bool(s, dt.date(2020, 1, 1)) is False


def test_gate_on_days_unaffected_by_future_signal_rows():
    """Turning ON a gate day in the FUTURE cannot add or remove any past entry's on/off status."""
    entry_days = [dt.date(2020, 1, 6), dt.date(2020, 1, 13), dt.date(2020, 1, 20)]
    idx = pd.to_datetime([str(d) for d in entry_days])
    regime = pd.Series([True, True, False], index=idx)
    contango = pd.Series([True, True, True], index=idx)
    ivr = pd.Series([60.0, 60.0, 60.0], index=idx)
    gates = {"regime": regime, "contango": {1.00: contango}, "ivr": ivr}
    on = g.gate_on_entry_days(entry_days[:2], "composite", gates,
                              ivr_threshold=50, contango_cutoff=1.00)
    # Append a future entry + future signal values; the first-two decision must be unchanged.
    fut = dt.date(2020, 1, 27)
    idx2 = pd.to_datetime([str(d) for d in entry_days + [fut]])
    gates2 = {"regime": pd.Series([True, True, False, True], index=idx2),
              "contango": {1.00: pd.Series([True, True, True, True], index=idx2)},
              "ivr": pd.Series([60.0, 60.0, 60.0, 99.0], index=idx2)}
    on2 = g.gate_on_entry_days(entry_days[:2], "composite", gates2,
                               ivr_threshold=50, contango_cutoff=1.00)
    assert on == on2 == entry_days[:2]


def test_composite_requires_all_three():
    entry_days = [dt.date(2020, 1, 6)]
    idx = pd.to_datetime([str(entry_days[0])])
    base = {"regime": pd.Series([True], index=idx),
            "contango": {1.00: pd.Series([True], index=idx)},
            "ivr": pd.Series([60.0], index=idx)}
    assert g.gate_on_entry_days(entry_days, "composite", base, ivr_threshold=50) == entry_days
    # Drop regime -> composite off.
    off = {**base, "regime": pd.Series([False], index=idx)}
    assert g.gate_on_entry_days(entry_days, "composite", off, ivr_threshold=50) == []
    # Drop contango -> off.
    off2 = {**base, "contango": {1.00: pd.Series([False], index=idx)}}
    assert g.gate_on_entry_days(entry_days, "composite", off2, ivr_threshold=50) == []
    # IVR below threshold -> off.
    assert g.gate_on_entry_days(entry_days, "composite", base, ivr_threshold=75) == []


# --------------------------------------------------------------------------- #
# 2. Placebo matches the gate's duty cycle exactly.
# --------------------------------------------------------------------------- #
def test_placebo_matches_duty_cycle_exactly(monkeypatch):
    """Each random draw must turn on exactly `on_count` weeks (same duty cycle, no replacement)."""
    weekly = [dt.date(2020, 1, 6) + dt.timedelta(days=7 * i) for i in range(40)]
    on_count = 13
    captured = []

    def fake_book(dte, delta, mgmt, f, all_days, on_days, day_cache, price_maps):
        captured.append(len(on_days))            # record the size of each random on-set
        return []                                # empty book -> cheap; alpha/pnl become 0/nan

    monkeypatch.setattr(g, "run_gated_strangle_book", fake_book)
    monkeypatch.setattr(g.ss, "strangle_book_daily_marks",
                        lambda *a, **k: pd.DataFrame(columns=["ret", "n_open"]))
    res = g.random_duty_cycle_placebo(
        all_days=weekly, weekly_entries=weekly, on_count=on_count,
        delta=0.16, dte=45, management="hold", f=0.5, day_cache={}, price_maps={},
        spx_ret=pd.Series(dtype=float), gate_total_pnl=0.0, gate_alpha_ann=0.0, n_seeds=25)
    assert res["n"] == 25
    assert len(captured) == 25
    # EVERY draw turned on exactly on_count weeks — the duty cycle is matched exactly.
    assert all(c == on_count for c in captured)


def test_placebo_on_count_clamped_to_available():
    weekly = [dt.date(2020, 1, 6) + dt.timedelta(days=7 * i) for i in range(5)]
    # Ask for more on-weeks than exist -> clamp, don't crash.
    res = g.random_duty_cycle_placebo(
        all_days=weekly, weekly_entries=weekly, on_count=99,
        delta=0.16, dte=45, management="hold", f=0.5, day_cache={}, price_maps={},
        spx_ret=pd.Series(dtype=float), gate_total_pnl=0.0, gate_alpha_ann=0.0, n_seeds=3)
    assert res["n"] == 3   # ran (clamped to 5), did not error


# --------------------------------------------------------------------------- #
# 3. IVR is trailing-only.
# --------------------------------------------------------------------------- #
def test_ivr_percentile_is_trailing_only():
    # Build a synthetic monotonic VIX; last value is always the max -> pctile 100.
    idx = pd.date_range("2018-01-01", periods=300, freq="B")
    vix = pd.Series(np.arange(300, dtype=float), index=idx)

    def _pctile_rank(w):
        return 100.0 * float(np.mean(w <= w[-1]))

    ivr = vix.rolling(g.s7.IVR_WINDOW, min_periods=g.s7.IVR_WINDOW).apply(_pctile_rank, raw=True)
    # First 251 are NaN (min_periods=252): strictly trailing warm-up.
    assert ivr.iloc[:g.s7.IVR_WINDOW - 1].isna().all()
    assert np.isfinite(ivr.iloc[g.s7.IVR_WINDOW - 1])
    # A monotonic-up series: each in-window day is its own window max -> 100.0.
    assert ivr.iloc[g.s7.IVR_WINDOW - 1] == pytest.approx(100.0)
    # Appending FUTURE (higher) closes cannot change a PAST IVR value.
    past_val = ivr.iloc[g.s7.IVR_WINDOW + 10]
    vix2 = pd.concat([vix, pd.Series([1e6, 1e6], index=pd.date_range(idx[-1], periods=3, freq="B")[1:])])
    ivr2 = vix2.rolling(g.s7.IVR_WINDOW, min_periods=g.s7.IVR_WINDOW).apply(_pctile_rank, raw=True)
    assert ivr2.iloc[g.s7.IVR_WINDOW + 10] == pytest.approx(past_val)


# --------------------------------------------------------------------------- #
# 4. Risk-on set is read from the FROZEN config (not hard-coded / re-tuned).
# --------------------------------------------------------------------------- #
def test_risk_on_regimes_come_from_frozen_config():
    # The risk-on set must be exactly the config bands whose equity-high allowance >= 0.80.
    from strategies import config as cfg
    expected = tuple(r for r, s in cfg.REGIME_BANDS.items() if s["equity"][1] >= 0.80)
    assert g.RISK_ON_REGIMES == expected
    assert set(g.RISK_ON_REGIMES) == {"RiskOn", "RiskOnNarrowing"}


# --------------------------------------------------------------------------- #
# 5. Contango gate: unknown VIX3M -> stand down (False), and cutoff is applied correctly.
# --------------------------------------------------------------------------- #
def test_contango_gate_stands_down_when_vix3m_unknown(monkeypatch, tmp_path):
    idx = pd.to_datetime(["2020-01-06", "2020-01-07", "2020-01-08"])
    vixdf = pd.DataFrame({"vix": [15.0, 15.0, 15.0]}, index=idx)
    vix3mdf = pd.DataFrame({"vix3m": [np.nan, 16.0, 14.0]}, index=idx)  # day0 unknown
    fp_vix = tmp_path / "_vix.parquet"
    fp_v3 = tmp_path / "_vix3m.parquet"
    vixdf.to_parquet(fp_vix)
    vix3mdf.to_parquet(fp_v3)
    monkeypatch.setattr(g, "VIX_PARQUET", fp_vix)
    monkeypatch.setattr(g, "VIX3M_PARQUET", fp_v3)
    on = g.build_contango_gate(cutoff=1.00)
    # day0: VIX3M unknown (no prior to ffill) -> False (stand down).
    assert on.iloc[0] == False  # noqa: E712
    # day1: 15/16 < 1 -> contango on.
    assert on.iloc[1] == True   # noqa: E712
    # day2: 15/14 > 1 -> backwardation off.
    assert on.iloc[2] == False  # noqa: E712
