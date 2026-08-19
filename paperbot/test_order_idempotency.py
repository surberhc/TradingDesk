"""
test_order_idempotency.py — the S0 order-idempotency acceptance matrix (spec §4).

NO broker, NO gateway, NO network. A fake `ib` records every placeOrder and serves
configurable broker truth (reqAllOpenOrders + reqExecutions). Proves the invariant: for a
given cycle each orderRef transmits AT MOST ONCE — a crash-resume, retry, manual re-run, or
a stacked ladder places NOTHING the broker already has working or filled, and never blindly
auto-retries an uncertain partial. Fails CLOSED on any broker-read failure.

Layers exercised:
  * already_present()  — the pre-transmit dedup classifier (broker + journal).
  * place()            — per-leg gate on the direct/block path.
  * place_laddered()   — per-leg gate before rung 1.
  * transmit_journal   — append-only ATTEMPTING/SENT/CYCLE_COMPLETE round trip.
  * the PER-RUN ref stamp (v0.34.0) — a NEW run is NEW WORK, but WITHIN one run nothing
    double-submits, and a SELL and a BUY of one symbol in one run never collide.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_order_idempotency.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import config
import order_router as orm
import transmit_journal as tj


# --- fake broker -------------------------------------------------------------
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
        self.orderStatus = _OrderStatus(qty, 0.0)   # fake fills fully so place() completes

    def isDone(self):
        return True


class _Exec:
    def __init__(self, order_ref, shares):
        self.orderRef = order_ref
        self.shares = shares
        self.acctNumber = "DU1"


class _Fill:
    def __init__(self, order_ref, shares):
        self.execution = _Exec(order_ref, shares)


class _OpenTrade:
    def __init__(self, order_ref):
        self.order = SimpleNamespace(orderRef=order_ref)


class FakeIB:
    """Records placeOrder. Broker truth is configured up front: `open_refs` = orderRefs
    currently working; `filled_by_ref` = {ref: total_shares}. `fail=True` makes both read
    APIs raise (the timeout/error case)."""
    def __init__(self, open_refs=None, filled_by_ref=None, fail=False):
        self.open_refs = list(open_refs or [])
        self.filled_by_ref = dict(filled_by_ref or {})
        self.fail = fail
        self.placed = []          # (contract, order)
        self.cancelled = []

    def reqAllOpenOrders(self):
        if self.fail:
            raise TimeoutError("simulated reqAllOpenOrders timeout")
        return [_OpenTrade(r) for r in self.open_refs]

    def reqExecutions(self, _flt=None):
        if self.fail:
            raise TimeoutError("simulated reqExecutions timeout")
        out = []
        for ref, shares in self.filled_by_ref.items():
            out.append(_Fill(ref, shares))
        return out

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

    def disconnect(self):
        pass

    def sleep(self, *_):
        pass

    def whatIfOrder(self, *_a, **_k):
        raise AssertionError("whatIfOrder must NEVER be called (it hangs).")


@pytest.fixture(autouse=True)
def _arm_and_isolate(monkeypatch, tmp_path):
    """Arm the transmit guard IN MEMORY (config on disk stays locked) and redirect STATE_DIR
    to a tmp dir so the transmit journal is hermetic per test."""
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))


def _built(account, as_of, side, symbol, qty, price=100.0):
    intent = SimpleNamespace(symbol=symbol, side=side, quantity=qty, limit_price=price)
    return orm.build([intent], account, as_of)[0]


def _ref(account, as_of, side, symbol):
    return orm._order_ref(account, as_of, side, symbol)


AS_OF = "2026-07-20"
CAPS = {"marketable_limit": 100.1, "midprice": 100.1, "adaptive": 100.1, "rel": 100.1}


# ============================================================================
# already_present() — the classifier
# ============================================================================
def test_fresh_when_nothing_open_or_filled():
    ib = FakeIB()
    assert orm.already_present(ib, _ref("DU1", AS_OF, "BUY", "SPY"), 10) == orm.LegState.FRESH


def test_working_when_ref_in_open_orders():
    ref = _ref("DU1", AS_OF, "BUY", "SPY")
    ib = FakeIB(open_refs=[ref])
    assert orm.already_present(ib, ref, 10) == orm.LegState.WORKING


def test_complete_when_filled_ge_target():
    ref = _ref("DU1", AS_OF, "BUY", "SPY")
    ib = FakeIB(filled_by_ref={ref: 10})
    assert orm.already_present(ib, ref, 10) == orm.LegState.COMPLETE


def test_partial_when_between_zero_and_target():
    ref = _ref("DU1", AS_OF, "BUY", "SPY")
    ib = FakeIB(filled_by_ref={ref: 4})
    assert orm.already_present(ib, ref, 10) == orm.LegState.PARTIAL


def test_working_beats_partial_no_stacking():
    # A ref that is BOTH partially filled AND still working must read WORKING (skip), never
    # PARTIAL — otherwise a resume would stack a fresh ladder on the resting remainder.
    ref = _ref("DU1", AS_OF, "BUY", "SPY")
    ib = FakeIB(open_refs=[ref], filled_by_ref={ref: 4})
    assert orm.already_present(ib, ref, 10) == orm.LegState.WORKING


def test_unknown_on_broker_read_failure():
    ref = _ref("DU1", AS_OF, "BUY", "SPY")
    ib = FakeIB(fail=True)
    assert orm.already_present(ib, ref, 10) == orm.LegState.UNKNOWN


def test_new_cycle_new_as_of_is_fresh():
    # Last month's cycle filled its ref; a NEW cycle carries a different as_of -> different
    # ref -> not falsely deduped.
    old_ref = _ref("DU1", "2026-06-20", "BUY", "SPY")
    new_ref = _ref("DU1", "2026-07-20", "BUY", "SPY")
    ib = FakeIB(filled_by_ref={old_ref: 10})
    assert orm.already_present(ib, new_ref, 10) == orm.LegState.FRESH


# --- journal (layer B) consulted by the gate ---------------------------------
def test_journal_sent_treated_complete():
    ref = _ref("DU1", AS_OF, "BUY", "SPY")
    tj.record_sent(ref, filled=10, remaining=0, rested_gtc=False, avg_px=100.0)
    ib = FakeIB()                       # broker shows nothing — journal alone -> COMPLETE
    assert orm.already_present(ib, ref, 10) == orm.LegState.COMPLETE


def test_journal_attempting_without_sent_is_unknown():
    ref = _ref("DU1", AS_OF, "BUY", "SPY")
    tj.record_attempting(ref, as_of=AS_OF, symbol="SPY", side="BUY", target_qty=10)
    ib = FakeIB()
    # placed-but-not-confirmed: uncertain -> UNKNOWN (skip + alert), never auto-retry.
    assert orm.already_present(ib, ref, 10) == orm.LegState.UNKNOWN


def test_explicit_journal_state_snapshot_overrides_disk():
    # Passing journal_state=None (the pre-ATTEMPTING snapshot) must ignore an ATTEMPTING the
    # SAME run just wrote — otherwise place()'s internal gate would trip on its own record.
    ref = _ref("DU1", AS_OF, "BUY", "SPY")
    tj.record_attempting(ref, as_of=AS_OF, symbol="SPY", side="BUY", target_qty=10)
    ib = FakeIB()
    assert orm.already_present(ib, ref, 10, journal_state=None) == orm.LegState.FRESH


def test_alert_states():
    assert orm.leg_state_needs_alert(orm.LegState.PARTIAL)
    assert orm.leg_state_needs_alert(orm.LegState.UNKNOWN)
    assert not orm.leg_state_needs_alert(orm.LegState.WORKING)
    assert not orm.leg_state_needs_alert(orm.LegState.COMPLETE)
    assert not orm.leg_state_needs_alert(orm.LegState.FRESH)


# ============================================================================
# place() — direct/block path, per-leg gate
# ============================================================================
def test_rerun_completed_cycle_zero_placeorder():
    # ACCEPTANCE: re-run of a fully-completed staged file -> 0 placeOrder calls.
    b1 = _built("DU1", AS_OF, "BUY", "SPY", 10)
    b2 = _built("DU1", AS_OF, "SELL", "TFLO", 5)
    ib = FakeIB(filled_by_ref={b1.order_ref: 10, b2.order_ref: 5})
    res = orm.place(ib, [b1, b2], armed=True)
    assert ib.placed == []                       # NOTHING re-sent
    assert res["transmitted"] == 0
    assert len(res["skipped"]) == 2
    assert all(s["state"] == orm.LegState.COMPLETE for s in res["skipped"])


def test_resume_leg1_complete_only_leg2_sent():
    # ACCEPTANCE: crash after leg 1 filled, resume -> leg 1 skipped (COMPLETE), only leg 2 sent.
    b1 = _built("DU1", AS_OF, "BUY", "SPY", 10)
    b2 = _built("DU1", AS_OF, "BUY", "IEF", 7)
    ib = FakeIB(filled_by_ref={b1.order_ref: 10})   # leg1 done, leg2 fresh
    res = orm.place(ib, [b1, b2], armed=True)
    assert len(ib.placed) == 1
    assert ib.placed[0][1].orderRef == b2.order_ref
    assert res["leg_states"][b1.order_ref] == orm.LegState.COMPLETE
    assert res["leg_states"][b2.order_ref] == orm.LegState.FRESH


def test_working_gtc_rest_skipped_no_stacking():
    # ACCEPTANCE: a resting GTC remainder exists for a ref -> leg skipped (WORKING), no stack.
    b1 = _built("DU1", AS_OF, "BUY", "SPY", 10)
    ib = FakeIB(open_refs=[b1.order_ref])
    res = orm.place(ib, [b1], armed=True)
    assert ib.placed == []
    assert res["skipped"][0]["state"] == orm.LegState.WORKING
    assert res["skipped"][0]["alert"] is False   # benign already-live skip


def test_partial_across_runs_skipped_and_alerts():
    # ACCEPTANCE: partial fill across runs -> whole leg skipped + alert; no double-send.
    b1 = _built("DU1", AS_OF, "BUY", "SPY", 10)
    ib = FakeIB(filled_by_ref={b1.order_ref: 4})
    res = orm.place(ib, [b1], armed=True)
    assert ib.placed == []
    assert res["skipped"][0]["state"] == orm.LegState.PARTIAL
    assert res["skipped"][0]["alert"] is True


def test_journal_attempting_no_sent_alerts_no_retry():
    # ACCEPTANCE: journal ATTEMPTING with no SENT -> alert; no auto-retry.
    b1 = _built("DU1", AS_OF, "BUY", "SPY", 10)
    tj.record_attempting(b1.order_ref, as_of=AS_OF, symbol="SPY", side="BUY", target_qty=10)
    ib = FakeIB()
    res = orm.place(ib, [b1], armed=True)         # gate queries the journal itself here
    assert ib.placed == []
    assert res["skipped"][0]["state"] == orm.LegState.UNKNOWN
    assert res["skipped"][0]["alert"] is True


def test_broker_read_failure_fail_closed_transmit_nothing():
    # ACCEPTANCE: reqAllOpenOrders/reqExecutions fails -> fail closed: transmit nothing + alert.
    b1 = _built("DU1", AS_OF, "BUY", "SPY", 10)
    ib = FakeIB(fail=True)
    res = orm.place(ib, [b1], armed=True)
    assert ib.placed == []
    assert res["skipped"][0]["state"] == orm.LegState.UNKNOWN
    assert res["skipped"][0]["alert"] is True


def test_fresh_cycle_places_normally():
    # ACCEPTANCE: genuinely new cycle -> places normally (not falsely deduped).
    b1 = _built("DU1", AS_OF, "BUY", "SPY", 10)
    b2 = _built("DU1", AS_OF, "BUY", "IEF", 7)
    ib = FakeIB()                                # empty broker, empty journal
    res = orm.place(ib, [b1, b2], armed=True)
    assert len(ib.placed) == 2
    assert res["transmitted"] == 2
    assert res["skipped"] == []


def test_place_dry_run_never_reaches_gate(monkeypatch):
    # When the guard blocks (not armed), the gate must not run — nothing transmitted, and a
    # broker that would raise is never touched.
    b1 = _built("DU1", AS_OF, "BUY", "SPY", 10)
    ib = FakeIB(fail=True)
    res = orm.place(ib, [b1], armed=False)
    assert ib.placed == []
    assert res["transmitted"] == 0


# ============================================================================
# place_laddered() — gate before rung 1
# ============================================================================
def test_laddered_working_skips_before_rung1():
    ref = _ref("DU1", AS_OF, "BUY", "TFLO")
    ib = FakeIB(open_refs=[ref])
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=CAPS,
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        order_ref=ref, armed=True, rung_seconds=1, poll=0)
    assert ib.placed == []
    assert res["skipped"] is True
    assert res["leg_state"] == orm.LegState.WORKING


def test_laddered_complete_skips():
    ref = _ref("DU1", AS_OF, "BUY", "TFLO")
    ib = FakeIB(filled_by_ref={ref: 100})
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=CAPS,
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        order_ref=ref, armed=True, rung_seconds=1, poll=0)
    assert ib.placed == []
    assert res["leg_state"] == orm.LegState.COMPLETE


def test_laddered_fresh_proceeds_to_rung1():
    ref = _ref("DU1", AS_OF, "BUY", "TFLO")
    ib = FakeIB()
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=CAPS,
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        order_ref=ref, armed=True, rung_seconds=1, poll=0)
    assert len(ib.placed) >= 1                    # rung 1 was placed (leg was FRESH)
    assert res.get("skipped") is not True


def test_laddered_broker_failure_fail_closed():
    ref = _ref("DU1", AS_OF, "BUY", "TFLO")
    ib = FakeIB(fail=True)
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=CAPS,
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        order_ref=ref, armed=True, rung_seconds=1, poll=0)
    assert ib.placed == []
    assert res["leg_state"] == orm.LegState.UNKNOWN


def test_laddered_snapshot_ignores_own_attempting():
    # Passing the pre-ATTEMPTING snapshot (None) lets a FRESH leg through even though this
    # run already journaled ATTEMPTING for the ref.
    ref = _ref("DU1", AS_OF, "BUY", "TFLO")
    tj.record_attempting(ref, as_of=AS_OF, symbol="TFLO", side="BUY", target_qty=100)
    ib = FakeIB()
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=CAPS,
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        order_ref=ref, armed=True, rung_seconds=1, poll=0, journal_state=None)
    assert len(ib.placed) >= 1                    # proceeded (own ATTEMPTING ignored)


# ============================================================================
# transmit_journal — append-only round trip
# ============================================================================
def test_journal_round_trip_none_attempting_sent():
    ref = "paperbot:DU1:2026-07-20:BUY:SPY"
    assert tj.state_for(ref) is None
    tj.record_attempting(ref, as_of=AS_OF, symbol="SPY", side="BUY", target_qty=10)
    assert tj.state_for(ref) == tj.ATTEMPTING
    tj.record_sent(ref, filled=10, remaining=0, rested_gtc=False, avg_px=100.0)
    assert tj.state_for(ref) == tj.SENT           # SENT beats a prior ATTEMPTING


def test_journal_day_scoped():
    ref = "paperbot:DU1:2026-07-20:BUY:SPY"
    tj.record_sent(ref, filled=10, day="2026-07-19")   # a DIFFERENT day
    assert tj.state_for(ref, day="2026-07-20") is None  # not visible for today
    assert tj.state_for(ref, day="2026-07-19") == tj.SENT


def test_journal_cycle_complete_marker_written(tmp_path):
    tj.record_cycle_complete(as_of=AS_OF, n_routes=3, n_sent=2, n_skipped=1)
    recs = tj._read_records()
    assert any(r["state"] == tj.CYCLE_COMPLETE and r["n_sent"] == 2 for r in recs)


def test_journal_never_rewrites_history():
    # Append-only: a second write for the same ref adds a line, never mutates the first.
    ref = "paperbot:DU1:2026-07-20:BUY:SPY"
    tj.record_attempting(ref, as_of=AS_OF, symbol="SPY", side="BUY", target_qty=10)
    tj.record_sent(ref, filled=10, remaining=0, rested_gtc=False, avg_px=100.0)
    recs = [r for r in tj._read_records() if r.get("order_ref") == ref]
    assert [r["state"] for r in recs] == [tj.ATTEMPTING, tj.SENT]


# ============================================================================
# morning_execute_run — end-to-end: the gate runs in BOTH pilot and armed modes
# ============================================================================
import json
import os
from datetime import date

import morning_execute_run as mer


class _FixedDate(date):
    @classmethod
    def today(cls):
        return date(2026, 7, 20)


def _write_stage(tmp_path, account="DU8922142", symbol="SPY", side="BUY", qty=10):
    day = "2026-07-20"
    payload = {
        "date": day, "staged_at": f"{day}T21:17:03-05:00", "paperbot_version": "0.17.0",
        "regime": {"confirmed": "GOLDILOCKS", "as_of": day},
        "as_of": {"Conservative": day},
        "routes": [{"route": "direct", "version": "Conservative", "symbol": symbol,
                    "side": side, "total_qty": qty, "fa_group": None, "fa_method": "",
                    "account": account, "per_account_split": {account: qty},
                    "reason": "REBALANCE_TO_MODEL"}],
        "prices_by_symbol": {symbol: 500.0},
    }
    path = os.path.join(str(tmp_path), f"{day}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path, account, day


def _morning_common(monkeypatch, tmp_path, fake):
    monkeypatch.setattr(mer, "date", _FixedDate)
    monkeypatch.setattr(mer, "PENDING_TRADES_DIR", str(tmp_path))
    monkeypatch.setattr(mer, "ARCHIVE_DIR", os.path.join(str(tmp_path), "archive"))
    monkeypatch.setattr(mer, "AUTOTRADE_DISABLED_SENTINEL",
                        os.path.join(str(tmp_path), "AUTOTRADE_DISABLED"))
    monkeypatch.setattr(mer, "_write_status", lambda *a, **k: None)
    # ledger.py resolves its file paths at import time — redirect them to tmp so the audit
    # write never touches real off-Drive state.
    monkeypatch.setattr(mer.ledger, "RUNS_JSONL", os.path.join(str(tmp_path), "runs.jsonl"))
    monkeypatch.setattr(mer.ledger, "LOG_TXT", os.path.join(str(tmp_path), "paperbot.log"))
    monkeypatch.setattr(mer, "bounded_connect", lambda *a, **k: fake)
    monkeypatch.setattr(mer.live_quotes, "fetch", lambda ib, universe: {})
    monkeypatch.setattr(mer.accounts, "discover", lambda ib: [])
    import rebalance_guard as rg
    monkeypatch.setattr(mer.rebalance_guard, "check",
                        lambda routes, ai, prices, claimed_regime=None:
                        rg.GuardResult(passed=True, reasons=[]))
    # Free gateway lock on a tmp path.
    import gateway_lock as gl
    real = gl.gateway_lock
    lock_path = os.path.join(str(tmp_path), "gateway.lock")

    def patched(purpose, client_id, on_busy="refuse", wait_secs=None, **kw):
        kw.setdefault("lock_path", lock_path)
        kw.setdefault("poll_interval", 0.001)
        kw.setdefault("sleep_fn", lambda s: None)
        return real(purpose, client_id, on_busy=on_busy,
                    wait_secs=0.02 if wait_secs is None else wait_secs, **kw)
    monkeypatch.setattr(mer, "gateway_lock", patched)


def test_morning_pilot_rehearses_gate_transmits_nothing(monkeypatch, tmp_path):
    # PILOT_MODE=True: the gate runs as a zero-transmit rehearsal (Q3). Nothing is placed;
    # a per-cycle ledger line + CYCLE_COMPLETE journal marker are written.
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    stage_path, account, day = _write_stage(tmp_path)
    fake = FakeIB()                      # empty broker -> WOULD SEND (FRESH)
    _morning_common(monkeypatch, tmp_path, fake)
    alerts = []
    monkeypatch.setattr(mer, "_alert_email", lambda subj, lines: alerts.append((subj, lines)))

    rc = mer.main()

    assert rc == 0
    assert fake.placed == []             # PILOT: nothing transmitted
    assert not os.path.exists(stage_path)   # archived
    body = "\n".join(l for _, lines in alerts for l in lines)
    assert "WOULD SEND" in body
    assert mer.transmit_journal.state_for("x", day) is None   # (sanity: journal reachable)
    recs = mer.transmit_journal._read_records(day)
    assert any(r["state"] == tj.CYCLE_COMPLETE for r in recs)
    assert os.path.exists(os.path.join(str(tmp_path), "runs.jsonl"))   # ledger line written


def test_morning_armed_fresh_leg_journals_and_places(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(mer, "PILOT_MODE", False)
    stage_path, account, day = _write_stage(tmp_path)
    fake = FakeIB()                      # FRESH leg
    _morning_common(monkeypatch, tmp_path, fake)
    monkeypatch.setattr(mer.arming, "probe_api_readonly", lambda *a, **k: False)  # already armed
    monkeypatch.setattr(mer.rebalance_execute, "backup_fa_groups", lambda ib: "backup.xml")
    monkeypatch.setattr(mer, "_alert_email", lambda *a, **k: None)

    rc = mer.main()

    assert rc == 0
    assert len(fake.placed) == 1         # the FRESH leg transmitted
    ref = orm.order_ref_for_route(mer._StagedIntent(
        {"route": "direct", "version": "Conservative", "symbol": "SPY", "side": "BUY",
         "total_qty": 10, "account": account, "per_account_split": {account: 10}}), day)
    assert mer.transmit_journal.state_for(ref, day) == tj.SENT   # ATTEMPTING then SENT


def test_morning_armed_completed_leg_is_skipped(monkeypatch, tmp_path):
    # ACCEPTANCE (integration): a leg already filled today is skipped end-to-end — 0 placeOrder.
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(mer, "PILOT_MODE", False)
    stage_path, account, day = _write_stage(tmp_path)
    ref = _ref(account, day, "BUY", "SPY")
    fake = FakeIB(filled_by_ref={ref: 10})    # already COMPLETE at the broker
    _morning_common(monkeypatch, tmp_path, fake)
    monkeypatch.setattr(mer.arming, "probe_api_readonly", lambda *a, **k: False)
    monkeypatch.setattr(mer.rebalance_execute, "backup_fa_groups", lambda ib: "backup.xml")
    monkeypatch.setattr(mer, "_alert_email", lambda *a, **k: None)

    rc = mer.main()

    assert rc == 0
    assert fake.placed == []                  # nothing re-transmitted
    assert mer.transmit_journal.state_for(ref, day) is None   # never journaled SENT (skipped)


# ============================================================================
# PER-RUN ORDER-REF STAMP (v0.34.0)
#
# The base ref ends at the model `as_of`, which for a monthly model is effectively a MONTH
# stamp — so a SECOND run of the same leg inside that month was deduped away as "already
# done" (the 2026-07-28 root cause). The ref now carries a per-run stamp. These tests pin
# BOTH halves of the contract: a NEW run is new work; WITHIN a run nothing double-submits.
# ============================================================================
RUN_A = "20260819T090000"
RUN_B = "20260819T143000"


def test_two_runs_same_day_same_leg_get_DIFFERENT_refs():
    # Same account, same as_of, same side, same symbol, same DAY — only the run differs.
    a = orm._order_ref("DU1", AS_OF, "BUY", "SPY", RUN_A)
    b = orm._order_ref("DU1", AS_OF, "BUY", "SPY", RUN_B)
    assert a != b
    # ... and the same for an FA BLOCK ref (keyed on the group, not the account).
    ga = orm._fa_block_ref("tier_growth", AS_OF, "BUY", "SPY", RUN_A)
    gb = orm._fa_block_ref("tier_growth", AS_OF, "BUY", "SPY", RUN_B)
    assert ga != gb


def test_second_run_of_an_already_filled_leg_is_FRESH_and_sends():
    # THE 2026-07-28 REGRESSION TEST. Run A's block filled at the broker. Run B asks for the
    # SAME group/symbol/side on the SAME day: under the old month-stamped ref it read COMPLETE
    # and sent nothing. With the per-run stamp it is FRESH -> it sends.
    ref_a = orm._fa_block_ref("tier_growth", AS_OF, "BUY", "SPY", RUN_A)
    ref_b = orm._fa_block_ref("tier_growth", AS_OF, "BUY", "SPY", RUN_B)
    ib = FakeIB(filled_by_ref={ref_a: 30})
    assert orm.already_present(ib, ref_a, 30) == orm.LegState.COMPLETE   # run A: done
    assert orm.already_present(ib, ref_b, 30) == orm.LegState.FRESH      # run B: NEW WORK

    bo = orm.build_fa_block("SPY", "BUY", 30, 100.0, "tier_growth", "", AS_OF, ib=ib,
                            run_id=RUN_B)
    res = orm.place(ib, [bo], armed=True)
    assert res["transmitted"] == 1
    assert len(ib.placed) == 1


def test_within_one_run_a_duplicate_leg_is_STILL_blocked():
    # THE GUARANTEE THAT MUST SURVIVE. Inside ONE run the stamp is constant, so the ref is
    # constant: a retry / straggler re-price / crash-resume of the SAME leg still gates.
    ref = orm._fa_block_ref("tier_growth", AS_OF, "BUY", "SPY", RUN_A)
    ib = FakeIB(open_refs=[ref])          # run A's own block is already WORKING
    assert orm.already_present(ib, ref, 30) == orm.LegState.WORKING

    bo = orm.build_fa_block("SPY", "BUY", 30, 100.0, "tier_growth", "", AS_OF, ib=ib,
                            run_id=RUN_A)
    assert bo.order_ref == ref            # the re-build derives the SAME ref within the run
    res = orm.place(ib, [bo], armed=True)
    assert res["transmitted"] == 0
    assert ib.placed == []                # nothing double-submitted
    assert res["skipped"][0]["state"] == orm.LegState.WORKING


def test_direct_leg_within_one_run_is_STILL_blocked():
    # Same guarantee on the DIRECT (per-account) path that build() stamps.
    ref = orm._order_ref("DU1", AS_OF, "BUY", "SPY", RUN_A)
    ib = FakeIB(open_refs=[ref])
    built = orm.build([SimpleNamespace(symbol="SPY", side="BUY", quantity=10,
                                       limit_price=100.0)], "DU1", AS_OF, run_id=RUN_A)
    assert built[0].order_ref == ref
    res = orm.place(ib, built, armed=True)
    assert res["transmitted"] == 0
    assert ib.placed == []


def test_sell_and_buy_of_the_same_symbol_in_one_run_do_not_collide():
    # The two-phase cash gate places a SELL block and a BUY block in the SAME run, so they
    # share the run stamp. `side` in the base ref is what must keep them apart.
    sell = orm._fa_block_ref("tier_growth", AS_OF, "SELL", "SPY", RUN_A)
    buy = orm._fa_block_ref("tier_growth", AS_OF, "BUY", "SPY", RUN_A)
    assert sell != buy
    # A WORKING/filled SELL must NOT gate the BUY of the same symbol in the same run.
    ib = FakeIB(open_refs=[sell], filled_by_ref={sell: 10})
    assert orm.already_present(ib, sell, 10) == orm.LegState.WORKING
    assert orm.already_present(ib, buy, 10) == orm.LegState.FRESH

    bo = orm.build_fa_block("SPY", "BUY", 10, 100.0, "tier_growth", "", AS_OF, ib=ib,
                            run_id=RUN_A)
    assert bo.order_ref == buy
    assert orm.place(ib, [bo], armed=True)["transmitted"] == 1


def test_ref_still_traces_back_to_its_run_and_its_inputs():
    # AUDIT: the ref is not opaque — every field that identifies the order is still readable,
    # and the trailing field is the run stamp the ledger also records.
    ref = orm._fa_block_ref("tier_growth", AS_OF, "SELL", "SPY", RUN_A)
    assert ref == f"paperbot:tier_growth:{AS_OF}:SELL:SPY:{RUN_A}"
    assert ref.split(":") == ["paperbot", "tier_growth", AS_OF, "SELL", "SPY", RUN_A]
    assert ref.rsplit(":", 1)[1] == RUN_A          # the run id, joinable to the ledger record
    assert ref.startswith(orm._fa_block_ref("tier_growth", AS_OF, "SELL", "SPY"))


def test_no_run_id_yields_the_UNCHANGED_base_ref():
    # BACK-COMPAT, ON PURPOSE: morning_execute_run keys a DURABLE transmit journal on the ref,
    # where cross-run "already sent today" is the INTENDED behavior. Omitting run_id must
    # therefore leave the historical ref byte-identical.
    assert orm._order_ref("DU1", AS_OF, "BUY", "SPY") == f"paperbot:DU1:{AS_OF}:BUY:SPY"
    assert (orm._fa_block_ref("tier_growth", AS_OF, "BUY", "SPY")
            == f"paperbot:tier_growth:{AS_OF}:BUY:SPY")
    route = SimpleNamespace(route="fa_block", fa_group="tier_growth", side="BUY", symbol="SPY",
                            account=None)
    assert orm.order_ref_for_route(route, AS_OF) == f"paperbot:tier_growth:{AS_OF}:BUY:SPY"
    assert (orm.order_ref_for_route(route, AS_OF, RUN_A)
            == f"paperbot:tier_growth:{AS_OF}:BUY:SPY:{RUN_A}")


def test_order_ref_for_route_matches_what_the_builders_stamp():
    # The gate and the wire MUST key on the same string — with the run stamp too.
    block = SimpleNamespace(route="fa_block", fa_group="tier_growth", side="BUY", symbol="SPY",
                            account=None)
    direct = SimpleNamespace(route="direct", fa_group=None, side="BUY", symbol="SPY",
                             account="DU1")
    bo = orm.build_fa_block("SPY", "BUY", 30, 100.0, "tier_growth", "", AS_OF, run_id=RUN_A)
    assert orm.order_ref_for_route(block, AS_OF, RUN_A) == bo.order_ref
    built = orm.build([SimpleNamespace(symbol="SPY", side="BUY", quantity=10,
                                       limit_price=100.0)], "DU1", AS_OF, run_id=RUN_A)
    assert orm.order_ref_for_route(direct, AS_OF, RUN_A) == built[0].order_ref
