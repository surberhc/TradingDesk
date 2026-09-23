"""Tests for the loud STUB block on the Trade Execution page.

What these pin down: when a run strands partial shares the page says so in a way that catches
the eye — a bold capital STUB naming that someone was rolled out of a model and a partial
share is left — and when it strands none the page says NOTHING about stubs at all. The second
half is as important as the first: a fraction left on a holding the model still wants never
reaches this block, and the page must not invent a reason to mention it.

Nothing here contacts a broker or the client system.

Run from dashboard/desk/:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
_EXECUTION = _HERE / "execution"
for _sub in ("paperbot", "backtester", "connections", "strategies", "dailyreport",
             "livebot"):
    _p = _REPO / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
for _p in (_HERE, _EXECUTION):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import page_trade_execution as pte  # noqa: E402

_STUBS = [{"account": "U7586137", "symbol": "BIL", "quantity": 0.8499, "value": 84.99},
          {"account": "U5721712", "symbol": "VTI", "quantity": 0.25, "value": 70.0}]


class _Recorder:
    """Stands in for streamlit: records every call so the test can read what was rendered."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _call(*args, **kwargs):
            self.calls.append((name, args))
        return _call

    @property
    def text(self) -> str:
        return " ".join(str(a) for _name, args in self.calls for a in args)


@pytest.fixture
def st(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(pte, "st", rec)
    return rec


def test_nothing_is_rendered_when_no_stub_was_stranded(st):
    """The usual case, and the point of the rule: silence."""
    pte._render_stubs([])
    assert st.calls == []


def test_the_block_shouts_stub_in_capitals(st):
    pte._render_stubs(_STUBS)
    assert "STUB" in st.text


def test_the_block_says_someone_rolled_out_of_a_model_holding(st):
    pte._render_stubs(_STUBS)
    assert "rolled out of a model holding" in st.text
    assert "partial share" in st.text


def test_the_block_names_the_accounts_and_the_tickers(st):
    pte._render_stubs(_STUBS)
    frames = [args[0] for name, args in st.calls if name == "dataframe"]
    assert len(frames) == 1
    rendered = frames[0].to_string()
    for expected in ("U7586137", "BIL", "U5721712", "VTI"):
        assert expected in rendered


def test_the_block_gives_the_total_value(st):
    pte._render_stubs(_STUBS)
    assert "$154.99" in st.text


def test_the_block_is_an_error_not_a_quiet_warning(st):
    """The owner asked for something that catches his eye, not another amber note."""
    pte._render_stubs(_STUBS)
    assert any(name == "error" for name, _args in st.calls)


def test_the_block_is_plain_english(st):
    """House rule #1 — no jargon. STUB is the one deliberate capitalised word."""
    pte._render_stubs(_STUBS)
    for jargon in ("TWS", "FRACTIONAL", "10243", "sub-share", "dust"):
        assert jargon not in st.text


def test_an_unpriced_stub_still_renders_and_says_the_value_is_unknown(st):
    pte._render_stubs([{"account": "U1", "symbol": "BIL", "quantity": 0.5, "value": 0.0}])
    assert "STUB" in st.text
    assert "could not be priced" in st.text


def test_the_result_renderer_shows_stubs_even_when_every_account_is_on_target(st):
    """The account re-read below has early returns; a stub must not be lost behind one."""
    pte._render_result({"created": {"created": 2},
                        "executed": {"outcomes": {"complete": True,
                                                  "counts": {"FILLED": 2}}},
                        "stubs": _STUBS,
                        "sync": {"ok": True, "in_sync": 2, "out_of_sync": 0,
                                 "accounts": []}})
    assert "STUB" in st.text


def test_the_result_renderer_stays_silent_when_the_run_stranded_nothing(st):
    pte._render_result({"created": {"created": 2},
                        "executed": {"outcomes": {"complete": True,
                                                  "counts": {"FILLED": 2}}},
                        "stubs": [],
                        "sync": {"ok": True, "in_sync": 2, "out_of_sync": 0,
                                 "accounts": []}})
    assert "STUB" not in st.text
