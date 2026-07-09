"""
test_rebalance_guard.py — offline unit tests for the pre-stage safety gate.

NO broker, NO gateway, NO network, NO real price-history reads: compute_regime_now()
is monkeypatched everywhere so these tests never touch backtester/src/data_loader or
the parquet warehouse. Proves all three checks fail closed independently, and that a
fully-clean trade list passes.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_rebalance_guard.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import rebalance_guard as rg


def _route(route="direct", version="Balanced", symbol="SPY", side="BUY", total_qty=10,
          account="DU8922142", per_account_split=None, fa_group=None):
    return SimpleNamespace(
        route=route, version=version, symbol=symbol, side=side, total_qty=total_qty,
        account=account, fa_group=fa_group, fa_method="",
        per_account_split=per_account_split or {account: total_qty},
        reason="REBALANCE_TO_MODEL")


def _account_inputs(**net_liqs):
    return [{"account": a, "net_liq": nl} for a, nl in net_liqs.items()]


@pytest.fixture(autouse=True)
def _fake_universe(monkeypatch):
    """Stub the known-universe source so the test never imports the real strategies
    package's config module chain."""
    monkeypatch.setattr(rg, "_known_universe", lambda: {"SPY", "VTI", "TFLO", "GLDM"})


@pytest.fixture
def clean_regime(monkeypatch):
    """A regime cross-check that always agrees with whatever is claimed."""
    def fake():
        return "GOLDILOCKS", "GOLDILOCKS", "2026-07-09"
    monkeypatch.setattr(rg, "compute_regime_now", fake)
    return fake


# --- (a) ticker allow-list -------------------------------------------------------
def test_passes_when_everything_is_clean(clean_regime):
    routes = [_route(symbol="SPY", total_qty=10)]
    result = rg.check(routes, _account_inputs(DU8922142=1_000_000.0),
                      {"SPY": 500.0}, claimed_regime="GOLDILOCKS")
    assert result.passed is True
    assert result.reasons == []
    assert bool(result) is True


def test_unrecognized_symbol_fails_closed(clean_regime):
    routes = [_route(symbol="ZZZBOGUS", total_qty=10)]
    result = rg.check(routes, _account_inputs(DU8922142=1_000_000.0),
                      {"ZZZBOGUS": 50.0}, claimed_regime="GOLDILOCKS")
    assert result.passed is False
    assert any("unrecognized symbol" in r for r in result.reasons)
    assert "ZZZBOGUS" in str(result.detail["bad_symbols"])


# --- (b) turnover / notional cap --------------------------------------------------
def test_turnover_within_cap_passes(clean_regime):
    # 10 shares * $500 = $5,000 on a $1,000,000 NAV account = 0.5% << 50% cap.
    routes = [_route(symbol="SPY", total_qty=10, account="DU8922142")]
    result = rg.check(routes, _account_inputs(DU8922142=1_000_000.0),
                      {"SPY": 500.0}, claimed_regime="GOLDILOCKS")
    assert result.passed is True


def test_turnover_breach_fails_closed(clean_regime):
    # 10,000 shares * $500 = $5,000,000 on a $1,000,000 NAV account = 500% >> 50% cap.
    routes = [_route(symbol="SPY", total_qty=10_000, account="DU8922142")]
    result = rg.check(routes, _account_inputs(DU8922142=1_000_000.0),
                      {"SPY": 500.0}, claimed_regime="GOLDILOCKS")
    assert result.passed is False
    assert any("turnover cap breached" in r for r in result.reasons)


def test_turnover_cap_boundary_uses_named_constant(clean_regime):
    # Exactly AT the cap should not breach (uses > not >=); just above should.
    nav = 1_000_000.0
    px = 100.0
    at_cap_qty = int((nav * rg.MAX_SINGLE_ACCOUNT_TURNOVER_PCT_NAV) / px)
    routes = [_route(symbol="SPY", total_qty=at_cap_qty, account="DU8922142")]
    result = rg.check(routes, _account_inputs(DU8922142=nav), {"SPY": px},
                      claimed_regime="GOLDILOCKS")
    assert result.passed is True

    over_qty = at_cap_qty + 100
    routes2 = [_route(symbol="SPY", total_qty=over_qty, account="DU8922142")]
    result2 = rg.check(routes2, _account_inputs(DU8922142=nav), {"SPY": px},
                       claimed_regime="GOLDILOCKS")
    assert result2.passed is False


def test_missing_price_fails_closed(clean_regime):
    routes = [_route(symbol="SPY", total_qty=10, account="DU8922142")]
    result = rg.check(routes, _account_inputs(DU8922142=1_000_000.0),
                      {}, claimed_regime="GOLDILOCKS")   # no price for SPY
    assert result.passed is False
    assert any("no usable price" in r for r in result.reasons)


def test_missing_nav_fails_closed(clean_regime):
    routes = [_route(symbol="SPY", total_qty=10, account="DU8922142")]
    result = rg.check(routes, [{"account": "DU8922142", "net_liq": 0.0}],
                      {"SPY": 500.0}, claimed_regime="GOLDILOCKS")
    assert result.passed is False
    assert any("no NAV on file" in r for r in result.reasons)


def test_fa_block_split_charged_to_every_member_account(clean_regime):
    routes = [_route(route="fa_block", symbol="SPY", total_qty=20, account=None,
                     fa_group="tier_balanced",
                     per_account_split={"DU8922143": 12, "DU8922144": 8})]
    result = rg.check(routes,
                      _account_inputs(DU8922143=1_000_000.0, DU8922144=1_000_000.0),
                      {"SPY": 500.0}, claimed_regime="GOLDILOCKS")
    assert result.passed is True
    assert result.detail["notional_by_account"]["DU8922143"] == pytest.approx(6_000.0)
    assert result.detail["notional_by_account"]["DU8922144"] == pytest.approx(4_000.0)


# --- (c) regime cross-check --------------------------------------------------------
def test_regime_mismatch_fails_closed(monkeypatch):
    monkeypatch.setattr(rg, "compute_regime_now",
                        lambda: ("BEAR", "BEAR", "2026-07-09"))
    routes = [_route(symbol="SPY", total_qty=10)]
    result = rg.check(routes, _account_inputs(DU8922142=1_000_000.0),
                      {"SPY": 500.0}, claimed_regime="GOLDILOCKS")   # staged under a
                                                                     # DIFFERENT regime
    assert result.passed is False
    assert any("DRIFTED APART" in r for r in result.reasons)


def test_regime_compute_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(rg, "compute_regime_now", lambda: (None, None, None))
    routes = [_route(symbol="SPY", total_qty=10)]
    result = rg.check(routes, _account_inputs(DU8922142=1_000_000.0),
                      {"SPY": 500.0}, claimed_regime="GOLDILOCKS")
    assert result.passed is False
    assert any("could not compute today's regime" in r for r in result.reasons)


def test_no_claimed_regime_fails_closed(clean_regime):
    routes = [_route(symbol="SPY", total_qty=10)]
    result = rg.check(routes, _account_inputs(DU8922142=1_000_000.0),
                      {"SPY": 500.0}, claimed_regime=None)
    assert result.passed is False
    assert any("did not supply the regime" in r for r in result.reasons)


def test_regime_check_never_raises_even_on_exception(monkeypatch):
    def boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(rg, "compute_regime_now", boom)
    routes = [_route(symbol="SPY", total_qty=10)]
    result = rg.check(routes, _account_inputs(DU8922142=1_000_000.0),
                      {"SPY": 500.0}, claimed_regime="GOLDILOCKS")
    assert result.passed is False
    assert any("internal error" in r for r in result.reasons)


# --- guard never raises past its own boundary -------------------------------------
def test_check_never_raises_on_malformed_route(clean_regime):
    """A route missing an expected attribute should degrade to a failing reason, not
    blow up the caller (nightly_monitor_run must never crash mid-cycle on a guard bug)."""
    class _BrokenRoute:
        symbol = "SPY"
        # no total_qty / per_account_split / account -> getattr(...) paths must cope
    routes = [_BrokenRoute()]
    result = rg.check(routes, _account_inputs(DU8922142=1_000_000.0),
                      {"SPY": 500.0}, claimed_regime="GOLDILOCKS")
    assert result.passed is False
