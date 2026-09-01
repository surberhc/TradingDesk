"""Tests for the Control Plane's BATCH MODEL SCOPE — the selector, the flag it passes to the
executor, the scope-aware confirm phrase, and the gate that refuses to arm a scope the
operator never previewed.

THE DEFECT THESE PIN. The Control Plane shelled out to batch_rebalance_execute.py with NO
account filter and a confirm phrase reading "REBALANCE ALL": the only run available was the
whole book. Once a scope exists, two new ways to send the wrong thing appear, and both are
closed here:

  * PREVIEW 14, SEND 185. The preview and the send are separate button presses with the scope
    selector sitting between them. A preview may only ever arm the EXACT scope it reviewed;
    change the selector and the gate refuses until a new preview is built.
  * A CONFIRM PHRASE THAT LIES. "REBALANCE ALL" typed over a three-model run says the opposite
    of what is about to happen, so a scoped run types "REBALANCE SELECTED" instead — and the
    phrase displayed and the phrase required both come from one function, so they cannot
    disagree.

Everything here is pure/offline: no subprocess, no gateway, no CRM.
"""
import sys
from pathlib import Path

import streamlit as st

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import page_control_plane as cp  # noqa: E402

SCOPE = ["Growth (Custom)", "Balanced (Custom)", "Growth (Small, Custom)"]


def _batch_row(account="U1", label="Growth", legs=3, sells=2, buys=1, status="OUT_OF_SPEC"):
    return (f"    BATCH-ACCOUNT account={account} version={label} status={status} "
            f"legs={legs} sells={sells} buys={buys} margin_preflight_ok=True")


def _batch_summary(roster=4, out_of_spec=2, in_spec=1, skipped=1, total_legs=6):
    return (f"    BATCH-SUMMARY roster={roster} out_of_spec={out_of_spec} in_spec={in_spec} "
            f"skipped={skipped} total_legs={total_legs} total_sells=12345.67 "
            f"total_buys=8910.11")


_HEALTHY_BATCH = "\n".join([
    _batch_row("U1"), _batch_row("U2"),
    _batch_summary(roster=4, out_of_spec=2, in_spec=1, skipped=1, total_legs=6),
])


def _clean():
    for key in ("cp_batch_last_preview", "cp_batch_model_choices"):
        st.session_state.pop(key, None)


# =========================================================================== #
# 1. The normalised scope: order and spacing are not a change of plan.        #
# =========================================================================== #
def test_scope_key_normalises_order_case_kept_and_whitespace():
    assert cp._scope_key([" Growth (Custom) ", "Balanced (Custom)"]) == [
        "Balanced (Custom)", "Growth (Custom)"]
    assert cp._scope_key(["Balanced (Custom)", "Growth (Custom)"]) == cp._scope_key(
        ["Growth (Custom)", "Balanced (Custom)"])


def test_scope_key_treats_nothing_selected_as_the_whole_book():
    assert cp._scope_key(None) == []
    assert cp._scope_key([]) == []
    assert cp._scope_key(["", "   "]) == []


def test_scope_key_dedupes():
    assert cp._scope_key(["Growth", "Growth"]) == ["Growth"]


# =========================================================================== #
# 2. The flag handed to the executor.                                         #
# =========================================================================== #
def test_no_selection_omits_the_flag_entirely():
    """An unscoped run must be the byte-for-byte pre-existing command line."""
    assert cp._batch_models_flag(None) == []
    assert cp._batch_models_flag([]) == []


def test_the_flag_is_one_comma_joined_argv_element():
    flag = cp._batch_models_flag(SCOPE)
    assert len(flag) == 1
    assert flag[0].startswith("--models=")
    # The labels themselves carry spaces, brackets and an internal comma; the executor's own
    # parser (batch_rebalance_execute.models_requested) reads them back whole.
    assert "Growth (Small, Custom)" in flag[0]


def test_the_executor_parses_back_exactly_what_the_page_sends():
    """END TO END on the wire format: what this page writes is what the executor reads."""
    sys.path.insert(0, str(Path(cp.REPO) / "paperbot"))
    import batch_rebalance_execute as bre

    flag = cp._batch_models_flag(SCOPE)
    assert bre.models_requested(flag) == cp._scope_key(SCOPE)


# =========================================================================== #
# 3. The scope-aware confirm phrase.                                          #
# =========================================================================== #
def test_the_whole_book_still_types_rebalance_all():
    assert cp._batch_confirm_phrase(None) == "REBALANCE ALL"
    assert cp._batch_confirm_phrase([]) == cp.BATCH_CONFIRM_PHRASE


def test_a_scoped_run_types_rebalance_selected():
    assert cp._batch_confirm_phrase(SCOPE) == "REBALANCE SELECTED"
    assert cp._batch_confirm_phrase(SCOPE) == cp.BATCH_CONFIRM_PHRASE_SCOPED


def test_the_two_phrases_are_different():
    """If they were the same string, typing 'ALL' would send a subset and nobody would see it."""
    assert cp.BATCH_CONFIRM_PHRASE != cp.BATCH_CONFIRM_PHRASE_SCOPED


# =========================================================================== #
# 4. THE SAFETY PROPERTY — preview 14, send 14.                               #
# =========================================================================== #
def test_the_same_scope_is_not_a_mismatch():
    assert cp._batch_scope_mismatch(SCOPE, SCOPE) is None
    assert cp._batch_scope_mismatch(None, []) is None


def test_reordering_the_selection_is_not_a_mismatch():
    assert cp._batch_scope_mismatch(list(reversed(SCOPE)), SCOPE) is None


def test_widening_a_scoped_preview_to_the_whole_book_is_refused():
    """PREVIEW 14, SEND 185 — the exact thing this gate exists to stop."""
    note = cp._batch_scope_mismatch([], SCOPE)
    assert note is not None
    assert "whole book" in note and "Growth (Custom)" in note


def test_narrowing_a_whole_book_preview_is_also_refused():
    """Sending less than was reviewed is still not what was reviewed."""
    assert cp._batch_scope_mismatch(SCOPE, []) is not None


def test_swapping_one_model_is_refused():
    assert cp._batch_scope_mismatch(["Growth (Custom)"], ["Balanced (Custom)"]) is not None


def test_a_preview_stored_without_a_scope_cannot_arm_a_scoped_run():
    """An older stored preview (or one recorded by the failure path) carries no scope. It reads
    as the whole book, so a scoped selection is a mismatch — fail closed."""
    assert cp._batch_scope_mismatch(SCOPE, None) is not None


# =========================================================================== #
# 5. End to end through session state — the stored preview remembers its scope. #
# =========================================================================== #
def test_the_stored_preview_records_the_scope_it_was_built_with():
    _clean()
    try:
        cp._store_batch_last_preview(_HEALTHY_BATCH, returncode=0, models=SCOPE)
        stored = st.session_state["cp_batch_last_preview"]
        assert stored["scope"] == cp._scope_key(SCOPE)
        assert stored["ok"] is True            # the existing gates are untouched
    finally:
        _clean()


def test_a_healthy_scoped_preview_arms_only_its_own_scope():
    _clean()
    try:
        cp._store_batch_last_preview(_HEALTHY_BATCH, returncode=0, models=SCOPE)
        stored = st.session_state["cp_batch_last_preview"]
        _age, fresh = cp._batch_preview_freshness()
        usable = cp._preview_is_armable(
            fresh, cp._stored_preview_failure("cp_batch_last_preview"))
        assert usable, "the freshness/completeness gates must still pass"

        # Same scope -> armable.
        assert cp._batch_scope_mismatch(SCOPE, stored["scope"]) is None
        # Widened selector -> the SEND button is dead even though the preview is fresh, a
        # complete read, and internally consistent.
        assert cp._batch_scope_mismatch([], stored["scope"]) is not None
        assert not (usable and cp._batch_scope_mismatch([], stored["scope"]) is None)
    finally:
        _clean()


def test_an_unscoped_preview_still_arms_the_whole_book():
    """The pre-existing whole-book flow is untouched: no selection, no mismatch."""
    _clean()
    try:
        cp._store_batch_last_preview(_HEALTHY_BATCH, returncode=0)
        stored = st.session_state["cp_batch_last_preview"]
        assert stored["scope"] == []
        _age, fresh = cp._batch_preview_freshness()
        assert cp._preview_is_armable(
            fresh, cp._stored_preview_failure("cp_batch_last_preview"))
        assert cp._batch_scope_mismatch([], stored["scope"]) is None
    finally:
        _clean()


# =========================================================================== #
# 6. The model-choice list never breaks the page.                             #
# =========================================================================== #
def test_model_choices_degrade_to_an_empty_list_when_the_crm_is_unreachable(monkeypatch):
    _clean()
    try:
        import roster
        monkeypatch.setattr(
            roster, "crm_enrolled_roster_scan",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("CRM down")))
        assert cp._batch_model_choices() == []
    finally:
        _clean()


def test_model_choices_are_read_once_and_cached(monkeypatch):
    _clean()
    try:
        import roster
        calls = []

        def _scan(*a, **k):
            calls.append(1)
            return {"models": ["Balanced", "Growth (Custom)"], "accounts": [], "held": [],
                    "unfunded": [], "scope": []}

        monkeypatch.setattr(roster, "crm_enrolled_roster_scan", _scan)
        assert cp._batch_model_choices() == ["Balanced", "Growth (Custom)"]
        assert cp._batch_model_choices() == ["Balanced", "Growth (Custom)"]
        assert len(calls) == 1, "Streamlit reruns this page on every keystroke"
    finally:
        _clean()
