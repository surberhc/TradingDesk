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
import order_router
import safe_execute as se
import strategy_target

ACCT = "U14438624"
OTHER = "U5721712"


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


def _plan(*, orders=None, alien_lines=None, investable=98_500.0, net_liq=100_000.0):
    return SimpleNamespace(
        account=ACCT, version="Growth", net_liq=net_liq, reserve=0.0,
        investable=investable, lines=[], needs_rebalance=True,
        orders=orders if orders is not None else {"VTI": 10, "USFR": 5, "BIL": -3},
        alien_lines=alien_lines if alien_lines is not None else [])


def _caps(per_order=se.MAX_ORDER_NOTIONAL_PCT_NLV, total_buy_le_investable=True,
          max_total_notional=None):
    return se.ExecutionCaps(per_order_notional_pct_nlv=per_order,
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


# --- 4. caps: per-order > %NLV, and total buy > investable --------------------------
def test_per_order_cap_blocks():
    # One VTI buy at $60k > 50% of $100k NetLiq.
    tgt = _target(weights={"VTI": 1.0}, prices={"VTI": 60_000.0})
    req = _request(plan=_plan(orders={"VTI": 1}, alien_lines=[]), target=tgt, armed=True)
    ib = _TxFakeIB(_summary())
    res = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)
    assert res.status == se.STATUS_BLOCKED
    assert ib.placed == []
    assert any("% of NetLiq" in r for r in res.reasons)


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
