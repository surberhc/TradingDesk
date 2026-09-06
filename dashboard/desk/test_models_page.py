"""AppTest render + read-only guard for the Strategy Models hub page.

Mirrors test_s0_model_page.py. Renders page_models through streamlit's AppTest and
asserts (a) it renders without exception, (b) it shows the frozen banner, every model
card (Growth/Balanced/Conservative resolved live + the PROPOSED Growth (Small) card),
and the shared engine content, (c) the resolved holdings shown MATCH what
strategy_target.current_target returns (no drift, no hardcoding), and (d) it exposes NO
edit/write widget of any kind — this view is display-only. Model editing is a later,
gated stage, not an in-app control.

IT ALSO GUARDS THE TWO-FAMILY SPLIT (2026-09-05). The page shows BOTH the models the
strategy code computes and the models Andrew writes himself, and the whole point of the
change is that the two can be told apart at a glance:
  * The computed labels must carry the "S0 " prefix on screen, while the `version` values
    the engine resolves against must stay the bare frozen identifiers. Both halves are
    asserted, because prefixing the wrong one would silently break resolution.
  * The hand-written models must appear when the client system returns rows. That read is
    STUBBED — the tests replace ``page_custom_alloc._load_state`` (the same seam
    test_custom_alloc_page.py stubs) so the suite never needs a live database, and so the
    stub proves page_models really goes through that loader rather than a query of its own.
  * When the client-system read fails, the page must degrade to one plain sentence and the
    computed models must still render in full.
"""
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

# page_models.py stays put, but it reuses page_s0_model's renderers and that module now
# lives in the strategy_views/ sub-folder, so that folder goes on sys.path too — exactly
# as desk_app.py does when the dashboard starts.
_HERE = Path(__file__).resolve().parent
_STRATEGY_VIEWS = _HERE / "strategy_views"
for _p in (_HERE, _STRATEGY_VIEWS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


_SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    f"sys.path.insert(0, r'{_HERE}')\n"
    f"sys.path.insert(0, r'{_STRATEGY_VIEWS}')\n"
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


# --------------------------------------------------------------------------- #
# The "S0 " prefix: display label only, never the resolved version identifier.
# --------------------------------------------------------------------------- #
def test_s0_labels_carry_the_prefix_but_versions_do_not():
    """Every computed model's DISPLAY label starts with "S0 " so it can never be confused
    with Andrew's hand-written "Growth (Custom)" — while the `version` it resolves against
    stays the bare frozen identifier the engine and the paperbot key on. Prefixing that
    would break resolution silently, so both halves are asserted."""
    import page_models

    labels = [m["label"] for m in page_models.MODELS]
    assert labels == ["S0 Growth", "S0 Balanced", "S0 Conservative", "S0 Growth (Small)"]
    for m in page_models.MODELS:
        assert m["label"].startswith("S0 "), f"{m['label']} is missing the S0 prefix"
        assert not m["version"].startswith("S0"), (
            f"{m['label']}: the frozen version identifier was prefixed too "
            f"({m['version']!r}) — the engine resolves against this and would fail")
    assert {m["version"] for m in page_models.MODELS} == {
        "Growth", "Balanced", "Conservative"}


def test_rendered_page_shows_the_prefixed_s0_labels():
    at = _run()
    blob = "\n".join(md.value for md in at.markdown)
    for label in ("S0 Growth", "S0 Balanced", "S0 Conservative", "S0 Growth (Small)"):
        assert label in blob, f"missing prefixed model card: {label}"
    # And the prefix is explained in plain English rather than left as bare shorthand.
    caps = "\n".join(c.value for c in at.caption)
    assert "Strategy 0" in caps


# --------------------------------------------------------------------------- #
# The models Andrew writes himself — read through page_custom_alloc's loader.
# --------------------------------------------------------------------------- #
# The client-system read is STUBBED at exactly the seam page_models reuses
# (page_custom_alloc._load_state), so these tests need no database AND they prove the
# reuse: if page_models ran a query of its own, stubbing this would change nothing.
_ACC = "aaaaaaaa-0000-0000-0000-000000000001"

_CUSTOM_SCRIPT = """
import sys
sys.path.insert(0, r'{here}')
sys.path.insert(0, r'{views}')
import page_models
import page_custom_alloc

_ROWS = [{{'ticker': 'SCHB', 'weight_pct': 60.0, 'version_number': 4,
           'effective_from': '2026-08-01', 'published_at': '2026-08-01 09:00'}},
         {{'ticker': 'USFR', 'weight_pct': 40.0, 'version_number': 4,
           'effective_from': '2026-08-01', 'published_at': '2026-08-01 09:00'}}]
_ACCOUNTS = [{{'account_number': 'U99999999', 'account_id': r'{acc}',
               'model': 'Growth (Custom)', 'advisor_name': 'Andrew P Surber',
               'entity': 'Test Entity', 'total_value': 250000.0}}]

_STATE = {{
    'built_at_str': '2026-09-05 12:00:00', 'n_roster': 301, 'assigned_total': 1,
    'labels': ['Growth (Custom)', 'Balanced (Custom)'],
    'models': {{
        'Growth (Custom)': {{'published': True, 'small': False, 'rows': _ROWS,
                            'target': None, 'meta': None, 'error': None,
                            'accounts': _ACCOUNTS, 'scan': None, 'scan_error': None}},
        'Balanced (Custom)': {{'published': False, 'small': False, 'rows': [],
                              'target': None, 'meta': None, 'error': None,
                              'accounts': [], 'scan': None, 'scan_error': None}},
    }},
}}

page_custom_alloc._load_state = lambda: _STATE
page_models.render_models()
"""

_CRM_DOWN_SCRIPT = """
import sys
sys.path.insert(0, r'{here}')
sys.path.insert(0, r'{views}')
import page_models
import page_custom_alloc


def _boom():
    raise RuntimeError('connection refused to the client system')


page_custom_alloc._load_state = _boom
page_models.render_models()
"""


def _run_script(template):
    at = AppTest.from_string(
        template.format(here=str(_HERE), views=str(_STRATEGY_VIEWS), acc=_ACC),
        default_timeout=300)
    return at.run()


def test_custom_models_appear_when_the_client_system_returns_rows():
    """A published hand-written model must show its name, its holdings (ticker + percent),
    its published version and how many accounts are on it."""
    at = _run_script(_CUSTOM_SCRIPT)
    assert not at.exception, f"page raised: {at.exception}"
    blob = "\n".join(md.value for md in at.markdown)

    assert "Growth (Custom)" in blob, "the hand-written model is missing from the page"
    # Its holdings, as ticker + percent, rendered by the shared holdings table.
    assert "SCHB" in blob and "USFR" in blob
    assert "60.000%" in blob and "40.000%" in blob
    # Its published version and the size of its assigned book.
    assert "version 4" in blob
    assert "Accounts assigned to this model" in blob
    assert "$250,000" in blob
    # A named-but-unpublished model is said to be unpublished, not silently dropped.
    assert "Balanced (Custom)" in blob
    assert "No holdings published yet" in blob


def test_custom_models_are_a_separate_clearly_labelled_family():
    """The two families must be told apart at a glance, in spelled-out plain English."""
    at = _run_script(_CUSTOM_SCRIPT)
    blob = "\n".join(md.value for md in at.markdown)
    caps = "\n".join(c.value for c in at.caption)
    assert "Models the strategy code works out automatically" in blob
    assert "Models Andrew writes himself" in blob
    # The one-sentence explanation of what actually separates them.
    assert "WHO CHOSE THE HOLDINGS" in caps
    # Both families really are on the one page.
    assert "S0 Growth" in blob and "Growth (Custom)" in blob


def test_status_badges_render_as_markup_not_as_literal_tag_text():
    """theme.section() escapes its whole argument, so the old
    ``theme.section(label + '<span ...>')`` put the raw tag on screen as text next to every
    model name. _section_with_badge fixes that; this stops it regressing, for BOTH families."""
    at = _run_script(_CUSTOM_SCRIPT)
    heads = [md.value for md in at.markdown if "dk-section" in md.value]
    assert heads, "no section headings rendered"
    for h in heads:
        assert "&lt;span" not in h, (
            f"section heading shows an escaped tag as literal text: {h[:160]}")
    joined = "\n".join(heads)
    assert "— LIVE (Strategy 0)" in joined, "the automatic-model badge is missing"
    assert "written by Andrew" in joined, "the hand-written-model badge is missing"


def test_custom_section_stays_read_only():
    """Adding the hand-written family must not add a single control — the Custom allocation
    page has a refresh button and a filter checkbox, and neither may leak in here."""
    at = _run_script(_CUSTOM_SCRIPT)
    for kind in ("button", "text_input", "number_input", "text_area", "checkbox",
                 "radio", "selectbox", "multiselect", "slider", "toggle"):
        assert len(getattr(at, kind)) == 0, (
            f"found a {kind} — the Models page must stay read-only")


def test_client_system_failure_degrades_to_one_sentence_and_keeps_the_s0_models():
    """No traceback, and the computed models must still render in full."""
    at = _run_script(_CRM_DOWN_SCRIPT)
    assert not at.exception, f"page raised: {at.exception}"
    warnings = "\n".join(w.value for w in at.warning)
    assert "could not be read from the client system" in warnings
    assert "connection refused" in warnings
    blob = "\n".join(md.value for md in at.markdown)
    for label in ("S0 Growth", "S0 Balanced", "S0 Conservative"):
        assert label in blob, f"{label} vanished when the client system was unreachable"


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
