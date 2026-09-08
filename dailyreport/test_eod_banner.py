r"""The evening report must not claim health it did not check.

The end-of-day email is deliberately trimmed to Strategy 0 (2026-07-07), so it builds
exactly TWO sections. Its banner nonetheless computed "all systems fresh" from
``len(sections)`` and its subject ended in a bare "— OK" — a whole-desk all-clear off a
two-job sample. It published exactly that every night of the 2026-09-02/03/04 live-data
Gateway outage: Strategy 0's own inputs really were fine, and something else was not.

These tests pin the honest wording, and pin the one-line visibility of the jobs that write
a status file but have no section here. The report stays trimmed — nothing below re-adds a
section.

Hermetic: no email is sent, no status directory on this machine is read (``STATUS_DIR`` is
redirected at a tmp_path in every test), and no job is run.

Run from dailyreport/:
    "C:\TradingDesk-Local\venv\Scripts\python.exe" -m pytest -q
"""
from __future__ import annotations

import json
import re

import pytest

import eod_report as E
import status


@pytest.fixture(autouse=True)
def _no_status_files_on_this_machine(tmp_path, monkeypatch):
    """Point the status store at an EMPTY temp directory for every test, so the real
    machine's job status files can never make an assertion here pass or fail."""
    monkeypatch.setattr(status, "STATUS_DIR", tmp_path)
    return tmp_path


def _write_status(tmp_path, job, state):
    (tmp_path / f"{job}.json").write_text(json.dumps(
        {"job": job, "date": "20260908", "status": state, "ts": "2026-09-08T18:00:00",
         "metrics": {}, "message": ""}))


def _text(html):
    """The banner as a human reads it — markup stripped."""
    return re.sub(r"<[^>]+>", " ", html).strip()


def _ok_sections():
    return [
        E._sec("s0_regime", "S0 Regime", "ok", "ok",
               [("Confirmed regime (governs the book)", "Bullish"),
                ("Equity band (confirmed regime)", "60-80%")]),
        E._sec("s0_data", "S0 Data", "ok", "ok", []),
    ]


def _subject(overall):
    """The subject main() builds, reproduced from the same pieces."""
    trouble = E._unsectioned_trouble()
    subject = (f"Trading Desk EOD — {E.TODAY.strftime('%b %d')} — "
               f"Strategy 0 inputs {overall.upper()}")
    if trouble:
        subject += (f" — plus {len(trouble)} other job"
                    f"{'s' if len(trouble) != 1 else ''} reporting trouble")
    return subject


# --------------------------------------------------------------------------- #
# 1. THE DEFECT: no whole-system health claim off a Strategy 0 sample.
# --------------------------------------------------------------------------- #
def test_banner_does_not_claim_whole_system_health():
    """The exact regression. Both sections green must never render as an all-clear for
    anything wider than what was actually checked."""
    sections = _ok_sections()
    txt = _text(E._status_banner(sections, E._overall(sections)))
    assert "all systems fresh" not in txt
    assert "systems fresh" not in txt, f"banner still speaks of 'systems': {txt}"


def test_banner_names_the_scope_it_actually_checked():
    sections = _ok_sections()
    txt = _text(E._status_banner(sections, E._overall(sections)))
    assert "Strategy 0's own inputs: all 2 fresh" in txt, txt
    # The regime read it already had is still there — the wording fix changed nothing else.
    assert "Bullish" in txt and "60-80%" in txt


def test_banner_counts_a_stale_section_within_that_scope():
    sections = _ok_sections()
    sections[1] = E._sec("s0_data", "S0 Data", "stale", "stale", [])
    txt = _text(E._status_banner(sections, E._overall(sections)))
    assert "Strategy 0's own inputs: 1 of 2 fresh" in txt, txt


def test_subject_says_what_was_checked_not_a_bare_verdict():
    sections = _ok_sections()
    subject = _subject(E._overall(sections))
    assert "Strategy 0 inputs OK" in subject, subject
    assert not subject.endswith("— OK"), (
        f"the subject is still a bare whole-desk verdict: {subject}")


# --------------------------------------------------------------------------- #
# 2. The jobs with no section here are at least VISIBLE when they report trouble.
# --------------------------------------------------------------------------- #
def test_a_healthy_or_absent_unsectioned_job_adds_nothing():
    """No noise: a job that reported ok, and a job that has never written a status file at
    all, both stay out of the banner and out of the subject."""
    assert E._unsectioned_trouble() == []          # empty directory: nothing written yet
    sections = _ok_sections()
    txt = _text(E._status_banner(sections, E._overall(sections)))
    assert "reported trouble" not in txt
    assert "other job" not in _subject(E._overall(sections))


def test_an_ok_unsectioned_job_is_not_flagged(_no_status_files_on_this_machine):
    _write_status(_no_status_files_on_this_machine, "tiingo", "ok")
    assert E._unsectioned_trouble() == []


def test_a_failed_unsectioned_job_is_named_in_the_banner(_no_status_files_on_this_machine):
    """The silent failure the outage exposed: a job nothing reads, failing every night."""
    _write_status(_no_status_files_on_this_machine, "forward", "fail")
    _write_status(_no_status_files_on_this_machine, "gex", "stale")
    _write_status(_no_status_files_on_this_machine, "tiingo", "ok")

    sections = _ok_sections()
    txt = _text(E._status_banner(sections, E._overall(sections)))
    assert "forward — FAILED" in txt, txt
    assert "gex — out of date" in txt, txt
    assert "tiingo" not in txt, "a healthy job must not be named"
    # And it says plainly that these are NOT covered by the sections below.
    assert "not reported on below" in txt


def test_a_failed_unsectioned_job_is_counted_in_the_subject(
        _no_status_files_on_this_machine):
    _write_status(_no_status_files_on_this_machine, "forward", "fail")
    subject = _subject("ok")
    assert "Strategy 0 inputs OK" in subject
    assert "plus 1 other job reporting trouble" in subject, subject


def test_the_paused_account_monitor_is_never_flagged(_no_status_files_on_this_machine):
    """It is paused ON PURPOSE (gateway quarantine, 2026-07-08). Its section was de-listed
    on 2026-07-28 precisely because a paused job rendered stale every night. Naming it here
    would bring that false alarm straight back."""
    _write_status(_no_status_files_on_this_machine, "account_monitor", "stale")
    assert E._unsectioned_trouble() == []
    assert "account_monitor" not in E.UNSECTIONED_JOBS


# --------------------------------------------------------------------------- #
# 3. The report stays trimmed to Strategy 0.
# --------------------------------------------------------------------------- #
def test_the_report_still_builds_only_the_two_strategy_0_sections():
    """Guard on the deliberate 2026-07-07 trim: this fix is a WORDING fix, and must never
    become a reason to re-add the sections that were removed."""
    src = (E.__file__ and open(E.__file__, encoding="utf-8").read()) or ""
    assert "sections = [s0_regime_sec, s0_data_sec]" in src, (
        "the report's section list changed — it must stay Strategy 0 only")
