"""
test_safe_execute.py — offline unit tests for the SHARED SAFE EXECUTION ENGINE
(safe_execute.execute_plan), extracted from s0_live_deploy in Phase 2 (conductor #64/#66,
spec docs/PRODUCTION_REBALANCE_CONTROL_PLANE.md §2/§7).

ZERO real transmit. NO broker, NO gateway, NO network. The PREVIEW fake IB raises
AssertionError if any order-placing method is touched; the transmit fake records placed/
cancelled orders but reaches no wire. These tests pin the engine's non-bypassable safety
envelope directly at the primitive (the deploy tests already pin it end-to-end via main()):

  1. PREVIEW builds the expected ordered leg list (sells before buys) and transmits nothing;
     order_router.transmit_guard is BLOCKED under the committed READONLY/DRY_RUN defaults.
  2. ARMED gate is fail-closed — not armed / kill-switch present / wrong account / stale (no)
     quote each -> BLOCKED, nothing placed.
  3. _transmit_phase honors transmit_guard: with READONLY still True it transmits NOTHING.
  4. per-order (>%NLV) and total-buy (>investable) caps each BLOCK, nothing placed.
  5. the two-phase cash-gate HARD invariant: placed buy notional <= realized cash*(1-buffer).
  6. position-based idempotency: an identical WORKING order is NOT double-submitted; a re-fire
     with no working order DOES place (delta-vs-positions is the source of truth).
  7. flip-and-restore-in-finally: config.READONLY/DRY_RUN are restored after an ARMED run.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_safe_execute.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import config
import live_quotes
import order_router
import safe_execute as se
import strategy_target
from connections import gateway_probe

ACCT = "U14438624"
OTHER = "U5721712"


def _per_order_rail_reasons(result):
    """Every blocking reason that came from the PER-ORDER rail — the new plan-relative one
    AND the retired flat 50%-of-NetLiq cap, so a 'this order is permitted' assertion fails
    loudly against either implementation instead of passing vacuously."""
    return [r for r in result.reasons
            if "fat-finger" in r or "never shorts" in r or "of NetLiq" in r]


# --- leak guard (mirrors the deploy suite) ------------------------------------------
@pytest.fixture(autouse=True)
def _no_config_flag_leak():
    prev_dry_run, prev_readonly = config.DRY_RUN, config.READONLY
    assert prev_dry_run is True and prev_readonly is True
    try:
        yield
    finally:
        config.DRY_RUN, config.READONLY = prev_dry_run, prev_readonly


# --- fixtures -----------------------------------------------------------------------
def _target(weights=None, prices=None):
    weights = weights or {"VTI": 0.5, "RSP": 0.3, "USFR": 0.2}
    prices = prices or {"VTI": 250.0, "RSP": 180.0, "USFR": 50.0,
                        "BIL": 91.0, "GDX": 30.0}
    return strategy_target.Target(
        weights=pd.Series(weights), prices=pd.Series(prices),
        as_of=pd.Timestamp("2026-07-28"), price_date=pd.Timestamp("2026-07-28"),
        version="Growth")


def _row(a, t, v):
    return SimpleNamespace(account=a, tag=t, value=v)


def _summary(net_liq="100000", buying_power="100000", total_cash="100000"):
    return [_row(ACCT, "NetLiquidation", net_liq), _row(ACCT, "BuyingPower", buying_power),
            _row(ACCT, "TotalCashValue", total_cash),
            _row(OTHER, "NetLiquidation", "999999"), _row("All", "NetLiquidation", "888")]


def _alien(symbol, shares):
    return SimpleNamespace(symbol=symbol, actual_shares=shares, status="ALIEN")


def _open_order(symbol, side):
    return SimpleNamespace(order=SimpleNamespace(action=side, orderRef="prior-run"),
                           contract=SimpleNamespace(symbol=symbol))


def _line(symbol, actual_shares):
    """A reconcile.Line stand-in — the per-order SELL rail reads `.symbol`/`.actual_shares`
    off the plan's own reconciliation to know how many shares are actually held."""
    return SimpleNamespace(symbol=symbol, actual_shares=float(actual_shares),
                           target_weight=0.0, target_shares=0, actual_weight=0.0,
                           drift_weight=0.0, status="DRIFTED")


def _plan(*, orders=None, alien_lines=None, investable=98_500.0, net_liq=100_000.0,
          lines=None):
    return SimpleNamespace(
        account=ACCT, version="Growth", net_liq=net_liq, reserve=0.0,
        investable=investable, lines=list(lines or []), needs_rebalance=True,
        orders=orders if orders is not None else {"VTI": 10, "USFR": 5, "BIL": -3},
        alien_lines=alien_lines if alien_lines is not None else [])


def _caps(per_order=se.MAX_ORDER_MODEL_MULTIPLE, total_buy_le_investable=True,
          max_total_notional=None):
    return se.ExecutionCaps(per_order_model_multiple=per_order,
                            total_buy_le_investable=total_buy_le_investable,
                            max_total_notional=max_total_notional)


def _request(*, plan, target, armed, conform=True, kill=False, account=ACCT,
             allowed=None, caps=None, net_liq=100_000.0, summary=None):
    return se.ExecutionRequest(
        account=account, strategy_version="Growth", plan=plan, target=target,
        quotes={}, prices=dict(target.prices), allowed_accounts=allowed or [ACCT],
        caps=caps or _caps(), conform=conform, run_id=None, net_liq=net_liq,
        summary=summary if summary is not None else _summary(), armed=armed, kill=kill)


class _NoTxIB:
    """Fake IB that BLOWS UP on any transmit (PREVIEW/blocked proof)."""
    def accountSummary(self): return _summary()
    def positions(self): return []
    def reqAllOpenOrders(self): return []
    def qualifyContracts(self, *a, **k): return list(a)
    def sleep(self, *a, **k): return None
    def placeOrder(self, *a, **k):
        raise AssertionError("placeOrder must never be called")


class _FakeTrade:
    def __init__(self, contract, order, *, fill=True):
        self.contract, self.order = contract, order
        q = float(order.totalQuantity)
        if fill:
            self.orderStatus = SimpleNamespace(status="Filled", filled=q, remaining=0.0,
                avgFillPrice=float(getattr(order, "lmtPrice", 0.0) or 0.0))
            self._done = True
        else:
            self.orderStatus = SimpleNamespace(status="Submitted", filled=0.0, remaining=q,
                                               avgFillPrice=0.0)
            self._done = False
    def isDone(self): return self._done


class _TxFakeIB:
    """Transmit-path fake: records placed/cancelled orders, fills everything except no_fill."""
    def __init__(self, summary_rows, *, open_orders=None, no_fill=None):
        self._summary = summary_rows
        self.open_orders = list(open_orders or [])
        self.no_fill = set(no_fill or [])
        self.placed, self.cancelled = [], []
    def accountSummary(self): return self._summary
    def positions(self): return []
    def reqAllOpenOrders(self): return list(self.open_orders)
    def qualifyContracts(self, *a, **k): return list(a)
    def sleep(self, *a, **k): return None
    def placeOrder(self, contract, order):
        self.placed.append(order)
        return _FakeTrade(contract, order, fill=contract.symbol not in self.no_fill)
    def cancelOrder(self, order): self.cancelled.append(order)


# --- 1. PREVIEW builds ordered legs, transmits nothing -------------------------------
def test_preview_builds_ordered_legs_transmits_nothing():
    plan = _plan(orders={"VTI": 10, "USFR": 5, "BIL": -3},
                 alien_lines=[_alien("GDX", 100)])
    req = _request(plan=plan, target=_target(), armed=False, conform=True)
    res = se.execute_plan(req, mode=se.MODE_PREVIEW, ib=_NoTxIB())

    assert res.status == se.STATUS_PREVIEW_ONLY
    assert res.rc == 0
    # sells (BIL, GDX-liquidation) sequenced before buys (VTI, USFR)
    sides = [l.side for l in res.legs]
    last_sell = max(i for i, s in enumerate(sides) if s == "SELL")
    first_buy = min(i for i, s in enumerate(sides) if s == "BUY")
    assert last_sell < first_buy
    assert any(l.symbol == "GDX" and l.source == "alien_liquidation" for l in res.legs)
    # committed guard is closed under the desk defaults -> nothing could transmit anyway
    permit, _why = order_router.transmit_guard(armed=True)
    assert permit is False
    assert config.DRY_RUN is True and config.READONLY is True


# --- 2. ARMED gate fail-closed: not armed -------------------------------------------
def test_armed_but_not_armed_flag_blocks():
    ib = _TxFakeIB(_summary())
    req = _request(plan=_plan(orders={"VTI": 10}), target=_target(), armed=False)
    res = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)
    assert res.status == se.STATUS_BLOCKED
    assert ib.placed == []
    assert any("not armed" in r for r in res.reasons)


# --- 2b. kill switch present blocks -------------------------------------------------
def test_kill_switch_blocks():
    ib = _TxFakeIB(_summary())
    req = _request(plan=_plan(orders={"VTI": 10}), target=_target(), armed=True, kill=True)
    res = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)
    assert res.status == se.STATUS_BLOCKED
    assert ib.placed == []
    assert any("KILL_SWITCH" in r for r in res.reasons)


# --- 2c. wrong account blocks -------------------------------------------------------
def test_wrong_account_blocks():
    ib = _TxFakeIB(_summary())
    req = _request(plan=_plan(orders={"VTI": 10}), target=_target(), armed=True,
                   account=OTHER, allowed=[ACCT])
    res = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)
    assert res.status == se.STATUS_BLOCKED
    assert ib.placed == []
    assert any(f"target account {OTHER} is not the single allowed account {ACCT}" in r
               for r in res.reasons)


# --- 2d. stale / missing quote (unpriceable) blocks ---------------------------------
def test_unpriceable_leg_blocks(monkeypatch):
    monkeypatch.setattr(se, "_probe_gateway_readonly", lambda ib, **k: False)
    # No quote AND no reference price for VTI -> the leg is unpriceable.
    tgt = strategy_target.Target(
        weights=pd.Series({"VTI": 1.0}), prices=pd.Series({"VTI": float("nan")}),
        as_of=pd.Timestamp("2026-07-28"), price_date=pd.Timestamp("2026-07-28"),
        version="Growth")
    req = _request(plan=_plan(orders={"VTI": 10}, alien_lines=[]), target=tgt, armed=True)
    req.prices = {"VTI": float("nan")}
    ib = _TxFakeIB(_summary())
    res = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)
    assert res.status == se.STATUS_BLOCKED
    assert ib.placed == []
    assert res.unpriceable and any("no usable price" in r for r in res.reasons)


# --- 3. _transmit_phase honors transmit_guard (READONLY still True -> nothing) --------
def test_transmit_phase_fails_closed_under_readonly():
    # config.READONLY is True (the committed default; NOT flipped here) -> guard blocks.
    leg = SimpleNamespace(symbol="VTI", side="BUY", qty=10, limit=250.0, notional=2500.0,
                          source="plan")
    ib = _TxFakeIB(_summary())
    results = se._transmit_phase(ib, [leg], account=ACCT, as_of=pd.Timestamp("2026-07-28"),
                                 run_id="R", phase_label="BUY", quotes={}, prices={"VTI": 250.0})
    assert ib.placed == []
    assert results and results[0]["status"] == "BLOCKED" and results[0]["skipped"] is True


# --- 4. rails: per-order fat-finger, and total buy > investable ---------------------
def test_per_order_buy_over_model_multiple_blocks():
    # Model wants 10% of NetLiq in VTI ($10,000 of $100,000); the leg is 200 x $250 =
    # $50,000 — 5x the model's own target, i.e. a fat-finger. Under the investable cap
    # ($98,500) so nothing else can mask the rail.
    tgt = _target(weights={"VTI": 0.1}, prices={"VTI": 250.0})
    req = _request(plan=_plan(orders={"VTI": 200}, alien_lines=[]), target=tgt, armed=True)
    ib = _TxFakeIB(_summary())
    res = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)
    assert res.status == se.STATUS_BLOCKED
    assert ib.placed == []
    assert any("fat-finger" in r and "BUY VTI" in r for r in res.reasons)


def test_never_invested_first_deploy_is_permitted():
    """THE U5721712 CASE (2026-08-25). A never-invested cash account's FIRST deploy buys the
    model's dominant leg at its full model weight — 85% of NetLiq by construction for
    Growth (Small) = SCHB 85% + USFR 15%. The old flat 50%-of-NetLiq cap blocked it
    ('BUY SCHB x27 notional 800.55 > 50% of NetLiq (478.55)'); the model-relative rail must
    not. Rail-only assertion: 'not armed' is still a reason on a preview."""
    tgt = _target(weights={"SCHB": 0.85, "USFR": 0.15},
                  prices={"SCHB": 29.65, "USFR": 50.0})
    plan = _plan(orders={"SCHB": 27, "USFR": 2}, alien_lines=[],
                 investable=957.10, net_liq=957.10)
    req = _request(plan=plan, target=tgt, armed=False, net_liq=957.10)
    res = se.execute_plan(req, mode=se.MODE_PREVIEW)
    assert any(l.symbol == "SCHB" and l.side == "BUY" and l.qty == 27 for l in res.legs)
    assert _per_order_rail_reasons(res) == [], res.reasons


def test_sell_over_shares_held_blocks():
    # The account holds 40 BIL; a corrupted plan asks to sell 400. This desk never shorts.
    tgt = _target(weights={"VTI": 1.0}, prices={"VTI": 250.0, "BIL": 91.0})
    plan = _plan(orders={"BIL": -400}, alien_lines=[],
                 lines=[_line("BIL", 40)])
    req = _request(plan=plan, target=tgt, armed=True)
    ib = _TxFakeIB(_summary())
    res = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)
    assert res.status == se.STATUS_BLOCKED
    assert ib.placed == []
    assert any("never shorts" in r and "SELL BIL" in r for r in res.reasons)


def test_ongoing_rebalance_sell_down_and_buy_up_still_permitted():
    """An ongoing REBALANCE that trims a big overweight and tops up another holding must be
    unaffected: the sell is within the shares held and the buy is within the model's target.
    Under the OLD %NLV cap the $60,000 GDX sell-down (60% of a $100k account) was a block."""
    tgt = _target(weights={"VTI": 0.6, "USFR": 0.4},
                  prices={"VTI": 250.0, "USFR": 50.0, "GDX": 30.0})
    plan = _plan(orders={"GDX": -2000, "VTI": 200}, alien_lines=[],
                 lines=[_line("GDX", 2000), _line("VTI", 40)])
    req = _request(plan=plan, target=tgt, armed=False, conform=False)
    res = se.execute_plan(req, mode=se.MODE_PREVIEW)
    assert {(l.side, l.symbol, l.qty) for l in res.legs} == {
        ("SELL", "GDX", 2000), ("BUY", "VTI", 200)}
    assert _per_order_rail_reasons(res) == [], res.reasons


def test_total_buy_over_investable_blocks():
    tgt = _target(weights={"VTI": 0.5, "RSP": 0.5}, prices={"VTI": 250.0, "RSP": 181.8})
    req = _request(plan=_plan(orders={"VTI": 32, "RSP": 44}, alien_lines=[],
                              investable=10_000.0), target=tgt, armed=True)
    ib = _TxFakeIB(_summary())
    res = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)
    assert res.status == se.STATUS_BLOCKED
    assert ib.placed == []
    assert any("over-deploy" in r for r in res.reasons)


# --- 5. two-phase cash-gate HARD invariant: buy notional <= realized cash*(1-buffer) --
def test_two_phase_cash_gate_hard_invariant(monkeypatch):
    monkeypatch.setattr(se, "_probe_gateway_readonly", lambda ib, **k: False)
    # Plan wants BUY VTI x40 = $10,000, but only $4,000 of realized cash lands.
    tgt = _target(weights={"VTI": 1.0}, prices={"VTI": 250.0})
    req = _request(plan=_plan(orders={"VTI": 40}, alien_lines=[]), target=tgt, armed=True,
                   summary=_summary(total_cash="4000"))
    ib = _TxFakeIB(_summary(total_cash="4000"))
    res = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)

    assert res.status == se.STATUS_COMPLETE
    buys = [o for o in ib.placed if o.action == "BUY"]
    assert buys, "a scaled-down buy should still transmit"
    total = sum(float(o.totalQuantity) * float(o.lmtPrice) for o in buys)
    budget = 4000.0 * (1.0 - se.CASH_SAFETY_BUFFER_PCT)
    assert total <= budget                                   # NEVER exceeds realized cash
    assert all(float(o.totalQuantity) < 40 for o in buys)    # reduced from the plan's 40
    # every order pinned to the single account, LIMIT, per-run deploy ref
    for o in ib.placed:
        assert o.account == ACCT
        assert o.orderType == "LMT"
        assert ":deploy:" in o.orderRef
    # flip-and-restore-in-finally
    assert config.DRY_RUN is True and config.READONLY is True


# --- 6. position-based idempotency (via _transmit_phase) ----------------------------
def test_working_order_not_double_submitted(monkeypatch):
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    leg = SimpleNamespace(symbol="VTI", side="BUY", qty=10, limit=250.0, notional=2500.0,
                          source="plan")
    ib = _TxFakeIB(_summary(), open_orders=[_open_order("VTI", "BUY")])
    results = se._transmit_phase(ib, [leg], account=ACCT, as_of=pd.Timestamp("2026-07-28"),
                                 run_id="R", phase_label="BUY", quotes={}, prices={"VTI": 250.0})
    assert results[0]["skipped"] is True
    assert ib.placed == []                                   # never double-submitted


def test_refire_places_when_no_working_order(monkeypatch):
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    leg = SimpleNamespace(symbol="SPY", side="BUY", qty=44, limit=500.0, notional=22000.0,
                          source="plan")
    ib = _TxFakeIB(_summary(), open_orders=[])
    results = se._transmit_phase(ib, [leg], account=ACCT, as_of=pd.Timestamp("2026-07-01"),
                                 run_id="20260728T120000", phase_label="BUY", quotes={},
                                 prices={"SPY": 500.0})
    assert results[0]["skipped"] is False and results[0]["filled"] == 44.0
    assert len(ib.placed) == 1
    assert ib.placed[0].orderRef.endswith("20260728T120000")


# --- 7. execute_plan ARMED clean path transmits the two-phase deploy -----------------
def test_armed_clean_path_transmits(monkeypatch):
    monkeypatch.setattr(se, "_probe_gateway_readonly", lambda ib, **k: False)
    req = _request(plan=_plan(orders={"VTI": 10}, alien_lines=[]), target=_target(),
                   armed=True, summary=_summary(total_cash="60000"))
    ib = _TxFakeIB(_summary(total_cash="60000"))
    res = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)
    assert res.status == se.STATUS_COMPLETE
    assert any(o.action == "BUY" and float(o.totalQuantity) == 10.0 for o in ib.placed)
    assert config.DRY_RUN is True and config.READONLY is True


# --- 8. invalid mode is rejected ----------------------------------------------------
def test_invalid_mode_raises():
    req = _request(plan=_plan(orders={"VTI": 10}), target=_target(), armed=False)
    with pytest.raises(ValueError):
        se.execute_plan(req, mode="AUTO", ib=_NoTxIB())


# --- 9. armed_session — the ONE arm gate (spec §2.2, conductor #64 Step 1) -----------
def test_armed_session_flips_both_flags_and_restores_on_normal_exit(monkeypatch):
    # Prior committed defaults (both True) -> flipped False inside -> restored True on exit.
    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "DRY_RUN", True)
    with se.armed_session(purpose="t", client_id=None) as sess:
        assert config.READONLY is False and config.DRY_RUN is False
        assert sess.fa_backup_path == ""                 # no backup taken on the deploy path
    assert config.READONLY is True and config.DRY_RUN is True


def test_armed_session_restores_to_prior_values_not_hardcoded_true(monkeypatch):
    # Restores the CAPTURED prior values, not a hardcoded True: prove with a mixed prior.
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", True)
    with se.armed_session(purpose="t", client_id=None):
        assert config.READONLY is False and config.DRY_RUN is False
    assert config.READONLY is False and config.DRY_RUN is True


def test_armed_session_restores_both_flags_when_body_raises(monkeypatch):
    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "DRY_RUN", True)
    with pytest.raises(RuntimeError):
        with se.armed_session(purpose="t", client_id=None):
            assert config.READONLY is False and config.DRY_RUN is False
            raise RuntimeError("boom")
    # the enablement can NEVER outlive the block, even on an exception
    assert config.READONLY is True and config.DRY_RUN is True


def test_armed_session_fa_backup_seam_is_step2(monkeypatch):
    # Step 1: the FA-backup helper is only a seam; the deploy path never calls it.
    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "DRY_RUN", True)
    with se.armed_session(purpose="t", client_id=None) as sess:
        with pytest.raises(NotImplementedError):
            sess.backup_fa_groups(object())


def test_armed_session_acquires_and_releases_gateway_lock(monkeypatch, tmp_path):
    # With gateway_lock_on_busy set and the lock FREE: it acquires for the whole block and
    # releases on exit. Redirect the lock file to a temp path (real _GatewayLock logic).
    import gateway_lock as gl
    lock_file = tmp_path / "gateway.lock"
    real = gl.gateway_lock
    monkeypatch.setattr(gl, "gateway_lock",
                        lambda **kw: real(lock_path=str(lock_file), **kw))
    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "DRY_RUN", True)
    with se.armed_session(purpose="t", client_id=7, gateway_lock_on_busy="refuse"):
        assert lock_file.exists()                        # held for the whole block
        assert config.READONLY is False and config.DRY_RUN is False
    assert not lock_file.exists()                        # released on exit
    assert config.READONLY is True and config.DRY_RUN is True


def test_armed_session_busy_refuse_propagates_and_never_flips_flags(monkeypatch, tmp_path):
    # A LIVE, non-stale holder already owns the lock -> GatewayBusyRefuse propagates AND the
    # flags are never flipped (the lock is acquired BEFORE the flip; on refuse the flip is
    # never reached and the finally leaves the priors intact).
    import json
    import os
    import time as _time
    import gateway_lock as gl
    lock_file = tmp_path / "gateway.lock"
    lock_file.write_text(json.dumps({
        "pid": os.getpid(), "client_id": 99, "purpose": "other-holder",
        "acquired_ts": _time.time(), "heartbeat_ts": _time.time()}))
    real = gl.gateway_lock
    monkeypatch.setattr(gl, "gateway_lock",
                        lambda **kw: real(lock_path=str(lock_file), wait_secs=0.05,
                                          poll_interval=0.01, **kw))
    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "DRY_RUN", True)
    with pytest.raises(gl.GatewayBusyRefuse):
        with se.armed_session(purpose="t", client_id=7, gateway_lock_on_busy="refuse"):
            pass  # unreachable — the lock refuses before the body runs
    assert config.READONLY is True and config.DRY_RUN is True


# --- 10. the ONE shared gateway read-only probe (connections.gateway_probe) ----------
# The consolidated, port-parameterized, zero-transmission probe that both arming (4002) and
# safe_execute (4003) now delegate to. A fake IB fires ONE error code back through errorEvent
# when the fabricated orderId is "cancelled" (nothing ever transmits), or fires none to prove
# the fail-closed / raise-on-indeterminate paths.
class _ProbeFakeIB:
    """Minimal fake IB for the shared probe: errorEvent supports +=/-=, and cancelOrder
    synchronously invokes every registered handler with `reply` (errorCode, errorString).
    reply=None -> no signal (the indeterminate/timeout path)."""
    def __init__(self, reply):
        self._reply = reply
        self._handlers = []
        self.cancelled = []
        self.client = SimpleNamespace(
            getReqId=lambda: 9999,
            cancelOrder=self._cancel)
        self.errorEvent = self  # so `ib.errorEvent += h` reaches __iadd__ below

    # errorEvent += / -= handler
    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def __isub__(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
        return self

    def _cancel(self, oid, manualCancelTime):
        self.cancelled.append(oid)
        if self._reply is not None:
            code, text = self._reply
            for h in list(self._handlers):
                h(oid, code, text)

    def sleep(self, *a, **k):
        return None


def test_shared_probe_readonly_code_321_returns_true():
    ib = _ProbeFakeIB((321, "The API interface is currently in Read-Only mode."))
    assert gateway_probe.probe_api_readonly(ib, port=4003) is True
    assert ib.cancelled == [9999]              # a fabricated id was cancelled; nothing rested


def test_shared_probe_readonly_by_message_returns_true():
    # No 321, but the message names read-only mode -> still read-only (blocked).
    ib = _ProbeFakeIB((0, "Order rejected — read only mode"))
    assert gateway_probe.probe_api_readonly(ib, port=4002) is True


def test_shared_probe_write_enabled_10147_returns_false():
    ib = _ProbeFakeIB((10147, "OrderId 9999 that needs to be cancelled is not found."))
    assert gateway_probe.probe_api_readonly(ib, port=4003) is False


def test_shared_probe_write_enabled_10148_or_message_returns_false():
    ib = _ProbeFakeIB((10148, "OrderId 9999 that needs to be cancelled can not be cancelled."))
    assert gateway_probe.probe_api_readonly(ib, port=4003) is False


def test_shared_probe_no_signal_fails_closed_to_true():
    # No error ever comes back within the timeout -> FAIL CLOSED to read-only (refuse).
    ib = _ProbeFakeIB(None)
    assert gateway_probe.probe_api_readonly(ib, port=4003, timeout=0) is True


def test_shared_probe_no_signal_raises_when_raise_on_indeterminate():
    # The arm/disarm verify path: no decisive signal must fail LOUDLY, not silently "locked".
    ib = _ProbeFakeIB(None)
    with pytest.raises(RuntimeError):
        gateway_probe.probe_api_readonly(ib, port=4002, timeout=0,
                                         raise_on_indeterminate=True)


def test_shared_probe_detaches_its_error_handler():
    # The handler is added for the probe and removed after (no leak onto the caller's ib).
    ib = _ProbeFakeIB((10147, "not found"))
    gateway_probe.probe_api_readonly(ib, port=4003)
    assert ib._handlers == []


# ====================================================================================
# 11. DEPLOY-vs-REBALANCE purpose (#…) + self-computed per-account margin pre-flight (#57)
# ====================================================================================
def _rebalance_request(*, plan, target, armed, kill=False, account=ACCT, allowed=None,
                       caps=None, net_liq=100_000.0, summary=None):
    """A REBALANCE-purpose request (conform=False by nature — the ongoing lane)."""
    return se.ExecutionRequest(
        account=account, strategy_version="Growth", plan=plan, target=target,
        quotes={}, prices=dict(target.prices), allowed_accounts=allowed or [ACCT],
        caps=caps or _caps(), conform=False, run_id=None, net_liq=net_liq,
        summary=summary if summary is not None else _summary(), armed=armed, kill=kill,
        purpose=se.PURPOSE_REBALANCE)


def _typed_summary(rows):
    """Build accountSummary rows for arbitrary (tag, value) pairs on the target account."""
    return [_row(ACCT, tag, val) for tag, val in rows]


# --- DEPLOY parity: purpose defaults to DEPLOY; conform=False still blocks identically ---
def test_deploy_default_purpose_conform_false_still_blocks_conform_reason():
    req = _request(plan=_plan(orders={"VTI": 10}, alien_lines=[]), target=_target(),
                   armed=True, conform=False)
    assert req.purpose == se.PURPOSE_DEPLOY                 # default — byte-identical intent
    ib = _TxFakeIB(_summary())
    res = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)
    assert res.status == se.STATUS_BLOCKED                  # blocked exactly as before
    assert ib.placed == []
    assert any("conform intent absent" in r for r in res.reasons)


def test_deploy_conform_true_preview_no_conform_reason_and_legs_unchanged():
    plan = _plan(orders={"VTI": 10, "USFR": 5, "BIL": -3}, alien_lines=[_alien("GDX", 100)])
    req = _request(plan=plan, target=_target(), armed=False, conform=True)
    res = se.execute_plan(req, mode=se.MODE_PREVIEW, ib=_NoTxIB())
    assert res.status == se.STATUS_PREVIEW_ONLY
    assert not any("conform intent absent" in r for r in res.reasons)
    # Same ordered legs as the pre-change build (sells first incl. alien liquidation, then buys).
    assert any(l.symbol == "GDX" and l.source == "alien_liquidation" for l in res.legs)
    legs = {(l.symbol, l.side, l.qty) for l in res.legs}
    assert legs == {("BIL", "SELL", 3), ("GDX", "SELL", 100),
                    ("VTI", "BUY", 10), ("USFR", "BUY", 5)}


# --- REBALANCE lane: no conform reason; preview transmits nothing; armed clean transmits ---
def test_rebalance_preview_no_conform_reason_transmits_nothing():
    plan = _plan(orders={"VTI": 10, "BIL": -3}, alien_lines=[_alien("GDX", 100)])
    req = _rebalance_request(plan=plan, target=_target(), armed=False)
    res = se.execute_plan(req, mode=se.MODE_PREVIEW, ib=_NoTxIB())
    assert res.status == se.STATUS_PREVIEW_ONLY
    assert res.sell_results == [] and res.buy_results == []
    assert not any("conform intent absent" in r for r in res.reasons)   # rebalance needs no conform
    assert any("not armed" in r for r in res.reasons)                   # still preview-blocked
    # conform=False -> alien GDX is NOT liquidated (no GDX leg); it's reported in aliens_left.
    assert not any(l.symbol == "GDX" for l in res.legs)
    assert any(getattr(ln, "symbol", None) == "GDX" for ln in res.aliens_left)


def test_rebalance_armed_clean_path_permit_true_transmits(monkeypatch):
    monkeypatch.setattr(se, "_probe_gateway_readonly", lambda ib, **k: False)
    req = _rebalance_request(plan=_plan(orders={"VTI": 10}, alien_lines=[]),
                             target=_target(), armed=True,
                             summary=_summary(total_cash="60000"))
    ib = _TxFakeIB(_summary(total_cash="60000"))
    res = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)
    assert res.status == se.STATUS_COMPLETE
    assert res.reasons == []                                # every gate clean -> permit was True
    assert any(o.action == "BUY" and float(o.totalQuantity) == 10.0 for o in ib.placed)
    assert config.DRY_RUN is True and config.READONLY is True   # flip-and-restore held


# --- margin gate: TRUST unlevered allows (ZERO reasons); levered CASH refuses ---
def test_margin_preflight_ok_trust_unlevered_allows_zero_reasons():
    # The live trust shape: AccountType=TRUST, BuyingPower > NetLiq. Unlevered exposure ~0.985.
    trust = _typed_summary([("AccountType", "TRUST"), ("NetLiquidation", "117000"),
                            ("BuyingPower", "378000"), ("TotalCashValue", "117000"),
                            ("ExcessLiquidity", "90000")])
    plan = _plan(orders={"VTI": 10}, investable=98_500.0, net_liq=100_000.0)
    ok, reason = se._margin_preflight_ok(trust, 100_000.0, 2_500.0, plan, _target())
    assert ok is True and reason == ""


def test_margin_preflight_refuses_levered_on_cash_account():
    cash = _typed_summary([("AccountType", "CASH"), ("NetLiquidation", "100000"),
                           ("BuyingPower", "100000"), ("ExcessLiquidity", "0")])
    plan = _plan(orders={"VTI": 10}, investable=150_000.0, net_liq=100_000.0)   # 1.5x exposure
    ok, reason = se._margin_preflight_ok(cash, 100_000.0, 2_500.0, plan, _target())
    assert ok is False
    assert "margin pre-flight REFUSED" in reason
    assert "1.5000x" in reason                              # the intended levered exposure


def test_margin_preflight_ok_unlevered_on_empty_summary_allows():
    # Fail-open on a thin/unreadable summary for the UNLEVERED path (matches _buying_power_ok).
    plan = _plan(orders={"VTI": 10}, investable=98_500.0, net_liq=100_000.0)
    ok, reason = se._margin_preflight_ok([], 100_000.0, 2_500.0, plan, _target())
    assert ok is True and reason == ""


# --- gate only tightens: it ADDS a reason for a levered armed run, and NEVER removes one ---
def test_margin_gate_blocks_levered_armed_rebalance(monkeypatch):
    # Armed, all code-gates clean, gateway write-enabled, but a genuinely levered plan on a CASH
    # account -> the margin gate ADDS a refusal and the run is BLOCKED (equal-or-stricter).
    monkeypatch.setattr(se, "_probe_gateway_readonly", lambda ib, **k: False)
    cash = _typed_summary([("AccountType", "CASH"), ("NetLiquidation", "100000"),
                           ("BuyingPower", "100000"), ("TotalCashValue", "100000"),
                           ("ExcessLiquidity", "0")])
    plan = _plan(orders={"VTI": 10}, alien_lines=[], investable=150_000.0, net_liq=100_000.0)
    req = _rebalance_request(plan=plan, target=_target(weights={"VTI": 1.0},
                                                       prices={"VTI": 250.0}),
                             armed=True, summary=cash)
    ib = _TxFakeIB(cash)
    res = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)
    assert res.status == se.STATUS_BLOCKED
    assert ib.placed == []
    assert any("margin pre-flight REFUSED" in r for r in res.reasons)


def test_margin_gate_never_loosens_already_blocked_run():
    # A run already blocked (not armed) with a levered plan stays BLOCKED — the margin gate is
    # guarded by `not reasons` and so can only ever ADD, never clear, a blocking reason.
    plan = _plan(orders={"VTI": 10}, alien_lines=[], investable=150_000.0, net_liq=100_000.0)
    req = _rebalance_request(plan=plan, target=_target(), armed=False)
    res = se.execute_plan(req, mode=se.MODE_PREVIEW, ib=_NoTxIB())
    assert res.status == se.STATUS_PREVIEW_ONLY
    assert any("not armed" in r for r in res.reasons)      # pre-existing block persists


def test_invalid_purpose_raises():
    req = _request(plan=_plan(orders={"VTI": 10}), target=_target(), armed=False)
    req.purpose = "SOMETHING_ELSE"
    with pytest.raises(ValueError):
        se.execute_plan(req, mode=se.MODE_PREVIEW, ib=_NoTxIB())


# --- 12. THE TWO PRICE BASES — plan basis vs worst-case-at-the-cap (v0.44.0) ---------
# The investable gate used to compare a CAP-priced buy total (qty x ask*(1+ORDER_CAP_K))
# against a plan.investable computed on the REFERENCE-price basis the engine sized on. Two
# different bases; on 2026-09-01 the mismatch refused three of eight custom-model accounts
# — $1,194,383 of $1,471,610, 81% of the deployment, including the largest account — for an
# overspend that cannot happen (the account pays the market, never the cap).
def _quote(symbol, bid, ask):
    return live_quotes.Quote(symbol=symbol, bid=bid, ask=ask, last=ask, close=ask, md_type=1)


# U25274773 as measured live on the 4003 gateway, 2026-09-01 (after-hours, ~51bps spread):
#   investable        818,504.60
#   buys at the caps  822,722.51   <- what HEAD compared, and blocked on
#   buys at plan px   818,478.28   <- what the plan actually intends to spend (fits, by 26.32)
_U25274773_INVESTABLE = 818_504.60
_U25274773_NET_LIQ = 826_772.32
_U25274773_CAP_TOTAL = 822_722.51
_U25274773_PLAN_TOTAL = 818_478.28


def _u25274773_request(armed=False, summary=None):
    """The real account's shape: every BUY leg quoted with an ask ~51bps above the reference
    price the engine sized on, so cap notional > plan notional on every leg."""
    tgt = _target(weights={"SCHB": 0.755, "USFR": 0.244, "VTI": 0.001},
                  prices={"SCHB": 61.79, "USFR": 49.94, "VTI": 272.76})
    plan = _plan(orders={"SCHB": 10_000, "USFR": 4_000, "VTI": 3}, alien_lines=[],
                 investable=_U25274773_INVESTABLE, net_liq=_U25274773_NET_LIQ)
    req = _request(plan=plan, target=tgt, armed=armed, net_liq=_U25274773_NET_LIQ,
                   summary=summary)
    req.quotes = {"SCHB": _quote("SCHB", 61.90, 61.92),
                  "USFR": _quote("USFR", 50.03, 50.05),
                  "VTI": _quote("VTI", 273.30, 273.35)}
    return req


def _overdeploy_reasons(result):
    return [r for r in result.reasons if "over-deploy" in r]


def test_u25274773_two_bases_fixture_is_the_measured_arithmetic():
    """Pin the fixture itself: the cap-priced total is the blocked figure measured live, the
    plan-priced total fits inside investable. If this drifts, the tests below prove nothing."""
    req = _u25274773_request()
    legs, _aliens, unpriceable = se.build_deploy_legs(req.plan, req.quotes, req.prices,
                                                      conform=True)
    assert unpriceable == []
    cap_total = sum(l.notional for l in legs if l.side == "BUY")
    plan_total = sum(se._leg_plan_notional(l, req.prices) for l in legs if l.side == "BUY")
    assert round(cap_total, 2) == _U25274773_CAP_TOTAL
    assert round(plan_total, 2) == _U25274773_PLAN_TOTAL
    assert cap_total > _U25274773_INVESTABLE      # HEAD's comparison -> block
    assert plan_total < _U25274773_INVESTABLE     # the plan actually fits


def test_u25274773_blocks_under_heads_comparison_and_clears_under_the_fix(monkeypatch):
    """FAIL-BEFORE / PASS-AFTER on the SAME fixture.

    HEAD's comparison is restored by making the plan-basis notional return the leg's own
    cap-priced notional — which collapses total_buy_plan back to total_buy, byte-identical to
    the pre-fix predicate. It blocks. Undo it and the real account is no longer refused."""
    # --- HEAD's comparison restored -> the account is refused -----------------------
    monkeypatch.setattr(se, "_leg_plan_notional", lambda l, prices: float(l.notional))
    res_head = se.execute_plan(_u25274773_request(), mode=se.MODE_PREVIEW, ib=_NoTxIB())
    blocked = _overdeploy_reasons(res_head)
    assert len(blocked) == 1, res_head.reasons
    assert f"{_U25274773_CAP_TOTAL:,.2f}" in blocked[0]

    # --- the fix -> no over-deploy reason; 'not armed' is the ONLY thing left --------
    monkeypatch.undo()
    res_fix = se.execute_plan(_u25274773_request(), mode=se.MODE_PREVIEW, ib=_NoTxIB())
    assert _overdeploy_reasons(res_fix) == [], res_fix.reasons
    assert _per_order_rail_reasons(res_fix) == [], res_fix.reasons
    assert [r for r in res_fix.reasons if "not armed" not in r] == [], res_fix.reasons


def test_genuinely_over_deployed_plan_still_blocks_and_reports_both_figures():
    """The gate is loosened to the right basis, NOT removed: a plan whose PLAN-BASIS spend
    exceeds investable is still refused, and the reason carries BOTH numbers."""
    tgt = _target(weights={"SCHB": 1.0}, prices={"SCHB": 61.79})
    # 14,000 x 61.79 = 865,060.00 at plan prices — well over the 818,504.60 investable.
    plan = _plan(orders={"SCHB": 14_000}, alien_lines=[],
                 investable=_U25274773_INVESTABLE, net_liq=_U25274773_NET_LIQ)
    req = _request(plan=plan, target=tgt, armed=False, net_liq=_U25274773_NET_LIQ)
    req.quotes = {"SCHB": _quote("SCHB", 61.90, 61.92)}       # cap 62.11 -> 869,540.00
    res = se.execute_plan(req, mode=se.MODE_PREVIEW, ib=_NoTxIB())
    blocked = _overdeploy_reasons(res)
    assert len(blocked) == 1, res.reasons
    assert "total BUY 865,060.00 at plan prices > investable 818,504.60" in blocked[0]
    assert "worst case at the marketable caps: 869,540.00" in blocked[0]


def test_buy_leg_missing_from_prices_falls_back_to_the_cap_priced_notional():
    """FAIL CLOSED per leg. A BUY whose symbol has no usable price in `prices` is counted at
    its own cap-priced notional — strictly the larger figure — so an unreadable price makes
    the gate STRICTER, never looser. Constructed so the fallback FLIPS the verdict: with it
    the plan basis is 100,020.00 (blocked); with that leg priced for real it is 99,900.00.

    The leg is still fully PRICEABLE (its cap comes from the live quote), so this is the
    price-basis fallback firing on its own, not the unpriceable rail."""
    # XYZ is in the model's weights but absent from `prices` (which _request takes from
    # target.prices) — it is priced only by its quote.
    tgt = _target(weights={"SCHB": 0.6, "XYZ": 0.4}, prices={"SCHB": 60.00})
    plan = _plan(orders={"SCHB": 1_000, "XYZ": 1_000}, alien_lines=[],
                 investable=100_000.0, net_liq=101_010.0)
    req = _request(plan=plan, target=tgt, armed=False, net_liq=101_010.0)
    req.quotes = {"SCHB": _quote("SCHB", 59.98, 60.00),       # cap 60.18
                  "XYZ": _quote("XYZ", 39.88, 39.90)}         # cap 40.02
    assert "XYZ" not in req.prices                            # the precondition under test

    res = se.execute_plan(req, mode=se.MODE_PREVIEW, ib=_NoTxIB())
    assert [r for r in res.reasons if "no usable price" in r] == [], res.reasons
    blocked = _overdeploy_reasons(res)
    assert len(blocked) == 1, res.reasons
    # SCHB at its plan price (60,000.00) + XYZ fallen back to its CAP notional (40,020.00).
    assert "total BUY 100,020.00 at plan prices > investable 100,000.00" in blocked[0]

    # Prove the fallback is what tipped it: give XYZ a real price and the account clears.
    req2 = _request(plan=plan, target=_target(weights={"SCHB": 0.6, "XYZ": 0.4},
                                              prices={"SCHB": 60.00, "XYZ": 39.90}),
                    armed=False, net_liq=101_010.0)
    req2.quotes = dict(req.quotes)
    res2 = se.execute_plan(req2, mode=se.MODE_PREVIEW, ib=_NoTxIB())
    assert _overdeploy_reasons(res2) == [], res2.reasons     # 60,000 + 39,900 = 99,900


def test_buying_power_still_refuses_against_the_worst_case_total(monkeypatch):
    """The investable gate moved to the plan basis; the BUYING-POWER gate did NOT. Whether the
    broker will PERMIT the order is genuinely a worst-case question, so it stays on the
    cap-priced total. BuyingPower is set BETWEEN the two totals: it must still refuse."""
    monkeypatch.setattr(se, "_probe_gateway_readonly", lambda ib, **k: False)
    bp = 820_000.0                                    # 818,478.28 < bp < 822,722.51
    summary = [_row(ACCT, "NetLiquidation", str(_U25274773_NET_LIQ)),
               _row(ACCT, "BuyingPower", str(bp)),
               _row(ACCT, "TotalCashValue", str(_U25274773_NET_LIQ))]
    req = _u25274773_request(armed=True, summary=summary)
    ib = _TxFakeIB(summary)
    res = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)

    assert res.status == se.STATUS_BLOCKED
    assert ib.placed == []
    assert _overdeploy_reasons(res) == [], res.reasons          # the investable gate CLEARED
    bp_reasons = [r for r in res.reasons if "buying power" in r]
    assert len(bp_reasons) == 1, res.reasons
    assert f"total BUY notional {_U25274773_CAP_TOTAL:,.2f}" in bp_reasons[0]
