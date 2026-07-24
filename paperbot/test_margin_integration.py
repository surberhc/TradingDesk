"""
test_margin_integration.py — order_router.place()/place_laddered() margin observability
wiring (conductor #26).

NO broker, NO gateway, NO network. Reuses the same FakeIB harness style as
test_order_idempotency.py (arm the transmit guard IN MEMORY, redirect STATE_DIR to a tmp
dir). Proves:
  (1) armed+permitted place() with an accountSummary that returns rows -> result["margin"]
      populated (before/after/delta) AND a kind='margin_impact' ledger record written.
  (2) dry/unarmed place() -> result has margin None or absent, NO margin_impact record.
  (3) accountSummary raising -> the order path still completes and result["margin"] is None
      (fail-soft — margin capture never blocks or alters a trade).
  (4) place_laddered() armed -> result["margin"] populated + margin_impact written.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_margin_integration.py -q
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

import config
import order_router as orm


# --- fake broker (mirrors test_order_idempotency.FakeIB, + accountSummary) ----------
class _OrderStatus:
    def __init__(self, filled, remaining):
        self.status = "Filled" if remaining <= 0 else "Submitted"
        self.filled = filled
        self.remaining = remaining
        self.avgFillPrice = 100.0


class _Trade:
    def __init__(self, contract, order):
        self.contract = contract
        self.order = order
        qty = float(order.totalQuantity)
        self.orderStatus = _OrderStatus(qty, 0.0)

    def isDone(self):
        return True


def _row(tag, value):
    return SimpleNamespace(tag=tag, value=value)


_SUMMARY_BEFORE = {
    "AccountType": "MARGIN", "NetLiquidation": "1000000", "BuyingPower": "3800000",
    "ExcessLiquidity": "900000", "InitMarginReq": "100000", "MaintMarginReq": "80000",
    "AvailableFunds": "850000",
}
_SUMMARY_AFTER = dict(_SUMMARY_BEFORE, BuyingPower="3500000", ExcessLiquidity="850000")


class FakeIB:
    """Records placeOrder; empty broker truth (all legs FRESH). accountSummary returns a
    configurable list of fake rows, and steps BEFORE->AFTER across successive calls so the
    per-run before/after diff is non-trivial. `summary_fail=True` makes accountSummary raise
    (the fail-soft case)."""
    def __init__(self, summary_fail=False):
        self.summary_fail = summary_fail
        self.placed = []
        self.cancelled = []
        self._summary_calls = 0

    def reqAllOpenOrders(self):
        return []

    def reqExecutions(self, _flt=None):
        return []

    def qualifyContracts(self, *contracts):
        for c in contracts:
            c.conId = 1
        return contracts

    def placeOrder(self, contract, order):
        t = _Trade(contract, order)
        self.placed.append((contract, order))
        return t

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def accountSummary(self, account):
        if self.summary_fail:
            raise TimeoutError("simulated accountSummary timeout")
        self._summary_calls += 1
        src = _SUMMARY_BEFORE if self._summary_calls == 1 else _SUMMARY_AFTER
        return [_row(k, v) for k, v in src.items()]

    def sleep(self, *_):
        pass

    def disconnect(self):
        pass


AS_OF = "2026-07-24"
CAPS = {"marketable_limit": 100.1, "midprice": 100.1, "adaptive": 100.1, "rel": 100.1}


@pytest.fixture(autouse=True)
def _arm_and_isolate(monkeypatch, tmp_path):
    """Arm the transmit guard in memory (disk config stays locked) and redirect STATE_DIR +
    ledger paths to tmp so the audit write is hermetic."""
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    import ledger
    monkeypatch.setattr(ledger, "RUNS_JSONL", os.path.join(str(tmp_path), "runs.jsonl"))
    monkeypatch.setattr(ledger, "LOG_TXT", os.path.join(str(tmp_path), "paperbot.log"))


def _built(account, side, symbol, qty, price=100.0):
    intent = SimpleNamespace(symbol=symbol, side=side, quantity=qty, limit_price=price)
    return orm.build([intent], account, AS_OF)[0]


def _runs_path(tmp_path):
    return os.path.join(str(tmp_path), "runs.jsonl")


def _margin_records(tmp_path):
    p = _runs_path(tmp_path)
    if not os.path.exists(p):
        return []
    out = []
    for line in open(p, encoding="utf-8").read().splitlines():
        rec = json.loads(line)
        if rec.get("kind") == "margin_impact":
            out.append(rec)
    return out


# ============================================================================
# place() — armed captures margin; dry/fail behave correctly
# ============================================================================
def test_place_armed_populates_margin_and_writes_ledger(tmp_path):
    b1 = _built("DU1", "BUY", "SPY", 10)
    ib = FakeIB()
    res = orm.place(ib, [b1], armed=True)

    assert len(ib.placed) == 1                       # the FRESH leg transmitted
    m = res["margin"]
    assert m is not None
    assert m["account"] == "DU1"
    assert m["context"] == "place"
    assert m["before"]["buying_power"] == 3_800_000.0
    assert m["after"]["buying_power"] == 3_500_000.0
    assert m["delta"]["buying_power_delta"] == -300_000.0

    recs = _margin_records(tmp_path)
    assert len(recs) == 1
    assert recs[0]["account"] == "DU1"


def test_place_uses_explicit_account_and_context(tmp_path):
    b1 = _built("DU1", "BUY", "SPY", 10)
    ib = FakeIB()
    res = orm.place(ib, [b1], armed=True, account="DUX", context="morning_execute")
    assert res["margin"]["account"] == "DUX"
    assert res["margin"]["context"] == "morning_execute"


def test_place_dry_run_no_margin_no_record(tmp_path):
    b1 = _built("DU1", "BUY", "SPY", 10)
    ib = FakeIB()
    res = orm.place(ib, [b1], armed=False)           # blocked: not armed
    assert ib.placed == []
    assert res.get("margin") is None
    assert _margin_records(tmp_path) == []


def test_place_summary_failure_is_fail_soft(tmp_path):
    # accountSummary raises -> order path STILL completes; margin is None; no record written.
    b1 = _built("DU1", "BUY", "SPY", 10)
    ib = FakeIB(summary_fail=True)
    res = orm.place(ib, [b1], armed=True)

    assert len(ib.placed) == 1                       # order still transmitted normally
    assert res["transmitted"] == 1
    # to_record(None, None) is a shaped dict with None before/after; nothing persisted.
    assert res["margin"]["before"] is None
    assert res["margin"]["after"] is None
    assert _margin_records(tmp_path) == []           # both None -> record_impact skipped


# ============================================================================
# place_laddered() — armed captures margin
# ============================================================================
def test_laddered_armed_populates_margin_and_writes_ledger(tmp_path):
    ref = orm._order_ref("DU1", AS_OF, "BUY", "TFLO")
    ib = FakeIB()
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=CAPS,
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        order_ref=ref, armed=True, rung_seconds=1, poll=0)

    assert len(ib.placed) >= 1
    m = res["margin"]
    assert m is not None
    assert m["account"] == "DU1"
    assert m["context"] == "place_laddered"
    assert m["delta"]["buying_power_delta"] == -300_000.0
    assert len(_margin_records(tmp_path)) == 1


def test_laddered_dry_run_no_margin(tmp_path):
    ref = orm._order_ref("DU1", AS_OF, "BUY", "TFLO")
    ib = FakeIB()
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=CAPS,
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        order_ref=ref, armed=False, rung_seconds=1, poll=0)
    assert ib.placed == []
    assert res.get("margin") is None
    assert _margin_records(tmp_path) == []
