"""test_capability.py — offline unit tests for the CRM capability gate (#42/#43).

Pure/offline: no broker, no gateway, no I/O. Runs with zero infra:
    cd crm
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest -q

Covers §5 of docs/CRM_DESIGN_groups_brain.md: the cushion property + div-by-zero guard,
the per-requirement checks, the HARD gray-out (block) with correct reasons, the ETF-only
allow-anywhere case, the SOFT margin warn (breach buying-power / drop cushion) vs. silence
with ample headroom, the allow-but-flag case, the assignable_templates dropdown view, and
the AccountCapabilities to_dict/from_dict round-trip.
"""
from __future__ import annotations

import pytest

import domain
from domain import Requirement, Sleeve, Template
import capability
from capability import (
    AccountCapabilities,
    DEFAULT_WARN_CUSHION_FLOOR,
    satisfies,
    unmet_requirements,
    hard_reasons,
    soft_warnings,
    GateResult,
    evaluate_template,
    assignable_templates,
)


# ===========================================================================
# Snapshot builders — a fully-qualified account and knobs to break each axis
# ===========================================================================
def _caps(**over) -> AccountCapabilities:
    """A fully-qualified margin account (L3 + margin + index perm, healthy margin). Override
    any field to break a specific axis."""
    base = dict(
        account_id="DU8922143",
        options_level=3,
        index_option_perm=True,
        is_margin=True,
        account_type="margin",
        net_liq=1_000_000.0,
        buying_power=2_000_000.0,
        excess_liquidity=800_000.0,
    )
    base.update(over)
    return AccountCapabilities(**base)


# Templates from the real registry: an ETF-only (no-requirement) template and an overlay
# (S8) template that carries all three requirements via domain.template_requirements.
ETF_ONLY = Template(template_id="balanced", name="Balanced ETF-only",
                    weights={"S0-Balanced": 1.0})
OVERLAY = Template(template_id="balanced_overlay", name="Balanced + S8 Overlay",
                   weights={"S0-Balanced": 0.75, "S8-Overlay": 0.25})


# ===========================================================================
# 1) cushion property incl. net_liq<=0 guard
# ===========================================================================
def test_cushion_normal():
    caps = _caps(net_liq=1_000_000.0, excess_liquidity=250_000.0)
    assert caps.cushion == pytest.approx(0.25)


def test_cushion_zero_netliq_guarded():
    assert _caps(net_liq=0.0, excess_liquidity=5.0).cushion == 0.0


def test_cushion_negative_netliq_guarded():
    assert _caps(net_liq=-10.0, excess_liquidity=5.0).cushion == 0.0


# ===========================================================================
# 2) Requirement checks / unmet_requirements  (§5.1 / §5.3)
# ===========================================================================
def test_satisfies_l3():
    assert satisfies(_caps(options_level=3), Requirement.OPTIONS_L3)
    assert satisfies(_caps(options_level=4), Requirement.OPTIONS_L3)


def test_l2_fails_options_l3():
    assert not satisfies(_caps(options_level=2), Requirement.OPTIONS_L3)


def test_none_options_level_fails_l3():
    assert not satisfies(_caps(options_level=None), Requirement.OPTIONS_L3)


def test_cash_account_fails_margin():
    assert not satisfies(_caps(is_margin=False, account_type="cash"),
                         Requirement.MARGIN_ACCOUNT)
    assert satisfies(_caps(is_margin=True), Requirement.MARGIN_ACCOUNT)


def test_no_index_perm_fails():
    assert not satisfies(_caps(index_option_perm=False), Requirement.INDEX_OPTION_PERM)
    assert satisfies(_caps(index_option_perm=True), Requirement.INDEX_OPTION_PERM)


def test_unmet_requirements_subset():
    caps = _caps(options_level=2, is_margin=False, account_type="cash",
                 index_option_perm=False)
    required = frozenset({Requirement.OPTIONS_L3, Requirement.MARGIN_ACCOUNT,
                          Requirement.INDEX_OPTION_PERM})
    assert unmet_requirements(caps, required) == required  # all three unmet


def test_unmet_requirements_partial():
    # Has L3 + index perm but is a cash account → only MARGIN_ACCOUNT unmet.
    caps = _caps(options_level=3, index_option_perm=True, is_margin=False,
                 account_type="cash")
    required = frozenset({Requirement.OPTIONS_L3, Requirement.MARGIN_ACCOUNT,
                          Requirement.INDEX_OPTION_PERM})
    assert unmet_requirements(caps, required) == frozenset({Requirement.MARGIN_ACCOUNT})


def test_fully_qualified_no_unmet():
    required = frozenset(Requirement)
    assert unmet_requirements(_caps(), required) == frozenset()


def test_satisfies_unknown_requirement_raises():
    class _Fake:
        pass
    with pytest.raises(ValueError):
        satisfies(_caps(), _Fake())  # not a mapped Requirement


# ===========================================================================
# 3) Hard gate — BLOCK an overlay for a missing-permission account, with reasons
# ===========================================================================
def test_hard_block_overlay_cash_l2_no_index():
    caps = _caps(options_level=2, is_margin=False, account_type="cash",
                 index_option_perm=False)
    res = evaluate_template(caps, OVERLAY)
    assert res.allowed is False
    assert res.blocked_requirements == frozenset({
        Requirement.OPTIONS_L3, Requirement.MARGIN_ACCOUNT, Requirement.INDEX_OPTION_PERM})
    joined = " || ".join(res.hard_reasons)
    assert "options Level 3" in joined and "L2" in joined
    assert "margin account" in joined and "cash" in joined
    assert "index-option" in joined


def test_hard_block_reason_none_options():
    caps = _caps(options_level=None, is_margin=True, index_option_perm=True)
    res = evaluate_template(caps, OVERLAY)
    assert res.allowed is False
    assert res.blocked_requirements == frozenset({Requirement.OPTIONS_L3})
    assert any("no options approval" in r for r in res.hard_reasons)


def test_hard_reasons_stable_order():
    caps = _caps(options_level=1, is_margin=False, account_type="cash",
                 index_option_perm=False)
    unmet = unmet_requirements(caps, frozenset(Requirement))
    reasons = hard_reasons(caps, unmet)
    # Declaration order: OPTIONS_L3, MARGIN_ACCOUNT, INDEX_OPTION_PERM.
    assert "options Level 3" in reasons[0]
    assert "margin account" in reasons[1]
    assert "index-option" in reasons[2]


# ===========================================================================
# 4) Hard gate — ALLOW an ETF-only (no-requirement) template for ANY account
# ===========================================================================
def test_etf_only_allowed_for_cash_account():
    # A bare cash account with no options approval at all — still fine for ETF-only.
    caps = _caps(options_level=None, is_margin=False, account_type="cash",
                 index_option_perm=False)
    res = evaluate_template(caps, ETF_ONLY)
    assert res.allowed is True
    assert res.blocked_requirements == frozenset()
    assert res.hard_reasons == []


def test_etf_only_never_soft_warns_even_tiny_account():
    # Even a thin cash account: ETF sleeve carries no requirement → no margin soft-warn.
    caps = _caps(options_level=None, is_margin=False, account_type="cash",
                 index_option_perm=False, net_liq=1000.0, buying_power=1.0,
                 excess_liquidity=1.0)
    res = evaluate_template(caps, ETF_ONLY)
    assert res.allowed is True
    assert res.soft_warnings == []


def test_overlay_allowed_when_fully_qualified_and_ample():
    res = evaluate_template(_caps(), OVERLAY)
    assert res.allowed is True
    assert res.blocked_requirements == frozenset()
    assert res.soft_warnings == []  # 250k need vs 800k XL / 2M BP, cushion 0.55 > 0.10


# ===========================================================================
# 5) Soft gate — warn on breach, silent with ample headroom
# ===========================================================================
def test_soft_warn_exceeds_buying_power():
    # Overlay 25% of 1M = 250k margin need; BuyingPower only 100k → breach.
    caps = _caps(buying_power=100_000.0, excess_liquidity=900_000.0)
    warns = soft_warnings(caps, OVERLAY)
    assert warns  # non-empty
    assert any("BuyingPower" in w for w in warns)


def test_soft_warn_drops_cushion_below_floor():
    # 250k need, ExcessLiquidity 300k → projected (300k-250k)/1M = 0.05 < 0.10 floor.
    caps = _caps(buying_power=5_000_000.0, excess_liquidity=300_000.0)
    warns = soft_warnings(caps, OVERLAY)
    assert warns
    assert any("cushion drops to 5.0%" in w for w in warns)


def test_soft_warn_exceeds_excess_liquidity():
    # 250k need > 200k ExcessLiquidity → breach_xl.
    caps = _caps(buying_power=5_000_000.0, excess_liquidity=200_000.0)
    warns = soft_warnings(caps, OVERLAY)
    assert any("exceeds ExcessLiquidity" in w for w in warns)


def test_soft_silent_with_ample_headroom():
    caps = _caps(buying_power=5_000_000.0, excess_liquidity=900_000.0)
    assert soft_warnings(caps, OVERLAY) == []


def test_soft_message_mirrors_spec_example():
    caps = _caps(buying_power=100_000.0, excess_liquidity=300_000.0)
    warns = soft_warnings(caps, OVERLAY)
    head = warns[0]
    assert "S8-Overlay at 25%" in head
    assert "would consume" in head
    assert "$250,000" in head


def test_soft_floor_is_overridable_param():
    # With a 0.0 floor and ample BP/XL, the cushion-floor breach disappears.
    caps = _caps(buying_power=5_000_000.0, excess_liquidity=300_000.0)
    assert soft_warnings(caps, OVERLAY, warn_cushion_floor=0.0) == []
    # Default floor DOES warn on the same account.
    assert soft_warnings(caps, OVERLAY) != []


def test_soft_silent_when_netliq_nonpositive():
    caps = _caps(net_liq=0.0, buying_power=0.0, excess_liquidity=0.0)
    assert soft_warnings(caps, OVERLAY) == []


def test_default_floor_constant():
    assert DEFAULT_WARN_CUSHION_FLOOR == 0.10


# ===========================================================================
# 6) Allow-but-flag: hard-allowed AND soft-warned  (§5.2)
# ===========================================================================
def test_hard_allowed_but_soft_warned():
    # Fully qualified (allowed) but thin margin (soft warn).
    caps = _caps(buying_power=100_000.0, excess_liquidity=300_000.0)
    res = evaluate_template(caps, OVERLAY)
    assert res.allowed is True                 # soft never blocks
    assert res.blocked_requirements == frozenset()
    assert res.soft_warnings                    # non-empty


def test_soft_computed_even_when_hard_blocked():
    # Missing index perm (hard block) AND thin margin → BOTH surfaced (design choice).
    caps = _caps(index_option_perm=False, buying_power=100_000.0,
                 excess_liquidity=300_000.0)
    res = evaluate_template(caps, OVERLAY)
    assert res.allowed is False
    assert res.blocked_requirements == frozenset({Requirement.INDEX_OPTION_PERM})
    assert res.soft_warnings  # margin picture still shown behind the hard wall


# ===========================================================================
# 7) assignable_templates — the mixed enabled/grayed dropdown  (§5.2)
# ===========================================================================
def test_assignable_templates_mixed_dropdown():
    templates = {"balanced": ETF_ONLY, "balanced_overlay": OVERLAY}

    # Cash L2 account: ETF-only enabled, overlay grayed out.
    cash = _caps(options_level=2, is_margin=False, account_type="cash",
                 index_option_perm=False)
    view = assignable_templates(cash, templates)
    assert set(view) == {"balanced", "balanced_overlay"}
    assert view["balanced"].allowed is True
    assert view["balanced_overlay"].allowed is False
    assert view["balanced_overlay"].hard_reasons

    # Fully-qualified ample account: both enabled, no warnings.
    rich = _caps()
    view2 = assignable_templates(rich, templates)
    assert view2["balanced"].allowed is True
    assert view2["balanced_overlay"].allowed is True
    assert view2["balanced_overlay"].soft_warnings == []

    # Fully-qualified but thin: overlay enabled WITH a soft warning.
    thin = _caps(buying_power=100_000.0, excess_liquidity=300_000.0)
    view3 = assignable_templates(thin, templates)
    assert view3["balanced_overlay"].allowed is True
    assert view3["balanced_overlay"].soft_warnings


def test_assignable_templates_returns_gateresult_per_key():
    view = assignable_templates(_caps(), {"balanced_overlay": OVERLAY})
    assert isinstance(view["balanced_overlay"], GateResult)
    assert view["balanced_overlay"].template_id == "balanced_overlay"


# ===========================================================================
# 8) AccountCapabilities to_dict / from_dict round-trip
# ===========================================================================
def test_caps_roundtrip():
    caps = _caps(options_level=None, account_type="cash", is_margin=False,
                 index_option_perm=False, net_liq=123456.78,
                 buying_power=200000.0, excess_liquidity=45000.0)
    back = AccountCapabilities.from_dict(caps.to_dict())
    assert back == caps


def test_caps_from_dict_defaults():
    # A minimal snapshot (only the id) fills sensible, safe defaults.
    caps = AccountCapabilities.from_dict({"account_id": "DUxxx"})
    assert caps.options_level is None
    assert caps.index_option_perm is False
    assert caps.is_margin is False
    assert caps.account_type == ""
    assert caps.net_liq == 0.0
    # And the gate treats it as unqualified for the overlay.
    assert evaluate_template(caps, OVERLAY).allowed is False


# ===========================================================================
# 9) custom registry path (proves registry is threaded, not hardcoded)
# ===========================================================================
def test_evaluate_with_custom_registry():
    reg = {
        "X-ETF": Sleeve(sleeve_id="X-ETF", strategy_key="adaptive_all_weather",
                        tier="Balanced", fa_group_name="x_etf"),
        "X-OPT": Sleeve(sleeve_id="X-OPT", strategy_key="s8_british_ic",
                        tier="Overlay", fa_group_name="x_opt"),
    }
    tmpl = Template(template_id="mix", name="mix",
                    weights={"X-ETF": 0.5, "X-OPT": 0.5})
    caps = _caps(options_level=1, is_margin=False, account_type="cash",
                 index_option_perm=False)
    res = evaluate_template(caps, tmpl, reg)
    assert res.allowed is False
    assert res.blocked_requirements == frozenset({
        Requirement.OPTIONS_L3, Requirement.MARGIN_ACCOUNT, Requirement.INDEX_OPTION_PERM})
