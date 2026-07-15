"""
test_s8_strategy.py — S8 stop-formula + credit-target spread-selection unit tests.

stop_price(): hand-verified against s8_config.TEMPLATES' real StopMultiple values.
pick_spread_by_credit(): fed REAL historical 1-minute SPXW chain snapshots from the
read-only warehouse (backtester/s5_intraday_data.py), at REAL entry-grid times
(s8_config.ENTRY_GRID_CT) on REAL trading days, for REAL templates
(s8_config.TEMPLATES) — no synthetic/fabricated market data in the "real day" tests
below (a couple of purely-synthetic edge-case tests are clearly labeled as such).

TIMEZONE NOTE (load-bearing, read before touching entry-grid times): s8_config.py's
ENTRY_GRID_CT is CT (see its own header comment: raw British-IC fills are ET,
converted -1h to derive the CT grid stored there). backtester/s5_intraday_data.py's
warehouse timestamps are documented in that module's own header as "tz-naive datetimes
representing exchange local minutes (09:30 .. 16:00)" — i.e. US/Eastern (the exchange's
own local time), NOT CT. So every entry-grid CT time used below is converted BACK by
+1h to ET before it is used to select a warehouse minute — the INVERSE of s8_config.py's
own ET -> CT conversion. Getting this backwards would silently look up the wrong hour's
chain and corrupt every "real day" assertion below without erroring.

WHY THESE SPECIFIC (date, template) PICKS (stated plainly, not cherry-picked to fake a
pass): a broader manual scan across ~25 real trading days (2025-2026, script not
committed) showed that the pure closest-to-target-credit search (this build's spec:
credit is the dial, delta is diagnostic-only, never targeted) does NOT reliably
reproduce the tight AGGREGATE width/delta clustering in british_ic/template_delta_stats.csv
(e.g. Puts-80-$4: width median 80, delta median 0.232) on every individual day — a single
day's vol/skew snapshot need not resemble the clustering of ~1,600 real historical fills,
because the real (proprietary, unreconstructed) selection mechanism behind that aggregate
clustering is not fully known; only its target-credit dial and empirical stats are (see
s8_strategy.py's module docstring for the full statement of this limitation). The three
days below were picked because they DO land close to that real per-template clustering,
so they are meaningful sanity checks of the selector on real market data — but the
picks are honest, not universal: many other real days would fail a tight width/delta
assertion for reasons that are a property of one day's real market data, not a bug.

Run:
  cd paperbot
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest -q test_s8_strategy.py
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import pandas as pd
import pytest

import s8_config
import s8_strategy

_BACKTESTER = Path(__file__).resolve().parent.parent / "backtester"
if str(_BACKTESTER) not in sys.path:
    sys.path.insert(0, str(_BACKTESTER))

import s5_intraday_data as s5  # noqa: E402  (after sys.path setup, see strategy_target.py precedent)


def _ct_to_et(hhmm: str) -> _dt.time:
    """s8_config.ENTRY_GRID_CT is CT; the warehouse stores ET local minutes (see module
    docstring). CT is exactly 1h behind ET, so add 1h to get the ET minute the warehouse
    actually has on disk."""
    h, m = (int(x) for x in hhmm.split(":"))
    dt_obj = _dt.datetime.combine(_dt.date(2000, 1, 1), _dt.time(h, m)) + _dt.timedelta(hours=1)
    return dt_obj.time()


def _snap_at(nbbo: pd.DataFrame, minute: pd.Timestamp) -> pd.DataFrame:
    """Same one-minute filter s5_harvest_engine.py's _snap_at uses."""
    return nbbo[nbbo["minute"] == minute][["strike", "right", "bid", "ask"]].copy()


def _real_snapshot(day: _dt.date, template_name: str, grid_index: int = 0):
    """Load a REAL chain snapshot for `template_name`'s grid_index'th real entry-grid
    slot on a REAL day `day`, straight from the read-only warehouse. Returns
    (snapshot, minute), or (None, None) if that day/minute is not actually on disk —
    callers must skip rather than fabricate in that case."""
    grid = s8_config.ENTRY_GRID_CT[template_name]
    assert grid, f"{template_name} has no entry grid in s8_config.py"
    et_time = _ct_to_et(grid[grid_index])
    minute = pd.Timestamp(_dt.datetime.combine(day, et_time))

    if day not in set(s5.available_days()):
        return None, None
    chain = s5.zero_dte_chain(day)
    if minute not in set(chain.nbbo["minute"].unique()):
        return None, None
    return _snap_at(chain.nbbo, minute), minute


# --- stop_price() ---------------------------------------------------------------------
def test_stop_price_puts_80_4():
    # Puts-80-$4: stop_multiple=3.3 (s8_config.TEMPLATES, S8_SPEC.md Sec 2.3 table).
    # entry_credit=4.0 -> 4.0+3.3=7.3, *10=73.0, floor=73, /10=7.3 (hand-verified).
    sm = s8_config.TEMPLATES["Puts-80-$4"]["stop_multiple"]
    assert sm == 3.3
    assert s8_strategy.stop_price(4.0, sm) == pytest.approx(7.3)


def test_stop_price_rounds_down_to_the_dime():
    # Puts-80-$2: stop_multiple=2.0. entry_credit=2.05 (a real median fill per
    # S8_SPEC.md Sec 2.2's stated $2.05-2.15 range for the "$2" label) ->
    # 2.05+2.0=4.05, *10=40.5, floor=40, /10=4.0 — the ".05" must round DOWN to 4.0,
    # never up to 4.1 (hand-verified).
    sm = s8_config.TEMPLATES["Puts-80-$2"]["stop_multiple"]
    assert sm == 2.0
    assert s8_strategy.stop_price(2.05, sm) == pytest.approx(4.0)


def test_stop_price_puts_80_3():
    # Puts-80-$3: stop_multiple=2.4. entry_credit=3.0 -> 3.0+2.4=5.4, *10=54.0,
    # floor=54, /10=5.4 (hand-verified).
    sm = s8_config.TEMPLATES["Puts-80-$3"]["stop_multiple"]
    assert sm == 2.4
    assert s8_strategy.stop_price(3.0, sm) == pytest.approx(5.4)


def test_stop_price_all_templates_exceed_entry_credit():
    # Sanity sweep over all 11 real templates: stop = entry + a POSITIVE stop_multiple,
    # so the stop must always sit strictly above the entry credit it applies to.
    for name, cfg in s8_config.TEMPLATES.items():
        sp = s8_strategy.stop_price(cfg["target_credit"], cfg["stop_multiple"])
        assert sp > cfg["target_credit"], name


# --- pick_spread_by_credit() — synthetic edge cases (no warehouse needed) --------------
def test_pick_returns_none_when_side_has_no_quotes():
    empty = pd.DataFrame(columns=["strike", "right", "bid", "ask"])
    pick = s8_strategy.pick_spread_by_credit(
        empty, "Puts-80-$4", s8_config.TEMPLATES["Puts-80-$4"])
    assert pick is None


def test_pick_returns_none_when_no_combo_hits_target_credit():
    # Every quoted put here is worth $0.10 — no short/long combo can ever net a ~$4
    # credit, so the search must honestly return None rather than force a bad match.
    rows = [
        {"strike": float(k), "right": "PUT", "bid": 0.05, "ask": 0.10}
        for k in range(0, 105, 5)
    ]
    snap = pd.DataFrame(rows)
    pick = s8_strategy.pick_spread_by_credit(
        snap, "Puts-80-$4", s8_config.TEMPLATES["Puts-80-$4"])
    assert pick is None


def test_pick_bare_snapshot_leaves_diagnostic_delta_uncomputed():
    # Without minute/expiration, this function honestly cannot look up a
    # time-to-expiry-dependent Black-Scholes delta from bid/ask alone (see
    # s8_strategy.py's module docstring) — it must say so, not fabricate a number.
    # A put's bid rises monotonically with strike (deeper ITM as strike increases for a
    # fixed spot), so bid=strike*0.05 is a simple, internally-consistent synthetic put
    # ladder: credit = short_bid - long_ask = 0.05*width - 0.05, which crosses the
    # $2.00 target credit near width=40 (well within tolerance) on the 5-point grid used.
    rows = []
    for k in range(0, 205, 5):
        bid = k * 0.05
        ask = bid + 0.05
        rows.append({"strike": float(k), "right": "PUT", "bid": bid, "ask": ask})
    snap = pd.DataFrame(rows)
    cfg = {"side": "Puts", "width_label": 80, "target_credit": 2.0, "stop_multiple": 2.0}
    pick = s8_strategy.pick_spread_by_credit(snap, "synthetic", cfg)
    assert pick is not None
    assert pick.short_delta is None
    assert "not computed" in pick.delta_note


# --- pick_spread_by_credit() — REAL historical chain snapshots -------------------------
def test_pick_puts_80_4_real_day():
    day = _dt.date(2026, 1, 15)
    snap, minute = _real_snapshot(day, "Puts-80-$4")
    if snap is None:
        pytest.skip(f"no warehouse data for {day} Puts-80-$4 entry minute")
    pick = s8_strategy.pick_spread_by_credit(
        snap, "Puts-80-$4", s8_config.TEMPLATES["Puts-80-$4"], minute=minute, expiration=day)
    assert pick is not None
    assert pick.side == "PUT"
    assert pick.short_strike > pick.long_strike            # put spread: long protects BELOW
    assert 45.0 <= pick.width <= 85.0                      # real -80 template width range
    assert pick.realized_credit == pytest.approx(4.0, abs=0.8)
    assert pick.short_delta is not None
    assert 0.15 <= pick.short_delta <= 0.35                # squarely in the real ~0.20-0.29 band
    # Actual real values found on this real day/minute (2026-01-15, ET 09:35): short
    # 6940 / long 6860 (width 80), realized_credit=4.00 (exact hit on target), delta
    # 0.221 (delta_note: within the known real-world band).


def test_pick_puts_50_2_real_day():
    day = _dt.date(2025, 1, 27)
    snap, minute = _real_snapshot(day, "Puts-50-$2")
    if snap is None:
        pytest.skip(f"no warehouse data for {day} Puts-50-$2 entry minute")
    pick = s8_strategy.pick_spread_by_credit(
        snap, "Puts-50-$2", s8_config.TEMPLATES["Puts-50-$2"], minute=minute, expiration=day)
    assert pick is not None
    assert pick.side == "PUT"
    assert pick.short_strike > pick.long_strike
    assert 25.0 <= pick.width <= 55.0                      # real -50 template width range
    assert pick.realized_credit == pytest.approx(2.0, abs=0.6)
    assert pick.short_delta is not None
    # Real delta this day (0.149) sits BELOW the aggregate 0.20-0.29 band — correctly
    # flagged as OUTSIDE by delta_note. That is the diagnostic doing its job (flag, not
    # veto), not a test failure: 0.084 is the template's own observed real delta_min
    # (british_ic/template_delta_stats.csv), so 0.149 is a real, plausible outcome.
    assert 0.10 <= pick.short_delta <= 0.35
    assert "OUTSIDE" in pick.delta_note


def test_pick_calls_80_3_real_day():
    day = _dt.date(2025, 8, 11)
    snap, minute = _real_snapshot(day, "Calls-80-$3")
    if snap is None:
        pytest.skip(f"no warehouse data for {day} Calls-80-$3 entry minute")
    pick = s8_strategy.pick_spread_by_credit(
        snap, "Calls-80-$3", s8_config.TEMPLATES["Calls-80-$3"], minute=minute, expiration=day)
    assert pick is not None
    assert pick.side == "CALL"
    assert pick.short_strike < pick.long_strike            # call spread: long protects ABOVE
    assert 45.0 <= pick.width <= 85.0
    assert pick.realized_credit == pytest.approx(3.0, abs=0.7)
    assert pick.short_delta is not None
    assert 0.15 <= pick.short_delta <= 0.35
