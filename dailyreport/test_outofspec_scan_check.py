"""Tests for the consolidated whole-book out-of-spec check.

Covers the PURE notice/detail assembly and the poster-side snooze skip (the piece that
silences the daily re-nag). The CRM/engine scan itself is monkeypatched — this file never
touches a broker or the live CRM.
"""
import json
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
    assert "Control Plane" in hint
    assert len(detail) == 2


def test_build_notice_flags_unidentified_and_held_back_accounts():
    scan = _scan()
    scan["verdicts"][0]["n_unclassified"] = 1
    scan["verdicts"][1].update({"n_held_aside": 1, "blocked": True})
    _, body, _, detail = job.build_notice(scan)
    assert "could not identify" in body.lower()
    assert "held back" in body.lower()
    assert detail[1]["held_back"] is True


def test_main_snooze_skips_repost(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "ac.db"))
    monkeypatch.setattr(job, "run_scan", lambda: _scan())
    import importlib
    import action_center
    importlib.reload(action_center)

    # first run posts ONE consolidated notice with detail_json
    assert job.main([]) == 0
    n = action_center.read_notices()
    assert len(n) == 1 and n[0]["kind"] == "outofspec"
    assert len(json.loads(n[0]["detail_json"])) == 2
    first_ts = n[0]["ts"]

    # operator ignores it for 10 days
    assert action_center.snooze("outofspec_open", 10)

    # next scheduled run must SKIP posting while snoozed
    assert job.main([]) == 0
    out = capsys.readouterr().out
    assert "snoozed" in out.lower()
    assert action_center.read_notices() == []          # still hidden
    snz = action_center.read_snoozed()
    assert len(snz) == 1 and snz[0]["ts"] == first_ts   # untouched, not refreshed


def test_main_posts_nothing_when_all_in_spec(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "ac2.db"))
    monkeypatch.setattr(job, "run_scan",
                        lambda: {"verdicts": [], "skipped": [], "n_accounts": 5,
                                 "n_out_of_spec": 0, "n_in_spec": 5, "bad_versions": []})
    import importlib
    import action_center
    importlib.reload(action_center)
    assert job.main([]) == 0
    assert action_center.read_notices() == []


def test_main_dry_run_posts_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "ac3.db"))
    monkeypatch.setattr(job, "run_scan", lambda: _scan())
    import importlib
    import action_center
    importlib.reload(action_center)
    assert job.main(["--dry-run"]) == 0
    assert action_center.read_notices() == []
    assert "WOULD post" in capsys.readouterr().out
