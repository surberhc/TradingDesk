"""
reentry.py — Staged re-entry ladder (SPEC.md §9).

After a defensive period, equity is rebuilt in STAGES, not all at once. This is a
smoothing layer that caps the equity target the regime/volatility engines would
otherwise allow, so a sharp bounce cannot whip the book straight back to full risk.

Stages -> equity cap (config.REENTRY_STAGES): 1->25%, 2->50%, 3->75%, 4->100%
(stage 0 -> 0%). Behavior:
  * De-risk is FAST: in a defensive regime the stage collapses immediately to
    whatever the conditions support (no waiting).
  * Re-risk is SLOW: the stage climbs at most ONE step per month toward the
    highest stage whose conditions are met.
  * Rollback: drop one stage when a stage's conditions fail AND credit/vol are
    deteriorating (a single soft miss does not force a rollback).
  * MAX-LAG override: if held below full for config.REENTRY_MAX_LAG_MONTHS while a
    sharp recovery is underway (the tape itself has recovered even though the
    breadth-based stage gates lag), jump straight to full so a V-recovery cannot
    strand the book in cash.

Pure and causal: stage[t] depends only on the conditions at and before t, so it
introduces no look-ahead.
"""

from __future__ import annotations

import pandas as pd

from strategies import config

_STAGE_COLS = ("stage1", "stage2", "stage3", "stage4")


def ladder_equity_cap(stage: int) -> float:
    """Equity ceiling (fraction of total) for a ladder stage; stage 0 -> 0%."""
    if stage <= 0:
        return 0.0
    return config.REENTRY_STAGES[min(stage, 4)]["equity_pct"]


def _target_stage(row: pd.Series) -> int:
    """Highest stage whose condition is met this month (0 if none)."""
    target = 0
    for n in range(1, 5):
        if bool(row[f"stage{n}"]):
            target = n
    return target


def compute_ladder_stages(
    conditions: pd.DataFrame,
    max_lag_months: int = config.REENTRY_MAX_LAG_MONTHS,
) -> pd.Series:
    """
    Walk the monthly conditions and return the ladder stage (0-4) each month.

    `conditions` columns (bool): stage1..stage4 (each stage's gate met),
    `defensive` (regime is Defensive/CapitalPreservation), `deteriorating`
    (credit or volatility worsening), `sharp_recovery` (the tape has recovered
    even if breadth lags — drives the MAX-LAG override). Indexed by signal date.
    """
    stage = 4  # no prior defensive period -> start fully invested
    months_capped = 0
    out: list[int] = []

    for _, row in conditions.iterrows():
        target = _target_stage(row)

        if bool(row["defensive"]):
            # Fast de-risk: a defensive regime can only LOWER the stage (collapse
            # toward what conditions support). It never raises equity — the staged
            # rebuild may only begin once the regime is no longer defensive.
            stage = min(stage, target)
            months_capped = 0
        elif target > stage:
            stage += 1              # staged rebuild: one step per month
        elif target < stage and bool(row["deteriorating"]):
            stage -= 1              # rollback one stage on a real deterioration
        # else: hold

        if stage < 4:
            months_capped += 1
            if months_capped >= max_lag_months and bool(row["sharp_recovery"]):
                stage = 4           # MAX-LAG override against a stranded V-recovery
                months_capped = 0
        else:
            months_capped = 0

        stage = max(0, min(4, stage))
        out.append(stage)

    return pd.Series(out, index=conditions.index, name="ladder_stage")
