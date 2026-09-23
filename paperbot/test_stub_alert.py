"""Tests for the stranded-partial-share (STUB) alert.

THE RULE THESE PIN DOWN, and it is the whole feature. A leftover fraction is a problem ONLY
when the ticker is leaving the account entirely — the model no longer holds it and a partial
share is stranded. A fraction sitting on a position the model STILL wants is not a stub and
must produce nothing at all: alerting on it would nag the owner about holdings he has
deliberately decided to keep, and paying a commission to zero it is money wasted.

Everything here is pure — no broker, no client system, no order.

Run from paperbot/:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest -q test_stub_alert.py
"""
from __future__ import annotations

import group_rebalance as gr
import rebalance_engine as re_
import reconcile
import stub_notice


def _line(symbol, target_shares, actual_shares, status, actual_weight=0.0):
    return reconcile.Line(symbol, 0.0 if target_shares == 0 else 0.1, target_shares,
                          actual_shares, actual_weight, 0.0, status)


class _Plan:
    """The two attributes stranded_stubs reads off an AccountPlan: lines and orders."""

    def __init__(self, account, lines, orders):
        self.account = account
        self.lines = lines
        self.orders = orders


# --------------------------------------------------------------- the full-exit test ---
def test_a_dropped_ticker_still_held_is_a_full_exit():
    assert re_.is_full_exit(_line("BIL", 0, 13.8499, reconcile.ROTATE_OUT))


def test_a_holding_the_model_still_wants_is_never_a_full_exit():
    """The heart of the rule: a fraction on a live position is not an exit."""
    assert not re_.is_full_exit(_line("SCHB", 120, 120.8499, "DRIFTED"))
    assert not re_.is_full_exit(_line("SCHB", 120, 120.8499, "MATCHED"))


def test_an_unrecognised_holding_is_not_a_full_exit():
    """ALIEN stays out — a spinoff or a client's own position is never auto-traded."""
    assert not re_.is_full_exit(_line("XYZ", 0, 4.5, reconcile.ALIEN))


def test_the_stub_fraction_is_the_part_no_whole_share_order_can_clear():
    assert round(re_.stub_fraction(13.8499), 4) == 0.8499
    assert round(re_.stub_fraction(-13.8499), 4) == 0.8499
    assert re_.stub_fraction(13.0) == 0.0
    assert re_.stub_fraction(0) == 0.0


# ------------------------------------------------- detection AT the truncation seam ---
def test_a_truncated_fraction_on_a_full_exit_raises_a_stub():
    """13.8499 of a dropped ticker goes out as 13; the 0.8499 left is the stub."""
    plan = _Plan("U7586137", [_line("BIL", 0, 13.8499, reconcile.ROTATE_OUT)],
                 {"BIL": -13.8499})
    stubs = gr.stranded_stubs([plan], prices={"BIL": 100.0})
    assert len(stubs) == 1
    assert stubs[0]["account"] == "U7586137"
    assert stubs[0]["symbol"] == "BIL"
    assert round(stubs[0]["quantity"], 4) == 0.8499
    assert stubs[0]["value"] == 84.99


def test_a_truncated_fraction_on_a_holding_the_model_still_wants_raises_nothing():
    """THE ONE THAT MATTERS. The model still holds SCHB, so the leftover fraction on the
    sell is NOT a stub and nothing is reported — no alert, no page block, no nagging."""
    plan = _Plan("U7586137", [_line("SCHB", 120, 130.8499, "DRIFTED")],
                 {"SCHB": -10.8499})
    assert gr.stranded_stubs([plan], prices={"SCHB": 25.0}) == []


def test_a_clean_whole_share_exit_raises_nothing():
    plan = _Plan("U7586137", [_line("BIL", 0, 13.0, reconcile.ROTATE_OUT)], {"BIL": -13.0})
    assert gr.stranded_stubs([plan], prices={"BIL": 100.0}) == []


def test_a_buy_never_raises_a_stub():
    plan = _Plan("U7586137", [_line("SCHB", 120, 0.0, "MISSING")], {"SCHB": 120})
    assert gr.stranded_stubs([plan], prices={"SCHB": 25.0}) == []


def test_an_unpriced_stub_is_still_reported_with_no_value():
    plan = _Plan("U1", [_line("BIL", 0, 2.25, reconcile.ROTATE_OUT)], {"BIL": -2.25})
    stubs = gr.stranded_stubs([plan])
    assert len(stubs) == 1 and stubs[0]["value"] == 0.0


def test_stubs_come_back_sorted_by_account_then_ticker():
    plans = [_Plan("U2", [_line("BIL", 0, 1.5, reconcile.ROTATE_OUT)], {"BIL": -1.5}),
             _Plan("U1", [_line("VTI", 0, 2.5, reconcile.ROTATE_OUT)], {"VTI": -2.5}),
             _Plan("U1", [_line("BIL", 0, 3.5, reconcile.ROTATE_OUT)], {"BIL": -3.5})]
    assert [(s["account"], s["symbol"]) for s in gr.stranded_stubs(plans)] == [
        ("U1", "BIL"), ("U1", "VTI"), ("U2", "BIL")]


# ------------------------------------------------------------ the client-system alert ---
_ROWS = [{"account": "U7586137", "symbol": "BIL", "quantity": 0.8499, "value": 84.99},
         {"account": "U7586137", "symbol": "VTI", "quantity": 0.25, "value": 70.0}]


def test_the_alert_title_leads_with_capital_stub():
    title, _body, _hint = stub_notice.build_notice(_ROWS)
    assert title.startswith("STUB")
    assert "STUB" in title


def test_the_alert_names_every_account_ticker_and_the_total_value():
    _title, body, _hint = stub_notice.build_notice(_ROWS)
    assert "U7586137" in body and "BIL" in body and "VTI" in body
    assert "$154.99" in body


def test_the_alert_says_it_must_be_cleared_by_hand_and_why():
    _title, body, hint = stub_notice.build_notice(_ROWS)
    assert "Interactive Brokers" in body
    assert "by hand" in body and "by hand" in hint


def test_the_alert_is_plain_english_with_no_desk_jargon():
    """House rule #1. STUB is the one deliberate capitalised word the owner asked for."""
    title, body, hint = stub_notice.build_notice(_ROWS)
    text = f"{title} {body} {hint}"
    for jargon in ("TWS", "FRACTIONAL", "ROTATE_OUT", "10243", "sub-share", "dust"):
        assert jargon not in text


def test_one_dedup_key_so_repeated_runs_do_not_stack():
    assert stub_notice.DEDUP_KEY == "fractional_stub_open"


def test_the_alert_has_its_own_category():
    """Its own kind, so closing a drift alert can never hide a stub."""
    assert stub_notice.KIND == "fractional_stub"
    assert stub_notice.KIND != "outofspec"


def test_posting_nothing_when_there_are_no_stubs():
    assert stub_notice.post([]) == ""
