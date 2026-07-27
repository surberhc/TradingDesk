r"""test_verify_ibkr_nightly.py — offline tests for the one-shot IBKR nightly EOD checker.

FULLY OFFLINE + FAST: no warehouse, no parquet, no pandas, no real mail. Every seam of
verify_ibkr_nightly.check(...) is injected — ``read_rows`` returns canned counts, ``send``
records (or raises) instead of emailing — so the whole suite runs in well under a second and
touches nothing real.

WHAT THESE PIN:
  * all-roots-present  -> PASS and exactly one email sent.
  * one-root-missing   -> FAIL, the missing root named in both the result and the subject.
  * empty parquet (0 rows) -> FAIL (a 0-row marker is not a successful pull).
  * a send() that RAISES does not propagate — check() swallows it and reports emailed=False.
  * main() returns 0 even when the underlying check errors.

Run from datacollector/ (so ``import config`` inside the module resolves):
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest test_verify_ibkr_nightly.py -q
"""

from __future__ import annotations

from typing import Any, List, Tuple

import verify_ibkr_nightly as v


class _Recorder:
    """A fake ``send`` that records every call and never touches mail."""

    def __init__(self, result: bool = True, raises: bool = False) -> None:
        self.result = result
        self.raises = raises
        self.calls: List[Tuple[str, List[Any]]] = []

    def __call__(self, subject: str, lines, **_kw) -> bool:
        self.calls.append((subject, list(lines)))
        if self.raises:
            raise RuntimeError("boom in send")
        return self.result


def _counts_reader(counts: dict):
    """An injectable ``read_rows`` that maps the parquet path's parent dir (the ROOT) to a
    canned row count, so tests never build a real file."""
    def _read(path) -> int:
        # path is config.RAW_OPTIONS/{ROOT}/{daystr}.parquet — the parent dir name is the root.
        root = path.parent.name
        return int(counts.get(root, 0))
    return _read


def test_all_roots_present_passes_and_emails():
    send = _Recorder()
    counts = {"SPX": 19000, "SPXW": 21000, "RUT": 4000, "NDX": 5000}
    res = v.check(today="20260727", read_rows=_counts_reader(counts), send=send, log=lambda *_: None)

    assert res["pass"] is True
    assert res["missing"] == []
    assert res["roots"] == counts
    assert res["emailed"] is True
    assert res["date"] == "20260727"
    # Exactly ONE email, and it says PASS.
    assert len(send.calls) == 1
    assert send.calls[0][0] == "IBKR nightly EOD: PASS"


def test_one_root_missing_fails_and_names_it():
    send = _Recorder()
    counts = {"SPX": 19000, "SPXW": 21000, "RUT": 4000, "NDX": 0}  # NDX file absent -> 0
    res = v.check(today="20260727", read_rows=_counts_reader(counts), send=send, log=lambda *_: None)

    assert res["pass"] is False
    assert res["missing"] == ["NDX"]
    assert res["emailed"] is True
    assert len(send.calls) == 1
    subject = send.calls[0][0]
    assert subject.startswith("IBKR nightly EOD: FAIL")
    assert "NDX" in subject


def test_empty_parquet_zero_rows_fails():
    send = _Recorder()
    # Every root present but SPXW is a 0-row (no-data-day) marker.
    counts = {"SPX": 19000, "SPXW": 0, "RUT": 4000, "NDX": 5000}
    res = v.check(today="20260727", read_rows=_counts_reader(counts), send=send, log=lambda *_: None)

    assert res["pass"] is False
    assert res["missing"] == ["SPXW"]
    assert res["roots"]["SPXW"] == 0
    assert "SPXW" in send.calls[0][0]


def test_multiple_missing_listed_in_subject():
    send = _Recorder()
    counts = {"SPX": 19000, "SPXW": 0, "RUT": 4000, "NDX": 0}
    res = v.check(today="20260727", read_rows=_counts_reader(counts), send=send, log=lambda *_: None)

    assert res["pass"] is False
    assert res["missing"] == ["SPXW", "NDX"]
    assert "SPXW,NDX" in send.calls[0][0]


def test_send_raising_does_not_propagate():
    send = _Recorder(raises=True)
    counts = {"SPX": 1, "SPXW": 1, "RUT": 1, "NDX": 1}
    # Must NOT raise, and must report emailed=False.
    res = v.check(today="20260727", read_rows=_counts_reader(counts), send=send, log=lambda *_: None)

    assert res["emailed"] is False
    assert res["pass"] is True          # the check itself still succeeded
    assert res["error"] is None         # a raising mailer is not a check error
    assert len(send.calls) == 1         # it was attempted exactly once


def test_read_rows_raising_counts_zero_not_crash():
    send = _Recorder()

    def _boom(_path):
        raise IOError("disk gone")

    res = v.check(today="20260727", read_rows=_boom, send=send, log=lambda *_: None)
    assert res["pass"] is False
    assert res["roots"] == {"SPX": 0, "SPXW": 0, "RUT": 0, "NDX": 0}
    assert res["missing"] == ["SPX", "SPXW", "RUT", "NDX"]
    assert res["emailed"] is True       # a FAIL email still goes out


def test_custom_roots_respected():
    send = _Recorder()
    counts = {"SPX": 5, "SPXW": 5}
    res = v.check(today="20260727", roots=("SPX", "SPXW"),
                  read_rows=_counts_reader(counts), send=send, log=lambda *_: None)
    assert res["pass"] is True
    assert set(res["roots"]) == {"SPX", "SPXW"}


def test_check_internal_error_still_emails_and_sets_error():
    # A read_rows the check will call is fine; force the failure EARLIER by making roots
    # iteration explode is hard, so instead drive the top-level guard via a send that is fine
    # but a _build_email path... simplest: pass a non-iterable-ish today is still str. Instead
    # monkeypatch _resolve_today to raise.
    send = _Recorder()
    orig = v._resolve_today
    v._resolve_today = lambda today=None: (_ for _ in ()).throw(ValueError("bad clock"))
    try:
        res = v.check(read_rows=_counts_reader({}), send=send, log=lambda *_: None)
    finally:
        v._resolve_today = orig
    assert res["pass"] is False
    assert res["error"] is not None and "bad clock" in res["error"]
    # Best-effort failure email still attempted.
    assert len(send.calls) == 1
    assert send.calls[0][0].startswith("IBKR nightly EOD: FAIL")


def test_main_returns_zero_even_when_check_errors(monkeypatch):
    def _boom(**_kw):
        raise RuntimeError("catastrophe")

    monkeypatch.setattr(v, "check", _boom)
    assert v.main() == 0


def test_main_returns_zero_normal(monkeypatch):
    monkeypatch.setattr(v, "check", lambda **_kw: {"pass": True, "emailed": True})
    assert v.main() == 0
