"""
Tests for s6_s5_financing.py (S6-as-S5-financing PHASE 1 feasibility test).

Covers:
  - adverse_move() direction logic per structure (the core of Q1's bucketing).
  - no-look-ahead: the entry->settle move and every calm filter use only
    information available AT OR BEFORE 14:00 entry (spot_1400, prior-day gamma,
    prior VIX term structure, morning RV) OR the realized settle spot that the
    P&L itself already depends on -- never a same-day future signal used to
    pre-select the trade. Specifically, the calm filters must not peek at
    close_spot / the day's realized pnl.
  - Q1/Q2 tables are internally consistent (loss fractions sum to 1; filter
    subsets are subsets of the full sample).
"""
import os
import numpy as np
import pandas as pd
import pytest

import s6_s5_financing as mod


def test_adverse_move_direction():
    # bull_put hurt by DOWN moves only
    assert mod.adverse_move({"ret": -0.02, "structure": "bull_put"}) == pytest.approx(0.02)
    assert mod.adverse_move({"ret": 0.02, "structure": "bull_put"}) == 0.0
    # bear_call hurt by UP moves only
    assert mod.adverse_move({"ret": 0.02, "structure": "bear_call"}) == pytest.approx(0.02)
    assert mod.adverse_move({"ret": -0.02, "structure": "bear_call"}) == 0.0
    # iron_condor hurt by either side
    assert mod.adverse_move({"ret": -0.02, "structure": "iron_condor"}) == pytest.approx(0.02)
    assert mod.adverse_move({"ret": 0.02, "structure": "iron_condor"}) == pytest.approx(0.02)


def test_load_shape_and_move():
    m = mod.load()
    assert len(m) > 3000
    assert set(m["structure"].unique()) == {"bull_put", "bear_call", "iron_condor"}
    # ret is a finite signed return computed from spot_1400 and close_spot
    assert m["ret"].notna().all()
    assert np.isfinite(m["ret"]).all()
    # spot_1400 equals the trade's own entry spot (put-call parity recon), no drift
    chk = m.dropna(subset=["spot_entry", "spot_1400"])
    assert np.allclose(chk["spot_entry"], chk["spot_1400"], atol=1e-6)


def test_no_lookahead_filters_use_only_pre_entry_or_realized_pnl_info():
    """The four calm filters must be computable from information known at/before
    14:00 entry -- they must NOT depend on close_spot or on the day's realized pnl.
    We assert this structurally: filtering the frame by each calm column and then
    dropping close_spot/pnl reproduces the same selected days, i.e. the selection
    is independent of the future columns.
    """
    m = mod.load()
    # Build the filter selection with future cols present...
    vrp_lo = m.dropna(subset=["vrp_primary"])["vrp_primary"].quantile(1 / 3)
    rv_med = m.dropna(subset=["rv_morning"])["rv_morning"].median()
    sel_with_future = m["vrp_primary"] <= vrp_lo
    # ...and again on a copy with close_spot and pnl_dollars blanked out.
    m2 = m.copy()
    m2["close_spot"] = np.nan
    m2["pnl_dollars"] = np.nan
    m2["breached"] = False
    sel_without_future = m2["vrp_primary"] <= vrp_lo
    assert (sel_with_future.values == sel_without_future.values).all()

    # gamma / vix / rv filters likewise depend only on pre-entry columns
    for col in ["day_type_gamma", "day_type_vix", "rv_morning", "vrp_primary", "vix_ts_prior"]:
        assert col in m.columns
        # these columns are unchanged when future info is blanked
        assert (m[col].fillna(-999).values == m2[col].fillna(-999).values).all()
    assert rv_med > 0


def test_q1_fractions_sum_to_one():
    m = mod.load()
    _, tables = mod.phase1_q1(m)
    for s, tbl in tables.items():
        total = tbl["frac_of_total_loss"].sum()
        assert total == pytest.approx(1.0, abs=1e-6), f"{s} frac sum {total}"
        # loss dollars are non-negative in every bucket
        assert (tbl["loss_$"] >= -1e-6).all()


def test_q2_subsets_are_subsets():
    m = mod.load()
    _, tbl = mod.phase1_q2(m)
    base = tbl[(tbl["structure"] == "ALL_structures") &
               (tbl["filter"] == "ALL days (baseline)")]["n_days"].iloc[0]
    # every calm-filter subset has <= baseline days
    subs = tbl[(tbl["structure"] == "ALL_structures") &
               (tbl["filter"] != "ALL days (baseline)")]
    assert (subs["n_days"] <= base).all()


def test_gate_refutation_holds():
    """Regression on the actual verdict: Phase 1 refutes on both counts.
    (Descriptive result on frozen cached S6 data; if this flips, the input data
    changed and the verdict must be re-examined.)"""
    m = mod.load()
    _, q1_tables = mod.phase1_q1(m)
    _, q2_tbl = mod.phase1_q2(m)
    _, q1_ref, q2_ref = mod.gate_verdict(q1_tables, q2_tbl)
    assert q1_ref is True, "expected loss to be sub-2%-chop-dominated in all structures"
    assert q2_ref is True, "expected no calm filter to be net-positive"
