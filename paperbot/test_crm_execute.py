"""test_crm_execute.py — offline unit tests for the PURE CRM->engine adapter
(crm_execute.requests_from_crm_plan / preview_crm) and the roster accessor
(roster.enrolled_roster), plus the generalized multi-account wall wording
(safe_execute.account_wall_ok). Control Plane multi-account, conductor #64/#66,
spec docs/PRODUCTION_REBALANCE_CONTROL_PLANE.md §6/§7 (Phase 3 SAFE slice).

ZERO real transmit. NO broker, NO gateway, NO network, NEVER the real CRM DB. The CRM
what-if is a SYNTHETIC dict of minimal AccountPlan-shaped objects built in-test. PREVIEW
runs with no `ib`, so execute_plan physically cannot place an order.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_crm_execute.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

import config
import crm_execute
import roster
import safe_execute as se
import strategy_target

ACCT_A = "DU8922142"      # Conservative in ENROLLMENT
ACCT_B = "DU8922143"      # Balanced in ENROLLMENT
ACCT_Z = "DU9999999"      # not enrolled anywhere


# --- synthetic fixtures (NEVER the real CRM DB) -------------------------------------
def _target(version, weights, prices):
    return strategy_target.Target(
        weights=pd.Series(weights), prices=pd.Series(prices),
        as_of=pd.Timestamp("2026-07-28"), price_date=pd.Timestamp("2026-07-28"),
        version=version)


def _plan(account, version, orders, *, net_liq=1_000_000.0, investable=985_000.0):
    """A minimal AccountPlan-shaped object (matches recon_report.AccountPlan's fields the
    engine reads: account/version/net_liq/investable/orders/alien_lines)."""
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


def _crm_result():
    """Synthetic plan_from_crm-shaped result: two tradeable accounts + one all-zero (in-band)
    account that must be skipped."""
    return {
        "plans": [
            _plan(ACCT_A, "Conservative", {"VTI": 10, "BIL": -3}),
            _plan(ACCT_B, "Balanced", {"RSP": 4}),
            _plan(ACCT_Z, "Balanced", {"VTI": 0, "RSP": 0}),   # in-band -> skipped
        ],
        "blocks": [], "routes": [], "skipped_option_sleeves": [], "flags": [],
    }


ROSTER = [ACCT_A, ACCT_B]


# --- roster accessor ----------------------------------------------------------------
def test_enrolled_roster_is_sorted_deduped_config_keys():
    r = roster.enrolled_roster()
    assert r == sorted(set(config.ENROLLMENT))
    assert r == sorted(r) and len(r) == len(set(r))       # sorted + de-duped
    assert ACCT_A in r and ACCT_B in r                    # the human-blessed accounts


# --- (a) one request per non-empty-orders account, correct field mapping ------------
def test_requests_one_per_tradeable_account_with_field_mapping():
    crm = _crm_result()
    targets = _targets()
    prices = _prices()
    reqs = crm_execute.requests_from_crm_plan(
        crm, targets=targets, quotes={}, prices=prices, roster=ROSTER)

    assert [r.account for r in reqs] == [ACCT_A, ACCT_B]   # ACCT_Z (all-zero) skipped
    ra, rb = reqs
    # account / version / plan / target / roster / conform mapping
    assert ra.account == ACCT_A and ra.strategy_version == "Conservative"
    assert ra.plan is crm["plans"][0]
    assert ra.target is targets["Conservative"]
    assert ra.allowed_accounts == ROSTER
    assert ra.conform is False                             # ongoing rebalance, NOT a deploy
    assert ra.purpose == se.PURPOSE_REBALANCE              # ongoing rebalance lane
    assert ra.armed is False and ra.kill is False
    assert ra.run_id is None
    assert ra.net_liq == crm["plans"][0].net_liq
    assert ra.summary == []                                # no summaries passed
    assert isinstance(ra.caps, se.ExecutionCaps)
    assert rb.account == ACCT_B and rb.target is targets["Balanced"]


# --- (b) an all-zero-orders account is skipped --------------------------------------
def test_zero_orders_account_skipped():
    crm = _crm_result()
    reqs = crm_execute.requests_from_crm_plan(
        crm, targets=_targets(), quotes={}, prices=_prices(), roster=ROSTER)
    assert ACCT_Z not in [r.account for r in reqs]


def test_summaries_are_threaded_per_account():
    crm = _crm_result()
    rows = [SimpleNamespace(account=ACCT_A, tag="BuyingPower", value="5000")]
    reqs = crm_execute.requests_from_crm_plan(
        crm, targets=_targets(), quotes={}, prices=_prices(), roster=ROSTER,
        summaries={ACCT_A: rows})
    by_acct = {r.account: r for r in reqs}
    assert by_acct[ACCT_A].summary is rows
    assert by_acct[ACCT_B].summary == []


# --- (c) preview_crm returns per-account ExecutionResults; legs match; no transmit ---
def test_preview_crm_legs_match_orders_and_transmit_nothing():
    crm = _crm_result()
    results = crm_execute.preview_crm(
        crm, targets=_targets(), quotes={}, prices=_prices(), roster=ROSTER)

    assert len(results) == 2                               # one per tradeable account
    for res in results:
        # PREVIEW transmits nothing: no armed transmit ran, no fills recorded.
        assert res.status == se.STATUS_PREVIEW_ONLY
        assert res.sell_results == [] and res.buy_results == []
        # REBALANCE lane + not armed -> "not armed" is expected; "conform intent absent" is
        # NOT emitted (the rebalance lane does not require conform).
        assert any("not armed" in r for r in res.reasons)
        assert not any("conform intent absent" in r for r in res.reasons)

    # legs are the plan's non-zero orders as (symbol, side, qty), sells before buys.
    legs_a = {(l.symbol, l.side, l.qty) for l in results[0].legs}
    assert legs_a == {("VTI", "BUY", 10), ("BIL", "SELL", 3)}
    legs_b = {(l.symbol, l.side, l.qty) for l in results[1].legs}
    assert legs_b == {("RSP", "BUY", 4)}


# --- (d) wall: out-of-roster refused with the generalized string; in-roster passes ---
def test_account_wall_generalized_multi_account_string():
    ok, reason = se.account_wall_ok(ACCT_Z, ROSTER)
    assert ok is False
    assert (f"target account {ACCT_Z} is not in the enrolled execution roster "
            f"{{{ACCT_A}, {ACCT_B}}} — refusing.") == reason


def test_account_wall_in_roster_passes():
    ok, reason = se.account_wall_ok(ACCT_A, ROSTER)
    assert ok is True and reason == ""


def test_account_wall_single_account_wording_unchanged():
    # len==1 branch must stay byte-identical to the deploy wall (parity guard).
    ok, reason = se.account_wall_ok(ACCT_Z, [ACCT_A])
    assert ok is False
    assert reason == (f"target account {ACCT_Z} is not the single allowed account "
                      f"{ACCT_A} — refusing.")
