"""
test_s0_live_exec.py — offline unit tests for the S0 tiny-test REAL-transmission executor.

ZERO real transmit. NO broker, NO gateway, NO network. The fake IB raises AssertionError
if any order-placing method is touched, and order_router.place / already_present are mocked,
so nothing can ever reach the wire. These tests pin the non-bypassable safety envelope:
  1. default (not armed) -> preview only; order_router.place / ib.placeOrder NEVER called.
  2. --arm-i-understand sets armed True (and nothing weaker does).
  3. caps: >1 share clamps to 1; a non-USFR symbol is never selected; >$150 notional refuses.
  4. gate: gateway still read-only -> refuse even when armed.
  5. gate: KILL_SWITCH present -> refuse.
  6. gate: target account != U5721712 -> refuse.
  7. dedup: already_present != FRESH -> skip, no transmit.
  8. armed + all gates pass -> order_router.place called EXACTLY once with a single BUY LIMIT
     USFR order, qty 1, account U5721712.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s0_live_exec.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import config
import order_router
import s0_live_exec as ex

ACCT = ex.EXEC_ACCOUNT                      # "U5721712"
TRUST = ex.FORBIDDEN_TRUST_ACCOUNT          # "U14438624"


# --- leak guard ---------------------------------------------------------------------
# The armed transmit path flips config.DRY_RUN/READONLY to False IN-PROCESS (restoring
# them in a finally). This autouse fixture is a belt-and-suspenders backstop: it snapshots
# both flags before every test and restores them after, so no test can ever leak
# DRY_RUN/READONLY=False into another test even if an assertion fails mid-flip.
@pytest.fixture(autouse=True)
def _no_config_flag_leak():
    prev_dry_run, prev_readonly = config.DRY_RUN, config.READONLY
    # The committed desk-wide defaults must be the starting point for every test.
    assert prev_dry_run is True and prev_readonly is True
    try:
        yield
    finally:
        config.DRY_RUN, config.READONLY = prev_dry_run, prev_readonly


# --- fixtures -----------------------------------------------------------------------
def _fake_target():
    return ex.strategy_target.Target(
        weights=pd.Series({"USFR": 0.5, "SPY": 0.5}),
        prices=pd.Series({"USFR": 50.0, "SPY": 500.0}),
        as_of=pd.Timestamp("2026-07-27"),
        price_date=pd.Timestamp("2026-07-25"),
        version="Balanced",
    )


def _summary_row(account, tag, value):
    return SimpleNamespace(account=account, tag=tag, value=value)


def _pos_row(account, symbol, position):
    return SimpleNamespace(account=account, position=position,
                           contract=SimpleNamespace(symbol=symbol))


class _FakeIB:
    """A fake IB that serves a filtered summary/positions and BLOWS UP on any transmit."""
    def __init__(self, summary_rows, position_rows):
        self._summary = summary_rows
        self._positions = position_rows
        self.disconnected = False

    def accountSummary(self):
        return self._summary

    def positions(self):
        return self._positions

    def qualifyContracts(self, *a, **k):
        return list(a)

    def disconnect(self):
        self.disconnected = True

    def placeOrder(self, *a, **k):
        raise AssertionError("ib.placeOrder must never be called by the tiny-test executor")


def _fake_plan(*, breached=True, orders=None):
    return SimpleNamespace(
        account=ACCT, version="Balanced", net_liq=100_000.0, reserve=0.0,
        investable=98_500.0, lines=[], needs_rebalance=breached,
        orders=orders if orders is not None else {"USFR": 1}, alien_lines=[])


def _patch_common(monkeypatch, *, plan_orders=None, target=None):
    """Neutralize every I/O boundary; leave the gate logic under test intact."""
    monkeypatch.setattr(ex.strategy_target, "current_target",
                        lambda *a, **k: (target or _fake_target()))
    monkeypatch.setattr(ex.sp, "_strategy_universe", lambda: {"USFR", "SPY"})
    monkeypatch.setattr(ex.live_quotes, "fetch", lambda ib, universe: {})
    monkeypatch.setattr(ex.rebalance_engine, "plan_account",
                        lambda *a, **k: _fake_plan(orders=plan_orders))
    # Default: no kill switch, gateway armed (not read-only), dedup FRESH.
    monkeypatch.setattr(ex, "_kill_switch_present", lambda: False)
    monkeypatch.setattr(ex, "_probe_gateway_readonly", lambda ib, **k: False)
    monkeypatch.setattr(order_router, "already_present",
                        lambda ib, ref, qty, **k: order_router.LegState.FRESH)


def _wire_connections(monkeypatch, fake):
    monkeypatch.setattr(ex.s0_live, "connect_s0_live", lambda *a, **k: fake)
    monkeypatch.setattr(ex.s0_live, "connect_s0_live_armed", lambda *a, **k: fake)


def _armed_summary():
    # Includes plenty of buying power + the trust/aggregate rows that MUST be ignored.
    return [_summary_row(ACCT, "NetLiquidation", "100000"),
            _summary_row(ACCT, "BuyingPower", "100000"),
            _summary_row(TRUST, "NetLiquidation", "999999"),
            _summary_row("All", "NetLiquidation", "888")]


# --- 1. default (not armed) -> preview only, never transmits -------------------------
def test_default_is_preview_never_transmits(monkeypatch):
    _patch_common(monkeypatch, plan_orders={"USFR": 1})
    fake = _FakeIB(_armed_summary(), [])
    _wire_connections(monkeypatch, fake)

    def _place_boom(*a, **k):
        raise AssertionError("order_router.place must not be called in preview mode")

    monkeypatch.setattr(order_router, "place", _place_boom)

    rc = ex.main(armed=False)

    assert rc == 0
    assert fake.disconnected is True
    # Preview path must NEVER flip the in-process safety flags.
    assert config.DRY_RUN is True and config.READONLY is True


# --- 2. arm token parsing -----------------------------------------------------------
def test_arm_token_sets_armed():
    assert ex.arm_requested(["--arm-i-understand"]) is True
    assert ex.arm_requested([]) is False
    assert ex.arm_requested(["--armed"]) is False          # a typo is NOT the token
    assert ex.arm_requested(["--arm"]) is False


# --- 3a. caps: a plan wanting >1 share is clamped DOWN to 1 --------------------------
def test_qty_clamped_down_to_one():
    plan = _fake_plan(orders={"USFR": 50})
    pick = ex._pick_test_order(plan, quotes={}, prices={"USFR": 50.0})
    assert pick is not None
    assert pick.qty == ex.MAX_TEST_SHARES == 1
    assert pick.raw_qty == 50                               # remembers what the plan wanted


# --- 3b. caps: a non-USFR symbol is never selected ----------------------------------
def test_non_whitelisted_symbol_not_selected():
    plan = _fake_plan(orders={"SPY": 10})                   # SPY buy, not whitelisted
    pick = ex._pick_test_order(plan, quotes={}, prices={"SPY": 500.0})
    assert pick is None


# --- 3c. caps: notional > $150 refuses (no transmit) --------------------------------
def test_notional_over_cap_refuses(monkeypatch):
    # USFR priced at 200 -> 1 share = $200 notional > $150 cap.
    _patch_common(monkeypatch, plan_orders={"USFR": 1})
    monkeypatch.setattr(ex.rebalance_engine, "plan_account",
                        lambda *a, **k: _fake_plan(orders={"USFR": 1}))

    tgt = ex.strategy_target.Target(
        weights=pd.Series({"USFR": 1.0}), prices=pd.Series({"USFR": 200.0}),
        as_of=pd.Timestamp("2026-07-27"), price_date=pd.Timestamp("2026-07-25"),
        version="Balanced")
    monkeypatch.setattr(ex.strategy_target, "current_target", lambda *a, **k: tgt)

    fake = _FakeIB([_summary_row(ACCT, "NetLiquidation", "100000")], [])
    _wire_connections(monkeypatch, fake)
    monkeypatch.setattr(order_router, "place",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not transmit over the notional cap")))

    rc = ex.main(armed=True)

    assert rc == 0                                          # clean preview, no transmit
    # A blocked (over-cap) run must NOT flip the flags.
    assert config.DRY_RUN is True and config.READONLY is True


# --- 4. gate: gateway still read-only -> refuse even when armed ----------------------
def test_readonly_gateway_refuses_even_when_armed(monkeypatch):
    _patch_common(monkeypatch, plan_orders={"USFR": 1})
    monkeypatch.setattr(ex, "_probe_gateway_readonly", lambda ib, **k: True)  # still locked
    fake = _FakeIB(_armed_summary(), [])
    _wire_connections(monkeypatch, fake)
    monkeypatch.setattr(order_router, "place",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not transmit while gateway is read-only")))

    rc = ex.main(armed=True)

    assert rc == 0
    assert config.DRY_RUN is True and config.READONLY is True


# --- 5. gate: KILL_SWITCH present -> refuse ------------------------------------------
def test_kill_switch_refuses(monkeypatch):
    _patch_common(monkeypatch, plan_orders={"USFR": 1})
    monkeypatch.setattr(ex, "_kill_switch_present", lambda: True)              # sentinel on
    fake = _FakeIB(_armed_summary(), [])
    _wire_connections(monkeypatch, fake)
    monkeypatch.setattr(order_router, "place",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not transmit with the kill switch on")))

    rc = ex.main(armed=True)

    assert rc == 0
    assert config.DRY_RUN is True and config.READONLY is True


# --- 6. gate: target account != U5721712 -> refuse ----------------------------------
def test_wrong_account_refuses(monkeypatch):
    _patch_common(monkeypatch, plan_orders={"USFR": 1})
    # Force the exec account to the FORBIDDEN trust account.
    monkeypatch.setattr(ex, "EXEC_ACCOUNT", TRUST)
    fake = _FakeIB([_summary_row(TRUST, "NetLiquidation", "100000"),
                    _summary_row(TRUST, "BuyingPower", "100000")], [])
    _wire_connections(monkeypatch, fake)
    monkeypatch.setattr(order_router, "place",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must NEVER transmit on the trust account")))

    rc = ex.main(armed=True)

    assert rc == 0
    assert config.DRY_RUN is True and config.READONLY is True
    ok, reason = ex._account_safety_ok()
    assert ok is False


# --- 7. dedup: already_present != FRESH -> skip, no transmit -------------------------
def test_dedup_not_fresh_skips(monkeypatch):
    _patch_common(monkeypatch, plan_orders={"USFR": 1})
    monkeypatch.setattr(order_router, "already_present",
                        lambda ib, ref, qty, **k: order_router.LegState.WORKING)  # already live
    fake = _FakeIB(_armed_summary(), [])
    _wire_connections(monkeypatch, fake)
    monkeypatch.setattr(order_router, "place",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not transmit when a leg is already WORKING")))

    rc = ex.main(armed=True)

    assert rc == 0
    assert config.DRY_RUN is True and config.READONLY is True


# --- 8. armed + all gates pass -> place called EXACTLY once with one BUY LIMIT USFR --
def test_armed_all_gates_pass_transmits_one_order(monkeypatch):
    _patch_common(monkeypatch, plan_orders={"USFR": 1})
    # Give USFR a real quote so a marketable cap is computed near the ask.
    monkeypatch.setattr(ex.live_quotes, "fetch",
                        lambda ib, universe: {"USFR": ex.live_quotes.Quote(
                            symbol="USFR", bid=49.99, ask=50.01, last=50.0, close=50.0,
                            md_type=1)})
    fake = _FakeIB(_armed_summary(), [])
    _wire_connections(monkeypatch, fake)

    captured = {}

    def _capture_place(ib, built, armed=False, **k):
        captured["armed"] = armed
        captured["built"] = built
        captured["account_kw"] = k.get("account")
        # Capture the in-process flags AT THE MOMENT place() is invoked: the transmit_guard
        # only permits when BOTH are False, so the flip must be live here.
        captured["dry_run_at_place"] = config.DRY_RUN
        captured["readonly_at_place"] = config.READONLY
        return {"transmitted": 1, "logged": 1, "fills": [
            {"symbol": "USFR", "status": "Filled", "filled": 1.0, "remaining": 0.0,
             "avgFillPrice": 50.0}]}

    monkeypatch.setattr(order_router, "place", _capture_place)

    rc = ex.main(armed=True)

    assert rc == 0
    assert captured["armed"] is True
    # The flip is live exactly when place() runs...
    assert captured["dry_run_at_place"] is False
    assert captured["readonly_at_place"] is False
    # ...and is RESTORED to the committed defaults once main() returns (no leakage).
    assert config.DRY_RUN is True and config.READONLY is True
    built = captured["built"]
    assert len(built) == 1                                  # EXACTLY one order
    b = built[0]
    assert b.symbol == "USFR"
    assert b.order.action == "BUY"                          # a BUY, never a SELL
    assert b.order.orderType == "LMT"                       # a LIMIT, never a market order
    assert float(b.order.totalQuantity) == 1.0             # qty 1
    assert b.order.account == ACCT == "U5721712"           # the individual account only
    assert captured["account_kw"] == ACCT
    assert b.order.lmtPrice <= ex.MAX_TEST_NOTIONAL         # within the notional cap for 1 sh
