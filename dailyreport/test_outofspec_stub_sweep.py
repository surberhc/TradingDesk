"""Tests for the nightly stranded-partial-share sweep, the safety net half of the STUB alert.

The stub is meant to be caught the moment the exit truncates, on the Trade Execution page.
This sweep is the "just in case" — and it applies the SAME rule: only a fraction whose ticker
the model no longer holds at all, never a remainder on a live position.

Hermetic. The autouse ``crm`` fixture in conftest.py stands in for the client system's task
table, so nothing here can post to the live Action Center.

Run from dailyreport/:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest -q
"""
from __future__ import annotations

import outofspec_scan_check as job
import reconcile
import crm_outofspec


_CLEAN = {"verdicts": [], "skipped": [], "excluded": [], "n_accounts": 3, "n_roster": 3,
          "n_out_of_spec": 0, "n_unmonitored": 0, "unresolved_models": [], "stubs": []}


def _scan(**over):
    out = dict(_CLEAN)
    out.update(over)
    return out


def _line(symbol, target_shares, actual_shares, status, actual_weight=0.0):
    return reconcile.Line(symbol, 0.0 if target_shares == 0 else 0.1, target_shares,
                          actual_shares, actual_weight, 0.0, status)


class _Plan:
    def __init__(self, account, lines):
        self.account = account
        self.lines = lines
        self.orders = {}
        self.net_liq = 100_000.0
        self.managed_net_liq = 100_000.0
        self.needs_rebalance = False
        self.held_aside = []
        self.held_aside_value = 0.0
        self.blocked_reasons = []
        self.unpriced_reasons = []
        self.alien_lines = []


# ----------------------------------------------- the rule, read off the whole-book scan ---
def test_a_dropped_ticker_with_a_partial_share_left_is_swept_up():
    plan = _Plan("U7586137", [_line("BIL", 0, 0.8499, reconcile.FRACTIONAL,
                                    actual_weight=0.00085)])
    verdicts = crm_outofspec.verdicts_from_plans(
        [plan], [{"account": "U7586137", "version": "Growth"}])
    assert verdicts[0]["stubs"] == [{"account": "U7586137", "symbol": "BIL",
                                     "quantity": 0.8499, "value": 85.0}]


def test_a_partial_share_on_a_holding_the_model_still_wants_is_never_swept_up():
    """THE ONE THAT MATTERS — the sweep must not nag about a live position's remainder."""
    plan = _Plan("U7586137", [_line("SCHB", 120, 120.8499, "DRIFTED", actual_weight=0.30)])
    verdicts = crm_outofspec.verdicts_from_plans(
        [plan], [{"account": "U7586137", "version": "Growth"}])
    assert verdicts[0]["stubs"] == []


def test_a_whole_position_the_model_dropped_is_not_a_stub():
    """13 whole shares of a dropped ticker is an ordinary sell, not a stranded fraction."""
    plan = _Plan("U1", [_line("BIL", 0, 13.8499, reconcile.ROTATE_OUT, actual_weight=0.01)])
    verdicts = crm_outofspec.verdicts_from_plans([plan], [{"account": "U1"}])
    assert verdicts[0]["stubs"] == []


def test_an_unrecognised_holding_is_not_a_stub():
    plan = _Plan("U1", [_line("XYZ", 0, 0.4, reconcile.ALIEN, actual_weight=0.001)])
    verdicts = crm_outofspec.verdicts_from_plans([plan], [{"account": "U1"}])
    assert verdicts[0]["stubs"] == []


def test_a_stub_does_not_make_an_account_out_of_spec():
    """A stub is not tradeable; making it breach created a permanent re-trade loop once."""
    plan = _Plan("U1", [_line("BIL", 0, 0.8499, reconcile.FRACTIONAL, actual_weight=0.0008)])
    verdicts = crm_outofspec.verdicts_from_plans([plan], [{"account": "U1"}])
    assert verdicts[0]["stubs"] and verdicts[0]["out_of_spec"] is False


def test_the_merge_flattens_every_accounts_stubs_across_the_book():
    merged = job._merge_scans([
        {"verdicts": [{"account": "U2", "out_of_spec": False, "net_liq": 1.0,
                       "n_held_aside": 0, "held_aside_value": 0.0, "blocked": False,
                       "stubs": [{"account": "U2", "symbol": "BIL", "quantity": 0.5,
                                  "value": 50.0}]}]},
        {"verdicts": [{"account": "U1", "out_of_spec": False, "net_liq": 2.0,
                       "n_held_aside": 0, "held_aside_value": 0.0, "blocked": False,
                       "stubs": [{"account": "U1", "symbol": "VTI", "quantity": 0.25,
                                  "value": 70.0}]}]}])
    assert [(s["account"], s["symbol"]) for s in merged["stubs"]] == [
        ("U1", "VTI"), ("U2", "BIL")]


# -------------------------------------------------------------- what the job actually posts ---
def test_the_nightly_job_posts_a_stub_alert_titled_in_capitals(monkeypatch, crm, capsys):
    monkeypatch.setattr(job, "run_scan", lambda: _scan(stubs=[
        {"account": "U7586137", "symbol": "BIL", "quantity": 0.8499, "value": 84.99}]))
    assert job.main([]) == job._EXIT_OK
    posted = [r for r in crm if r["category"] == "fractional_stub"]
    assert len(posted) == 1
    assert posted[0]["title"].startswith("STUB")
    assert "U7586137" in posted[0]["description"] and "BIL" in posted[0]["description"]
    assert posted[0]["dedup_key"] == "fractional_stub_open"


def test_a_clean_book_posts_no_stub_alert(monkeypatch, crm, capsys):
    monkeypatch.setattr(job, "run_scan", lambda: _scan())
    assert job.main([]) == job._EXIT_OK
    assert [r for r in crm if r["category"] == "fractional_stub"] == []
    assert "No partial shares are stranded" in capsys.readouterr().out


def test_a_second_run_does_not_stack_a_duplicate(monkeypatch, crm):
    monkeypatch.setattr(job, "run_scan", lambda: _scan(stubs=[
        {"account": "U1", "symbol": "BIL", "quantity": 0.5, "value": 50.0}]))
    job.main([])
    job.main([])
    assert len([r for r in crm if r["category"] == "fractional_stub"]) == 1


def test_the_dry_run_posts_nothing(monkeypatch, crm, capsys):
    monkeypatch.setattr(job, "run_scan", lambda: _scan(stubs=[
        {"account": "U1", "symbol": "BIL", "quantity": 0.5, "value": 50.0}]))
    job.main(["--dry-run"])
    assert crm == []
    assert "WOULD post" in capsys.readouterr().out


def test_the_stub_alert_is_raised_even_when_the_drift_section_returns_early(
        monkeypatch, crm):
    """n_out_of_spec == 0 returns out of main() before the drift notice. The stub alert is
    ordered ahead of that on purpose, so it can never be lost behind the early return."""
    monkeypatch.setattr(job, "run_scan", lambda: _scan(n_out_of_spec=0, stubs=[
        {"account": "U1", "symbol": "BIL", "quantity": 0.5, "value": 50.0}]))
    job.main([])
    assert len([r for r in crm if r["category"] == "fractional_stub"]) == 1
