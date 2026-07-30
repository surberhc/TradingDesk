"""
test_s0_live_deploy.py — offline unit tests for the S0 GROWTH full-account DEPLOY executor.

ZERO real transmit. NO broker, NO gateway, NO network. The preview fake IB raises
AssertionError if any order-placing method is touched; the transmit fake IB (_TxFakeIB) serves
a mocked account and records placed/cancelled orders but reaches no wire. These tests pin the
non-bypassable safety envelope AND the 2026-07-28 rebuild:
  1. preview (default) transmits nothing; no placeOrder ever called.
  2. conform=False leaves ALIENs untouched (no liquidation legs; aliens returned for review).
  3. conform=True adds a full-liquidation SELL for each ALIEN holding.
  4. sells are sequenced BEFORE buys in the ordered leg list.
  5. total BUY notional > investable -> refuse (no transmit).
  6. a single order > 50% of NetLiq -> refuse (no transmit).
  7. wrong account (not U14438624) -> refuse.
  8. arm token required (and conform alone, without arming, transmits nothing).
  9. kill switch present -> preview only.
 10. whole-share rounding (fractional alien shares truncate; deltas stay integer).
 11. TWO-PHASE: sells transmit before buys; buys funded from realized cash.
 12. TWO-PHASE cash gate: a short realized cash reduces/skips buys, never exceeding cash.
 13. _scale_buys_to_cash: whole-share, never exceeds cash*(1-buffer); 0 cash skips all.
 14. re-price loop: an unfilled straggler is cancelled + re-priced and, after the cap, reported.
 15. per-run ref: a re-fire (bought-then-sold; no working order) re-buys, but an identical
     currently-WORKING order is NOT double-submitted.
 16. armed + conform transmits the two-phase deploy (no model gate — removed 2026-07-29,
     owner manages model divestment manually).

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s0_live_deploy.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import config
import order_router
import s0_live_deploy as dep
import safe_execute as se               # the gate/probe/tuning constants MOVED here (Phase 2)

ACCT = dep.EXEC_ACCOUNT                     # "U14438624" (trust account, PDT-clear)
OTHER = "U5721712"                          # a NON-target account — must be refused


# --- leak guard ---------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_config_flag_leak():
    prev_dry_run, prev_readonly = config.DRY_RUN, config.READONLY
    assert prev_dry_run is True and prev_readonly is True
    try:
        yield
    finally:
        config.DRY_RUN, config.READONLY = prev_dry_run, prev_readonly


# --- fixtures -----------------------------------------------------------------------
def _fake_target(weights=None, prices=None):
    weights = weights or {"VTI": 0.5, "RSP": 0.3, "USFR": 0.2}
    prices = prices or {"VTI": 250.0, "RSP": 180.0, "USFR": 50.0,
                        "BUCK": 100.0, "GDX": 30.0, "SIL": 40.0, "BIL": 91.0}
    return dep.strategy_target.Target(
        weights=pd.Series(weights),
        prices=pd.Series(prices),
        as_of=pd.Timestamp("2026-07-28"),
        price_date=pd.Timestamp("2026-07-28"),
        version="Growth",
    )


def _summary_row(account, tag, value):
    return SimpleNamespace(account=account, tag=tag, value=value)


def _pos_row(account, symbol, position):
    return SimpleNamespace(account=account, position=position,
                           contract=SimpleNamespace(symbol=symbol))


def _alien(symbol, shares):
    return SimpleNamespace(symbol=symbol, actual_shares=shares, status="ALIEN")


def _open_order(symbol, side):
    """An open-order row shaped like ib_async's reqAllOpenOrders() Trade (order + contract)."""
    return SimpleNamespace(order=SimpleNamespace(action=side, orderRef="prior-run"),
                           contract=SimpleNamespace(symbol=symbol))


class _FakeIB:
    """A fake IB that serves a filtered summary/positions and BLOWS UP on any transmit
    (used by the PREVIEW tests to prove nothing reaches the wire)."""
    def __init__(self, summary_rows, position_rows):
        self._summary = summary_rows
        self._positions = position_rows
        self.disconnected = False

    def accountSummary(self):
        return self._summary

    def positions(self):
        return self._positions

    def reqAllOpenOrders(self):
        return []

    def qualifyContracts(self, *a, **k):
        return list(a)

    def sleep(self, *a, **k):
        return None

    def disconnect(self):
        self.disconnected = True

    def placeOrder(self, *a, **k):
        raise AssertionError("ib.placeOrder must never be called by the deploy executor")


class _FakeTrade:
    """A placed-order stand-in with a controllable terminal/fill state."""
    def __init__(self, contract, order, *, fill=True, filled=None):
        self.contract = contract
        self.order = order
        q = float(order.totalQuantity)
        if fill:
            self.orderStatus = SimpleNamespace(
                status="Filled", filled=q, remaining=0.0,
                avgFillPrice=float(getattr(order, "lmtPrice", 0.0) or 0.0))
            self._done = True
        else:
            f = float(filled or 0.0)
            self.orderStatus = SimpleNamespace(status="Submitted", filled=f,
                                               remaining=q - f, avgFillPrice=0.0)
            self._done = False

    def isDone(self):
        return self._done


class _TxFakeIB:
    """Transmit-path fake IB: serves a mocked summary/positions/open-orders and RECORDS the
    orders placed/cancelled. Fills every order except symbols in no_fill (which stay working,
    to exercise the re-price loop). Reaches no real broker."""
    def __init__(self, summary_rows, position_rows, *, open_orders=None, no_fill_symbols=None):
        self._summary = summary_rows
        self._positions = position_rows
        self.open_orders = list(open_orders or [])
        self.no_fill = set(no_fill_symbols or [])
        self.placed = []       # every order object handed to placeOrder (transmit=True)
        self.cancelled = []
        self.disconnected = False

    def accountSummary(self):
        return self._summary

    def positions(self):
        return self._positions

    def reqAllOpenOrders(self):
        return list(self.open_orders)

    def qualifyContracts(self, *a, **k):
        return list(a)

    def sleep(self, *a, **k):
        return None

    def placeOrder(self, contract, order):
        self.placed.append(order)
        fill = contract.symbol not in self.no_fill
        return _FakeTrade(contract, order, fill=fill)

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def disconnect(self):
        self.disconnected = True


def _fake_plan(*, orders=None, alien_lines=None, investable=98_500.0, net_liq=100_000.0):
    return SimpleNamespace(
        account=ACCT, version="Growth", net_liq=net_liq, reserve=0.0,
        investable=investable, lines=[], needs_rebalance=True,
        orders=orders if orders is not None else {"VTI": 10, "USFR": 5, "BIL": -3},
        alien_lines=alien_lines if alien_lines is not None else [])


def _patch_common(monkeypatch, *, plan=None, target=None):
    monkeypatch.setattr(dep.strategy_target, "current_target",
                        lambda *a, **k: (target or _fake_target()))
    monkeypatch.setattr(dep.sp, "_strategy_universe", lambda: {"VTI", "RSP", "USFR", "BIL"})
    monkeypatch.setattr(dep.live_quotes, "fetch", lambda ib, universe: {})
    monkeypatch.setattr(dep.rebalance_engine, "plan_account",
                        lambda *a, **k: (plan or _fake_plan()))
    monkeypatch.setattr(dep, "_kill_switch_present", lambda: False)
    # The gateway read-only probe moved to safe_execute (Phase 2); the engine calls it there.
    monkeypatch.setattr(se, "_probe_gateway_readonly", lambda ib, **k: False)


def _wire_connections(monkeypatch, fake):
    monkeypatch.setattr(dep.s0_live, "connect_s0_live", lambda *a, **k: fake)
    monkeypatch.setattr(dep.s0_live, "connect_s0_live_armed", lambda *a, **k: fake)


def _summary(net_liq="100000", buying_power="100000", total_cash="100000"):
    return [_summary_row(ACCT, "NetLiquidation", net_liq),
            _summary_row(ACCT, "BuyingPower", buying_power),
            _summary_row(ACCT, "TotalCashValue", total_cash),
            _summary_row(OTHER, "NetLiquidation", "999999"),
            _summary_row("All", "NetLiquidation", "888")]


# --- 1. preview (default) transmits nothing -----------------------------------------
def test_preview_transmits_nothing(monkeypatch):
    _patch_common(monkeypatch)
    fake = _FakeIB(_summary(), [])
    _wire_connections(monkeypatch, fake)

    rc = dep.main(armed=False, conform=True)   # conform on, NOT armed -> the proof run

    assert rc == 0
    assert fake.disconnected is True
    assert config.DRY_RUN is True and config.READONLY is True


# --- 2. conform=False leaves ALIENs (no liquidation legs) ----------------------------
def test_conform_false_leaves_aliens():
    plan = _fake_plan(orders={"VTI": 10}, alien_lines=[_alien("GDX", 100), _alien("SIL", 50)])
    legs, aliens_left, unpriceable = dep.build_deploy_legs(
        plan, quotes={}, prices={"VTI": 250.0, "GDX": 30.0, "SIL": 40.0}, conform=False)
    assert all(l.source != "alien_liquidation" for l in legs)
    assert not any(l.symbol in ("GDX", "SIL") for l in legs)
    assert {ln.symbol for ln in aliens_left} == {"GDX", "SIL"}


# --- 3. conform=True adds full-liquidation SELLs for each ALIEN ----------------------
def test_conform_true_liquidates_aliens():
    plan = _fake_plan(orders={"VTI": 10}, alien_lines=[_alien("GDX", 100), _alien("SIL", 50)])
    legs, aliens_left, unpriceable = dep.build_deploy_legs(
        plan, quotes={}, prices={"VTI": 250.0, "GDX": 30.0, "SIL": 40.0}, conform=True)
    liq = {l.symbol: l for l in legs if l.source == "alien_liquidation"}
    assert set(liq) == {"GDX", "SIL"}
    assert liq["GDX"].side == "SELL" and liq["GDX"].qty == 100
    assert liq["SIL"].side == "SELL" and liq["SIL"].qty == 50
    assert not aliens_left


# --- 4. sells sequenced BEFORE buys -------------------------------------------------
def test_sells_before_buys():
    plan = _fake_plan(orders={"VTI": 10, "USFR": 5, "BIL": -3},
                      alien_lines=[_alien("GDX", 100)])
    legs, _, _ = dep.build_deploy_legs(
        plan, quotes={},
        prices={"VTI": 250.0, "USFR": 50.0, "BIL": 91.0, "GDX": 30.0}, conform=True)
    sides = [l.side for l in legs]
    last_sell = max((i for i, s in enumerate(sides) if s == "SELL"), default=-1)
    first_buy = min((i for i, s in enumerate(sides) if s == "BUY"), default=len(sides))
    assert last_sell < first_buy
    assert any(l.symbol == "GDX" and l.side == "SELL" for l in legs)


# --- 5. total BUY notional > investable -> refuse -----------------------------------
def test_total_buy_over_investable_refuses(monkeypatch):
    plan = _fake_plan(orders={"VTI": 32, "RSP": 44}, alien_lines=[], investable=10_000.0)
    tgt = _fake_target(weights={"VTI": 0.5, "RSP": 0.5},
                       prices={"VTI": 250.0, "RSP": 181.8})
    _patch_common(monkeypatch, plan=plan, target=tgt)
    fake = _TxFakeIB(_summary(), [])
    _wire_connections(monkeypatch, fake)

    rc = dep.main(armed=True, conform=True)

    assert rc == 0
    assert fake.placed == []       # nothing transmitted over the investable cap
    assert config.DRY_RUN is True and config.READONLY is True


# --- 6. a single order > 50% of NetLiq -> refuse ------------------------------------
def test_per_order_over_half_netliq_refuses(monkeypatch):
    plan = _fake_plan(orders={"VTI": 1}, alien_lines=[], investable=98_500.0,
                      net_liq=100_000.0)
    tgt = _fake_target(weights={"VTI": 1.0}, prices={"VTI": 60_000.0})
    _patch_common(monkeypatch, plan=plan, target=tgt)
    fake = _TxFakeIB(_summary(), [])
    _wire_connections(monkeypatch, fake)

    rc = dep.main(armed=True, conform=True)

    assert rc == 0
    assert fake.placed == []
    assert config.DRY_RUN is True and config.READONLY is True


# --- 7. wrong account -> refuse -----------------------------------------------------
def test_wrong_account_refuses(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(dep, "EXEC_ACCOUNT", OTHER)
    fake = _TxFakeIB([_summary_row(OTHER, "NetLiquidation", "100000"),
                      _summary_row(OTHER, "BuyingPower", "100000"),
                      _summary_row(OTHER, "TotalCashValue", "100000")], [])
    _wire_connections(monkeypatch, fake)

    rc = dep.main(armed=True, conform=True)

    assert rc == 0
    assert fake.placed == []
    ok, _reason = dep._account_safety_ok()
    assert ok is False
    assert config.DRY_RUN is True and config.READONLY is True


# --- 8. arm token / conform flag parsing + conform alone transmits nothing -----------
def test_flag_parsing_and_conform_alone(monkeypatch):
    assert dep.arm_requested(["--arm-i-understand"]) is True
    assert dep.arm_requested(["--conform"]) is False
    assert dep.conform_requested(["--conform"]) is True
    assert dep.conform_requested([]) is False

    _patch_common(monkeypatch)
    fake = _FakeIB(_summary(), [])
    _wire_connections(monkeypatch, fake)

    rc = dep.main(armed=False, conform=True)   # conform on, not armed

    assert rc == 0
    assert config.DRY_RUN is True and config.READONLY is True


# --- 8b. armed WITHOUT conform does NOT transmit ------------------------------------
def test_armed_without_conform_refuses(monkeypatch):
    _patch_common(monkeypatch,
                  plan=_fake_plan(orders={"VTI": 10}, alien_lines=[_alien("GDX", 100)]))
    fake = _FakeIB(_summary(), [])
    _wire_connections(monkeypatch, fake)

    rc = dep.main(armed=True, conform=False)

    assert rc == 0
    assert config.DRY_RUN is True and config.READONLY is True


# --- 9. kill switch forces preview --------------------------------------------------
def test_kill_switch_forces_preview(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(dep, "_kill_switch_present", lambda: True)
    fake = _FakeIB(_summary(), [])
    _wire_connections(monkeypatch, fake)

    rc = dep.main(armed=True, conform=True)

    assert rc == 0
    assert config.DRY_RUN is True and config.READONLY is True


# --- 10. whole-share rounding -------------------------------------------------------
def test_whole_share_rounding():
    plan = _fake_plan(orders={"VTI": 10}, alien_lines=[_alien("GDX", 100.9)])
    legs, aliens_left, _ = dep.build_deploy_legs(
        plan, quotes={}, prices={"VTI": 250.0, "GDX": 30.0}, conform=True)
    liq = next(l for l in legs if l.symbol == "GDX")
    assert liq.qty == 100 and isinstance(liq.qty, int)
    plan2 = _fake_plan(orders={"VTI": 10}, alien_lines=[_alien("SIL", 0.4)])
    legs2, aliens_left2, _ = dep.build_deploy_legs(
        plan2, quotes={}, prices={"VTI": 250.0, "SIL": 40.0}, conform=True)
    assert not any(l.symbol == "SIL" for l in legs2)
    assert {ln.symbol for ln in aliens_left2} == {"SIL"}


# --- 11. TWO-PHASE: sells transmit before buys; buys funded from realized cash --------
def test_two_phase_sells_before_buys_and_funded(monkeypatch):
    plan = _fake_plan(orders={"VTI": 10, "BIL": -3}, alien_lines=[_alien("GDX", 100)],
                      investable=98_500.0, net_liq=100_000.0)
    tgt = _fake_target(weights={"VTI": 0.8, "USFR": 0.2},
                       prices={"VTI": 250.0, "USFR": 50.0, "BIL": 91.0, "GDX": 30.0})
    _patch_common(monkeypatch, plan=plan, target=tgt)
    # Realized cash after the sells is ample (60k) -> buys fully funded, no scaling.
    fake = _TxFakeIB(_summary(total_cash="60000"),
                     [_pos_row(ACCT, "BIL", 3), _pos_row(ACCT, "GDX", 100)])
    _wire_connections(monkeypatch, fake)

    rc = dep.main(armed=True, conform=True)

    assert rc == 0
    # flags restored after main() returns
    assert config.DRY_RUN is True and config.READONLY is True
    actions = [o.action for o in fake.placed]
    # every SELL was placed before every BUY (two phases in order)
    last_sell = max(i for i, a in enumerate(actions) if a == "SELL")
    first_buy = min(i for i, a in enumerate(actions) if a == "BUY")
    assert last_sell < first_buy
    # the alien GDX full-liquidation sell (100 sh) is present
    assert any(o.action == "SELL" and ":SELL:GDX:" in (o.orderRef or "")
               and float(o.totalQuantity) == 100.0 for o in fake.placed)
    # VTI buy fully funded at 10 shares (realized cash >> notional)
    vti = [o for o in fake.placed if o.action == "BUY" and ":BUY:VTI:" in (o.orderRef or "")]
    assert vti and float(vti[0].totalQuantity) == 10.0
    # every order pinned to the single account, LIMIT, per-run ref (:deploy:<stamp>)
    for o in fake.placed:
        assert o.account == ACCT == "U14438624"
        assert o.orderType == "LMT"
        assert ":deploy:" in o.orderRef
    # buy notional never exceeds realized cash
    buy_notional = sum(float(o.totalQuantity) * float(o.lmtPrice)
                       for o in fake.placed if o.action == "BUY")
    assert buy_notional <= 60000.0


# --- 12. TWO-PHASE cash gate: short realized cash reduces buys, never exceeding cash ---
def test_two_phase_short_cash_reduces_buys(monkeypatch):
    # Plan wants BUY VTI x40 = $10,000, but only $4,000 of cash actually lands.
    plan = _fake_plan(orders={"VTI": 40}, alien_lines=[], investable=98_500.0,
                      net_liq=100_000.0)
    tgt = _fake_target(weights={"VTI": 1.0}, prices={"VTI": 250.0})
    _patch_common(monkeypatch, plan=plan, target=tgt)
    fake = _TxFakeIB(_summary(total_cash="4000"), [])
    _wire_connections(monkeypatch, fake)

    rc = dep.main(armed=True, conform=True)

    assert rc == 0
    buys = [o for o in fake.placed if o.action == "BUY"]
    assert buys, "a scaled-down buy should still transmit"
    total = sum(float(o.totalQuantity) * float(o.lmtPrice) for o in buys)
    assert total <= 4000.0                       # NEVER exceeds realized cash — no negative
    assert all(float(o.totalQuantity) < 40 for o in buys)   # reduced from the plan's 40


# --- 13. _scale_buys_to_cash: whole-share, bounded by cash*(1-buffer); 0 cash skips all ---
def test_scale_buys_to_cash_never_exceeds():
    NS = SimpleNamespace
    buys = [NS(symbol="VTI", side="BUY", qty=40, limit=250.0, notional=40 * 250.0,
               source="plan")]
    scaled, adj = dep._scale_buys_to_cash(buys, 4000.0)
    budget = 4000.0 * (1.0 - dep.CASH_SAFETY_BUFFER_PCT)
    assert sum(l.notional for l in scaled) <= budget
    assert scaled and scaled[0].qty < 40
    assert adj and adj[0]["symbol"] == "VTI" and adj[0]["new_qty"] < 40


def test_scale_buys_to_cash_zero_cash_skips_all():
    NS = SimpleNamespace
    buys = [NS(symbol="VTI", side="BUY", qty=10, limit=250.0, notional=2500.0, source="plan")]
    scaled, adj = dep._scale_buys_to_cash(buys, 0.0)
    assert scaled == []
    assert adj and adj[0]["new_qty"] == 0


# --- 14. re-price loop: unfilled straggler cancelled + re-priced, then reported ------
def test_transmit_phase_reprices_then_gives_up(monkeypatch):
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    # The two-phase tuning constants moved to safe_execute (Phase 2); _transmit_phase reads
    # them there, so patch them at their new home.
    monkeypatch.setattr(se, "REPRICE_AFTER_SEC", 0.0)      # re-price on the first poll
    monkeypatch.setattr(se, "POLL_SEC", 0.0)
    monkeypatch.setattr(se, "PHASE_TERMINAL_TIMEOUT_SEC", 0.15)
    NS = SimpleNamespace
    leg = NS(symbol="THIN", side="BUY", qty=5, limit=100.0, notional=500.0, source="plan")
    ib = _TxFakeIB(_summary(), [], no_fill_symbols={"THIN"})   # THIN never fills

    results = dep._transmit_phase(ib, [leg], account=ACCT, as_of=pd.Timestamp("2026-07-28"),
                                  run_id="R1", phase_label="BUY", quotes={},
                                  prices={"THIN": 100.0})

    r = results[0]
    assert r["reprices"] == dep.REPRICE_MAX_ATTEMPTS       # chased up to the cap
    assert r["filled"] == 0.0
    assert r["skipped"] is False and r["reason"]           # LOUD unfilled report
    assert len(ib.cancelled) >= dep.REPRICE_MAX_ATTEMPTS   # cancelled on each re-price
    # each re-place used a strictly-more-aggressive (higher, for a BUY) limit
    limits = [float(o.lmtPrice) for o in ib.placed]
    assert limits == sorted(limits) and limits[-1] > limits[0]


# --- 15. per-run ref: re-fire re-buys (not blocked); working order NOT double-submitted --
def test_transmit_phase_refire_places_when_no_working_order(monkeypatch):
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    NS = SimpleNamespace
    # SPY was bought AND then manually sold earlier today; nothing is currently working.
    leg = NS(symbol="SPY", side="BUY", qty=44, limit=500.0, notional=22000.0, source="plan")
    ib = _TxFakeIB(_summary(), [], open_orders=[])

    results = dep._transmit_phase(ib, [leg], account=ACCT, as_of=pd.Timestamp("2026-07-01"),
                                  run_id="20260728T120000", phase_label="BUY", quotes={},
                                  prices={"SPY": 500.0})

    assert results[0]["skipped"] is False and results[0]["filled"] == 44.0
    assert len(ib.placed) == 1                             # the re-buy proceeded
    ref = ib.placed[0].orderRef
    assert ":deploy:" in ref and ref.endswith("20260728T120000")   # per-run ref


def test_transmit_phase_working_order_not_double_submitted(monkeypatch):
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    NS = SimpleNamespace
    leg = NS(symbol="VTI", side="BUY", qty=10, limit=250.0, notional=2500.0, source="plan")
    # An identical VTI BUY is already working at the broker (from any prior run).
    ib = _TxFakeIB(_summary(), [], open_orders=[_open_order("VTI", "BUY")])

    results = dep._transmit_phase(ib, [leg], account=ACCT, as_of=pd.Timestamp("2026-07-28"),
                                  run_id="R2", phase_label="BUY", quotes={},
                                  prices={"VTI": 250.0})

    assert results[0]["skipped"] is True
    assert ib.placed == []                                # never double-submitted


# --- 16. armed + conform transmits (no model gate — removed 2026-07-29) --------------
def test_armed_conform_transmits(monkeypatch):
    _patch_common(monkeypatch, plan=_fake_plan(orders={"VTI": 10}, alien_lines=[]))
    fake = _TxFakeIB(_summary(total_cash="60000"), [])
    _wire_connections(monkeypatch, fake)

    rc = dep.main(armed=True, conform=True)

    assert rc == 0
    assert any(o.action == "BUY" and float(o.totalQuantity) == 10.0 for o in fake.placed)
    assert config.DRY_RUN is True and config.READONLY is True


# --- 17. deploy ref format: base + :deploy, plus per-run stamp -----------------------
def test_deploy_ref_format_and_per_run():
    as_of = pd.Timestamp("2026-07-01")
    std = order_router._order_ref(ACCT, as_of, "BUY", "USFR")
    tagged = dep._deploy_ref(ACCT, as_of, "BUY", "USFR")
    assert tagged == std + ":" + dep.DEPLOY_REF_TAG
    assert tagged.endswith(":deploy") and tagged != std
    per_run = dep._deploy_ref(ACCT, as_of, "BUY", "USFR", "20260728T120000")
    assert per_run == tagged + ":20260728T120000"
