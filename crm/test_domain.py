"""test_domain.py — offline unit tests for the CRM 'brain' domain model (conductor #42/#43).

Pure/offline: no broker, no gateway, no I/O. Runs with zero infra:
    cd crm
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest -q
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from domain import (
    WEIGHT_TOL,
    VALID_TIERS,
    VALID_STRATEGY_KEYS,
    Requirement,
    Sleeve,
    sleeve_requirements,
    SLEEVE_REGISTRY,
    Template,
    validate_template,
    template_requirements,
    AccountAssignment,
    AssignmentBook,
    derive_group_membership,
    EXAMPLE_TEMPLATES,
)


# ---------------------------------------------------------------------------
# sleeve_requirements — derived, never hand-tagged (§5.1)
# ---------------------------------------------------------------------------
def test_s8_overlay_requires_three():
    reqs = sleeve_requirements(SLEEVE_REGISTRY["S8-Overlay"])
    assert reqs == frozenset({
        Requirement.OPTIONS_L3,
        Requirement.MARGIN_ACCOUNT,
        Requirement.INDEX_OPTION_PERM,
    })


def test_s0_sleeves_require_nothing():
    for sleeve_id in ("S0-Conservative", "S0-Balanced", "S0-Growth"):
        assert sleeve_requirements(SLEEVE_REGISTRY[sleeve_id]) == frozenset()


def test_requirements_are_a_function_of_strategy_not_the_sleeve_id():
    # Two different sleeve ids on the same strategy derive identical requirements.
    a = Sleeve("X", "s8_british_ic", "Overlay", "g_x")
    b = Sleeve("Y", "s8_british_ic", "Overlay", "g_y")
    assert sleeve_requirements(a) == sleeve_requirements(b)


def test_sleeve_validate_rejects_bad_strategy_and_tier():
    with pytest.raises(ValueError):
        Sleeve("Z", "not_a_strategy", "Balanced", "g").validate()
    with pytest.raises(ValueError):
        Sleeve("Z", "adaptive_all_weather", "NotATier", "g").validate()
    # A good one validates fluently and returns self.
    good = Sleeve("Z", "adaptive_all_weather", "Balanced", "g")
    assert good.validate() is good


def test_registry_entries_are_all_valid():
    for sleeve in SLEEVE_REGISTRY.values():
        sleeve.validate()
        assert sleeve.strategy_key in VALID_STRATEGY_KEYS
        assert sleeve.tier in VALID_TIERS


# ---------------------------------------------------------------------------
# validate_template — mirrors model_portfolio.validate_policy rules
# ---------------------------------------------------------------------------
def test_template_sum_not_one_rejected():
    with pytest.raises(ValueError, match="not 1.0"):
        validate_template(Template("t", "t", {"S0-Balanced": 0.5, "S8-Overlay": 0.4}))


def test_template_unknown_sleeve_rejected():
    with pytest.raises(ValueError, match="unknown sleeve_id"):
        validate_template(Template("t", "t", {"S0-Nonexistent": 1.0}))


def test_template_nan_weight_rejected():
    with pytest.raises(ValueError, match="not finite"):
        validate_template(Template("t", "t", {"S0-Balanced": float("nan")}))


def test_template_inf_weight_rejected():
    with pytest.raises(ValueError, match="not finite"):
        validate_template(Template("t", "t", {"S0-Balanced": float("inf")}))


def test_template_negative_weight_rejected():
    with pytest.raises(ValueError, match="out of range"):
        validate_template(Template("t", "t", {"S0-Balanced": -0.1, "S8-Overlay": 1.1}))


def test_template_over_one_weight_rejected():
    with pytest.raises(ValueError, match="out of range"):
        validate_template(Template("t", "t", {"S0-Balanced": 1.5}))


def test_template_empty_weights_rejected():
    with pytest.raises(ValueError, match="no sleeve weights"):
        validate_template(Template("t", "t", {}))


def test_template_within_tolerance_accepted():
    # 1.0 +/- just under the tolerance must pass.
    ok = Template("t", "t", {"S0-Balanced": 1.0 - WEIGHT_TOL / 2})
    validate_template(ok)                 # no raise
    assert ok.validate() is ok            # fluent


def test_template_just_outside_tolerance_rejected():
    with pytest.raises(ValueError):
        validate_template(Template("t", "t", {"S0-Balanced": 1.0 - WEIGHT_TOL * 10}))


def test_two_sleeve_template_sums_to_one_accepted():
    validate_template(Template("t", "t", {"S0-Balanced": 0.75, "S8-Overlay": 0.25}))


# ---------------------------------------------------------------------------
# template_requirements — union over member sleeves (§5.1)
# ---------------------------------------------------------------------------
def test_overlay_template_auto_requires_options_l3():
    t = Template("bo", "Balanced+Overlay", {"S0-Balanced": 0.75, "S8-Overlay": 0.25})
    reqs = template_requirements(t)
    assert Requirement.OPTIONS_L3 in reqs
    assert Requirement.MARGIN_ACCOUNT in reqs
    assert Requirement.INDEX_OPTION_PERM in reqs


def test_etf_only_template_has_no_requirements():
    t = Template("b", "Balanced", {"S0-Balanced": 1.0})
    assert template_requirements(t) == frozenset()


def test_template_requirements_is_union_not_intersection():
    t = Template("mix", "mix", {"S0-Growth": 0.5, "S8-Overlay": 0.5})
    # S0 contributes nothing, S8 contributes 3 -> union is exactly the 3.
    assert template_requirements(t) == sleeve_requirements(SLEEVE_REGISTRY["S8-Overlay"])


# ---------------------------------------------------------------------------
# AssignmentBook — supersede-not-overwrite + append-only audit (§3.3)
# ---------------------------------------------------------------------------
def test_assign_then_reassign_supersedes_and_chains_prior():
    book = AssignmentBook()
    t0 = datetime(2026, 7, 24, 9, 0, 0)
    t1 = t0 + timedelta(hours=1)

    a0 = book.assign("DU1", "balanced", set_by="andrew", now=t0)
    assert a0.prior_template_id is None
    assert book.current("DU1").template_id == "balanced"

    a1 = book.assign("DU1", "balanced_overlay", set_by="andrew", now=t1)
    # current reflects the latest
    assert book.current("DU1").template_id == "balanced_overlay"
    # prior_template_id chains to what was current before
    assert a1.prior_template_id == "balanced"


def test_history_is_append_only_and_chronological():
    book = AssignmentBook()
    t0 = datetime(2026, 7, 24, 9, 0, 0)
    book.assign("DU1", "balanced", set_by="a", now=t0)
    book.assign("DU1", "balanced_overlay", set_by="a", now=t0 + timedelta(hours=1))
    book.assign("DU1", "balanced", set_by="a", now=t0 + timedelta(hours=2))

    hist = book.history("DU1")
    assert [h.template_id for h in hist] == ["balanced", "balanced_overlay", "balanced"]
    assert [h.set_at for h in hist] == sorted(h.set_at for h in hist)  # chronological

    # returned history is a copy — mutating it must not affect the book
    hist.clear()
    assert len(book.history("DU1")) == 3


def test_effective_at_defaults_to_set_at_but_is_injectable():
    book = AssignmentBook()
    t0 = datetime(2026, 7, 24, 9, 0, 0)
    a = book.assign("DU1", "balanced", set_by="a", now=t0)
    assert a.effective_at == t0 == a.set_at

    eff = datetime(2026, 8, 1, 0, 0, 0)
    b = book.assign("DU2", "balanced", set_by="a", now=t0, effective_at=eff)
    assert b.effective_at == eff
    assert b.set_at == t0


def test_all_current_across_multiple_accounts():
    book = AssignmentBook()
    t0 = datetime(2026, 7, 24, 9, 0, 0)
    book.assign("DU1", "balanced", set_by="a", now=t0)
    book.assign("DU2", "balanced_overlay", set_by="a", now=t0)
    book.assign("DU2", "balanced", set_by="a", now=t0 + timedelta(hours=1))

    cur = book.all_current()
    assert set(cur) == {"DU1", "DU2"}
    assert cur["DU1"].template_id == "balanced"
    assert cur["DU2"].template_id == "balanced"          # superseded

    # all_current is a copy
    cur.clear()
    assert set(book.all_current()) == {"DU1", "DU2"}


def test_current_none_for_unknown_account():
    assert AssignmentBook().current("nobody") is None


# ---------------------------------------------------------------------------
# derive_group_membership — pure derivation (§2 / §3.4)
# ---------------------------------------------------------------------------
def _templates():
    return {
        "balanced": Template("balanced", "Balanced", {"S0-Balanced": 1.0}),
        "balanced_overlay": Template(
            "balanced_overlay", "Balanced+Overlay",
            {"S0-Balanced": 0.75, "S8-Overlay": 0.25}),
    }


def test_single_sleeve_template_puts_account_in_one_group():
    book = AssignmentBook()
    book.assign("DU1", "balanced", set_by="a", now=datetime(2026, 7, 24))
    membership = derive_group_membership(book, _templates())
    assert membership == {"tier_balanced": {"DU1"}}


def test_two_sleeve_overlay_template_puts_account_in_both_groups():
    book = AssignmentBook()
    book.assign("DU1", "balanced_overlay", set_by="a", now=datetime(2026, 7, 24))
    membership = derive_group_membership(book, _templates())
    assert membership == {"tier_balanced": {"DU1"}, "s8_overlay": {"DU1"}}


def test_reassignment_moves_account_between_groups():
    book = AssignmentBook()
    t0 = datetime(2026, 7, 24)
    book.assign("DU1", "balanced_overlay", set_by="a", now=t0)
    book.assign("DU1", "balanced", set_by="a", now=t0 + timedelta(hours=1))
    membership = derive_group_membership(book, _templates())
    # now only in the ETF group — overlay membership is gone
    assert membership == {"tier_balanced": {"DU1"}}


def test_multiple_accounts_aggregate_into_group_sets():
    book = AssignmentBook()
    t0 = datetime(2026, 7, 24)
    book.assign("DU1", "balanced", set_by="a", now=t0)
    book.assign("DU2", "balanced_overlay", set_by="a", now=t0)
    book.assign("DU3", "balanced", set_by="a", now=t0)
    membership = derive_group_membership(book, _templates())
    assert membership["tier_balanced"] == {"DU1", "DU2", "DU3"}
    assert membership["s8_overlay"] == {"DU2"}


def test_dangling_template_assignment_raises():
    book = AssignmentBook()
    book.assign("DU1", "no_such_template", set_by="a", now=datetime(2026, 7, 24))
    with pytest.raises(ValueError, match="dangling assignment"):
        derive_group_membership(book, _templates())


def test_empty_book_yields_empty_membership():
    assert derive_group_membership(AssignmentBook(), _templates()) == {}


# ---------------------------------------------------------------------------
# to_dict / from_dict round-trips (the future-transport boundary, §8)
# ---------------------------------------------------------------------------
def test_sleeve_roundtrip():
    s = SLEEVE_REGISTRY["S8-Overlay"]
    assert Sleeve.from_dict(s.to_dict()) == s


def test_template_roundtrip():
    t = Template("bo", "Balanced+Overlay", {"S0-Balanced": 0.75, "S8-Overlay": 0.25})
    back = Template.from_dict(t.to_dict())
    assert back.template_id == t.template_id
    assert back.name == t.name
    assert dict(back.weights) == dict(t.weights)
    assert back.active == t.active


def test_assignment_roundtrip_including_datetimes():
    a = AccountAssignment(
        account_id="DU1", template_id="balanced_overlay",
        effective_at=datetime(2026, 8, 1, 0, 0, 0),
        set_by="andrew", set_at=datetime(2026, 7, 24, 9, 30, 15),
        prior_template_id="balanced")
    back = AccountAssignment.from_dict(a.to_dict())
    assert back == a
    assert back.effective_at == a.effective_at
    assert back.set_at == a.set_at


def test_assignment_roundtrip_with_none_prior():
    a = AccountAssignment(
        account_id="DU1", template_id="balanced",
        effective_at=datetime(2026, 7, 24), set_by="a",
        set_at=datetime(2026, 7, 24), prior_template_id=None)
    assert AccountAssignment.from_dict(a.to_dict()) == a


# ---------------------------------------------------------------------------
# EXAMPLE_TEMPLATES — illustrative fixtures must at least be well-formed
# ---------------------------------------------------------------------------
def test_example_templates_validate():
    for t in EXAMPLE_TEMPLATES.values():
        validate_template(t)


def test_example_overlay_requires_options():
    assert Requirement.OPTIONS_L3 in template_requirements(
        EXAMPLE_TEMPLATES["balanced_overlay"])
