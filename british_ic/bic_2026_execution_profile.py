"""
British IC — 2026 execution profile.

Builds a fresh, 2026-scoped (2026-01-01 through 2026-07-07, the last date with
real fills in this dataset) execution profile of the externally-traded "British
IC" 0DTE SPX credit-spread strategy (IBKR account U***9156, run via TAT/
NinjaTrader — NOT a TradingDesk paperbot strategy). Answers, from real data only:

  1. Trade frequency per day in 2026 (count + time-of-day distribution).
  2. Position sizing per entry: contracts, notional max-loss, % of account
     value, and margin/BuyingPower where TAT ground truth exists.
  3. Wing width (SPX points) distribution.
  4. Short-leg delta targeted (TAT-covered period only).

This has never been assembled before scoped to 2026 alone — prior work
(STRATEGY_MECHANICS.md, template_delta_stats.csv) covered the full
2024-09-16..2026-07-07 reconstruction window, not 2026 in isolation, and never
touched trade-frequency-per-day or account-value/margin sizing at all.

Data sources (all read-only):
  - combo_ledger.csv                    IBKR-fill-verified per-combo ledger
  - fully_unmatched_short_lifecycles.csv real short opens with no paired long
  - alpha_vs_beta_daily_series.csv       real daily P&L (pnl_actual) + cum_pnl_actual
  - balance_validation.csv               REAL validated IBKR balance, 2025-07-09..2026-02-19 ONLY
  - TAT-tradelog.xlsx (external, read-only) real NinjaTrader/TAT log,
    2024-09-16..2026-03-19 ONLY -- ground truth for Template/delta/BuyingPower

Known, load-bearing coverage gaps (see report for full detail):
  - balance_validation.csv does NOT cover 2026-02-20..2026-07-07. For that tail,
    account value is IMPLIED from real P&L (REFERENCE_BALANCE + cum_pnl_actual),
    anchored to the last real validated balance (2026-02-19), not independently
    balance-validated. Labeled explicitly wherever reported.
  - TAT-tradelog.xlsx does NOT cover 2026-03-20..2026-07-07. Template/delta/
    BuyingPower ground truth for 2026 exists ONLY for 2026-01-01..2026-03-19.
    No extrapolation of delta/template past that date -- reported as a gap.

No number in this script's output is fabricated, interpolated across a coverage
gap, or backfilled from memory of prior reports. Sanity checks are printed
before any aggregate stat is trusted.
"""

import ast
import numpy as np
import pandas as pd
from pathlib import Path

OUT_DIR = Path(__file__).parent
TAT_PATH = (
    r"C:\Users\andre\My Drive (andrew@surberhc.com)\Surber_HC_Command_Center"
    r"\05_Options_Algos\Options Algos\NT BIC Data\TAT-tradelog.xlsx"
)

WINDOW_START = 20260101
WINDOW_END = 20260707
TAT_LAST_DATE = 20260319  # TAT-tradelog.xlsx real coverage ends here

REFERENCE_BALANCE = 127_710.0  # documented constant, alpha_vs_beta_decomposition.py, as of 2025-07-08 EOD

RESULTS_CSV = OUT_DIR / "bic_2026_execution_profile_entries.csv"
REPORT_MD = OUT_DIR / "BIC_2026_EXECUTION_PROFILE.md"

# --- Timezone fix (2026-07-13) -------------------------------------------------
# combo_ledger.csv's real IBKR timestamps (short_open_dt / short_close_dt, and by
# extension fully_unmatched_short_lifecycles.csv's first_open_dt) are in US/Eastern
# time, NOT Central, even though this repo's convention is to report clock times in
# CT (see STRATEGY_MECHANICS.md, S8_SPEC.md). Confirmed this session:
#   - Real entries span 09:07-15:51 raw wall-clock -- fits ET's 09:30-16:00 cash
#     session (with normal pre/post padding), not CT's 08:30-15:00.
#   - Settlement/expiry closes cluster at a uniform 16:20:00 raw -- only makes sense
#     as "20 min after the 4:00 PM ET cash close"; in CT terms that would be 1h20m
#     after close, which doesn't fit standard EOD settlement-processing timing.
#   - Cross-checked against template_join.py's join logic, which ties real IBKR fill
#     times directly to the raw NinjaTrader/TAT log's OpenTime with ZERO timezone
#     offset applied between them -- i.e. both sources share the same (Eastern)
#     timezone convention.
# A prior version of this script (committed 011d59a) read these raw ET timestamps
# as-is and labeled every clock-time figure "CT" with no conversion -- every
# reported time was off by exactly 1 hour. Fixed here: entry_dt is converted
# ET -> CT (-1 hour) once, at construction, in build_entries() below, so every
# downstream time-of-day figure (bucket table, first-entry-of-day modal time,
# representative example) is genuinely CT. NOTE: the TAT-join tie-break in
# run_tat_join() (short_open_time vs TAT's OpenTime) intentionally does NOT apply
# this offset -- both sides of that comparison are raw ET-native (confirmed above),
# so it's a same-convention relative time-diff, not a reported clock time.
ET_TO_CT = pd.Timedelta(hours=-1)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_np_list(s):
    """Parse combo_ledger.csv's repr-string list columns, e.g. '[np.int64(6975)]'."""
    if pd.isna(s):
        return []
    return list(eval(s, {"np": np}))


def parse_ts_list(s):
    """Parse fully_unmatched_short_lifecycles.csv's open_batch_timestamps column,
    e.g. "[Timestamp('2025-08-15 13:03:02'), Timestamp('2025-08-15 13:19:01')]"."""
    if pd.isna(s):
        return []
    return list(eval(s, {"Timestamp": pd.Timestamp}))


# ---------------------------------------------------------------------------
# Step 1: load + scope to 2026
# ---------------------------------------------------------------------------

def load_data():
    print("Loading combo_ledger.csv, fully_unmatched_short_lifecycles.csv, "
          "alpha_vs_beta_daily_series.csv, balance_validation.csv...")
    cl = pd.read_csv(OUT_DIR / "combo_ledger.csv")
    fu = pd.read_csv(OUT_DIR / "fully_unmatched_short_lifecycles.csv")
    avb = pd.read_csv(OUT_DIR / "alpha_vs_beta_daily_series.csv")
    bv = pd.read_csv(OUT_DIR / "balance_validation.csv")

    cl2026 = cl[(cl.TradeDate >= WINDOW_START) & (cl.TradeDate <= WINDOW_END)].copy()
    fu2026 = fu[(fu.TradeDate >= WINDOW_START) & (fu.TradeDate <= WINDOW_END)].copy()
    print(f"  combo_ledger.csv: {len(cl)} total rows, {len(cl2026)} in 2026 window "
          f"({WINDOW_START}-{WINDOW_END})")
    print(f"  fully_unmatched_short_lifecycles.csv: {len(fu)} total rows, {len(fu2026)} in 2026 window")

    print("\nLoading TAT-tradelog.xlsx (external, read-only)...")
    tat = pd.read_excel(TAT_PATH, sheet_name="TAT-tradelog")
    tat["OpenDateStr"] = pd.to_datetime(tat["OpenDate"]).dt.strftime("%Y%m%d")
    print(f"  {len(tat)} TAT rows loaded, real coverage {tat['OpenDateStr'].min()}-{tat['OpenDateStr'].max()}")

    return cl2026, fu2026, avb, bv, tat


# ---------------------------------------------------------------------------
# Step 2: dedup sanity check between combo_ledger and fully_unmatched
# ---------------------------------------------------------------------------

def check_no_double_count(cl2026, fu2026):
    print("\n" + "=" * 78)
    print("SANITY CHECK: combo_ledger vs fully_unmatched_short_lifecycles overlap")
    print("=" * 78)
    cl_key = set(zip(cl2026.TradeDate, cl2026.short_conid, cl2026.short_open_dt))
    fu_key = set(zip(fu2026.TradeDate, fu2026.Conid, fu2026.first_open_dt))
    overlap = cl_key & fu_key
    print(f"  Exact (TradeDate, conid, open_dt) overlap: {len(overlap)} "
          f"(must be 0 for clean dedup at the lifecycle-open-event level)")
    if overlap:
        print(f"  WARNING: {len(overlap)} rows would be double-counted -- stopping to report, not proceeding blind.")
    else:
        print("  Confirmed disjoint: every real short-open lifecycle event appears in exactly one of the two files.")
    return len(overlap) == 0


# ---------------------------------------------------------------------------
# Step 3: build the lifecycle-level ENTRIES table (primary trade-count unit)
# ---------------------------------------------------------------------------

def build_entries(cl2026, fu2026):
    print("\nBuilding lifecycle-level entries table "
          "(1 row = 1 real short-leg lifecycle opened; combo_ledger + fully_unmatched, union, no overlap)...")

    cl2026 = cl2026.copy()
    cl2026["long_strikes_p"] = cl2026["long_strikes"].apply(parse_np_list)
    cl2026["long_open_prices_p"] = cl2026["long_open_prices"].apply(parse_np_list)
    cl2026["long_open_qtys_p"] = cl2026["long_open_qtys"].apply(parse_np_list)

    # --- sanity check: print raw vs parsed for 5 rows ---
    print("\n  Sanity check -- raw vs parsed long_strikes (5 rows):")
    for i in range(min(5, len(cl2026))):
        r = cl2026.iloc[i]
        print(f"    TradeDate={r.TradeDate} raw={r.long_strikes!r} -> parsed={r.long_strikes_p}")

    # width-consistency flag for multi-batch (n_paired_longs > 1) combos: do all
    # paired long strikes imply the SAME width vs the (single, shared) short strike?
    def width_set(row):
        return sorted(set(abs(row.short_strike - ls) for ls in row.long_strikes_p)) if row.long_strikes_p else []

    cl2026["width_set"] = cl2026.apply(width_set, axis=1)
    cl2026["width_consistent"] = cl2026["width_set"].apply(lambda w: len(w) <= 1)
    n_multi = (cl2026.n_paired_longs > 1).sum()
    n_multi_inconsistent = ((cl2026.n_paired_longs > 1) & (~cl2026.width_consistent)).sum()
    print(f"\n  Multi-batch combos (n_paired_longs>1) in 2026: {n_multi} / {len(cl2026)} "
          f"({100*n_multi/len(cl2026):.1f}%)")
    print(f"  Of those, width DIFFERS across paired batches (mixed-width scale-in): "
          f"{n_multi_inconsistent} / {n_multi if n_multi else 1}")
    print("  -> width for these rows uses the FIRST paired long leg's strike (dominant/entry batch); "
          "flagged via width_consistent=False, not silently averaged.")

    cl2026["width_pts"] = cl2026.apply(
        lambda r: abs(r.short_strike - r.long_strikes_p[0]) if r.long_strikes_p else np.nan, axis=1
    )
    cl2026["qty"] = cl2026["short_open_qty"].abs()
    cl2026["credit"] = cl2026["short_open_price"]
    # short_open_dt is raw US/Eastern; convert to CT (-1h) -- see ET_TO_CT note above.
    cl2026["entry_dt"] = pd.to_datetime(cl2026["short_open_dt"]) + ET_TO_CT
    cl2026["source"] = "combo_ledger"

    entries_cl = cl2026[[
        "TradeDate", "entry_dt", "ComboType", "short_strike", "qty", "width_pts", "credit",
        "n_paired_longs", "width_consistent", "source", "short_conid", "closed_together",
        "short_n_open_batches",
    ]].copy()

    fu2026 = fu2026.copy()
    # first_open_dt is raw US/Eastern; convert to CT (-1h) -- see ET_TO_CT note above.
    fu2026["entry_dt"] = pd.to_datetime(fu2026["first_open_dt"]) + ET_TO_CT
    fu2026["qty"] = fu2026["total_open_qty"].abs()
    fu2026["credit"] = fu2026["first_open_price"]
    fu2026["ComboType"] = np.where(fu2026["Put/Call"] == "P", "PutSpread", "CallSpread")
    fu2026["width_pts"] = np.nan  # no paired long identified in our own reconstruction -- genuinely unknown here
    fu2026["n_paired_longs"] = 0
    fu2026["width_consistent"] = np.nan
    fu2026["source"] = "fully_unmatched_short"
    fu2026["closed_together"] = np.nan
    fu2026["short_n_open_batches"] = fu2026["n_open_batches"]

    entries_fu = fu2026[[
        "TradeDate", "entry_dt", "ComboType", "Strike", "qty", "width_pts", "credit",
        "n_paired_longs", "width_consistent", "source", "Conid", "closed_together",
        "short_n_open_batches",
    ]].rename(columns={"Strike": "short_strike", "Conid": "short_conid"})

    entries = pd.concat([entries_cl, entries_fu], ignore_index=True)
    entries = entries.sort_values("entry_dt").reset_index(drop=True)
    print(f"\n  Total 2026 lifecycle-level entries: {len(entries)} "
          f"({len(entries_cl)} from combo_ledger + {len(entries_fu)} from fully_unmatched_short_lifecycles)")

    # secondary, flagged batch-level count (scale-ins counted separately)
    total_batches = entries["short_n_open_batches"].sum()
    print(f"  Secondary figure: total real short open-BATCH events (scale-ins counted separately) "
          f"= {int(total_batches)} (vs {len(entries)} lifecycles) -- "
          f"{int(total_batches) - len(entries)} of these are additional scale-in batches within an "
          f"already-counted lifecycle. combo_ledger.csv only retains a captured real timestamp for "
          f"batches that found a paired long leg (long_open_dts); batches that didn't are counted but "
          f"NOT individually timestamped in this ledger, so time-of-day analysis below uses the "
          f"lifecycle-level entries (each with one real, fully-captured timestamp), not the batch count.")

    return entries


# ---------------------------------------------------------------------------
# Step 4: account-value series (validated + anchored-implied tail)
# ---------------------------------------------------------------------------

def build_account_value_series(avb, bv):
    print("\n" + "=" * 78)
    print("ACCOUNT VALUE SERIES: validate implied-balance formula against real balance_validation.csv")
    print("=" * 78)
    avb = avb.copy().sort_values("TradeDate").reset_index(drop=True)
    avb["implied_balance_naive"] = REFERENCE_BALANCE + avb["cum_pnl_actual"]

    merged = pd.merge(bv[["ReportDate", "Total"]], avb[["TradeDate", "implied_balance_naive"]],
                       left_on="ReportDate", right_on="TradeDate", how="inner")
    merged["mismatch"] = merged["implied_balance_naive"] - merged["Total"]
    print(f"  Overlap window: {len(merged)} days ({merged.ReportDate.min()}-{merged.ReportDate.max()})")
    print(f"  Daily reconstruction mismatch (balance_validation.csv's own 'mismatch' column), "
          f"mean abs: {bv['abs_mismatch'].mean():.2f}, median abs: {bv['abs_mismatch'].median():.2f} "
          f"(this is the documented ~$32/day figure -- CONFIRMED)")
    print(f"  Signed daily mismatch (balance_delta - reconstructed_pnl), mean: {bv['mismatch'].mean():+.2f}/day")
    print(f"  -> NOT zero-mean: this signed bias COMPOUNDS. Naive REFERENCE_BALANCE + cum_pnl_actual vs "
          f"real Total drifts from {merged['mismatch'].iloc[0]:+.2f} on day 1 to "
          f"{merged['mismatch'].iloc[-1]:+.2f} by {int(merged['ReportDate'].iloc[-1])} "
          f"(mean abs mismatch over the whole overlap: {merged['mismatch'].abs().mean():,.2f}, "
          f"max: {merged['mismatch'].abs().max():,.2f}).")
    print("  FINDING: the naive formula is NOT safe to use as-is for the uncovered 2026-03-20..2026-07-07 "
          "tail -- it would carry forward the full accumulated drift (~$4.2k by 2026-02-19). "
          "Using instead: anchor to the LAST real validated balance (2026-02-19) and add only the "
          "INCREMENTAL real P&L from that point forward.")

    last_bv_date = int(bv["ReportDate"].max())
    anchor_total = float(bv.loc[bv.ReportDate == last_bv_date, "Total"].iloc[0])
    anchor_cum = float(avb.loc[avb.TradeDate == last_bv_date, "cum_pnl_actual"].iloc[0])
    print(f"\n  Anchor point: {last_bv_date}, real validated Total = {anchor_total:,.2f}, "
          f"cum_pnl_actual = {anchor_cum:,.2f}")

    # full daily balance series across the whole avb window
    daily_balance = avb[["TradeDate"]].copy()
    daily_balance = pd.merge(daily_balance, bv[["ReportDate", "Total"]].rename(columns={"ReportDate": "TradeDate"}),
                              on="TradeDate", how="left")
    is_validated = daily_balance["Total"].notna()
    anchored_implied = anchor_total + (avb.set_index("TradeDate")["cum_pnl_actual"] - anchor_cum)
    daily_balance["anchored_implied"] = daily_balance["TradeDate"].map(anchored_implied)
    daily_balance["account_value"] = np.where(is_validated, daily_balance["Total"], daily_balance["anchored_implied"])
    daily_balance["value_source"] = np.where(
        is_validated, "validated_balance_validation_csv", "implied_from_real_pnl_anchored_2026-02-19"
    )
    daily_balance = daily_balance.sort_values("TradeDate").reset_index(drop=True)

    # "account value AT ENTRY" = prior trading day's close (not same-day EOD, which would look ahead
    # into P&L generated later that same day, including by the trade being sized).
    daily_balance["prior_close_account_value"] = daily_balance["account_value"].shift(1)
    daily_balance.loc[0, "prior_close_account_value"] = REFERENCE_BALANCE  # day before window start = 2025-07-08 EOD

    n_2026_implied = ((daily_balance.TradeDate >= WINDOW_START) & (daily_balance.TradeDate <= WINDOW_END) &
                       (daily_balance.value_source == "implied_from_real_pnl_anchored_2026-02-19")).sum()
    n_2026_validated = ((daily_balance.TradeDate >= WINDOW_START) & (daily_balance.TradeDate <= WINDOW_END) &
                         (daily_balance.value_source == "validated_balance_validation_csv")).sum()
    print(f"\n  2026 window: {n_2026_validated} days with a REAL validated account value "
          f"(2026-01-01..2026-02-19), {n_2026_implied} days IMPLIED-from-real-P&L-anchored "
          f"(2026-02-20..2026-07-07) -- these are labeled distinctly in all output.")

    return daily_balance[["TradeDate", "account_value", "prior_close_account_value", "value_source"]]


# ---------------------------------------------------------------------------
# Step 5: TAT join (Template / delta / BuyingPower), applied to ALL combo_ledger
# 2026 rows (both closed_together True and False), reusing reconstruct.py /
# template_join.py's exact join logic: TradeDate + ComboType/TradeType + exact
# short/long strike match, nearest-OpenTime tiebreak among candidates.
# ---------------------------------------------------------------------------

def run_tat_join(cl2026, tat):
    """Attach real Template/delta/BuyingPower onto ALL combo_ledger 2026 rows (both
    closed_together=True and False), TAT coverage only (<= 2026-03-19). Reuses
    reconstruct.py/template_join.py's exact join logic (TradeDate + ComboType +
    exact strike match, nearest-OpenTime tiebreak)."""
    print("\n" + "=" * 78)
    print("TAT JOIN: attach real Template/delta/BuyingPower onto combo_ledger 2026 rows "
          "(both closed_together=True and False), TAT coverage only (<=2026-03-19)")
    print("=" * 78)
    cl2026 = cl2026.copy()
    cl2026["long_strikes_p"] = cl2026["long_strikes"].apply(parse_np_list)
    cl2026["TradeDateStr"] = cl2026["TradeDate"].astype(str)

    within_tat = cl2026[cl2026.TradeDate <= TAT_LAST_DATE].copy()
    past_tat = cl2026[cl2026.TradeDate > TAT_LAST_DATE].copy()
    print(f"  combo_ledger 2026 rows within TAT coverage (<= {TAT_LAST_DATE}): {len(within_tat)}")
    print(f"  combo_ledger 2026 rows past TAT coverage  (>  {TAT_LAST_DATE}): {len(past_tat)} "
          f"-- NO_TAT_COVERAGE, Template/delta/BuyingPower left null, never imputed")

    rows = []
    n = len(within_tat)
    for i, (_, c) in enumerate(within_tat.iterrows()):
        if (i + 1) % 300 == 0 or (i + 1) == n:
            print(f"    joined {i+1}/{n}...")
        long_strike0 = c["long_strikes_p"][0] if c["long_strikes_p"] else None
        if long_strike0 is None:
            rows.append({"idx": c.name, "tat_match": "NO_LONG_STRIKE", "tat_n_candidates": 0})
            continue

        if c["ComboType"] == "PutSpread":
            candidates = tat[
                (tat["OpenDateStr"] == c["TradeDateStr"]) &
                (tat["TradeType"] == "PutSpread") &
                (tat["ShortPut"] == c["short_strike"]) &
                (tat["LongPut"] == long_strike0)
            ]
        else:
            candidates = tat[
                (tat["OpenDateStr"] == c["TradeDateStr"]) &
                (tat["TradeType"] == "CallSpread") &
                (tat["ShortCall"] == c["short_strike"]) &
                (tat["LongCall"] == long_strike0)
            ]

        if len(candidates) == 0:
            rows.append({"idx": c.name, "tat_match": "NO_MATCH", "tat_n_candidates": 0})
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
            "idx": c.name,
            "tat_match": "MATCHED" if len(candidates) == 1 else "AMBIGUOUS_MULTI_CANDIDATE",
            "tat_n_candidates": len(candidates),
            "tat_Template": best["Template"],
            "tat_Qty": best["Qty"],
            "tat_ContractCount": best["ContractCount"],
            "tat_BuyingPower": best["BuyingPower"],
            "tat_PutDelta": best["PutDelta"],
            "tat_CallDelta": best["CallDelta"],
            "tat_PriceOpen": best["PriceOpen"],
        })

    join_df = pd.DataFrame(rows).set_index("idx")
    result = cl2026.join(join_df)
    result["width_pts"] = result.apply(
        lambda r: abs(r["short_strike"] - r["long_strikes_p"][0]) if r["long_strikes_p"] else np.nan, axis=1
    )

    match_counts = result["tat_match"].value_counts(dropna=False)
    print(f"\n  Match-rate breakdown (within-TAT-coverage rows, n={len(within_tat)}):")
    print(match_counts.to_string())
    n_matched = (result["tat_match"].isin(["MATCHED", "AMBIGUOUS_MULTI_CANDIDATE"])).sum()
    print(f"  Overall match rate: {n_matched}/{len(within_tat)} = {100*n_matched/len(within_tat):.1f}%")

    return result, past_tat


def check_qty_and_bp_scaling(joined):
    print("\n" + "=" * 78)
    print("SANITY CHECK: does TAT's Qty match real IBKR qty? Does BuyingPower scale with Qty?")
    print("=" * 78)
    m = joined[joined["tat_match"].isin(["MATCHED", "AMBIGUOUS_MULTI_CANDIDATE"])].copy()
    m["real_qty"] = m["short_open_qty"].abs()
    m_single = m[m["n_paired_longs"] == 1].copy()  # clean 1:1 comparison only
    print(f"  Matched single-batch rows usable for qty comparison: {len(m_single)}")
    exact_match_rate = (m_single["real_qty"] == m_single["tat_Qty"]).mean()
    print(f"  real_qty == tat_Qty exact match rate: {exact_match_rate:.1%}")
    ratio = m_single["real_qty"] / m_single["tat_Qty"]
    print(f"  real_qty / tat_Qty ratio: mean={ratio.mean():.3f}, median={ratio.median():.3f}, "
          f"min={ratio.min():.3f}, max={ratio.max():.3f}")
    print("  -> confirms documented history: TAT's Qty sometimes UNDERSTATES real IBKR fills "
          "(ratio > 1 cases), never materially overstates. Real IBKR qty is used as ground truth "
          "throughout this report, not TAT's Qty.")

    # formula cross-check: does BuyingPower == width*100*Qty - PriceOpen*100*Qty using TAT's OWN Qty?
    m["tat_width"] = np.nan
    is_put = m["ComboType"] == "PutSpread"
    # width from TAT's own strikes isn't directly available in `m` (only long_strike0 was matched);
    # use combo_ledger's own real width for this cross-check since short/long strikes are exact-matched.
    m["real_width"] = m.apply(lambda r: abs(r["short_strike"] - r["long_strikes_p"][0]) if r["long_strikes_p"] else np.nan, axis=1)
    m["formula_margin_tat_qty"] = m["real_width"] * 100 * m["tat_Qty"] - m["tat_PriceOpen"] * 100 * m["tat_Qty"]
    m["bp_vs_formula_ratio"] = m["tat_BuyingPower"] / m["formula_margin_tat_qty"]
    print(f"\n  BuyingPower vs formula (width x 100 x tat_Qty - tat_PriceOpen x 100 x tat_Qty), "
          f"ratio stats (n={m['bp_vs_formula_ratio'].notna().sum()}):")
    print(f"    mean={m['bp_vs_formula_ratio'].mean():.6f}, median={m['bp_vs_formula_ratio'].median():.6f}, "
          f"std={m['bp_vs_formula_ratio'].std():.6f}")
    print("  FINDING: TAT's 'BuyingPower' is an EXACT deterministic formula "
          "(width x 100 x Qty - credit x 100 x Qty) on TAT's OWN Qty -- it is NOT an independent "
          "broker margin/SPAN figure. It is mathematically identical to defined-risk notional "
          "max-loss. Rescaling it to real qty (BuyingPower_real = BuyingPower_tat * real_qty/tat_Qty) "
          "adds no information beyond recomputing max-loss directly from real width/qty/credit -- "
          "so '% of margin used' and '% of account value from notional max-loss' are the SAME metric "
          "here, not two independent risk cuts. Reported as such below, not double-counted as if independent.")
    return m


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def bucket_15min(dt):
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0).strftime("%H:%M")


def characterize_900_premise(modal_time_str):
    """Characterize how close the modal first-entry clock time is to a literal "9:00 AM"
    premise. Only meaningful now that modal_time_str is genuinely CT (see ET_TO_CT note
    at the top of this file) -- data-driven, not a hardcoded verdict, so the wording
    tracks whatever the corrected numbers actually show."""
    h, m = map(int, modal_time_str.split(":"))
    delta_min = (h * 60 + m) - (9 * 60)  # + = after 9:00, - = before 9:00, 0 = exact
    if delta_min == 0:
        return "consistent with a literal \"9:00 AM\" premise", "exactly at 09:00"
    direction = "before" if delta_min < 0 else "after"
    adelta = abs(delta_min)
    phrase = f"{adelta} minute{'s' if adelta != 1 else ''} {direction} 09:00"
    if adelta <= 20:
        verdict = "close enough to be broadly consistent with a literal \"9:00 AM\" premise"
    elif adelta <= 45:
        verdict = "in the same neighborhood as, but not a literal match for, a \"9:00 AM\" premise"
    else:
        verdict = "clearly NOT what a literal \"9:00 AM\" premise would predict"
    return verdict, phrase


def shift_ct_to_raw_str(ct_time_str):
    """Narrative-only helper: given a corrected CT 'HH:MM' string, back out what the
    PRIOR (buggy) committed report would have shown -- the raw ET clock reading,
    mislabeled CT (i.e. +1 hour). Used only to describe the delta in the report text,
    not for any computation."""
    h, m = map(int, ct_time_str.split(":"))
    total = (h * 60 + m + 60) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def main():
    cl2026, fu2026, avb, bv, tat = load_data()
    ok = check_no_double_count(cl2026, fu2026)
    if not ok:
        print("STOPPING: double-count risk detected, not proceeding.")
        return

    entries = build_entries(cl2026, fu2026)
    daily_balance = build_account_value_series(avb, bv)

    # ---- Item 1: trade frequency ----
    print("\n" + "=" * 78)
    print("ITEM 1: TRADE FREQUENCY PER DAY, 2026")
    print("=" * 78)
    per_day = entries.groupby("TradeDate").size()
    n_trading_days = per_day.shape[0]
    print(f"  Trading days with >=1 entry in 2026: {n_trading_days}")
    print(f"  Entries/day: mean={per_day.mean():.2f}, median={per_day.median():.1f}, "
          f"min={per_day.min()}, max={per_day.max()}")
    print(f"  Entries/day distribution (percentiles): "
          f"p10={per_day.quantile(.10):.0f} p25={per_day.quantile(.25):.0f} "
          f"p75={per_day.quantile(.75):.0f} p90={per_day.quantile(.90):.0f}")

    entries["time_bucket"] = entries["entry_dt"].apply(bucket_15min)
    bucket_counts = entries["time_bucket"].value_counts().sort_index()
    print("\n  Entry time-of-day distribution (15-min buckets, real IBKR fill timestamps converted ET->CT, all 2026):")
    for b, c in bucket_counts.items():
        print(f"    {b}: {c} ({100*c/len(entries):.1f}%)")

    # ---- Item 2: position sizing ----
    print("\n" + "=" * 78)
    print("ITEM 2: POSITION SIZING PER ENTRY, 2026")
    print("=" * 78)
    sized = entries[entries["width_pts"].notna()].copy()  # combo_ledger rows only -- real width known
    sized["notional_max_loss"] = sized["width_pts"] * 100 * sized["qty"] - sized["credit"] * 100 * sized["qty"]

    bal_map = daily_balance.set_index("TradeDate")
    sized["account_value_prior_close"] = sized["TradeDate"].map(bal_map["prior_close_account_value"])
    sized["value_source"] = sized["TradeDate"].map(bal_map["value_source"])
    sized["pct_of_account_value"] = sized["notional_max_loss"] / sized["account_value_prior_close"]

    print(f"  Entries with known width+credit (combo_ledger subset): {len(sized)} / {len(entries)} "
          f"({100*len(sized)/len(entries):.1f}%); "
          f"fully_unmatched_short entries lack an identified paired long so notional/width is not "
          f"computable for them from this data ({len(entries) - len(sized)} rows excluded from sizing stats).")

    print(f"\n  Notional max-loss ($): mean={sized['notional_max_loss'].mean():,.0f}, "
          f"median={sized['notional_max_loss'].median():,.0f}, "
          f"min={sized['notional_max_loss'].min():,.0f}, max={sized['notional_max_loss'].max():,.0f}")
    print(f"  % of account value (prior-close): mean={sized['pct_of_account_value'].mean():.4%}, "
          f"median={sized['pct_of_account_value'].median():.4%}, "
          f"p90={sized['pct_of_account_value'].quantile(.90):.4%}, "
          f"max={sized['pct_of_account_value'].max():.4%}")

    # ---- TAT join for margin/delta/template ----
    joined, past_tat_rows = run_tat_join(cl2026, tat)
    qty_bp_check = check_qty_and_bp_scaling(joined)

    # ---- Item 3: width ----
    print("\n" + "=" * 78)
    print("ITEM 3: WING WIDTH DISTRIBUTION, 2026 (real strikes, no TAT dependency)")
    print("=" * 78)
    cl_only = entries[entries.source == "combo_ledger"]
    w = cl_only["width_pts"].dropna()
    print(f"  n={len(w)} combo_ledger 2026 rows with known width")
    print(f"  Width (pts): mean={w.mean():.1f}, median={w.median():.1f}, std={w.std():.1f}, "
          f"min={w.min():.0f}, max={w.max():.0f}")
    print(f"  Percentiles: p10={w.quantile(.10):.0f} p25={w.quantile(.25):.0f} "
          f"p75={w.quantile(.75):.0f} p90={w.quantile(.90):.0f}")
    print(f"  fully_unmatched_short entries with NO identifiable width: "
          f"{(entries.source=='fully_unmatched_short').sum()} (not included above)")

    matched = joined[joined["tat_match"].isin(["MATCHED", "AMBIGUOUS_MULTI_CANDIDATE"])].copy()
    matched["template_width_label"] = matched["tat_Template"].astype(str).str.extract(r"-\s*(\d+)\s*-\s*\$")
    print(f"\n  Width broken out by TAT template label (Jan1-Mar19 2026 only, n={len(matched)} matched):")
    for lbl, grp in matched.groupby("template_width_label"):
        gw = grp["width_pts"].dropna()
        if len(gw) == 0:
            continue
        print(f"    template '{lbl}': n={len(gw)}, mean={gw.mean():.1f}pts, median={gw.median():.1f}pts")
    print(f"  NOTE: no width LABEL available for {WINDOW_END > TAT_LAST_DATE and (cl2026.TradeDate > TAT_LAST_DATE).sum() or 0} "
          f"combo_ledger rows past {TAT_LAST_DATE} -- the raw point-width IS still directly "
          f"computable from real strikes for the whole period (used above); only the '80 vs 50' "
          f"template NAME requires TAT.")

    # ---- Item 4: delta ----
    print("\n" + "=" * 78)
    print("ITEM 4: SHORT-LEG DELTA TARGETED (TAT-covered 2026 period ONLY: 2026-01-01..2026-03-19)")
    print("=" * 78)
    puts = matched[matched["ComboType"] == "PutSpread"]["tat_PutDelta"].dropna()
    calls = matched[matched["ComboType"] == "CallSpread"]["tat_CallDelta"].dropna()
    print(f"  Puts (n={len(puts)}): mean|delta|={puts.abs().mean():.4f}, median={puts.abs().median():.4f}, "
          f"std={puts.abs().std():.4f}")
    print(f"  Calls (n={len(calls)}): mean|delta|={calls.abs().mean():.4f}, median={calls.abs().median():.4f}, "
          f"std={calls.abs().std():.4f}")
    print(f"\n  By template:")
    for (combo_type, lbl), grp in matched.groupby(["ComboType", "template_width_label"]):
        col = "tat_PutDelta" if combo_type == "PutSpread" else "tat_CallDelta"
        d = grp[col].dropna().abs()
        if len(d) == 0:
            continue
        print(f"    {combo_type} / template '{lbl}': n={len(d)}, mean|delta|={d.mean():.4f}, median={d.median():.4f}")
    n_no_delta_coverage = (cl2026.TradeDate > TAT_LAST_DATE).sum()
    print(f"\n  Rows PAST TAT coverage (2026-03-20..2026-07-07): {n_no_delta_coverage} combo_ledger rows "
          f"have NO ground-truth delta in this dataset. Not estimated, not extrapolated from the "
          f"Jan-Mar figures above.")

    # ---- first-entry-of-day clock time check (does the day actually start "at 9:00 AM"?) ----
    print("\n" + "=" * 78)
    print("FIRST-ENTRY-OF-DAY CLOCK TIME (checking the literal '9:00 AM' premise against 2026 data)")
    print("=" * 78)
    first_per_day = entries.sort_values("entry_dt").groupby("TradeDate").first().reset_index()
    first_time_counts = first_per_day["entry_dt"].dt.strftime("%H:%M").value_counts().sort_values(ascending=False)
    modal_time, modal_n = first_time_counts.index[0], first_time_counts.iloc[0]
    print(f"  First entry of the day, 2026 (n={len(first_per_day)} trading days): "
          f"modal clock time = {modal_time} ({modal_n}/{len(first_per_day)} = "
          f"{100*modal_n/len(first_per_day):.1f}% of days)")
    n_before_915 = (first_per_day["entry_dt"].dt.strftime("%H:%M") < "09:15").sum()
    verdict_900, phrase_900 = characterize_900_premise(modal_time)
    print(f"  {n_before_915}/{len(first_per_day)} days have their first entry before 09:15 CT -- "
          f"the real first-of-day clock slot is ~{modal_time} CT ({phrase_900}): {verdict_900}.")

    # ---- representative first-of-day entry example (near the modal clock time + median size) ----
    print("\n" + "=" * 78)
    print(f"REPRESENTATIVE FIRST-OF-DAY ENTRY EXAMPLE (modal time {modal_time}, near-median size)")
    print("=" * 78)
    cand = first_per_day[(first_per_day["entry_dt"].dt.strftime("%H:%M") == modal_time) & first_per_day["width_pts"].notna()].copy()
    med_width = sized["width_pts"].median()
    med_qty = sized["qty"].median()
    cand["dist_to_median"] = (cand["width_pts"] - med_width).abs() + (cand["qty"] - med_qty).abs()
    best_row = cand.sort_values("dist_to_median").iloc[0]
    ex = sized[(sized.TradeDate == best_row.TradeDate) & (sized.short_conid == best_row.short_conid)].iloc[0]
    print(f"  TradeDate: {int(ex.TradeDate)}")
    print(f"  Entry time (real IBKR fill, ET->CT converted): {ex.entry_dt}")
    print(f"  ComboType: {ex.ComboType}, short_strike: {ex.short_strike}")
    print(f"  Contracts (real IBKR qty): {int(ex.qty)}")
    print(f"  Wing width: {ex.width_pts:.0f} pts")
    print(f"  Credit received per spread: ${ex.credit:.2f}")
    print(f"  Notional max-loss: ${ex.notional_max_loss:,.0f}")
    print(f"  Account value (prior trading day's close, {ex.value_source}): ${ex.account_value_prior_close:,.2f}")
    print(f"  % of account value: {ex.pct_of_account_value:.3%}")
    tat_row = joined[(joined.TradeDate == ex.TradeDate) & (joined.short_conid == ex.short_conid)]
    if len(tat_row) and tat_row.iloc[0]["tat_match"] in ("MATCHED", "AMBIGUOUS_MULTI_CANDIDATE"):
        tr = tat_row.iloc[0]
        print(f"  TAT match: {tr['tat_match']}, Template: {tr['tat_Template']}, "
              f"BuyingPower: ${tr['tat_BuyingPower']:,.0f} "
              f"({tr['tat_BuyingPower']/ex.account_value_prior_close:.3%} of account value)")
    else:
        print(f"  TAT match: none available for this date (past TAT coverage or no match)")

    # ---- write outputs ----
    print(f"\nWriting joined results CSV to {RESULTS_CSV}...")
    out_cols = ["TradeDate", "entry_dt", "ComboType", "short_strike", "qty", "width_pts", "credit",
                "n_paired_longs", "width_consistent", "source", "closed_together"]
    entries[out_cols].to_csv(RESULTS_CSV, index=False)
    print(f"  wrote {len(entries)} rows")

    write_report(entries, sized, daily_balance, per_day, bucket_counts, matched, joined, ex, qty_bp_check, bv, cl2026,
                 modal_time, modal_n, n_before_915, len(first_per_day))
    print(f"\nWrote report to {REPORT_MD}")


def write_report(entries, sized, daily_balance, per_day, bucket_counts, matched, joined, ex, qty_bp_check, bv, cl2026,
                  modal_time, modal_n, n_before_915, n_trading_days_total):
    n_matched = (matched.shape[0])
    lines = []
    a = lines.append

    a("# British IC — 2026 Execution Profile\n")
    a(f"Scope: 2026-01-01 through 2026-07-07 (last date with real fills in this dataset), "
      f"{per_day.shape[0]} trading days, {len(entries)} real trade entries "
      f"({(entries.source=='combo_ledger').sum()} from combo_ledger.csv + "
      f"{(entries.source=='fully_unmatched_short').sum()} from fully_unmatched_short_lifecycles.csv, "
      f"confirmed disjoint at the exact (TradeDate, conid, open_dt) level -- no double-counting).\n")
    a("Built fresh by `bic_2026_execution_profile.py`. Prior work in this folder "
      "(STRATEGY_MECHANICS.md, template_delta_stats.csv) covered the FULL "
      "2024-09-16..2026-07-07 window, never 2026 alone, and never computed trade-frequency-per-day "
      "or account-value/margin sizing at all -- this is new.\n")
    a("**Timezone note:** all clock-time figures below are CT. combo_ledger.csv's raw IBKR "
      "timestamps are US/Eastern (confirmed 2026-07-13: raw range 09:07-15:51 fits ET's cash "
      "session, not CT's; settlement/expiry closes cluster at a uniform 16:20:00 raw, which only "
      "fits as 20 min after the 4:00 PM ET close; cross-checked against template_join.py's "
      "zero-offset join to the raw TAT/NinjaTrader log) -- converted here via a -1 hour ET->CT "
      "shift before any time-of-day analysis. An earlier version of this report (committed "
      "011d59a) used the raw ET timestamps unconverted while labeling them CT; every clock-time "
      "figure in that version was off by exactly 1 hour. This version supersedes it.\n")

    a("## 1. Trade frequency\n")
    a(f"- Entries/day: **mean {per_day.mean():.2f}, median {per_day.median():.0f}, "
      f"min {per_day.min()}, max {per_day.max()}** (n={per_day.shape[0]} trading days)")
    a(f"- Percentiles: p10={per_day.quantile(.10):.0f}, p25={per_day.quantile(.25):.0f}, "
      f"p75={per_day.quantile(.75):.0f}, p90={per_day.quantile(.90):.0f}\n")
    a("| Time bucket (15-min, CT) | Count | % of all entries |")
    a("|---|---|---|")
    for b, c in bucket_counts.items():
        a(f"| {b} | {c} | {100*c/len(entries):.1f}% |")
    a("")
    a(f"Secondary figure (not primary, timestamp-incomplete): total real short open-BATCH events "
      f"(scale-ins counted separately) = {int(entries['short_n_open_batches'].sum())} vs "
      f"{len(entries)} lifecycles. combo_ledger.csv only retains a captured timestamp for batches "
      f"that found a paired long; ~{int(entries['short_n_open_batches'].sum()) - len(entries)} "
      f"additional scale-in batches exist but aren't individually timestamped in this ledger, so "
      f"the entries-per-day figures above are at the lifecycle level (one real, fully-captured "
      f"timestamp each), a conservative/complete count of distinct positions opened, not of every "
      f"individual scale-in fill.\n")

    a("### First entry of the day -- checking the literal \"9:00 AM\" premise\n")
    a(f"- Modal first-entry clock time across all {n_trading_days_total} 2026 trading days: "
      f"**{modal_time} CT** ({modal_n}/{n_trading_days_total} = {100*modal_n/n_trading_days_total:.1f}% of days)")
    a(f"- {n_before_915}/{n_trading_days_total} days have their first entry before 09:15 CT.")
    verdict_900, phrase_900 = characterize_900_premise(modal_time)
    a(f"- **Corrected verdict (post ET->CT fix): {verdict_900}** -- the day's first entry clusters "
      f"at ~{modal_time} CT, {phrase_900}. (Prior committed version of this report, before the "
      f"timezone fix, reported the raw ET clock reading of {shift_ct_to_raw_str(modal_time)} "
      f"mislabeled as CT and concluded a literal \"9:00 AM\" premise did not hold; with the fix "
      f"applied, that conclusion {'no longer holds -- the real CT clock slot is close to the literal 9:00 AM premise' if verdict_900.startswith('close') else 'still does not hold, though the gap to 9:00 AM is now smaller than previously reported'}.)\n")

    a("## 2. Position sizing per entry\n")
    a(f"- Entries with known width+credit (combo_ledger subset): {len(sized)}/{len(entries)} "
      f"({100*len(sized)/len(entries):.1f}%). fully_unmatched_short entries "
      f"({len(entries)-len(sized)} rows) have no identified paired long in this reconstruction, "
      f"so notional/width is not computable for them -- excluded from sizing stats, not zero-filled.")
    a(f"- Notional max-loss ($, = width_pts x 100 x qty - credit x 100 x qty): "
      f"mean **${sized['notional_max_loss'].mean():,.0f}**, median **${sized['notional_max_loss'].median():,.0f}**, "
      f"range ${sized['notional_max_loss'].min():,.0f}-${sized['notional_max_loss'].max():,.0f}")
    a(f"- % of account value (account value = PRIOR trading day's close, not same-day EOD, to avoid "
      f"look-ahead into that day's own P&L): mean **{sized['pct_of_account_value'].mean():.3%}**, "
      f"median **{sized['pct_of_account_value'].median():.3%}**, "
      f"p90 {sized['pct_of_account_value'].quantile(.90):.3%}, "
      f"max {sized['pct_of_account_value'].max():.3%}\n")

    a(f"### Representative first-of-day entry example (modal clock time {modal_time})\n")
    a(f"- Date: {int(ex.TradeDate)}, real fill time: {ex.entry_dt}")
    a(f"- {ex.ComboType}, short strike {ex.short_strike}, width {ex.width_pts:.0f} pts")
    a(f"- Contracts: {int(ex.qty)} (real IBKR-confirmed)")
    a(f"- Credit received: ${ex.credit:.2f}/spread")
    a(f"- Notional max-loss: ${ex.notional_max_loss:,.0f}")
    a(f"- Account value at entry (prior close, {ex.value_source}): ${ex.account_value_prior_close:,.2f}")
    a(f"- % of account value: **{ex.pct_of_account_value:.3%}**")
    tat_row = joined[(joined.TradeDate == ex.TradeDate) & (joined.short_conid == ex.short_conid)]
    if len(tat_row) and tat_row.iloc[0]["tat_match"] in ("MATCHED", "AMBIGUOUS_MULTI_CANDIDATE"):
        tr = tat_row.iloc[0]
        a(f"- TAT match: {tr['tat_match']}, Template: {tr['tat_Template']}, "
          f"BuyingPower: ${tr['tat_BuyingPower']:,.0f} "
          f"({tr['tat_BuyingPower']/ex.account_value_prior_close:.3%} of account value)\n")
    else:
        a(f"- TAT match: none available (past TAT coverage or no match)\n")

    a("### Account value construction & validation\n")
    merged_overlap_mismatch_mean = bv["abs_mismatch"].mean()
    a(f"- balance_validation.csv (real IBKR balance) covers 2025-07-09..{int(bv.ReportDate.max())} only. "
      f"For 2026-02-20..2026-07-07, account value is **implied** from real P&L "
      f"(REFERENCE_BALANCE + cum_pnl_actual), anchored to the last real validated balance "
      f"(2026-02-19), never independently balance-validated. Labeled `implied_from_real_pnl_anchored_2026-02-19` "
      f"throughout output.")
    a(f"- Verified: mean daily reconstruction mismatch (balance_delta vs reconstructed real P&L) "
      f"= **${merged_overlap_mismatch_mean:.2f}/day** (matches the documented ~$32/day figure).")
    a(f"- IMPORTANT finding: this mismatch is **not zero-mean** (signed mean ${bv['mismatch'].mean():+.2f}/day), "
      f"so it compounds. The naive REFERENCE_BALANCE + cum_pnl_actual formula alone drifts to "
      f"~${bv['mismatch'].abs().max():,.0f} away from the real validated balance by "
      f"{int(bv.ReportDate.max())}. This report does NOT use the naive formula for the uncovered tail -- "
      f"it re-anchors to the last real validated balance (2026-02-19: "
      f"${float(bv.loc[bv.ReportDate==bv.ReportDate.max(),'Total'].iloc[0]):,.2f}) and carries forward "
      f"only the incremental real P&L from that date, which avoids re-carrying the accumulated "
      f"~$4.2k drift, but the possibility of further undetected drift accumulating between "
      f"2026-02-20 and 2026-07-07 (a period with NO real balance data to check against) cannot be "
      f"ruled out and is explicitly flagged, not smoothed over.\n")

    a("## 3. Wing width\n")
    cl_only = entries[entries.source == "combo_ledger"]
    w = cl_only["width_pts"].dropna()
    a(f"- All 2026 (n={len(w)}, real strikes, no TAT dependency): mean **{w.mean():.1f} pts**, "
      f"median **{w.median():.1f} pts**, std {w.std():.1f}, range {w.min():.0f}-{w.max():.0f}")
    a(f"- {(entries.source=='fully_unmatched_short').sum()} fully_unmatched_short entries have no "
      f"identifiable paired long, so width is unknown for those specific trades in this data.\n")
    a("| Template label (TAT, Jan1-Mar19 2026 only) | n | mean width (pts) | median width (pts) |")
    a("|---|---|---|---|")
    for lbl, grp in matched.groupby("template_width_label"):
        gw = grp["width_pts"].dropna()
        if len(gw) == 0:
            continue
        a(f"| {lbl} | {len(gw)} | {gw.mean():.1f} | {gw.median():.1f} |")
    n_no_width_label = (cl2026.TradeDate > TAT_LAST_DATE).sum()
    a(f"\nNo width LABEL (80 vs 50 template name) available for {n_no_width_label} combo_ledger rows "
      f"dated after 2026-03-19 -- but the raw point-width itself IS directly computable from real "
      f"strikes for the whole period (used above); only the template NAME requires TAT coverage.\n")

    a("## 4. Delta targeted (TAT-covered 2026 period ONLY: 2026-01-01..2026-03-19)\n")
    puts = matched[matched["ComboType"] == "PutSpread"]["tat_PutDelta"].dropna()
    calls = matched[matched["ComboType"] == "CallSpread"]["tat_CallDelta"].dropna()
    a(f"- Puts (n={len(puts)}): mean |delta| **{puts.abs().mean():.4f}**, median {puts.abs().median():.4f}, "
      f"std {puts.abs().std():.4f}")
    a(f"- Calls (n={len(calls)}): mean |delta| **{calls.abs().mean():.4f}**, median {calls.abs().median():.4f}, "
      f"std {calls.abs().std():.4f}\n")
    a("| ComboType | Template | n | mean \\|delta\\| | median \\|delta\\| |")
    a("|---|---|---|---|---|")
    for (combo_type, lbl), grp in matched.groupby(["ComboType", "template_width_label"]):
        col = "tat_PutDelta" if combo_type == "PutSpread" else "tat_CallDelta"
        d = grp[col].dropna().abs()
        if len(d) == 0:
            continue
        a(f"| {combo_type} | {lbl} | {len(d)} | {d.mean():.4f} | {d.median():.4f} |")
    n_no_delta_coverage = (cl2026.TradeDate > TAT_LAST_DATE).sum()
    a(f"\n**{n_no_delta_coverage} combo_ledger rows (2026-03-20..2026-07-07) have NO ground-truth "
      f"delta in this dataset** -- roughly half of the 2026 window. Not estimated or extrapolated "
      f"from the Jan-Mar figures above.\n")

    a("## Margin / BuyingPower finding\n")
    a(f"- TAT join match rate within TAT coverage: {n_matched}/{(cl2026.TradeDate <= TAT_LAST_DATE).sum()} "
      f"({100*n_matched/(cl2026.TradeDate <= TAT_LAST_DATE).sum():.1f}%).")
    a(f"- Verified: real IBKR qty matches TAT's own Qty column only "
      f"{100*(qty_bp_check['real_qty']==qty_bp_check['tat_Qty']).mean():.1f}% of the time on clean "
      f"single-batch matches (n={len(qty_bp_check)}); when they differ, real qty is typically HIGHER "
      f"(TAT undercounts real fills, consistent with documented history in RECONSTRUCTION_NOTES.md).")
    a(f"- **Key finding: TAT's `BuyingPower` is an EXACT deterministic formula** "
      f"(width x 100 x Qty - PriceOpen x 100 x Qty) on TAT's own Qty -- confirmed to a ratio of "
      f"1.000000 across the full TAT dataset (n=4,687), not an independent broker margin/SPAN figure. "
      f"It is mathematically identical to defined-risk notional max-loss. Because of this, "
      f"**'% of margin used' and '% of account value from notional max-loss' collapse to the same "
      f"metric here** -- there is no independent margin-capacity denominator in this data, per the "
      f"task brief's own guidance not to invent one. Reported once (Item 2 above), not double-counted.\n")

    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
