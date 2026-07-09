"""
Template-level B2-vs-actual analysis: fixed single template (80-$4) and a 6-config
grid search over {$2,$3,$4} x {50,80}, following STRATEGY_RECONSTRUCTION.md Part 2's
exact methodology (chronological 70/30 train/test split by date, dollar-weighted
P&L via `multiple * long_entry_cost`, leg-level + day-level win rate, single-largest-
day-removed robustness check) -- but now isolated PER TEMPLATE rather than blended.

Motivation: if a bot has to run ONE deterministic template (it can't replicate human
discretionary template-switching), which template, and is there real evidence for
that choice without curve-fitting the choice to the data being judged?

Inputs (all READ-ONLY):
  - tat_full_join.csv               (from template_join.py; TAT Template label +
                                      short_open_price $ proxy, joined onto every
                                      decoupled_long_legs.csv row)
  - longleg_rule_backtest_results.csv (B2_close_on_short_stop and actual_exit_multiple
                                      per leg, 1,584 of 1,617 legs -- the 98.0%-covered
                                      population with real 1-min SPXW OHLC backing)

Row alignment: longleg_rule_backtest_results.csv has NO explicit join key back to
decoupled_long_legs.csv (leg_id is just its own row-position). The correspondence is
recovered by SEQUENTIAL positional matching on (TradeDate, ComboType,
actual_exit_multiple == long_pnl_multiple to 1e-6) -- verified exactly reproducing
the known published totals in longleg_rule_summary_dollars.csv (train-with-crash
actual = +$14,221.46, train-no-crash B2 = +$12,463, test-with-crash B2 = +$83,489,
all reproduced to the cent) before trusting this script's own numbers.

Dollar conversion: dollar_pnl = multiple * long_entry_cost (same method as the
original Part 2 analysis; verified above).

Two population cuts, per the task brief:
  (a) "true-labeled through March 2026": final_width_label + final_dollar_label from
      a real TAT Template match (MATCHED or AMBIGUOUS_MULTI_CANDIDATE), TradeDate <=
      2026-03-19. Width label is NEVER available past this date.
  (b) "$-label-only proxy through July 2026": final_dollar_label from short_open_price
      banding alone (cutpoints $2.55 / $3.55, see template_join.py), full window
      through 2026-07-07. This mixes both width families under one $ label -- stated
      explicitly wherever it's used, never presented as width-confirmed.

Every cell recomputes its OWN 70/30 date split fresh (not reusing the blended
analysis's split-index #165), because each isolated subpopulation has its own date
range and day count.

Outputs:
  - template_fixed_grid_results.csv   (per-leg dollar P&L + labels + split assignment
                                        for the isolated population, for inspection)
  - printed tables (read into TEMPLATE_FIXED_AND_GRID_ANALYSIS.md, never fabricated)
"""

import pandas as pd
import numpy as np
from pathlib import Path
import sys

_BACKTESTER_ROOT = str((Path(__file__).parent.parent / "backtester").resolve())
if _BACKTESTER_ROOT not in sys.path:
    sys.path.insert(0, _BACKTESTER_ROOT)
from src import deflated_sharpe as ds  # noqa: E402

OUT_DIR = Path(__file__).parent
JOIN_PATH = OUT_DIR / "tat_full_join.csv"
RULE_RESULTS_PATH = OUT_DIR / "longleg_rule_backtest_results.csv"
OUT_RESULTS_PATH = OUT_DIR / "template_fixed_grid_results.csv"

TAT_LAST_DATE = 20260319
CRASH_DATE = 20251010

# Known published totals (longleg_rule_summary_dollars.csv) used to verify the
# row-alignment + dollar-conversion method before trusting any new number below.
KNOWN_CHECKS = {
    ('train_with_crash', 'actual'): 14221.460451,
    ('train_no_crash', 'B2'): 12463.0,
    ('test_with_crash', 'B2'): 83489.0,
}

MIN_LEGS_FOR_HEADLINE = 15
MIN_DAYS_FOR_HEADLINE = 10


def load_and_align():
    """
    Load tat_full_join.csv and longleg_rule_backtest_results.csv, recover the
    sequential positional correspondence between them, and merge B2/actual dollar
    P&L onto the joined (template-labeled) population. Verifies against known
    published totals before returning.
    """
    print("Loading tat_full_join.csv (template-labeled decoupled long legs)...")
    tj = pd.read_csv(JOIN_PATH)
    print(f"  {len(tj)} rows")

    print("Loading longleg_rule_backtest_results.csv (B2 vs actual, 1,584 rows)...")
    rb = pd.read_csv(RULE_RESULTS_PATH)
    print(f"  {len(rb)} rows")

    print("Recovering sequential positional correspondence "
          "(TradeDate + ComboType + multiple match)...")
    tj_sorted = tj.sort_values('dl_row').reset_index(drop=True)
    tj_idx = 0
    matched_dl_rows = []
    for _, r in rb.iterrows():
        while tj_idx < len(tj_sorted):
            d = tj_sorted.iloc[tj_idx]
            if (d['TradeDate'] == r['TradeDate'] and d['ComboType'] == r['ComboType']
                    and abs(d['long_pnl_multiple'] - r['actual_exit_multiple']) < 1e-6):
                matched_dl_rows.append(d['dl_row'])
                tj_idx += 1
                break
            else:
                tj_idx += 1
        else:
            matched_dl_rows.append(None)

    n_matched = sum(x is not None for x in matched_dl_rows)
    print(f"  matched {n_matched} / {len(rb)} rule-backtest rows to a decoupled_long_legs row")
    if n_matched != len(rb):
        print("  WARNING: not all rule-backtest rows matched -- investigate before trusting results",
              file=sys.stderr)

    rb = rb.copy()
    rb['dl_row'] = matched_dl_rows
    merged = rb.merge(
        tj_sorted[['dl_row', 'long_entry_cost', 'final_width_label', 'final_dollar_label',
                   'dollar_label_source', 'tat_match']],
        on='dl_row', how='left'
    )
    merged['actual_dollars'] = merged['actual_exit_multiple'] * merged['long_entry_cost']
    merged['B2_dollars'] = merged['B2_close_on_short_stop'] * merged['long_entry_cost']

    return merged


def verify_known_totals(merged):
    """Reproduce the three known published totals exactly before trusting anything else."""
    print("\n" + "=" * 78)
    print("VERIFICATION: reproducing known published totals from longleg_rule_summary_dollars.csv")
    print("=" * 78)
    dates = sorted(merged['TradeDate'].unique())
    split_idx = int(round(len(dates) * 0.7))
    train_dates = set(dates[:split_idx])
    test_dates = set(dates[split_idx:])
    train = merged[merged['TradeDate'].isin(train_dates)]
    test = merged[merged['TradeDate'].isin(test_dates)]

    checks = {
        ('train_with_crash', 'actual'): train['actual_dollars'].sum(),
        ('train_no_crash', 'B2'): train.loc[train['TradeDate'] != CRASH_DATE, 'B2_dollars'].sum(),
        ('test_with_crash', 'B2'): test['B2_dollars'].sum(),
    }
    all_ok = True
    for key, computed in checks.items():
        known = KNOWN_CHECKS[key]
        ok = abs(computed - known) < 0.01
        all_ok = all_ok and ok
        print(f"  {key}: computed=${computed:,.2f}  known=${known:,.2f}  "
              f"{'MATCH' if ok else 'MISMATCH -- STOP, do not trust downstream results'}")
    if not all_ok:
        raise AssertionError("Row-alignment / dollar-conversion verification failed.")
    print("  All checks passed -- row-alignment and dollar-conversion method confirmed correct.")
    return


def split_dates(dates_list):
    """Fresh chronological 70/30 split by unique date, for THIS population only."""
    dates = sorted(dates_list)
    n = len(dates)
    split_idx = int(round(n * 0.7))
    train_dates = set(dates[:split_idx])
    test_dates = set(dates[split_idx:])
    return train_dates, test_dates, dates


def analyze_population(df, label, min_legs=MIN_LEGS_FOR_HEADLINE, min_days=MIN_DAYS_FOR_HEADLINE):
    """
    Given an isolated leg population (already filtered to one template / label cut),
    run the full Part-2-style analysis: fresh 70/30 date split, B2 vs actual dollar
    P&L totals, leg-level win rate, day-level win rate, single-largest-day-removed
    check on both splits. Returns a dict of all computed numbers; prints as it goes.
    Explicitly flags (does not silently report) any cell below the thinness floor.
    """
    print("\n" + "-" * 78)
    print(f"POPULATION: {label}")
    print("-" * 78)

    n_total = len(df)
    n_days_total = df['TradeDate'].nunique()
    print(f"n legs = {n_total}, n days = {n_days_total}, "
          f"date range {df['TradeDate'].min()}-{df['TradeDate'].max()}")

    if n_total < min_legs or n_days_total < min_days:
        print(f"  *** TOO THIN (< {min_legs} legs or < {min_days} days) -- "
              f"no headline number reported for this cut. ***")
        return {'label': label, 'n_legs': n_total, 'n_days': n_days_total, 'too_thin': True}

    train_dates, test_dates, all_dates = split_dates(df['TradeDate'].unique())
    train = df[df['TradeDate'].isin(train_dates)]
    test = df[df['TradeDate'].isin(test_dates)]
    print(f"date split: {len(all_dates)} unique dates -> "
          f"{len(train_dates)} train ({min(train_dates)}-{max(train_dates)}), "
          f"{len(test_dates)} test ({min(test_dates)}-{max(test_dates)})")

    result = {'label': label, 'n_legs': n_total, 'n_days': n_days_total, 'too_thin': False}

    for split_name, split_df in (('train', train), ('test', test)):
        n_legs = len(split_df)
        n_days = split_df['TradeDate'].nunique()
        if n_legs == 0:
            result[f'{split_name}_n_legs'] = 0
            result[f'{split_name}_n_days'] = 0
            result[f'{split_name}_too_thin'] = True
            print(f"  {split_name}: EMPTY (0 legs)")
            continue

        too_thin = n_legs < min_legs or n_days < min_days
        actual_total = split_df['actual_dollars'].sum()
        b2_total = split_df['B2_dollars'].sum()
        leg_win_rate = (split_df['B2_dollars'] >= split_df['actual_dollars']).mean()

        day_pnl_actual = split_df.groupby('TradeDate')['actual_dollars'].sum()
        day_pnl_b2 = split_df.groupby('TradeDate')['B2_dollars'].sum()
        day_win_rate = (day_pnl_b2 >= day_pnl_actual).mean()

        # single-largest-day-removed robustness (by B2 dollar contribution)
        day_b2_totals = split_df.groupby('TradeDate')['B2_dollars'].sum()
        biggest_day = day_b2_totals.abs().idxmax()
        ex_biggest = split_df[split_df['TradeDate'] != biggest_day]
        b2_total_ex_biggest = ex_biggest['B2_dollars'].sum()
        actual_total_ex_biggest = ex_biggest['actual_dollars'].sum()

        result[f'{split_name}_n_legs'] = n_legs
        result[f'{split_name}_n_days'] = n_days
        result[f'{split_name}_too_thin'] = too_thin
        result[f'{split_name}_actual_total'] = actual_total
        result[f'{split_name}_b2_total'] = b2_total
        result[f'{split_name}_leg_win_rate'] = leg_win_rate
        result[f'{split_name}_day_win_rate'] = day_win_rate
        result[f'{split_name}_biggest_day'] = int(biggest_day)
        result[f'{split_name}_biggest_day_b2_contribution'] = day_b2_totals.loc[biggest_day]
        result[f'{split_name}_b2_total_ex_biggest_day'] = b2_total_ex_biggest
        result[f'{split_name}_actual_total_ex_biggest_day'] = actual_total_ex_biggest

        thin_flag = "  *** THIN (< min legs/days threshold) ***" if too_thin else ""
        print(f"  {split_name}: n_legs={n_legs}, n_days={n_days}{thin_flag}")
        print(f"    actual total=${actual_total:,.2f}   B2 total=${b2_total:,.2f}")
        print(f"    leg-level win rate (B2 >= actual): {leg_win_rate:.1%}")
        print(f"    day-level win rate (B2 day-total >= actual day-total): {day_win_rate:.1%}")
        print(f"    biggest day by |B2 $|: {biggest_day} (contributes ${day_b2_totals.loc[biggest_day]:,.2f} to B2 total)")
        print(f"    B2 total EXCLUDING biggest day: ${b2_total_ex_biggest:,.2f}  "
              f"(actual excl.: ${actual_total_ex_biggest:,.2f})")

        # per-observation Sharpe (multiple-based, matches DSR module convention)
        obs = split_df['B2_close_on_short_stop'].values.astype(float)
        if len(obs) >= 2 and obs.std(ddof=0) > 0:
            result[f'{split_name}_b2_sharpe'] = float(obs.mean() / obs.std(ddof=0))
        else:
            result[f'{split_name}_b2_sharpe'] = np.nan

    return result


def dollar_only_proxy_population(merged, dollar_label):
    """$-label-only proxy cut: final_dollar_label (short_open_price banding), full window."""
    return merged[merged['final_dollar_label'] == dollar_label].copy()


def true_labeled_population(merged, width_label, dollar_label):
    """
    True-labeled cut: real TAT Template match (MATCHED or AMBIGUOUS_MULTI_CANDIDATE),
    through TAT's coverage window only. Width label is NaN past 2026-03-19 by
    construction (template_join.py), so filtering on final_width_label notna()
    already restricts to the covered window.
    """
    mask = (
        (merged['final_width_label'].astype(str) == str(width_label)) &
        (merged['final_dollar_label'] == dollar_label) &
        (merged['tat_match'].isin(['MATCHED', 'AMBIGUOUS_MULTI_CANDIDATE']))
    )
    return merged[mask].copy()


def main():
    merged = load_and_align()
    verify_known_totals(merged)

    print(f"\nWriting per-leg results to {OUT_RESULTS_PATH}...")
    merged.to_csv(OUT_RESULTS_PATH, index=False)
    print(f"  wrote {len(merged)} rows")

    all_results = {}

    # ------------------------------------------------------------------
    # Analysis #1: fixed single template 80-$4
    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("ANALYSIS #1 -- FIXED SINGLE TEMPLATE: 80-$4 (Puts+Calls collapsed)")
    print("=" * 78)

    pop_true_80_4 = true_labeled_population(merged, '80.0', '$4')
    res = analyze_population(pop_true_80_4, "80-$4 TRUE-LABELED (through Mar 2026, width+$ confirmed)")
    all_results['80_4_true_labeled'] = res

    pop_proxy_4 = dollar_only_proxy_population(merged, '$4')
    res = analyze_population(pop_proxy_4, "$4-LABEL-ONLY PROXY (full window through Jul 2026, "
                              "width NOT confirmed -- mixes 80-$4 and 50-$4)")
    all_results['4_proxy_full_window'] = res

    # ------------------------------------------------------------------
    # Analysis #2: 6-config grid search (true-labeled, through March 2026 only)
    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("ANALYSIS #2 -- 6-CONFIG GRID SEARCH (true-labeled, through Mar 2026)")
    print("=" * 78)

    grid_configs = [
        ('80.0', '$2'), ('80.0', '$3'), ('80.0', '$4'),
        ('50.0', '$2'), ('50.0', '$3'), ('50.0', '$4'),
    ]
    grid_results = {}
    for width, dollar in grid_configs:
        cfg_label = f"{width.replace('.0','')}-{dollar}"
        pop = true_labeled_population(merged, width, dollar)
        res = analyze_population(pop, f"GRID CELL {cfg_label} (true-labeled through Mar 2026)")
        grid_results[cfg_label] = res

    all_results['grid'] = grid_results

    # ------------------------------------------------------------------
    # DSR: n_trials=6 (the intended grid), deflating the best cell.
    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("DSR -- deflating the best of the 6-config grid search")
    print("=" * 78)
    print("Per task spec: n_trials=6 (all 6 {$2,$3,$4} x {50,80} configs), following")
    print("canslim/dsr_report.py's report_multi_trial pattern (N = number of trials,")
    print("var_trials = population variance of the trial Sharpes, deflate the best).")
    print()
    print("Trial statistic per cell: per-observation Sharpe of the B2 rule's per-leg")
    print("MULTIPLE (B2_close_on_short_stop), full population (train+test combined --")
    print("template selection is a single frozen choice, not itself trained/tested,")
    print("so there is no leakage in using the full window for the trial statistic).")
    print()

    trial_sharpes = {}
    trial_series = {}
    thin_cells = []
    for width, dollar in grid_configs:
        cfg_label = f"{width.replace('.0','')}-{dollar}"
        pop = true_labeled_population(merged, width, dollar)
        if len(pop) < MIN_LEGS_FOR_HEADLINE or pop['TradeDate'].nunique() < MIN_DAYS_FOR_HEADLINE:
            thin_cells.append(cfg_label)
            print(f"  {cfg_label}: TOO THIN (n={len(pop)} legs, {pop['TradeDate'].nunique()} days) "
                  f"-- excluded from the DSR trial set, cannot contribute a trustworthy Sharpe.")
            continue
        obs = pop['B2_close_on_short_stop'].values.astype(float)
        sharpe = obs.mean() / obs.std(ddof=0)
        trial_sharpes[cfg_label] = sharpe
        trial_series[cfg_label] = obs
        print(f"  {cfg_label}: n={len(obs)} legs, per-observation Sharpe = {sharpe:+.4f}")

    print(f"\n{len(thin_cells)} of 6 cells excluded as too thin ({thin_cells}); "
          f"{len(trial_sharpes)} cells contribute a usable trial Sharpe.")
    print("Per task spec, n_trials is nonetheless fixed at 6 (the design intent of the "
          "grid search itself, not shrunk post-hoc to the cells that happened to have "
          "enough data) -- this is the conservative/honest choice: a smaller n_trials "
          "would UNDER-penalize the search. var_trials is computed from the actual "
          "observed Sharpes of the 4 usable cells (the only real information available).")

    if len(trial_sharpes) >= 2:
        sharpe_values = np.array(list(trial_sharpes.values()))
        var_trials = float(np.var(sharpe_values, ddof=0))
        best_cfg = max(trial_sharpes, key=trial_sharpes.get)
        best_returns = trial_series[best_cfg]
        dsr_lines = []
        dsr_result = ds.deflated_sharpe_ratio(
            observed_sharpe=trial_sharpes[best_cfg],
            T=len(best_returns),
            n_trials=6,
            var_trials=var_trials,
            skew=ds.sharpe_and_moments(best_returns)['skew'],
            kurtosis=ds.sharpe_and_moments(best_returns)['kurtosis'],
        )
        print(f"\nBest cell (of the {len(trial_sharpes)} usable): {best_cfg} "
              f"(Sharpe={trial_sharpes[best_cfg]:+.4f})")
        print(f"  N trials (per task spec)         : 6")
        print(f"  var(trial Sharpes, n={len(trial_sharpes)} usable cells) : {var_trials:.6f}")
        print(f"  T (best cell's leg count)         : {len(best_returns)}")
        print(f"  E[max SR] haircut (sr0)           : {dsr_result.sr0:+.4f}")
        print(f"  DSR  P(true SR > E[max SR])       : {dsr_result.dsr:.4f}")
        verdict = (
            "SURVIVES the multiple-comparisons haircut (DSR > 0.95)" if dsr_result.dsr > 0.95 else
            "does NOT survive the haircut (DSR <= 0.95) -- best cell's edge is "
            "consistent with grid-search luck, i.e. the plateau moves together"
        )
        print(f"  VERDICT: {verdict}")
        all_results['dsr'] = {
            'best_cfg': best_cfg, 'n_trials': 6, 'n_usable_cells': len(trial_sharpes),
            'var_trials': var_trials, 'T': len(best_returns), 'sr0': dsr_result.sr0,
            'dsr': dsr_result.dsr, 'trial_sharpes': trial_sharpes,
        }
    else:
        print("\nFewer than 2 usable cells -- cannot compute a meaningful DSR "
              "(no dispersion to estimate var_trials from).")
        all_results['dsr'] = None

    return all_results, merged


if __name__ == '__main__':
    main()
