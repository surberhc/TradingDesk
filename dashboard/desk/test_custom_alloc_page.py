r"""Read-only enforcement + render tests for the Custom allocation page.

THE POINT OF THIS FILE is the read-only guarantee. page_custom_alloc.py shows Andrew's
hand-authored model allocations and previews the trades that would bring accounts back in
line with them — and it must be STRUCTURALLY INCAPABLE of sending any of it. "Read-only" is
easy to promise in a docstring and easy to lose in a later edit, so it is asserted here
three ways, mirroring the spirit of test_models_page.py's zero-widget assertion:

  1. NO ARM/EXECUTE AFFORDANCE. Every interactive widget the page renders is enumerated and
     its label checked against the arming vocabulary; there is no free-text box at all (so
     no confirm phrase can be typed) and no form.
  2. NO ARM TOKEN, ANYWHERE. Neither the module source nor the rendered page contains the
     literal arm token the executors require ("--arm-i-understand"), or the batch confirm
     phrase, or an armed=True construction.
  3. IT CANNOT START A PROGRAM. The module source contains no process-spawning machinery of
     any kind, and names no executor script, so it cannot invoke an executor with OR without
     an arm flag. The whole preview is built in-process from the pure engine instead.

The pure helpers (whole-share viability, the minimum viable account size, the percentage
read, and the drift/would-trade scan) are tested directly on synthetic data — which is the
only way to exercise the drift path today, since no account is assigned to a custom model
yet in the live book.

Run from dashboard/:
    "C:\TradingDesk-Local\venv\Scripts\python.exe" -m pytest -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

# page_custom_alloc imports the paperbot modules by bare name, relying on desk_app.py's
# sys.path bootstrap. Reproduce it here so the module imports standalone under pytest.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
for _sub in ("paperbot", "backtester", "connections", "strategies", "dailyreport",
             "livebot"):
    _p = _REPO / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import page_custom_alloc  # noqa: E402

_MODULE_SRC = (_HERE / "page_custom_alloc.py").read_text(encoding="utf-8")

_SCRIPT = (
    "import sys\n"
    f"sys.path.insert(0, r'{_HERE}')\n"
    "import page_custom_alloc\n"
    "page_custom_alloc.render_custom_alloc()\n"
)


def _run():
    at = AppTest.from_string(_SCRIPT, default_timeout=300)
    return at.run()


# --------------------------------------------------------------------------- #
# 1. It renders.
# --------------------------------------------------------------------------- #
def test_page_renders_without_exception():
    at = _run()
    assert not at.exception, f"page raised: {at.exception}"


def test_page_shows_its_read_only_statement():
    at = _run()
    heads = "\n".join(h.value for h in at.subheader)
    blob = "\n".join(md.value for md in at.markdown)
    assert "Custom allocation" in heads
    assert "read-only" in blob.lower()
    assert "cannot send anything" in blob.lower()


# --------------------------------------------------------------------------- #
# 2. READ-ONLY ENFORCEMENT — no arm/execute affordance, no arm token, no spawn.
# --------------------------------------------------------------------------- #
# The vocabulary of transmission. A widget on this page must never be labelled with any of
# these: they are the words an operator would look for to make something happen for real.
_FORBIDDEN_WIDGET_WORDS = (
    "arm", "execute", "send", "transmit", "place order", "place the order",
    "confirm", "go live", "submit",
)

# The literal tokens/phrases that make a transmit possible anywhere in this codebase.
# batch_rebalance_execute.py arms ONLY on the literal "--arm-i-understand" token, and the
# Control Plane's batch rail types "REBALANCE ALL" as its typed confirmation.
_FORBIDDEN_TOKENS = (
    "--arm-i-understand",
    "arm-i-understand",
    "arm_i_understand",
    "REBALANCE ALL",
    "armed=True",
)

# The machinery that could START an executor, and the executors themselves. Their absence
# from the source is what makes the page structurally unable to transmit: it cannot invoke
# an executor at all, so the presence or absence of an arm flag never even arises.
_FORBIDDEN_SPAWN = (
    "subprocess", "Popen", "os.system", "os.spawn", "os.exec", "runpy",
    "s0_live_deploy", "batch_rebalance_execute", "safe_execute",
    "live_fa_block_execute", "rebalance_run", "morning_execute",
)


def _all_widgets(at):
    """Every interactive element the page rendered, as (kind, label) pairs."""
    out = []
    for kind in ("button", "text_input", "number_input", "text_area", "checkbox",
                 "radio", "selectbox", "multiselect", "slider", "toggle",
                 "date_input", "time_input", "color_picker"):
        block = getattr(at, kind, None)
        if block is None:
            continue
        for w in block:
            out.append((kind, str(getattr(w, "label", "") or "")))
    return out


def test_no_arm_or_execute_affordance():
    """No control on this page may be an arm/execute/send affordance, and there is no
    free-text box at all — so the typed confirmation the transmit rails require cannot even
    be entered here."""
    at = _run()
    widgets = _all_widgets(at)

    assert len(at.text_input) == 0, "found a text_input — a confirm phrase could be typed"
    assert len(at.text_area) == 0, "found a text_area — read-only violation"
    assert len(at.number_input) == 0, "found a number_input — read-only violation"

    for kind, label in widgets:
        low = label.lower()
        for word in _FORBIDDEN_WIDGET_WORDS:
            assert word not in low, (
                f"{kind} labelled {label!r} contains the transmission word {word!r} — "
                f"this page must be read-only")


def test_module_source_has_no_arm_token():
    """The arm token / confirm phrase must not appear in this module at all."""
    for token in _FORBIDDEN_TOKENS:
        assert token not in _MODULE_SRC, (
            f"page_custom_alloc.py contains the arming token {token!r} — this page must be "
            f"structurally incapable of transmitting")


def test_rendered_page_has_no_arm_token():
    """Nor may the arm token reach the screen, where it could be copied into a rail that
    does transmit."""
    at = _run()
    parts = [md.value for md in at.markdown]
    parts += [label for _kind, label in _all_widgets(at)]
    blob = "\n".join(parts)
    for token in _FORBIDDEN_TOKENS:
        assert token not in blob, f"the rendered page shows the arming token {token!r}"


def test_module_cannot_start_a_program():
    """No process-spawning machinery and no executor named — the page cannot shell an
    executor with or without an arm flag, because it cannot shell anything."""
    for name in _FORBIDDEN_SPAWN:
        assert name not in _MODULE_SRC, (
            f"page_custom_alloc.py references {name!r} — this page must start no program "
            f"and name no executor")


def test_module_declares_no_write_path():
    """Belt and braces: the CRM seam is one-way. No write verb anywhere in the module."""
    lowered = _MODULE_SRC.lower()
    for verb in ("insert into", "update ", "delete from", " commit(", "executemany"):
        assert verb not in lowered, f"page_custom_alloc.py contains a write verb {verb!r}"


# --------------------------------------------------------------------------- #
# 3. PURE helpers — the two validations this page owns.
# --------------------------------------------------------------------------- #
def test_pct_rows_converts_percent_to_fraction_and_keeps_the_total():
    rows = [{"ticker": "usfr", "weight_pct": 40.0}, {"ticker": "SCHB", "weight_pct": 60.0}]
    out, total = page_custom_alloc._pct_rows(rows)
    assert total == 100.0
    assert out[0] == ("SCHB", 0.6)          # sorted heaviest first, ticker upper-cased
    assert out[1] == ("USFR", 0.4)


def test_pct_rows_surfaces_a_total_that_is_not_100():
    """A book that does not add up must SHOW that it does not add up, never be normalised."""
    rows = [{"ticker": "SCHB", "weight_pct": 60.0}, {"ticker": "USFR", "weight_pct": 30.0}]
    _out, total = page_custom_alloc._pct_rows(rows)
    assert total == 90.0


def test_whole_share_check_flags_a_position_that_rounds_to_zero():
    """The small-account failure this validation exists for: a 3% sleeve of a $180 fund in a
    $5,000 account buys ZERO shares — never held, and never band-breaching."""
    weights = {"SCHB": 0.97, "XLK": 0.03}
    prices = {"SCHB": 30.0, "XLK": 180.0}
    rows = page_custom_alloc.whole_share_rows(weights, prices, 5_000.0)
    by = {r["ticker"]: r for r in rows}
    assert by["SCHB"]["whole_shares"] == 161 and by["SCHB"]["buyable"] is True
    assert by["XLK"]["whole_shares"] == 0
    assert by["XLK"]["buyable"] is False
    assert "ZERO" in by["XLK"]["note"]


def test_whole_share_check_passes_a_viable_book():
    weights = {"SCHB": 0.6, "USFR": 0.4}
    prices = {"SCHB": 29.52, "USFR": 50.49}
    rows = page_custom_alloc.whole_share_rows(weights, prices, 5_000.0)
    assert all(r["buyable"] for r in rows)
    assert all(r["invested"] <= r["target_dollars"] + 1e-9 for r in rows)


def test_whole_share_check_flags_an_unpriced_leg():
    rows = page_custom_alloc.whole_share_rows({"ZZZZ": 1.0}, {}, 10_000.0)
    assert rows[0]["buyable"] is False
    assert rows[0]["price"] is None


def test_min_nav_for_whole_book():
    """The plain-English 'how small can an account be and still hold this properly' number:
    max(price / weight) over the book."""
    got = page_custom_alloc.min_nav_for_whole_book({"SCHB": 0.6, "USFR": 0.4},
                                                   {"SCHB": 29.52, "USFR": 50.49})
    assert got == max(29.52 / 0.6, 50.49 / 0.4)
    assert page_custom_alloc.min_nav_for_whole_book({"ZZZZ": 1.0}, {}) is None


def test_small_model_labels_are_display_only_and_cover_the_three_small_models():
    assert page_custom_alloc._is_small_model("Growth (Small, Custom)")
    assert not page_custom_alloc._is_small_model("Growth (Custom)")
    assert page_custom_alloc.SMALL_MODEL_LABELS <= set(page_custom_alloc.CUSTOM_MODEL_LABELS)


# --------------------------------------------------------------------------- #
# 4. The drift / would-trade preview path, on SYNTHETIC accounts.
# --------------------------------------------------------------------------- #
# No account is assigned to a custom model in the live book yet, so this is the only way to
# prove the drift read + would-trade preview actually work end to end against a custom
# Target. It runs the same pure engine the page runs — which builds and transmits nothing.
def _synthetic_target():
    from strategy_target import Target

    return Target(
        weights=pd.Series({"SCHB": 0.6, "USFR": 0.4}, dtype="float64"),
        prices=pd.Series({"SCHB": 30.0, "USFR": 50.0}, dtype="float64"),
        as_of=pd.Timestamp("2026-08-25"),
        price_date=pd.Timestamp("2026-08-24"),
        version="Growth (Custom)",
    )


def _synthetic_book():
    roster = [{
        "account_number": "U99999999", "account_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "model": "Growth (Custom)", "advisor_name": "Andrew P Surber",
        "entity": "Test Entity", "master_name": "TEST", "total_value": 100_000.0,
        "no_trade": False, "keep_open": False,
    }]
    holdings = {
        "aaaaaaaa-0000-0000-0000-000000000001": [
            {"account_id": "aaaaaaaa-0000-0000-0000-000000000001", "symbol": "SCHB",
             "asset_category": "STK", "quantity": 3333, "mark_price": 30.0,
             "market_value": 99_990.0, "as_of_date": "2026-08-24"},
        ]
    }
    return roster, holdings


def test_drift_scan_produces_a_would_trade_preview_for_a_custom_model():
    target = _synthetic_target()
    roster, holdings = _synthetic_book()
    universe = {"SCHB", "USFR"}

    scan = page_custom_alloc.scan_accounts_for_model(target, roster, holdings,
                                                     universe=universe)
    assert scan["n_accounts"] == 1
    assert scan["n_out_of_spec"] == 1
    verdict = scan["verdicts"][0]
    assert verdict["account"] == "U99999999"
    assert verdict["version"] == "Growth (Custom)"
    legs = {leg["symbol"]: leg for leg in verdict["legs"]}
    # 100% SCHB against a 60/40 book: sell SCHB down, buy USFR.
    assert legs["SCHB"]["side"] == "SELL"
    assert legs["USFR"]["side"] == "BUY"
    # A hand-authored book goes through the engine's STANDING CASH BUFFER exactly like a
    # computed one — 40% of the INVESTABLE amount, not of NAV. Derived from the shared knob
    # so this asserts the carve-out actually applied, without hardcoding its size.
    import investable

    expected = int((100_000.0 * (1.0 - investable.buffer_pct()) * 0.4) // 50.0)
    assert legs["USFR"]["shares"] == expected


def test_drift_scan_reports_in_line_when_the_account_already_matches():
    """The same account, already holding exactly what the custom book asks for (after the
    engine's standing cash buffer): nothing would trade."""
    import investable

    target = _synthetic_target()
    roster, _ = _synthetic_book()
    invested = 100_000.0 * (1.0 - investable.buffer_pct())
    schb = int((invested * 0.6) // 30.0)
    usfr = int((invested * 0.4) // 50.0)
    holdings = {
        "aaaaaaaa-0000-0000-0000-000000000001": [
            {"account_id": "aaaaaaaa-0000-0000-0000-000000000001", "symbol": "SCHB",
             "asset_category": "STK", "quantity": schb, "mark_price": 30.0,
             "market_value": schb * 30.0, "as_of_date": "2026-08-24"},
            {"account_id": "aaaaaaaa-0000-0000-0000-000000000001", "symbol": "USFR",
             "asset_category": "STK", "quantity": usfr, "mark_price": 50.0,
             "market_value": usfr * 50.0, "as_of_date": "2026-08-24"},
        ]
    }
    scan = page_custom_alloc.scan_accounts_for_model(target, roster, holdings,
                                                     universe={"SCHB", "USFR"})
    assert scan["n_out_of_spec"] == 0
    assert scan["verdicts"][0]["n_legs"] == 0


# --------------------------------------------------------------------------- #
# 5. The LOUD-FAILURE render paths, driven with a synthetic state.
# --------------------------------------------------------------------------- #
# The live CRM has exactly one published allocation and it is healthy (SCHB/USFR, both
# priceable, both buyable), and no account is assigned to any custom model — so the failure
# paths cannot be exercised against live data without publishing a bad allocation, which
# this stage is forbidden to do (the seam is read-only). Instead the page's single loader is
# replaced with a synthetic state so the RENDERING of both validations and of the drift
# preview is proved end to end.
_SYNTHETIC_SCRIPT = """
import sys
sys.path.insert(0, r'{here}')
import pandas as pd
import page_custom_alloc as p          # its bootstrap puts paperbot on sys.path
from strategy_target import Target

_ACC = 'aaaaaaaa-0000-0000-0000-000000000001'
_ROSTER = [{{'account_number': 'U99999999', 'account_id': _ACC,
             'model': 'Growth (Small, Custom)', 'advisor_name': 'Andrew P Surber',
             'entity': 'Test Entity', 'master_name': 'TEST', 'total_value': 5000.0}}]
_HOLD = {{_ACC: [{{'account_id': _ACC, 'symbol': 'SCHB', 'asset_category': 'STK',
                  'quantity': 166, 'mark_price': 30.0, 'market_value': 4980.0,
                  'as_of_date': '2026-08-24'}}]}}

_small = Target(weights=pd.Series({{'SCHB': 0.97, 'XLK': 0.03}}, dtype='float64'),
                prices=pd.Series({{'SCHB': 30.0, 'XLK': 180.0}}, dtype='float64'),
                as_of=pd.Timestamp('2026-08-25'), price_date=pd.Timestamp('2026-08-24'),
                version='Growth (Small, Custom)')

_BROKEN = ("custom model 'Growth (Custom)': NO PRICE HISTORY for one or more of its "
           "tickers (SCHB, ZZZZ).")

_STATE = {{
    'built_at_str': '2026-08-25 12:00:00', 'n_roster': 1, 'assigned_total': 1,
    'labels': ['Growth (Custom)', 'Growth (Small, Custom)'],
    'models': {{
        'Growth (Custom)': {{
            'published': True, 'small': False,
            'rows': [{{'ticker': 'SCHB', 'weight_pct': 60.0, 'version_number': 3,
                      'effective_from': '2026-08-01', 'published_at': '2026-08-01 09:00'}},
                     {{'ticker': 'ZZZZ', 'weight_pct': 40.0, 'version_number': 3,
                      'effective_from': '2026-08-01', 'published_at': '2026-08-01 09:00'}}],
            'target': None, 'meta': None, 'error': _BROKEN,
            'accounts': [], 'scan': None, 'scan_error': None,
        }},
        'Growth (Small, Custom)': {{
            'published': True, 'small': True,
            'rows': [{{'ticker': 'SCHB', 'weight_pct': 97.0, 'version_number': 1,
                      'effective_from': '2026-08-20', 'published_at': '2026-08-20 09:00'}},
                     {{'ticker': 'XLK', 'weight_pct': 3.0, 'version_number': 1,
                      'effective_from': '2026-08-20', 'published_at': '2026-08-20 09:00'}}],
            'target': _small, 'meta': None, 'error': None,
            'accounts': _ROSTER,
            'scan': p.scan_accounts_for_model(_small, _ROSTER, _HOLD,
                                              universe={{'SCHB', 'XLK'}}),
            'scan_error': None,
        }},
    }},
}}

p._load_state = lambda: _STATE
p.render_custom_alloc()
"""


def _run_synthetic():
    at = AppTest.from_string(_SYNTHETIC_SCRIPT.format(here=str(_HERE)), default_timeout=300)
    return at.run()


def test_unpriceable_ticker_is_surfaced_loudly_and_verbatim():
    """VALIDATION C-1. custom_target raises naming the ticker and the model; the page must
    show that message, not swallow it."""
    at = _run_synthetic()
    assert not at.exception, f"page raised: {at.exception}"
    errors = "\n".join(e.value for e in at.error)
    assert "ZZZZ" in errors, "the unpriceable ticker is not named on screen"
    assert "Growth (Custom)" in errors, "the affected model is not named on screen"
    blob = "\n".join(md.value for md in at.markdown)
    assert "CANNOT be traded as published" in blob


def test_whole_share_zero_rounding_is_flagged_on_screen():
    """VALIDATION C-2. A 3% XLK sleeve in a $5,000 account rounds to zero shares — the page
    must say so, naming the ticker."""
    at = _run_synthetic()
    blob = "\n".join(md.value for md in at.markdown)
    assert "round to ZERO shares" in blob
    assert "XLK" in blob


def test_drift_and_would_trade_preview_render_for_an_assigned_account():
    at = _run_synthetic()
    metrics = {m.label: m.value for m in at.metric}
    assert metrics.get("Accounts on this model") == "1"
    assert metrics.get("Out of line with the allocation") == "1"
    # The would-trade legs are rendered as a dataframe inside the preview expander.
    frames = [df.value for df in at.dataframe]
    legs = [f for f in frames if list(f.columns) == ["symbol", "side", "shares"]]
    assert legs, "no would-trade leg table rendered"
    symbols = set(legs[0]["symbol"])
    assert "SCHB" in symbols

    # AND THIS IS THE WHOLE POINT OF VALIDATION C-2, demonstrated: the 3% XLK sleeve of this
    # $5,000 account is worth ~$148 against a $180 share, so the engine emits NO XLK leg at
    # all. The account would never hold XLK, the miss never breaches a rebalance band, and
    # nothing in the trade preview would ever mention it. Only the whole-share check names
    # it — which the previous test asserts it does.
    assert "XLK" not in symbols
