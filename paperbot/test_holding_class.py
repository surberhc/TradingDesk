"""test_holding_class.py — the MANAGED vs HELD-ASIDE classifier and the carve-out.

SYNTHETIC data only (fake symbols, types, prices). No broker, no gateway, no orders.
Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest ^
    "C:\\TradingDesk\\paperbot\\test_holding_class.py" -q

These prove the things the no-trade list must get right:
  * classification is driven by the INSTRUMENT TYPE, never a symbol-string guess
  * an unknown / unrecognised type FAILS CLOSED (held aside + flagged), never traded
  * a bond is valued percent-of-par per 100, never qty*mark
  * the carve-out splits the account so managed + held-aside == NetLiq
  * an unpriceable held-aside holding BLOCKS orders instead of under-carving silently
  * with no sec_types at all, the carve-out is the identity (today's behavior)
"""
from __future__ import annotations

import pytest

import holding_class as hc


# --- 1. classification ---------------------------------------------------------
def test_equity_types_are_managed():
    for t in ("STK", "stk", " Stk ", "ETF", "FUND", "STOCK", "EQUITY", "MF"):
        assert hc.classify(t) == hc.MANAGED
        assert hc.is_held_aside(t) is False
        assert hc.needs_classification(t) is False
        assert hc.reason_for(t) == ""


def test_bond_is_held_aside_on_purpose_not_pending_classification():
    assert hc.classify("BOND") == hc.HELD_ASIDE
    assert hc.is_held_aside("BOND") is True
    # A bond is held aside by DECISION, so it does not need a human to identify it.
    assert hc.needs_classification("BOND") is False
    assert "never traded" in hc.reason_for("BOND")


@pytest.mark.parametrize("bad", [None, "", "   ", "???"])
def test_unknown_type_fails_closed_to_held_aside_and_flags(bad):
    # FAIL CLOSED: an instrument whose type cannot be determined is NEVER assumed tradeable.
    assert hc.classify(bad) == hc.HELD_ASIDE
    assert hc.needs_classification(bad) is True
    assert hc.reason_for(bad)          # a human-readable reason is always produced


@pytest.mark.parametrize("t", ["OPT", "FUT", "WAR", "CFD", "CASH", "CMDTY"])
def test_recognised_but_unmanaged_types_also_fail_closed(t):
    # A real instrument type we have not been taught to manage is held aside AND flagged —
    # the desk never trades something on the strength of it merely looking like a type.
    assert hc.classify(t) == hc.HELD_ASIDE
    assert hc.needs_classification(t) is True
    assert t in hc.reason_for(t)


def test_classification_ignores_the_symbol_entirely():
    # The classifier takes NO symbol: a CUSIP-looking string typed STK is managed, and a
    # plain ticker typed BOND is held aside. Only the instrument type decides.
    assert hc.classify("STK") == hc.MANAGED
    assert hc.classify("BOND") == hc.HELD_ASIDE


def test_new_held_aside_type_needs_only_a_table_entry(monkeypatch):
    """The class is extensible by design: teaching the desk a second held-aside instrument
    type is ONE dict entry — no other logic changes."""
    monkeypatch.setitem(hc.HELD_ASIDE_TYPES, "WAR", "legacy warrant — held aside")
    assert hc.classify("WAR") == hc.HELD_ASIDE
    assert hc.needs_classification("WAR") is False       # now a deliberate policy member
    assert hc.reason_for("WAR") == "legacy warrant — held aside"


# --- 2. valuation --------------------------------------------------------------
def test_bond_valued_percent_of_par_per_100():
    # Live IBKR shape: 10,000 face @ 100.14628819 per-100 => 10,014.63 (NOT 1,001,462.88).
    assert hc.price_multiplier("BOND") == 0.01
    assert hc.position_value(10000, 100.14628819, "BOND") == pytest.approx(10014.628819)


def test_equity_valued_qty_times_price():
    assert hc.price_multiplier("STK") == 1.0
    assert hc.position_value(10, 500.0, "STK") == pytest.approx(5000.0)


def test_reported_market_value_is_the_fallback_when_the_mark_is_missing():
    assert hc.position_value(10000, 0.0, "BOND", reported_value=10014.63) == pytest.approx(10014.63)
    assert hc.position_value(10000, float("nan"), "BOND", reported_value=9999.0) == pytest.approx(9999.0)
    # ...but a real positive mark always wins (precedence unchanged from the prior code).
    assert hc.position_value(10000, 100.0, "BOND", reported_value=1.0) == pytest.approx(10000.0)


def test_unvaluable_position_returns_none_not_zero():
    # A silent zero would UNDER-carve the account and over-invest the managed sleeve.
    assert hc.position_value(10000, 0.0, "BOND", reported_value=None) is None
    assert hc.position_value(10000, None, "BOND", reported_value=0.0) is None


# --- 3. the carve-out ----------------------------------------------------------
def test_no_sec_types_is_the_identity_carve_out():
    """The behavior-preserving default: with no classification data NOTHING is held aside,
    positions pass through untouched, and the managed sleeve is the whole account."""
    positions = {"SPY": 10, "BIL": 5}
    c = hc.carve_out(100_000.0, positions)
    assert c.managed_positions == positions
    assert c.managed_net_liq == 100_000.0
    assert c.held_aside_value == 0.0
    assert c.held_aside == []
    assert c.blocked_reasons == []


def test_carve_out_splits_managed_and_held_aside():
    positions = {"SPY": 100, "797843BE8 4.6 08/01/34": 10000}
    sec_types = {"SPY": "STK", "797843BE8 4.6 08/01/34": "BOND"}
    prices = {"SPY": 500.0, "797843BE8 4.6 08/01/34": 100.14628819}
    c = hc.carve_out(100_000.0, positions, sec_types=sec_types, prices=prices)

    # The bond is GONE from what the engine will reconcile...
    assert c.managed_positions == {"SPY": 100}
    # ...and PRESENT, priced and named, in the reporting block.
    assert len(c.held_aside) == 1
    h = c.held_aside[0]
    assert h.symbol == "797843BE8 4.6 08/01/34"
    assert h.sec_type == "BOND"
    assert h.quantity == 10000
    assert h.market_value == pytest.approx(10014.628819)
    assert h.priced is True
    assert h.needs_classification is False
    # total == managed + held aside, both priced. The account never loses money to rounding
    # into a category nobody prints.
    assert c.held_aside_value == pytest.approx(10014.628819)
    assert c.managed_net_liq == pytest.approx(100_000.0 - 10014.628819)
    assert c.managed_net_liq + c.held_aside_value == pytest.approx(c.net_liq)
    assert c.blocked_reasons == []


def test_symbol_absent_from_sec_types_is_held_aside_and_flagged():
    # Supplying sec_types means it must cover every holding; a gap is UNKNOWN, not managed.
    c = hc.carve_out(50_000.0, {"SPY": 10, "MYSTERY": 4},
                     sec_types={"SPY": "STK"}, values={"MYSTERY": 1_000.0})
    assert c.managed_positions == {"SPY": 10}
    assert [h.symbol for h in c.held_aside] == ["MYSTERY"]
    assert c.held_aside[0].sec_type == hc.UNKNOWN
    assert c.held_aside[0].needs_classification is True
    assert [h.symbol for h in c.unclassified] == ["MYSTERY"]
    # It is priced from the supplied value, so the sleeve is still sizeable.
    assert c.managed_net_liq == pytest.approx(49_000.0)
    assert c.blocked_reasons == []


def test_unpriceable_held_aside_blocks_instead_of_under_carving():
    c = hc.carve_out(100_000.0, {"SPY": 10, "BONDX": 10000},
                     sec_types={"SPY": "STK", "BONDX": "BOND"})   # no price, no value
    assert [h.symbol for h in c.unpriced] == ["BONDX"]
    assert c.held_aside[0].market_value is None
    assert len(c.blocked_reasons) == 1
    assert "could not be priced" in c.blocked_reasons[0]
    assert "BONDX" in c.blocked_reasons[0]


def test_held_aside_value_above_netliq_blocks():
    c = hc.carve_out(5_000.0, {"BONDX": 10000}, sec_types={"BONDX": "BOND"},
                     values={"BONDX": 10_014.63})
    assert c.managed_net_liq == 0.0            # clamped, never negative
    assert any("exceeds the account NetLiq" in r for r in c.blocked_reasons)


def test_zero_quantity_held_aside_is_dropped_managed_zero_is_kept():
    c = hc.carve_out(1_000.0, {"SPY": 0, "BONDX": 0},
                     sec_types={"SPY": "STK", "BONDX": "BOND"})
    assert c.managed_positions == {"SPY": 0}   # engine input unchanged for managed lines
    assert c.held_aside == []                  # nothing held -> nothing to price or report
    assert c.blocked_reasons == []


def test_held_aside_records_serialize_for_display():
    c = hc.carve_out(100_000.0, {"B": 10000}, sec_types={"B": "BOND"},
                     values={"B": 10_014.63})
    d = c.held_aside[0].as_dict()
    assert set(d) == {"symbol", "sec_type", "quantity", "price", "market_value",
                      "reason", "needs_classification"}
    assert d["market_value"] == pytest.approx(10_014.63)
