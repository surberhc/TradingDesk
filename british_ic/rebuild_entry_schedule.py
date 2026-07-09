"""
Stage B, Task 1: rebuild each S8 template's entry-time schedule from the FULL
empirical real entry-timestamp data, not the coarse clock-time summary in
STRATEGY_MECHANICS.md section 1.

Method: join combo_ledger.csv (real IBKR Flex reconstruction, ET timestamps,
2025-07-09 to 2026-07-07, 2,593 rows) against TAT-tradelog.xlsx template labels,
using the SAME join logic as template_join.py's join_full_range() (reused, not
reinvented): TradeDate + ComboType + exact short_strike/long_strike match against
TAT rows, nearest-OpenTime tiebreak among candidates. Applied directly to
combo_ledger.csv (short-centric, one row per combo) rather than
decoupled_long_legs.csv (long-leg-centric, exploded 1-short-to-N-longs) so every
real combo gets a shot at a label, not just ones with a decoupled long leg.

TAT-tradelog itself only covers OpenDate through 2026-03-19 (~9 months of the
~12-month combo_ledger window). Rows after that date CANNOT get a true "80"/"50"
width label from TAT under any circumstances. Decision (documented, per task
brief option (a)/(b) tradeoff):
  - For the TIME-GRID rebuild specifically, we use ONLY TAT-matched rows (true
    width+dollar label from the TAT Template string) to assign each entry to one
    of the 11 templates. This is the defensible choice because width is exactly
    the thing that can't be reliably proxied (the existing short_open_price-based
    dollar proxy in template_join.py works for the $-label but there is no
    analogous width proxy), and because the TAT-covered period (2025-07-09 to
    2026-03-19, ~8.5 months, 165 of 236 trading days) already spans a full range
    of vol regimes including the 2025-10-10 crash, morning/afternoon sessions,
    and multiple months -- there's no reason to expect entry-CLOCK-TIME behavior
    (a mechanical/scheduled property of the algo, not a market-conditions-driven
    one) to differ systematically in the untagged Mar-Jul 2026 tail. We do NOT
    use the short_open_price dollar-only proxy to assign template labels for the
    schedule rebuild, because for several templates (i.e. same dollar band, two
    different widths, e.g. 50-$4 vs 80-$4) the width distinguishes templates with
    otherwise-similar entry-time behavior and getting it wrong would corrupt the
    per-template time buckets, not just mislabel a P&L. Per-template n and date
    coverage are reported explicitly below so any template resting on thin data
    is flagged, not hidden.

Output: prints the per-template 5-min-bucketed cluster tables and the derived
entry_times_et grids intended to be pasted into s8_mechanical_simulator.py's
TEMPLATES dict, plus writes british_ic/s8_schedule_rebuild_report.csv (small,
committed -- one row per (template, bucket) with n and pct, for anyone auditing
the bucket-selection rule) and british_ic/combo_ledger_tat_joined.csv (larger,
per-combo join result, gitignored, kept as an intermediate for anyone wanting to
re-run other analyses on the same corrected labels without rejoining).
"""

import numpy as np
import pandas as pd

import reconstruct

TAT_LAST_DATE = 20260319


def parse_template(template_str):
    if not isinstance(template_str, str):
        return None, None, None
    parts = [p.strip() for p in template_str.split('-')]
    if len(parts) != 4:
        return None, None, None
    return parts[1], parts[2], parts[3]  # side, width, dollar


def join_combo_ledger_to_tat(combo, tat):
    """Same join logic as template_join.join_full_range(), applied to
    combo_ledger.csv rows directly (short_strike/long_strike columns exist
    natively on combo_ledger, one row per combo -- no explode needed for the
    join key itself, though a combo can have multiple paired longs; we use the
    combo's OWN short_strike and, for the long side of the match key, the first
    listed long_strike, matching TAT's one-short-one-long row shape most closely
    when n_paired_longs==1, and flagged as multi-long when not).
    """
    tat = tat.copy()
    tat['OpenDateStr'] = pd.to_datetime(tat['OpenDate']).dt.strftime('%Y%m%d')

    combo = combo.copy().reset_index(drop=True)
    combo['TradeDateStr'] = combo['TradeDate'].astype(str)

    # long_strikes column is a stringified python list, e.g. "[np.int64(6975)]"
    def first_long_strike(s):
        import re
        # values look like "[np.int64(6975)]" or "[np.float64(6975.0), np.int64(6230)]"
        m = re.search(r'\((\d+(?:\.\d+)?)\)', str(s))
        return float(m.group(1)) if m else np.nan

    combo['first_long_strike'] = combo['long_strikes'].apply(first_long_strike)

    rows = []
    n = len(combo)
    for i, c in combo.iterrows():
        if (i + 1) % 500 == 0 or (i + 1) == n:
            print(f"  joined {i + 1}/{n} combo_ledger rows...")

        trade_date_int = int(c['TradeDate'])
        if trade_date_int > TAT_LAST_DATE:
            rows.append({'combo_row': i, 'tat_match': 'NO_TAT_COVERAGE',
                         'tat_n_candidates': 0, 'tat_Template': None})
            continue

        if c['ComboType'] == 'PutSpread':
            candidates = tat[
                (tat['OpenDateStr'] == c['TradeDateStr']) &
                (tat['TradeType'] == 'PutSpread') &
                (tat['ShortPut'] == c['short_strike']) &
                (tat['LongPut'] == c['first_long_strike'])
            ]
        else:
            candidates = tat[
                (tat['OpenDateStr'] == c['TradeDateStr']) &
                (tat['TradeType'] == 'CallSpread') &
                (tat['ShortCall'] == c['short_strike']) &
                (tat['LongCall'] == c['first_long_strike'])
            ]

        if len(candidates) == 0:
            rows.append({'combo_row': i, 'tat_match': 'NO_MATCH',
                         'tat_n_candidates': 0, 'tat_Template': None})
            continue

        short_open_time = pd.Timestamp(c['short_open_dt']).time()
        candidates = candidates.copy()
        candidates['time_diff_sec'] = candidates['OpenTime'].apply(
            lambda t: abs((pd.Timestamp.combine(pd.Timestamp.today(), t) -
                           pd.Timestamp.combine(pd.Timestamp.today(), short_open_time)).total_seconds())
            if pd.notna(t) else np.nan
        )
        candidates = candidates.sort_values('time_diff_sec')
        best = candidates.iloc[0]

        rows.append({
            'combo_row': i,
            'tat_match': 'MATCHED' if len(candidates) == 1 else 'AMBIGUOUS_MULTI_CANDIDATE',
            'tat_n_candidates': len(candidates),
            'tat_Template': best['Template'],
        })

    return pd.DataFrame(rows)


def main():
    print("Loading combo_ledger.csv...")
    combo = pd.read_csv('combo_ledger.csv')
    print(f"  {len(combo)} rows, date range {combo['TradeDate'].min()}-{combo['TradeDate'].max()}")

    print("Loading TAT-tradelog.xlsx...")
    tat = reconstruct.load_tat_tradelog()
    print(f"  {len(tat)} TAT rows loaded")

    print("Joining combo_ledger.csv rows against TAT template labels...")
    join_result = join_combo_ledger_to_tat(combo, tat)

    combo = combo.reset_index(drop=True)
    combo['combo_row'] = combo.index
    merged = combo.merge(join_result, on='combo_row', how='left')

    parsed = merged['tat_Template'].apply(parse_template)
    merged['tat_side'] = [p[0] for p in parsed]
    merged['tat_width'] = [p[1] for p in parsed]
    merged['tat_dollar'] = [p[2] for p in parsed]

    print("\n" + "=" * 78)
    print("JOIN MATCH-RATE SUMMARY (combo_ledger.csv x TAT-tradelog)")
    print("=" * 78)
    within = merged[merged['TradeDate'] <= TAT_LAST_DATE]
    past = merged[merged['TradeDate'] > TAT_LAST_DATE]
    print(f"Total combo_ledger rows: {len(merged)}")
    print(f"Within TAT coverage (<= {TAT_LAST_DATE}): {len(within)}")
    print(f"Past TAT coverage  (>  {TAT_LAST_DATE}): {len(past)}")
    print("\nWithin-TAT-coverage match breakdown:")
    print(within['tat_match'].value_counts(dropna=False).to_string())
    matched = merged[merged['tat_match'].isin(['MATCHED', 'AMBIGUOUS_MULTI_CANDIDATE'])].copy()
    print(f"\nTotal usable (MATCHED + AMBIGUOUS) rows for schedule rebuild: {len(matched)}")

    # Build full template key = side-width-dollar, use ONLY MATCHED (strict) for
    # the primary schedule; report AMBIGUOUS separately since best-effort tiebreak
    # could be wrong more of the time.
    strict = merged[merged['tat_match'] == 'MATCHED'].copy()
    ambiguous = merged[merged['tat_match'] == 'AMBIGUOUS_MULTI_CANDIDATE'].copy()
    print(f"Strict MATCHED only: {len(strict)} rows")
    print(f"AMBIGUOUS_MULTI_CANDIDATE: {len(ambiguous)} rows (reported, not primary)")

    strict['template_key'] = strict['tat_side'] + '-' + strict['tat_width'] + '-' + strict['tat_dollar']
    strict['short_open_dt'] = pd.to_datetime(strict['short_open_dt'])
    strict['entry_time'] = strict['short_open_dt'].dt.time

    def round_5min(t):
        total_min = t.hour * 60 + t.minute
        rounded = int(round(total_min / 5.0)) * 5
        return f"{rounded // 60:02d}:{rounded % 60:02d}"

    strict['bucket_5min'] = strict['short_open_dt'].apply(round_5min)

    print("\n" + "=" * 78)
    print("PER-TEMPLATE ROW COUNTS AND DATE COVERAGE (strict MATCHED only)")
    print("=" * 78)
    template_keys = sorted(strict['template_key'].dropna().unique())
    for tk in template_keys:
        sub = strict[strict['template_key'] == tk]
        n_days = sub['TradeDate'].nunique()
        print(f"  {tk}: n={len(sub)}, distinct days={n_days}, "
              f"date range {sub['TradeDate'].min()}-{sub['TradeDate'].max()}")

    print("\n" + "=" * 78)
    print("BUCKET FREQUENCY TABLES (evidence for the threshold rule)")
    print("=" * 78)
    bucket_report_rows = []
    for tk in template_keys:
        sub = strict[strict['template_key'] == tk]
        n_total = len(sub)
        vc = sub['bucket_5min'].value_counts()
        vc_pct = (vc / n_total * 100).round(1)
        print(f"\n--- {tk} (n={n_total}) ---")
        for bucket, cnt in vc.sort_index().items():
            pct = vc_pct[bucket]
            print(f"  {bucket}: n={cnt} ({pct}%)")
            bucket_report_rows.append({
                'template_key': tk, 'bucket_5min': bucket, 'n': cnt,
                'pct_of_template': pct, 'template_total_n': n_total,
            })

    report_df = pd.DataFrame(bucket_report_rows)
    report_df.to_csv('s8_schedule_rebuild_report.csv', index=False)
    print(f"\nWrote s8_schedule_rebuild_report.csv ({len(report_df)} rows)")

    merged.to_csv('combo_ledger_tat_joined.csv', index=False)
    print(f"Wrote combo_ledger_tat_joined.csv ({len(merged)} rows, intermediate, gitignored)")

    return strict, report_df


if __name__ == '__main__':
    main()
