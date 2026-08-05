"""AppTest render for the Action Center page — with a consolidated out-of-spec notice
(detail_json), the snooze / "ignore for N days" control, and the snoozed section.

Renders the page through streamlit's AppTest against a TEMP Action Center DB and asserts it
renders without exception, surfaces the notice + its detail, exposes the Dismiss + Ignore
controls, and that snoozing actually hides a notice from the active list.
"""
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


_SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    f"sys.path.insert(0, r'{_HERE}')\n"
    "import page_action_center\n"
    "page_action_center.render_action_center()\n"
)


def _seed(db_path):
    """Post one cash notice + one consolidated out-of-spec notice with detail_json."""
    import importlib
    import action_center
    importlib.reload(action_center)
    action_center.post_notice("cash_deploy", "Idle cash to deploy", "Some idle cash.",
                              severity="warn", dedup_key="s0_cash_deploy_open")
    action_center.post_notice(
        "outofspec", "3 of 10 accounts out of spec — rebalance needed",
        "Three accounts drifted.", severity="warn", dedup_key="outofspec_open",
        detail_json=[{"account": "U1", "model": "Growth", "advisor": "A", "net_liq": 1e6,
                      "n_legs": 4, "n_bonds": 1, "manual_bond_liquidation": True},
                     {"account": "U2", "model": "Growth", "advisor": "A", "net_liq": 5e4,
                      "n_legs": 2, "n_bonds": 0, "manual_bond_liquidation": False}])
    return action_center


def test_page_renders_without_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "ac.db"))
    _seed(tmp_path / "ac.db")
    at = AppTest.from_string(_SCRIPT, default_timeout=120).run()
    assert not at.exception, f"page raised: {at.exception}"


def test_shows_both_notices(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "ac.db"))
    _seed(tmp_path / "ac.db")
    at = AppTest.from_string(_SCRIPT, default_timeout=120).run()
    blob = "\n".join(md.value for md in at.markdown)
    assert "out of spec" in blob.lower()
    assert "Idle cash" in blob


def test_exposes_dismiss_and_snooze_controls(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "ac.db"))
    _seed(tmp_path / "ac.db")
    at = AppTest.from_string(_SCRIPT, default_timeout=120).run()
    labels = [b.label for b in at.button]
    assert any("Dismiss" in l for l in labels), labels
    assert any("Ignore for" in l for l in labels), labels
    # a 5/10/30 day snooze dropdown per notice
    assert len(at.selectbox) >= 1
    opts = at.selectbox[0].options
    assert opts == ["5 days", "10 days", "30 days"] or set(opts) == {"5 days", "10 days", "30 days"}


def test_snooze_button_hides_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGDESK_ACTION_CENTER_DB", str(tmp_path / "ac.db"))
    ac = _seed(tmp_path / "ac.db")
    at = AppTest.from_string(_SCRIPT, default_timeout=120).run()
    # click the out-of-spec notice's "Ignore for 5 days" button
    for b in at.button:
        if b.label == "Ignore for 5 days" and "outofspec" in b.key:
            b.click().run()
            break
    else:
        raise AssertionError("no outofspec Ignore button found: "
                             f"{[(b.key, b.label) for b in at.button]}")
    assert ac.is_snoozed("outofspec_open")
    active_keys = [n["dedup_key"] for n in ac.read_notices()]
    assert "outofspec_open" not in active_keys
