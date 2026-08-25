"""test_custom_tier.py — the CUSTOM-family whole-share size ladder, pinned threshold by
threshold in BOTH directions.

This is an ORDER-AFFECTING rule: it decides which model an account trades. The CRM runs the
IDENTICAL ladder in SQL, so every number here is a contract between the two implementations —
if they disagree, an account flips models every day. These tests are the desk's half of that
contract, written so a reviewer can diff them straight against the SQL.

PURE. No CRM, no broker, no network, no file I/O.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_custom_tier.py -q
"""
from __future__ import annotations

import pytest

import custom_tier as ct

FULL_G = "Growth (Custom)"
FULL_B = "Balanced (Custom)"
FULL_C = "Conservative (Custom)"
SMALL_G = "Growth (Small, Custom)"
SMALL_B = "Balanced (Small, Custom)"
SMALL_C = "Conservative (Small, Custom)"
STARTER = "Starter (Custom)"


# =====================================================================================
# The label table itself — the closed set every decision dispatches on.
# =====================================================================================
def test_the_family_is_exactly_seven_labels():
    labels = set(ct.FULL_LABELS) | set(ct.SMALL_LABELS) | {ct.STARTER_LABEL}
    assert labels == {FULL_G, FULL_B, FULL_C, SMALL_G, SMALL_B, SMALL_C, STARTER}


def test_tier_of_reads_each_rung():
    assert ct.tier_of(FULL_G) == ct.TIER_FULL
    assert ct.tier_of(SMALL_B) == ct.TIER_SMALL
    assert ct.tier_of(STARTER) == ct.TIER_STARTER


def test_starter_carries_no_risk_level_but_the_other_six_do():
    assert ct.risk_of(STARTER) is None          # ONE book shared by all three risk levels
    assert ct.risk_of(FULL_C) == "Conservative"
    assert ct.risk_of(SMALL_B) == "Balanced"


def test_thresholds_are_the_house_numbers():
    # Pinned so a silent edit to a boundary breaks a test rather than an account.
    assert (ct.STARTER_DEMOTE_AT, ct.STARTER_THRESHOLD, ct.STARTER_PROMOTE_AT) == (
        4_500.0, 5_000.0, 5_500.0)
    assert (ct.FULL_DEMOTE_AT, ct.FULL_THRESHOLD, ct.FULL_PROMOTE_AT) == (
        22_500.0, 25_000.0, 27_500.0)


# =====================================================================================
# HYSTERESIS — every threshold, both directions.
# =====================================================================================
def test_starter_promotes_to_small_at_5500_not_a_cent_below():
    assert ct.tier_for(5_499.99, current_label=STARTER) == STARTER
    assert ct.tier_for(5_500.0, current_label=STARTER) == SMALL_G


def test_small_demotes_to_starter_below_4500_and_holds_at_4500():
    assert ct.tier_for(4_499.99, current_label=SMALL_B) == STARTER
    assert ct.tier_for(4_500.0, current_label=SMALL_B) == SMALL_B


def test_small_promotes_to_full_at_27500_not_a_cent_below():
    assert ct.tier_for(27_499.99, current_label=SMALL_B) == SMALL_B
    assert ct.tier_for(27_500.0, current_label=SMALL_B) == FULL_B


def test_full_demotes_to_small_below_22500_and_holds_at_22500():
    assert ct.tier_for(22_499.99, current_label=FULL_C) == SMALL_C
    assert ct.tier_for(22_500.0, current_label=FULL_C) == FULL_C


def test_the_band_holds_an_incumbent_still_at_5200_and_at_24000():
    # THE WHOLE POINT OF THE BAND. 5,200 is above the plain 5,000 boundary and 24,000 is
    # below the plain 25,000 one, yet neither incumbent moves: only the promote/demote
    # levels can move an account that already holds a model.
    assert ct.tier_for(5_200.0, current_label=STARTER) == STARTER
    assert ct.tier_for(5_200.0, current_label=SMALL_G) == SMALL_G
    assert ct.tier_for(24_000.0, current_label=SMALL_G) == SMALL_G
    assert ct.tier_for(24_000.0, current_label=FULL_G) == FULL_G


def test_small_to_full_and_back_preserves_the_risk_level():
    assert ct.tier_for(30_000.0, current_label=SMALL_C) == FULL_C
    assert ct.tier_for(10_000.0, current_label=FULL_C) == SMALL_C
    assert ct.tier_for(30_000.0, current_label=SMALL_B) == FULL_B
    assert ct.tier_for(10_000.0, current_label=FULL_B) == SMALL_B


# =====================================================================================
# RULE 4(c) — TWO RUNGS AWAY GOES DIRECTLY TO TARGET, NO BAND, IN ONE STEP.
# Andrew's call 2026-08-25. The earlier one-rung-per-run reading made a Starter account
# funded to $30,000 trade into the 11-line small book tonight and the 15-line full book
# tomorrow — the spread paid TWICE for an intermediate book nobody chose. A band damps
# oscillation around a boundary, and $30,000 is nowhere near the $5,000 boundary.
# =====================================================================================
def test_starter_at_30000_with_balanced_history_goes_straight_to_full_balanced():
    assert ct.tier_for(30_000.0, current_label=STARTER,
                       prior_risk="Balanced") == FULL_B      # one step, not SMALL_B first


def test_starter_at_30000_with_no_history_goes_straight_to_full_growth():
    # Leaving starter recovers the risk level even on the two-rung jump; absent history is
    # Andrew's GROWTH default.
    assert ct.tier_for(30_000.0, current_label=STARTER) == FULL_G
    assert ct.tier_for(30_000.0, current_label=STARTER, prior_risk=None) == FULL_G


def test_full_at_1000_goes_straight_to_starter():
    assert ct.tier_for(1_000.0, current_label=FULL_G) == STARTER
    assert ct.tier_for(1_000.0, current_label=FULL_C) == STARTER
    assert ct.tier_for(0.0, current_label=FULL_B) == STARTER


def test_the_two_rung_jump_consults_neither_band():
    # 25,000 is the plain full boundary; a starter incumbent there is two rungs from full, so
    # neither the 5,500 nor the 27,500 band gets a vote.
    assert ct.tier_for(25_000.0, current_label=STARTER) == FULL_G
    # 4,999.99 is just below the plain starter boundary; a full incumbent goes straight down.
    assert ct.tier_for(4_999.99, current_label=FULL_G) == STARTER


def test_adjacent_band_cases_all_still_hold():
    # Re-confirmation sweep: 4(c) must not have loosened 4(b) anywhere.
    assert ct.tier_for(5_200.0, current_label=STARTER) == STARTER    # natural small, band no
    assert ct.tier_for(24_000.0, current_label=FULL_G) == FULL_G     # natural small, band no
    assert ct.tier_for(4_000.0, current_label=SMALL_B) == STARTER    # natural starter, band yes
    assert ct.tier_for(30_000.0, current_label=SMALL_B) == FULL_B    # natural full, band yes


def test_natural_tier_is_the_plain_boundary_function():
    assert ct.natural_tier(4_999.99) == ct.TIER_STARTER
    assert ct.natural_tier(5_000.0) == ct.TIER_SMALL
    assert ct.natural_tier(24_999.99) == ct.TIER_SMALL
    assert ct.natural_tier(25_000.0) == ct.TIER_FULL


# =====================================================================================
# FIRST ASSIGNMENT — plain boundaries, no band (there is no incumbent to be sticky about).
# =====================================================================================
def test_first_assignment_at_4900_lands_on_starter():
    assert ct.tier_for(4_900.0, current_label=None) == STARTER


def test_first_assignment_at_30000_lands_on_full():
    assert ct.tier_for(30_000.0, current_label=None) == FULL_G


def test_first_assignment_uses_the_plain_5000_and_25000_boundaries():
    assert ct.tier_for(4_999.99, current_label=None) == STARTER
    assert ct.tier_for(5_000.0, current_label=None) == SMALL_G     # band would have said no
    assert ct.tier_for(24_999.99, current_label=None) == SMALL_G
    assert ct.tier_for(25_000.0, current_label=None) == FULL_G     # band would have said no


def test_first_assignment_honours_a_stated_risk_level():
    assert ct.tier_for(10_000.0, current_label=None, prior_risk="Conservative") == SMALL_C
    assert ct.tier_for(50_000.0, current_label=None, prior_risk="Balanced") == FULL_B


# =====================================================================================
# FIRST ASSIGNMENT DRIVEN BY has_prior_custom_assignment — THE CRM DIVERGENCE THIS FIXES.
# An account can HOLD a label and still be a first assignment. Testing "is the label None"
# made the desk treat a freshly assigned account as a sticky incumbent: a $4,900 account
# resolved to Starter (Custom) in the CRM and Growth (Small, Custom) on the desk.
# =====================================================================================
def test_the_4900_divergence_case_now_matches_the_crm():
    # Freshly assigned to the small book at $4,900, no earlier custom model. Plain
    # boundaries -> 4,900 < 5,000 -> Starter. The band would have said stay (4,900 >= 4,500).
    assert ct.tier_for(4_900.0, current_label=SMALL_G, has_prior_assignment=False) == STARTER
    # ... and the incumbent reading, which is what the desk used to do, still does stay.
    assert ct.tier_for(4_900.0, current_label=SMALL_G, has_prior_assignment=True) == SMALL_G


def test_first_assignment_by_flag_uses_the_plain_boundaries_at_every_step():
    for label in (STARTER, SMALL_G, FULL_G):
        assert ct.tier_for(4_999.99, current_label=label, has_prior_assignment=False) == STARTER
        assert ct.tier_for(5_000.0, current_label=label, has_prior_assignment=False) == SMALL_G
        assert ct.tier_for(24_999.99, current_label=label,
                           has_prior_assignment=False) == SMALL_G
        assert ct.tier_for(25_000.0, current_label=label, has_prior_assignment=False) == FULL_G


def test_first_assignment_by_flag_preserves_the_incumbent_risk_level():
    # The label still supplies the risk word where it carries one — "first assignment"
    # changes WHICH BOUNDARIES apply, not where the risk level comes from.
    assert ct.tier_for(30_000.0, current_label=SMALL_C, has_prior_assignment=False) == FULL_C
    assert ct.tier_for(10_000.0, current_label=FULL_B, has_prior_assignment=False) == SMALL_B
    # Starter carries none, so history (then Growth) supplies it.
    assert ct.tier_for(10_000.0, current_label=STARTER, has_prior_assignment=False,
                       prior_risk="Conservative") == SMALL_C
    assert ct.tier_for(10_000.0, current_label=STARTER, has_prior_assignment=False) == SMALL_G


def test_has_prior_true_is_the_incumbent_band_path():
    assert ct.tier_for(5_200.0, current_label=STARTER, has_prior_assignment=True) == STARTER
    assert ct.tier_for(24_000.0, current_label=FULL_G, has_prior_assignment=True) == FULL_G


def test_a_missing_has_prior_flag_fails_toward_the_incumbent():
    # The view lagging the code must NOT silently re-tier a live account off a plain
    # boundary. None == "the CRM did not say" -> treat as incumbent -> the band applies.
    assert ct.tier_for(4_900.0, current_label=SMALL_G, has_prior_assignment=None) == SMALL_G
    assert ct.tier_for(4_900.0, current_label=SMALL_G) == SMALL_G          # default is None
    assert ct.tier_for(5_200.0, current_label=STARTER, has_prior_assignment=None) == STARTER


def test_no_current_label_is_a_first_assignment_whatever_the_flag_says():
    # No custom assignment at all -> there is no incumbent rung to be sticky about, so the
    # plain boundaries apply even if the flag is absent or True.
    for flag in (None, True, False):
        assert ct.tier_for(4_900.0, current_label=None, has_prior_assignment=flag) == STARTER
        assert ct.tier_for(5_000.0, current_label=None, has_prior_assignment=flag) == SMALL_G


def test_has_prior_assignment_from_row_reads_the_crm_field():
    assert ct.has_prior_assignment_from_row({ct.HAS_PRIOR_FIELD: True}) is True
    assert ct.has_prior_assignment_from_row({ct.HAS_PRIOR_FIELD: False}) is False
    assert ct.has_prior_assignment_from_row({ct.HAS_PRIOR_FIELD: None}) is None
    # Absent (a lagging view) is the third state, NOT False.
    assert ct.has_prior_assignment_from_row({"account_number": "U1"}) is None
    assert ct.has_prior_assignment_from_row(object()) is None


# =====================================================================================
# RISK RECOVERY on the way back up off the shared Starter book.
# =====================================================================================
def test_starter_to_small_recovers_balanced_from_history():
    assert ct.tier_for(6_000.0, current_label=STARTER, prior_risk="Balanced") == SMALL_B


def test_starter_to_small_recovers_conservative_from_history():
    assert ct.tier_for(6_000.0, current_label=STARTER, prior_risk="Conservative") == SMALL_C


def test_starter_to_small_with_no_history_goes_to_growth():
    # Andrew's decision 2026-08-25, not a guess: never flagged, never skipped.
    assert ct.tier_for(6_000.0, current_label=STARTER, prior_risk=None) == SMALL_G
    assert ct.tier_for(6_000.0, current_label=STARTER) == SMALL_G


def test_an_unusable_prior_risk_falls_back_to_growth_rather_than_a_bad_label():
    for junk in ("", "  ", "Aggressive", "growth", "Starter", 7):
        assert ct.tier_for(6_000.0, current_label=STARTER, prior_risk=junk) == SMALL_G


def test_prior_risk_is_ignored_where_the_label_already_carries_one():
    # small<->full never consults history; the incumbent label is authoritative.
    assert ct.tier_for(30_000.0, current_label=SMALL_C, prior_risk="Growth") == FULL_C
    assert ct.tier_for(10_000.0, current_label=FULL_B, prior_risk="Growth") == SMALL_B


def test_prior_risk_from_row_reads_the_crm_field_when_it_exists():
    assert ct.prior_risk_from_row({ct.PRIOR_RISK_FIELD: "Balanced"}) == "Balanced"
    assert ct.prior_risk_from_row({ct.PRIOR_RISK_FIELD: None}) is None
    assert ct.prior_risk_from_row({ct.PRIOR_RISK_FIELD: "Aggressive"}) is None
    # A lagging view leaves it absent -> None -> the Growth default. Must not raise.
    assert ct.prior_risk_from_row({"account_number": "U1", "model": STARTER}) is None
    assert ct.prior_risk_from_row(object()) is None


# =====================================================================================
# THE ORIGINAL HAZARD — this function must never touch, or produce, an S0 model.
# =====================================================================================
S0_LABELS = ["Growth", "Balanced", "Conservative",
             "Growth (Small)", "Balanced (Small)", "Conservative (Small)"]


@pytest.mark.parametrize("label", S0_LABELS)
def test_an_s0_label_is_never_in_the_custom_family(label):
    assert ct.tier_of(label) is None
    assert ct.is_custom_family(label) is False


@pytest.mark.parametrize("label", S0_LABELS)
def test_tier_for_refuses_an_s0_label_outright(label):
    # It REFUSES rather than answering. Silently returning something would make this function
    # a second, spelling-based decision-maker over S0 accounts — exactly what it must not be.
    with pytest.raises(ValueError):
        ct.tier_for(1_000_000.0, current_label=label)


@pytest.mark.parametrize("flag", [None, True, False])
@pytest.mark.parametrize("label", [FULL_G, FULL_B, FULL_C, SMALL_G, SMALL_B, SMALL_C, STARTER])
@pytest.mark.parametrize("value", [0.0, 1.0, 4_499.0, 4_500.0, 5_000.0, 5_499.0, 5_500.0,
                                   22_499.0, 22_500.0, 24_999.0, 25_000.0, 27_499.0,
                                   27_500.0, 1_000_000.0])
def test_a_custom_label_can_never_be_rewritten_to_an_s0_model(label, value, flag):
    """THE hazard this module exists to prevent. Every reachable output — every boundary,
    every incumbent, every state of the first-assignment flag — is one of the seven custom
    labels."""
    out = ct.tier_for(value, current_label=label, has_prior_assignment=flag)
    assert ct.is_custom_family(out)
    assert out not in S0_LABELS


@pytest.mark.parametrize("value", [0.0, 4_999.0, 5_000.0, 24_999.0, 25_000.0, 1_000_000.0])
def test_a_first_assignment_can_never_produce_an_s0_model_either(value):
    out = ct.tier_for(value, current_label=None)
    assert ct.is_custom_family(out)
    assert out not in S0_LABELS


def test_the_module_does_not_import_small_tier():
    # Structural, not decorative: small_tier's helpers are spelling-based and one CRM rename
    # away from discarding a hand-authored book. Reading the source is the only way to pin
    # "does not reuse" rather than "happens not to call today".
    import pathlib
    src = pathlib.Path(ct.__file__).read_text(encoding="utf-8")
    for banned in ("import small_tier", "from strategies import small_tier",
                   "parent_version(", "is_small(", "collapse("):
        assert banned not in src, f"custom_tier must not use {banned!r}"


def test_labels_are_matched_exactly_not_by_suffix():
    # "Something (Custom)" is NOT automatically a full-tier label — dispatch is on the closed
    # set, so an unrecognised custom-looking name is refused, not silently re-tiered.
    for lookalike in ["Aggressive (Custom)", "Growth (Custom) v2", "growth (custom)",
                      "Growth (Small, Custom) ARCHIVED", "Starter", "Starter (Small, Custom)"]:
        assert ct.tier_of(lookalike) is None
        with pytest.raises(ValueError):
            ct.tier_for(10_000.0, current_label=lookalike)
