"""test_roster.py — the human-blessed EXECUTION ROSTER accessor (roster.py).

TWO DEFECTS THESE PIN.

1. THE NO-TRADE HOLD WAS DEAD ON THE EXECUTION RAIL. ``v_tradingdesk_roster`` carries
   ``no_trade`` and ``crm_roster.fetch_roster`` has always SELECTed it, but its WHERE clause is
   built only from advisor_name and model, and nothing anywhere in the desk read the flag: a
   hold set in the CRM did not stop the desk trading that account. The hold is now applied in
   the allow-list itself, so a held account is missing from BOTH walls.

2. THERE WAS NO WAY TO SCOPE A RUN TO A SUBSET OF ACCOUNTS. The only run available was the
   whole book. A model scope now narrows the roster itself — and, crucially, a scope that
   CANNOT be honoured refuses rather than silently widening back to the whole fallback
   allow-list.

Everything here is offline: no DSN, no socket, NEVER the real CRM DB.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_roster.py -q
"""
from __future__ import annotations

import pytest

import config
import crm_roster
import roster


def _row(account="U1", model="Growth", no_trade=False, account_id=None, **extra):
    """A v_tradingdesk_roster row as fetch_roster returns it (a plain dict)."""
    row = {"account_number": account, "model": model,
           "account_id": account_id or f"id-{account}"}
    if no_trade is not _MISSING:
        row["no_trade"] = no_trade
    row.update(extra)
    return row


class _Missing:
    def __repr__(self):  # pragma: no cover — debugging aid only
        return "<no no_trade key at all>"


_MISSING = _Missing()


# =========================================================================== #
# 1. THE NO-TRADE HOLD — fail closed.                                          #
# =========================================================================== #
def test_explicit_false_clears_the_hold():
    """The live view populates an explicit false on every row, so this is the normal case:
    a tradeable account."""
    assert roster.has_no_trade_hold({"no_trade": False}) is False


def test_true_is_a_hold():
    assert roster.has_no_trade_hold({"no_trade": True}) is True


def test_a_missing_key_is_a_hold():
    """FAIL CLOSED. 'The record does not say this account is safe to trade' is not the same
    thing as 'it is'. If the column ever stops being populated, refusing is the right answer."""
    assert roster.has_no_trade_hold({}) is True
    assert roster.has_no_trade_hold({"model": "Growth"}) is True


def test_null_is_a_hold():
    assert roster.has_no_trade_hold({"no_trade": None}) is True


def test_a_row_that_is_not_a_mapping_is_a_hold():
    assert roster.has_no_trade_hold(object()) is True


def test_truthy_and_falsy_values_follow_the_flag():
    assert roster.has_no_trade_hold({"no_trade": 1}) is True
    assert roster.has_no_trade_hold({"no_trade": 0}) is False


# =========================================================================== #
# 2. MODEL SCOPE — an empty selection is the WHOLE BOOK, never nothing.        #
# =========================================================================== #
def test_no_scope_matches_every_row():
    assert roster.matches_models(_row(model="Growth"), None) is True
    assert roster.matches_models(_row(model="Growth"), []) is True
    assert roster.matches_models(_row(model="Growth"), ["", "  "]) is True


def test_exact_label_matches():
    assert roster.matches_models(_row(model="Growth (Custom)"),
                                 ["Growth (Custom)"]) is True


def test_a_label_outside_the_scope_does_not_match():
    assert roster.matches_models(_row(model="Growth"), ["Growth (Custom)"]) is False


def test_scope_matching_tolerates_surrounding_whitespace():
    assert roster.matches_models(_row(model=" Growth (Custom) "),
                                 [" Growth (Custom)"]) is True


def test_a_row_with_no_model_matches_nothing_scoped():
    assert roster.matches_models(_row(model=None), ["Growth"]) is False
    assert roster.matches_models(_row(model=None), None) is True


# =========================================================================== #
# 3. partition_roster_rows — split by hold, drop everything out of scope.      #
# =========================================================================== #
def test_partition_splits_held_from_tradeable():
    rows = [_row("U1", "Growth"), _row("U2", "Growth", no_trade=True)]
    tradeable, held = roster.partition_roster_rows(rows)
    assert [r["account_number"] for r in tradeable] == ["U1"]
    assert [r["account_number"] for r in held] == ["U2"]


def test_partition_drops_rows_outside_the_scope_entirely():
    """Out of scope is not 'held' — it was never asked for, so it appears in neither list."""
    rows = [_row("U1", "Growth (Custom)"), _row("U2", "Growth"),
            _row("U3", "Growth", no_trade=True)]
    tradeable, held = roster.partition_roster_rows(rows, ["Growth (Custom)"])
    assert [r["account_number"] for r in tradeable] == ["U1"]
    assert held == []


def test_partition_holds_a_row_missing_the_flag():
    rows = [_row("U1", "Growth", no_trade=_MISSING)]
    tradeable, held = roster.partition_roster_rows(rows)
    assert tradeable == []
    assert [r["account_number"] for r in held] == ["U1"]


# =========================================================================== #
# 4. crm_enrolled_roster_scan — a held account never reaches the allow-list.   #
# =========================================================================== #
class _FakeConn:
    closed = False

    def close(self):
        self.closed = True


def _fake_crm(monkeypatch, rows, funded=None):
    """Wire the CRM seam to synthetic rows. NEVER touches a database."""
    conn = _FakeConn()
    monkeypatch.setattr(crm_roster, "_connect", lambda: conn)
    monkeypatch.setattr(crm_roster, "fetch_roster",
                        lambda advisor_name=None, model=None, conn=None: list(rows))
    asked: dict = {}

    def _funded(ids, conn=None):
        asked["ids"] = list(ids)
        return set(ids) if funded is None else set(funded)

    monkeypatch.setattr(crm_roster, "funded_account_ids", _funded)
    return conn, asked


def test_a_held_account_never_appears_in_accounts(monkeypatch):
    """THE DEFECT, pinned. The hold used to be a record with no effect."""
    _fake_crm(monkeypatch, [_row("U1", "Growth"), _row("U2", "Growth", no_trade=True)])
    scan = roster.crm_enrolled_roster_scan()
    assert scan["accounts"] == ["U1"]
    assert scan["held"] == ["U2"]
    assert "U2" not in roster.crm_enrolled_roster()


def test_a_row_missing_the_flag_is_held_not_traded(monkeypatch):
    _fake_crm(monkeypatch, [_row("U1", "Growth", no_trade=_MISSING)])
    scan = roster.crm_enrolled_roster_scan()
    assert scan["accounts"] == [] and scan["held"] == ["U1"]


def test_the_scan_reports_the_unfunded_accounts_separately(monkeypatch):
    _fake_crm(monkeypatch, [_row("U1", "Growth"), _row("U2", "Growth")],
              funded={"id-U1"})
    scan = roster.crm_enrolled_roster_scan()
    assert scan["accounts"] == ["U1"] and scan["unfunded"] == ["U2"]


def test_funded_reality_is_never_asked_about_a_held_account(monkeypatch):
    """A held account's funded state is irrelevant, and asking would widen the query."""
    _conn, asked = _fake_crm(
        monkeypatch, [_row("U1", "Growth"), _row("U2", "Growth", no_trade=True)])
    roster.crm_enrolled_roster_scan()
    assert asked["ids"] == ["id-U1"]


def test_the_scan_offers_every_model_in_the_book_before_scoping(monkeypatch):
    """A UI must be able to offer the REAL choices instead of a hardcoded list — including
    the models of accounts the scope then excludes."""
    _fake_crm(monkeypatch, [_row("U1", "Growth"), _row("U2", "Growth (Custom)"),
                            _row("U3", "Balanced")])
    scan = roster.crm_enrolled_roster_scan(models=["Growth (Custom)"])
    assert scan["models"] == ["Balanced", "Growth", "Growth (Custom)"]
    assert scan["accounts"] == ["U2"]
    assert scan["scope"] == ["Growth (Custom)"]


def test_the_scan_closes_its_connection(monkeypatch):
    conn, _asked = _fake_crm(monkeypatch, [_row("U1", "Growth")])
    roster.crm_enrolled_roster_scan()
    assert conn.closed is True


# =========================================================================== #
# 5. enrolled_roster_scan — the fallback, and its ONE exception.               #
# =========================================================================== #
def test_unscoped_run_still_degrades_to_config_when_the_crm_is_unconfigured(monkeypatch):
    """The pre-existing degraded behaviour is untouched: the account wall always has a
    deterministic allow-list."""
    monkeypatch.setattr(crm_roster, "is_configured", lambda: False)
    scan = roster.enrolled_roster_scan()
    assert scan["accounts"] == sorted(set(config.ENROLLMENT))
    assert scan["source"] == "config"
    assert roster.enrolled_roster() == sorted(set(config.ENROLLMENT))


def test_a_scoped_run_refuses_when_the_crm_is_unconfigured(monkeypatch):
    """REFUSE, NEVER WIDEN. config.ENROLLMENT carries no model labels, so it cannot answer
    'only these models' — and silently returning the whole fallback allow-list would trade
    the accounts the operator explicitly excluded."""
    monkeypatch.setattr(crm_roster, "is_configured", lambda: False)
    with pytest.raises(roster.RosterScopeUnavailable):
        roster.enrolled_roster_scan(models=["Growth (Custom)"])
    with pytest.raises(roster.RosterScopeUnavailable):
        roster.enrolled_roster(models=["Growth (Custom)"])


def test_a_scoped_run_refuses_when_the_crm_is_unreachable(monkeypatch):
    monkeypatch.setattr(crm_roster, "is_configured", lambda: True)

    def _boom(**kwargs):
        raise crm_roster.CrmRosterUnavailable("simulated outage")

    monkeypatch.setattr(roster, "crm_enrolled_roster_scan", _boom)
    with pytest.raises(roster.RosterScopeUnavailable):
        roster.enrolled_roster_scan(models=["Growth (Custom)"])


def test_an_unscoped_run_does_not_raise_when_the_crm_is_unreachable(monkeypatch):
    """Same outage, no scope asked for -> the degraded fallback, exactly as before."""
    monkeypatch.setattr(crm_roster, "is_configured", lambda: True)

    def _boom(**kwargs):
        raise crm_roster.CrmRosterUnavailable("simulated outage")

    monkeypatch.setattr(roster, "crm_enrolled_roster_scan", _boom)
    assert roster.enrolled_roster_scan()["accounts"] == sorted(set(config.ENROLLMENT))
    assert roster.enrolled_roster_scan()["source"] == "config"


def test_the_refusal_names_the_scope_it_could_not_honour(monkeypatch):
    monkeypatch.setattr(crm_roster, "is_configured", lambda: False)
    with pytest.raises(roster.RosterScopeUnavailable) as exc:
        roster.enrolled_roster(models=["Growth (Custom)", "Balanced (Custom)"])
    assert "Growth (Custom)" in str(exc.value) and "Balanced (Custom)" in str(exc.value)


def test_a_scoped_read_that_selects_nobody_is_authoritative(monkeypatch):
    """'No account is on that model' is a REAL answer. It must not fall through to the config
    list — that would run accounts the operator never selected."""
    monkeypatch.setattr(crm_roster, "is_configured", lambda: True)
    monkeypatch.setattr(
        roster, "crm_enrolled_roster_scan",
        lambda **k: {"accounts": [], "held": [], "unfunded": [], "models": [],
                     "scope": ["Growth (Custom)"]})
    scan = roster.enrolled_roster_scan(models=["Growth (Custom)"])
    assert scan["accounts"] == [] and scan["source"] == "crm"


def test_an_unscoped_crm_read_that_returns_only_held_accounts_is_authoritative(monkeypatch):
    """A book whose every account is HELD must not fall back to config and start trading the
    fallback accounts — the holds are the answer."""
    monkeypatch.setattr(crm_roster, "is_configured", lambda: True)
    monkeypatch.setattr(
        roster, "crm_enrolled_roster_scan",
        lambda **k: {"accounts": [], "held": ["U1"], "unfunded": [], "models": [],
                     "scope": []})
    scan = roster.enrolled_roster_scan()
    assert scan["accounts"] == [] and scan["held"] == ["U1"] and scan["source"] == "crm"


def test_enrolled_roster_threads_the_scope_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(crm_roster, "is_configured", lambda: True)

    def _scan(models=None, **k):
        seen["models"] = models
        return {"accounts": ["U9"], "held": [], "unfunded": [], "models": [],
                "scope": list(models or ())}

    monkeypatch.setattr(roster, "crm_enrolled_roster_scan", _scan)
    assert roster.enrolled_roster(models=["Growth (Custom)"]) == ["U9"]
    assert seen["models"] == ["Growth (Custom)"]


def test_the_roster_is_sorted_and_deduped(monkeypatch):
    _fake_crm(monkeypatch, [_row("U2", "Growth"), _row("U1", "Growth"),
                            _row("U1", "Growth", account_id="id-U1")])
    accounts = roster.crm_enrolled_roster()
    assert accounts == ["U1", "U2"]
