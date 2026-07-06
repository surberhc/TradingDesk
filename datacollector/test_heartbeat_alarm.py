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
