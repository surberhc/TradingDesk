"""Tests for the Action Center store (post / read / dedup / dismiss / unread), temp DB."""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def test_post_read_dedup_dismiss(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "ac.db"))
    import action_center  # imported AFTER env override; db_path() reads env per call

    assert action_center.read_notices() == []
    assert action_center.unread_count() == 0

    k1 = action_center.post_notice("cash_deploy", "T1", "B1", severity="warn", dedup_key="d")
    assert k1
    assert action_center.unread_count() == 1
    assert action_center.has_open("d")

    # same open dedup_key -> update in place, no new row, key unchanged
    k2 = action_center.post_notice("cash_deploy", "T2", "B2", severity="warn", dedup_key="d")
    assert k2 == k1
    assert action_center.unread_count() == 1
    assert action_center.read_notices()[0]["title"] == "T2"

    # dismiss
    assert action_center.dismiss(k1)
    assert action_center.unread_count() == 0
    assert not action_center.has_open("d")

    # after dismiss, same dedup_key creates a FRESH notice
    k3 = action_center.post_notice("cash_deploy", "T3", "B3", dedup_key="d")
    assert k3 and k3 != k1
    assert action_center.unread_count() == 1


def test_no_dedup_key_always_inserts(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "ac2.db"))
    import importlib
    import action_center
    importlib.reload(action_center)
    a = action_center.post_notice("x", "A", "a")
    b = action_center.post_notice("x", "B", "b")
    assert a and b and a != b
    assert action_center.unread_count() == 2


# --------------------------------------------------------------------------- #
# detail_json round-trip                                                       #
# --------------------------------------------------------------------------- #
def test_detail_json_round_trip(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "det.db"))
    import action_center

    detail = [{"account": "U1", "model": "Growth", "net_liq": 123456.0,
               "managed_net_liq": 113456.0, "n_held_aside": 1,
               "held_aside_value": 10000.0, "held_back": False},
              {"account": "U2", "model": "Growth", "net_liq": 5000.0,
               "managed_net_liq": 5000.0, "n_held_aside": 0,
               "held_aside_value": 0.0, "held_back": False}]
    # pass a Python list -> serialized by the store
    k = action_center.post_notice("outofspec", "T", "B", dedup_key="oos",
                                  detail_json=detail)
    assert k
    n = action_center.read_notices()[0]
    assert json.loads(n["detail_json"]) == detail

    # dedup update-in-place refreshes the detail too
    detail2 = detail[:1]
    action_center.post_notice("outofspec", "T2", "B2", dedup_key="oos",
                              detail_json=detail2)
    n = action_center.read_notices()[0]
    assert json.loads(n["detail_json"]) == detail2

    # old-style notice (no detail) reads NULL
    action_center.post_notice("cash_deploy", "C", "c", dedup_key="cash")
    row = [x for x in action_center.read_notices() if x["kind"] == "cash_deploy"][0]
    assert row["detail_json"] is None


# --------------------------------------------------------------------------- #
# snooze / is_snoozed / read_snoozed / unsnooze                                #
# --------------------------------------------------------------------------- #
def test_snooze_hides_from_active_and_badge(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "snz.db"))
    import action_center

    action_center.post_notice("cash_deploy", "T", "B", dedup_key="d")
    assert action_center.unread_count() == 1
    assert not action_center.is_snoozed("d")

    # snooze 5 days -> hidden from active list AND the badge, but is_snoozed True
    assert action_center.snooze("d", 5)
    assert action_center.is_snoozed("d")
    assert action_center.read_notices() == []          # hidden from active
    assert action_center.unread_count() == 0           # doesn't light the badge
    snz = action_center.read_snoozed()
    assert len(snz) == 1 and snz[0]["dedup_key"] == "d"
    assert snz[0]["snoozed_until"] is not None

    # un-snooze brings it straight back
    assert action_center.unsnooze("d")
    assert not action_center.is_snoozed("d")
    assert action_center.unread_count() == 1
    assert len(action_center.read_notices()) == 1


def test_snooze_poster_skips_while_snoozed(tmp_path, monkeypatch):
    """The POSTER-facing contract: while snoozed, is_snoozed(dedup_key) is True so the poster
    skips re-posting; the ONE existing (hidden) notice is untouched. This is what silences the
    daily re-nag that dismiss alone cannot."""
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "skip.db"))
    import action_center

    action_center.post_notice("cash_deploy", "orig", "b", dedup_key="d")
    action_center.snooze("d", 10)

    # simulate a poster's guard: it must see the snooze and NOT post
    if not action_center.is_snoozed("d"):
        action_center.post_notice("cash_deploy", "renagged", "b2", dedup_key="d")
    # nothing new surfaced; the single hidden notice still carries the ORIGINAL title
    assert action_center.read_notices() == []
    assert action_center.read_snoozed()[0]["title"] == "orig"


def test_snooze_expiry_re_enables(tmp_path, monkeypatch):
    """When snoozed_until passes, the notice re-surfaces automatically (no cron)."""
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "exp.db"))
    import sqlite3
    import action_center

    action_center.post_notice("cash_deploy", "T", "B", dedup_key="d")
    action_center.snooze("d", 5)
    assert action_center.is_snoozed("d")

    # force the stamp into the past (a real snooze would reach this time-wise)
    con = sqlite3.connect(str(tmp_path / "exp.db"))
    con.execute("UPDATE notices SET snoozed_until = '2000-01-01 00:00:00' WHERE dedup_key='d'")
    con.commit()
    con.close()

    assert not action_center.is_snoozed("d")
    assert action_center.unread_count() == 1
    assert len(action_center.read_notices()) == 1
    assert action_center.read_snoozed() == []


# --------------------------------------------------------------------------- #
# schema migration: an OLD DB (no detail_json / snoozed_until) upgrades cleanly #
# --------------------------------------------------------------------------- #
def test_migration_upgrades_old_db(tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "old.db"
    # Build a pre-migration store: the ORIGINAL 12-column schema + one row.
    con = sqlite3.connect(str(old))
    con.execute(
        "CREATE TABLE notices (notice_key TEXT UNIQUE, ts TEXT, day TEXT, kind TEXT, "
        "severity TEXT, title TEXT, body TEXT, action_hint TEXT, dedup_key TEXT, "
        "status TEXT, created_at TEXT, dismissed_at TEXT)")
    con.execute(
        "INSERT INTO notices VALUES ('abc','2026-08-01 10:00:00','20260801','cash_deploy',"
        "'warn','Old title','Old body','hint','s0_cash_deploy_open','unread',"
        "'2026-08-01 10:00:00',NULL)")
    con.commit()
    con.close()

    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(old))
    import action_center

    # first touch runs the additive migration; old row survives, new cols read NULL
    notices = action_center.read_notices()
    assert len(notices) == 1
    n = notices[0]
    assert n["title"] == "Old title"
    assert n["detail_json"] is None
    assert n["snoozed_until"] is None
    assert action_center.unread_count() == 1

    # the upgraded store now supports snooze on the migrated row
    assert action_center.snooze("s0_cash_deploy_open", 5)
    assert action_center.is_snoozed("s0_cash_deploy_open")
    assert action_center.read_notices() == []

    # and the new columns physically exist
    con = sqlite3.connect(str(old))
    cols = {r[1] for r in con.execute("PRAGMA table_info(notices)").fetchall()}
    con.close()
    assert "detail_json" in cols and "snoozed_until" in cols
