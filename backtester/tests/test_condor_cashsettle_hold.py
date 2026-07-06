r"""
test_condor_cashsettle_hold.py -- guards for ARM 5, the 0DTE iron-condor PURE
HOLD-TO-CASH-SETTLEMENT arm (condor_cashsettle_hold).

Pins the CONTRACT (the mechanism), not any strategy outcome:
  (a) NO EARLY EXIT EVER -- every position resolves at 16:00 cash settlement; there is no
      profit-target / stop / early-close code path, and the settle P&L ignores every
      intermediate minute (so no future minute can change it -- no look-ahead).
  (b) SETTLEMENT IS COSTLESS INTRINSIC -- the exit charges NO bid/ask spread. The condor's
      settle value is credit minus capped short-strike intrinsic against the 16:00 index, and
      it does NOT depend on the 16:00 quotes' bid/ask width at all.
  (c) FROZEN CONSTANTS -- the width ladder is the pre-registered 5/10/20/30/50, the 5-pt control
      is present, the entry chassis is inherited verbatim from the control, and there is no
      management dial.

The intrinsic tests use pure arithmetic -- no warehouse needed.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import condor_cashsettle_hold as ch  # noqa: E402
import s6_control as ctrl  # noqa: E402
import condor_management_experiment as cm  # noqa: E402


# --------------------------------------------------------------------------- #
# (c) FROZEN CONSTANTS -- the ladder, the control width, the entry chassis, no management dial.
# --------------------------------------------------------------------------- #
def test_width_ladder_is_the_preregistered_frozen_set():
    assert ch.WIDTHS == (5.0, 10.0, 20.0, 30.0, 50.0)   # exactly Arm 1's five, NOT swept-to-winner
    assert ctrl.SPREAD_WIDTH in ch.WIDTHS               # the 5-pt control baseline is present
    assert ch.WIDTH_TAGS == ("w5", "w10", "w20", "w30", "w50")


def test_entry_chassis_inherited_verbatim_from_control():
    assert ch.ENTRY_TIME == ctrl.ENTRY_TIME
    assert ch.SETTLEMENT_TIME == ctrl.SETTLEMENT_TIME
    assert ch.TARGET_SHORT_DELTA == ctrl.TARGET_SHORT_DELTA
    assert ch.MIN_ENTRY_CREDIT == ctrl.MIN_ENTRY_CREDIT
    # The blended ENTRY-credit fill band is the management experiment's own (one code path).
    assert ch.cm._blended_credit_to_open is cm._blended_credit_to_open
    assert ch.FILL_FRACS == (0.0, 0.25, 0.50, 1.0)
    assert ch.HEADLINE_FILL == 0.50


def test_there_is_no_management_dial():
    """Arm 5 has NONE of the management knobs that the managed arms carry. Their absence is the
    whole point -- assert they are not present as module constants here."""
    for knob in ("PROFIT_TARGET_FRAC", "PROFIT_TARGET_FRACS", "STOP_MULTIPLE",
                 "WINNER_DEBIT", "TIME_EXIT_TIMES", "COMBO_PROFIT_FRAC"):
        assert not hasattr(ch, knob), f"Arm 5 must NOT define a management knob ({knob})"


def test_no_early_exit_scan_exists_in_the_module():
    """There is no minute-walk exit scan callable at all -- the only resolution is cash
    settlement. Guards against someone quietly reintroducing an early-management code path.
    (The module DOCSTRING names these to explain the contrast with the managed arms, so we scan
    the module's actual callables, not its prose.)"""
    callables = {name for name, obj in inspect.getmembers(ch)
                 if inspect.isfunction(obj) and obj.__module__ == ch.__name__}
    for banned in ("_scan_exit", "scan_pt25_exit_at_fill", "_scan_managed_exits",
                   "_scan_managed_exits_at_fill"):
        assert banned not in callables, f"Arm 5 must not define an early-exit scan ({banned})"
    # And the module must not have imported such a scan into its own namespace as a bare name.
    for banned in ("_scan_exit", "scan_pt25_exit_at_fill", "_scan_managed_exits"):
        assert not hasattr(ch, banned), f"Arm 5 must not expose an early-exit scan ({banned})"


# --------------------------------------------------------------------------- #
# (b) SETTLEMENT IS COSTLESS INTRINSIC -- pure arithmetic on condor_cash_settle_pnl.
# --------------------------------------------------------------------------- #
def test_winner_keeps_full_credit_when_index_settles_between_the_shorts():
    """S* strictly between the shorts -> both intrinsic terms zero -> keep the whole credit.
    No spread is subtracted (a managed close would have paid a 4-leg debit here)."""
    pnl = ch.condor_cash_settle_pnl(entry_credit=1.20, settle_spot=5050.0,
                                    short_put_k=5000.0, short_call_k=5100.0, width=20.0)
    assert pnl == pytest.approx(1.20)


def test_put_breach_loss_is_capped_at_the_wing_width():
    """S* far below the short put -> loss capped at the wing width (defined risk), NOT unbounded."""
    # 200 points below the short put, but a 20-wide wing caps the loss at 20.
    pnl = ch.condor_cash_settle_pnl(entry_credit=1.00, settle_spot=4800.0,
                                    short_put_k=5000.0, short_call_k=5100.0, width=20.0)
    assert pnl == pytest.approx(1.00 - 20.0)


def test_partial_put_breach_is_linear_intrinsic():
    """S* 8 points below the short put, inside a 20-wide wing -> loss is exactly 8 points."""
    pnl = ch.condor_cash_settle_pnl(entry_credit=1.00, settle_spot=4992.0,
                                    short_put_k=5000.0, short_call_k=5100.0, width=20.0)
    assert pnl == pytest.approx(1.00 - 8.0)


def test_call_breach_loss_is_capped_at_the_wing_width():
    pnl = ch.condor_cash_settle_pnl(entry_credit=1.00, settle_spot=5400.0,
                                    short_put_k=5000.0, short_call_k=5100.0, width=30.0)
    assert pnl == pytest.approx(1.00 - 30.0)


def test_settlement_ignores_bid_ask_entirely_no_exit_spread_charged():
    """The settle P&L is a function of (credit, S*, strikes, width) ONLY -- it never sees a
    bid or an ask. Two 16:00 chains with WILDLY different bid/ask WIDTHS but the same recovered
    index level must give the IDENTICAL settle P&L. That is the mechanism: zero exit spread."""
    spk, sck, w = 5000.0, 5100.0, 20.0
    credit = 1.00
    settle_spot = 4995.0  # a 5-pt put breach
    # Compute directly from strikes + level; there is no quote input at all.
    pnl = ch.condor_cash_settle_pnl(credit, settle_spot, spk, sck, w)
    assert pnl == pytest.approx(1.00 - 5.0)
    # Sanity: the function signature takes NO snapshot / bid / ask argument.
    params = set(inspect.signature(ch.condor_cash_settle_pnl).parameters)
    assert params == {"entry_credit", "settle_spot", "short_put_k", "short_call_k", "width"}


# --------------------------------------------------------------------------- #
# (a) NO LOOK-AHEAD -- the run_day settlement uses ONLY the 14:00 (entry) and 16:00 (settle)
# snapshots. A catastrophic intermediate minute cannot change the booked P&L, because no
# intermediate minute is consulted for any decision. We verify with an in-memory chain.
# --------------------------------------------------------------------------- #
class _StubClassifier:
    def classify(self, d):
        return {"gamma_regime": "neutral", "vix_regime": "contango"}


def _chain_with_settle(entry_minute, settle_minute, mid_minute,
                       short_put_k, short_call_k, width, entry_spot, settle_spot):
    """Build a minimal 0DTE NBBO grid: an entry snapshot that recovers `entry_spot` and builds a
    condor at `width`, a benign-vs-catastrophic intermediate minute, and a 16:00 snapshot that
    recovers `settle_spot`. Put-call parity spot recovery needs >=3 common strikes near ATM with
    C - P = (F - K); we lay a small ATM ladder that satisfies it plus the four condor legs."""
    def parity_rows(minute, spot):
        rows = []
        # ATM ladder for spot recovery: C - P = spot - K at each strike (T~0 -> disc~1).
        for k in (spot - 10, spot, spot + 10):
            c = max(spot - k, 0.0) + 5.0   # arbitrary time value, symmetric so C - P = spot - k
            p = max(k - spot, 0.0) + 5.0
            rows.append({"minute": minute, "strike": float(k), "right": "CALL", "bid": c - 0.1, "ask": c + 0.1})
            rows.append({"minute": minute, "strike": float(k), "right": "PUT", "bid": p - 0.1, "ask": p + 0.1})
        return rows

    rows = []
    # Entry snapshot: parity ladder (recovers entry_spot) + the four condor legs with a real
    # bid/ask so build_condor_at_width + the fill band work.
    rows += parity_rows(entry_minute, entry_spot)
    for k, right, b, a in [(short_put_k, "PUT", 2.0, 2.2),
                           (short_put_k - width, "PUT", 0.5, 0.6),
                           (short_call_k, "CALL", 2.0, 2.2),
                           (short_call_k + width, "CALL", 0.5, 0.6)]:
        rows.append({"minute": entry_minute, "strike": float(k), "right": right, "bid": b, "ask": a})
    # Intermediate minute -- values here must NOT matter to the settle P&L.
    for k, right, b, a in [(short_put_k, "PUT", 99.0, 99.9)]:
        rows.append({"minute": mid_minute, "strike": float(k), "right": right, "bid": b, "ask": a})
    # 16:00 snapshot: parity ladder recovering settle_spot.
    rows += parity_rows(settle_minute, settle_spot)
    return pd.DataFrame(rows)


def test_run_day_settles_only_from_1600_ignoring_intermediate_minutes(monkeypatch):
    """Two identical days differing ONLY in an intermediate minute's (huge) quote must book the
    SAME settle P&L, because run_day consults only 14:00 (entry) and 16:00 (settle)."""
    import datetime as dt
    d = dt.date(2024, 1, 2)
    entry_minute = pd.Timestamp(dt.datetime.combine(d, ch.ENTRY_TIME))
    settle_minute = pd.Timestamp(dt.datetime.combine(d, ch.SETTLEMENT_TIME))
    mid_minute = pd.Timestamp(dt.datetime(2024, 1, 2, 15, 0))
    entry_spot, settle_spot = 5050.0, 4995.0   # a small put breach at settle

    # Short strikes the control's 0.15-delta picker will select from our synthetic deltas:
    # simplest is to stub build_condor_at_width to return fixed legs so the test is deterministic
    # and independent of the delta recon (which the intrinsic math does not need).
    def _stub_build(snap, dtbl, w):
        spk, sck = 5000.0, 5100.0
        return {"short_put_k": spk, "long_put_k": spk - w,
                "short_call_k": sck, "long_call_k": sck + w,
                "entry_credit": 1.00,
                "legs": [(spk, "PUT", +1), (spk - w, "PUT", -1),
                         (sck, "CALL", +1), (sck + w, "CALL", -1)]}
    monkeypatch.setattr(ch.ws, "build_condor_at_width", _stub_build)
    # Make the blended entry credit deterministic (1.00 at every fill) so P&L is exactly credit-intrinsic.
    monkeypatch.setattr(ch.cm, "_blended_credit_to_open", lambda snap, legs, f: 1.00)

    chain = _chain_with_settle(entry_minute, settle_minute, mid_minute,
                               5000.0, 5100.0, 20.0, entry_spot, settle_spot)

    class _StubChain:
        def __init__(self, nbbo): self.nbbo = nbbo

    # Stub the data layer so run_day uses our in-memory chain and recovers our spots.
    monkeypatch.setattr(ch.s5, "load_day", lambda d: object())
    monkeypatch.setattr(ch.s5, "zero_dte_chain", lambda d, day_data=None: _StubChain(chain))
    monkeypatch.setattr(ch.recon, "per_strike_delta",
                        lambda snap, m, d, spot: pd.DataFrame(
                            {"strike": [5000.0, 5100.0], "right": ["PUT", "CALL"], "delta": [-0.15, 0.15]}))

    rec = ch.run_day(d, _StubClassifier())
    assert rec.traded, rec.skip_reason
    # Settle at 4995 with 20-wide wing: put breach of 5 pts -> P&L = (1.00 - 5.0)*100 = -400.
    pnl_w20_f50 = rec.widths["w20"]["f50"]["pnl"]
    assert pnl_w20_f50 == pytest.approx((1.00 - 5.0) * ch.CONTRACT_MULTIPLIER)
    # The recovered settle level must be ~4995 (parity), and the breach recorded as a put breach.
    assert rec.settle_spot == pytest.approx(4995.0, abs=1.0)
    assert rec.widths["w20"]["breach"] == "put"
    assert rec.widths["w20"]["breach_depth"] == pytest.approx(5.0, abs=1.0)

    # Now blow up the intermediate minute -- the booked settle P&L must be IDENTICAL.
    chain2 = _chain_with_settle(entry_minute, settle_minute, mid_minute,
                                5000.0, 5100.0, 20.0, entry_spot, settle_spot)
    chain2.loc[chain2["minute"] == mid_minute, ["bid", "ask"]] = [9999.0, 99999.0]
    monkeypatch.setattr(ch.s5, "zero_dte_chain", lambda d, day_data=None: _StubChain(chain2))
    rec2 = ch.run_day(d, _StubClassifier())
    assert rec2.widths["w20"]["f50"]["pnl"] == pytest.approx(pnl_w20_f50)
