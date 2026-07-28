"""
test_s0_live_deploy.py — offline unit tests for the S0 GROWTH full-account DEPLOY executor.

ZERO real transmit. NO broker, NO gateway, NO network. The fake IB raises AssertionError if
any order-placing method is touched, and order_router.place / already_present are mocked, so
nothing can ever reach the wire. These tests pin the non-bypassable safety envelope:
  1. preview (default) transmits nothing; order_router.place / ib.placeOrder NEVER called.
  2. conform=False leaves ALIENs untouched (no liquidation legs; aliens returned for review).
  3. conform=True adds a full-liquidation SELL for each ALIEN holding.
  4. sells are sequenced BEFORE buys in the ordered leg list.
  5. total BUY notional > investable -> refuse (no transmit).
  6. a single order > 50% of NetLiq -> refuse (no transmit).
  7. wrong account (not U14438624) -> refuse.
  8. arm token required (and conform alone, without arming, transmits nothing).
  9. kill switch present -> preview only.
 10. whole-share rounding (fractional alien shares truncate; deltas stay integer).
 11. armed + conform + all gates pass -> place called ONCE with the ordered (sells-first) list.

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
        raise AssertionError("ib.placeOrder must never be called by the deploy executor")


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
    monkeypatch.setattr(dep, "_probe_gateway_readonly", lambda ib, **k: False)
    monkeypatch.setattr(order_router, "already_present",
                        lambda ib, ref, qty, **k: order_router.LegState.FRESH)


def _wire_connections(monkeypatch, fake):
    monkeypatch.setattr(dep.s0_live, "connect_s0_live", lambda *a, **k: fake)
    monkeypatch.setattr(dep.s0_live, "connect_s0_live_armed", lambda *a, **k: fake)


def _summary(net_liq="100000", buying_power="100000"):
    return [_summary_row(ACCT, "NetLiquidation", net_liq),
            _summary_row(ACCT, "BuyingPower", buying_power),
            _summary_row(OTHER, "NetLiquidation", "999999"),
            _summary_row("All", "NetLiquidation", "888")]


# --- 1. preview (default) transmits nothing -----------------------------------------
def test_preview_transmits_nothing(monkeypatch):
    _patch_common(monkeypatch)
    fake = _FakeIB(_summary(), [])
    _wire_connections(monkeypatch, fake)
    monkeypatch.setattr(order_router, "place",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("order_router.place must not run in preview")))

    rc = dep.main(armed=False, conform=True)   # conform on, NOT armed -> the proof run

    assert rc == 0
    assert fake.disconnected is True
    assert config.DRY_RUN is True and config.READONLY is True


# --- 2. conform=False leaves ALIENs (no liquidation legs) ----------------------------
def test_conform_false_leaves_aliens():
    plan = _fake_plan(orders={"VTI": 10}, alien_lines=[_alien("GDX", 100), _alien("SIL", 50)])
    legs, aliens_left, unpriceable = dep.build_deploy_legs(
        plan, quotes={}, prices={"VTI": 250.0, "GDX": 30.0, "SIL": 40.0}, conform=False)
    # No alien liquidation legs.
    assert all(l.source != "alien_liquidation" for l in legs)
    assert not any(l.symbol in ("GDX", "SIL") for l in legs)
    # ALIENs surfaced for review instead.
    assert {ln.symbol for ln in aliens_left} == {"GDX", "SIL"}


# --- 3. conform=True adds full-liquidation SELLs for each ALIEN ----------------------
def test_conform_true_liquidates_aliens():
    plan = _fake_plan(orders={"VTI": 10}, alien_lines=[_alien("GDX", 100), _alien("SIL", 50)])
    legs, aliens_left, unpriceable = dep.build_deploy_legs(
        plan, quotes={}, prices={"VTI": 250.0, "GDX": 30.0, "SIL": 40.0}, conform=True)
    liq = {l.symbol: l for l in legs if l.source == "alien_liquidation"}
    assert set(liq) == {"GDX", "SIL"}
    assert liq["GDX"].side == "SELL" and liq["GDX"].qty == 100   # FULL share count
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
    # every SELL index precedes every BUY index
    last_sell = max((i for i, s in enumerate(sides) if s == "SELL"), default=-1)
    first_buy = min((i for i, s in enumerate(sides) if s == "BUY"), default=len(sides))
    assert last_sell < first_buy
    # the alien liquidation is among the sells
    assert any(l.symbol == "GDX" and l.side == "SELL" for l in legs)


# --- 5. total BUY notional > investable -> refuse -----------------------------------
def test_total_buy_over_investable_refuses(monkeypatch):
    # Two buys of 8,000 each = 16,000 total; investable only 10,000. Each < 50% NLV.
    plan = _fake_plan(orders={"VTI": 32, "RSP": 44}, alien_lines=[], investable=10_000.0)
    tgt = _fake_target(weights={"VTI": 0.5, "RSP": 0.5},
                       prices={"VTI": 250.0, "RSP": 181.8})
    _patch_common(monkeypatch, plan=plan, target=tgt)
    fake = _FakeIB(_summary(), [])
    _wire_connections(monkeypatch, fake)
    monkeypatch.setattr(order_router, "place",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not transmit over the investable cap")))

    rc = dep.main(armed=True, conform=True)

    assert rc == 0
    assert config.DRY_RUN is True and config.READONLY is True


# --- 6. a single order > 50% of NetLiq -> refuse ------------------------------------
def test_per_order_over_half_netliq_refuses(monkeypatch):
    # One BUY of 60,000 notional on a 100,000 NLV account -> > 50% cap. investable large
    # enough that the total-buy cap is NOT what trips it.
    plan = _fake_plan(orders={"VTI": 1}, alien_lines=[], investable=98_500.0,
                      net_liq=100_000.0)
    tgt = _fake_target(weights={"VTI": 1.0}, prices={"VTI": 60_000.0})
    _patch_common(monkeypatch, plan=plan, target=tgt)
    fake = _FakeIB(_summary(), [])
    _wire_connections(monkeypatch, fake)
    monkeypatch.setattr(order_router, "place",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not transmit a >50%-NLV single order")))

    rc = dep.main(armed=True, conform=True)

    assert rc == 0
    assert config.DRY_RUN is True and config.READONLY is True


# --- 7. wrong account -> refuse -----------------------------------------------------
def test_wrong_account_refuses(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(dep, "EXEC_ACCOUNT", OTHER)
    fake = _FakeIB([_summary_row(OTHER, "NetLiquidation", "100000"),
                    _summary_row(OTHER, "BuyingPower", "100000")], [])
    _wire_connections(monkeypatch, fake)
    monkeypatch.setattr(order_router, "place",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must NEVER transmit on a non-target account")))

    rc = dep.main(armed=True, conform=True)

    assert rc == 0
    ok, _reason = dep._account_safety_ok()
    assert ok is False
    assert config.DRY_RUN is True and config.READONLY is True


# --- 8. arm token required; conform alone does NOT transmit --------------------------
def test_arm_token_required(monkeypatch):
    assert dep.arm_requested(["--arm-i-understand"]) is True
    assert dep.arm_requested(["--conform"]) is False
    assert dep.conform_requested(["--conform"]) is True
    assert dep.conform_requested([]) is False

    _patch_common(monkeypatch)
    fake = _FakeIB(_summary(), [])
    _wire_connections(monkeypatch, fake)
    monkeypatch.setattr(order_router, "place",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("conform without arming must transmit nothing")))

    rc = dep.main(armed=False, conform=True)   # conform on, not armed

    assert rc == 0
    assert config.DRY_RUN is True and config.READONLY is True


# --- 8b. armed WITHOUT conform does NOT transmit (conform is a required gate) --------
def test_armed_without_conform_refuses(monkeypatch):
    _patch_common(monkeypatch, plan=_fake_plan(orders={"VTI": 10}, alien_lines=[_alien("GDX", 100)]))
    fake = _FakeIB(_summary(), [])
    _wire_connections(monkeypatch, fake)
    monkeypatch.setattr(order_router, "place",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("armed without conform must transmit nothing")))

    rc = dep.main(armed=True, conform=False)

    assert rc == 0
    assert config.DRY_RUN is True and config.READONLY is True


# --- 9. kill switch forces preview --------------------------------------------------
def test_kill_switch_forces_preview(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(dep, "_kill_switch_present", lambda: True)
    fake = _FakeIB(_summary(), [])
    _wire_connections(monkeypatch, fake)
    monkeypatch.setattr(order_router, "place",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not transmit with the kill switch on")))

    rc = dep.main(armed=True, conform=True)

    assert rc == 0
    assert config.DRY_RUN is True and config.READONLY is True


# --- 10. whole-share rounding -------------------------------------------------------
def test_whole_share_rounding():
    # Fractional alien share count truncates toward 0 (never a fractional order).
    plan = _fake_plan(orders={"VTI": 10}, alien_lines=[_alien("GDX", 100.9)])
    legs, aliens_left, _ = dep.build_deploy_legs(
        plan, quotes={}, prices={"VTI": 250.0, "GDX": 30.0}, conform=True)
    liq = next(l for l in legs if l.symbol == "GDX")
    assert liq.qty == 100 and isinstance(liq.qty, int)   # truncated, integer
    # A sub-1-share alien can't be whole-share liquidated -> left for review.
    plan2 = _fake_plan(orders={"VTI": 10}, alien_lines=[_alien("SIL", 0.4)])
    legs2, aliens_left2, _ = dep.build_deploy_legs(
        plan2, quotes={}, prices={"VTI": 250.0, "SIL": 40.0}, conform=True)
    assert not any(l.symbol == "SIL" for l in legs2)
    assert {ln.symbol for ln in aliens_left2} == {"SIL"}


# --- 11. armed + conform + all gates pass -> place called ONCE, sells first ----------
def test_armed_conform_all_gates_pass_transmits(monkeypatch):
    plan = _fake_plan(orders={"VTI": 10, "BIL": -3}, alien_lines=[_alien("GDX", 100)],
                      investable=98_500.0, net_liq=100_000.0)
    tgt = _fake_target(weights={"VTI": 0.8, "USFR": 0.2},
                       prices={"VTI": 250.0, "USFR": 50.0, "BIL": 91.0, "GDX": 30.0})
    _patch_common(monkeypatch, plan=plan, target=tgt)
    # BIL (a plan sell) and GDX (an alien) are HELD — so they fall inside the priced universe
    # (target weights + held positions), exactly as they would in a real book.
    fake = _FakeIB(_summary(), [_pos_row(ACCT, "BIL", 3), _pos_row(ACCT, "GDX", 100)])
    _wire_connections(monkeypatch, fake)

    captured = {}

    def _capture_place(ib, built, armed=False, **k):
        captured["armed"] = armed
        captured["built"] = built
        captured["account_kw"] = k.get("account")
        captured["dry_run_at_place"] = config.DRY_RUN
        captured["readonly_at_place"] = config.READONLY
        return {"transmitted": len(built), "logged": len(built),
                "fills": [{"symbol": b.symbol, "status": "Filled", "filled": 1.0,
                           "remaining": 0.0, "avgFillPrice": 1.0} for b in built]}

    monkeypatch.setattr(order_router, "place", _capture_place)

    rc = dep.main(armed=True, conform=True)

    assert rc == 0
    assert captured["armed"] is True
    assert captured["dry_run_at_place"] is False and captured["readonly_at_place"] is False
    # flags restored after main() returns
    assert config.DRY_RUN is True and config.READONLY is True
    built = captured["built"]
    # legs: SELL BIL, SELL GDX (alien liquidation), BUY VTI -> all sells before the buy
    actions = [b.order.action for b in built]
    last_sell = max(i for i, a in enumerate(actions) if a == "SELL")
    first_buy = min(i for i, a in enumerate(actions) if a == "BUY")
    assert last_sell < first_buy
    # every order pinned to the single allowed account, LIMIT (never market)
    for b in built:
        assert b.order.account == ACCT == "U14438624"
        assert b.order.orderType == "LMT"
    # the alien GDX full-liquidation sell is present
    assert any(b.symbol == "GDX" and b.order.action == "SELL"
               and float(b.order.totalQuantity) == 100.0 for b in built)
    assert captured["account_kw"] == ACCT


# --- 12. deploy ref namespace: standard (tiny-test) ref present does NOT block -------
def test_deploy_ref_namespace_not_blocked_by_standard_ref(monkeypatch):
    """The confirmed 2026-07-28 bug: this morning's tiny-test (s0_live_exec) already placed a
    BUY USFR on U14438624 with the STANDARD order_router._order_ref. The deploy must NOT
    false-block: its legs use the :deploy-tagged ref, which is FRESH, so all-or-nothing
    transmit proceeds. Proven by making already_present return PARTIAL for any ref that is NOT
    :deploy-tagged (the tiny-test's ref) but FRESH for the :deploy-tagged deploy ref."""
    plan = _fake_plan(orders={"VTI": 10, "USFR": 5}, alien_lines=[],
                      investable=98_500.0, net_liq=100_000.0)
    tgt = _fake_target(weights={"VTI": 0.8, "USFR": 0.2},
                       prices={"VTI": 250.0, "USFR": 50.0})
    _patch_common(monkeypatch, plan=plan, target=tgt)

    seen_refs = []

    def _dedup(ib, ref, qty, **k):
        seen_refs.append(ref)
        # standard tiny-test ref (no :deploy tag) is already present -> PARTIAL; the deploy's
        # own :deploy-tagged ref is FRESH.
        return (order_router.LegState.FRESH if ref.endswith(":" + dep.DEPLOY_REF_TAG)
                else order_router.LegState.PARTIAL)

    monkeypatch.setattr(order_router, "already_present", _dedup)

    fake = _FakeIB(_summary(), [])
    _wire_connections(monkeypatch, fake)

    captured = {}

    def _capture_place(ib, built, armed=False, **k):
        captured["built"] = built
        return {"transmitted": len(built), "logged": len(built),
                "fills": [{"symbol": b.symbol, "status": "Filled", "filled": 1.0,
                           "remaining": 0.0, "avgFillPrice": 1.0} for b in built]}

    monkeypatch.setattr(order_router, "place", _capture_place)

    rc = dep.main(armed=True, conform=True)

    assert rc == 0
    # the deploy transmitted despite the colliding standard ref being PARTIAL
    assert "built" in captured
    assert {b.symbol for b in captured["built"]} == {"VTI", "USFR"}
    # every ref the dedup gate checked carried the :deploy namespace tag
    assert seen_refs and all(r.endswith(":" + dep.DEPLOY_REF_TAG) for r in seen_refs)
    # the transmitted orderRef matches the deploy-namespaced ref exactly (dedup ref == place ref)
    assert all(b.order_ref.endswith(":" + dep.DEPLOY_REF_TAG) for b in captured["built"])


# --- 13. deploy ref namespace: re-send protection intact ----------------------------
def test_deploy_ref_namespace_blocks_own_resend(monkeypatch):
    """Re-send protection unchanged: when the DEPLOY-tagged ref itself is already present
    (a genuine re-run of the same deploy leg), the dedup gate still returns not-FRESH and the
    all-or-nothing gate blocks the whole deploy -> preview only, nothing transmitted."""
    plan = _fake_plan(orders={"VTI": 10, "USFR": 5}, alien_lines=[],
                      investable=98_500.0, net_liq=100_000.0)
    tgt = _fake_target(weights={"VTI": 0.8, "USFR": 0.2},
                       prices={"VTI": 250.0, "USFR": 50.0})
    _patch_common(monkeypatch, plan=plan, target=tgt)

    def _dedup(ib, ref, qty, **k):
        # the deploy-tagged ref is already present (prior deploy run) -> not FRESH -> block.
        return (order_router.LegState.PARTIAL if ref.endswith(":" + dep.DEPLOY_REF_TAG)
                else order_router.LegState.FRESH)

    monkeypatch.setattr(order_router, "already_present", _dedup)

    fake = _FakeIB(_summary(), [])
    _wire_connections(monkeypatch, fake)
    monkeypatch.setattr(order_router, "place",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("re-send of an already-present deploy ref must "
                                           "transmit nothing")))

    rc = dep.main(armed=True, conform=True)

    assert rc == 0
    assert config.DRY_RUN is True and config.READONLY is True


# --- 14. the two ref sites are byte-identical (built via the one _deploy_ref helper) --
def test_deploy_ref_format_and_single_source():
    """_deploy_ref = the standard order_router ref + ':<DEPLOY_REF_TAG>'. Both the dedup-check
    and the place() call build the ref through this one helper, so the checked ref and the
    transmitted ref can never drift."""
    as_of = pd.Timestamp("2026-07-01")
    std = order_router._order_ref(ACCT, as_of, "BUY", "USFR")
    tagged = dep._deploy_ref(ACCT, as_of, "BUY", "USFR")
    assert tagged == std + ":" + dep.DEPLOY_REF_TAG
    assert tagged.endswith(":deploy") and tagged != std
