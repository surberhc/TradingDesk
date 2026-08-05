"""test_heartbeat_alarm.py — regression tests for the staleness-alarm suppression logic.

CENTERPIECE: reproduce the 2026-07-05 silent death. During a ~3.75h ThetaData terminal
outage the universe supervisor was NOT running, yet the progress JSON still carried a
leftover 'complete: true' from a prior scope. The old assess() checked completion BEFORE
freshness, so for hours it logged "universe_dl: COMPLETE — no alert" over a dead job. The
fix makes freshness gate completion: a stale complete flag beside a COLD/absent heartbeat
must ALERT. These tests pin that, and pin that a genuinely-fresh completion still suppresses
(no false page) so the fix didn't over-correct.

Run from datacollector/ so `import config` (which heartbeat_alarm imports) resolves:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest test_heartbeat_alarm.py -q
"""

from __future__ import annotations

import datetime as dt
import json
import os

import heartbeat_alarm as hba


THRESHOLD_S = 60 * 60  # matches the universe_dl job's 60-min staleness window


def _write(path, payload: dict, age_s: float, now: float) -> None:
    """Write a JSON file and back-date its mtime to now-age_s (simulate staleness)."""
    path.write_text(json.dumps(payload))
    mtime = now - age_s
    os.utime(path, (mtime, mtime))


def _job(tmp_path, name="universe_dl"):
    return {
        "name": name,
        "label": "expanded-universe options downloader",
        "heartbeat": tmp_path / "hb.txt",
        "progress": tmp_path / "progress.json",
        "threshold_s": THRESHOLD_S,
        "task_name": "UniverseDownloadEod",
    }


# --------------------------------------------------------------------------- #
# THE 07-05 FAILURE: stale complete:true + cold/absent heartbeat  ->  MUST ALERT
# --------------------------------------------------------------------------- #
def test_stale_complete_flag_with_cold_heartbeat_alerts(tmp_path):
    """Progress says complete:true but its file — and the heartbeat — are STALE
    (older than the threshold). The old code returned complete/no-alert; the fix must
    ALERT (status 'stale')."""
    now = dt.datetime.now().timestamp()
    job = _job(tmp_path)
    # Heartbeat exists but is COLD (2h old, past the 1h threshold), with an ordinary
    # (non-COMPLETE) last line — the supervisor died mid-run.
    _write_text = job["heartbeat"]
    _write_text.write_text("2026-07-05 02:00:00  shard 3 alive done=25866/399600\n")
    stale_age = 2 * 3600
    os.utime(_write_text, (now - stale_age, now - stale_age))
    # Stale leftover 'complete: true' from a PRIOR scope, also 2h old.
    _write(job["progress"], {"complete": True, "done": 25866, "total": 399600, "pct": 6.47},
           age_s=stale_age, now=now)

    a = hba.assess(job, now)
    assert a["alert"] is True, "stale complete:true beside a cold heartbeat must ALERT"
    assert a["status"] == "stale"


def test_stale_complete_flag_with_absent_heartbeat_alerts(tmp_path):
    """Same trap via the no-heartbeat-file branch: heartbeat absent + a STALE progress
    file carrying complete:true must ALERT (status 'missing'), not suppress."""
    now = dt.datetime.now().timestamp()
    job = _job(tmp_path)
    assert not job["heartbeat"].exists()
    _write(job["progress"], {"complete": True, "done": 25866, "total": 399600},
           age_s=2 * 3600, now=now)

    a = hba.assess(job, now)
    assert a["alert"] is True, "stale complete:true with no heartbeat must ALERT"
    assert a["status"] == "missing"


def test_stale_COMPLETE_marker_in_cold_heartbeat_alerts(tmp_path):
    """The text-marker path has the same hole: a COLD heartbeat whose last line still
    says COMPLETE (leftover) must ALERT, not suppress."""
    now = dt.datetime.now().timestamp()
    job = _job(tmp_path)
    hb = job["heartbeat"]
    hb.write_text("2026-07-05 01:00:00  COMPLETE — scope finished\n")
    os.utime(hb, (now - 2 * 3600, now - 2 * 3600))

    a = hba.assess(job, now)
    assert a["alert"] is True, "a cold heartbeat with a stale COMPLETE line must ALERT"
    assert a["status"] == "stale"


# --------------------------------------------------------------------------- #
# LEGIT cases must still suppress (no false page)
# --------------------------------------------------------------------------- #
def test_fresh_complete_flag_still_suppresses(tmp_path):
    """A job that genuinely just finished: FRESH heartbeat + FRESH progress complete:true
    must still suppress (status 'complete', no alert)."""
    now = dt.datetime.now().timestamp()
    job = _job(tmp_path)
    hb = job["heartbeat"]
    hb.write_text("2026-07-06 10:00:00  shard done\n")
    os.utime(hb, (now - 60, now - 60))  # 1 min old = fresh
    _write(job["progress"], {"complete": True, "done": 399600, "total": 399600, "pct": 100.0},
           age_s=60, now=now)

    a = hba.assess(job, now)
    assert a["alert"] is False, "a freshly-finished job must NOT alert"
    assert a["status"] == "complete"


def test_fresh_COMPLETE_marker_still_suppresses(tmp_path):
    """FRESH heartbeat whose last line says COMPLETE must suppress (legit finish)."""
    now = dt.datetime.now().timestamp()
    job = _job(tmp_path)
    hb = job["heartbeat"]
    hb.write_text("2026-07-06 10:00:00  COMPLETE — scope finished\n")
    os.utime(hb, (now - 60, now - 60))

    a = hba.assess(job, now)
    assert a["alert"] is False
    assert a["status"] == "complete"


def test_fresh_in_progress_heartbeat_no_alert(tmp_path):
    """The live state today: fresh heartbeat, progress NOT complete -> fresh, no alert."""
    now = dt.datetime.now().timestamp()
    job = _job(tmp_path)
    hb = job["heartbeat"]
    hb.write_text("2026-07-06 10:44:00  shard 4 alive\n")
    os.utime(hb, (now - 30, now - 30))
    _write(job["progress"], {"complete": False, "done": 25886, "total": 399600, "pct": 6.48},
           age_s=30, now=now)

    a = hba.assess(job, now)
    assert a["alert"] is False
    assert a["status"] == "fresh"


def test_cold_in_progress_heartbeat_alerts(tmp_path):
    """Baseline: a cold heartbeat with no completion flag anywhere must alert (the
    original supervisor-death case). Guards against the fix breaking normal staleness."""
    now = dt.datetime.now().timestamp()
    job = _job(tmp_path)
    hb = job["heartbeat"]
    hb.write_text("2026-07-05 02:00:00  shard alive\n")
    os.utime(hb, (now - 2 * 3600, now - 2 * 3600))
    _write(job["progress"], {"complete": False, "done": 100, "total": 399600},
           age_s=2 * 3600, now=now)

    a = hba.assess(job, now)
    assert a["alert"] is True
    assert a["status"] == "stale"


# --------------------------------------------------------------------------- #
# _progress_pct: the universe job's real keys must render (not "unknown")
# --------------------------------------------------------------------------- #
def test_progress_pct_reads_universe_done_total(tmp_path):
    """Audit fix: _progress_pct read only days_done/days_total, so the universe job
    (done/total) showed 'unknown'. It must now render the real numbers."""
    p = tmp_path / "progress.json"
    p.write_text(json.dumps({"done": 25866, "total": 399600, "pct": 6.47, "complete": False}))
    s = hba._progress_pct(p)
    assert s != "unknown"
    assert "25866" in s and "399600" in s and "6.47%" in s


def test_progress_pct_reads_spxw_days(tmp_path):
    """The SPXW schema (days_done/days_total) must still render, labeled 'days'."""
    p = tmp_path / "progress.json"
    p.write_text(json.dumps({"days_done": 1052, "days_total": 1173, "pct": 89.7}))
    s = hba._progress_pct(p)
    assert "1052" in s and "1173" in s and "days" in s


# --------------------------------------------------------------------------- #
# 2026-07-09 fix: live Task Scheduler deadline lookup (stop hand-maintaining a
# second copy of the schedule that silently drifted and caused false pages).
# --------------------------------------------------------------------------- #
_FAKE_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <TimeTrigger>
      <StartBoundary>2026-07-09T19:00:00-05:00</StartBoundary>
    </TimeTrigger>
    <TimeTrigger>
      <StartBoundary>2026-07-09T20:45:00-05:00</StartBoundary>
    </TimeTrigger>
  </Triggers>
</Task>
"""


class _FakeCompletedProcess:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_latest_task_trigger_hhmm_picks_later_of_two_triggers(monkeypatch):
    """Tiingo-shaped case: two TimeTriggers (19:00 and 20:45) -> must return the
    LATER one (20:45), not the first in document order."""
    hba._task_deadline_cache.clear()

    def fake_run(*args, **kwargs):
        return _FakeCompletedProcess(_FAKE_TASK_XML)

    monkeypatch.setattr(hba.subprocess, "run", fake_run)
    result = hba._latest_task_trigger_hhmm("TiingoDailyUpdate")
    assert result == (20, 45)


def test_latest_task_trigger_hhmm_is_cached_per_process(monkeypatch):
    """Second call for the same task name must NOT re-invoke subprocess (module-level
    cache for the life of the process)."""
    hba._task_deadline_cache.clear()
    calls = {"n": 0}

    def fake_run(*args, **kwargs):
        calls["n"] += 1
        return _FakeCompletedProcess(_FAKE_TASK_XML)

    monkeypatch.setattr(hba.subprocess, "run", fake_run)
    first = hba._latest_task_trigger_hhmm("TiingoDailyUpdate")
    second = hba._latest_task_trigger_hhmm("TiingoDailyUpdate")
    assert first == second == (20, 45)
    assert calls["n"] == 1


def test_latest_task_trigger_hhmm_none_when_subprocess_raises(monkeypatch):
    """schtasks unavailable / times out -> must return None, never raise."""
    hba._task_deadline_cache.clear()

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("schtasks not found")

    monkeypatch.setattr(hba.subprocess, "run", fake_run)
    assert hba._latest_task_trigger_hhmm("NoSuchTask") is None


def test_latest_task_trigger_hhmm_none_on_malformed_xml(monkeypatch):
    """Corrupt/truncated XML from schtasks must return None, never raise."""
    hba._task_deadline_cache.clear()

    def fake_run(*args, **kwargs):
        return _FakeCompletedProcess("<Task><Triggers><Broken")

    monkeypatch.setattr(hba.subprocess, "run", fake_run)
    assert hba._latest_task_trigger_hhmm("BrokenTask") is None


def test_latest_task_trigger_hhmm_none_when_no_start_boundary(monkeypatch):
    """A task with only a boot/logon trigger (no StartBoundary at all) must return
    None gracefully rather than crash or invent a time."""
    hba._task_deadline_cache.clear()
    xml_no_time_trigger = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        '<Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>'
        '</Task>'
    )

    def fake_run(*args, **kwargs):
        return _FakeCompletedProcess(xml_no_time_trigger)

    monkeypatch.setattr(hba.subprocess, "run", fake_run)
    assert hba._latest_task_trigger_hhmm("LogonOnlyTask") is None


def test_latest_task_trigger_hhmm_none_when_returncode_nonzero(monkeypatch):
    """Task not found -> schtasks exits non-zero; must return None, not raise."""
    hba._task_deadline_cache.clear()

    def fake_run(*args, **kwargs):
        return _FakeCompletedProcess("", returncode=1)

    monkeypatch.setattr(hba.subprocess, "run", fake_run)
    assert hba._latest_task_trigger_hhmm("DeletedTask") is None


def _deadline_job(tmp_path, **overrides):
    job = {
        "name": "tiingo",
        "label": "Tiingo daily data refresh",
        "status_file": tmp_path / "tiingo.json",
        "deadline_hhmm_fallback": (21, 0),
        "deadline_buffer_min": 15,
        "task_name": "TiingoDailyUpdate",
        "market_dependent": False,  # avoid the market-calendar dependency in this test
    }
    job.update(overrides)
    return job


def test_handle_deadline_uses_live_trigger_plus_buffer(tmp_path, monkeypatch):
    """When the live query succeeds (20:45 latest trigger), the effective deadline
    must be 20:45 + 15min buffer = 21:00 -> a run at 20:50 is still pre-deadline."""
    hba._task_deadline_cache.clear()
    monkeypatch.setattr(hba, "_latest_task_trigger_hhmm", lambda task_name: (20, 45))
    job = _deadline_job(tmp_path)
    state: dict = {}
    now_dt = dt.datetime.now().replace(hour=20, minute=50, second=0, microsecond=0)
    line, problem = hba.handle_deadline(job, state, now_dt.timestamp())
    assert "pre-deadline" in line
    assert "21:00" in line
    assert problem is None  # pre-deadline is never an outstanding problem


def test_handle_deadline_falls_back_and_logs_when_live_query_fails(tmp_path, monkeypatch, capsys):
    """If the live Task Scheduler lookup returns None (task renamed/deleted/schtasks
    unavailable), handle_deadline must fall back to deadline_hhmm_fallback, log one
    informational line about the fallback, and must NOT itself alarm/email for that
    reason alone."""
    hba._task_deadline_cache.clear()
    monkeypatch.setattr(hba, "_latest_task_trigger_hhmm", lambda task_name: None)
    job = _deadline_job(tmp_path)
    state: dict = {}
    # Before the fallback deadline (21:00) -> should just report pre-deadline, no email.
    now_dt = dt.datetime.now().replace(hour=20, minute=0, second=0, microsecond=0)
    line, problem = hba.handle_deadline(job, state, now_dt.timestamp())
    assert "pre-deadline" in line
    assert "21:00" in line  # fallback value, unchanged by any buffer
    assert problem is None

    out = capsys.readouterr().out
    assert "fallback" in out.lower()
    assert "TiingoDailyUpdate" in out


# --------------------------------------------------------------------------- #
# 2026-07-20 fix: first-run grace on the ABSENT-heartbeat path (missing_ok_until).
# A newly-activated job whose first scheduled run hasn't come due yet has a
# legitimately absent heartbeat and must NOT false-page. The grace applies ONLY to
# the absent path — a cold/stale heartbeat still pages regardless — and fails safe
# (missing/unparseable field -> pages exactly as before).
# --------------------------------------------------------------------------- #
def _iso(now: float, delta_s: float) -> str:
    """An ISO-8601 timestamp with tz offset at now+delta_s, for missing_ok_until."""
    return dt.datetime.fromtimestamp(now + delta_s).astimezone().isoformat()


def test_absent_heartbeat_with_future_grace_no_alert(tmp_path):
    """Absent heartbeat + missing_ok_until in the FUTURE -> NOT an alert: the first
    scheduled run simply hasn't come due yet (status 'pending_first_run')."""
    now = dt.datetime.now().timestamp()
    job = _job(tmp_path, name="new_job")
    job["progress"] = None
    job["missing_ok_until"] = _iso(now, +6 * 3600)  # 6h in the future
    assert not job["heartbeat"].exists()

    a = hba.assess(job, now)
    assert a["alert"] is False, "absent heartbeat under future grace must NOT alert"
    assert a["status"] == "pending_first_run"
    assert a["status"] != "complete"  # must not borrow the special 'complete' word


def test_absent_heartbeat_with_past_grace_alerts(tmp_path):
    """Absent heartbeat + missing_ok_until in the PAST -> unchanged behaviour: the
    first run is genuinely overdue, so status 'missing' and ALERT."""
    now = dt.datetime.now().timestamp()
    job = _job(tmp_path, name="new_job")
    job["progress"] = None
    job["missing_ok_until"] = _iso(now, -3600)  # 1h ago
    assert not job["heartbeat"].exists()

    a = hba.assess(job, now)
    assert a["alert"] is True, "absent heartbeat past the grace must ALERT"
    assert a["status"] == "missing"


def test_absent_heartbeat_without_grace_alerts(tmp_path):
    """Absent heartbeat + NO missing_ok_until -> unchanged behaviour (missing/alert).
    Jobs without the field must behave exactly as before."""
    now = dt.datetime.now().timestamp()
    job = _job(tmp_path, name="new_job")
    job["progress"] = None
    assert "missing_ok_until" not in job
    assert not job["heartbeat"].exists()

    a = hba.assess(job, now)
    assert a["alert"] is True
    assert a["status"] == "missing"


def test_cold_heartbeat_with_future_grace_still_alerts(tmp_path):
    """The grace must NOT touch the COLD/STALE path: a heartbeat that EXISTS but is
    old is a job that ran once and went cold — a real failure — and must still ALERT
    even with a FUTURE missing_ok_until set."""
    now = dt.datetime.now().timestamp()
    job = _job(tmp_path, name="new_job")
    job["progress"] = None
    job["missing_ok_until"] = _iso(now, +6 * 3600)  # future grace present
    hb = job["heartbeat"]
    hb.write_text("2026-07-20 21:30:00  rclone copy started\n")
    os.utime(hb, (now - 2 * THRESHOLD_S, now - 2 * THRESHOLD_S))  # well past threshold

    a = hba.assess(job, now)
    assert a["alert"] is True, "a cold existing heartbeat must ALERT despite future grace"
    assert a["status"] == "stale"


def test_absent_heartbeat_with_unparseable_grace_alerts(tmp_path):
    """An unparseable missing_ok_until must FAIL SAFE toward the existing behaviour:
    do not crash, do not suppress -> status 'missing' and ALERT."""
    now = dt.datetime.now().timestamp()
    job = _job(tmp_path, name="new_job")
    job["progress"] = None
    job["missing_ok_until"] = "not-a-timestamp"
    assert not job["heartbeat"].exists()

    a = hba.assess(job, now)
    assert a["alert"] is True, "an unparseable grace must never suppress a real alert"
    assert a["status"] == "missing"


# --------------------------------------------------------------------------- #
# 2026-08-05: ONE-EMAIL-A-DAY consolidated morning digest, silent overnight.
# The 15-min sweep still assesses/logs every entry; only EMAIL is gated. The digest
# sends at most once per calendar day, only inside [DIGEST_SEND_HOUR,
# DIGEST_WINDOW_END_HOUR), and only when >=1 problem is outstanding. These tests pin
# (a) outside-window -> no send; (b) inside-window, unsent -> exactly one consolidated
# send covering ALL items; (c) second sweep same day -> no resend; (d) zero problems ->
# no send; (e) --dry-run never sends. Time + mailer are injected so no wall-clock
# dependence and no real email.
# --------------------------------------------------------------------------- #
def _ts_at_hour(hour: int, minute: int = 0) -> float:
    """A POSIX timestamp for today at the given local hour:minute (deterministic)."""
    return dt.datetime.now().replace(
        hour=hour, minute=minute, second=0, microsecond=0).timestamp()


def _sample_problems() -> list[dict]:
    """Two distinct outstanding problems, digest-descriptor shape."""
    return [
        {"label": "Job Alpha", "status": "STALE", "cause": "Alpha supervisor died.",
         "rows": [("Age (cold for)", "2h05m"), ("Owning task", "AlphaTask")]},
        {"label": "Job Bravo", "status": "MISSING", "cause": "Bravo never ran today.",
         "rows": [("Detail", "date=None status=None"), ("Owning task", "BravoTask")]},
    ]


def _capture_send(monkeypatch):
    """Replace hba._send with a recorder; returns the list it appends (subject, html) to."""
    calls: list[tuple[str, str]] = []

    def fake_send(subject, html):
        calls.append((subject, html))
        return True

    monkeypatch.setattr(hba, "_send", fake_send)
    return calls


def test_digest_outside_window_no_send(monkeypatch):
    """(a) Outstanding problems but the local hour is BEFORE the window (overnight) ->
    NO email, and last_sent_date is not recorded. Overnight is silent."""
    calls = _capture_send(monkeypatch)
    state: dict = {}
    now = _ts_at_hour(3)  # 03:00, well before DIGEST_SEND_HOUR (7)
    assert not (hba.DIGEST_SEND_HOUR <= 3 < hba.DIGEST_WINDOW_END_HOUR)
    line = hba.maybe_send_digest(state, now, _sample_problems(), dry_run=False)
    assert calls == [], "overnight must be totally silent"
    assert "OUTSIDE" in line
    assert state.get("_digest", {}).get("last_sent_date") is None


def test_digest_inside_window_sends_once_covering_all_items(monkeypatch):
    """(b) Inside the morning window, not yet sent today, >=1 problem -> EXACTLY ONE
    consolidated send whose body lists ALL outstanding items; last_sent_date recorded."""
    calls = _capture_send(monkeypatch)
    state: dict = {}
    now = _ts_at_hour(hba.DIGEST_SEND_HOUR + 1)  # comfortably inside [7,12)
    problems = _sample_problems()
    line = hba.maybe_send_digest(state, now, problems, dry_run=False)

    assert len(calls) == 1, "must send exactly one consolidated email"
    subject, html = calls[0]
    assert "2 items need attention" in subject
    # Body must cover BOTH items — labels, statuses, and their remediation wording.
    for token in ("Job Alpha", "Job Bravo", "STALE", "MISSING",
                  "Alpha supervisor died.", "Bravo never ran today.",
                  "AlphaTask", "BravoTask"):
        assert token in html, f"digest body missing {token!r}"
    today = dt.datetime.fromtimestamp(now).strftime("%Y%m%d")
    assert state["_digest"]["last_sent_date"] == today
    assert "SENT" in line


def test_digest_second_sweep_same_day_no_resend(monkeypatch):
    """(c) A second sweep the SAME calendar day, still inside the window, must NOT send
    a second email (at most one per day)."""
    calls = _capture_send(monkeypatch)
    now = _ts_at_hour(hba.DIGEST_SEND_HOUR + 2)
    today = dt.datetime.fromtimestamp(now).strftime("%Y%m%d")
    state: dict = {"_digest": {"last_sent_date": today}}  # already sent this morning
    line = hba.maybe_send_digest(state, now, _sample_problems(), dry_run=False)
    assert calls == [], "must not resend after already sending today"
    assert "already sent" in line


def test_digest_inside_window_zero_problems_no_send(monkeypatch):
    """(d) Inside the window, not yet sent, but ZERO outstanding problems -> no email
    (a clean morning stays silent), and last_sent_date is not recorded."""
    calls = _capture_send(monkeypatch)
    state: dict = {}
    now = _ts_at_hour(hba.DIGEST_SEND_HOUR + 1)
    line = hba.maybe_send_digest(state, now, [], dry_run=False)
    assert calls == [], "no problems -> no email"
    assert "0 outstanding" in line
    assert state.get("_digest", {}).get("last_sent_date") is None


def test_digest_dry_run_never_sends(monkeypatch):
    """(e) --dry-run: even inside the window with outstanding problems, NEVER actually
    send and NEVER record last_sent_date (so a real send can still happen later)."""
    calls = _capture_send(monkeypatch)
    state: dict = {}
    now = _ts_at_hour(hba.DIGEST_SEND_HOUR + 1)
    line = hba.maybe_send_digest(state, now, _sample_problems(), dry_run=True)
    assert calls == [], "dry-run must never send"
    assert "WOULD-SEND" in line
    assert state.get("_digest", {}).get("last_sent_date") is None


# --------------------------------------------------------------------------- #
# Handlers now RETURN (line, problem|None) — problem is folded into the digest.
# Assessment/detection is unchanged; these pin the new return contract.
# --------------------------------------------------------------------------- #
def test_handle_job_returns_problem_when_stale(tmp_path):
    """A cold heartbeat -> handle_job returns a non-None problem descriptor carrying the
    job's status + cause, and a line marking it OUTSTANDING (no immediate send)."""
    now = dt.datetime.now().timestamp()
    job = _job(tmp_path)
    hb = job["heartbeat"]
    hb.write_text("2026-08-05 02:00:00  shard alive\n")
    os.utime(hb, (now - 2 * 3600, now - 2 * 3600))
    _write(job["progress"], {"complete": False, "done": 100, "total": 399600},
           age_s=2 * 3600, now=now)
    state: dict = {}
    line, problem = hba.handle_job(job, state, now)
    assert problem is not None
    assert problem["status"] == "STALE"
    assert problem["label"] == job["label"]
    assert "OUTSTANDING" in line


def test_handle_job_returns_none_when_fresh(tmp_path):
    """A fresh, in-progress heartbeat -> no problem (nothing folded into the digest)."""
    now = dt.datetime.now().timestamp()
    job = _job(tmp_path)
    hb = job["heartbeat"]
    hb.write_text("2026-08-05 10:44:00  shard 4 alive\n")
    os.utime(hb, (now - 30, now - 30))
    _write(job["progress"], {"complete": False, "done": 25886, "total": 399600},
           age_s=30, now=now)
    state: dict = {}
    line, problem = hba.handle_job(job, state, now)
    assert problem is None
    assert "no alert" in line
