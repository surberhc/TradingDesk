"""Tests for the S0 idle-cash deploy-check DECISION logic (pure; no broker)."""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _p in (str(_HERE), str(_REPO / "connections"), str(_REPO / "paperbot"),
           str(_REPO / "dashboard" / "desk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import s0_cash_deploy_check as job  # noqa: E402


def test_proposes_when_excess_above_threshold():
    # buffer 1.5%, threshold 2% -> trigger when cash fraction > 3.5%
    d = job.decide(net_liq=100_000, total_cash=5_000, buffer=0.015, threshold=0.02)
    assert d["ok"] and d["should_propose"]
    assert round(d["excess_cash"], 2) == round(5_000 - 0.015 * 100_000, 2)


def test_holds_within_band():
    d = job.decide(net_liq=100_000, total_cash=3_000, buffer=0.015, threshold=0.02)
    assert d["ok"] and not d["should_propose"]


def test_exactly_at_threshold_does_not_propose():
    # cash fraction == buffer + threshold exactly -> strictly-greater trigger stays False
    d = job.decide(net_liq=100_000, total_cash=3_500, buffer=0.015, threshold=0.02)
    assert d["ok"] and not d["should_propose"]


def test_bad_navs_never_propose():
    for nl in (0, None, -5):
        d = job.decide(net_liq=nl, total_cash=10_000, buffer=0.015)
        assert not d["ok"] and not d["should_propose"]
    d = job.decide(net_liq=100_000, total_cash=None, buffer=0.015)
    assert not d["ok"] and not d["should_propose"]


def test_build_notice_is_plain_english():
    d = job.decide(net_liq=100_000, total_cash=5_000, buffer=0.015, threshold=0.02)
    title, body, hint = job.build_notice(d)
    assert "Idle cash" in title
    assert job.ACCOUNT in body
    assert "Control Plane" in hint


def test_main_snooze_skips_repost(tmp_path, monkeypatch, capsys):
    """While the operator has the idle-cash notice snoozed, the daily run must SKIP posting —
    the poster-side is_snoozed skip is what actually silences the re-nag (dismiss alone
    re-posts)."""
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "ac.db"))
    # force a should-propose scenario without touching the broker
    monkeypatch.setattr(job, "read_cash",
                        lambda: {"net_liq": 100_000, "total_cash": 5_000})
    import importlib
    import action_center
    importlib.reload(action_center)

    # first run posts the idle-cash notice
    assert job.main([]) == 0
    assert action_center.has_open(job._DEDUP_KEY)

    # operator ignores it for 5 days
    assert action_center.snooze(job._DEDUP_KEY, 5)

    # next run must skip posting; the hidden notice is left untouched
    assert job.main([]) == 0
    assert "snoozed" in capsys.readouterr().out.lower()
    assert action_center.read_notices() == []
    assert action_center.is_snoozed(job._DEDUP_KEY)
