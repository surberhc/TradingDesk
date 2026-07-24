"""
test_flatten_accounts.py — offline unit tests for the SAFETY REWORK of flatten_accounts.py
(conductor #51). NO broker, NO gateway. A MockIB records/serves everything.

Proves the guarantees the rework rests on:
  (a) no allowlist -> main() exits non-zero and NEVER connects;
  (b) DRY-RUN (default) places NOTHING;
  (c) an OPTION (non-STK) position is REFUSED — never priced via a Stock quote, never sent;
  (d) a NaN / 0 / negative price is rejected by flatten's OWN guard (not just live_quotes');
  (e) a still-working order is CANCELLED before disconnect, and a non-flat reconcile is a
      hard failure (non-zero exit).

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_flatten_accounts.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import flatten_accounts as fa
import live_quotes


# --- minimal ib_async stand-ins -------------------------------------------------
def _contract(symbol="SPY", sec_type="STK", con_id=1, exchange="ARCA"):
    return SimpleNamespace(symbol=symbol, secType=sec_type, conId=con_id,
                           exchange=exchange)


def _position(account, contract, qty):
    return SimpleNamespace(account=account, contract=contract, position=float(qty))


class _OrderStatus:
    def __init__(self, status, filled, avg):
        self.status = status
        self.filled = float(filled)
        self.avgFillPrice = float(avg)


class _Trade:
    """A Trade stand-in. `done` controls isDone(); an un-done trade is a still-WORKING
    order (the case the tool must cancel before disconnect)."""
    def __init__(self, contract, order, status, filled, done):
        self.contract = contract
        self.order = order
        self.orderStatus = _OrderStatus(status, filled, order.lmtPrice)
        self._done = done

    def isDone(self):
        return self._done


class MockIB:
    """Serves positions and records placeOrder/cancelOrder. `working` (bool) makes every
    placed order come back still-working (not done) so the cancel-before-disconnect path is
    exercised. `reconcile_positions`, if set, is what the SECOND reqPositions()/positions()
    returns (the post-fill reconcile read)."""
    def __init__(self, positions, working=False, reconcile_positions=None):
        self._positions = list(positions)
        self._reconcile = reconcile_positions
        self._reads = 0
        self.working = working
        self.placed = []       # (contract, order)
        self.cancelled = []    # orders
        self.disconnected = False

    def reqPositions(self):
        self._reads += 1

    def positions(self):
        # First read = initial scan; a later read (after fills) = reconcile snapshot.
        if self._reads > 1 and self._reconcile is not None:
            return list(self._reconcile)
        return list(self._positions)

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        if self.working:
            t = _Trade(contract, order, "Submitted", 0.0, done=False)
        else:
            t = _Trade(contract, order, "Filled", order.totalQuantity, done=True)
        return t

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def sleep(self, *_):
        pass

    def disconnect(self):
        self.disconnected = True


def _quote(symbol="SPY", bid=100.0, ask=100.10, last=100.05, close=100.0):
    return live_quotes.Quote(symbol, bid=bid, ask=ask, last=last, close=close, md_type=1)


@pytest.fixture(autouse=True)
def _no_ledger(monkeypatch):
    """Never touch the real off-Drive audit ledger from a unit test."""
    monkeypatch.setattr(fa.ledger, "record_run", lambda *_a, **_k: "")


# --- (a) hard guard: no allowlist -> non-zero exit, no connection ---------------
def test_main_refuses_without_any_allowlist(monkeypatch):
    connected = {"n": 0}
    monkeypatch.setattr(fa, "IB", lambda: (_ for _ in ()).throw(
        AssertionError("must not construct IB / connect without an allowlist")))
    assert fa.main([]) == 2
    assert connected["n"] == 0


def test_main_refuses_accounts_without_symbols_or_conids(monkeypatch):
    monkeypatch.setattr(fa, "IB", lambda: (_ for _ in ()).throw(
        AssertionError("must not connect without a symbol/conid allowlist")))
    assert fa.main(["--accounts", "DU8922143"]) == 2


def test_main_refuses_symbols_without_accounts(monkeypatch):
    monkeypatch.setattr(fa, "IB", lambda: (_ for _ in ()).throw(
        AssertionError("must not connect without an account allowlist")))
    assert fa.main(["--symbols", "SPY"]) == 2


# --- (b) dry-run (default) places nothing ---------------------------------------
def test_dry_run_places_nothing(monkeypatch):
    pos = [_position("DU8922143", _contract("SPY", con_id=1), 100)]
    ib = MockIB(pos)
    monkeypatch.setattr(live_quotes, "fetch", lambda _ib, syms: {s: _quote(s) for s in syms})

    rc = fa.flatten(ib, ["DU8922143"], ["SPY"], None, execute=False)

    assert ib.placed == []          # nothing transmitted in dry-run
    assert ib.cancelled == []
    assert rc == 0                  # a clean, fully-flattenable preview


def test_out_of_scope_symbol_is_ignored(monkeypatch):
    # A held position NOT on the --symbols allowlist must never be swept.
    pos = [_position("DU8922143", _contract("VTI", con_id=9), 800)]
    ib = MockIB(pos)
    monkeypatch.setattr(live_quotes, "fetch", lambda _ib, syms: {s: _quote(s) for s in syms})
    rc = fa.flatten(ib, ["DU8922143"], ["SPY"], None, execute=False)
    assert ib.placed == []
    assert rc == 0                  # nothing in scope is held


# --- (c) an OPTION position is REFUSED (never priced via Stock, never sent) ------
def test_option_position_is_refused_not_priced(monkeypatch):
    opt = _position("DU8922143", _contract("SPY", sec_type="OPT", con_id=42), -1)
    ib = MockIB([opt])
    called = {"fetch": 0}

    def _fetch(_ib, syms):
        called["fetch"] += 1
        return {s: _quote(s) for s in syms}

    monkeypatch.setattr(live_quotes, "fetch", _fetch)

    rc = fa.flatten(ib, ["DU8922143"], ["SPY"], None, execute=True)

    assert ib.placed == []              # no naked option leg ever sent
    assert called["fetch"] == 0         # never priced the option at all
    assert rc == 3                      # refusal is a hard, non-flat failure


# --- (d) NaN / 0 / negative price rejected by flatten's OWN guard ----------------
@pytest.mark.parametrize("bad", [float("nan"), 0.0, -1.0, float("inf")])
def test_bad_price_is_rejected(monkeypatch, bad):
    # A Quote whose every field is the bad value — flatten must reject it itself and NOT
    # place an order. (live_quotes normally screens these, so we bypass it and hand the bad
    # values straight to flatten to prove its own guard fires.)
    pos = [_position("DU8922143", _contract("SPY", con_id=1), 100)]
    ib = MockIB(pos)
    bad_q = live_quotes.Quote("SPY", bid=bad, ask=bad, last=bad, close=bad, md_type=1)
    monkeypatch.setattr(live_quotes, "fetch", lambda _ib, syms: {"SPY": bad_q})

    rc = fa.flatten(ib, ["DU8922143"], ["SPY"], None, execute=True)

    assert ib.placed == []
    assert rc == 3                      # unpriceable -> non-flat hard failure


def test_valid_price_helper():
    assert fa._valid_price(1.23) is True
    assert fa._valid_price(0.0) is False
    assert fa._valid_price(-5.0) is False
    assert fa._valid_price(float("nan")) is False
    assert fa._valid_price(float("inf")) is False
    assert fa._valid_price(None) is False


# --- (e) still-working order cancelled before disconnect + hard-fail reconcile ---
def test_working_order_is_cancelled_and_run_fails(monkeypatch):
    pos = [_position("DU8922143", _contract("SPY", con_id=1), 100)]
    # order stays WORKING (never fills); reconcile still shows the position -> non-flat.
    ib = MockIB(pos, working=True, reconcile_positions=pos)
    monkeypatch.setattr(live_quotes, "fetch", lambda _ib, syms: {s: _quote(s) for s in syms})

    rc = fa.flatten(ib, ["DU8922143"], ["SPY"], None, execute=True)

    assert len(ib.placed) == 1
    assert len(ib.cancelled) == 1       # the resting order was cancelled before we returned
    assert ib.cancelled[0] is ib.placed[0][1]   # it was THE placed order that got cancelled
    assert rc == 3                      # non-flat reconcile is a hard failure


def test_clean_fill_flattens_and_copies_contract(monkeypatch):
    pos = [_position("DU8922143", _contract("SPY", con_id=1, exchange="ARCA"), 100)]
    ib = MockIB(pos, working=False, reconcile_positions=[])   # fills, then flat
    monkeypatch.setattr(live_quotes, "fetch", lambda _ib, syms: {s: _quote(s) for s in syms})

    rc = fa.flatten(ib, ["DU8922143"], ["SPY"], None, execute=True)

    assert len(ib.placed) == 1
    sent_contract, sent_order = ib.placed[0]
    assert sent_order.action == "SELL"                 # closing a long
    assert sent_order.totalQuantity == 100
    assert sent_contract.exchange == "SMART"           # routed SMART on the COPY
    assert pos[0].contract.exchange == "ARCA"          # live position object NOT mutated
    assert ib.cancelled == []
    assert rc == 0                                     # fully flat


def test_execute_sells_long_covers_short(monkeypatch):
    pos = [_position("DU8922143", _contract("SPY", con_id=1), -50)]   # short
    ib = MockIB(pos, working=False, reconcile_positions=[])
    monkeypatch.setattr(live_quotes, "fetch", lambda _ib, syms: {s: _quote(s) for s in syms})
    fa.flatten(ib, ["DU8922143"], ["SPY"], None, execute=True)
    _c, order = ib.placed[0]
    assert order.action == "BUY"                       # cover the short
    assert order.totalQuantity == 50
