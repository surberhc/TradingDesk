r"""test_heartbeat_digest_overnight.py — the alarm of last resort must survive the night.

THE DEFECT (2026-09-08). Every deadline job is due in the EVENING (18:30 forward, 19:45
gex, 21:00 tiingo, 21:15 eod_report) and the digest may only SEND in the MORNING
(07:00-12:00). The outstanding-problem list was rebuilt from scratch on each 15-minute
sweep and never written down, so the evening's finding was thrown away at midnight and by
07:00 every job read "pre-deadline — no check". The live-data Gateway failed three nights
running (2026-09-02, 03, 04), the log said OUTSTANDING each evening and "0 outstanding"
each morning, and NO EMAIL WAS EVER SENT. Three sessions of SPX/SPXW options data were
lost permanently.

These tests drive the REAL sweep — main(), the same entry point the 15-minute scheduled
task runs — across a simulated overnight boundary. No email is ever sent: the mail path is
stubbed and the state/log/marker files all live under tmp_path.

Run from datacollector/:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest test_heartbeat_digest_overnight.py -q
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import types

import heartbeat_alarm as hba


class _Harness:
    """One simulated desk: a single deadline job, a stubbed mailer, throwaway state."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.mp = monkeypatch
        self.sent: list[tuple[str, str]] = []
        self.status_file = tmp_path / "forward.json"

        monkeypatch.setattr(hba, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(hba, "LOG", tmp_path / "alarm.log")
        monkeypatch.setattr(hba, "RAN_MARKER", tmp_path / "ran.txt")
        monkeypatch.setattr(hba, "JOBS", [])
        monkeypatch.setattr(hba, "DEADLINE_JOBS", [
            {"name": "forward", "label": "IBKR forward options collector",
             "status_file": self.status_file,
             "deadline_hhmm_fallback": (19, 0), "deadline_buffer_min": 60,
             "task_name": "IbkrForwardEodDaily", "market_dependent": True},
        ])
        # No Task Scheduler, no calendar, no Drive check, no real email.
        monkeypatch.setattr(hba, "_latest_task_trigger_hhmm", lambda task_name: (17, 30))
        monkeypatch.setattr(hba, "_is_trading_day", lambda d: True)
        monkeypatch.setattr(hba, "_tripwire", types.SimpleNamespace(
            evaluate=lambda: {"should_page": False}))
        monkeypatch.setattr(hba, "_send", self._capture)
        monkeypatch.setattr(sys, "argv", ["heartbeat_alarm.py"])

    def _capture(self, subject: str, html: str) -> bool:
        self.sent.append((subject, html))
        return True

    def collector_succeeded_on(self, day: dt.date) -> None:
        self.status_file.write_text(json.dumps(
            {"job": "forward", "date": day.strftime("%Y%m%d"), "status": "ok"}))

    def sweep(self, moment: dt.datetime) -> None:
        """One 15-minute sweep at `moment` — the real main(), with a frozen clock."""

        class _DT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return moment

        self.mp.setattr(hba, "dt", types.SimpleNamespace(
            datetime=_DT, date=dt.date, timedelta=dt.timedelta))
        hba._task_deadline_cache.clear()
        hba.main()

    def emails_after(self, moment: dt.datetime) -> list[tuple[str, str]]:
        before = len(self.sent)
        self.sweep(moment)
        return self.sent[before:]


DAY1 = dt.datetime(2026, 9, 2)          # the night the live-data Gateway died
DAY2 = DAY1 + dt.timedelta(days=1)
DAY3 = DAY1 + dt.timedelta(days=2)


def _at(day: dt.datetime, hh: int, mm: int) -> dt.datetime:
    return day.replace(hour=hh, minute=mm)


# --------------------------------------------------------------------------- #
# THE KEY TEST — an evening failure must reach the owner the NEXT MORNING
# --------------------------------------------------------------------------- #
def test_evening_failure_is_reported_next_morning(tmp_path, monkeypatch, capsys):
    """End to end across midnight. The collector never runs on day 1; the digest must
    name it in day 2's morning window. Before the fix this sent nothing, ever."""
    h = _Harness(tmp_path, monkeypatch)

    # Evening of day 1: past the 18:30 deadline, nothing was ever written. Silent.
    assert h.emails_after(_at(DAY1, 19, 0)) == [], "no email may go out in the evening"
    assert h.emails_after(_at(DAY1, 21, 15)) == [], "no email may go out in the evening"
    assert h.emails_after(_at(DAY2, 2, 0)) == [], "no email may go out in the small hours"

    # Morning of day 2, inside the 07:00-12:00 window. THE ALARM MUST FIRE.
    morning = h.emails_after(_at(DAY2, 7, 5))
    assert len(morning) == 1, "last night's failure must be reported this morning"
    subject, html = morning[0]
    assert "1 item needs attention" in subject
    assert "IBKR forward options collector" in html
    assert "2026-09-02 19:00" in html, "the digest must say when it was first seen"

    print("\n--- subject ---\n" + subject)
    print("--- first seen row ---\n" + [
        seg for seg in html.split("<tr>") if "First seen" in seg][0])
    capsys.readouterr()


def test_only_one_email_per_day(tmp_path, monkeypatch):
    """The next sweep is 15 minutes later and the problem is still outstanding. The
    owner's hard rule is one email a day, so nothing more may go out."""
    h = _Harness(tmp_path, monkeypatch)
    h.sweep(_at(DAY1, 19, 0))
    assert len(h.emails_after(_at(DAY2, 7, 5))) == 1
    for hh, mm in [(7, 20), (8, 0), (11, 45)]:
        assert h.emails_after(_at(DAY2, hh, mm)) == [], f"second email at {hh:02d}:{mm:02d}"
    assert len(h.sent) == 1


def test_recovered_job_stops_being_reported(tmp_path, monkeypatch):
    """Nobody clears this by hand. The collector simply works the following night, and
    the record must clear itself — day 3's morning is silent."""
    h = _Harness(tmp_path, monkeypatch)
    h.sweep(_at(DAY1, 19, 0))                     # failed
    h.sweep(_at(DAY2, 7, 5))                      # reported

    h.collector_succeeded_on(DAY2.date())         # the job runs again, successfully
    h.sweep(_at(DAY2, 19, 0))                     # the sweep sees it healthy

    assert json.loads((tmp_path / "state.json").read_text())["_outstanding"] == {}
    assert h.emails_after(_at(DAY3, 7, 5)) == [], "a recovered job must not be reported"
    assert len(h.sent) == 1, "only day 2's digest was ever sent"


def test_second_night_of_the_same_outage_still_reports(tmp_path, monkeypatch):
    """The 09-02..09-04 shape: the outage runs for days. Each morning must report it,
    once, until it is actually fixed."""
    h = _Harness(tmp_path, monkeypatch)
    h.sweep(_at(DAY1, 19, 0))
    assert len(h.emails_after(_at(DAY2, 7, 5))) == 1
    h.sweep(_at(DAY2, 19, 0))
    assert len(h.emails_after(_at(DAY3, 7, 5))) == 1
    assert len(h.sent) == 2, "one email on each of the two mornings"


def test_pre_deadline_does_not_clear_a_standing_problem(tmp_path, monkeypatch):
    """The heart of the bug: at 07:00 the date has rolled over and the job reads
    'pre-deadline — no check'. That is NOT evidence of health and must not clear the
    record."""
    h = _Harness(tmp_path, monkeypatch)
    h.sweep(_at(DAY1, 19, 0))
    h.sweep(_at(DAY2, 6, 30))                     # before the window AND pre-deadline
    book = json.loads((tmp_path / "state.json").read_text())["_outstanding"]
    assert "forward" in book, "a pre-deadline sweep must leave the record alone"
