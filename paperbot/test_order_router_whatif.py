"""
test_order_router_whatif.py — offline unit tests for order_router.what_if()'s HARD TIMEOUT.
NO broker, NO gateway, NO real orders. The ib is a mock whose whatIfOrderAsync coroutine we
control. Proves BUG #48 is fixed:

  (1) A what-if request that never resolves (the IBKR error-321 hang: whatIf=True without
      transmit) must NOT wedge the loop over `built` — what_if() returns within the timeout
      with None for that order instead of hanging forever.
  (2) A timeout is recorded as None (NO STATE RETURNED), NEVER as acceptance.
  (3) One hung order does not poison the others — a following, healthy order still validates.
  (4) The existing exception path (a real rejection) still returns None.
  (5) A normal accepted what-if returns its OrderState.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_order_router_whatif.py -q
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from ib_async import LimitOrder, Stock

import order_router as orm


def _built(symbol="TFLO", qty=10, px=50.0):
    contract = Stock(symbol, "SMART", "USD")
    order = LimitOrder("BUY", qty, px)
    order.transmit = False
    return orm.BuiltOrder(symbol, contract, order, f"paperbot:DU1:t:BUY:{symbol}")


class _ScriptedIB:
    """Mock ib whose whatIfOrderAsync behavior is chosen per-symbol by `behavior`:
      "hang"   -> coroutine that sleeps far longer than any test timeout (the error-321 hang),
      "reject" -> coroutine that raises (a genuine rejection — the existing exception path),
      "ok"     -> coroutine that returns a stand-in OrderState.
    Records how many times it was called so we can assert the loop actually advanced."""
    def __init__(self, behavior: dict):
        self.behavior = behavior
        self.calls = 0

    async def whatIfOrderAsync(self, contract, order):
        self.calls += 1
        what = self.behavior[contract.symbol]
        if what == "hang":
            await asyncio.sleep(60)          # never resolves within the test window
            return SimpleNamespace()          # unreachable
        if what == "reject":
            raise ValueError("FA master rejected direct unallocated order")
        return SimpleNamespace(initMarginChange="1234.5", commission=1.0,
                               commissionCurrency="USD")


def test_what_if_hang_times_out_returns_none_not_hang():
    # The core BUG #48 assertion: a never-resolving what-if returns promptly with None,
    # bounded by the timeout — it does NOT hang forever.
    ib = _ScriptedIB({"TFLO": "hang"})
    start = time.monotonic()
    states = orm.what_if(ib, [_built("TFLO")], timeout=0.2)
    elapsed = time.monotonic() - start
    assert states == [None]                   # timeout -> NO STATE, never acceptance
    assert elapsed < 5.0                      # returned bounded by the timeout, not wedged
    assert ib.calls == 1


def test_what_if_timeout_never_recorded_as_acceptance():
    # A timeout must map to None, never to a truthy/accepted state.
    ib = _ScriptedIB({"TFLO": "hang"})
    states = orm.what_if(ib, [_built("TFLO")], timeout=0.15)
    assert states[0] is None


def test_what_if_hang_does_not_wedge_following_orders():
    # A single hung order must not stop the loop — the next, healthy order still validates.
    ib = _ScriptedIB({"TFLO": "hang", "SPY": "ok"})
    states = orm.what_if(ib, [_built("TFLO"), _built("SPY")], timeout=0.2)
    assert states[0] is None                  # the hung one timed out
    assert states[1] is not None              # the healthy one still returned its state
    assert states[1].initMarginChange == "1234.5"
    assert ib.calls == 2                       # the loop advanced past the hang


def test_what_if_real_rejection_still_returns_none():
    # The pre-existing exception path (a genuine rejection) is preserved -> None.
    ib = _ScriptedIB({"TFLO": "reject"})
    states = orm.what_if(ib, [_built("TFLO")], timeout=1.0)
    assert states == [None]


def test_what_if_accepted_returns_state():
    ib = _ScriptedIB({"SPY": "ok"})
    states = orm.what_if(ib, [_built("SPY")], timeout=1.0)
    assert len(states) == 1
    assert states[0] is not None
    assert states[0].commission == 1.0
    assert states[0].commissionCurrency == "USD"


def test_what_if_default_timeout_is_the_named_constant():
    # The default must come from the module-level constant (a sane bound, not 0/None).
    assert isinstance(orm.WHATIF_TIMEOUT_SEC, (int, float))
    assert orm.WHATIF_TIMEOUT_SEC > 0
