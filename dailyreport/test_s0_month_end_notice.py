"""
test_s0_month_end_notice.py — offline unit tests for the S0 month-end EXACT verdict (Job B).

NO broker, NO backtester, NO network, NO email. The heavy target/plan computation
(strategy_target + rebalance_engine) is LAZY inside compute_plan, so these tests exercise the
pure verdict logic with a mocked snapshot + a stubbed plan and never import the backtester.

Proves:
  * verdict_case: legs>0 -> 'trade'; legs==0 -> 'no_trade'; missing/failed snapshot -> 'no_read'.
  * build_verdict renders the correct subject for each case (incl. exact leg count + pluralization).
  * a FAILED snapshot marker (ok=false) and a missing snapshot both fail-honest to 'no_read'.
  * load_snapshot ignores a stale (wrong-date) snapshot and returns None.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s0_month_end_notice.py -q
"""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import s0_month_end_notice as job_b

AS_OF = dt.date(2026, 7, 31)          # a Friday month-end (July 31, 2026)
NEXT = dt.date(2026, 8, 3)            # next session (Monday)


def _snap(ok=True, positions=None, error=None):
    rec = {"as_of": AS_OF.isoformat(), "account": "U14438624", "ok": ok}
    if ok:
        rec["net_liq"] = 250000.0
        rec["positions"] = positions or {"SPY": 100}
    else:
        rec["error"] = error or "gateway unreachable"
    return rec


def _plan(orders):
    """Minimal AccountPlan stand-in — build_verdict/verdict_case read only `.orders`."""
    return SimpleNamespace(orders=dict(orders))


# --- verdict_case -------------------------------------------------------------------
def test_case_trade_when_legs_present():
    assert job_b.verdict_case(_snap(), _plan({"SPY": 85, "VTI": 193})) == "trade"


def test_case_no_trade_when_zero_legs():
    assert job_b.verdict_case(_snap(), _plan({})) == "no_trade"


def test_case_no_read_when_snapshot_missing():
    assert job_b.verdict_case(None, None) == "no_read"


def test_case_no_read_when_snapshot_failed():
    assert job_b.verdict_case(_snap(ok=False), None) == "no_read"


def test_case_no_read_when_plan_is_none():
    # snapshot ok but plan could not be computed -> fail-honest, never a guessed verdict.
    assert job_b.verdict_case(_snap(), None) == "no_read"


# --- build_verdict subjects ---------------------------------------------------------
def test_subject_trade_counts_legs_and_pluralizes():
    subj, text, html = job_b.build_verdict(
        AS_OF, NEXT, False, _snap(), _plan({"SPY": 85, "VTI": 193, "RSP": 323}),
        prices={"SPY": 729.46, "VTI": 360.42, "RSP": 215.73})
    assert subj == "S0: TRADE tomorrow — 3 legs at next open"
    assert "TRADE tomorrow" in text
    assert "SPY" in text and "VTI" in text and "RSP" in text


def test_subject_trade_singular_one_leg():
    subj, _, _ = job_b.build_verdict(AS_OF, NEXT, False, _snap(),
                                     _plan({"SPY": 10}), prices={"SPY": 729.46})
    assert subj == "S0: TRADE tomorrow — 1 leg at next open"


def test_subject_no_trade():
    subj, text, _ = job_b.build_verdict(AS_OF, NEXT, False, _snap(), _plan({}), prices={})
    assert subj == job_b.SUBJECT_NO_TRADE == "S0: NO trade tomorrow — account already conforms"
    assert "already conforms" in text


def test_subject_no_read_missing_snapshot():
    subj, text, _ = job_b.build_verdict(AS_OF, NEXT, False, None, None, prices=None,
                                        error="no snapshot file for today")
    assert subj == job_b.SUBJECT_NO_READ == "S0: month-end — could not read holdings at close"
    assert "could not" in text.lower() and "will not guess" in text.lower()


def test_subject_no_read_failed_marker():
    subj, _, _ = job_b.build_verdict(AS_OF, NEXT, False, _snap(ok=False), None, prices=None,
                                     error="snapshot marked failed: gateway unreachable")
    assert subj == job_b.SUBJECT_NO_READ


# --- load_snapshot ------------------------------------------------------------------
def test_load_snapshot_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGDESK_S0_MONTHEND_SNAPSHOT", str(tmp_path / "nope.json"))
    assert job_b.load_snapshot(AS_OF) is None


def test_load_snapshot_stale_date_ignored(tmp_path, monkeypatch):
    p = tmp_path / "snap.json"
    stale = _snap()
    stale["as_of"] = "2026-06-30"          # a DIFFERENT month-end
    p.write_text(json.dumps(stale), encoding="utf-8")
    monkeypatch.setenv("TRADINGDESK_S0_MONTHEND_SNAPSHOT", str(p))
    assert job_b.load_snapshot(AS_OF) is None


def test_load_snapshot_today_ok(tmp_path, monkeypatch):
    p = tmp_path / "snap.json"
    p.write_text(json.dumps(_snap()), encoding="utf-8")
    monkeypatch.setenv("TRADINGDESK_S0_MONTHEND_SNAPSHOT", str(p))
    rec = job_b.load_snapshot(AS_OF)
    assert rec is not None and rec["ok"] and rec["positions"] == {"SPY": 100}


# --- signal-day calendar (sanity) ---------------------------------------------------
def test_july_31_2026_is_signal_day():
    assert job_b.is_month_end_signal_day(AS_OF) is True


def test_july_30_2026_is_not_signal_day():
    assert job_b.is_month_end_signal_day(dt.date(2026, 7, 30)) is False
