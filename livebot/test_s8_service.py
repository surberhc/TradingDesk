"""test_s8_service.py — OFFLINE tests for the S8 unified all-day service (Phase 4a).

100% offline: NO real broker, NO network, NO real sleeps. Exercises the unified
entry+exit service (``s8_service.S8Service``) end to end with a fake IB, synthetic tick
streams, a synthetic 0DTE chain, and a capture double that persists a real open
TradeRecord. Every test points ``S8_PILOT_ROOT`` at a pytest ``tmp_path`` so the real
C:\\TradingDesk-Local\\s8_pilot tree is NEVER touched.

What is proved here (the service's own orchestration; the frozen pick math and the frozen
stop/B2 exit logic are covered by test_s8_strategy / test_s8_monitor):
  * FULL LIFECYCLE in one process: a due slot -> shared entry function persists an open
    TradeRecord + the legs get subscribed -> a synthetic tick stream crosses the stop ->
    the exit is finalized and the record closed.
  * ENTRY IDEMPOTENCY: the same (template, slot) evaluated twice -> exactly ONE entry
    (the second is skipped via the store-backed idempotency check).
  * CRASH RECOVERY: a service started against a store that already holds an open position
    resumes monitoring it and does NOT re-enter its slot.
  * EOD: still-open positions get close_all_eod'd.
  * ZERO-TRANSMIT: order_router.place / place_laddered are wired to explode; the full
    lifecycle never trips them.

Run (from C:\\TradingDesk\\livebot):
    powershell -Command "$env:PYTHONPATH=''; C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s8_service.py -q"
"""

from __future__ import annotations

import pandas as pd
import pytest

import s8_runner
import s8_service
import s8_store
from s8_monitor_core import Sample
from s8_schema import (
    EntryInfo,
    LegGrab,
    Provenance,
    TradeRecord,
    make_trade_id,
)
from s8_strategy import stop_price as frozen_stop_price

DATE = "20260717"
TEMPLATE = "Puts-80-$4"
SLOT = "12:35"
SHORT, LONG = 7480.0, 7445.0
CREDIT = 4.05
STOP_MULTIPLE = 2.0
STOP_PRICE = frozen_stop_price(CREDIT, STOP_MULTIPLE)   # 6.0 (frozen formula)
ENTRY_ISO = "2026-07-17T12:35:00.000-05:00"


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path, monkeypatch):
    """Point the store at a throwaway root for every test (real tree untouched)."""
    monkeypatch.setenv("S8_PILOT_ROOT", str(tmp_path))
    assert s8_store.get_root() == tmp_path
    return tmp_path


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #

class _FakeClient:
    def __init__(self):
        self._next = 1000

    def getReqId(self) -> int:
        self._next += 1
        return self._next


class _FakeEvent:
    """Minimal stand-in for ib.pendingTickersEvent supporting += / -=."""
    def __iadd__(self, handler):
        return self

    def __isub__(self, handler):
        return self


class _FakeTicker:
    def __init__(self, contract):
        self.contract = contract


class _FakeIB:
    """Just enough of ib_async.IB for the service's offline seams: an account summary read,
    a reqId source (order-group build), and the read-only market-data subscribe surface the
    monitor's _subscribe uses. There is NO placeOrder / order path here."""

    def __init__(self, summary):
        self._summary = summary
        self.client = _FakeClient()
        self.pendingTickersEvent = _FakeEvent()
        self.disconnected = False
        self.subscribed = []     # contracts passed to reqMktData
        self.cancelled = []

    def accountSummary(self):
        return self._summary

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def reqMktData(self, contract, *a, **k):
        self.subscribed.append(contract)
        return _FakeTicker(contract)

    def cancelMktData(self, contract):
        self.cancelled.append(contract)

    def isConnected(self):
        return True

    def waitOnUpdate(self, timeout=0):
        return None

    def disconnect(self):
        self.disconnected = True


def _synthetic_chain_snapshot() -> pd.DataFrame:
    """Simple internally-consistent PUT ladder (same construction as test_s8_runner): a
    real (non-None) pick is found end to end, offline."""
    rows = []
    for k in range(0, 205, 5):
        bid = k * 0.05
        ask = bid + 0.05
        rows.append({"strike": float(k), "right": "PUT", "bid": bid, "ask": ask})
    df = pd.DataFrame(rows, columns=["strike", "right", "bid", "ask"])
    df.attrs["spot"] = 100.0
    df.attrs["expiration"] = "20260717"
    df.attrs["snapshot_time"] = "2026-07-17T12:35:00.000"
    return df


def _leg(strike, right="P", greeks=True):
    return LegGrab(
        right=right, strike=strike, bid=1.0, ask=1.2, last=1.1,
        bid_size=10, ask_size=12, volume=100, open_interest=500,
        delta=-0.2 if greeks else None, gamma=0.01 if greeks else None,
        vega=0.4 if greeks else None, theta=-0.8 if greeks else None,
        iv=0.22 if greeks else None, underlying_spot=7400.0,
        grab_ts=ENTRY_ISO, complete=greeks,
    )


def _canonical_open_record(trade_id, status="open"):
    """A well-formed open TradeRecord with a known 6.0 stop, matching test_s8_monitor's
    conventions so the synthetic crossing stream and the pnl arithmetic line up."""
    return TradeRecord(
        trade_id=trade_id, date=DATE, account="U14438624",
        template=TEMPLATE, slot=SLOT, side="PUT", expiration="20260717",
        qty=1, status=status,
        entry=EntryInfo(
            entry_ts=ENTRY_ISO, entry_spot=7400.0, entry_vix=14.0,
            short_strike=SHORT, long_strike=LONG, width=SHORT - LONG,
            realized_credit=CREDIT, stop_multiple=STOP_MULTIPLE, stop_price=STOP_PRICE,
            short_leg=_leg(SHORT), long_leg=_leg(LONG), greeks_complete=True,
        ),
        provenance=Provenance(paperbot_version="0.16.0", pilot_mode=True),
    )


def _capture_double_factory(calls):
    """A stand-in for s8_capture.capture_and_persist_entry that persists a canonical open
    TradeRecord (so the composed monitor can pick it up + monitor it) and returns its
    trade_id. The live capture (greeks/quotes over the gateway) is covered by the s8_capture
    tests; here we only need a real persisted open record to drive the service orchestration.
    """
    def _double(ib, pick, cfg, account, qty, chain_snap, stop_price):
        calls.append((pick, stop_price))
        trade_id = make_trade_id(DATE, TEMPLATE, SLOT, SHORT, LONG)
        s8_store.upsert_trade_record(_canonical_open_record(trade_id))
        return trade_id
    return _double


def _boom_place(*a, **k):
    raise AssertionError("order_router.place must NEVER be called (PILOT_MODE / zero-transmit)")


def _sample(ts_offset, short_ask, long_bid, *, spot=7400.0):
    return Sample(ts=1_000_000.0 + ts_offset, short_ask=short_ask,
                  short_bid=short_ask - 0.2, short_last=short_ask,
                  long_bid=long_bid, long_ask=long_bid + 0.2, long_last=long_bid, spot=spot)


def _records_by_id():
    return {r.trade_id: r for r in s8_store.read_trade_records()}


def _count_trade_lines():
    path = s8_store._trades_file()
    if not path.exists():
        return 0
    with open(path, "r", encoding="utf-8") as fh:
        return sum(1 for ln in fh if ln.strip())


def _wire_entry(monkeypatch, calls, *, due=((TEMPLATE, SLOT),)):
    """Shared monkeypatching so a due slot flows through the SHARED entry function offline:
    fixed 'today', a forced due list, the synthetic chain, the capture double, and the
    zero-transmit tripwires."""
    monkeypatch.setattr(s8_service, "current_ct_date", lambda: DATE)
    monkeypatch.setattr(s8_runner, "due_templates", lambda now: list(due))
    monkeypatch.setattr(s8_service.s8_chain, "snapshot_0dte_chain",
                        lambda ib, *a, **k: _synthetic_chain_snapshot())
    monkeypatch.setattr(s8_runner.s8_capture, "capture_and_persist_entry",
                        _capture_double_factory(calls))
    monkeypatch.setattr(s8_runner.order_router, "place", _boom_place)
    monkeypatch.setattr(s8_runner.order_router, "place_laddered", _boom_place)


_MARGIN_OK = {"AccountType": "MARGIN", "BuyingPower": 10_000_000.0,
              "ExcessLiquidity": 5_000_000.0}


# --------------------------------------------------------------------------- #
# FULL LIFECYCLE — entry -> subscribe -> stop-out -> finalize (one process)
# --------------------------------------------------------------------------- #

def test_full_lifecycle_entry_subscribe_stopout(monkeypatch):
    calls = []
    _wire_entry(monkeypatch, calls)

    ib = _FakeIB(_MARGIN_OK)
    svc = s8_service.S8Service(account="U14438624")
    svc._bind_ib(ib)

    # --- ENTRY: the due slot flows through the shared entry function ---
    outcomes = svc.entry_cycle(ib)
    assert len(outcomes) == 1
    tid = make_trade_id(DATE, TEMPLATE, SLOT, SHORT, LONG)
    assert outcomes[0]["trade_id"] == tid
    assert "would_transmit" in outcomes[0]              # approved, would-have-transmitted
    assert len(calls) == 1                               # capture ran exactly once

    # persisted an OPEN record, is now monitored, and its legs got subscribed
    assert _records_by_id()[tid].status == "open"
    assert tid in svc.monitor._positions
    assert tid in svc.monitor._tickers
    assert len(ib.subscribed) == 2                       # short + long legs

    # --- EXIT: a synthetic tick stream crosses the 6.0 stop -> finalize ---
    for i, a in enumerate([2.0, 4.0, 5.9, 6.0]):         # crosses at 6.0
        svc.monitor.on_sample(tid, _sample(60 * (i + 1), a, 0.5),
                              short_leg=_leg(SHORT), long_leg=_leg(LONG))

    rec = _records_by_id()[tid]
    assert rec.status == "closed"
    assert rec.exit.exit_reason == "stop_hit"
    # crossing short_ask=6.0, long_bid=0.5 -> close 5.5 -> pnl (4.05-5.5)*100 = -145
    assert rec.exit.pnl == pytest.approx(-145.0)
    assert tid not in s8_store.read_open_state()
    assert tid not in svc.monitor._positions
    # ticks were written to the date-partitioned parquet
    ticks = pd.read_parquet(s8_store._ticks_dir())
    assert set(ticks["trade_id"]) == {tid}


# --------------------------------------------------------------------------- #
# ENTRY IDEMPOTENCY — same (template, slot) twice -> exactly ONE entry
# --------------------------------------------------------------------------- #

def test_entry_idempotency_second_due_check_skips(monkeypatch):
    calls = []
    _wire_entry(monkeypatch, calls)

    ib = _FakeIB(_MARGIN_OK)
    svc = s8_service.S8Service(account="U14438624")
    svc._bind_ib(ib)

    first = svc.entry_cycle(ib)                           # enters
    second = svc.entry_cycle(ib)                           # same slot again -> skipped

    tid = make_trade_id(DATE, TEMPLATE, SLOT, SHORT, LONG)
    assert first and first[0]["trade_id"] == tid
    assert second == []                                    # idempotent no-op
    assert len(calls) == 1                                 # capture ran ONCE total
    assert _count_trade_lines() == 1                       # exactly one persisted entry
    assert list(_records_by_id().keys()) == [tid]


# --------------------------------------------------------------------------- #
# CRASH RECOVERY — resume an already-open position, do NOT re-enter its slot
# --------------------------------------------------------------------------- #

def test_crash_recovery_resumes_and_does_not_reenter(monkeypatch):
    calls = []
    _wire_entry(monkeypatch, calls)

    # Seed the store as if a prior life had already entered this slot (open position),
    # then start a fresh service against that store.
    tid = make_trade_id(DATE, TEMPLATE, SLOT, SHORT, LONG)
    s8_store.upsert_trade_record(_canonical_open_record(tid))
    s8_store.write_open_state({tid: {"status": "open"}})

    ib = _FakeIB(_MARGIN_OK)
    svc = s8_service.S8Service(account="U14438624")

    # RESUME: reloads the open position from the durable store and subscribes it.
    assert svc.resume(ib) == [tid]
    assert tid in svc.monitor._positions
    assert tid in svc.monitor._tickers                     # re-subscribed on resume

    # A due-check for the SAME slot must NOT re-enter (store-backed idempotency).
    outcomes = svc.entry_cycle(ib)
    assert outcomes == []
    assert len(calls) == 0                                 # capture never ran
    assert _count_trade_lines() == 1                       # still the single seeded entry

    # And it still monitors normally after the restart: a crossing tick finalizes it.
    svc.monitor.on_sample(tid, _sample(60, 6.5, 0.5),
                          short_leg=_leg(SHORT), long_leg=_leg(LONG))
    assert _records_by_id()[tid].status == "closed"
    assert _records_by_id()[tid].exit.exit_reason == "stop_hit"


# --------------------------------------------------------------------------- #
# EOD — still-open positions get closed at session end
# --------------------------------------------------------------------------- #

def test_eod_closes_still_open_positions(monkeypatch):
    calls = []
    _wire_entry(monkeypatch, calls)

    ib = _FakeIB(_MARGIN_OK)
    svc = s8_service.S8Service(account="U14438624")
    svc._bind_ib(ib)

    tid = make_trade_id(DATE, TEMPLATE, SLOT, SHORT, LONG)
    svc.entry_cycle(ib)
    # non-crossing samples all session (short_ask under the 6.0 stop) -> stays open
    for i, (a, lb) in enumerate([(2.0, 0.5), (2.5, 0.5), (1.5, 0.3)]):
        svc.monitor.on_sample(tid, _sample(60 * (i + 1), a, lb),
                              short_leg=_leg(SHORT), long_leg=_leg(LONG))
    assert _records_by_id()[tid].status == "open"

    closed = svc.close_eod(reason="eod")
    assert closed == [tid]
    rec = _records_by_id()[tid]
    assert rec.status == "closed"
    assert rec.exit.exit_reason == "eod"
    # final marks short_ask 1.5, long_bid 0.3 -> close 1.2 -> pnl (4.05-1.2)*100 = 285
    assert rec.exit.pnl == pytest.approx(285.0)
    assert tid not in s8_store.read_open_state()


# --------------------------------------------------------------------------- #
# Entry guards — nothing due, and the TBD fail-closed refusal
# --------------------------------------------------------------------------- #

def test_entry_cycle_no_due_is_noop(monkeypatch):
    monkeypatch.setattr(s8_service, "current_ct_date", lambda: DATE)
    monkeypatch.setattr(s8_runner, "due_templates", lambda now: [])

    def _boom_chain(*a, **k):
        raise AssertionError("must not touch the chain when nothing is due")

    monkeypatch.setattr(s8_service.s8_chain, "snapshot_0dte_chain", _boom_chain)

    ib = _FakeIB(_MARGIN_OK)
    svc = s8_service.S8Service(account="U14438624")
    svc._bind_ib(ib)
    assert svc.entry_cycle(ib) == []


def test_entry_cycle_account_tbd_enters_nothing(monkeypatch):
    calls = []
    _wire_entry(monkeypatch, calls)

    ib = _FakeIB(_MARGIN_OK)
    svc = s8_service.S8Service(account="TBD")
    svc._bind_ib(ib)
    assert svc.entry_cycle(ib) == []
    assert len(calls) == 0
    assert _count_trade_lines() == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
