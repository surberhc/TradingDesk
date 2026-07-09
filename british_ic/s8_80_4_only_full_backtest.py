r"""
s8_80_4_only_full_backtest.py -- "if the account had traded ONLY the 80-$4 template
(both Puts and Calls) for the ENTIRE window, what would TOTAL S8 performance have
looked like?"

This is NOT a repeat of TEMPLATE_FIXED_AND_GRID_ANALYSIS.md's Analysis #1, which
isolated the 80-$4 subset's LONG-LEG-ONLY edge (B2 vs actual, measured only on the
long leg's own dollar swing). This script computes the FULL combo P&L (short leg's
real realized short_fifo_pnl + long leg's P&L under the B2 rule) for the 80-$4-only
combo population, in the SAME headline format as docs/S8_SPEC.md Sec 4 (Total P&L,
return on the $127,710 reference balance, month-by-month, day/leg win rates) --
reusing S8's own full-strategy P&L reconstruction from alpha_vs_beta_decomposition.py
(pnl_actual / pnl_s8 per combo_ledger.csv row), not rebuilding it.

REUSED, NOT REINVENTED:
  - reconstruct.explode_combo_groups_to_pairs() -- combo_ledger.csv (2,592 rows, one
    per short lifecycle, N paired longs each) exploded to one row per short-long pair,
    matching TAT's own per-entry granularity. This is the SAME function reconstruct.py
    itself uses before its own TAT cross-check.
  - reconstruct.load_tat_tradelog() -- loads TAT-tradelog.xlsx unchanged.
  - template_join.py's exact TAT-join key/tie-break logic (TradeDate + ComboType +
    exact short/long strike match, nearest-OpenTime tiebreak, AMBIGUOUS_MULTI_CANDIDATE
    kept not dropped) and its exact $-label proxy cutpoints ($2.55 / $3.55), applied
    here to the FULL combo_ledger population (2,592 combos, both closed_together=True
    AND False) rather than just the 1,617-row decoupled-legs subset that
    tat_full_join.csv covers -- because closed_together=True combos (long closed with
    the short, no correction needed) are NOT in decoupled_long_legs.csv at all and
    would be silently excluded from an 80-$4-only cut if only tat_full_join.csv were
    used. This is a superset extension of the existing join, same method.
  - alpha_vs_beta_decomposition.load_b2_corrected_ledger()'s exact B2-correction logic
    (pnl_actual = total_realized_pnl as traded; pnl_s8 = total_realized_pnl + the B2
    long-leg correction from longleg_rule_backtest_results.csv, uncovered legs kept at
    actual) -- imported and called directly, then filtered to the 80-$4 combo subset
    identified above, rather than reimplementing the B2 join.

Two reported cuts (per task brief, do not blend):
  (a) TRUE-LABELED 80-$4: real TAT Template match (MATCHED or AMBIGUOUS_MULTI_CANDIDATE),
      width='80' AND dollar='$4', TradeDate <= 2026-03-19 (TAT's coverage ceiling).
  (b) $4-LABEL-PROXY, full window through 2026-07-07: final_dollar_label == '$4' from
      short_open_price banding alone (mixes 80-$4 and 50-$4; width NOT confirmed).
      Confirmed-vs-assumed-width split is reported explicitly within this cut.

PAPER / research only. OFFLINE. STRICTLY READ-ONLY on all source CSVs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

OUT_DIR = Path(__file__).resolve().parent
if str(OUT_DIR) not in sys.path:
    sys.path.insert(0, str(OUT_DIR))

import reconstruct  # noqa: E402
from template_join import dollar_label_from_short_open_price, parse_template  # noqa: E402
from alpha_vs_beta_decomposition import load_b2_corrected_ledger, REFERENCE_BALANCE  # noqa: E402

OUT_RESULTS_PATH = OUT_DIR / "s8_80_4_only_combo_labels.csv"

TAT_LAST_DATE = 20260319
CRASH_DATE = 20251010
OUTLIER_DATE = 20260518

# S8_SPEC.md Sec 4 headline, for direct comparison.
HEADLINE_TOTAL_PNL = 138_982.0
HEADLINE_TOTAL_RET = 1.088
HEADLINE_N_DAYS = 236


# --------------------------------------------------------------------------- #
# 1. Explode combo_ledger.csv (ALL 2,592 combos) to pair-grain, TAT-join it,
#    exactly like template_join.py does for decoupled_long_legs.csv -- but on
#    the full combo population (including closed_together=True combos, which
#    never appear in decoupled_long_legs.csv / tat_full_join.csv at all).
# --------------------------------------------------------------------------- #
def parse_list_col(s):
    """combo_ledger.csv's long_strikes/long_open_dts/etc columns round-trip through
    CSV as Python-repr strings like '[np.int64(6200), np.int64(6230)]'. Safe-eval
    with only numpy exposed (no other builtins) -- same data this repo already wrote,
    not external/untrusted input."""
    if isinstance(s, list):
        return s
    return eval(s, {"__builtins__": {}}, {"np": np})


def explode_full_combo_ledger(combo: pd.DataFrame) -> pd.DataFrame:
    """Reuses reconstruct.explode_combo_groups_to_pairs's exact per-row shape, but
    re-parses the list-valued columns from their CSV string form first (that function
    was written to run on in-memory objects the same session, not a reloaded CSV)."""
    combo = combo.copy()
    for col in ["long_conids", "long_strikes", "long_open_dts", "long_open_prices",
                "long_open_qtys", "long_close_dts", "long_fifo_pnls"]:
        combo[col] = combo[col].apply(parse_list_col)
    return reconstruct.explode_combo_groups_to_pairs(combo)


def join_full_range_on_pairs(pair_df: pd.DataFrame, tat: pd.DataFrame) -> pd.DataFrame:
    """Identical join key/tie-break to template_join.join_full_range, applied to the
    combo-ledger-exploded pair population instead of decoupled_long_legs.csv."""
    tat = tat.copy()
    tat["OpenDateStr"] = pd.to_datetime(tat["OpenDate"]).dt.strftime("%Y%m%d")

    pair_df = pair_df.copy().reset_index(drop=True)
    pair_df["pair_row"] = pair_df.index
    pair_df["TradeDateStr"] = pair_df["TradeDate"].astype(str)

    rows = []
    n = len(pair_df)
    for i, c in pair_df.iterrows():
        if (i + 1) % 500 == 0 or (i + 1) == n:
            print(f"  joined {i+1}/{n} exploded combo pairs...", flush=True)

        trade_date_int = int(c["TradeDate"])
        if trade_date_int > TAT_LAST_DATE:
            rows.append({"pair_row": c["pair_row"], "tat_match": "NO_TAT_COVERAGE",
                         "tat_n_candidates": 0, "tat_Template": None})
            continue

        if c["ComboType"] == "PutSpread":
            candidates = tat[
                (tat["OpenDateStr"] == c["TradeDateStr"]) &
                (tat["TradeType"] == "PutSpread") &
                (tat["ShortPut"] == c["short_strike"]) &
                (tat["LongPut"] == c["long_strike"])
            ]
        else:
            candidates = tat[
                (tat["OpenDateStr"] == c["TradeDateStr"]) &
                (tat["TradeType"] == "CallSpread") &
                (tat["ShortCall"] == c["short_strike"]) &
                (tat["LongCall"] == c["long_strike"])
            ]

        if len(candidates) == 0:
            rows.append({"pair_row": c["pair_row"], "tat_match": "NO_MATCH",
                         "tat_n_candidates": 0, "tat_Template": None})
            continue

        short_open_time = pd.Timestamp(c["short_open_dt"]).time()
        candidates = candidates.copy()
        candidates["time_diff_sec"] = candidates["OpenTime"].apply(
            lambda t: abs((pd.Timestamp.combine(pd.Timestamp.today(), t) -
                           pd.Timestamp.combine(pd.Timestamp.today(), short_open_time)).total_seconds())
            if pd.notna(t) else np.nan
        )
        candidates = candidates.sort_values("time_diff_sec")
        best = candidates.iloc[0]

        rows.append({
            "pair_row": c["pair_row"],
            "tat_match": "MATCHED" if len(candidates) == 1 else "AMBIGUOUS_MULTI_CANDIDATE",
            "tat_n_candidates": len(candidates),
            "tat_Template": best["Template"],
        })

    return pd.DataFrame(rows)


def label_combos(combo: pd.DataFrame) -> pd.DataFrame:
    """Returns combo_ledger.csv (2,592 rows) with final_width_label / final_dollar_label
    / tat_match columns attached at the COMBO grain (short_conid + short_open_dt), by
    exploding to pairs, TAT-joining each pair, then rolling the label back up. A combo
    with >1 paired long leg (n_paired_longs>1) is a single scheduled entry -- if any
    exploded pair for that combo gets a real TAT match, that match's width/dollar label
    is used for the whole combo (the short leg, and hence the template, is the same
    across all paired longs in one combo group by construction)."""
    print("Exploding combo_ledger.csv (2,592 combos) to pair-grain (matches TAT's own "
          "per-entry granularity)...")
    pairs = explode_full_combo_ledger(combo)
    print(f"  {len(pairs)} exploded pairs from {len(combo)} combos")

    print("Loading TAT-tradelog.xlsx (full range, 2024-09-16 to 2026-03-19)...")
    tat = reconstruct.load_tat_tradelog()
    print(f"  {len(tat)} TAT rows loaded")

    print("Joining TAT Template onto every exploded combo pair "
          "(TradeDate+ComboType+strikes, nearest-OpenTime tiebreak)...")
    join_result = join_full_range_on_pairs(pairs, tat)

    pairs = pairs.reset_index(drop=True)
    pairs["pair_row"] = pairs.index
    merged = pairs.merge(join_result, on="pair_row", how="left")

    parsed = merged["tat_Template"].apply(parse_template)
    merged["tat_side"] = [p[0] for p in parsed]
    merged["tat_width"] = [p[1] for p in parsed]
    merged["tat_dollar"] = [p[2] for p in parsed]
    merged["final_width_label"] = np.where(
        merged["tat_match"].isin(["MATCHED", "AMBIGUOUS_MULTI_CANDIDATE"]),
        merged["tat_width"], np.nan,
    )
    merged["dollar_label_proxy"] = merged["short_open_price"].apply(dollar_label_from_short_open_price)
    merged["final_dollar_label"] = merged["tat_dollar"].fillna(merged["dollar_label_proxy"])

    # roll back up to combo grain (short_conid, short_open_dt): a combo has exactly one
    # short leg, so its template label is uniform across all its exploded pair-rows --
    # take the first non-null real TAT match if one exists (any pair matching is
    # sufficient since they all share the same short leg / same TAT row family).
    def pick_combo_label(g):
        real = g[g["tat_match"].isin(["MATCHED", "AMBIGUOUS_MULTI_CANDIDATE"])]
        if len(real) > 0:
            row = real.iloc[0]
            return pd.Series({
                "tat_match": row["tat_match"],
                "final_width_label": row["final_width_label"],
                "final_dollar_label": row["final_dollar_label"],
            })
        row = g.iloc[0]
        return pd.Series({
            "tat_match": row["tat_match"],
            "final_width_label": np.nan,
            "final_dollar_label": row["final_dollar_label"],
        })

    combo_labels = (
        merged.groupby(["short_conid", "short_open_dt"], group_keys=True)
        .apply(pick_combo_label, include_groups=False)
        .reset_index()
    )

    labeled = combo.merge(combo_labels, on=["short_conid", "short_open_dt"], how="left")
    return labeled


# --------------------------------------------------------------------------- #
# 2. Filter to 80-$4, attach S8 (B2-corrected) full P&L, report headline
# --------------------------------------------------------------------------- #
def compute_headline(df: pd.DataFrame, label: str, reference_balance: float = REFERENCE_BALANCE) -> dict:
    n_legs = len(df)
    n_days = df["TradeDate"].nunique()
    print("\n" + "-" * 78)
    print(f"CUT: {label}")
    print("-" * 78)
    print(f"n combos = {n_legs}, n trading days = {n_days}, "
          f"date range {df['TradeDate'].min()}-{df['TradeDate'].max()}")

    total_pnl = float(df["pnl_s8"].sum())
    total_ret = total_pnl / reference_balance
    day_pnl = df.groupby("TradeDate")["pnl_s8"].sum()
    day_win_rate = (day_pnl > 0).mean()
    leg_win_rate = (df["pnl_s8"] > df["pnl_actual"]).mean()
    # secondary metric, matching this project's other convention (S8 vs the actual
    # discretionary/blended-template outcome, not vs breakeven) -- day-level
    day_pnl_actual = df.groupby("TradeDate")["pnl_actual"].sum()
    day_beats_actual_rate = (day_pnl >= day_pnl_actual).mean()

    df = df.copy()
    df["month"] = pd.to_datetime(df["TradeDate"].astype(str), format="%Y%m%d").dt.to_period("M")
    monthly = df.groupby("month")["pnl_s8"].sum()
    monthly_ret = monthly / reference_balance
    n_months_positive = int((monthly > 0).sum())
    n_months_total = len(monthly)

    day_totals = df.groupby("TradeDate")["pnl_s8"].sum().sort_values(ascending=False)
    top2_days = day_totals.head(2)
    top2_sum = float(top2_days.sum())
    ex_top2 = df[~df["TradeDate"].isin(top2_days.index)]
    ex_top2_total = float(ex_top2["pnl_s8"].sum())
    ex_top2_day_win_rate = (ex_top2.groupby("TradeDate")["pnl_s8"].sum() > 0).mean() if len(ex_top2) else np.nan
    ex_top2_ndays = ex_top2["TradeDate"].nunique()

    print(f"Total P&L (S8/B2-corrected)      : ${total_pnl:,.2f}")
    print(f"Return on ${reference_balance:,.0f} reference balance : {total_ret:+.1%}")
    print(f"Months positive                  : {n_months_positive} / {n_months_total}")
    print(f"Day-level win rate (day P&L > 0) : {day_win_rate:.1%}  ({n_days} days)")
    print(f"Day-level win rate (S8 day total >= actual day total) : {day_beats_actual_rate:.1%}")
    print(f"Leg/combo-level win rate (S8 combo total > actual combo total) : {leg_win_rate:.1%}")
    print(f"Top-2 single days by P&L: {[(int(d), f'${v:,.2f}') for d, v in top2_days.items()]}")
    print(f"  sum of top-2: ${top2_sum:,.2f}")
    print(f"  total EXCLUDING top-2 days: ${ex_top2_total:,.2f}  "
          f"({ex_top2_ndays} days, day-win-rate {ex_top2_day_win_rate:.1%})")

    return dict(
        label=label, n_combos=n_legs, n_days=n_days,
        date_min=int(df["TradeDate"].min()), date_max=int(df["TradeDate"].max()),
        total_pnl=total_pnl, total_ret=total_ret,
        n_months_positive=n_months_positive, n_months_total=n_months_total,
        monthly_pnl=monthly, monthly_ret=monthly_ret,
        day_win_rate=day_win_rate, day_beats_actual_rate=day_beats_actual_rate,
        leg_win_rate=leg_win_rate,
        top2_days=top2_days, top2_sum=top2_sum,
        ex_top2_total=ex_top2_total, ex_top2_ndays=ex_top2_ndays,
        ex_top2_day_win_rate=ex_top2_day_win_rate,
    )


def main():
    combo = pd.read_csv(OUT_DIR / "combo_ledger.csv")
    print(f"Loaded combo_ledger.csv: {len(combo)} combos, "
          f"date range {combo['TradeDate'].min()}-{combo['TradeDate'].max()}")

    labeled = label_combos(combo)

    print("\n" + "=" * 78)
    print("COMBO-LEVEL TEMPLATE LABEL COVERAGE (all 2,592 combos)")
    print("=" * 78)
    print(labeled["tat_match"].value_counts(dropna=False).to_string())
    print(f"\nfinal_width_label non-null (true-labeled): "
          f"{labeled['final_width_label'].notna().sum()} / {len(labeled)}")

    labeled.to_csv(OUT_RESULTS_PATH, index=False)
    print(f"\nWrote combo-level labels to {OUT_RESULTS_PATH}")

    # --- load the S8 (B2-corrected) full-strategy per-combo P&L (reused, not rebuilt) ---
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

    # combined has TradeDate/ComboType/pnl_actual/pnl_s8, but NOT short_conid/short_open_dt
    # (fus/ul rows lack a combo-level key at all -- they're unmatched shorts / unclaimed
    # longs with no paired leg, never part of a "template" question). Re-derive the join
    # by re-running load_b2_corrected_ledger's own combo-level frame directly instead of
    # its already-concatenated `combined` output, so we can attach short_conid/short_open_dt.
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

    # re-verify this reproduces the same combo-only total the concatenated `combined`
    # frame's combo rows do (fus/ul excluded from both sides here)
    check = abs(combo_raw["pnl_s8"].sum() -
                combined[combined["ComboType"].isin(["PutSpread", "CallSpread"])]["pnl_s8"].sum())
    print(f"  combo-only S8 total cross-check (should be ~0): ${check:,.2f}")

    full_combo = combo_raw.merge(
        labeled[["short_conid", "short_open_dt", "tat_match", "final_width_label", "final_dollar_label"]],
        on=["short_conid", "short_open_dt"], how="left",
    )

    # ------------------------------------------------------------------
    # (a) TRUE-LABELED 80-$4, through TAT coverage ceiling (2026-03-19)
    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("CUT (a): TRUE-LABELED 80-$4 (width+$ confirmed via real TAT match)")
    print("=" * 78)
    def _is_width_80(s):
        # width label round-trips as the string "80" in-memory (parse_template's raw
        # split) but as the float 80.0 if ever reloaded from a written CSV -- normalize
        # via a numeric compare so both representations match.
        try:
            return float(s) == 80.0
        except (TypeError, ValueError):
            return False

    mask_a = (
        full_combo["final_width_label"].apply(_is_width_80) &
        (full_combo["final_dollar_label"] == "$4") &
        (full_combo["tat_match"].isin(["MATCHED", "AMBIGUOUS_MULTI_CANDIDATE"]))
    )
    pop_a = full_combo[mask_a].copy()
    res_a = compute_headline(pop_a, "80-$4 TRUE-LABELED (through TAT coverage, <=2026-03-19)")

    # ------------------------------------------------------------------
    # (b) $4-LABEL-PROXY, full window through 2026-07-07 (mixes 80-$4 + 50-$4)
    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("CUT (b): $4-LABEL-ONLY PROXY, full window (width NOT confirmed -- mixes 80-$4 + 50-$4)")
    print("=" * 78)
    mask_b = full_combo["final_dollar_label"] == "$4"
    pop_b = full_combo[mask_b].copy()
    res_b = compute_headline(pop_b, "$4-LABEL-PROXY (full window through 2026-07-07, mixed width)")

    # width-confirmed vs assumed breakdown WITHIN cut (b)
    def _is_width_50(s):
        try:
            return float(s) == 50.0
        except (TypeError, ValueError):
            return False

    n_confirmed_80 = int((mask_b & full_combo["final_width_label"].apply(_is_width_80) &
                          full_combo["tat_match"].isin(["MATCHED", "AMBIGUOUS_MULTI_CANDIDATE"])).sum())
    n_confirmed_50 = int((mask_b & full_combo["final_width_label"].apply(_is_width_50) &
                          full_combo["tat_match"].isin(["MATCHED", "AMBIGUOUS_MULTI_CANDIDATE"])).sum())
    n_unconfirmed = int(mask_b.sum()) - n_confirmed_80 - n_confirmed_50
    print(f"\nWithin cut (b): {n_confirmed_80} combos confirmed 80-width via real TAT match, "
          f"{n_confirmed_50} confirmed 50-width, {n_unconfirmed} width UNCONFIRMED (past TAT "
          f"coverage or no TAT match -- $-label-only proxy).")

    # ------------------------------------------------------------------
    # sample-size honesty: active-day comparison against full blended S8 (236 days)
    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("SAMPLE-SIZE HONESTY: active trading days, 80-$4-only vs full blended S8")
    print("=" * 78)
    full_combo["month"] = pd.to_datetime(full_combo["TradeDate"].astype(str), format="%Y%m%d")
    all_days = pd.concat([
        full_combo[["TradeDate"]],
        combined.loc[~combined["ComboType"].isin(["PutSpread", "CallSpread"]), ["TradeDate"]],
    ])["TradeDate"].nunique()
    print(f"Full blended S8 (all templates + unmatched/unclaimed legs): {all_days} distinct trading days "
          f"(S8_SPEC.md documents {HEADLINE_N_DAYS})")
    print(f"Cut (a) 80-$4 true-labeled active days : {res_a['n_days']}")
    print(f"Cut (b) $4-proxy active days            : {res_b['n_days']}")

    # ------------------------------------------------------------------
    # monthly tables
    # ------------------------------------------------------------------
    for res in (res_a, res_b):
        print(f"\nMonthly S8 P&L / return on ${REFERENCE_BALANCE:,.0f} -- {res['label']}")
        for m, pnl in res["monthly_pnl"].items():
            ret = pnl / REFERENCE_BALANCE
            print(f"  {m}: ${pnl:>12,.2f}  ({ret:+.2%})")

    return res_a, res_b, full_combo


if __name__ == "__main__":
    main()
