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

import os
from types import SimpleNamespace

import pandas as pd

import batch_rebalance_execute as bre
import config
import crm_roster
import custom_target
import custom_tier
import ledger
import rebalance_engine
import roster
import safe_execute as se
import strategy_target

ACCT_A = "DU8922142"      # Conservative in ENROLLMENT
ACCT_B = "DU8922143"      # Balanced in ENROLLMENT
ACCT_C = "DU8922145"      # Growth in ENROLLMENT
ACCT_Z = "DU9999999"      # not enrolled anywhere


# --- synthetic fixtures (NEVER the real CRM DB / broker) ----------------------------
def _roster_scan(accounts, *, held=(), unfunded=(), models=(), scope=(), source="config"):
    """A roster.enrolled_roster_scan-shaped result. main() now resolves the roster through the
    SCAN (the allow-list plus the no-trade holds, the unfunded accounts, the model list and the
    scope actually applied) so it can report what was excluded, so this is the seam the tests
    patch."""
    return {"accounts": list(accounts), "held": list(held), "unfunded": list(unfunded),
            "models": list(models), "scope": list(scope), "source": source}


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


# =====================================================================================
# ANDREW-AUTHORED ("CUSTOM") ALLOCATIONS — Stage 5 wiring + Stage 6 audit trail (v0.37.0)
# =====================================================================================
# NEVER the real CRM: every test below fakes crm_roster's two custom-allocation reads and
# builds Targets from synthetic rows through custom_target's OWN builder (so the real
# row->Target code runs, with no database and no price files). Nothing connects, nothing
# transmits, and no allocation is created, modified or published anywhere.

CUSTOM_LABEL = "Growth (Custom)"
CUSTOM_ACCT = "DU8922150"


def _alloc_rows(label, pairs, *, version_number=7, version_id="ver-7-uuid",
                effective_from="2026-08-20", published_at="2026-08-20T14:03:11+00:00"):
    """Rows shaped exactly like v_tradingdesk_custom_allocations returns them."""
    return [{"strategy_name": label, "strategy_code": "GROWTH_CUSTOM", "ticker": t,
             "weight_pct": pct, "version_number": version_number,
             "effective_from": effective_from, "published_at": published_at,
             "version_id": version_id, "strategy_id": "strat-uuid"}
            for t, pct in pairs]


def _price_frame(tickers, last="2026-08-24"):
    idx = pd.to_datetime(["2026-08-21", "2026-08-22", last])
    return pd.DataFrame({t: [100.0, 100.0, 100.0] for t in tickers}, index=idx)


def _custom_target_and_meta(label=CUSTOM_LABEL, pairs=(("SCHB", 60.0), ("USFR", 40.0)),
                            **kw):
    """A REAL custom_target.Target/AllocationMeta pair built from synthetic rows."""
    rows = _alloc_rows(label, list(pairs), **kw)
    return custom_target.build_target(rows, label,
                                      prices=_price_frame([t for t, _ in pairs]))


def _fake_crm(monkeypatch, published_labels, rows_by_label=None):
    """Point BOTH custom-allocation reads at synthetic data and declare the CRM configured."""
    monkeypatch.setattr(crm_roster, "is_configured", lambda: True)
    monkeypatch.setattr(crm_roster, "custom_allocation_labels",
                        lambda conn=None: set(published_labels))
    rows_by_label = rows_by_label or {}

    def _fetch(strategy_names=None, conn=None):
        names = list(strategy_names) if strategy_names is not None else list(rows_by_label)
        out = []
        for n in names:
            out.extend(rows_by_label.get(str(n), []))
        return out

    monkeypatch.setattr(crm_roster, "fetch_custom_allocations", _fetch)


# -------------------------------------------------------------------------------------
# STAGE 5 (1) — TARGET DISPATCH: custom labels built from the CRM, S0 labels unchanged,
# and a custom target that cannot be built fails EXACTLY as loudly as an S0 one.
# -------------------------------------------------------------------------------------
def test_split_versions_is_source_based_not_name_based(monkeypatch):
    # "Growth (Custom)" is custom because the CRM says so; "Balanced (Custom)" is NOT,
    # despite the identical naming convention, because nothing is published for it.
    _fake_crm(monkeypatch, {CUSTOM_LABEL})
    custom, other = bre.split_versions([CUSTOM_LABEL, "Balanced (Custom)", "Growth"])
    assert custom == [CUSTOM_LABEL]
    assert other == ["Balanced (Custom)", "Growth"]


def test_split_versions_no_crm_means_no_custom_models(monkeypatch):
    # The config.ENROLLMENT fallback environment: no CRM -> nothing is custom, no read made.
    monkeypatch.setattr(crm_roster, "is_configured", lambda: False)

    def _must_not_read(conn=None):
        raise AssertionError("no CRM read may be attempted when the CRM is unconfigured")

    monkeypatch.setattr(crm_roster, "custom_allocation_labels", _must_not_read)
    assert bre.split_versions([CUSTOM_LABEL, "Growth"]) == ([], [CUSTOM_LABEL, "Growth"])


def test_build_targets_dispatches_custom_to_crm_and_s0_to_backtester(monkeypatch):
    _fake_crm(monkeypatch, {CUSTOM_LABEL},
              {CUSTOM_LABEL: _alloc_rows(CUSTOM_LABEL, [("SCHB", 60.0), ("USFR", 40.0)])})
    ct, cmeta = _custom_target_and_meta()
    monkeypatch.setattr(custom_target, "custom_targets_with_meta",
                        lambda labels, conn=None: {CUSTOM_LABEL: (ct, cmeta)})
    s0_calls = []

    def _s0(version):
        s0_calls.append(version)
        return _target(version, {"VTI": 1.0}, _prices())

    monkeypatch.setattr(strategy_target, "current_target", _s0)

    targets, metas = bre.build_targets([CUSTOM_LABEL, "Balanced"])

    # The custom label NEVER reached strategy_target (trap 1: no backtester, no small_tier).
    assert s0_calls == ["Balanced"]
    assert set(targets) == {CUSTOM_LABEL, "Balanced"}
    assert targets[CUSTOM_LABEL] is ct
    # Only the custom label carries a published-allocation identity.
    assert set(metas) == {CUSTOM_LABEL}
    assert metas[CUSTOM_LABEL].version_number == 7


def test_build_targets_custom_failure_fails_closed(monkeypatch):
    """A custom target that cannot be built RAISES naming the label — it must never fall
    through to the S0 backtester and must never become an empty (liquidating) book."""
    import pytest
    _fake_crm(monkeypatch, {CUSTOM_LABEL})

    def _boom(labels, conn=None):
        raise custom_target.CustomAllocationError("weights sum to 90%, not 100%")

    def _no_fallback(version):
        raise AssertionError("S0 fallback must not be reached for a custom label")

    monkeypatch.setattr(custom_target, "custom_targets_with_meta", _boom)
    monkeypatch.setattr(strategy_target, "current_target", _no_fallback)
    with pytest.raises(bre.TargetBuildFailed) as exc:
        bre.build_targets([CUSTOM_LABEL])
    assert exc.value.label == CUSTOM_LABEL


def test_build_targets_custom_label_with_no_target_fails_closed(monkeypatch):
    """split said it is custom; the builder returned nothing for it. The two CRM reads
    disagree -> refuse, never size the account against something else."""
    import pytest
    _fake_crm(monkeypatch, {CUSTOM_LABEL})
    monkeypatch.setattr(custom_target, "custom_targets_with_meta",
                        lambda labels, conn=None: {})
    with pytest.raises(bre.TargetBuildFailed) as exc:
        bre.build_targets([CUSTOM_LABEL])
    assert exc.value.label == CUSTOM_LABEL


def test_main_refuses_and_connects_nothing_when_a_custom_target_fails(monkeypatch):
    """The pre-existing fail-closed contract (COULD NOT BUILD TARGET -> rc 2, connects to
    nothing) applies identically to a custom allocation."""
    monkeypatch.setattr(roster, "enrolled_roster_scan",
                        lambda models=None: _roster_scan([CUSTOM_ACCT]))
    monkeypatch.setattr(bre, "resolve_roster_versions",
                        lambda accts: {CUSTOM_ACCT: CUSTOM_LABEL})

    def _boom(versions):
        raise bre.TargetBuildFailed(CUSTOM_LABEL,
                                    custom_target.NoCustomAllocation("nothing published"))

    def _must_not_connect(*a, **k):
        raise AssertionError("MUST NOT CONNECT after a target build failure")

    monkeypatch.setattr(bre, "build_targets", _boom)
    monkeypatch.setattr(bre.s0_live, "connect_s0_live", _must_not_connect)
    monkeypatch.setattr(bre.s0_live, "connect_s0_live_armed", _must_not_connect)
    assert bre.main(armed=True) == 2


# -------------------------------------------------------------------------------------
# STAGE 5 (2) — a CUSTOM label NEVER enters the S0 (small_tier) NAV override. The split is
# SOURCE-based, so a CRM RENAME cannot re-point the account onto an S0 model. The pair below
# uses the SAME label string and the SAME NAV: the only difference is whether the CRM says an
# allocation is published under it.
#
# 0.39.0: "not small_tier" is not "no check". A custom account is now re-tiered by its OWN
# ladder (custom_tier), which can only ever emit one of the seven custom labels. See the
# STAGE 5 (2b) block below.
# -------------------------------------------------------------------------------------
RENAMED_CUSTOM = "Growth (Small)"      # what a careless CRM rename could produce


def _tier_setup(monkeypatch, model, nav, *, published, row_extra=None):
    bre._TIER_MISMATCHES.clear()
    _fake_crm(monkeypatch, published)
    row = {"account_number": CUSTOM_ACCT, "model": model, "total_value": nav}
    row.update(row_extra or {})
    monkeypatch.setattr(crm_roster, "fetch_roster",
                        lambda advisor_name=None, model=None, conn=None: [row])


def test_custom_label_is_never_re_tiered_by_nav(monkeypatch):
    # A $5M account on a label that LOOKS like the S0 small tier. Because the CRM publishes an
    # allocation under it, it is Andrew's hand-authored book: the S0 override is skipped, and
    # custom_tier refuses it too (the label is not one of the seven), so it passes through
    # VERBATIM. This is the original hazard, still nailed shut.
    _tier_setup(monkeypatch, RENAMED_CUSTOM, 5_000_000.0, published={RENAMED_CUSTOM})
    versions = bre.resolve_roster_versions([CUSTOM_ACCT])
    assert versions == {CUSTOM_ACCT: RENAMED_CUSTOM}
    assert bre._TIER_MISMATCHES == {}


def test_same_label_is_re_tiered_when_it_is_not_a_custom_allocation(monkeypatch):
    # CONTROL for the test above: identical label, identical NAV, but nothing published ->
    # the S0 NAV override runs and REWRITES the account's label to the full-size model.
    _tier_setup(monkeypatch, RENAMED_CUSTOM, 5_000_000.0, published=set())
    versions = bre.resolve_roster_versions([CUSTOM_ACCT])
    assert versions == {CUSTOM_ACCT: "Growth"}          # rewritten
    assert CUSTOM_ACCT in bre._TIER_MISMATCHES
    bre._TIER_MISMATCHES.clear()


def test_custom_small_label_is_not_demoted_onto_an_s0_proxy(monkeypatch):
    # WAS "is not demoted either" (pre-0.39.0, when a custom label skipped the check
    # entirely). A tiny-NAV custom account IS now re-tiered — down the custom ladder to the
    # 2-line Starter book, which is the whole reason that book exists. What must still never
    # happen is a collapse onto the S0 whole-share proxy "Growth (Small)".
    _tier_setup(monkeypatch, "Growth (Small, Custom)", 1_000.0,
                published={"Growth (Small, Custom)"})
    assert bre.resolve_roster_versions([CUSTOM_ACCT]) == {CUSTOM_ACCT: "Starter (Custom)"}
    assert bre._TIER_MISMATCHES[CUSTOM_ACCT] == (
        "Growth (Small, Custom)", "Starter (Custom)", 1_000.0)
    bre._TIER_MISMATCHES.clear()


# -------------------------------------------------------------------------------------
# STAGE 5 (2b) / 0.39.0-0.40.0 — the CUSTOM ladder wired into resolve_roster_versions. The
# pure rule itself is pinned threshold-by-threshold in test_custom_tier.py; these pin the
# WIRING: the right inputs (NAV + BOTH CRM history columns) reach it, its answer is used, and
# the mismatch is surfaced.
# -------------------------------------------------------------------------------------
def test_custom_account_in_the_band_is_left_alone(monkeypatch):
    # $24,000 on the small book sits INSIDE the 22,500/27,500 band: below the plain 25,000
    # boundary a first assignment would use, and below the 27,500 promote level. The
    # incumbent holds and nothing is flagged as stale — that is the band doing its job.
    _tier_setup(monkeypatch, "Balanced (Small, Custom)", 24_000.0,
                published={"Balanced (Small, Custom)"})
    assert bre.resolve_roster_versions([CUSTOM_ACCT]) == {
        CUSTOM_ACCT: "Balanced (Small, Custom)"}
    assert bre._TIER_MISMATCHES == {}


def test_custom_account_is_promoted_to_the_full_book_and_flagged(monkeypatch):
    _tier_setup(monkeypatch, "Conservative (Small, Custom)", 30_000.0,
                published={"Conservative (Small, Custom)"})
    assert bre.resolve_roster_versions([CUSTOM_ACCT]) == {
        CUSTOM_ACCT: "Conservative (Custom)"}      # risk level preserved
    assert bre._TIER_MISMATCHES[CUSTOM_ACCT] == (
        "Conservative (Small, Custom)", "Conservative (Custom)", 30_000.0)
    bre._TIER_MISMATCHES.clear()


def test_starter_account_promoted_with_no_history_goes_to_growth(monkeypatch):
    # prior_custom_risk_level NULL -> Andrew's GROWTH default (2026-08-25), applied silently
    # and deliberately: never flagged, never skipped.
    _tier_setup(monkeypatch, "Starter (Custom)", 6_000.0, published={"Starter (Custom)"},
                row_extra={custom_tier.HAS_PRIOR_FIELD: True,
                           custom_tier.PRIOR_RISK_FIELD: None})
    assert bre.resolve_roster_versions([CUSTOM_ACCT]) == {
        CUSTOM_ACCT: "Growth (Small, Custom)"}
    bre._TIER_MISMATCHES.clear()


def test_starter_account_promoted_recovers_the_risk_level_from_the_crm(monkeypatch):
    _tier_setup(monkeypatch, "Starter (Custom)", 6_000.0, published={"Starter (Custom)"},
                row_extra={custom_tier.HAS_PRIOR_FIELD: True,
                           custom_tier.PRIOR_RISK_FIELD: "Balanced"})
    assert bre.resolve_roster_versions([CUSTOM_ACCT]) == {
        CUSTOM_ACCT: "Balanced (Small, Custom)"}
    bre._TIER_MISMATCHES.clear()


def test_starter_funded_to_30000_jumps_straight_to_the_full_book(monkeypatch):
    # 0.40.0, Andrew's call: two rungs -> direct, no band, ONE step. The old ladder would
    # have traded this account into the 11-line small book tonight and the 15-line full book
    # tomorrow — spread paid twice for a book nobody chose.
    _tier_setup(monkeypatch, "Starter (Custom)", 30_000.0, published={"Starter (Custom)"},
                row_extra={custom_tier.HAS_PRIOR_FIELD: True,
                           custom_tier.PRIOR_RISK_FIELD: "Balanced"})
    assert bre.resolve_roster_versions([CUSTOM_ACCT]) == {CUSTOM_ACCT: "Balanced (Custom)"}
    assert bre._TIER_MISMATCHES[CUSTOM_ACCT] == (
        "Starter (Custom)", "Balanced (Custom)", 30_000.0)
    bre._TIER_MISMATCHES.clear()


def test_full_book_collapsed_to_1000_drops_straight_to_starter(monkeypatch):
    _tier_setup(monkeypatch, "Growth (Custom)", 1_000.0, published={"Growth (Custom)"},
                row_extra={custom_tier.HAS_PRIOR_FIELD: True})
    assert bre.resolve_roster_versions([CUSTOM_ACCT]) == {CUSTOM_ACCT: "Starter (Custom)"}
    bre._TIER_MISMATCHES.clear()


def test_a_freshly_assigned_4900_account_matches_the_crm(monkeypatch):
    # THE DIVERGENCE has_prior_custom_assignment FIXES. Same label, same NAV; only the flag
    # differs. False = first assignment -> plain 5,000 boundary -> Starter, which is what the
    # CRM scan produces. True = incumbent -> the 4,500 band -> stay. Before 0.40.0 the desk
    # always took the second branch and disagreed with the CRM on a brand-new account.
    _tier_setup(monkeypatch, "Growth (Small, Custom)", 4_900.0,
                published={"Growth (Small, Custom)"},
                row_extra={custom_tier.HAS_PRIOR_FIELD: False})
    assert bre.resolve_roster_versions([CUSTOM_ACCT]) == {CUSTOM_ACCT: "Starter (Custom)"}
    bre._TIER_MISMATCHES.clear()

    _tier_setup(monkeypatch, "Growth (Small, Custom)", 4_900.0,
                published={"Growth (Small, Custom)"},
                row_extra={custom_tier.HAS_PRIOR_FIELD: True})
    assert bre.resolve_roster_versions([CUSTOM_ACCT]) == {
        CUSTOM_ACCT: "Growth (Small, Custom)"}
    assert bre._TIER_MISMATCHES == {}


def test_a_lagging_roster_view_fails_toward_the_incumbent(monkeypatch):
    # Neither history column present (the view lags the code). The account holds a label, so
    # it is treated as an INCUMBENT and the band applies — it is NOT silently re-tiered off a
    # plain boundary. Nothing raises.
    _tier_setup(monkeypatch, "Growth (Small, Custom)", 4_900.0,
                published={"Growth (Small, Custom)"})
    assert bre.resolve_roster_versions([CUSTOM_ACCT]) == {
        CUSTOM_ACCT: "Growth (Small, Custom)"}
    assert bre._TIER_MISMATCHES == {}


def test_a_custom_account_with_no_nav_is_left_alone(monkeypatch):
    # No NAV to decide with -> no re-tier. Never guess a model from a missing balance.
    _tier_setup(monkeypatch, "Growth (Custom)", None, published={"Growth (Custom)"})
    assert bre.resolve_roster_versions([CUSTOM_ACCT]) == {CUSTOM_ACCT: "Growth (Custom)"}
    assert bre._TIER_MISMATCHES == {}


def test_the_custom_ladder_never_emits_an_s0_model(monkeypatch):
    # Sweep every custom incumbent across every boundary through the REAL wiring; nothing
    # that comes out may be an S0 label.
    s0 = {"Growth", "Balanced", "Conservative",
          "Growth (Small)", "Balanced (Small)", "Conservative (Small)"}
    for label in ("Growth (Custom)", "Balanced (Custom)", "Conservative (Custom)",
                  "Growth (Small, Custom)", "Balanced (Small, Custom)",
                  "Conservative (Small, Custom)", "Starter (Custom)"):
        for nav in (0.0, 4_499.0, 4_500.0, 5_500.0, 22_499.0, 27_500.0, 5_000_000.0):
            _tier_setup(monkeypatch, label, nav, published={label})
            got = bre.resolve_roster_versions([CUSTOM_ACCT])[CUSTOM_ACCT]
            assert got not in s0, f"{label} @ {nav} -> {got}"
            assert custom_tier.is_custom_family(got)
    bre._TIER_MISMATCHES.clear()


# -------------------------------------------------------------------------------------
# STAGE 5 (3) — THE UNIVERSE TRAP. A ticker Andrew REMOVES from his allocation must be SOLD.
# With S0's universe it classifies ALIEN, ALIEN never breaches the band and never produces a
# delta, so the rotation would silently do nothing.
# -------------------------------------------------------------------------------------
S0_UNIVERSE = {"VTI", "RSP", "BIL", "SPY", "SCHB", "USFR"}


def test_account_universe_s0_model_is_unchanged():
    t = _target("Balanced", {"VTI": 0.7, "RSP": 0.3}, _prices())
    assert bre.account_universe(t, None, {"VTI": 10}, base=S0_UNIVERSE) is S0_UNIVERSE


def test_account_universe_custom_is_allocation_plus_held():
    t, meta = _custom_target_and_meta()
    uni = bre.account_universe(t, meta, {"SCHB": 10, "IWM": 5}, base=S0_UNIVERSE)
    # The allocation's own tickers PLUS what the account holds — the intended rotation set.
    assert uni == {"SCHB", "USFR", "IWM"}
    assert "VTI" not in uni       # S0's universe is deliberately NOT unioned in


def _removed_ticker_plan(universe):
    """Account holds IWM, which Andrew has REMOVED from the published allocation."""
    t, _meta = _custom_target_and_meta()
    positions = {"SCHB": 600, "USFR": 400, "IWM": 100}
    prices = {"SCHB": 100.0, "USFR": 100.0, "IWM": 100.0}
    return rebalance_engine.plan_account(
        CUSTOM_ACCT, t.version, 110_000.0, positions, t, prices=prices, universe=universe)


def test_removed_custom_ticker_produces_a_sell():
    t, meta = _custom_target_and_meta()
    uni = bre.account_universe(t, meta, {"SCHB": 600, "USFR": 400, "IWM": 100})
    plan = _removed_ticker_plan(uni)
    assert plan.orders.get("IWM", 0) < 0          # SOLD — the rotation actually happens
    assert not plan.alien_lines


def test_removed_custom_ticker_is_silently_ignored_under_the_s0_universe():
    """THE TRAP, pinned. Same account, same allocation, S0's universe: IWM is ALIEN, so it
    never breaches the band and never produces a delta. This is what change (3) fixes."""
    plan = _removed_ticker_plan(S0_UNIVERSE - {"IWM"})
    assert "IWM" not in plan.orders
    assert [ln.symbol for ln in plan.alien_lines] == ["IWM"]


# -------------------------------------------------------------------------------------
# STAGE 5 (4) — targets[plan.version] must resolve for a custom label. The failure mode is a
# hard KeyError in crm_execute, not a graceful skip.
# -------------------------------------------------------------------------------------
def test_targets_resolve_for_a_custom_plan_version():
    t, _meta = _custom_target_and_meta()
    assert t.version == CUSTOM_LABEL          # Target.version IS the roster label, verbatim
    plans = [_plan(CUSTOM_ACCT, CUSTOM_LABEL, {"SCHB": 5, "IWM": -100})]
    reqs = bre.build_batch_requests(
        plans, targets={CUSTOM_LABEL: t}, quotes={}, prices={"SCHB": 100.0, "IWM": 100.0},
        roster_accounts=[CUSTOM_ACCT])
    assert reqs[0].target is t
    assert reqs[0].strategy_version == CUSTOM_LABEL


def test_build_batch_requests_stamps_the_batch_run_id():
    t, _meta = _custom_target_and_meta()
    plans = [_plan(CUSTOM_ACCT, CUSTOM_LABEL, {"SCHB": 5})]
    reqs = bre.build_batch_requests(
        plans, targets={CUSTOM_LABEL: t}, quotes={}, prices={"SCHB": 100.0},
        roster_accounts=[CUSTOM_ACCT], run_id="20260825T101112")
    assert reqs[0].run_id == "20260825T101112"


# -------------------------------------------------------------------------------------
# STAGE 6 — THE AUDIT TRAIL: one ledger record per batch run, and the join from a trade
# back to the exact published allocation version that produced it.
# -------------------------------------------------------------------------------------
def _preview(request):
    return se.execute_plan(request, mode=se.MODE_PREVIEW)


def _custom_request_and_result(run_id="20260825T101112"):
    t, meta = _custom_target_and_meta()
    plans = [_plan(CUSTOM_ACCT, CUSTOM_LABEL, {"SCHB": 5, "IWM": -100})]
    reqs = bre.build_batch_requests(
        plans, targets={CUSTOM_LABEL: t}, quotes={},
        prices={"SCHB": 100.0, "IWM": 100.0, "USFR": 100.0},
        roster_accounts=[CUSTOM_ACCT], summaries={CUSTOM_ACCT: _summary(CUSTOM_ACCT)},
        run_id=run_id)
    return reqs[0], _preview(reqs[0]), t, meta


def test_order_refs_are_byte_identical_to_the_transmit_path():
    """The recorded ref must be the ref the wire carries — built with safe_execute's own
    _deploy_ref, never re-implemented here."""
    req, res, t, _meta = _custom_request_and_result()
    refs = bre.order_refs_for(req, res, "20260825T101112")
    expected = [se._deploy_ref(CUSTOM_ACCT, t.as_of, l.side, l.symbol, "20260825T101112")
                for l in res.legs]
    assert refs == expected and refs


def test_run_id_round_trips_out_of_an_order_ref():
    ref = se._deploy_ref(CUSTOM_ACCT, pd.Timestamp("2026-08-20"), "SELL", "IWM",
                         "20260825T101112")
    assert bre.run_id_from_order_ref(ref) == "20260825T101112"
    # A ref with no run stamp yields None rather than a wrong answer.
    assert bre.run_id_from_order_ref(
        se._deploy_ref(CUSTOM_ACCT, pd.Timestamp("2026-08-20"), "SELL", "IWM")) is None


def test_account_audit_record_carries_the_allocation_version():
    req, res, t, meta = _custom_request_and_result()
    rec = bre.account_audit_record(req, res, model_label=CUSTOM_LABEL, target=t, meta=meta,
                                   run_id="20260825T101112", margin_ok=True)
    assert rec["account"] == CUSTOM_ACCT
    assert rec["model"] == CUSTOM_LABEL
    assert rec["is_custom_allocation"] is True
    # The identity the orderRef CANNOT carry (the ref holds only a date, and two allocations
    # can be published on the same day).
    assert rec["custom_version_number"] == 7
    assert rec["custom_version_id"] == "ver-7-uuid"
    assert rec["custom_effective_from"] == "2026-08-20"
    # The book itself, stored — the record stays re-derivable without the CRM.
    assert rec["target_weights"] == {"SCHB": 0.6, "USFR": 0.4}
    assert rec["target_as_of"] == "2026-08-20"
    assert {l["sym"] for l in rec["legs"]} == {"SCHB", "IWM"}
    assert rec["order_refs"] == bre.order_refs_for(req, res, "20260825T101112")


def test_account_audit_record_for_an_s0_model_has_no_allocation_version():
    """The trail is written for S0 runs too; the custom_* fields are simply absent, which is
    itself the record of 'computed model, not a hand-authored book'."""
    t = _target("Balanced", {"VTI": 0.7, "RSP": 0.3}, _prices())
    plans = [_plan(ACCT_A, "Balanced", {"VTI": 4})]
    reqs = bre.build_batch_requests(plans, targets={"Balanced": t}, quotes={},
                                    prices=_prices(), roster_accounts=[ACCT_A],
                                    run_id="RID")
    rec = bre.account_audit_record(reqs[0], _preview(reqs[0]), model_label="Balanced",
                                   target=t, meta=None, run_id="RID", margin_ok=True)
    assert rec["is_custom_allocation"] is False
    assert "custom_version_number" not in rec
    assert rec["target_weights"] == {"VTI": 0.7, "RSP": 0.3}      # weights still stored
    assert rec["order_refs"]


def test_batch_run_record_top_level_shape():
    req, res, t, meta = _custom_request_and_result()
    acct = bre.account_audit_record(req, res, model_label=CUSTOM_LABEL, target=t, meta=meta,
                                    run_id="20260825T101112", margin_ok=True)
    rec = bre.batch_run_record(
        run_id="20260825T101112", mode="BATCH_REBALANCE_PREVIEW", accounts=[acct],
        summary=bre.summarize_batch([req], [req], [res]),
        skipped=[], armed=False, kill=False, permitted=False)
    # The keys ledger.record_run's human log line reads, so paperbot.log stays scannable.
    for key in ("mode", "account", "nav", "n_intents", "n_approved", "n_transmitted",
                "halted", "run_id", "accounts", "paperbot_version"):
        assert key in rec
    assert rec["run_id"] == "20260825T101112"
    assert rec["gate"] == {"armed": False, "kill_switch": False, "permitted": False,
                           "port": bre.LIVE_TRADE_PORT}


# --- the executor actually writes it (fake broker, PREVIEW, transmits nothing) --------
class _FakeContract:
    def __init__(self, symbol, sec_type="STK"):
        self.symbol = symbol
        self.secType = sec_type


class _FakePosition:
    def __init__(self, account, symbol, qty, sec_type="STK"):
        self.account = account
        self.contract = _FakeContract(symbol, sec_type)
        self.position = qty


class _FakeIB:
    """Read-only stand-in: PREVIEW mode never touches `ib` inside execute_plan."""

    def __init__(self, summary, positions):
        self._summary = summary
        self._positions = positions

    def accountSummary(self):
        return self._summary

    def positions(self):
        return self._positions

    def placeOrder(self, *a, **k):     # pragma: no cover - must never be reached
        raise AssertionError("PREVIEW must never place an order")


def _run_session(monkeypatch, tmp_path):
    """Drive run_batch_session end to end against a fake broker; return the written records."""
    monkeypatch.setattr(ledger, "RUNS_JSONL", os.path.join(str(tmp_path), "runs.jsonl"))
    monkeypatch.setattr(ledger, "LOG_TXT", os.path.join(str(tmp_path), "paperbot.log"))
    # Flat $100 quotes for everything the run asks about — including IWM, which is HELD and
    # therefore part of the quote universe (a leg with no usable price is dropped as
    # unpriceable, which would hide the rotation this test is about).
    monkeypatch.setattr(
        bre.live_quotes, "fetch",
        lambda ib, syms: {s: bre.live_quotes.Quote(s, 100.0, 100.0, 100.0, 100.0, 1)
                          for s in syms})
    monkeypatch.setattr(bre.sp, "_strategy_universe", lambda: set(S0_UNIVERSE) - {"IWM"})

    t, meta = _custom_target_and_meta()
    summary = [_row(CUSTOM_ACCT, "NetLiquidation", "110000"),
               _row(CUSTOM_ACCT, "BuyingPower", "110000"),
               _row(CUSTOM_ACCT, "TotalCashValue", "0")]
    positions = [_FakePosition(CUSTOM_ACCT, "SCHB", 600),
                 _FakePosition(CUSTOM_ACCT, "USFR", 400),
                 _FakePosition(CUSTOM_ACCT, "IWM", 100)]
    ib = _FakeIB(summary, positions)

    rc = bre.run_batch_session(ib, [CUSTOM_ACCT], {CUSTOM_ACCT: CUSTOM_LABEL},
                               {CUSTOM_LABEL: t}, armed=False, armed_conn=False, kill=False,
                               metas={CUSTOM_LABEL: meta})
    assert rc == 0
    return list(ledger.iter_runs()), t


def test_run_batch_session_writes_exactly_one_audit_record(monkeypatch, tmp_path):
    records, _t = _run_session(monkeypatch, tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["mode"] == "BATCH_REBALANCE_PREVIEW"
    assert rec["run_id"]
    assert len(rec["accounts"]) == 1
    acct = rec["accounts"][0]
    assert acct["account"] == CUSTOM_ACCT
    assert acct["model"] == CUSTOM_LABEL
    assert acct["custom_version_number"] == 7
    # The removed ticker really was sold in the plan the record describes (change 3 wired
    # into the live sizing path, not just the helper).
    assert any(l["sym"] == "IWM" and l["side"] == "SELL" for l in acct["legs"])


def test_audit_join_from_a_trade_back_to_the_allocation_version(monkeypatch, tmp_path):
    """ANDREW'S REQUIREMENT, walked end to end: given a trade, recover the exact published
    allocation version that produced it.

        IBKR order's orderRef -> run_id -> ledger.find_run -> account entry -> version_number
    """
    records, t = _run_session(monkeypatch, tmp_path)
    rec = records[0]

    # [1] START FROM THE WIRE. Rebuild the ref for the IWM sell exactly as safe_execute's
    # transmit phase builds it — this is the string an examiner reads off the IBKR order.
    wire_ref = se._deploy_ref(CUSTOM_ACCT, t.as_of, "SELL", "IWM", rec["run_id"])

    # [2] ref -> run_id (the ref's last field).
    run_id = bre.run_id_from_order_ref(wire_ref)
    assert run_id == rec["run_id"]

    # [3] run_id -> the run record (the ledger's new reader).
    found = ledger.find_run(run_id)
    assert found is not None

    # [4] record -> the account entry that owns this ref.
    owner = [a for a in found["accounts"] if wire_ref in a["order_refs"]]
    assert len(owner) == 1

    # [5] entry -> the exact allocation version, and the book it held.
    assert owner[0]["model"] == CUSTOM_LABEL
    assert owner[0]["custom_version_number"] == 7
    assert owner[0]["custom_version_id"] == "ver-7-uuid"
    assert owner[0]["custom_effective_from"] == "2026-08-20"
    assert owner[0]["target_weights"] == {"SCHB": 0.6, "USFR": 0.4}


def test_ledger_reader_is_tolerant_and_honest(monkeypatch, tmp_path):
    monkeypatch.setattr(ledger, "RUNS_JSONL", os.path.join(str(tmp_path), "runs.jsonl"))
    monkeypatch.setattr(ledger, "LOG_TXT", os.path.join(str(tmp_path), "paperbot.log"))
    # No file yet -> no history, not an error.
    assert list(ledger.iter_runs()) == []
    assert ledger.find_run("nope") is None

    ledger.record_run({"mode": "X", "run_id": "R1"})
    with open(ledger.RUNS_JSONL, "a", encoding="utf-8") as fh:
        fh.write('{"mode": "torn line\n')          # a half-written concurrent append
    ledger.record_run({"mode": "Y", "run_id": "R2"})

    assert [r["run_id"] for r in ledger.iter_runs()] == ["R1", "R2"]   # torn line skipped
    assert ledger.find_run("R2")["mode"] == "Y"
    assert ledger.find_run("R3") is None


# --- THE ARMED BATCH RAIL REFUSES A MISSING STRATEGY UNIVERSE (v0.41.0) --------------
# sp._strategy_universe() swallows every exception and returns None, justified in its
# docstring as safe because the s0 pilot preview is zero-transmit. This rail is NOT that
# rail: one failed import would silently disarm the corp-action guard here, turning every
# unrecognised holding into a full liquidation. Refuse instead.
def test_batch_session_refuses_without_a_strategy_universe(monkeypatch, tmp_path):
    monkeypatch.setattr(ledger, "RUNS_JSONL", os.path.join(str(tmp_path), "runs.jsonl"))
    monkeypatch.setattr(ledger, "LOG_TXT", os.path.join(str(tmp_path), "paperbot.log"))
    monkeypatch.setattr(bre.sp, "_strategy_universe", lambda: None)

    class _BoomIB:
        def accountSummary(self):
            raise AssertionError("the broker must not be read once the guard is off")

        def positions(self):
            raise AssertionError("the broker must not be read once the guard is off")

    t, meta = _custom_target_and_meta()
    rc = bre.run_batch_session(_BoomIB(), [CUSTOM_ACCT], {CUSTOM_ACCT: CUSTOM_LABEL},
                               {CUSTOM_LABEL: t}, armed=False, armed_conn=False,
                               kill=False, metas={CUSTOM_LABEL: meta})
    assert rc == 2
    assert list(ledger.iter_runs()) == []          # nothing recorded, nothing sized


# =====================================================================================
# HELD-ASIDE PRICING ON THE BATCH RAIL (owner decision D6)
# -------------------------------------------------------------------------------------
# The carve-out needs TWO inputs to work: `sec_types` (what the instrument IS) and `values`
# (what a held-aside holding is WORTH — an individual bond has no live quote, no strategy
# close and no model weight, so recon_report._portfolio_values is its only reader). This
# rail already passed sec_types; without `values` a bond-holding account hit
# holding_class.UNPRICED_BLOCK_REASON and had its WHOLE order set withheld.
#
# And the constraint that makes this more than a copy of the single-account deploy rail:
# _portfolio_values is a broker round-trip PER ACCOUNT and this rail loops the whole roster,
# so it must be paid ONLY by an account that actually holds a held-aside candidate.
# =====================================================================================
BOND_SYM = "912828ZZ9"


def _held_aside_session(monkeypatch, tmp_path, positions, portfolio_values):
    """Drive run_batch_session over ONE account with the given broker positions.

    Returns (plan_account_kwargs, _portfolio_values_call_log, plans)."""
    monkeypatch.setattr(ledger, "RUNS_JSONL", os.path.join(str(tmp_path), "runs.jsonl"))
    monkeypatch.setattr(ledger, "LOG_TXT", os.path.join(str(tmp_path), "paperbot.log"))
    # Flat $100 quotes for every EQUITY. The bond is deliberately NOT quoted — that is the
    # real-world case `values` exists to cover, and it proves the fallback is wired, not just
    # the kwarg.
    monkeypatch.setattr(
        bre.live_quotes, "fetch",
        lambda ib, syms: {s: bre.live_quotes.Quote(s, 100.0, 100.0, 100.0, 100.0, 1)
                          for s in syms if s in S0_UNIVERSE})
    monkeypatch.setattr(bre.sp, "_strategy_universe", lambda: set(S0_UNIVERSE))

    calls: list[str] = []

    def _values(ib, account):
        calls.append(account)
        return dict(portfolio_values)

    monkeypatch.setattr(bre.recon_report, "_portfolio_values", _values)

    seen_kwargs: list[dict] = []
    plans: list = []
    real_plan_account = bre.rebalance_engine.plan_account

    def _spy(*a, **k):
        seen_kwargs.append(dict(k))
        plan = real_plan_account(*a, **k)      # the REAL engine, unchanged
        plans.append(plan)
        return plan

    monkeypatch.setattr(bre.rebalance_engine, "plan_account", _spy)

    t, meta = _custom_target_and_meta()
    summary = [_row(CUSTOM_ACCT, "NetLiquidation", "110000"),
               _row(CUSTOM_ACCT, "BuyingPower", "110000"),
               _row(CUSTOM_ACCT, "TotalCashValue", "0")]
    rc = bre.run_batch_session(_FakeIB(summary, positions), [CUSTOM_ACCT],
                               {CUSTOM_ACCT: CUSTOM_LABEL}, {CUSTOM_LABEL: t},
                               armed=False, armed_conn=False, kill=False,
                               metas={CUSTOM_LABEL: meta})
    assert rc == 0
    assert len(seen_kwargs) == 1
    return seen_kwargs[0], calls, plans


def test_batch_passes_values_for_an_account_holding_a_held_aside_instrument(monkeypatch,
                                                                           tmp_path):
    """A BOND position -> exactly one _portfolio_values fetch, and it reaches plan_account,
    so the bond is PRICED and carved out instead of blocking the account's orders."""
    positions = [_FakePosition(CUSTOM_ACCT, "SCHB", 600),
                 _FakePosition(CUSTOM_ACCT, "USFR", 400),
                 _FakePosition(CUSTOM_ACCT, BOND_SYM, 10_000, sec_type="BOND")]
    kwargs, calls, plans = _held_aside_session(monkeypatch, tmp_path, positions,
                                               {BOND_SYM: 10_000.0})

    assert calls == [CUSTOM_ACCT]                       # fetched once, for this account only
    assert kwargs["sec_types"][BOND_SYM] == "BOND"      # classification input (already wired)
    assert kwargs["values"] == {BOND_SYM: 10_000.0}     # THE FIX: the valuation input

    # And the consequence that matters: the bond is priced from the broker's reported value,
    # so the account is NOT benched by UNPRICED_BLOCK_REASON.
    plan = plans[0]
    assert [h.symbol for h in plan.held_aside] == [BOND_SYM]
    assert plan.held_aside[0].market_value == 10_000.0
    assert plan.blocked_reasons == []
    # Carve-out arithmetic: the model's 100% applies to what is left, not the whole account.
    assert plan.managed_net_liq == 100_000.0


def test_batch_all_stk_account_makes_no_extra_broker_call(monkeypatch, tmp_path):
    """PERFORMANCE GUARD. This rail loops 186 roster accounts. An account holding nothing
    but STK has no held-aside candidate, so _portfolio_values must not be called AT ALL, and
    its plan_account kwargs must be exactly what they were before the fix."""
    positions = [_FakePosition(CUSTOM_ACCT, "SCHB", 600),
                 _FakePosition(CUSTOM_ACCT, "USFR", 400)]
    kwargs, calls, plans = _held_aside_session(monkeypatch, tmp_path, positions, {})

    assert calls == []                        # NOT called once — zero extra broker round-trips
    assert "values" not in kwargs             # and the kwarg is not even passed
    assert set(kwargs) == {"prices", "universe", "sec_types", "cash_reserve_pct",
                           "strict_prices"}
    assert plans[0].held_aside == []


def test_batch_held_aside_test_reuses_holding_class_predicate():
    """The conditional fetch must key off holding_class's OWN predicate, never a locally
    invented list of secTypes — otherwise a type added to HELD_ASIDE_TYPES later would be
    carved out by the engine but skipped by the fetch, and silently block accounts."""
    import holding_class
    assert bre.holding_class is holding_class
    assert holding_class.is_held_aside("BOND") is True
    assert holding_class.is_held_aside("STK") is False
    assert holding_class.is_held_aside(None) is True        # fail closed on unknown


# =====================================================================================
# MODEL SCOPE (--models) — narrowing a run to a subset of the book, and the loud report
# of everything the roster resolution excluded.
#
# THE GAP THIS CLOSES. The Control Plane shelled out to this executor with NO account
# filter at all: the only run available was the whole book. The SQL-side single-`model`
# filter already existed one layer down in crm_roster.fetch_roster and was never connected
# to anything. A first live deployment therefore had to be all-or-nothing.
# =====================================================================================
def test_models_token_absent_means_the_whole_book():
    """No --models token -> [] -> main(models=None): byte-for-byte the pre-existing run."""
    assert bre.models_requested([]) == []
    assert bre.models_requested(["--arm-i-understand"]) == []
    assert bre.models_requested(["--models="]) == []


def test_models_token_parses_a_simple_comma_list():
    assert bre.models_requested(["--models=Growth,Balanced"]) == ["Growth", "Balanced"]


def test_models_token_parses_labels_with_spaces_and_parentheses():
    """Real CRM labels contain spaces AND a comma INSIDE the parentheses. The comma in
    "Growth (Small, Custom)" is part of the label, not a separator — split on it and the run
    would be scoped to two models that do not exist, silently selecting nobody."""
    argv = ["--models=Growth (Custom),Balanced (Custom),Growth (Small, Custom)"]
    assert bre.models_requested(argv) == [
        "Growth (Custom)", "Balanced (Custom)", "Growth (Small, Custom)"]


def test_models_token_strips_whitespace_and_drops_empties():
    assert bre.models_requested(["--models= Growth (Custom) , ,Balanced (Custom) "]) == [
        "Growth (Custom)", "Balanced (Custom)"]


def test_cli_threads_the_scope_through_to_main(monkeypatch):
    seen = {}
    monkeypatch.setattr(bre, "main", lambda **kw: seen.update(kw) or 0)
    bre.cli(["--models=Growth (Small, Custom),Balanced (Custom)"])
    assert seen["armed"] is False
    assert seen["models"] == ["Growth (Small, Custom)", "Balanced (Custom)"]


def test_cli_with_no_scope_passes_models_none(monkeypatch):
    seen = {}
    monkeypatch.setattr(bre, "main", lambda **kw: seen.update(kw) or 0)
    bre.cli([])
    assert seen["models"] is None


def test_cli_scope_and_arm_token_are_independent(monkeypatch):
    """The scope token must not arm anything, and the arm token must not widen the scope."""
    seen = {}
    monkeypatch.setattr(bre, "main", lambda **kw: seen.update(kw) or 0)
    bre.cli(["--arm-i-understand", "--models=Growth (Custom)"])
    assert seen["armed"] is True and seen["models"] == ["Growth (Custom)"]


def test_main_passes_the_scope_to_the_roster_resolution(monkeypatch):
    """The scope narrows the ROSTER ITSELF — the allow-list every downstream wall reads."""
    seen = {}

    def _scan(models=None):
        seen["models"] = models
        return _roster_scan([])

    monkeypatch.setattr(roster, "enrolled_roster_scan", _scan)
    monkeypatch.setattr(bre.s0_live, "connect_s0_live",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no connect")))
    assert bre.main(models=["Growth (Custom)"]) == 0
    assert seen["models"] == ["Growth (Custom)"]


def test_main_refuses_a_scoped_run_the_roster_cannot_honour(monkeypatch):
    """FAIL CLOSED. A scope that cannot be honoured must refuse, not widen: the degraded
    config fallback carries no model labels, so falling back would run the WHOLE book after
    the operator deliberately narrowed it."""
    def _boom(models=None):
        raise roster.RosterScopeUnavailable("no model labels in the fallback")

    def _must_not_connect(*a, **k):
        raise AssertionError("MUST NOT CONNECT after a refused scope")

    monkeypatch.setattr(roster, "enrolled_roster_scan", _boom)
    monkeypatch.setattr(bre.s0_live, "connect_s0_live", _must_not_connect)
    monkeypatch.setattr(bre.s0_live, "connect_s0_live_armed", _must_not_connect)
    assert bre.main(armed=True, models=["Growth (Custom)"]) == 2


def test_main_prints_the_scope_line_and_names_every_exclusion(monkeypatch, capsys):
    """A NO-TRADE HOLD must never be a silent omission: the run names every held account and
    every in-scope unfunded one, and prints ONE machine-parseable BATCH-SCOPE line."""
    monkeypatch.setattr(
        roster, "enrolled_roster_scan",
        lambda models=None: _roster_scan(
            [], held=["U111", "U222"], unfunded=["U333"],
            scope=["Balanced (Custom)", "Growth (Custom)"], source="crm"))
    assert bre.main(models=["Growth (Custom)", "Balanced (Custom)"]) == 0
    out = capsys.readouterr().out
    assert "BATCH-SCOPE models=Balanced (Custom),Growth (Custom) roster=0 held=2 " \
           "unfunded=1 source=crm" in out
    assert "NO-TRADE HOLD" in out and "U111" in out and "U222" in out
    assert "NOT FUNDED/VISIBLE" in out and "U333" in out


def test_main_says_whole_book_when_nothing_was_scoped(monkeypatch, capsys):
    monkeypatch.setattr(roster, "enrolled_roster_scan",
                        lambda models=None: _roster_scan([], source="config"))
    assert bre.main() == 0
    out = capsys.readouterr().out
    assert "WHOLE BOOK" in out
    assert "BATCH-SCOPE models=ALL roster=0 held=0 unfunded=0 source=config" in out


def test_the_batch_summary_line_is_unchanged(monkeypatch):
    """The Control Plane parses BATCH-SUMMARY. BATCH-SCOPE is an ADDITIONAL line; the summary's
    own format must not have moved."""
    import inspect
    src = inspect.getsource(bre.run_batch_session)
    assert 'BATCH-SUMMARY roster=' in src
    assert 'out_of_spec=' in src and 'in_spec=' in src and 'skipped=' in src


# =====================================================================================
# BROKER-SUPPLIED CONTRACTS (2026-09-01). The rail must USE the contract ib.positions()
# already handed it, and must never die because one holding cannot be resolved.
#
# THE INCIDENT. U27295881 / U27305011 were moved onto custom models while still holding
# MUTUAL FUNDS from a previous advisor. Every rail here threw the broker's contract away
# and rebuilt Stock(symbol, "SMART", "USD") from the ticker string. A fund is not a US
# stock: IBKR answered "Unknown contract", conId was never populated, and reqMktData
# RAISED while hashing it — killing the preview for ALL 16 accounts, not just those two.
# =====================================================================================
FUND_SYM = "DODGX"


class _FundContract(_FakeContract):
    """What ib.positions() really returns for a mutual fund: FUND on FUNDSERV, real conId."""

    def __init__(self, symbol=FUND_SYM, con_id=86797803):
        super().__init__(symbol, "FUND")
        self.exchange = "FUNDSERV"
        self.currency = "USD"
        self.conId = con_id


class _FundPosition:
    def __init__(self, account, symbol=FUND_SYM, qty=100.0):
        self.account = account
        self.contract = _FundContract(symbol)
        self.position = qty


def _contract_universe_seen(monkeypatch, tmp_path, positions):
    """Drive run_batch_session and capture EXACTLY what was handed to live_quotes.fetch."""
    monkeypatch.setattr(ledger, "RUNS_JSONL", os.path.join(str(tmp_path), "runs.jsonl"))
    monkeypatch.setattr(ledger, "LOG_TXT", os.path.join(str(tmp_path), "paperbot.log"))
    seen: dict = {}

    def _fetch(ib, syms):
        seen["syms"] = syms
        return {s: bre.live_quotes.Quote(s, 100.0, 100.0, 100.0, 100.0, 1) for s in syms}

    monkeypatch.setattr(bre.live_quotes, "fetch", _fetch)
    monkeypatch.setattr(bre.sp, "_strategy_universe", lambda: set(S0_UNIVERSE))
    monkeypatch.setattr(bre.recon_report, "_portfolio_values",
                        lambda ib, account: {FUND_SYM: 10_000.0})

    requests: list = []
    real_build = bre.build_batch_requests

    def _spy_build(*a, **k):
        out = real_build(*a, **k)
        requests.extend(out)
        return out

    monkeypatch.setattr(bre, "build_batch_requests", _spy_build)

    t, meta = _custom_target_and_meta()
    summary = [_row(CUSTOM_ACCT, "NetLiquidation", "110000"),
               _row(CUSTOM_ACCT, "BuyingPower", "110000"),
               _row(CUSTOM_ACCT, "TotalCashValue", "0")]
    rc = bre.run_batch_session(_FakeIB(summary, positions), [CUSTOM_ACCT],
                               {CUSTOM_ACCT: CUSTOM_LABEL}, {CUSTOM_LABEL: t},
                               armed=False, armed_conn=False, kill=False,
                               metas={CUSTOM_LABEL: meta})
    assert rc == 0
    return seen["syms"], requests


def test_batch_hands_the_quote_path_the_brokers_own_contracts(monkeypatch, tmp_path):
    """THE FIX. The quote universe carries the broker's fully-qualified contract for every
    HELD symbol — the same objects ib.positions() returned — and None for a model target
    nobody holds yet (which still takes the Stock(symbol, "SMART", "USD") path)."""
    fund_pos = _FundPosition(CUSTOM_ACCT)
    positions = [_FakePosition(CUSTOM_ACCT, "SCHB", 600), fund_pos]

    syms, _requests = _contract_universe_seen(monkeypatch, tmp_path, positions)

    assert isinstance(syms, dict)                          # a map, not a bare ticker list
    assert syms[FUND_SYM] is fund_pos.contract             # the broker's OWN object, verbatim
    assert (syms[FUND_SYM].secType, syms[FUND_SYM].exchange) == ("FUND", "FUNDSERV")
    assert syms["SCHB"] is positions[0].contract           # held ETF: also the broker's
    assert syms["USFR"] is None                            # target nobody holds: rebuild it


def test_batch_stamps_the_broker_contracts_onto_every_execution_request(monkeypatch,
                                                                       tmp_path):
    """The transmit half. safe_execute places a leg for a held symbol against the BROKER'S
    contract, so the map has to ride on the request beside quotes/prices — stamped here, so
    the shared crm_execute request builder stays untouched."""
    fund_pos = _FundPosition(CUSTOM_ACCT)
    positions = [_FakePosition(CUSTOM_ACCT, "SCHB", 600), fund_pos]

    _syms, requests = _contract_universe_seen(monkeypatch, tmp_path, positions)

    assert requests, "the account should be out of spec and produce a request"
    for req in requests:
        assert req.contracts[FUND_SYM] is fund_pos.contract
        assert req.contracts["SCHB"] is positions[0].contract
        # And what safe_execute would actually place the fund's SELL against.
        assert bre.safe_execute._leg_contract(None, FUND_SYM, req.contracts) is \
            fund_pos.contract


def test_batch_all_etf_account_passes_no_exotic_contracts(monkeypatch, tmp_path):
    """EXISTING BEHAVIOUR UNCHANGED. An all-ETF account's quote universe holds exactly the
    same symbols as before, and every request's contract map is just its own holdings."""
    positions = [_FakePosition(CUSTOM_ACCT, "SCHB", 600),
                 _FakePosition(CUSTOM_ACCT, "USFR", 400)]

    syms, requests = _contract_universe_seen(monkeypatch, tmp_path, positions)

    assert sorted(syms) == ["SCHB", "USFR"]
    assert all(getattr(c, "secType", None) == "STK" for c in syms.values())
    for req in requests:
        assert sorted(req.contracts) == ["SCHB", "USFR"]
