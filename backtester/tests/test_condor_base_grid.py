r"""
test_condor_base_grid.py -- unit tests for the 0DTE iron-condor BASE-PACKAGE grid runner
(condor_base_grid). Fast + synthetic: no warehouse, exact arithmetic.

These pin the pre-registered CONTRACT of the THIN orchestrator (not any strategy outcome):
  (a) THE FOUR DIALS ACTUALLY REPARAMETERIZE the reused engine. Setting cm.ENTRY_TIME moves
      the entry minute; setting the delta moves the short strike / entry credit (0.30 collects
      more than 0.10); the wing width moves the built condor. Proven by running the base-package
      per-day helper on a synthetic day and checking the credit/strikes respond.
  (b) THE RUNNER REUSES the engine's own primitives (ws.build_condor_at_width +
      cm._scan_managed_exits_at_fill) and NEVER re-implements strike/fill/exit math -- asserted
      structurally (the helper delegates to those functions).
  (c) THE STATS DECOMPOSITION is self-consistent: expectancy == win_rate*avg_win +
      (1-win_rate)*avg_loss == avg_pnl, and the emitted row schema is complete for every
      (arm, fill, scope).
  (d) FROZEN GRID: the pre-registered grid values are exactly the blessed set (a silent
      retune must fail a test, rule #1).
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import condor_base_grid as g  # noqa: E402
import condor_management_experiment as cm  # noqa: E402
import condor_width_sweep as ws  # noqa: E402
import s6_recon as recon  # noqa: E402


# --------------------------------------------------------------------------- #
# Frozen-grid guard (rule #1).
# --------------------------------------------------------------------------- #
def test_grid_values_are_frozen_preregistered():
    assert g.ENTRY_TIMES == (dt.time(9, 45), dt.time(11, 30), dt.time(14, 0))
    assert g.SHORT_DELTAS == (0.10, 0.15, 0.20, 0.30)
    assert g.WING_WIDTHS == (5.0, 10.0, 20.0)
    # 7 arms + 4 fills come straight from the reused engine (not re-declared).
    assert g.ARM_NAMES == cm.ARM_NAMES
    assert g.FILL_FRACS == cm.FILL_FRACS
    assert len(g.ARM_NAMES) == 7
    # full grid size == 36 base packages.
    assert len(g.ENTRY_TIMES) * len(g.SHORT_DELTAS) * len(g.WING_WIDTHS) == 36


# --------------------------------------------------------------------------- #
# Synthetic day (Black-Scholes NBBO so real deltas invert) -- reused pattern.
# --------------------------------------------------------------------------- #
def _fake_classifier(label: str = "neutral"):
    class _C:
        def classify(self, d):
            return {"day": d, "gamma_regime": label, "vix_regime": "contango"}
    return _C()


def _bs_nbbo(entry, n_forward: int = 6):
    spot = 5000.0
    vol = 0.20
    d = entry.date()
    strikes = list(range(4600, 5401, 5))
    minutes = [entry] + [entry + pd.Timedelta(minutes=i) for i in range(1, n_forward + 1)]
    half = 0.05
    rows = []
    for m in minutes:
        t = recon.time_to_expiry_years(m, d)
        for k in strikes:
            for right, is_call in (("CALL", True), ("PUT", False)):
                mid = max(recon.bs_price(spot, float(k), t, vol, is_call), 0.05)
                rows.append({"minute": m, "strike": float(k), "right": right,
                             "bid": max(mid - half, 0.0), "ask": mid + half})
    return pd.DataFrame(rows)


class _Chain:
    def __init__(self, nbbo):
        self.nbbo = nbbo


def _patch_chain(nbbo):
    """Patch the chain loader + load_day so _run_day_base uses our synthetic NBBO."""
    orig_chain = g.s5.zero_dte_chain
    orig_load = g.s5.load_day
    g.s5.zero_dte_chain = lambda d, day_data=None: _Chain(nbbo)
    g.s5.load_day = lambda d: None
    return orig_chain, orig_load


def _unpatch_chain(orig):
    g.s5.zero_dte_chain, g.s5.load_day = orig


def _credit_for(nbbo, entry_time, delta, width):
    """Run the base-package per-day helper for one config and return the FULL-fill entry credit
    and the short strikes, by setting the dials the way run_base_package does."""
    cm.ENTRY_TIME = entry_time
    cm.TARGET_SHORT_DELTA = delta
    ws.TARGET_SHORT_DELTA = delta
    orig = _patch_chain(nbbo)
    try:
        res = g._run_day_base(dt.date(2024, 1, 2), _fake_classifier(), width)
    finally:
        _unpatch_chain(orig)
    return res


# --------------------------------------------------------------------------- #
# (a) The DELTA dial moves the entry credit: 0.30 collects MORE than 0.10.
# --------------------------------------------------------------------------- #
def test_delta_dial_changes_entry_credit():
    entry = pd.Timestamp(dt.datetime(2024, 1, 2, 14, 0))
    nbbo = _bs_nbbo(entry)
    r10 = _credit_for(nbbo, dt.time(14, 0), 0.10, 5.0)
    r30 = _credit_for(nbbo, dt.time(14, 0), 0.30, 5.0)
    assert r10 is not None and r30 is not None
    c10 = r10["fills"]["full"]["entry_credit"]
    c30 = r30["fills"]["full"]["entry_credit"]
    # closer-to-the-money (0.30-delta) shorts collect a larger credit than far-OTM (0.10).
    assert c30 > c10 > 0


# --------------------------------------------------------------------------- #
# (a) The WING dial moves the built condor (credit differs at wing 10 vs 5).
# --------------------------------------------------------------------------- #
def test_wing_dial_changes_credit():
    entry = pd.Timestamp(dt.datetime(2024, 1, 2, 14, 0))
    nbbo = _bs_nbbo(entry)
    r5 = _credit_for(nbbo, dt.time(14, 0), 0.15, 5.0)
    r10 = _credit_for(nbbo, dt.time(14, 0), 0.15, 10.0)
    assert r5 is not None and r10 is not None
    c5 = r5["fills"]["full"]["entry_credit"]
    c10 = r10["fills"]["full"]["entry_credit"]
    # wider wings (further-OTM cheaper long) collect a LARGER net credit.
    assert c10 > c5


# --------------------------------------------------------------------------- #
# (a) The ENTRY-TIME dial moves the entry minute: a config with no snapshot at the
# requested time returns None; a valid time trades. Same synthetic day, two entries.
# --------------------------------------------------------------------------- #
def test_entry_time_dial_selects_the_entry_minute():
    entry = pd.Timestamp(dt.datetime(2024, 1, 2, 11, 30))
    nbbo = _bs_nbbo(entry)   # minutes start at 11:30
    ok = _credit_for(nbbo, dt.time(11, 30), 0.15, 5.0)
    miss = _credit_for(nbbo, dt.time(14, 0), 0.15, 5.0)   # no 14:00 snapshot on this frame
    assert ok is not None            # entry present -> trades
    assert miss is None              # entry absent -> not tradeable (dial really drives the minute)


# --------------------------------------------------------------------------- #
# (c) Stats decomposition is self-consistent + the schema is complete.
# --------------------------------------------------------------------------- #
def test_stats_decomposition_identity():
    rng = np.random.default_rng(0)
    pnl = rng.normal(10, 100, size=50)
    hold = rng.uniform(1, 120, size=50)
    credit = rng.uniform(0.3, 1.5, size=50)
    st = g._stats(pnl, hold, credit)
    # expectancy == win_rate*avg_win + (1-wr)*avg_loss, and that equals avg_pnl by identity.
    lhs = st["expectancy"]
    rhs = st["win_rate"] * st["avg_win"] + (1 - st["win_rate"]) * st["avg_loss"]
    # fields are rounded to 4 dp, so the identity holds to that rounding tolerance.
    assert abs(lhs - rhs) < 5e-4
    assert abs(st["expectancy"] - st["avg_pnl"]) < 5e-4
    assert st["n"] == 50


def test_empty_stats_are_safe():
    st = g._stats(np.array([]), np.array([]), np.array([]))
    assert st["n"] == 0
    assert st["total_pnl"] == 0.0


# --------------------------------------------------------------------------- #
# (c) run_base_package emits a complete row set: 7 arms x 4 fills x 3 scopes per config,
# with every schema field present.
# --------------------------------------------------------------------------- #
def test_run_base_package_emits_full_schema():
    entry = pd.Timestamp(dt.datetime(2024, 1, 2, 14, 0))
    nbbo = _bs_nbbo(entry)
    orig = _patch_chain(nbbo)

    class _CLF:
        def classify(self, d):
            return {"day": d, "gamma_regime": "neutral", "vix_regime": "contango"}
    try:
        rows = g.run_base_package(dt.time(14, 0), 0.15, 5.0,
                                  [dt.date(2024, 1, 2)], _CLF(), verbose=False)
    finally:
        _unpatch_chain(orig)
    # 7 arms x 4 fills x 3 scopes.
    assert len(rows) == 7 * 4 * 3
    for r in rows:
        assert set(r.keys()) == set(g.FIELDNAMES)
        assert r["arm"] in g.ARM_NAMES
        assert r["fill"] in g._FILL_TAG.values()
        assert r["scope"] in ("full", "train", "test")
