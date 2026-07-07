"""
Guards for the CanslimIbkrPriceGapfill THRASH-LOOP fix (ibkr_price_gapfill.py).

Root cause this pins down: ~17 survivors RESOLVE against IBKR but can never return daily
bars (IBKR error 162 — no market-data permission for PINK/ARCAEDGE names, or no history for
delisted-but-resolvable tickers). The old completion check counted them as outstanding work
forever, so `_is_complete()` was never True and the watchdog kill+relaunched the pull every
~65s with zero forward progress.

These tests assert the fix WITHOUT touching the real warehouse or IBKR: all state paths are
monkeypatched into a temp dir, so they run fully offline.

Guarantees pinned:
  1. A resolved symbol that returns no bars for TERMINAL_SKIP_AFTER consecutive runs is
     promoted to terminal-skip and drops out of the "remaining work" set.
  2. Once every resolved survivor is on-disk OR terminal-skip, _is_complete() is True and
     _remaining_symbols() is empty (so the watchdog stands down instead of relaunching).
  3. A single empty run does NOT terminal-skip (guards against a transient farm blip).
  4. A symbol that later returns bars has its empty-counter cleared (no lingering skip).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ibkr_price_gapfill as g  # noqa: E402


@pytest.fixture()
def state(tmp_path, monkeypatch):
    """Point every state path at an isolated temp dir and stub the universe."""
    st = tmp_path / "_state"
    prices = tmp_path / "prices"
    st.mkdir(parents=True)
    prices.mkdir(parents=True)
    monkeypatch.setattr(g, "STATE", st)
    monkeypatch.setattr(g, "PRICES", prices)
    monkeypatch.setattr(g, "RESOLVED_JSON", st / "ibkr_resolved.json")
    monkeypatch.setattr(g, "UNRESOLVED_JSON", st / "ibkr_unresolved.json")
    monkeypatch.setattr(g, "TERMINAL_JSON", st / "ibkr_terminal_skip.json")
    monkeypatch.setattr(g, "HEARTBEAT_JSON", st / "ibkr_heartbeat.json")
    monkeypatch.setattr(g, "LOG_TXT", st / "ibkr_pull_log.txt")
    # universe of 3 resolvable survivors
    monkeypatch.setattr(g, "_candidates", lambda: ["GOOD", "BAD1", "BAD2"])
    g._save_json(g.RESOLVED_JSON, {"GOOD": {}, "BAD1": {}, "BAD2": {}})
    # force TERMINAL_SKIP_AFTER=2 (module default) explicitly so the test is self-describing
    monkeypatch.setattr(g, "TERMINAL_SKIP_AFTER", 2)
    return tmp_path, prices


def _put_on_disk(prices, sym):
    """Write a minimal non-empty parquet so _done_symbols() counts it."""
    import pandas as pd
    pd.DataFrame({"date": [pd.Timestamp("2020-01-02")], "close": [1.0]}).to_parquet(
        prices / f"{sym}.parquet", index=False)


def test_single_empty_does_not_terminal_skip(state):
    _, _prices = state
    assert g._record_empty("BAD1") is False          # 1st empty -> not terminal yet
    assert "BAD1" not in g._terminal_skip()
    # still counted as remaining work (nothing on disk, not terminal)
    assert "BAD1" in g._remaining_symbols()


def test_two_consecutive_empties_promote_to_terminal(state):
    g._record_empty("BAD1")
    assert g._record_empty("BAD1") is True           # 2nd empty -> terminal
    assert "BAD1" in g._terminal_skip()
    assert "BAD1" not in g._remaining_symbols()       # dropped from outstanding work


def test_clear_empty_resets_counter(state):
    g._record_empty("BAD1")                           # one empty on the books
    g._clear_empty("BAD1")                            # symbol finally returned bars
    assert g._record_empty("BAD1") is False           # counter restarted, not immediately terminal


def test_complete_when_all_on_disk_or_terminal(state):
    _, prices = state
    # GOOD lands on disk; BAD1 + BAD2 are permanently unpullable
    _put_on_disk(prices, "GOOD")
    for sym in ("BAD1", "BAD2"):
        g._record_empty(sym); g._record_empty(sym)    # -> terminal
    assert g._terminal_skip() == {"BAD1", "BAD2"}
    assert g._remaining_symbols() == []               # nothing pullable left
    assert g._is_complete() is True                   # watchdog will STAND DOWN


def test_not_complete_while_real_work_remains(state):
    _, prices = state
    _put_on_disk(prices, "GOOD")
    g._record_empty("BAD1"); g._record_empty("BAD1")  # BAD1 terminal
    # BAD2 is still genuinely pullable (0 empties) -> NOT complete
    assert "BAD2" in g._remaining_symbols()
    assert g._is_complete() is False


def test_not_complete_before_any_resolve_pass(state, monkeypatch):
    monkeypatch.setattr(g, "_save_json", g._save_json)  # keep real
    g._save_json(g.RESOLVED_JSON, {})                  # no resolve pass yet
    assert g._is_complete() is False                   # there is work to do
