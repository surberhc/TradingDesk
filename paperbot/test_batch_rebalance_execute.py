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
    monkeypatch.setattr(roster, "enrolled_roster", lambda: [CUSTOM_ACCT])
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
# STAGE 5 (2) — a CUSTOM label is NEVER re-tiered by the small-account NAV override.
# The exclusion is SOURCE-based, so a CRM RENAME cannot re-point the account onto an S0
# model. The pair below uses the SAME label string and the SAME NAV: the only difference
# is whether the CRM says an allocation is published under it.
# -------------------------------------------------------------------------------------
RENAMED_CUSTOM = "Growth (Small)"      # what a careless CRM rename could produce


def _tier_setup(monkeypatch, model, nav, *, published):
    bre._TIER_MISMATCHES.clear()
    _fake_crm(monkeypatch, published)
    row = {"account_number": CUSTOM_ACCT, "model": model, "total_value": nav}
    monkeypatch.setattr(crm_roster, "fetch_roster",
                        lambda advisor_name=None, model=None, conn=None: [row])


def test_custom_label_is_never_re_tiered_by_nav(monkeypatch):
    # A $5M account on a label that LOOKS like the small tier. Because the CRM publishes an
    # allocation under it, it is Andrew's hand-authored book and the NAV override is skipped.
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


def test_custom_small_label_is_not_demoted_either(monkeypatch):
    # The other direction: a tiny-NAV account on a custom label is not demoted onto the
    # whole-share proxy — there is no parent model to collapse a hand-authored book onto.
    _tier_setup(monkeypatch, "Growth (Small, Custom)", 1_000.0,
                published={"Growth (Small, Custom)"})
    assert bre.resolve_roster_versions([CUSTOM_ACCT]) == {
        CUSTOM_ACCT: "Growth (Small, Custom)"}
    assert bre._TIER_MISMATCHES == {}


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
    def __init__(self, account, symbol, qty):
        self.account = account
        self.contract = _FakeContract(symbol)
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
