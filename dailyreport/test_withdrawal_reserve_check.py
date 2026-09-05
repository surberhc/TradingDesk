"""Tests for the withdrawal-reserve shortfall check's DECISION logic (pure; no broker)."""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _p in (str(_HERE), str(_REPO / "connections"), str(_REPO / "paperbot"),
           str(_REPO / "dashboard" / "desk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cashflows  # noqa: E402
import withdrawal_reserve_check as job  # noqa: E402


# --------------------------------------------------------------------------- #
# accounts_to_check()
# --------------------------------------------------------------------------- #
def test_accounts_to_check_includes_only_distribution_accounts(monkeypatch):
    fake_schedule = {
        "U1_DIST": [cashflows.Flow("distribution", amount=100.0, pct_nav=0.0, day=1)],
        "U2_CONTRIB_ONLY": [cashflows.Flow("contribution", amount=500.0, pct_nav=0.0, day=1)],
        "U3_BOTH": [cashflows.Flow("contribution", amount=500.0, pct_nav=0.0, day=1),
                    cashflows.Flow("distribution", amount=50.0, pct_nav=0.0, day=15)],
    }
    monkeypatch.setattr(cashflows, "SCHEDULE", fake_schedule)
    assert job.accounts_to_check() == ["U1_DIST", "U3_BOTH"]


def test_accounts_to_check_empty_schedule(monkeypatch):
    monkeypatch.setattr(cashflows, "SCHEDULE", {})
    assert job.accounts_to_check() == []


def test_accounts_to_check_sorted_and_stable(monkeypatch):
    fake_schedule = {
        "U9": [cashflows.Flow("distribution", amount=1.0, pct_nav=0.0, day=1)],
        "U1": [cashflows.Flow("distribution", amount=1.0, pct_nav=0.0, day=1)],
        "U5": [cashflows.Flow("distribution", amount=1.0, pct_nav=0.0, day=1)],
    }
    monkeypatch.setattr(cashflows, "SCHEDULE", fake_schedule)
    assert job.accounts_to_check() == ["U1", "U5", "U9"]


# --------------------------------------------------------------------------- #
# decide()
# --------------------------------------------------------------------------- #
def test_cash_comfortably_covers_reserve_no_alert(monkeypatch):
    fake_schedule = {
        "UOK": [cashflows.Flow("distribution", amount=1000.0, pct_nav=0.0, day=15)],
    }
    monkeypatch.setattr(cashflows, "SCHEDULE", fake_schedule)
    # reserve = 2 months * 1000 = 2000
    d = job.decide("UOK", net_liq=100_000, total_cash=5_000)
    assert d["ok"] is True
    assert d["reserve"] == 2_000.0
    assert d["shortfall"] == 2_000.0 - 5_000.0
    assert d["should_alert"] is False


def test_cash_short_alerts_with_correct_shortfall(monkeypatch):
    fake_schedule = {
        "USHORT": [cashflows.Flow("distribution", amount=8500.0, pct_nav=0.0, day=15)],
    }
    monkeypatch.setattr(cashflows, "SCHEDULE", fake_schedule)
    # reserve = 2 * 8500 = 17000
    d = job.decide("USHORT", net_liq=500_000, total_cash=10_000)
    assert d["ok"] is True
    assert d["reserve"] == 17_000.0
    assert round(d["shortfall"], 2) == round(17_000.0 - 10_000.0, 2)
    assert d["should_alert"] is True


def test_shortfall_exactly_zero_does_not_alert(monkeypatch):
    fake_schedule = {
        "UEXACT": [cashflows.Flow("distribution", amount=1000.0, pct_nav=0.0, day=15)],
    }
    monkeypatch.setattr(cashflows, "SCHEDULE", fake_schedule)
    # reserve = 2000, cash exactly 2000 -> shortfall == 0, strictly-greater trigger stays False
    d = job.decide("UEXACT", net_liq=100_000, total_cash=2_000)
    assert d["ok"] is True
    assert d["shortfall"] == 0.0
    assert d["should_alert"] is False


def test_net_liq_missing_or_zero_is_inconclusive():
    for nl in (0, None, -5):
        d = job.decide("UANY", net_liq=nl, total_cash=10_000)
        assert d["ok"] is False
        assert d["should_alert"] is False
        assert d["reserve"] is None


def test_total_cash_missing_is_inconclusive():
    d = job.decide("UANY", net_liq=100_000, total_cash=None)
    assert d["ok"] is False
    assert d["should_alert"] is False


def test_account_with_no_schedule_entry_never_alerts(monkeypatch):
    """Important edge case: an account with NO entry in cashflows.SCHEDULE at all has
    reserve = 0.0 (cashflows.reserve_for defaults to no flows), so it can never be
    'short' of a zero reserve no matter how little cash it holds."""
    monkeypatch.setattr(cashflows, "SCHEDULE", {})
    d = job.decide("UNOTSCHEDULED", net_liq=100_000, total_cash=0.0)
    assert d["ok"] is True
    assert d["reserve"] == 0.0
    assert d["shortfall"] == 0.0
    assert d["should_alert"] is False


def test_build_notice_is_plain_english(monkeypatch):
    fake_schedule = {
        "U10555316": [cashflows.Flow("distribution", amount=8500.0, pct_nav=0.0, day=15)],
    }
    monkeypatch.setattr(cashflows, "SCHEDULE", fake_schedule)
    d = job.decide("U10555316", net_liq=500_000, total_cash=10_000)
    title, body, hint = job.build_notice(d)
    assert "Withdrawal reserve short" in title
    assert "U10555316" in title
    assert "U10555316" in body
    assert "no-trade" in hint or "no_trade" in hint.lower()


def test_main_dry_run_reads_and_prints_but_posts_nothing(monkeypatch, capsys):
    fake_schedule = {
        "UA": [cashflows.Flow("distribution", amount=1000.0, pct_nav=0.0, day=15)],
        "UB": [cashflows.Flow("contribution", amount=500.0, pct_nav=0.0, day=1)],
    }
    monkeypatch.setattr(cashflows, "SCHEDULE", fake_schedule)
    monkeypatch.setattr(job, "read_cash",
                        lambda account: {"net_liq": 100_000, "total_cash": 500})
    rc = job.main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run]" in out
    assert "UA" in out
    # UB has no distribution flow so it's not in accounts_to_check() at all
    assert "UB" not in out


def test_main_one_account_failure_does_not_block_the_rest(monkeypatch, capsys):
    fake_schedule = {
        "UGOOD": [cashflows.Flow("distribution", amount=100.0, pct_nav=0.0, day=15)],
        "UBAD": [cashflows.Flow("distribution", amount=100.0, pct_nav=0.0, day=15)],
    }
    monkeypatch.setattr(cashflows, "SCHEDULE", fake_schedule)

    def fake_read_cash(account):
        if account == "UBAD":
            raise RuntimeError("account UBAD not found under the live-trading login")
        return {"net_liq": 100_000, "total_cash": 5_000}

    monkeypatch.setattr(job, "read_cash", fake_read_cash)
    rc = job.main(["--dry-run"])
    out = capsys.readouterr().out
    # UBAD's failure is logged to stderr (not captured here) but must not raise/abort;
    # UGOOD must still be evaluated and reported.
    assert "UGOOD" in out
    assert rc == 1  # non-zero because one account failed, but it still ran to completion


def test_main_snooze_skips_repost(tmp_path, monkeypatch, capsys):
    """While the operator has an account's withdrawal-reserve notice snoozed, the run must
    SKIP posting for that account — dismiss alone re-posts a fresh notice next run."""
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "ac.db"))
    fake_schedule = {
        "UZ": [cashflows.Flow("distribution", amount=8500.0, pct_nav=0.0, day=15)],
    }
    monkeypatch.setattr(cashflows, "SCHEDULE", fake_schedule)
    monkeypatch.setattr(job, "read_cash",
                        lambda account: {"net_liq": 500_000, "total_cash": 1_000})
    import importlib
    import action_center
    importlib.reload(action_center)

    dedup_key = "withdrawal_reserve_UZ"

    # first run posts the notice
    assert job.main([]) == 0
    assert action_center.has_open(dedup_key)

    # operator ignores it for 5 days
    assert action_center.snooze(dedup_key, 5)

    # next run must skip posting; the hidden notice is left untouched
    assert job.main([]) == 0
    assert "snoozed" in capsys.readouterr().out.lower()
    assert action_center.read_notices() == []
    assert action_center.is_snoozed(dedup_key)
