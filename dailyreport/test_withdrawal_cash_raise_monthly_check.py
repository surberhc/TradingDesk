"""Tests for the monthly consolidated withdrawal-cash-raise notice (pure logic + posting
shape; no broker, no live CRM)."""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _p in (str(_HERE), str(_REPO / "connections"), str(_REPO / "paperbot"),
           str(_REPO / "dashboard" / "desk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import withdrawal_cash_raise_monthly_check as job  # noqa: E402
import withdrawal_cash_raise as wcr  # noqa: E402


def _row(account, shortfall, total_cash=1_000.0, reserve=None, net_liq=100_000.0):
    if reserve is None:
        reserve = total_cash + shortfall
    return {"account": account, "net_liq": net_liq, "total_cash": total_cash,
            "reserve": reserve, "shortfall": shortfall}


def _patch_report(monkeypatch, rows, unreadable=()):
    """Stand in for the LIVE read, which reports BOTH the accounts found short AND the
    accounts that could not be read at all."""
    monkeypatch.setattr(wcr, "accounts_needing_cash_and_unreadable",
                        lambda: (list(rows), list(unreadable)))


# --------------------------------------------------------------------------- #
# build_notice() — pure
# --------------------------------------------------------------------------- #
def test_build_notice_names_every_account_and_totals_shortfall(monkeypatch):
    monkeypatch.setattr(job, "_household_names", lambda accounts: {})
    rows = [_row("UA", 1_000.0), _row("UB", 2_500.0)]
    title, body, hint = job.build_notice(rows)
    assert "2" in title
    assert "UA" in body and "UB" in body
    assert "3,500" in body  # combined shortfall
    assert "Raise withdrawal cash" in hint


def test_build_notice_includes_household_name_when_available(monkeypatch):
    monkeypatch.setattr(job, "_household_names", lambda accounts: {"UA": "Smith Household"})
    rows = [_row("UA", 1_000.0)]
    _, body, _ = job.build_notice(rows)
    assert "Smith Household" in body


def test_build_notice_singular_wording_for_one_account(monkeypatch):
    monkeypatch.setattr(job, "_household_names", lambda accounts: {})
    title, body, _ = job.build_notice([_row("UA", 1_000.0)])
    assert "account needs" in title
    assert "accounts need" not in title


# --------------------------------------------------------------------------- #
# main() — empty list posts nothing
# --------------------------------------------------------------------------- #
def test_main_empty_list_posts_nothing(monkeypatch, capsys):
    _patch_report(monkeypatch, [])
    calls = []
    import action_center
    monkeypatch.setattr(action_center, "post_notice",
                        lambda *a, **k: calls.append((a, k)) or "should-not-be-called")
    rc = job.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No accounts need" in out
    assert calls == []


# --------------------------------------------------------------------------- #
# main() — non-empty list posts exactly ONE consolidated notice
# --------------------------------------------------------------------------- #
def test_main_nonempty_posts_exactly_one_notice_with_all_accounts(monkeypatch, crm):
    rows = [_row("UA", 1_000.0), _row("UB", 2_500.0), _row("UC", 300.0)]
    _patch_report(monkeypatch, rows)
    monkeypatch.setattr(job, "_household_names", lambda accounts: {})

    calls = []
    import action_center
    real_post_notice = action_center.post_notice

    def _spy(*a, **k):
        calls.append(k)
        return real_post_notice(*a, **k)
    monkeypatch.setattr(action_center, "post_notice", _spy)

    rc = job.main([])
    assert rc == 0
    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["dedup_key"] == "withdrawal_cash_raise_monthly"
    assert kwargs["severity"] == "warn"
    for acct in ("UA", "UB", "UC"):
        assert acct in kwargs["body"]

    # posted for real, exactly once
    assert action_center.has_open("withdrawal_cash_raise_monthly")


def test_main_rerun_leaves_the_one_open_alert_alone(monkeypatch, crm):
    """A re-run must never stack a duplicate. The desk has no UPDATE permission on the CRM, so
    the second run leaves the ORIGINAL alert standing rather than refreshing it — the newly
    added account shows up only after Andrew closes the open task."""
    monkeypatch.setattr(job, "_household_names", lambda accounts: {})

    _patch_report(monkeypatch, [_row("UA", 1_000.0)])
    assert job.main([]) == 0

    _patch_report(monkeypatch, [_row("UA", 1_000.0), _row("UB", 500.0)])
    assert job.main([]) == 0

    alerts = [r for r in crm if r["dedup_key"] == "withdrawal_cash_raise_monthly"]
    assert len(alerts) == 1
    assert "UA" in alerts[0]["description"]
    assert "UB" not in alerts[0]["description"]


# --------------------------------------------------------------------------- #
# main() — an already-open alert posts nothing
# --------------------------------------------------------------------------- #
def test_main_posts_nothing_while_an_alert_is_already_open(monkeypatch, crm, capsys):
    monkeypatch.setattr(job, "_household_names", lambda accounts: {})
    _patch_report(monkeypatch, [_row("UA", 1_000.0)])

    import action_center
    assert job.main([]) == 0  # first run posts + creates the open alert
    assert action_center.has_open("withdrawal_cash_raise_monthly")

    calls = []
    real_post_notice = action_center.post_notice
    monkeypatch.setattr(action_center, "post_notice",
                        lambda *a, **k: calls.append(k) or real_post_notice(*a, **k))

    rc = job.main([])
    out = capsys.readouterr().out.lower()
    assert rc == 0
    # The job no longer decides this for itself: it ASKS post_notice, which declines to file a
    # second alert and says so. The wording must never blame a snooze nobody set.
    assert "already open" in out
    assert "snoozed" not in out
    assert len(calls) == 1
    assert len(crm) == 1          # nothing new was filed


# --------------------------------------------------------------------------- #
# --dry-run — prints, posts nothing
# --------------------------------------------------------------------------- #
def test_dry_run_prints_and_posts_nothing(monkeypatch, capsys):
    monkeypatch.setattr(job, "_household_names", lambda accounts: {})
    _patch_report(monkeypatch, [_row("UA", 1_000.0)])

    calls = []
    import action_center
    monkeypatch.setattr(action_center, "post_notice",
                        lambda *a, **k: calls.append(k) or "x")

    rc = job.main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run]" in out
    assert "UA" in out
    assert calls == []


def test_main_reports_failure_when_the_alert_cannot_be_filed(crm_write_broken, monkeypatch,
                                                             capsys):
    """An outage on the reporting channel is a FAILED run. Non-zero exit, and never a claim
    that the operator snoozed a notice nobody snoozed."""
    monkeypatch.setattr(job, "_household_names", lambda accounts: {})
    _patch_report(monkeypatch, [_row("UA", 1_000.0)])

    assert job.main([]) == 1
    assert crm_write_broken == []
    assert "snoozed" not in capsys.readouterr().out.lower()


# --------------------------------------------------------------------------- #
# main() — accounts that could NOT be read are a failed run, never a clean "nobody is short".
# With the Gateway down every account is unreadable and the short list is empty, which must
# not be reported the same way as an all-clear.
# --------------------------------------------------------------------------- #
def test_every_account_unreadable_fails_and_never_claims_no_accounts_need_cash(
        monkeypatch, capsys):
    _patch_report(monkeypatch, [], unreadable=["UA", "UB", "UC"])
    calls = []
    import action_center
    monkeypatch.setattr(action_center, "post_notice",
                        lambda *a, **k: calls.append(k) or "should-not-be-called")

    rc = job.main([])
    out = capsys.readouterr().out

    assert rc == 1
    assert "No accounts need withdrawal cash raised this cycle." not in out
    assert "Could not read 3 withdrawal accounts" in out
    for acct in ("UA", "UB", "UC"):
        assert acct in out
    assert calls == []


def test_readable_and_fine_mixed_with_unreadable_still_reports_failure(monkeypatch, capsys):
    """Some accounts read fine and were genuinely not short, one could not be read at all.
    The run is still incomplete, so it must exit non-zero and say so."""
    _patch_report(monkeypatch, [], unreadable=["UB"])
    rc = job.main([])
    out = capsys.readouterr().out

    assert rc == 1
    assert "No accounts need withdrawal cash raised this cycle." not in out
    assert "Could not read 1 withdrawal account:" in out
    assert "UB" in out


def test_short_account_posts_its_notice_but_still_fails_when_one_was_unreadable(
        monkeypatch, crm, capsys):
    """A real shortfall is still reported, but an unexamined account keeps the exit non-zero."""
    monkeypatch.setattr(job, "_household_names", lambda accounts: {})
    _patch_report(monkeypatch, [_row("UA", 1_000.0)], unreadable=["UB"])

    rc = job.main([])
    out = capsys.readouterr().out

    assert rc == 1
    assert "UB" in out
    import action_center
    assert action_center.has_open("withdrawal_cash_raise_monthly")


def test_dry_run_also_fails_when_an_account_was_unreadable(monkeypatch, capsys):
    monkeypatch.setattr(job, "_household_names", lambda accounts: {})
    _patch_report(monkeypatch, [_row("UA", 1_000.0)], unreadable=["UB"])
    assert job.main(["--dry-run"]) == 1
    assert "[dry-run]" in capsys.readouterr().out


def test_all_accounts_readable_and_none_short_exits_zero_cleanly(monkeypatch, capsys):
    """The good case is preserved: an account that is genuinely not short is NOT an error."""
    _patch_report(monkeypatch, [], unreadable=[])
    rc = job.main([])
    out = capsys.readouterr().out

    assert rc == 0
    assert "No accounts need withdrawal cash raised this cycle." in out
    assert "Could not read" not in out
