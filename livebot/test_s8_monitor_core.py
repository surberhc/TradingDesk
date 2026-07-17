"""test_s8_monitor_core.py — exhaustive OFFLINE tests for the S8 exit-logic core.

100% offline: NO IBKR, NO network, NO store I/O. Pure functions over synthetic samples.

Run (from C:\\TradingDesk\\livebot):
    powershell -Command "$env:PYTHONPATH=''; C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s8_monitor_core.py -q"

Timestamps are epoch seconds throughout (see s8_monitor_core module docstring).
"""

from __future__ import annotations

import math

import pytest

from s8_monitor_core import (
    MonitorPosition,
    MonitorState,
    Sample,
    build_exit_info,
    close_at_session_end,
    pnl_at,
    process_sample,
)

# The stored stop_price is the FROZEN one computed at entry by s8_strategy.stop_price.
# We import it to tie these tests to the real frozen formula rather than hardcoding a
# level (the core itself never recomputes it — it reads position.stop_price verbatim).
from s8_strategy import stop_price as frozen_stop_price


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

CREDIT = 4.05          # points received at entry
STOP_MULTIPLE = 2.0    # frozen template knob
ENTRY_TS = 1_000_000.0  # epoch seconds


def make_position(qty: int = 1) -> MonitorPosition:
    stop = frozen_stop_price(CREDIT, STOP_MULTIPLE)
    # floor(10*(4.05+2.0))/10 = floor(60.5)/10 = 6.0
    assert stop == 6.0
    return MonitorPosition(
        trade_id="20260717:Puts-80-$4:12:35:7480:7445",
        side="PUT",
        short_strike=7480.0,
        long_strike=7445.0,
        qty=qty,
        realized_credit=CREDIT,
        stop_price=stop,
        entry_ts=ENTRY_TS,
    )


def run_stream(position, samples, state=None):
    if state is None:
        state = MonitorState()
    for s in samples:
        process_sample(position, state, s)
    return state


# --------------------------------------------------------------------------- #
# pnl_at sign sanity (hand-computed)
# --------------------------------------------------------------------------- #

def test_pnl_at_positive_when_closing_cheap():
    pos = make_position()
    # spread_close_value = 2.0 - 0.5 = 1.5; pnl = (4.05 - 1.5)*100 = 255.0
    s = Sample(ts=ENTRY_TS + 60, short_ask=2.0, long_bid=0.5)
    assert pnl_at(pos, s) == pytest.approx(255.0)


def test_pnl_at_negative_when_closing_rich():
    pos = make_position()
    # spread_close_value = 6.0 - 1.0 = 5.0; pnl = (4.05 - 5.0)*100 = -95.0
    s = Sample(ts=ENTRY_TS + 60, short_ask=6.0, long_bid=1.0)
    assert pnl_at(pos, s) == pytest.approx(-95.0)


def test_pnl_at_scales_with_qty():
    pos = make_position(qty=3)
    s = Sample(ts=ENTRY_TS + 60, short_ask=2.0, long_bid=0.5)
    assert pnl_at(pos, s) == pytest.approx(255.0 * 3)


def test_pnl_at_none_when_price_missing():
    pos = make_position()
    assert pnl_at(pos, Sample(short_ask=None, long_bid=0.5)) is None
    assert pnl_at(pos, Sample(short_ask=2.0, long_bid=None)) is None
    assert pnl_at(pos, Sample(short_ask=None, long_bid=None)) is None


# --------------------------------------------------------------------------- #
# stop-hit: triggers at the FIRST crossing sample
# --------------------------------------------------------------------------- #

def test_stop_hit_triggers_at_first_crossing():
    pos = make_position()
    # short_ask rises and crosses stop_price (6.0). long_bid constant 0.5.
    asks = [2.0, 4.0, 5.9, 6.0, 7.0]     # first cross at index 3 (== 6.0, the >= boundary)
    samples = [
        Sample(ts=ENTRY_TS + 60 * (i + 1), short_ask=a, short_bid=a - 0.2,
               long_bid=0.5, long_ask=0.7, spot=7400.0 - i)
        for i, a in enumerate(asks)
    ]
    state = run_stream(pos, samples)

    assert state.triggered is True
    assert state.exit_reason == "stop_hit"
    # The exit sample is the FIRST one at/above the stop (short_ask == 6.0), not later.
    assert state.exit_sample is samples[3]
    assert state.exit_sample.short_ask == 6.0
    assert state.n_samples == 5

    # pnl at the crossing is negative and correct:
    # spread_close = 6.0 - 0.5 = 5.5; pnl = (4.05 - 5.5)*100 = -145.0
    info = build_exit_info(pos, state)
    assert info["exit_reason"] == "stop_hit"
    assert info["pnl"] == pytest.approx(-145.0)
    assert info["pnl"] < 0
    assert info["spread_value_at_exit"] == pytest.approx(5.5)
    assert info["exit_ts"] == samples[3].ts
    assert info["exit_spot"] == samples[3].spot
    assert info["duration_secs"] == pytest.approx(samples[3].ts - ENTRY_TS)


def test_stop_hit_strictly_below_does_not_trigger():
    pos = make_position()
    # short_ask reaches 5.9 (just under 6.0) — must NOT fire.
    samples = [Sample(ts=ENTRY_TS + 60, short_ask=5.9, long_bid=0.5)]
    state = run_stream(pos, samples)
    assert state.triggered is False
    assert state.exit_reason is None
    assert state.exit_sample is None


# --------------------------------------------------------------------------- #
# never-hit -> eod
# --------------------------------------------------------------------------- #

def test_never_hit_then_eod_positive_pnl():
    pos = make_position()
    # short_ask stays well below the 6.0 stop the whole session.
    asks = [2.0, 2.5, 2.0, 1.5]
    long_bids = [0.5, 0.5, 0.4, 0.3]
    samples = [
        Sample(ts=ENTRY_TS + 60 * (i + 1), short_ask=a, short_bid=a - 0.2,
               long_bid=lb, long_ask=lb + 0.2, spot=7400.0)
        for i, (a, lb) in enumerate(zip(asks, long_bids))
    ]
    state = run_stream(pos, samples)
    assert state.triggered is False          # process_sample never triggered

    final = samples[-1]
    state = close_at_session_end(pos, state, final, reason="eod")
    assert state.triggered is True
    assert state.exit_reason == "eod"
    assert state.exit_sample is final

    info = build_exit_info(pos, state)
    # winning spread closes cheap: spread_close = 1.5 - 0.3 = 1.2; pnl = (4.05-1.2)*100 = 285
    assert info["exit_reason"] == "eod"
    assert info["pnl"] == pytest.approx(285.0)
    assert info["pnl"] > 0
    assert info["duration_secs"] == pytest.approx(final.ts - ENTRY_TS)


def test_close_at_session_end_expiry_reason():
    pos = make_position()
    final = Sample(ts=ENTRY_TS + 3600, short_ask=1.0, long_bid=0.2, spot=7400.0)
    state = MonitorState()
    process_sample(pos, state, final)
    state = close_at_session_end(pos, state, final, reason="expiry")
    assert state.exit_reason == "expiry"
    assert build_exit_info(pos, state)["exit_reason"] == "expiry"


# --------------------------------------------------------------------------- #
# MAE: worst mid-life pnl captured even when exit pnl is better
# --------------------------------------------------------------------------- #

def test_mae_captures_worst_dip_then_recovers():
    pos = make_position()
    # pnl path: +155, -105 (worst), +235.  None cross the 6.0 stop.
    specs = [
        (3.0, 0.5),   # close 2.5 -> pnl (4.05-2.5)*100 =  155
        (5.5, 0.4),   # close 5.1 -> pnl (4.05-5.1)*100 = -105  (worst)
        (2.0, 0.3),   # close 1.7 -> pnl (4.05-1.7)*100 =  235
    ]
    samples = [
        Sample(ts=ENTRY_TS + 60 * (i + 1), short_ask=a, long_bid=lb, spot=7400.0)
        for i, (a, lb) in enumerate(specs)
    ]
    state = run_stream(pos, samples)
    assert state.triggered is False
    assert state.last_pnl == pytest.approx(235.0)
    assert state.mae == pytest.approx(-105.0)   # worst running pnl, not the final

    # eod at the recovered (positive) mark; MAE still reflects the dip.
    state = close_at_session_end(pos, state, samples[-1], reason="eod")
    info = build_exit_info(pos, state)
    assert info["pnl"] == pytest.approx(235.0)
    assert info["max_adverse_excursion"] == pytest.approx(-105.0)
    assert info["max_adverse_excursion"] < info["pnl"]


def test_mae_stays_zero_when_never_underwater():
    pos = make_position()
    # every sample is a winner -> running pnl never negative -> MAE stays 0.0
    samples = [
        Sample(ts=ENTRY_TS + 60 * (i + 1), short_ask=1.5, long_bid=0.3)
        for i in range(3)
    ]
    state = run_stream(pos, samples)
    assert state.mae == pytest.approx(0.0)
    assert state.last_pnl > 0


# --------------------------------------------------------------------------- #
# idempotency
# --------------------------------------------------------------------------- #

def test_idempotent_after_trigger_more_extreme_samples_do_not_move_exit():
    pos = make_position()
    cross = Sample(ts=ENTRY_TS + 60, short_ask=6.0, short_bid=5.8, long_bid=0.5,
                   long_ask=0.7, spot=7400.0)
    worse = Sample(ts=ENTRY_TS + 120, short_ask=9.0, short_bid=8.8, long_bid=0.9,
                   long_ask=1.1, spot=7390.0)
    even_worse = Sample(ts=ENTRY_TS + 180, short_ask=12.0, short_bid=11.5,
                        long_bid=1.2, long_ask=1.4, spot=7380.0)

    state = MonitorState()
    process_sample(pos, state, cross)
    assert state.exit_sample is cross
    assert state.exit_reason == "stop_hit"

    process_sample(pos, state, worse)
    process_sample(pos, state, even_worse)

    # exit is still pinned to the FIRST crossing; reason unchanged.
    assert state.exit_sample is cross
    assert state.exit_reason == "stop_hit"
    assert state.n_samples == 3

    # but life-of-position stats still advanced with the later (worse) samples:
    # worst pnl at short_ask=12.0, long_bid=1.2 -> close 10.8 -> (4.05-10.8)*100 = -675
    assert state.mae == pytest.approx(-675.0)

    # a subsequent session-end close is a NO-OP (stop exit preserved).
    state2 = close_at_session_end(pos, state, even_worse, reason="eod")
    assert state2.exit_reason == "stop_hit"
    assert state2.exit_sample is cross

    info = build_exit_info(pos, state)
    assert info["exit_reason"] == "stop_hit"
    assert info["exit_ts"] == cross.ts
    # pnl reported at the crossing sample, not the later worse one.
    # close = 6.0 - 0.5 = 5.5 -> pnl (4.05-5.5)*100 = -145
    assert info["pnl"] == pytest.approx(-145.0)


def test_close_at_session_end_is_noop_when_already_stopped():
    pos = make_position()
    cross = Sample(ts=ENTRY_TS + 60, short_ask=6.5, long_bid=0.5, spot=7400.0)
    state = MonitorState()
    process_sample(pos, state, cross)
    assert state.exit_reason == "stop_hit"
    final = Sample(ts=ENTRY_TS + 9999, short_ask=1.0, long_bid=0.1, spot=7500.0)
    close_at_session_end(pos, state, final, reason="eod")
    assert state.exit_reason == "stop_hit"
    assert state.exit_sample is cross


# --------------------------------------------------------------------------- #
# missing-price robustness
# --------------------------------------------------------------------------- #

def test_missing_short_ask_does_not_crash_or_false_trigger():
    pos = make_position()
    samples = [
        Sample(ts=ENTRY_TS + 60, short_ask=None, long_bid=0.5),   # no ask -> skip trigger
        Sample(ts=ENTRY_TS + 120, short_ask=None, long_bid=None),  # nothing priceable
        Sample(ts=ENTRY_TS + 180, short_ask=3.0, long_bid=0.5),    # priceable, below stop
    ]
    state = run_stream(pos, samples)
    assert state.triggered is False
    assert state.n_samples == 3
    # only the last sample was priceable -> last_pnl reflects it; earlier Nones ignored.
    assert state.last_pnl == pytest.approx((4.05 - (3.0 - 0.5)) * 100)


def test_missing_long_bid_still_allows_stop_trigger():
    pos = make_position()
    # short_ask alone determines the stop; long_bid missing must not block the trigger,
    # even though pnl at that sample is unknown (None).
    s = Sample(ts=ENTRY_TS + 60, short_ask=6.0, long_bid=None, spot=7400.0)
    state = MonitorState()
    process_sample(pos, state, s)
    assert state.triggered is True
    assert state.exit_reason == "stop_hit"
    assert pnl_at(pos, s) is None
    info = build_exit_info(pos, state)
    assert info["pnl"] is None
    assert info["spread_value_at_exit"] is None


def test_none_short_ask_never_false_triggers_even_if_huge_other_prices():
    pos = make_position()
    # a garbage sample with everything None on the short ask must not fire.
    s = Sample(ts=ENTRY_TS + 60, short_ask=None, short_bid=99.0, long_bid=99.0)
    state = MonitorState()
    process_sample(pos, state, s)
    assert state.triggered is False


# --------------------------------------------------------------------------- #
# build_exit_info shape / greeks-are-None contract
# --------------------------------------------------------------------------- #

def test_build_exit_info_legs_are_price_only_no_greeks():
    pos = make_position()
    s = Sample(ts=ENTRY_TS + 60, short_bid=5.8, short_ask=6.0, short_last=5.9,
               long_bid=0.5, long_ask=0.7, long_last=0.6, spot=7400.0)
    state = MonitorState()
    process_sample(pos, state, s)
    info = build_exit_info(pos, state)

    short_leg = info["short_leg_exit"]
    long_leg = info["long_leg_exit"]
    # prices carried through
    assert short_leg["ask"] == 6.0 and short_leg["bid"] == 5.8 and short_leg["last"] == 5.9
    assert short_leg["strike"] == pos.short_strike
    assert long_leg["bid"] == 0.5 and long_leg["strike"] == pos.long_strike
    assert short_leg["underlying_spot"] == 7400.0
    # greeks explicitly NOT populated by this core (Phase-2b attaches them)
    for g in ("delta", "gamma", "vega", "theta", "iv"):
        assert short_leg[g] is None
        assert long_leg[g] is None
    assert short_leg["complete"] is False


def test_build_exit_info_no_exit_sample_returns_nones():
    pos = make_position()
    state = MonitorState()   # never triggered, no exit sample
    info = build_exit_info(pos, state)
    assert info["exit_ts"] is None
    assert info["pnl"] is None
    assert info["duration_secs"] is None
    assert info["short_leg_exit"] is None
    assert info["long_leg_exit"] is None
    assert info["exit_reason"] is None
    assert info["max_adverse_excursion"] == 0.0   # fresh-state MAE default


def test_exit_info_keys_match_schema_exitinfo_fields():
    # The dict must be droppable into s8_schema.ExitInfo.from_dict without surprises.
    from s8_schema import ExitInfo, LegGrab
    pos = make_position()
    s = Sample(ts=ENTRY_TS + 60, short_bid=5.8, short_ask=6.0, long_bid=0.5,
               long_ask=0.7, spot=7400.0)
    state = MonitorState()
    process_sample(pos, state, s)
    info = build_exit_info(pos, state)

    ei = ExitInfo.from_dict(info)
    assert ei.exit_reason == "stop_hit"
    assert ei.pnl == pytest.approx(-145.0)
    assert isinstance(ei.short_leg_exit, LegGrab)
    assert ei.short_leg_exit.ask == 6.0
    assert ei.short_leg_exit.delta is None
    assert ei.duration_secs == pytest.approx(60.0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
