r"""
test_s5fh_dead_window.py -- the HARD dead-window guard of s5_financing_harness.

The warehouse's two-sided quotes are dead 2020-08-13..2021-12-31; a fill there would be
fiction. This pins that the exclusion is a GUARD (raises/flags), not a convention:
  * is_clean_day / is_dead_day classify the boundary dates correctly;
  * load_chain on a dead day raises DeadWindowError (never returns rows to fill);
  * available_days(clean_only=True) never lists a dead-window date;
  * day_is_fillable rejects a chain whose two-sided fraction is below the threshold.

Date-logic tests need no warehouse. load_chain is exercised with a stub monkeypatch so the
test stays hermetic (does not require the warehouse to be mounted).
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s5_financing_harness as h  # noqa: E402


def test_clean_and_dead_day_classification_at_boundaries():
    # window A end / dead start
    assert h.is_clean_day(dt.date(2020, 8, 12)) is True
    assert h.is_dead_day(dt.date(2020, 8, 12)) is False
    assert h.is_clean_day(dt.date(2020, 8, 13)) is False
    assert h.is_dead_day(dt.date(2020, 8, 13)) is True
    # dead end / window B start
    assert h.is_dead_day(dt.date(2021, 12, 31)) is True
    assert h.is_clean_day(dt.date(2021, 12, 31)) is False
    assert h.is_clean_day(dt.date(2022, 1, 3)) is True
    assert h.is_dead_day(dt.date(2022, 1, 3)) is False


def test_load_chain_raises_on_dead_window():
    with pytest.raises(h.DeadWindowError):
        h.load_chain(dt.date(2021, 7, 1))
    with pytest.raises(h.DeadWindowError):
        h.load_chain(dt.date(2020, 11, 2))


def test_available_days_excludes_dead_window(monkeypatch):
    # Stub the folder glob to a mix of clean + dead dates; clean_only must drop the dead.
    # Intentionally out of order so the sort inside available_days is exercised.
    dates = [dt.date(2022, 1, 3), dt.date(2020, 8, 13), dt.date(2021, 7, 1),
             dt.date(2020, 8, 12)]

    class _FakePath:
        """A sortable stub path (available_days sorts glob results). Sort by .stem so the
        stub orders like a real Path list; add __lt__ so `sorted()` does not TypeError."""
        def __init__(self, stem):
            self.stem = stem

        def __lt__(self, other):
            return self.stem < other.stem

        def __repr__(self):
            return f"_FakePath({self.stem})"

    def fake_glob(_pattern):
        return [_FakePath(d.strftime("%Y%m%d")) for d in dates]

    class _FakeFolder:
        def is_dir(self):
            return True

        def glob(self, pattern):
            return fake_glob(pattern)

    monkeypatch.setattr(h, "WAREHOUSE", type("W", (), {"__truediv__": lambda self, o: _FakeFolder()})())
    # This test pins the DATE-window exclusion, not the empty-placeholder probe; the stub
    # paths are not real files, so short-circuit the row-count probe to "has rows".
    monkeypatch.setattr(h, "_parquet_has_rows", lambda _p: True)
    out = h.available_days(clean_only=True)
    assert dt.date(2020, 8, 13) not in out
    assert dt.date(2021, 7, 1) not in out
    assert dt.date(2020, 8, 12) in out
    assert dt.date(2022, 1, 3) in out


def test_day_is_fillable_rejects_sparse_chain():
    # 10% two-sided -> not fillable; 90% -> fillable
    def chain(frac_two_sided):
        n = 100
        n_good = int(frac_two_sided * n)
        bids = [1.0] * n_good + [0.0] * (n - n_good)
        asks = [1.2] * n
        df = pd.DataFrame({"bid": bids, "ask": asks})
        df["two_sided"] = (df["bid"] > 0) & (df["ask"] > 0)
        return df

    assert h.day_is_fillable(chain(0.10)) is False
    assert h.day_is_fillable(chain(0.90)) is True
