"""
test_rebalance_execute.py — offline unit tests for the transmit-capable executor's HARD
SAFETY surface. NO broker, NO gateway, NO orders. Proves the three guarantees the Monday
path rests on:

  (a) DEFAULT NEVER TRANSMITS — with no arm token the guard blocks transmission and
      place() sends nothing; config defaults (READONLY/DRY_RUN True) are untouched.
  (b) THE PRICE GUARD rejects NaN / None / <= 0 limits BEFORE any order object is built,
      in BOTH order_router.build (direct) and build_fa_block (group).
  (c) THE ARMED GATE requires ALL THREE conditions together (READONLY=False AND
      DRY_RUN=False AND armed=True); any one missing -> blocked, fails closed.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_rebalance_execute.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import config
import order_router
import rebalance_execute as rx


def _intent(symbol, side, qty, limit):
    return SimpleNamespace(symbol=symbol, side=side, quantity=qty, limit_price=limit)


# --- (a) default never transmits -----------------------------------------------
def test_committed_config_defaults_are_safe():
    # The on-disk safety posture must remain locked. The executor flips these only in
    # memory and only with the arm token — never on disk.
    assert config.READONLY is True
    assert config.DRY_RUN is True


def test_no_token_means_not_armed():
    # No arm token on the command line -> arm_requested False (condition 4 absent).
    assert rx.arm_requested([]) is False
    assert rx.arm_requested(["--armed"]) is False           # near-miss does NOT arm
    assert rx.arm_requested(["--arm"]) is False
    assert rx.arm_requested([rx.ARM_TOKEN]) is True          # only the exact token


def test_default_gate_blocks_transmission():
    # With the committed defaults (READONLY/DRY_RUN True) and armed=False, the gate the
    # executor delegates to is BLOCKED.
    permit, why = rx.gate_state(armed=False)
    assert permit is False
    assert why == "DRY_RUN=True"        # fails closed on the first reason


def test_place_transmits_nothing_when_not_permitted():
    # place() with armed=False must transmit 0 even with real built orders. ib is never
    # touched because the guard blocks before any placeOrder call.
    built = order_router.build([_intent("SPY", "BUY", 10, 100.0)], "DU0001", "t", ib=None)
    sentinel = object()   # a broker that would explode if place() tried to use it
    result = order_router.place(sentinel, built, armed=False)
    assert result["transmitted"] == 0
    assert result["logged"] == 1


# --- (b) price guard rejects NaN / None / <= 0 BEFORE building ------------------
@pytest.mark.parametrize("bad", [float("nan"), None, 0.0, -1.0, -0.01])
def test_price_guard_blocks_direct_build(bad):
    # order_router.build must REFUSE to build a direct order with a bad limit, raising
    # before any BuiltOrder is produced.
    with pytest.raises(ValueError):
        order_router.build([_intent("SPY", "BUY", 10, bad)], "DU0001", "t", ib=None)


@pytest.mark.parametrize("bad", [float("nan"), None, 0.0, -5.0])
def test_price_guard_blocks_fa_block_build(bad):
    # build_fa_block must REFUSE a bad limit too (a $0/NaN block would split across a tier).
    with pytest.raises(ValueError):
        order_router.build_fa_block("SPY", "BUY", 30, bad, "tier_growth", "", "t", ib=None)


def test_price_guard_passes_valid_price():
    # A clean positive price builds normally (sanity: the guard isn't over-broad).
    built = order_router.build([_intent("SPY", "BUY", 10, 123.45)], "DU0001", "t", ib=None)
    assert len(built) == 1
    assert built[0].order.lmtPrice == 123.45
    bo = order_router.build_fa_block("SPY", "BUY", 30, 123.45, "tier_growth", "", "t", ib=None)
    assert bo.order.lmtPrice == 123.45


def test_price_guard_no_order_built_on_reject(monkeypatch):
    # Prove NOTHING is constructed when the guard fires: if a limit is NaN, the LimitOrder
    # constructor must never be reached.
    import order_router as orm
    calls = {"n": 0}
    real = orm.LimitOrder

    def spy_limit(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(orm, "LimitOrder", spy_limit)
    with pytest.raises(ValueError):
        orm.build([_intent("SPY", "BUY", 10, float("nan"))], "DU0001", "t", ib=None)
    assert calls["n"] == 0          # the guard short-circuited before any order object


# --- (c) the armed gate requires ALL THREE conditions --------------------------
def test_armed_gate_requires_all_three(monkeypatch):
    # Drive transmit_guard / gate_state across every combination; permitted IFF all three.
    cases = [
        # (readonly, dry_run, armed) -> permitted
        (True,  True,  False, False),   # committed default
        (True,  True,  True,  False),   # armed but still read-only + dry-run
        (False, True,  True,  False),   # read-only cleared, dry-run still on
        (True,  False, True,  False),   # dry-run cleared, read-only still on
        (False, False, False, False),   # both cleared but human did NOT arm
        (False, False, True,  True),    # ALL three -> the only permitted state
    ]
    for readonly, dry_run, armed, expected in cases:
        monkeypatch.setattr(config, "READONLY", readonly)
        monkeypatch.setattr(config, "DRY_RUN", dry_run)
        permit, _ = rx.gate_state(armed)
        assert permit is expected, (readonly, dry_run, armed, permit)


def test_gate_fails_closed_first_reason(monkeypatch):
    # Order of the fail-closed reasons (DRY_RUN checked first, then READONLY, then armed).
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(config, "READONLY", True)
    assert rx.gate_state(True)[1] == "DRY_RUN=True"
    monkeypatch.setattr(config, "DRY_RUN", False)
    assert rx.gate_state(True)[1] == "READONLY=True"
    monkeypatch.setattr(config, "READONLY", False)
    assert rx.gate_state(False)[1] == "session not armed by a human"
    assert rx.gate_state(True) == (True, "ARMED")


def test_armed_place_path_is_unreachable_without_all_three(monkeypatch):
    # Even calling place(armed=True), if READONLY/DRY_RUN are still set the guard blocks:
    # the executor can never transmit unless the in-process flags were flipped by the token.
    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "DRY_RUN", True)
    built = order_router.build([_intent("SPY", "BUY", 10, 100.0)], "DU0001", "t", ib=None)
    sentinel = object()
    result = order_router.place(sentinel, built, armed=True)   # armed, but config locked
    assert result["transmitted"] == 0
