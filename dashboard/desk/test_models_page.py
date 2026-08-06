"""AppTest render + read-only guard for the Strategy Models hub page.

Mirrors test_s0_model_page.py. Renders page_models through streamlit's AppTest and
asserts (a) it renders without exception, (b) it shows the frozen banner, every model
card (Growth/Balanced/Conservative resolved live + the PROPOSED Growth (Small) card),
and the shared engine content, (c) the resolved holdings shown MATCH what
strategy_target.current_target returns (no drift, no hardcoding), and (d) it exposes NO
edit/write widget of any kind — this view is display-only. Model editing is a later,
gated stage, not an in-app control.
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
    "import page_models\n"
    "page_models.render_models()\n"
)


def _run():
    at = AppTest.from_string(_SCRIPT, default_timeout=300)
    return at.run()


def test_page_renders_without_exception():
    at = _run()
    assert not at.exception, f"page raised: {at.exception}"


def test_no_edit_or_write_widgets():
    """The whole point: this view DISPLAYS models, never edits them. Editing is the
    later gated review->validate->deploy stage, not an in-app control."""
    at = _run()
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


def test_shows_banner_all_models_and_shared_engine():
    at = _run()
    blob = "\n".join(md.value for md in at.markdown)
    # Frozen / read-only banner.
    assert "frozen" in blob.lower()
    assert "read-only" in blob.lower()
    assert "review" in blob.lower() and "deploy" in blob.lower()  # gated pipeline named
    # Every model card present.
    for label in ("Growth", "Balanced", "Conservative", "Growth (Small)"):
        assert label in blob, f"missing model card: {label}"
    # Growth is flagged the live S0 model; Growth (Small) flagged PROPOSED/not deployed.
    assert "LIVE" in blob
    assert "PROPOSED" in blob and "not yet deployed" in blob.lower()
    # Growth (Small) proposed holdings + auto-tier note.
    assert "SCHB" in blob and "USFR" in blob
    assert "$25,000" in blob or "25,000" in blob or "$25k" in blob.lower()
    # Shared engine (reused S0 renderers): regime ladder + universe tickers.
    for label in ("Risk-On", "Caution", "Defensive", "Capital preservation"):
        assert label in blob, f"missing regime row: {label}"
    for tkr in ("XLK", "SGOV", "GLDM"):
        assert tkr in blob, f"missing universe ticker: {tkr}"


def test_resolved_holdings_match_engine():
    """The resolved holdings shown must equal strategy_target.current_target's output
    for each live-resolving version — proving they are pulled live, never hardcoded."""
    import strategy_target  # on sys.path via page_models' bootstrap (import triggers it)
    import page_models  # noqa: F401  (ensures the sys.path bootstrap has run)

    at = _run()
    blob = "\n".join(md.value for md in at.markdown)
    for version in ("Growth", "Balanced", "Conservative"):
        t = strategy_target.current_target(version)
        for tkr, w in t.weights.items():
            if w <= 1e-9:
                continue
            cell = f"{w * 100:.3f}%"
            assert str(tkr) in blob, f"{version}: ticker {tkr} missing from page"
            assert cell in blob, f"{version}: weight {cell} for {tkr} missing from page"
