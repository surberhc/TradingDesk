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
