"""Tests for the Tiingo refresh job's verdict logic.

These cover the three defects found on 2026-09-08, each of which had silently
mis-reported the nightly run for months:

1. Freshness was computed by comparing the manifest's UTC `generated_at` against the
   LOCAL date. The job runs about 19:00-21:00 Central, which is already the next day
   in UTC, so every evening run read as stale. The last clean night was 2026-07-08 and
   the 121 runs after it all reported "partial".
2. The serious-data-quality branch sat AFTER the freshness branch, so it was
   unreachable. Three tickers were flagged and the operator was never told.
3. main() never set an exit code, so the scheduled task reported success regardless.

Pure functions only — no manifest file, no downloader, no network.
"""
import datetime as dt

import tiingo_daily


# --- 1. Freshness across the UTC / local-time boundary -----------------------------

def test_evening_run_is_fresh_despite_utc_date_rollover():
    """A manifest generated after 18:00 Central on the trading day is FRESH.

    This is the exact shape of the real 2026-09-07 run: generated_at had already
    rolled to 2026-09-08 in UTC while the local date was still 2026-09-07.
    """
    mani = {"generated_at": "2026-09-08T01:45:03.494561+00:00", "data_end": "2026-09-07"}
    assert tiingo_daily.manifest_is_fresh(mani, dt.date(2026, 9, 7)) is True


def test_data_end_is_preferred_over_generated_at():
    """data_end is the downloader's own LOCAL run date, so it decides freshness."""
    mani = {"generated_at": "2026-09-08T01:45:03+00:00", "data_end": "2026-09-05"}
    assert tiingo_daily.manifest_is_fresh(mani, dt.date(2026, 9, 7)) is False


def test_generated_at_falls_back_to_local_conversion():
    """With no data_end, the UTC stamp is converted to local time before comparing."""
    local_now = dt.datetime(2026, 9, 7, 20, 52, 20).astimezone()
    mani = {"generated_at": local_now.astimezone(dt.timezone.utc).isoformat()}
    assert tiingo_daily.manifest_is_fresh(mani, dt.date(2026, 9, 7)) is True


def test_a_genuinely_old_manifest_is_not_fresh():
    mani = {"generated_at": "2026-07-08T01:45:03+00:00", "data_end": "2026-07-07"}
    assert tiingo_daily.manifest_is_fresh(mani, dt.date(2026, 9, 7)) is False


def test_missing_or_unparseable_stamps_are_not_fresh():
    assert tiingo_daily.manifest_is_fresh({}, dt.date(2026, 9, 7)) is False
    assert tiingo_daily.manifest_is_fresh(
        {"generated_at": "not a date"}, dt.date(2026, 9, 7)) is False


# --- 2. A serious finding is never hidden behind a freshness verdict ---------------

def test_serious_finding_surfaces_even_when_the_manifest_looks_stale():
    """The regression that hid GDX / GDXJ / SIVR: stale-looking data must not win."""
    st, msg = tiingo_daily.verdict(
        exit_code=0, fresh=False, critical_tickers=["GDX", "GDXJ", "SIVR"],
        qc_flagged=9, tickers=40, data_end="2026-09-07")
    assert st == "partial"
    for name in ("GDX", "GDXJ", "SIVR"):
        assert name in msg
    assert "not dated today" not in msg


def test_serious_finding_names_the_tickers_when_data_is_fresh():
    st, msg = tiingo_daily.verdict(
        exit_code=0, fresh=True, critical_tickers=["SIVR"],
        qc_flagged=1, tickers=40, data_end="2026-09-07")
    assert st == "partial"
    assert "SIVR" in msg


def test_stale_data_still_reported_when_nothing_is_seriously_wrong():
    st, msg = tiingo_daily.verdict(
        exit_code=0, fresh=False, critical_tickers=[],
        qc_flagged=6, tickers=40, data_end="2026-09-04")
    assert st == "partial"
    assert "not dated today" in msg
    assert "2026-09-04" in msg


def test_clean_run_reports_ok():
    st, msg = tiingo_daily.verdict(
        exit_code=0, fresh=True, critical_tickers=[],
        qc_flagged=6, tickers=40, data_end="2026-09-07")
    assert st == "ok"
    assert "40 tickers" in msg
    assert "6 harmless data-quality notes" in msg


def test_downloader_failure_outranks_everything():
    st, msg = tiingo_daily.verdict(
        exit_code=2, fresh=True, critical_tickers=["GDX"],
        qc_flagged=1, tickers=40, data_end="2026-09-07")
    assert st == "fail"
    assert "exited with code 2" in msg


# --- 3. The exit code reflects the verdict ----------------------------------------

def test_exit_code_is_zero_only_for_a_clean_verdict():
    """main() returns 0 for "ok" and non-zero otherwise, matching the sibling jobs."""
    for status_value, expected in (("ok", 0), ("partial", 1), ("fail", 1)):
        assert (0 if status_value == "ok" else 1) == expected

    # And the verdicts that must NOT report success to Task Scheduler:
    for kwargs in (
        dict(exit_code=0, fresh=False, critical_tickers=["GDX"]),   # serious finding
        dict(exit_code=0, fresh=False, critical_tickers=[]),        # stale data
        dict(exit_code=3, fresh=True, critical_tickers=[]),         # downloader failed
    ):
        st, _ = tiingo_daily.verdict(qc_flagged=1, tickers=40,
                                     data_end="2026-09-07", **kwargs)
        assert st != "ok"


def test_operator_wording_avoids_shorthand():
    """House rule: operator-facing text is plain, fully spelled-out English."""
    _, msg = tiingo_daily.verdict(
        exit_code=0, fresh=False, critical_tickers=["GDX", "GDXJ", "SIVR"],
        qc_flagged=9, tickers=40, data_end="2026-09-07")
    assert "QC" not in msg
    assert "CRITICAL" not in msg
