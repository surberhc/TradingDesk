"""
Stage B, Task 1 step 2-3: apply a concrete frequency threshold to
s8_schedule_rebuild_report.csv's per-template 5-min bucket tables, and emit
the resulting entry_times_et grids to paste into s8_mechanical_simulator.py.

Threshold rule (stated, not hand-waved):
  A 5-min bucket counts as a real scheduled slot iff BOTH:
    (a) it accounts for >= 3% of that template's total MATCHED entries, AND
    (b) it appears on >= 3 distinct trading days (guards against one busy day
        inflating a single bucket's share for low-n templates).
  Rationale for 3%: with the two largest templates at n=439/332, a 3% cutoff
  requires roughly n>=13/10 hits before a slot counts -- enough to distinguish
  a repeating clock slot from one-off manual/discretionary entries, while still
  keeping real secondary slots (e.g. Puts-80-$4's afternoon 13:05 cluster at
  6.6% n=29) that a stricter 5-10% cutoff would wrongly discard. For n<30
  templates the 3-distinct-day floor (b) does the real work since %-based
  alone would let a single day's 2 trades (e.g. 2/17 = 11.8%) count as a
  "slot" off one day's noise.
  Templates with fewer than 10 MATCHED rows total (Puts-50-$3 n=2, Calls-50-$4
  n=1, Puts-50-$4 n=1) are FLAGGED LOW-CONFIDENCE and fall back to the
  existing Stage-A/STRATEGY_MECHANICS.md-derived grid rather than deriving a
  grid from 1-2 points.
"""

import pandas as pd

MIN_PCT = 3.0
MIN_DAYS = 3
MIN_TOTAL_N_FOR_DERIVATION = 10
MIN_COVERAGE_PCT_FOR_TRUST = 40.0  # if kept slots cover less than this share of
# a template's real entries, the derived grid is too sparse to trust over the
# existing Stage-A grid -- flag low-confidence instead of shipping a 1-slot grid

# from rebuild_entry_schedule.py's `strict` frame -- distinct day counts per
# (template, bucket) aren't in the saved report (which only has n/pct at the
# template level), so recompute directly from combo_ledger_tat_joined.csv.
import reconstruct  # noqa: F401  (ensures same import path works)

joined = pd.read_csv('combo_ledger_tat_joined.csv')
strict = joined[joined['tat_match'] == 'MATCHED'].copy()
strict['template_key'] = (
    strict['tat_side'].astype(str) + '-' +
    strict['tat_width'].astype(str) + '-' +
    strict['tat_dollar'].astype(str)
)
strict['short_open_dt'] = pd.to_datetime(strict['short_open_dt'])


def round_5min(t):
    total_min = t.hour * 60 + t.minute
    rounded = int(round(total_min / 5.0)) * 5
    return f"{rounded // 60:02d}:{rounded % 60:02d}"


strict['bucket_5min'] = strict['short_open_dt'].apply(round_5min)
strict['trade_date'] = strict['short_open_dt'].dt.date

report_rows = []
grids = {}

for tk in sorted(strict['template_key'].dropna().unique()):
    sub = strict[strict['template_key'] == tk]
    n_total = len(sub)
    if n_total < MIN_TOTAL_N_FOR_DERIVATION:
        grids[tk] = None  # signal: fall back to existing hardcoded grid
        print(f"{tk}: n={n_total} < {MIN_TOTAL_N_FOR_DERIVATION} -> LOW-CONFIDENCE, "
              f"fallback to existing Stage-A grid")
        continue

    bycount = sub.groupby('bucket_5min').agg(
        n=('bucket_5min', 'size'),
        n_days=('trade_date', 'nunique'),
    ).reset_index()
    bycount['pct'] = (bycount['n'] / n_total * 100).round(2)
    bycount['kept'] = (bycount['pct'] >= MIN_PCT) & (bycount['n_days'] >= MIN_DAYS)
    bycount = bycount.sort_values('bucket_5min')

    kept = bycount[bycount['kept']].copy()
    grid = sorted(kept['bucket_5min'].tolist())
    coverage_pct = kept['n'].sum() / n_total * 100

    if coverage_pct < MIN_COVERAGE_PCT_FOR_TRUST:
        grids[tk] = None
        print(f"\n{tk}: n={n_total}, kept {len(grid)}/{len(bycount)} buckets but only "
              f"{coverage_pct:.1f}% coverage (< {MIN_COVERAGE_PCT_FOR_TRUST}%) -> "
              f"LOW-CONFIDENCE, fallback to existing Stage-A grid")
        for _, row in bycount.iterrows():
            flag = "KEEP" if row['kept'] else "    "
            print(f"    [{flag}] {row['bucket_5min']}: n={int(row['n'])} ({row['pct']}%), "
                  f"{int(row['n_days'])} distinct days")
        for _, row in bycount.iterrows():
            report_rows.append({
                'template_key': tk, 'bucket_5min': row['bucket_5min'], 'n': int(row['n']),
                'pct': row['pct'], 'n_days': int(row['n_days']), 'kept': bool(row['kept']),
            })
        continue

    grids[tk] = grid
    print(f"\n{tk}: n={n_total}, kept {len(grid)}/{len(bycount)} buckets "
          f"({coverage_pct:.1f}% of entries covered by kept slots)")
    for _, row in bycount.iterrows():
        flag = "KEEP" if row['kept'] else "    "
        print(f"    [{flag}] {row['bucket_5min']}: n={int(row['n'])} ({row['pct']}%), "
              f"{int(row['n_days'])} distinct days")

    for _, row in bycount.iterrows():
        report_rows.append({
            'template_key': tk, 'bucket_5min': row['bucket_5min'], 'n': int(row['n']),
            'pct': row['pct'], 'n_days': int(row['n_days']), 'kept': bool(row['kept']),
        })

pd.DataFrame(report_rows).to_csv('s8_grid_threshold_report.csv', index=False)
print("\nWrote s8_grid_threshold_report.csv")

print("\n" + "=" * 78)
print("FINAL GRIDS (paste into TEMPLATES dict)")
print("=" * 78)
for tk, grid in grids.items():
    if grid is None:
        print(f'  "{tk}": LOW-CONFIDENCE, keep existing hardcoded grid')
    else:
        print(f'  "{tk}": {tuple(grid)}')
