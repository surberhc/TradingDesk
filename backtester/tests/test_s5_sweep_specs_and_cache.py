r"""
test_s5_sweep_specs_and_cache.py -- the NEW StructureSpec builders on s5_financing_sweep and
the shared UNGATED-universe BACKTEST CACHE.

Pins:
  * each new spec (put_write / iron_condor neutral+income / put_calendar / sell_vs_tail)
    builds a harness Structure with the expected legs when called with (tenor, delta, mgmt);
  * the cache makes a GATED (calm_only) cell REUSE the ungated universe backtest instead of
    recomputing it -- and yields byte-identical net_pct_yr_of_core to the uncached path;
  * clear_backtest_cache empties the memo.

Hermetic: h.backtest_structure is monkeypatched to a synthetic per-entry-day trade frame and
a CALL COUNTER, so we can both (a) assert the cache cut the number of universe backtests and
(b) check numeric identity -- with NO warehouse read.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s5_financing_harness as h  # noqa: E402
import s5_financing_sweep as s     # noqa: E402


# --------------------------------------------------------------------------- #
# SPEC builders -- each maps (tenor, delta, mgmt) onto the expected harness legs.
# --------------------------------------------------------------------------- #
def test_put_write_spec_builds_single_short_put():
    spec = s.put_write_spec()
    struct = spec.builder(45, 0.15, s.build_management("hold_to_expiry"))
    assert len(struct.legs) == 1
    assert (struct.legs[0].right, struct.legs[0].action) == ("PUT", "sell")
    assert struct.legs[0].target_delta == pytest.approx(0.15)
    assert spec.net_credit is True


def test_iron_condor_spec_neutral_and_income_arms():
    neutral = s.iron_condor_spec(wing=10.0)                      # call_delta=None -> half
    income = s.iron_condor_spec(wing=10.0, call_delta=0.15)      # explicit nearer call
    sn = neutral.builder(45, 0.20, s.build_management("hold_to_expiry"))
    si = income.builder(45, 0.20, s.build_management("hold_to_expiry"))
    # neutral: short call delta = short_delta * 0.5
    scn = [l for l in sn.legs if l.right == "CALL" and l.action == "sell"][0]
    assert scn.target_delta == pytest.approx(0.10)
    # income: short call delta fixed at 0.15 regardless of the put short_delta
    sci = [l for l in si.legs if l.right == "CALL" and l.action == "sell"][0]
    assert sci.target_delta == pytest.approx(0.15)
    assert len(sn.legs) == 4 and len(si.legs) == 4
    assert "neutral" in neutral.name and "income" in income.name


def test_put_calendar_spec_builds_debit_two_leg_with_dte_override():
    spec = s.put_calendar_spec(back_dte_mult=2.0, strike_offset=-25.0)
    struct = spec.builder(45, 0.15, s.build_management("hold_to_expiry"))
    assert len(struct.legs) == 2
    front, back = struct.legs
    assert (front.right, front.action) == ("PUT", "sell") and front.dte is None
    assert (back.right, back.action) == ("PUT", "buy") and back.dte == 90
    assert back.strike_offset == pytest.approx(-25.0)
    assert spec.net_credit is False    # a long calendar is a net-DEBIT family


def test_sell_vs_tail_spec_builds_short_plus_owned_tail():
    spec = s.sell_against_owned_tail_spec(tail_moneyness=-0.20, tail_dte=63)
    struct = spec.builder(45, 0.15, s.build_management("hold_to_expiry"))
    assert len(struct.legs) == 2
    short, tail = struct.legs
    assert (short.right, short.action) == ("PUT", "sell")
    assert (tail.right, tail.action) == ("PUT", "buy")
    assert tail.target_moneyness == pytest.approx(-0.20) and tail.dte == 63
    assert spec.net_credit is True


# --------------------------------------------------------------------------- #
# The shared UNGATED-universe cache: reuse + numeric identity.
# --------------------------------------------------------------------------- #
def _fake_universe_frame():
    """A synthetic per-entry-day trade frame over a short window, with SOME days 'not calm'
    so the calm filter genuinely takes a subset. entry_underlying constant so core notional
    is fixed; net_pnl varies by day."""
    days = [dt.date(2023, 1, d) for d in range(3, 28)]  # 25 business-ish days
    rng = np.random.default_rng(7)
    rows = []
    for i, d in enumerate(days):
        rows.append({
            "name": "fake", "entry_date": d, "exit_date": d,
            "exit_reason": "settle", "hold_days": 45,
            "entry_credit": 2000.0, "net_pnl": float(100 + 10 * (i % 5) - (i % 3) * 40),
            "total_commission": 1.30, "entry_underlying": 4000.0, "exit_underlying": 4010.0,
        })
    df = pd.DataFrame(rows)
    df.attrs["truncated_dropped"] = 0
    df.attrs["entry_rejects"] = {"min_credit_floor": 0, "unfillable_or_unselectable": 0}
    return df


@pytest.fixture
def hermetic_sweep(monkeypatch):
    """Patch the harness backtest + the calm filter + available_days + windows so the sweep
    runs with NO warehouse. Returns a call-counter for h.backtest_structure."""
    frame = _fake_universe_frame()
    all_days = list(frame["entry_date"])
    calls = {"n": 0}

    def fake_backtest_structure(structure, start=None, end=None, entry_days=None, **kw):
        calls["n"] += 1
        df = frame.copy()
        df.attrs = dict(frame.attrs)
        if entry_days is not None:
            es = set(entry_days)
            df = df[df["entry_date"].isin(es)].reset_index(drop=True)
            df.attrs = dict(frame.attrs)
        if start is not None:
            df = df[df["entry_date"] >= start].reset_index(drop=True); df.attrs = dict(frame.attrs)
        if end is not None:
            df = df[df["entry_date"] <= end].reset_index(drop=True); df.attrs = dict(frame.attrs)
        return df

    # calm = every other day is calm (a genuine subset, deterministic)
    calm_days = set(all_days[::2])

    monkeypatch.setattr(h, "backtest_structure", fake_backtest_structure)
    monkeypatch.setattr(s.h, "backtest_structure", fake_backtest_structure)
    monkeypatch.setattr(s.h, "available_days", lambda clean_only=True: list(all_days))
    monkeypatch.setattr(s, "calm_entry_filter",
                        lambda vix_level=s.CALM_VIX_LEVEL: (lambda d: d in calm_days))
    monkeypatch.setattr(s, "WINDOWS", {"B": (all_days[0], all_days[-1])})
    s.clear_backtest_cache()
    return calls


def test_gated_cell_reuses_cached_universe(hermetic_sweep):
    calls = hermetic_sweep
    spec = s.put_write_spec()
    # ungated first (populates cache), then the gated cell must NOT trigger a new backtest.
    s._run_cell_window(spec, 45, 0.15, "hold_to_expiry", "ungated", "B", 20, 1, use_cache=True)
    n_after_ungated = calls["n"]
    s._run_cell_window(spec, 45, 0.15, "hold_to_expiry", "calm_only", "B", 20, 1, use_cache=True)
    # gated cell reused the cached universe -> zero additional harness backtests.
    assert calls["n"] == n_after_ungated


def test_cache_numerically_identical_to_uncached(hermetic_sweep):
    spec = s.put_write_spec()

    def cell(regime, use_cache):
        s.clear_backtest_cache()
        return s._run_cell_window(spec, 45, 0.15, "hold_to_expiry", regime, "B", 20, 1,
                                  use_cache=use_cache)

    for regime in ("ungated", "calm_only"):
        cached = cell(regime, True)
        uncached = cell(regime, False)
        assert cached["net_pct_yr_of_core"] == pytest.approx(uncached["net_pct_yr_of_core"],
                                                             rel=0, abs=0)
        assert cached["n_trades"] == uncached["n_trades"]
        assert cached["sharpe_ann"] == pytest.approx(uncached["sharpe_ann"], nan_ok=True)


def test_clear_backtest_cache_empties_memo(hermetic_sweep):
    spec = s.put_write_spec()
    s._universe_trades(spec, 45, 0.15, "hold_to_expiry", "B", use_cache=True)
    assert len(s._UNIVERSE_CACHE) == 1
    s.clear_backtest_cache()
    assert len(s._UNIVERSE_CACHE) == 0
