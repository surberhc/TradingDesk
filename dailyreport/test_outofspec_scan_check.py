"""Tests for the consolidated whole-book out-of-spec check.

Covers the PURE notice/detail assembly and the poster-side snooze skip (the piece that
silences the daily re-nag). The CRM/engine scan itself is monkeypatched — this file never
touches a broker or the live CRM.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _p in (str(_HERE), str(_REPO / "connections"), str(_REPO / "paperbot"),
           str(_REPO / "dashboard" / "desk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import outofspec_scan_check as job  # noqa: E402


def _scan(n_oos=2, n_acct=10):
    # U1 holds an individual bond: HELD ASIDE (priced, counted, never traded) and sitting
    # outside the model allocation, so its 4 legs rebalance the MANAGED sleeve only.
    verdicts = [
        {"account": "U1", "version": "Growth", "advisor_name": "Amy", "net_liq": 1_000_000.0,
         "managed_net_liq": 900_000.0, "held_aside_value": 100_000.0,
         "out_of_spec": True, "n_legs": 4, "n_held_aside": 1, "n_unclassified": 0,
         "blocked": False},
        {"account": "U2", "version": "Growth", "advisor_name": None, "net_liq": 50_000.0,
         "managed_net_liq": 50_000.0, "held_aside_value": 0.0,
         "out_of_spec": True, "n_legs": 2, "n_held_aside": 0, "n_unclassified": 0,
         "blocked": False},
        {"account": "U3", "version": "Growth", "advisor_name": "Amy", "net_liq": 25_000.0,
         "managed_net_liq": 25_000.0, "held_aside_value": 0.0,
         "out_of_spec": False, "n_legs": 0, "n_held_aside": 0, "n_unclassified": 0,
         "blocked": False},
    ]
    return {"verdicts": verdicts, "skipped": [], "n_accounts": n_acct,
            "n_out_of_spec": n_oos, "n_in_spec": n_acct - n_oos, "bad_versions": []}


def test_build_detail_only_out_of_spec_rows():
    detail = job.build_detail(_scan())
    accts = [r["account"] for r in detail]
    assert accts == ["U1", "U2"]           # in-spec U3 excluded
    assert detail[0]["n_held_aside"] == 1                 # holds a bond
    assert detail[0]["held_aside_value"] == 100_000.0
    assert detail[0]["managed_net_liq"] == 900_000.0      # what the model actually manages
    assert detail[1]["n_held_aside"] == 0
    assert detail[0]["net_liq"] == 1_000_000.0
    assert detail[0]["held_back"] is False


def test_build_notice_plain_english_and_counts():
    title, body, hint, detail = job.build_notice(_scan(n_oos=2, n_acct=10))
    assert title == "2 of 10 accounts out of spec — rebalance needed"
    assert "out of spec" in body.lower()
    # Held-aside holdings are explained as never-traded, NOT as a pending manual sale.
    assert "never trades" in body.lower()
    assert "outside the model allocation" in body.lower()
    assert "liquidation" not in body.lower()
    assert "Trade Execution" in hint
    assert len(detail) == 2


def test_build_notice_flags_unidentified_and_held_back_accounts():
    scan = _scan()
    scan["verdicts"][0]["n_unclassified"] = 1
    scan["verdicts"][1].update({"n_held_aside": 1, "blocked": True})
    _, body, _, detail = job.build_notice(scan)
    assert "could not identify" in body.lower()
    assert "held back" in body.lower()
    assert detail[1]["held_back"] is True


def test_main_skips_repost_while_an_alert_is_already_open(crm, monkeypatch, capsys):
    """The poster-side skip that silences the daily re-nag. Snooze is gone: the job now skips
    because an OPEN alert is already sitting in the CRM, and it stays skipped until Andrew
    closes that task."""
    monkeypatch.setattr(job, "run_scan", lambda: _scan())

    # first run posts ONE consolidated alert
    assert job.main([]) == 0
    assert len(crm) == 1
    assert crm[0]["category"] == "outofspec"
    assert crm[0]["dedup_key"] == "outofspec_open"

    # next scheduled run must SKIP posting while that alert is still open
    assert job.main([]) == 0
    assert "snoozed" in capsys.readouterr().out.lower()
    assert len(crm) == 1                       # untouched, not duplicated

    # once Andrew closes it in the CRM, the next run may raise it again
    crm[0]["status"] = "done"
    assert job.main([]) == 0
    assert len(crm) == 2


def test_main_posts_nothing_when_all_in_spec(crm, monkeypatch, capsys):
    monkeypatch.setattr(job, "run_scan",
                        lambda: {"verdicts": [], "skipped": [], "n_accounts": 5,
                                 "n_out_of_spec": 0, "n_in_spec": 5, "bad_versions": []})
    assert job.main([]) == 0
    assert crm == []


def test_main_dry_run_posts_nothing(crm, monkeypatch, capsys):
    monkeypatch.setattr(job, "run_scan", lambda: _scan())
    assert job.main(["--dry-run"]) == 0
    assert crm == []
    assert "WOULD post" in capsys.readouterr().out
