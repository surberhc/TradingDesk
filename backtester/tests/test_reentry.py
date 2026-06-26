"""
Unit tests for the staged re-entry ladder (SPEC.md §9).

Covers: the stage->cap mapping; fast de-risk; one-stage-per-month rebuild;
rollback only on a real deterioration; and the MAX-LAG override against a
stranded V-recovery.
"""

import pandas as pd
import pytest

from strategies import config
from strategies.parts import reentry


def _conds(rows: list[dict]) -> pd.DataFrame:
    """Build a monthly conditions frame; unspecified flags default to False."""
    cols = ["stage1", "stage2", "stage3", "stage4", "defensive", "deteriorating", "sharp_recovery"]
    idx = pd.bdate_range("2020-01-31", periods=len(rows), freq="ME")
    return pd.DataFrame([{c: r.get(c, False) for c in cols} for r in rows], index=idx)


def _all_stages(met: bool) -> dict:
    return {f"stage{n}": met for n in range(1, 5)}


def test_cap_mapping():
    assert reentry.ladder_equity_cap(0) == 0.0
    assert reentry.ladder_equity_cap(1) == config.REENTRY_STAGES[1]["equity_pct"]
    assert reentry.ladder_equity_cap(4) == 1.0


def test_fast_derisk_collapses_immediately():
    # Defensive month with no stage conditions met -> stage 0 at once.
    stages = reentry.compute_ladder_stages(_conds([{"defensive": True}]))
    assert stages.iloc[0] == 0


def test_staged_rebuild_one_per_month():
    rows = [{"defensive": True}]  # de-risk to 0
    rows += [{**_all_stages(True)} for _ in range(5)]  # full conditions thereafter
    stages = reentry.compute_ladder_stages(_conds(rows))
    assert list(stages) == [0, 1, 2, 3, 4, 4]  # climbs one stage per month, then holds


def test_rollback_only_on_deterioration():
    # Climb to stage 3, then a month that only supports stage 2.
    base = [{"defensive": True}] + [{**_all_stages(True)} for _ in range(3)]  # -> stage 3
    stage2_only = {"stage1": True, "stage2": True}

    deteriorating = reentry.compute_ladder_stages(_conds(base + [{**stage2_only, "deteriorating": True}]))
    assert deteriorating.iloc[-1] == 2  # rolls back one stage

    soft_miss = reentry.compute_ladder_stages(_conds(base + [{**stage2_only}]))
    assert soft_miss.iloc[-1] == 3  # single soft miss without deterioration -> hold


def test_max_lag_override():
    # Stuck at stage 1 (only stage1 ever clears) but the tape has V-recovered.
    rows = [{"defensive": True}]
    rows += [{"stage1": True, "sharp_recovery": True} for _ in range(5)]
    stages = reentry.compute_ladder_stages(_conds(rows), max_lag_months=3)
    assert stages.iloc[1] == 1            # climbs to 1
    assert stages.iloc[-1] == 4           # MAX-LAG forces full re-entry
    assert (stages == 4).any()


def test_no_defensive_period_stays_full():
    # Never defensive, conditions always full -> stays at stage 4 throughout.
    stages = reentry.compute_ladder_stages(_conds([{**_all_stages(True)} for _ in range(4)]))
    assert (stages == 4).all()
