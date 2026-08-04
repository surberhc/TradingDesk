"""test_crm_roster.py — the desk's read-only CRM roster reader (no live DB).

Covers the CRM->desk account-identity map, the env-var gate, the fail-closed behavior when
the connection string is absent, and roster.enrolled_roster's graceful fallback to config.
Nothing here opens a socket or hits the CRM.
"""
import pytest

import crm_roster
import roster
import config


def test_account_identifier_is_the_ib_number():
    assert crm_roster.account_identifier({"account_number": "U20984696"}) == "U20984696"


def test_is_configured_reflects_env(monkeypatch):
    monkeypatch.delenv(crm_roster.DSN_ENV, raising=False)
    assert crm_roster.is_configured() is False
    monkeypatch.setenv(crm_roster.DSN_ENV, "postgresql://x")
    assert crm_roster.is_configured() is True
    monkeypatch.setenv(crm_roster.DSN_ENV, "   ")
    assert crm_roster.is_configured() is False


def test_dsn_missing_raises_unavailable(monkeypatch):
    monkeypatch.delenv(crm_roster.DSN_ENV, raising=False)
    with pytest.raises(crm_roster.CrmRosterUnavailable):
        crm_roster._dsn()


def test_fetch_roster_without_dsn_raises(monkeypatch):
    monkeypatch.delenv(crm_roster.DSN_ENV, raising=False)
    with pytest.raises(crm_roster.CrmRosterUnavailable):
        crm_roster.fetch_roster()


def test_enrolled_roster_falls_back_to_config_when_crm_unset(monkeypatch):
    """No DSN wired -> enrolled_roster returns the local config allow-list unchanged."""
    monkeypatch.delenv(crm_roster.DSN_ENV, raising=False)
    assert roster.enrolled_roster() == sorted(set(config.ENROLLMENT))


def test_enrolled_roster_falls_back_when_crm_unavailable(monkeypatch):
    """DSN present but the CRM read fails -> still degrades to config, never raises."""
    monkeypatch.setenv(crm_roster.DSN_ENV, "postgresql://unreachable")

    def boom(*a, **k):
        raise crm_roster.CrmRosterUnavailable("down")

    monkeypatch.setattr(roster, "crm_enrolled_roster", boom)
    assert roster.enrolled_roster() == sorted(set(config.ENROLLMENT))


def test_enrolled_roster_uses_crm_when_available(monkeypatch):
    monkeypatch.setenv(crm_roster.DSN_ENV, "postgresql://ok")
    monkeypatch.setattr(roster, "crm_enrolled_roster",
                        lambda *a, **k: ["U999", "U111"])
    assert roster.enrolled_roster() == ["U999", "U111"]


def test_funded_account_ids_keeps_only_accounts_with_holdings(monkeypatch):
    monkeypatch.setattr(crm_roster, "fetch_holdings_latest",
                        lambda ids, conn=None: {"a1": [{"symbol": "SPY"}], "a2": []})
    assert crm_roster.funded_account_ids(["a1", "a2", "a3"]) == {"a1"}
