"""AppTest render + read-only guard for the Strategy 0 Model & Parameters page.

Renders the page through streamlit's AppTest and asserts (a) it renders without
exception, (b) it shows the frozen-config banner and the live-state / ladder /
allowance content pulled from config, and (c) it exposes NO edit/write widget of any
kind (no button, input, form, checkbox, slider, etc.) — this view is display-only.
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
    "import page_s0_model\n"
    "page_s0_model.render_s0_model()\n"
)


def _run():
    at = AppTest.from_string(_SCRIPT, default_timeout=120)
    return at.run()


def test_page_renders_without_exception():
    at = _run()
    assert not at.exception, f"page raised: {at.exception}"


def test_no_edit_or_write_widgets():
    """The whole point: this view DISPLAYS parameters, never edits them."""
    at = _run()
    # Every interactive/input widget class AppTest can surface. All must be empty.
    assert len(at.button) == 0, "found a button — this page must be read-only"
    assert len(at.text_input) == 0, "found a text_input — read-only violation"
    assert len(at.number_input) == 0, "found a number_input — read-only violation"
    assert len(at.text_area) == 0, "found a text_area — read-only violation"
    assert len(at.checkbox) == 0, "found a checkbox — read-only violation"
    assert len(at.radio) == 0, "found a radio — read-only violation"
    assert len(at.selectbox) == 0, "found a selectbox — read-only violation"
    assert len(at.multiselect) == 0, "found a multiselect — read-only violation"
    assert len(at.slider) == 0, "found a slider — read-only violation"
    assert len(at.toggle) == 0, "found a toggle — read-only violation"


def test_shows_frozen_banner_and_model_content():
    at = _run()
    blob = "\n".join(md.value for md in at.markdown)
    # Frozen / read-only banner.
    assert "frozen" in blob.lower()
    assert "Read-only" in blob or "read-only" in blob.lower()
    # Regime ladder (all five regimes, plain English).
    for label in ("Risk-On", "Caution", "Defensive", "Capital preservation"):
        assert label in blob, f"missing regime row: {label}"
    # Version allowances incl. the live Growth marker.
    assert "Growth" in blob and "LIVE" in blob
    # Ticker universe (a representative ticker from each sleeve).
    for tkr in ("SPY", "XLK", "SGOV", "GLDM"):
        assert tkr in blob, f"missing universe ticker: {tkr}"


def test_ladder_values_match_frozen_config():
    """The ladder must reflect config LIVE, not hardcoded numbers."""
    from strategies import config as scfg  # noqa: E402
    at = _run()
    blob = "\n".join(md.value for md in at.markdown)
    # RiskOn score floor + equity band top come straight from config.
    lo, hi = scfg.REGIME_BANDS["RiskOn"]["score"]
    e_lo, e_hi = scfg.REGIME_BANDS["RiskOn"]["equity"]
    assert f"{lo:.0f}–{hi:.0f}" in blob or f"{lo:.0f}-{hi:.0f}" in blob
    assert f"{e_lo * 100:.0f}%" in blob and f"{e_hi * 100:.0f}%" in blob
