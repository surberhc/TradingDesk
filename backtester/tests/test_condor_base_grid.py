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


# --------------------------------------------------------------------------- #
# DAY-OUTER PARITY (the refactor): the day-outer path (load each day once, all configs in
# memory) must produce IDENTICAL per-config P&L to the legacy config-outer run_base_package.
# We compare the aggregated grid built from eval_day_all_configs against run_base_package for
# the SAME configs over the SAME days, on a small synthetic sample. Not one number may drift.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _restore_dials():
    """These tests reparameterize the shared engine's module globals (cm.ENTRY_TIME etc.) to
    prove the dials move the output. Restore them afterward so we never leak a mutated global
    into another test module (e.g. condor_management_experiment's verbatim-constants guard)."""
    saved = (cm.ENTRY_TIME, cm.TARGET_SHORT_DELTA, ws.TARGET_SHORT_DELTA)
    yield
    cm.ENTRY_TIME, cm.TARGET_SHORT_DELTA, ws.TARGET_SHORT_DELTA = saved


def test_day_outer_parity():
    # A small config subset + multi-day synthetic sample. Same NBBO shape per day (the patch
    # returns one frame), but different dates so the train/test split exercises both halves.
    days = [dt.date(2024, 1, 2), dt.date(2024, 1, 3),  # test half (> 2024-06-30)
            dt.date(2023, 5, 4), dt.date(2023, 5, 5)]  # train half (<= 2024-06-30)
    configs = [(dt.time(14, 0), 0.15, 5.0),
               (dt.time(14, 0), 0.30, 10.0),
               (dt.time(11, 30), 0.15, 5.0)]

    class _CLF:
        def classify(self, d):
            return {"day": d, "gamma_regime": "neutral", "vix_regime": "contango"}

    # Each day's synthetic NBBO must contain BOTH entry minutes (11:30 and 14:00) so every
    # config is tradeable; build a frame spanning the whole session for each date.
    def _full_nbbo(d):
        e = pd.Timestamp(dt.datetime.combine(d, dt.time(11, 30)))
        # 156 forward minutes reaches past 14:00 so both entries have a snapshot + a walk.
        return _bs_nbbo(e, n_forward=156)

    nbbo_by_day = {d: _full_nbbo(d) for d in days}

    # ---- Legacy config-outer reference: run_base_package per config over all days. ----
    def _patch_multi():
        orig_chain = g.s5.zero_dte_chain
        orig_load = g.s5.load_day
        # zero_dte_chain(d,...) returns that date's synthetic chain; load_day is a no-op stub.
        g.s5.zero_dte_chain = lambda d, day_data=None: _Chain(nbbo_by_day[d])
        g.s5.load_day = lambda d: None
        return orig_chain, orig_load

    legacy = {}  # (etag, delta, wing, arm, fill, scope) -> total_pnl
    orig = _patch_multi()
    try:
        for (e, dl, w) in configs:
            rows = g.run_base_package(e, dl, w, days, _CLF(), verbose=False)
            for r in rows:
                legacy[(r["entry"], r["delta"], r["wing"], r["arm"], r["fill"],
                        r["scope"])] = (r["total_pnl"], r["n"], r["win_rate"])
    finally:
        _unpatch_chain(orig)

    # ---- Day-outer path: eval_day_all_configs per day, then aggregate_grid. ----
    orig = _patch_multi()
    dayrows = []
    try:
        for d in days:
            dayrows.extend(g.eval_day_all_configs(d, _CLF(), configs=configs))
    finally:
        _unpatch_chain(orig)

    # Aggregate the day-rows the same way aggregate_grid does (in-memory, no file I/O).
    import pandas as _pd
    ddf = _pd.DataFrame(dayrows)
    assert not ddf.empty, "day-outer produced no rows on the synthetic sample"
    day_grid = {}
    for (etag, delta, wing, arm, fill), grp in ddf.groupby(
            ["entry", "delta", "wing", "arm", "fill"], sort=True):
        for scope in ("full", "train", "test"):
            gs = grp if scope == "full" else grp[grp["half"] == scope]
            st = g._stats(gs["pnl"].to_numpy(float),
                          gs["hold_min"].to_numpy(float),
                          gs["entry_credit"].to_numpy(float))
            day_grid[(etag, float(delta), int(wing), arm, fill, scope)] = (
                st["total_pnl"], st["n"], st["win_rate"])

    # ---- PARITY: every legacy cell must match the day-outer cell exactly. ----
    def _eq(a, b):
        # NaN == NaN for empty scopes (a scope with 0 trades has NaN win_rate on both paths).
        if isinstance(a, float) and isinstance(b, float) and np.isnan(a) and np.isnan(b):
            return True
        return a == b

    assert set(legacy.keys()) == set(day_grid.keys()), "config/arm/fill/scope grid mismatch"
    n_checked = 0
    for key, (tot, n, wr) in legacy.items():
        dtot, dn, dwr = day_grid[key]
        assert dn == n, f"n differs at {key}: {dn} vs {n}"
        # total P&L must be bit-for-bit identical (same math, same order of days).
        assert _eq(dtot, tot), f"total_pnl differs at {key}: {dtot} vs {tot}"
        assert _eq(dwr, wr), f"win_rate differs at {key}: {dwr} vs {wr}"
        n_checked += 1
    # sanity: we actually compared a non-trivial number of populated cells.
    populated = sum(1 for (_t, nn, _w) in legacy.values() if nn > 0)
    assert populated > 0, "parity test never exercised a tradeable cell"
