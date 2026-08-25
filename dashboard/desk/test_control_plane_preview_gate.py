"""Tests for the Control Plane's PREVIEW-DID-IT-ACTUALLY-RUN gate (single account + batch).

THE DEFECT THESE PIN. The Control Plane never looked at the preview subprocess's return
code. A preview whose executor CRASHED printed nothing, parsed to zero legs, and was stored
with a fresh wall-clock timestamp exactly like a healthy preview of an account with nothing
to trade. Three things followed from that:

  * the top-of-page verdict rendered the GREEN "In line — nothing to trade" card off the mere
    ABSENCE of order legs, telling the operator the account had been read and matched its
    target when in fact it had never been read at all;
  * the arm gate (``preview_fresh and confirmed``) was satisfied, because freshness was only
    a wall-clock age check on a timestamp stored unconditionally; and
  * the one amber "the structured plan view was unavailable" note was rendered inside the
    build handler and stored nowhere, so any rerun — typing in the confirm box, pressing the
    armed-state probe — left only the green card on screen.

The batch rail had the same shape plus one of its own: the four tiles are read from the
BATCH-SUMMARY line while the table under them is built from the BATCH-ACCOUNT lines, and
nothing compared the two. A summary claiming three out-of-spec accounts over a table holding
two rendered clean and armed.

What is pinned here: a preview is trusted only on POSITIVE evidence (the program exited
cleanly AND printed either an order list or its own "already conforms" confirmation), that
judgement is stored alongside the timestamp so it survives a rerun, and the batch tiles are
reconciled against the batch table with the arm gate refusing on a mismatch.
"""
import sys
from pathlib import Path

import pytest
import streamlit as st

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import page_control_plane as cp  # noqa: E402

_SESSION_KEYS = ("cp_last_preview", "cp_batch_last_preview")


@pytest.fixture(autouse=True)
def _clean_session():
    for key in _SESSION_KEYS:
        st.session_state.pop(key, None)
    yield
    for key in _SESSION_KEYS:
        st.session_state.pop(key, None)


# --- Realistic executor output, copied in shape from the real scripts ----------------- #
_CONFORMS_STDOUT = "\n".join([
    "    account=U14438624   NetLiq=123,456.78   open_positions=5",
    "[7] DEPLOY order list (0 leg(s); sells first, then buys) — conform=ON:",
    "    (no legs — account already conforms to the target, or nothing to trade)",
    "TRANSMISSION BLOCKED — PREVIEW ONLY. Nothing was placed, armed, or sent.",
])
_PLAN_STDOUT = "\n".join([
    "    account=U14438624   NetLiq=123,456.78   open_positions=5",
    "[7] DEPLOY order list (1 leg(s); sells first, then buys) — conform=ON:",
    "    SELL SPY    x100      LIMIT ~    500.00  notional ~   50,000.00  [plan]  -> target ~0.00%",
    "TRANSMISSION BLOCKED — PREVIEW ONLY. Nothing was placed, armed, or sent.",
])


def _verdict_cards(monkeypatch):
    """Render the top-of-page verdict and return the ('Rebalance status', tier, headline, sub)
    cards it produced. Spies on theme.status_card, which is the only way that verdict is drawn."""
    calls = []
    real = cp.theme.status_card

    def _spy(label, tier, big_text, sub="", **kwargs):
        calls.append((label, tier, big_text, sub))
        return real(label, tier, big_text, sub, **kwargs)

    monkeypatch.setattr(cp.theme, "status_card", _spy)
    cp._render_verdict_and_tiles()
    return [c for c in calls if c[0] == "Rebalance status"]


def _is_green_nothing_to_trade(cards):
    return any(tier == "good" and "in line" in big.lower() for _, tier, big, _ in cards)


# =========================================================================== #
# 1. The pure judgement: did this preview actually run and read the account?   #
# =========================================================================== #
def test_nonzero_return_code_is_a_failure():
    reason = cp._preview_failure(cp._parse_preview(""), 1)
    assert reason is not None
    assert "exit code 1" in reason


def test_nonzero_return_code_fails_even_with_a_partial_plan():
    """A crash halfway through still printed legs; the legs are not evidence it finished."""
    reason = cp._preview_failure(cp._parse_preview(_PLAN_STDOUT), 3)
    assert reason is not None and "exit code 3" in reason


def test_empty_output_with_a_clean_exit_is_still_a_failure():
    """Exit code 0 but nothing readable is NOT 'nothing to trade' — it is 'nothing read'."""
    reason = cp._preview_failure(cp._parse_preview(""), 0)
    assert reason is not None
    assert "no readable plan" in reason.lower() or "did not confirm" in reason.lower()


def test_already_conforms_output_is_not_a_failure():
    assert cp._preview_failure(cp._parse_preview(_CONFORMS_STDOUT), 0) is None


def test_real_plan_output_is_not_a_failure():
    assert cp._preview_failure(cp._parse_preview(_PLAN_STDOUT), 0) is None


# =========================================================================== #
# 2. A crashed preview does not arm, and does not render green.                #
# =========================================================================== #
def test_crashed_preview_does_not_satisfy_the_arm_gate():
    cp._store_last_preview("", returncode=1)
    _age, fresh = cp._preview_freshness()
    assert fresh, "the stored timestamp is still young — the age check alone cannot save us"
    assert cp._stored_preview_failure("cp_last_preview") is not None, (
        "a crashed preview must be recorded as unusable, so the arm gate refuses it")


def test_the_arm_gate_itself_refuses_a_fresh_but_failed_preview():
    """The gate expression both rails use, on its own: freshness is necessary and no longer
    sufficient. This is the line that used to read `preview_fresh and confirmed`."""
    assert cp._preview_is_armable(True, None) is True
    assert cp._preview_is_armable(True, "the check did not complete") is False
    assert cp._preview_is_armable(False, None) is False
    assert cp._preview_is_armable(False, "the check did not complete") is False


def test_the_arm_gate_refuses_the_stored_crashed_preview_end_to_end():
    cp._store_last_preview("", returncode=1)
    _age, fresh = cp._preview_freshness()
    assert not cp._preview_is_armable(
        fresh, cp._stored_preview_failure("cp_last_preview"))


def test_the_arm_gate_still_allows_a_healthy_preview_end_to_end():
    cp._store_last_preview(_PLAN_STDOUT, returncode=0)
    _age, fresh = cp._preview_freshness()
    assert cp._preview_is_armable(fresh, cp._stored_preview_failure("cp_last_preview"))


def test_the_batch_arm_gate_refuses_a_mismatched_batch_end_to_end():
    stdout = "\n".join([
        _batch_row("U1"), _batch_row("U2"),
        _batch_summary(roster=5, out_of_spec=3, in_spec=1, skipped=1, total_legs=9),
    ])
    cp._store_batch_last_preview(stdout, returncode=0)
    _age, fresh = cp._batch_preview_freshness()
    assert not cp._preview_is_armable(
        fresh, cp._stored_preview_failure("cp_batch_last_preview"))


def test_crashed_preview_does_not_render_the_green_verdict(monkeypatch):
    cp._store_last_preview("", returncode=1)
    cards = _verdict_cards(monkeypatch)
    assert cards, "the verdict must still say something"
    assert not _is_green_nothing_to_trade(cards)
    assert any(tier == "bad" for _, tier, _, _ in cards)


def test_crashed_preview_with_partial_output_does_not_arm_or_render_green(monkeypatch):
    cp._store_last_preview(_PLAN_STDOUT, returncode=2)
    assert cp._stored_preview_failure("cp_last_preview") is not None
    assert not _is_green_nothing_to_trade(_verdict_cards(monkeypatch))


def test_failure_survives_a_rerun(monkeypatch):
    """The operator types in the confirm box (a rerun). Nothing re-runs the subprocess; the
    verdict is redrawn from session state alone. It must still refuse."""
    cp._store_last_preview("", returncode=1)
    _verdict_cards(monkeypatch)                       # first render (the build run)
    cards = _verdict_cards(monkeypatch)               # the rerun
    assert not _is_green_nothing_to_trade(cards)
    assert any(tier == "bad" for _, tier, _, _ in cards)
    assert cp._stored_preview_failure("cp_last_preview") is not None


def test_a_failed_attempt_retires_an_earlier_good_preview():
    """Build a good preview, then a run that dies. The older, still-fresh preview must not
    remain armable behind the failure."""
    cp._store_last_preview(_PLAN_STDOUT, returncode=0)
    assert cp._stored_preview_failure("cp_last_preview") is None
    cp._store_failed_preview("cp_last_preview",
                             "The preview timed out reaching the gateway.")
    assert cp._stored_preview_failure("cp_last_preview") is not None


# =========================================================================== #
# 3. A genuinely in-line account still reads green and still arms.             #
# =========================================================================== #
def test_in_line_account_renders_green_and_arms(monkeypatch):
    cp._store_last_preview(_CONFORMS_STDOUT, returncode=0)
    assert cp._stored_preview_failure("cp_last_preview") is None
    _age, fresh = cp._preview_freshness()
    assert fresh
    assert _is_green_nothing_to_trade(_verdict_cards(monkeypatch))


def test_account_with_a_plan_arms_and_reads_as_trades_to_do(monkeypatch):
    cp._store_last_preview(_PLAN_STDOUT, returncode=0)
    assert cp._stored_preview_failure("cp_last_preview") is None
    cards = _verdict_cards(monkeypatch)
    assert not _is_green_nothing_to_trade(cards)
    assert any("trade(s) to rebalance" in big for _, _, big, _ in cards)


def test_an_empty_preview_is_never_green_even_through_the_old_call_shape(monkeypatch):
    """Stated in the ORIGINAL call shape (no return code at all) so it exercises nothing but
    the verdict's own reasoning: zero legs is not evidence the account is in line."""
    cp._store_last_preview("")
    assert not _is_green_nothing_to_trade(_verdict_cards(monkeypatch))


def test_a_stored_preview_without_a_success_marker_fails_closed():
    st.session_state["cp_last_preview"] = {"built_at": None, "n_legs": 0}
    assert cp._stored_preview_failure("cp_last_preview") is not None


def test_no_preview_at_all_is_not_reported_as_a_failure():
    """'Not checked yet' is its own state, already blocked upstream; do not cry failure."""
    assert cp._stored_preview_failure("cp_last_preview") is None


# =========================================================================== #
# 4. The batch tiles are reconciled against the batch table.                   #
# =========================================================================== #
def _batch_row(account="U1", label="Growth", legs=3, sells=2, buys=1, status="OUT_OF_SPEC"):
    return (f"    BATCH-ACCOUNT account={account} version={label} status={status} "
            f"legs={legs} sells={sells} buys={buys} margin_preflight_ok=True")


def _batch_summary(roster=4, out_of_spec=2, in_spec=1, skipped=1, total_legs=6):
    return (f"    BATCH-SUMMARY roster={roster} out_of_spec={out_of_spec} in_spec={in_spec} "
            f"skipped={skipped} total_legs={total_legs} total_sells=12345.67 "
            f"total_buys=8910.11")


_CONSISTENT_BATCH = "\n".join([
    _batch_row("U1"), _batch_row("U2"),
    _batch_summary(roster=4, out_of_spec=2, in_spec=1, skipped=1, total_legs=6),
])


def test_more_accounts_claimed_than_rows_rendered_is_reconciled():
    stdout = "\n".join([
        _batch_row("U1"), _batch_row("U2"),
        _batch_summary(roster=5, out_of_spec=3, in_spec=1, skipped=1, total_legs=9),
    ])
    warning = cp._batch_reconciliation_warning(cp._parse_batch_preview(stdout))
    assert warning is not None
    assert "3 accounts" in warning and "2 account rows" in warning
    assert "Nothing was transmitted." in warning


def test_leg_totals_that_disagree_are_reconciled():
    stdout = "\n".join([
        _batch_row("U1", legs=3), _batch_row("U2", legs=3),
        _batch_summary(roster=4, out_of_spec=2, in_spec=1, skipped=1, total_legs=99),
    ])
    warning = cp._batch_reconciliation_warning(cp._parse_batch_preview(stdout))
    assert warning is not None and "99" in warning and "6" in warning


def test_the_mismatch_is_said_out_loud_in_the_main_batch_warning():
    """Same style, same place as the unreadable-rows warning: one plain-English block above
    the table."""
    stdout = "\n".join([
        _batch_row("U1"),
        _batch_summary(roster=5, out_of_spec=3, in_spec=1, skipped=1, total_legs=9),
    ])
    warning = cp._batch_parse_warning(cp._parse_batch_preview(stdout))
    assert warning is not None and "3 accounts" in warning


def test_batch_mismatch_blocks_the_arm_gate():
    stdout = "\n".join([
        _batch_row("U1"), _batch_row("U2"),
        _batch_summary(roster=5, out_of_spec=3, in_spec=1, skipped=1, total_legs=9),
    ])
    cp._store_batch_last_preview(stdout, returncode=0)
    _age, fresh = cp._batch_preview_freshness()
    assert fresh
    assert cp._stored_preview_failure("cp_batch_last_preview") is not None


def test_consistent_batch_preview_still_arms():
    cp._store_batch_last_preview(_CONSISTENT_BATCH, returncode=0)
    assert cp._batch_reconciliation_warning(
        cp._parse_batch_preview(_CONSISTENT_BATCH)) is None
    assert cp._stored_preview_failure("cp_batch_last_preview") is None


def test_batch_with_nothing_out_of_spec_still_arms():
    """A whole roster that already conforms prints no account rows and a zero summary —
    positive evidence, not an empty read."""
    stdout = _batch_summary(roster=3, out_of_spec=0, in_spec=3, skipped=0, total_legs=0)
    cp._store_batch_last_preview(stdout, returncode=0)
    assert cp._stored_preview_failure("cp_batch_last_preview") is None


def test_crashed_batch_preview_does_not_arm():
    cp._store_batch_last_preview("", returncode=1)
    _age, fresh = cp._batch_preview_freshness()
    assert fresh
    assert cp._stored_preview_failure("cp_batch_last_preview") is not None


def test_batch_preview_with_no_totals_line_does_not_arm():
    """No BATCH-SUMMARY line means there is nothing to check the table against."""
    cp._store_batch_last_preview(_batch_row("U1"), returncode=0)
    assert cp._stored_preview_failure("cp_batch_last_preview") is not None


def test_batch_unreadable_row_blocks_through_the_reconciliation():
    stdout = "\n".join([
        _batch_row("U1"),
        "    BATCH-ACCOUNT account=U2 version=Growth status=OUT_OF_SPEC legs=oops",
        _batch_summary(roster=4, out_of_spec=2, in_spec=1, skipped=1, total_legs=6),
    ])
    cp._store_batch_last_preview(stdout, returncode=0)
    assert cp._stored_preview_failure("cp_batch_last_preview") is not None
