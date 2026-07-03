"""
test_reentry_ladder.py — unit + parity tests for the re-entry scale-in ladder overlay.

The two loads that matter:
  1. OFF-PARITY (the safety net): with the overlay OFF (mode="control"), the S0 backtest
     must be BYTE-IDENTICAL to current production (backtest.run_backtest with the stock
     AdaptiveAllWeather). This test asserts exact NAV, weights, and monthly-target equality.
  2. The ladder's pre-registered MECHANICS: the 1/3 -> 2/3 -> 1 rung sequence on a
     re-entry, immediate exit-override (never slow scaling OUT), and realized <= engine.

Mechanics are tested on the pure _ladder_multiplier state machine driven by a synthetic
engine-target sequence, so they are deterministic and data-independent.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies import config
from src import backtest, data_loader
from src import reentry_ladder as rl
from src.reentry_ladder import LadderedAllWeather, LADDER_MULTIPLIERS


# ---------------------------------------------------------------------------
# Shared data load (once) for the end-to-end parity test.
# ---------------------------------------------------------------------------
def _load_inputs():
    prices = data_loader.load_prices()
    bond_t = config.BENCHMARK_6040[1]
    if bond_t not in prices.columns:
        try:
            prices = prices.join(data_loader.load_prices([bond_t]))
        except Exception:
            pass
    try:
        hyg = data_loader.load_prices([config.CREDIT_PROXY[0]])[config.CREDIT_PROXY[0]]
    except Exception:
        hyg = None
    yld, _ = data_loader.load_treasury_10y()
    vix, _ = data_loader.load_vix()
    oas, _ = data_loader.load_hy_oas()
    return prices, yld, hyg, vix, oas


START = "2007-01-01"
VERSION = "Balanced"


# ===========================================================================
# 1. Pure state-machine mechanics (deterministic, no data)
# ===========================================================================
def _run_sequence(targets):
    """Feed a sequence of engine targets through a fresh ladder; return the m's."""
    strat = LadderedAllWeather(ladder_enabled=True)
    return [strat._ladder_multiplier(t) for t in targets]


def test_reentry_produces_three_rung_ladder():
    # De-risked (0) then rising steadily -> the FIRST 3 rebalances get 1/3, 2/3, 1,
    # then the ladder is inactive (m=1 thereafter while still rising).
    seq = [0.0, 0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    m = _run_sequence(seq)
    assert m[0] == 1.0 and m[1] == 1.0          # still de-risked -> follow engine
    assert m[2] == pytest.approx(1.0 / 3.0)     # rung 1
    assert m[3] == pytest.approx(2.0 / 3.0)     # rung 2
    assert m[4] == pytest.approx(1.0)           # rung 3
    assert m[5] == 1.0 and m[6] == 1.0          # ladder done -> engine unchanged


def test_exit_overrides_and_aborts_ladder():
    # Re-entry starts, then engine target FALLS on rung 2 -> follow the drop immediately
    # (m=1, i.e. never slow the exit), ladder aborts. A later re-entry re-arms fresh.
    seq = [0.0, 0.3, 0.5, 0.2, 0.0, 0.4]
    #        -    r1    r2   DROP  0    re-entry
    m = _run_sequence(seq)
    assert m[1] == pytest.approx(1.0 / 3.0)     # rung 1
    assert m[2] == pytest.approx(2.0 / 3.0)     # rung 2
    assert m[3] == 1.0                          # engine fell -> exit override, no slowing
    assert m[4] == 1.0                          # de-risked, ladder inactive
    assert m[5] == pytest.approx(1.0 / 3.0)     # fresh re-entry re-arms at rung 1


def test_realized_never_exceeds_engine():
    # m is always in [0,1]: realized = m*engine can never exceed the engine target.
    seq = [0.0, 0.1, 0.3, 0.5, 0.2, 0.6, 0.9, 1.0, 0.0, 0.5, 0.7, 1.0]
    for m in _run_sequence(seq):
        assert 0.0 <= m <= 1.0


def test_no_reentry_no_ladder():
    # A book that stays fully invested (never de-risks) never triggers the ladder.
    seq = [0.8, 0.9, 1.0, 1.0, 0.95, 1.0]
    m = _run_sequence(seq)
    assert all(x == 1.0 for x in m)


def test_flat_after_reentry_holds_rung_without_advancing():
    # If the engine target goes flat mid-ladder (not rising), hold the current rung's
    # cap and do NOT advance — we only advance when actually scaling in.
    seq = [0.0, 0.3, 0.3, 0.5, 0.7]
    #        -    r1  flat  adv  adv
    m = _run_sequence(seq)
    assert m[1] == pytest.approx(1.0 / 3.0)     # rung 1
    assert m[2] == pytest.approx(1.0 / 3.0)     # flat -> hold rung 1
    assert m[3] == pytest.approx(2.0 / 3.0)     # rising again -> rung 2
    assert m[4] == pytest.approx(1.0)           # rung 3


def test_ladder_multipliers_are_the_pre_registered_thirds():
    assert LADDER_MULTIPLIERS == (1.0 / 3.0, 2.0 / 3.0, 1.0)


# ===========================================================================
# 2. OFF-PARITY — the safety net. control == production, EXACTLY.
# ===========================================================================
@pytest.fixture(scope="module")
def inputs():
    return _load_inputs()


def test_off_parity_byte_identical_to_production(inputs):
    prices, yld, hyg, vix, oas = inputs

    # Production: the stock strategy through the canonical runner.
    prod = backtest.run_backtest(prices, yld, hyg, vix, oas, start=START, version=VERSION)
    # Control: the ladder overlay in OFF mode (LadderedAllWeather, both flags off).
    ctrl = rl.run_laddered_backtest(prices, yld, hyg, vix, oas,
                                    start=START, version=VERSION, mode="control")

    # NAV: byte-identical.
    pd.testing.assert_series_equal(prod["nav"], ctrl["nav"])
    pd.testing.assert_series_equal(prod["returns"], ctrl["returns"])
    # Executed target weights: byte-identical.
    pd.testing.assert_frame_equal(prod["weights"], ctrl["weights"])
    # Regime / equity_target / score path: identical.
    for col in ("regime", "score", "equity_target", "ladder_stage"):
        pd.testing.assert_series_equal(
            prod["monthly"][col], ctrl["monthly"][col], check_names=False
        )


def test_ladder_changes_something_but_preserves_composition(inputs):
    # Sanity: the ladder ON must actually differ from control (it caps some months),
    # yet NEVER hold MORE equity than the engine wanted on any month.
    prices, yld, hyg, vix, oas = inputs
    lad = rl.run_laddered_backtest(prices, yld, hyg, vix, oas,
                                   start=START, version=VERSION, mode="ladder")
    m = lad["monthly"]
    # Realized equity target <= engine target on EVERY month (never exceeds).
    assert (m["equity_target"] <= m["engine_equity_target"] + 1e-12).all()
    # At least one month was actually capped (ladder fired at some re-entry).
    assert (m["ladder_multiplier"] < 1.0 - 1e-9).any()
