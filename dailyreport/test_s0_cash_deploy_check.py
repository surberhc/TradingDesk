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
    assert "Trade Execution" in hint


def test_main_skips_repost_while_an_alert_is_already_open(crm, monkeypatch, capsys):
    """While an idle-cash alert is still OPEN in the CRM, the daily run must SKIP posting —
    that poster-side skip is what silences the re-nag. Andrew closing the task in the CRM is
    what lets the alert be raised again.

    The job no longer pre-checks this itself: it calls post_notice and reads the SKIPPED
    answer. And it never claims the operator snoozed anything, because nobody did."""
    import action_center

    # force a should-propose scenario without touching the broker
    monkeypatch.setattr(job, "read_cash",
                        lambda: {"net_liq": 100_000, "total_cash": 5_000})

    # first run posts the idle-cash alert
    assert job.main([]) == 0
    assert action_center.has_open(job._DEDUP_KEY)
    assert len(crm) == 1

    # next run must skip posting; the open alert is left untouched
    assert job.main([]) == 0
    out = capsys.readouterr().out.lower()
    assert "already open" in out
    assert "snoozed" not in out
    assert len(crm) == 1
    assert action_center.has_open(job._DEDUP_KEY)


def test_main_reports_failure_when_the_alert_cannot_be_filed(crm_write_broken, monkeypatch,
                                                             capsys):
    """An outage on the reporting channel is a FAILED run. Non-zero exit, and never a claim
    that the operator snoozed a notice nobody snoozed."""
    monkeypatch.setattr(job, "read_cash",
                        lambda: {"net_liq": 100_000, "total_cash": 5_000})

    assert job.main([]) == 1
    assert crm_write_broken == []
    assert "snoozed" not in capsys.readouterr().out.lower()


def test_main_survives_a_failed_duplicate_check_and_posts_nothing(crm, monkeypatch):
    """If the CRM cannot be asked whether an alert is already open, the job must post nothing
    and still finish normally. The desk can file a task but cannot delete one, so a duplicate
    filed during a blip would have to be dismissed by hand; a report held back tonight is
    raised again by tomorrow night's run."""
    import action_center
    import conftest

    class _SelectBrokenCursor(conftest._FakeCursor):
        """Would happily accept an insert, but breaks on the "is one already open?" read."""

        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("SELECT"):
                raise RuntimeError("the connection dropped while reading")
            super().execute(sql, params)

    class _SelectBrokenConn(conftest._FakeConn):
        def cursor(self):
            return _SelectBrokenCursor(self.rows)

    monkeypatch.setattr(job, "read_cash",
                        lambda: {"net_liq": 100_000, "total_cash": 5_000})
    monkeypatch.setattr(action_center, "_connect", lambda: _SelectBrokenConn(crm))

    assert job.main([]) == 0        # the reporting channel broke; the job did not
    assert crm == []                # and no alert was written
