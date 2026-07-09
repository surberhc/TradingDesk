r"""
s8_single_template_comparison.py -- two follow-ups to S8_80_4_ONLY_FULL_BACKTEST.md:

Task 1: isolate whether 80-$4's B2-vs-actual net-negative result is a crash-day
        (2025-10-10) artifact, or persists in the calmer subset. Also checks
        2026-05-18 (the other previously-flagged outsized day) individually.

Task 2: re-run the SAME full-strategy single-template backtest for the other two
        templates with usable sample size per TEMPLATE_FIXED_AND_GRID_ANALYSIS.md's
        6-config grid (80-$3, 50-$2) -- 80-$4 is reused from the prior script's
        already-verified output, not rerun from scratch.

REUSED, NOT REINVENTED (same pattern as s8_80_4_only_full_backtest.py):
  - label_combos() / explode_full_combo_ledger() / join_full_range_on_pairs() --
    imported directly from s8_80_4_only_full_backtest.py (identical TAT-join logic,
    now applied generically per template instead of hardcoded to 80-$4).
  - alpha_vs_beta_decomposition.load_b2_corrected_ledger() -- same B2-correction
    reconstruction, verified against the S8_SPEC.md headline before trusting any
    filtered subset.
  - compute_headline()'s reporting shape -- reused verbatim so all three templates
    are directly comparable in the same format as S8_80_4_ONLY_FULL_BACKTEST.md.

PAPER / research only. OFFLINE. STRICTLY READ-ONLY on all source CSVs. S8 is not
live; nothing here changes strategy/regime config or paperbot.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

OUT_DIR = Path(__file__).resolve().parent
if str(OUT_DIR) not in sys.path:
    sys.path.insert(0, str(OUT_DIR))

from alpha_vs_beta_decomposition import load_b2_corrected_ledger, REFERENCE_BALANCE  # noqa: E402
from s8_80_4_only_full_backtest import (  # noqa: E402
    label_combos, compute_headline, HEADLINE_TOTAL_PNL,
)

CRASH_DATE = 20251010
OUTLIER_DATE = 20260518
TAT_LAST_DATE = 20260319

OUT_LABELS_PATH = OUT_DIR / "s8_single_template_combo_labels.csv"


def _is_width(s, target):
    try:
        return float(s) == float(target)
    except (TypeError, ValueError):
        return False


def build_full_combo_frame():
    """Identical construction to s8_80_4_only_full_backtest.main()'s full_combo
    frame: combo_ledger.csv + B2-corrected pnl_actual/pnl_s8 + TAT template labels,
    computed ONCE here and reused for every template cut (both tasks), rather than
    recomputing the (slow, ~2600-row) TAT join per template."""
    combo = pd.read_csv(OUT_DIR / "combo_ledger.csv")
    print(f"Loaded combo_ledger.csv: {len(combo)} combos, "
          f"date range {combo['TradeDate'].min()}-{combo['TradeDate'].max()}")

    labeled = label_combos(combo)
    labeled.to_csv(OUT_LABELS_PATH, index=False)
    print(f"Wrote combo-level labels to {OUT_LABELS_PATH}")

    print("\nLoading S8 (B2-corrected) full-strategy per-combo P&L via "
          "alpha_vs_beta_decomposition.load_b2_corrected_ledger()...")
    combined, diag = load_b2_corrected_ledger()
    print(f"  grand total actual: ${diag['grand_total_actual']:,.2f}  "
          f"grand total S8: ${diag['grand_total_s8']:,.2f}")
    sanity = abs(diag["grand_total_s8"] - HEADLINE_TOTAL_PNL) < 100.0
    print(f"  sanity check vs S8_SPEC.md headline (${HEADLINE_TOTAL_PNL:,.0f}): "
          f"{'PASS' if sanity else 'FAIL -- STOP, do not trust downstream results'}")
    if not sanity:
        raise AssertionError("S8 headline reproduction failed -- do not trust filtered subset numbers.")

    # re-derive combo-grain pnl_actual/pnl_s8 with short_conid/short_open_dt attached
    # (identical logic to s8_80_4_only_full_backtest.main())
    combo_raw = pd.read_csv(OUT_DIR / "combo_ledger.csv")
    decoupled = pd.read_csv(OUT_DIR / "decoupled_long_legs.csv")
    rules = pd.read_csv(OUT_DIR / "longleg_rule_backtest_results.csv")
    d = decoupled.copy()
    r = rules.copy()
    d["key_round"] = d["long_pnl_multiple"].round(6)
    r["key_round"] = r["actual_exit_multiple"].round(6)
    d["occ"] = d.groupby(["TradeDate", "ComboType", "key_round"]).cumcount()
    r["occ"] = r.groupby(["TradeDate", "ComboType", "key_round"]).cumcount()
    dmerged = d.merge(
        r[["TradeDate", "ComboType", "key_round", "occ", "B2_close_on_short_stop"]],
        on=["TradeDate", "ComboType", "key_round", "occ"], how="left",
    )
    dmerged["long_pnl_B2"] = dmerged["B2_close_on_short_stop"] * dmerged["long_entry_cost"]
    dmerged["long_pnl_B2"] = dmerged["long_pnl_B2"].fillna(dmerged["long_fifo_pnl"])
    dmerged["delta_long_pnl"] = dmerged["long_pnl_B2"] - dmerged["long_fifo_pnl"]
    corr = dmerged.groupby(["short_conid", "short_open_dt"])["delta_long_pnl"].sum().reset_index()
    combo_raw = combo_raw.merge(corr, on=["short_conid", "short_open_dt"], how="left")
    combo_raw["delta_long_pnl"] = combo_raw["delta_long_pnl"].fillna(0.0)
    combo_raw["pnl_actual"] = combo_raw["total_realized_pnl"]
    combo_raw["pnl_s8"] = combo_raw["total_realized_pnl"] + combo_raw["delta_long_pnl"]

    check = abs(combo_raw["pnl_s8"].sum() -
                combined[combined["ComboType"].isin(["PutSpread", "CallSpread"])]["pnl_s8"].sum())
    print(f"  combo-only S8 total cross-check (should be ~0): ${check:,.2f}")

    full_combo = combo_raw.merge(
        labeled[["short_conid", "short_open_dt", "tat_match", "final_width_label", "final_dollar_label"]],
        on=["short_conid", "short_open_dt"], how="left",
    )
    return full_combo


def template_cuts(full_combo: pd.DataFrame, width: str, dollar: str, label_prefix: str):
    """Returns (cut_a_true_labeled_through_march, cut_b_proxy_through_july) for a
    given width/dollar template, exactly matching s8_80_4_only_full_backtest.py's
    two-cut convention."""
    mask_a = (
        full_combo["final_width_label"].apply(lambda s: _is_width(s, width)) &
        (full_combo["final_dollar_label"] == dollar) &
        (full_combo["tat_match"].isin(["MATCHED", "AMBIGUOUS_MULTI_CANDIDATE"]))
    )
    pop_a = full_combo[mask_a].copy()

    mask_b = full_combo["final_dollar_label"] == dollar
    pop_b = full_combo[mask_b].copy()

    return pop_a, pop_b


# --------------------------------------------------------------------------- #
# Task 1 -- crash-day / outlier-day isolation for 80-$4
# --------------------------------------------------------------------------- #
def single_day_isolation(df: pd.DataFrame, label: str) -> dict:
    """B2-vs-actual dollar gap (S8 total - actual total) with/without each of
    2025-10-10 and 2026-05-18 individually, and both together."""
    def gap(d):
        return float(d["pnl_s8"].sum() - d["pnl_actual"].sum())

    full_gap = gap(df)
    has_crash = (df["TradeDate"] == CRASH_DATE).any()
    has_outlier = (df["TradeDate"] == OUTLIER_DATE).any()

    ex_crash = df[df["TradeDate"] != CRASH_DATE]
    ex_outlier = df[df["TradeDate"] != OUTLIER_DATE]
    ex_both = df[~df["TradeDate"].isin([CRASH_DATE, OUTLIER_DATE])]

    crash_day_gap = full_gap - gap(ex_crash) if has_crash else 0.0
    outlier_day_gap = full_gap - gap(ex_outlier) if has_outlier else 0.0

    result = dict(
        label=label,
        n_combos=len(df), n_days=df["TradeDate"].nunique(),
        gap_full=full_gap,
        has_crash_day=bool(has_crash), has_outlier_day=bool(has_outlier),
        crash_day_contribution=crash_day_gap,
        outlier_day_contribution=outlier_day_gap,
        gap_ex_crash=gap(ex_crash),
        gap_ex_outlier=gap(ex_outlier),
        gap_ex_both=gap(ex_both),
    )

    print(f"\n{'=' * 78}\nSINGLE-DAY ISOLATION: {label}\n{'=' * 78}")
    print(f"n combos={len(df)}, n days={df['TradeDate'].nunique()}")
    print(f"B2-vs-actual gap, ALL DAYS INCLUDED         : ${full_gap:,.2f}")
    print(f"  2025-10-10 present in this cut: {has_crash}  "
          f"(day's own contribution to gap: ${crash_day_gap:,.2f})")
    print(f"  2026-05-18 present in this cut: {has_outlier}  "
          f"(day's own contribution to gap: ${outlier_day_gap:,.2f})")
    print(f"gap EXCLUDING 2025-10-10 only                : ${result['gap_ex_crash']:,.2f}")
    print(f"gap EXCLUDING 2026-05-18 only                : ${result['gap_ex_outlier']:,.2f}")
    print(f"gap EXCLUDING BOTH                            : ${result['gap_ex_both']:,.2f}")
    return result


def run_task1(full_combo: pd.DataFrame):
    print("\n" + "#" * 78)
    print("# TASK 1 -- 80-$4 crash-day isolation")
    print("#" * 78)
    pop_a, pop_b = template_cuts(full_combo, "80", "$4", "80-$4")
    res_a = single_day_isolation(pop_a, "80-$4 TRUE-LABELED (through 2026-03-19)")
    res_b = single_day_isolation(pop_b, "80-$4 $4-PROXY (full window through 2026-07-07)")
    return res_a, res_b


# --------------------------------------------------------------------------- #
# Task 2 -- full-strategy backtest for 80-$3 and 50-$2
# --------------------------------------------------------------------------- #
def run_task2(full_combo: pd.DataFrame):
    print("\n" + "#" * 78)
    print("# TASK 2 -- full-strategy backtests, 80-$3 and 50-$2")
    print("#" * 78)

    results = {}
    for width, dollar, name in [("80", "$3", "80-$3"), ("50", "$2", "50-$2")]:
        pop_a, pop_b = template_cuts(full_combo, width, dollar, name)

        print("\n" + "=" * 78)
        print(f"{name} -- CUT (a): TRUE-LABELED (through TAT coverage, <=2026-03-19)")
        print("=" * 78)
        res_a = compute_headline(pop_a, f"{name} TRUE-LABELED (through TAT coverage, <=2026-03-19)")
        iso_a = single_day_isolation(pop_a, f"{name} TRUE-LABELED -- single-day isolation")

        print("\n" + "=" * 78)
        print(f"{name} -- CUT (b): $-LABEL-PROXY, full window through 2026-07-07")
        print("=" * 78)
        res_b = compute_headline(pop_b, f"{name} $-LABEL-PROXY (full window through 2026-07-07)")
        iso_b = single_day_isolation(pop_b, f"{name} $-LABEL-PROXY -- single-day isolation")

        # width-confirmation breakdown within cut (b), same convention as the 80-$4 script
        def _is_w(s, w):
            return _is_width(s, w)
        n_confirmed_target = int((
            (full_combo["final_dollar_label"] == dollar) &
            full_combo["final_width_label"].apply(lambda s: _is_w(s, width)) &
            full_combo["tat_match"].isin(["MATCHED", "AMBIGUOUS_MULTI_CANDIDATE"])
        ).sum())
        n_total_b = len(pop_b)
        n_unconfirmed = n_total_b - int((
            (full_combo["final_dollar_label"] == dollar) &
            full_combo["final_width_label"].notna() &
            full_combo["tat_match"].isin(["MATCHED", "AMBIGUOUS_MULTI_CANDIDATE"])
        ).sum())
        print(f"\nWithin cut (b): {n_confirmed_target} combos confirmed {width}-width via real "
              f"TAT match; {n_unconfirmed} of {n_total_b} width UNCONFIRMED (past TAT coverage "
              f"or no TAT match -- $-label-only proxy).")

        results[name] = dict(res_a=res_a, res_b=res_b, iso_a=iso_a, iso_b=iso_b,
                              n_confirmed_target=n_confirmed_target,
                              n_unconfirmed_b=n_unconfirmed, n_total_b=n_total_b)
    return results


# --------------------------------------------------------------------------- #
# Final synthesis table
# --------------------------------------------------------------------------- #
def print_synthesis(res_80_4_a, res_80_4_b, iso_80_4_a, iso_80_4_b, task2_results):
    print("\n" + "#" * 78)
    print("# FINAL SYNTHESIS -- 3-way template comparison")
    print("#" * 78)

    rows = []
    rows.append(dict(
        template="80-$4", cut="true-labeled (thru Mar 2026)",
        total_pnl=res_80_4_a["total_pnl"], total_ret=res_80_4_a["total_ret"],
        n_days=res_80_4_a["n_days"],
        gap_full=iso_80_4_a["gap_full"], gap_ex_crash=iso_80_4_a["gap_ex_crash"],
    ))
    rows.append(dict(
        template="80-$4", cut="$-proxy (full window)",
        total_pnl=res_80_4_b["total_pnl"], total_ret=res_80_4_b["total_ret"],
        n_days=res_80_4_b["n_days"],
        gap_full=iso_80_4_b["gap_full"], gap_ex_crash=iso_80_4_b["gap_ex_crash"],
    ))
    for name in ["80-$3", "50-$2"]:
        r = task2_results[name]
        rows.append(dict(
            template=name, cut="true-labeled (thru Mar 2026)",
            total_pnl=r["res_a"]["total_pnl"], total_ret=r["res_a"]["total_ret"],
            n_days=r["res_a"]["n_days"],
            gap_full=r["iso_a"]["gap_full"], gap_ex_crash=r["iso_a"]["gap_ex_crash"],
        ))
        rows.append(dict(
            template=name, cut="$-proxy (full window)",
            total_pnl=r["res_b"]["total_pnl"], total_ret=r["res_b"]["total_ret"],
            n_days=r["res_b"]["n_days"],
            gap_full=r["iso_b"]["gap_full"], gap_ex_crash=r["iso_b"]["gap_ex_crash"],
        ))

    print(f"\n{'template':<8} {'cut':<28} {'total_pnl':>14} {'total_ret':>10} "
          f"{'n_days':>7} {'B2-actual_gap':>15} {'gap_ex_20251010':>17}")
    for r in rows:
        print(f"{r['template']:<8} {r['cut']:<28} {r['total_pnl']:>14,.2f} "
              f"{r['total_ret']:>10.1%} {r['n_days']:>7} {r['gap_full']:>15,.2f} "
              f"{r['gap_ex_crash']:>17,.2f}")

    return rows


def main():
    full_combo = build_full_combo_frame()

    iso_80_4_a, iso_80_4_b = run_task1(full_combo)

    print("\n" + "=" * 78)
    print("80-$4 FULL-STRATEGY HEADLINE (reused from s8_80_4_only_full_backtest.py, "
          "recomputed here for the synthesis table)")
    print("=" * 78)
    pop_80_4_a, pop_80_4_b = template_cuts(full_combo, "80", "$4", "80-$4")
    res_80_4_a = compute_headline(pop_80_4_a, "80-$4 TRUE-LABELED (through TAT coverage, <=2026-03-19)")
    res_80_4_b = compute_headline(pop_80_4_b, "80-$4 $-LABEL-PROXY (full window through 2026-07-07)")

    task2_results = run_task2(full_combo)

    rows = print_synthesis(res_80_4_a, res_80_4_b, iso_80_4_a, iso_80_4_b, task2_results)

    return dict(
        iso_80_4_a=iso_80_4_a, iso_80_4_b=iso_80_4_b,
        res_80_4_a=res_80_4_a, res_80_4_b=res_80_4_b,
        task2_results=task2_results, synthesis_rows=rows,
    )


if __name__ == "__main__":
    main()
