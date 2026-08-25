"""Tests for the Control Plane's parsing of the batch/single executor stdout (pure).

THE DEFECT THESE PIN. ``version=`` on a BATCH-ACCOUNT line carries the CRM's MODEL LABEL, and
real labels contain spaces and brackets — "Growth (Small)", "Growth (Custom)",
"Balanced (Small, Custom)". The pattern read that field as ``\\S+``, which stops at the first
space, so the WHOLE line failed to match and ``_parse_batch_preview`` skipped it with no
trace: every account on a spaced label was silently absent from the operator's preview table,
which still looked complete. "Growth (Small)" is an existing S0 small-tier label, so this was
live before custom allocations existed.

Two things are pinned here: the label parses, and an UNREADABLE line is counted and turned
into a visible plain-English warning instead of vanishing.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import page_control_plane as cp  # noqa: E402

# The six custom labels the CRM can publish, plus the three S0 small-tier labels, plus the
# plain parent labels. Every one of these must survive the round trip.
CUSTOM_LABELS = [
    "Growth (Custom)",
    "Balanced (Custom)",
    "Conservative (Custom)",
    "Growth (Small, Custom)",
    "Balanced (Small, Custom)",
    "Conservative (Small, Custom)",
]
SMALL_LABELS = ["Growth (Small)", "Balanced (Small)", "Conservative (Small)"]
PLAIN_LABELS = ["Growth", "Balanced", "Conservative"]


def _acct_line(label, account="U14438624", status="OUT_OF_SPEC",
               legs=3, sells=2, buys=1, margin="True"):
    """Byte-for-byte the line batch_rebalance_execute.py prints (four-space indent)."""
    return (f"    BATCH-ACCOUNT account={account} version={label} status={status} "
            f"legs={legs} sells={sells} buys={buys} margin_preflight_ok={margin}")


_SUMMARY_LINE = ("    BATCH-SUMMARY roster=4 out_of_spec=2 in_spec=1 skipped=1 "
                 "total_legs=6 total_sells=12345.67 total_buys=8910.11")


def _summary_line(roster, out_of_spec, in_spec, skipped, total_legs):
    """A BATCH-SUMMARY line that AGREES with the account rows the test prints alongside it.

    The executor prints exactly one BATCH-ACCOUNT line per out-of-spec account, and its
    total_legs is the sum of those rows' legs (batch_rebalance_execute.summarize_batch), so a
    fixture whose summary contradicts its rows is output the executor could never produce.
    Two tests below reused the shared _SUMMARY_LINE while printing a different number of rows
    — harmless while the only thing under test was row parsing, but the Control Plane now
    reconciles the tiles against the table, so each of those fixtures states its own
    consistent summary."""
    return (f"    BATCH-SUMMARY roster={roster} out_of_spec={out_of_spec} in_spec={in_spec} "
            f"skipped={skipped} total_legs={total_legs} total_sells=12345.67 "
            f"total_buys=8910.11")


# --------------------------------------------------------------------------- #
# 1. Every real model label parses, spaces and brackets included.              #
# --------------------------------------------------------------------------- #
def test_every_custom_label_parses():
    for label in CUSTOM_LABELS:
        parsed = cp._parse_batch_preview(_acct_line(label))
        assert len(parsed["accounts"]) == 1, f"{label!r} was dropped from the preview"
        assert parsed["accounts"][0]["version"] == label
        assert parsed["accounts"][0]["status"] == "OUT_OF_SPEC"
        assert parsed["unreadable_account_lines"] == []


def test_small_tier_labels_parse():
    """"Growth (Small)" is a LIVE S0 label — it has always contained a space."""
    for label in SMALL_LABELS:
        parsed = cp._parse_batch_preview(_acct_line(label))
        assert len(parsed["accounts"]) == 1, f"{label!r} was dropped from the preview"
        assert parsed["accounts"][0]["version"] == label


def test_plain_labels_still_parse_regression():
    for label in PLAIN_LABELS:
        parsed = cp._parse_batch_preview(_acct_line(label))
        assert len(parsed["accounts"]) == 1
        assert parsed["accounts"][0]["version"] == label


def test_spaced_label_keeps_every_other_field_correct():
    """The lazy version= must not eat the fields after it."""
    parsed = cp._parse_batch_preview(
        _acct_line("Balanced (Small, Custom)", account="U99", status="IN_SPEC",
                   legs=7, sells=4, buys=3, margin="False"))
    a = parsed["accounts"][0]
    assert a == {"account": "U99", "version": "Balanced (Small, Custom)",
                 "status": "IN_SPEC", "legs": 7, "sells": 4, "buys": 3,
                 "margin_preflight_ok": False}


def test_mixed_roster_returns_every_account():
    """A roster holding plain, small-tier and custom labels loses nobody."""
    labels = ["Growth", "Growth (Small)", "Growth (Custom)", "Conservative (Small, Custom)"]
    # FOUR out-of-spec rows at the _acct_line default of 3 legs each = 12 legs, so the summary
    # says out_of_spec=4 and total_legs=12 (roster 4 out of spec + 1 in spec + 1 skipped = 6).
    # This fixture used the shared _SUMMARY_LINE, which claims 2 out-of-spec accounts and 6
    # legs — a contradiction the reconciliation now catches. The test's purpose (every label
    # survives the round trip, nobody is dropped) is untouched.
    summary = _summary_line(roster=6, out_of_spec=4, in_spec=1, skipped=1, total_legs=12)
    stdout = "\n".join(
        _acct_line(lb, account=f"U{i}") for i, lb in enumerate(labels)) + "\n" + summary
    parsed = cp._parse_batch_preview(stdout)
    assert [a["version"] for a in parsed["accounts"]] == labels
    assert [a["account"] for a in parsed["accounts"]] == [f"U{i}" for i in range(len(labels))]
    assert cp._batch_parse_warning(parsed) is None


def test_summary_line_still_parses():
    parsed = cp._parse_batch_preview(_SUMMARY_LINE)
    assert parsed["summary"] == {
        "roster": 4, "out_of_spec": 2, "in_spec": 1, "skipped": 1,
        "total_legs": 6, "total_sells": 12345.67, "total_buys": 8910.11}
    assert parsed["unreadable_summary_lines"] == []


def test_batch_account_header_line_is_not_mistaken_for_a_data_row():
    """The human header prints "BATCH ACCOUNT" (a space); only "BATCH-ACCOUNT" is data."""
    parsed = cp._parse_batch_preview(
        "--- BATCH ACCOUNT U14438624 [Growth (Small)] (purpose=REBALANCE) ---")
    assert parsed["accounts"] == []
    assert parsed["unreadable_account_lines"] == []
    assert cp._batch_parse_warning(parsed) is None


# --------------------------------------------------------------------------- #
# 2. An unreadable row is COUNTED and SURFACED, never dropped in silence.      #
# --------------------------------------------------------------------------- #
def test_unreadable_account_row_is_counted_not_dropped():
    stdout = "\n".join([
        _acct_line("Growth (Custom)", account="U1"),
        "    BATCH-ACCOUNT account=U2 version=Growth status=OUT_OF_SPEC legs=oops",
        _SUMMARY_LINE,
    ])
    parsed = cp._parse_batch_preview(stdout)
    assert len(parsed["accounts"]) == 1
    assert len(parsed["unreadable_account_lines"]) == 1
    assert "U2" in parsed["unreadable_account_lines"][0]


def test_unreadable_account_row_produces_a_visible_plain_english_warning():
    stdout = "\n".join([
        "    BATCH-ACCOUNT account=U1 version=Growth status=OUT_OF_SPEC legs=oops",
        "    BATCH-ACCOUNT account=U2 legs=1",
        "    BATCH-ACCOUNT",
        _SUMMARY_LINE,
    ])
    warning = cp._batch_parse_warning(cp._parse_batch_preview(stdout))
    assert warning is not None
    assert "3 account rows could not be read" in warning
    assert "missing from this table" in warning
    assert "Nothing was transmitted." in warning


def test_single_unreadable_row_uses_singular_plain_english():
    stdout = "    BATCH-ACCOUNT account=U1 legs=1"
    warning = cp._batch_parse_warning(cp._parse_batch_preview(stdout))
    assert "1 account row could not be read" in warning
    assert "is missing from this table" in warning


def test_unreadable_summary_line_is_surfaced():
    stdout = "    BATCH-SUMMARY roster=4 out_of_spec=two in_spec=1"
    parsed = cp._parse_batch_preview(stdout)
    assert parsed["summary"] is None
    assert len(parsed["unreadable_summary_lines"]) == 1
    warning = cp._batch_parse_warning(parsed)
    assert "1 batch total line could not be read" in warning
    assert "may be wrong" in warning


def test_clean_output_produces_no_warning():
    # ONE out-of-spec row at the _acct_line default of 3 legs, so the summary says
    # out_of_spec=1 and total_legs=3 (roster 1 out of spec + 2 in spec + 1 skipped = 4). This
    # fixture used the shared _SUMMARY_LINE, which claims 2 out-of-spec accounts and 6 legs
    # over a single row — a contradiction, and no longer "clean output". The test's purpose
    # (readable output raises no warning) is untouched.
    stdout = (_acct_line("Growth (Small)") + "\n"
              + _summary_line(roster=4, out_of_spec=1, in_spec=2, skipped=1, total_legs=3))
    assert cp._batch_parse_warning(cp._parse_batch_preview(stdout)) is None


def test_parse_never_raises_on_garbage():
    for junk in ("", "\x00\x01", "BATCH-ACCOUNT" * 200, "no batch lines here at all"):
        parsed = cp._parse_batch_preview(junk)
        assert isinstance(parsed["accounts"], list)


def test_parse_error_is_reported_not_swallowed():
    parsed = {"accounts": [], "unreadable_account_lines": [],
              "unreadable_summary_lines": [], "parse_error": "ValueError: boom"}
    warning = cp._batch_parse_warning(parsed)
    assert "stopped early" in warning and "ValueError: boom" in warning


# --------------------------------------------------------------------------- #
# 3. The single-account rail's leg parser has the same shape — same treatment. #
# --------------------------------------------------------------------------- #
_GOOD_LEG = ("    SELL SPY    x100      LIMIT ~    500.00  notional ~   50,000.00  "
             "[plan]  -> target ~0.00%")


def test_single_account_leg_still_parses_regression():
    parsed = cp._parse_preview(_GOOD_LEG)
    assert len(parsed["legs"]) == 1
    assert parsed["legs"][0]["symbol"] == "SPY"
    assert parsed["unreadable_leg_lines"] == []
    assert cp._preview_parse_warning(parsed) is None


def test_single_account_unreadable_leg_is_counted_and_warned():
    stdout = _GOOD_LEG + "\n" + "    BUY QQQ x50 LIMIT ~ four hundred  notional ~ ???  [plan]"
    parsed = cp._parse_preview(stdout)
    assert len(parsed["legs"]) == 1
    assert len(parsed["unreadable_leg_lines"]) == 1
    warning = cp._preview_parse_warning(parsed)
    assert "1 order line could not be read" in warning
    assert "Nothing was transmitted." in warning


def test_single_account_phase_log_lines_are_not_mistaken_for_legs():
    """Engine phase chatter starts with "[PHASE]", not SELL/BUY — no false alarm."""
    stdout = "\n".join([
        _GOOD_LEG,
        "    [PHASE 1] SENT SELL SPY x100 LIMIT 500.00 (working)",
        "    TOTALS   sells ~50,000.00   buys ~0.00",
    ])
    parsed = cp._parse_preview(stdout)
    assert parsed["unreadable_leg_lines"] == []
    assert cp._preview_parse_warning(parsed) is None


def test_single_account_header_read_still_parses():
    parsed = cp._parse_preview(
        "    account=U14438624   NetLiq=123,456.78   open_positions=5")
    assert parsed["account"] == "U14438624"
    assert parsed["net_liq"] == 123456.78
    assert parsed["open_positions"] == 5
