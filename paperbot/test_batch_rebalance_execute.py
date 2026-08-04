"""test_batch_rebalance_execute.py — offline unit tests for the MULTI-ACCOUNT BATCH REBALANCE
executor's PURE, broker-free surface (batch_rebalance_execute): roster->version resolution,
per-account request assembly (reusing crm_execute), the out-of-spec subset selection, the
self-computed per-account margin pre-flight line (#57), the aggregate summary, and the
roster-scoped account wall.

ZERO real transmit. NO broker, NO gateway, NO network, NEVER the real CRM DB. Plans are
SYNTHETIC AccountPlan-shaped objects; PREVIEW runs with no `ib`, so execute_plan physically
cannot place an order. These pin that the batch stays sandboxed to the roster and sends
nothing without an explicit arm.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_batch_rebalance_execute.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

import batch_rebalance_execute as bre
import config
import crm_roster
import roster
import safe_execute as se
import strategy_target

ACCT_A = "DU8922142"      # Conservative in ENROLLMENT
ACCT_B = "DU8922143"      # Balanced in ENROLLMENT
ACCT_C = "DU8922145"      # Growth in ENROLLMENT
ACCT_Z = "DU9999999"      # not enrolled anywhere


# --- synthetic fixtures (NEVER the real CRM DB / broker) ----------------------------
def _target(version, weights, prices):
    return strategy_target.Target(
        weights=pd.Series(weights), prices=pd.Series(prices),
        as_of=pd.Timestamp("2026-07-28"), price_date=pd.Timestamp("2026-07-28"),
        version=version)


def _plan(account, version, orders, *, net_liq=1_000_000.0, investable=985_000.0):
    """A minimal AccountPlan-shaped object (matches the fields the engine/adapter read)."""
    return SimpleNamespace(
        account=account, version=version, net_liq=net_liq, reserve=0.0,
        investable=investable, lines=[], needs_rebalance=bool(orders),
        orders=dict(orders), alien_lines=[])


def _prices():
    return {"VTI": 250.0, "RSP": 180.0, "BIL": 91.0}


def _targets():
    p = _prices()
    return {
        "Conservative": _target("Conservative", {"VTI": 0.5, "BIL": 0.5}, p),
        "Balanced": _target("Balanced", {"VTI": 0.7, "RSP": 0.3}, p),
    }


def _row(a, t, v):
    return SimpleNamespace(account=a, tag=t, value=v)


def _summary(account, net_liq="1000000", buying_power="1000000", total_cash="1000000"):
    return [_row(account, "NetLiquidation", net_liq),
            _row(account, "BuyingPower", buying_power),
            _row(account, "TotalCashValue", total_cash)]


ROSTER = [ACCT_A, ACCT_B]


# =====================================================================================
# roster -> version resolution (CRM not configured in the test env -> config fallback)
# =====================================================================================
def test_resolve_roster_versions_config_fallback(monkeypatch):
    # Force the CRM path OFF so we exercise the deterministic config.ENROLLMENT fallback.
    monkeypatch.setattr(crm_roster, "is_configured", lambda: False)
    versions = bre.resolve_roster_versions([ACCT_A, ACCT_B, ACCT_C])
    assert versions == {ACCT_A: "Conservative", ACCT_B: "Balanced", ACCT_C: "Growth"}


def test_resolve_roster_versions_unmapped_defaults_to_strategy_version(monkeypatch):
    monkeypatch.setattr(crm_roster, "is_configured", lambda: False)
    versions = bre.resolve_roster_versions([ACCT_Z])
    assert versions == {ACCT_Z: config.STRATEGY_VERSION}


# =====================================================================================
# build_batch_requests — one request per OUT-OF-SPEC roster account, roster-scoped wall
# =====================================================================================
def test_build_batch_requests_out_of_spec_subset_and_fields():
    plans = [
        _plan(ACCT_A, "Conservative", {"VTI": 10, "BIL": -3}),
        _plan(ACCT_B, "Balanced", {"RSP": 4}),
        _plan(ACCT_Z, "Balanced", {"VTI": 0, "RSP": 0}),   # in-band -> skipped
    ]
    reqs = bre.build_batch_requests(
        plans, targets=_targets(), quotes={}, prices=_prices(),
        roster_accounts=ROSTER, summaries={ACCT_A: _summary(ACCT_A)})

    # Only the two out-of-spec accounts produce a request; the all-zero account is dropped.
    assert [r.account for r in reqs] == [ACCT_A, ACCT_B]
    ra, rb = reqs
    assert ra.purpose == se.PURPOSE_REBALANCE and ra.conform is False
    assert ra.allowed_accounts == ROSTER          # the account wall = the roster
    assert ra.armed is False and ra.kill is False  # preview by default
    assert ra.summary == _summary(ACCT_A)
    assert rb.summary == []                        # no summary passed for B


def test_build_batch_requests_armed_and_kill_threaded():
    plans = [_plan(ACCT_A, "Conservative", {"VTI": 10})]
    reqs = bre.build_batch_requests(
        plans, targets=_targets(), quotes={}, prices=_prices(),
        roster_accounts=ROSTER, armed=True, kill=True)
    assert reqs[0].armed is True and reqs[0].kill is True


# =====================================================================================
# PREVIEW end-to-end (no ib) — transmits nothing; legs match; wall refuses off-roster
# =====================================================================================
def test_preview_transmits_nothing_and_legs_match():
    plans = [
        _plan(ACCT_A, "Conservative", {"VTI": 10, "BIL": -3}),
        _plan(ACCT_B, "Balanced", {"RSP": 4}),
    ]
    reqs = bre.build_batch_requests(
        plans, targets=_targets(), quotes={}, prices=_prices(), roster_accounts=ROSTER,
        summaries={ACCT_A: _summary(ACCT_A), ACCT_B: _summary(ACCT_B)})
    results = [se.execute_plan(r, mode=se.MODE_PREVIEW) for r in reqs]

    for res in results:
        assert res.status == se.STATUS_PREVIEW_ONLY       # never armed -> preview only
        assert res.sell_results == [] and res.buy_results == []
        assert any("not armed" in reason for reason in res.reasons)
        assert not any("conform intent absent" in reason for reason in res.reasons)

    legs_a = {(l.symbol, l.side, l.qty) for l in results[0].legs}
    assert legs_a == {("VTI", "BUY", 10), ("BIL", "SELL", 3)}
    legs_b = {(l.symbol, l.side, l.qty) for l in results[1].legs}
    assert legs_b == {("RSP", "BUY", 4)}


def test_off_roster_account_refused_by_wall():
    # A plan for an account NOT on the roster still builds a request (planner output), but the
    # engine's account wall independently refuses it — the wall never trusts the planner.
    plans = [_plan(ACCT_Z, "Balanced", {"RSP": 4})]
    reqs = bre.build_batch_requests(
        plans, targets=_targets(), quotes={}, prices=_prices(), roster_accounts=ROSTER)
    res = se.execute_plan(reqs[0], mode=se.MODE_PREVIEW)
    assert any("not in the enrolled execution roster" in reason for reason in res.reasons)
    assert res.sell_results == [] and res.buy_results == []


# =====================================================================================
# margin pre-flight line (#57) — unlevered book passes on any account type, zero reasons
# =====================================================================================
def test_margin_preflight_line_unlevered_passes():
    plans = [_plan(ACCT_A, "Conservative", {"VTI": 10})]
    reqs = bre.build_batch_requests(
        plans, targets=_targets(), quotes={}, prices=_prices(), roster_accounts=ROSTER,
        summaries={ACCT_A: _summary(ACCT_A)})
    res = se.execute_plan(reqs[0], mode=se.MODE_PREVIEW)
    ok, reason = bre.margin_preflight_line(reqs[0], res)
    assert ok is True and reason == ""


# =====================================================================================
# aggregate summary
# =====================================================================================
def test_summarize_batch_counts_and_notionals():
    plans = [
        _plan(ACCT_A, "Conservative", {"VTI": 10, "BIL": -3}),
        _plan(ACCT_B, "Balanced", {"RSP": 4}),
        _plan(ACCT_Z, "Balanced", {"VTI": 0}),   # in-band
    ]
    reqs = bre.build_batch_requests(
        plans, targets=_targets(), quotes={}, prices=_prices(), roster_accounts=ROSTER)
    results = [se.execute_plan(r, mode=se.MODE_PREVIEW) for r in reqs]
    summary = bre.summarize_batch(plans, reqs, results)
    assert summary["n_roster"] == 3
    assert summary["n_out_of_spec"] == 2
    assert summary["n_in_spec"] == 1
    assert summary["total_legs"] == 3            # VTI+BIL for A, RSP for B
    assert summary["total_sells"] > 0 and summary["total_buys"] > 0


# =====================================================================================
# FA-block whatIf stub stays OFF (no non-PyPI dependency wired) and is inert
# =====================================================================================
def test_fa_block_whatif_is_stubbed_off():
    assert bre.FA_BLOCK_WHATIF_ENABLED is False
    import pytest
    with pytest.raises(NotImplementedError):
        bre.fa_block_whatif_preflight()
