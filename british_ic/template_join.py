"""
Template join — attach TAT's true `Template` string (the only ground-truth source
for the 50/80 width label) onto every row of decoupled_long_legs.csv, across TAT's
FULL available date range (2024-09-16 to 2026-03-19), extending the existing
`cross_check_tat()` join in reconstruct.py (which only covered October 2025) to the
whole window.

Why this exists: Andrew wants to know which single deterministic template (of the
11 real Template configs) a bot should run if it can't replicate human discretionary
template-switching. Answering that requires the true 50/80 width label, which only
TAT-tradelog carries (combo_ledger.csv / decoupled_long_legs.csv have `short_open_price`,
a proxy for the $ label only, with no width information at all).

Method (identical join logic to reconstruct.cross_check_tat, reused not reinvented):
  - Join key: TradeDate (OpenDateStr) + ComboType (PutSpread/CallSpread maps to
    TradeType) + exact short_strike/long_strike match against ShortPut/LongPut or
    ShortCall/LongCall.
  - Among same-day/same-strike candidates, break ties by nearest OpenTime to the
    combo's own short_open_dt time-of-day (no explicit tolerance -- closest wins).
  - >1 remaining candidate after the strike filter => AMBIGUOUS_MULTI_CANDIDATE
    (kept, but the Template pull is best-effort / flagged, not silently trusted).
  - 0 candidates => NO_MATCH.

decoupled_long_legs.csv runs 2025-07-09 to 2026-07-07; TAT-tradelog runs through
2026-03-19 only. Rows dated after 2026-03-19 CANNOT get a true width (50/80) label
under any circumstances -- per instruction, that field is left null/NaN for those
rows, never imputed. Those rows still get a $ label via short_open_price banding
(see `dollar_label_from_short_open_price` below) using cutpoints picked from the
already-established per-label medians in STRATEGY_MECHANICS.md section 3
($2 medians ~2.05-2.15, $3 ~3.00-3.08, $4 ~3.95-4.20): cutpoints used here are
$2.55 (between $2 and $3 medians) and $3.55 (between $3 and $4 medians).

Output: tat_full_join.csv (gitignored, not committed) -- one row per
decoupled_long_legs.csv row, with columns:
  all original decoupled_long_legs.csv columns, plus
  tat_match ('MATCHED' / 'AMBIGUOUS_MULTI_CANDIDATE' / 'NO_MATCH' / 'NO_TAT_COVERAGE'),
  tat_n_candidates, tat_Template, template_width ('80'/'50'/NaN),
  template_dollar_tat (from TAT Template string, only when matched),
  dollar_label_proxy (from short_open_price banding, always populated),
  final_dollar_label (tat-derived when available, else proxy),
  final_width_label (tat-derived only, NaN past 2026-03-19 or on no-match).
"""

import pandas as pd
import numpy as np
from pathlib import Path

import reconstruct

OUT_DIR = Path(__file__).parent
DECOUPLED_PATH = OUT_DIR / "decoupled_long_legs.csv"
OUT_PATH = OUT_DIR / "tat_full_join.csv"

TAT_LAST_DATE = 20260319

# short_open_price banding cutpoints -- picked as the midpoints between the
# established per-label medians in STRATEGY_MECHANICS.md section 3
# ($2 median ~2.05-2.15, $3 median ~3.00-3.08, $4 median ~3.95-4.20).
DOLLAR_BAND_CUT_LOW = 2.55   # below this => "$2"
DOLLAR_BAND_CUT_HIGH = 3.55  # between low and high => "$3"; above high => "$4"


def dollar_label_from_short_open_price(price):
    if pd.isna(price):
        return np.nan
    if price < DOLLAR_BAND_CUT_LOW:
        return '$2'
    elif price < DOLLAR_BAND_CUT_HIGH:
        return '$3'
    else:
        return '$4'


def parse_template(template_str):
    """
    'British IC - Puts - 80 - $4' -> ('Puts', '80', '$4')
    Returns (None, None, None) if the string doesn't parse as expected.
    """
    if not isinstance(template_str, str):
        return None, None, None
    parts = [p.strip() for p in template_str.split('-')]
    # e.g. ['British IC', 'Puts', '80', '$4']
    if len(parts) != 4:
        return None, None, None
    side = parts[1]
    width = parts[2]
    dollar = parts[3]
    return side, width, dollar


def join_full_range(dl, tat):
    """
    Extend cross_check_tat's join logic to the full decoupled_long_legs.csv
    population (no focus_year_month_prefixes filter), matching reconstruct.py's
    join key/tie-break exactly: TradeDate + ComboType/TradeType + exact
    short_strike/long_strike, nearest OpenTime among candidates.
    """
    tat = tat.copy()
    tat['OpenDateStr'] = pd.to_datetime(tat['OpenDate']).dt.strftime('%Y%m%d')

    dl = dl.copy().reset_index().rename(columns={'index': 'dl_row'})
    dl['TradeDateStr'] = dl['TradeDate'].astype(str)

    rows = []
    n = len(dl)
    for i, c in dl.iterrows():
        if (i + 1) % 300 == 0 or (i + 1) == n:
            print(f"  joined {i+1}/{n} decoupled long legs...")

        trade_date_int = int(c['TradeDate'])
        if trade_date_int > TAT_LAST_DATE:
            rows.append({
                'dl_row': c['dl_row'], 'tat_match': 'NO_TAT_COVERAGE',
                'tat_n_candidates': 0, 'tat_Template': None,
            })
            continue

        if c['ComboType'] == 'PutSpread':
            candidates = tat[
                (tat['OpenDateStr'] == c['TradeDateStr']) &
                (tat['TradeType'] == 'PutSpread') &
                (tat['ShortPut'] == c['short_strike']) &
                (tat['LongPut'] == c['long_strike'])
            ]
        else:
            candidates = tat[
                (tat['OpenDateStr'] == c['TradeDateStr']) &
                (tat['TradeType'] == 'CallSpread') &
                (tat['ShortCall'] == c['short_strike']) &
                (tat['LongCall'] == c['long_strike'])
            ]

        if len(candidates) == 0:
            rows.append({
                'dl_row': c['dl_row'], 'tat_match': 'NO_MATCH',
                'tat_n_candidates': 0, 'tat_Template': None,
            })
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
            'dl_row': c['dl_row'],
            'tat_match': 'MATCHED' if len(candidates) == 1 else 'AMBIGUOUS_MULTI_CANDIDATE',
            'tat_n_candidates': len(candidates),
            'tat_Template': best['Template'],
        })

    return pd.DataFrame(rows)


def main():
    print("Loading decoupled_long_legs.csv...")
    dl = pd.read_csv(DECOUPLED_PATH)
    print(f"  {len(dl)} rows, date range {dl['TradeDate'].min()}-{dl['TradeDate'].max()}")

    print("\nLoading TAT-tradelog.xlsx (full range, 2024-09-16 to 2026-03-19)...")
    tat = reconstruct.load_tat_tradelog()
    print(f"  {len(tat)} TAT rows loaded")

    print("\nJoining TAT Template onto every decoupled_long_legs.csv row"
          " (TradeDate+ComboType+strikes, nearest-OpenTime tiebreak)...")
    join_result = join_full_range(dl, tat)

    dl = dl.reset_index().rename(columns={'index': 'dl_row'})
    merged = dl.merge(join_result, on='dl_row', how='left')

    # parse the matched Template string into side/width/dollar
    parsed = merged['tat_Template'].apply(parse_template)
    merged['tat_side'] = [p[0] for p in parsed]
    merged['tat_width'] = [p[1] for p in parsed]
    merged['tat_dollar'] = [p[2] for p in parsed]

    # width label: ONLY from a real TAT match, never past TAT coverage / on no-match
    merged['final_width_label'] = np.where(
        merged['tat_match'] == 'MATCHED', merged['tat_width'],
        np.where(merged['tat_match'] == 'AMBIGUOUS_MULTI_CANDIDATE', merged['tat_width'], np.nan)
    )
    # NOTE: AMBIGUOUS_MULTI_CANDIDATE still carries a best-effort Template pull
    # (nearest-OpenTime winner) -- flagged via tat_match, not dropped, consistent
    # with cross_check_tat's own convention. Downstream analysis should treat
    # ambiguous rows cautiously (reported separately in match-rate summary).

    # $ label: prefer real TAT-derived label when available (MATCHED or AMBIGUOUS),
    # else fall back to short_open_price banding proxy (always available).
    merged['dollar_label_proxy'] = merged['short_open_price'].apply(dollar_label_from_short_open_price)
    merged['final_dollar_label'] = merged['tat_dollar'].fillna(merged['dollar_label_proxy'])
    merged['dollar_label_source'] = np.where(
        merged['tat_dollar'].notna(), 'tat_template_string', 'short_open_price_proxy'
    )

    print(f"\nWriting joined file to {OUT_PATH}...")
    merged.to_csv(OUT_PATH, index=False)
    print(f"  wrote {len(merged)} rows")

    # ---- match-rate report ----
    print("\n" + "=" * 78)
    print("JOIN MATCH-RATE SUMMARY")
    print("=" * 78)
    print(f"Total decoupled_long_legs.csv rows: {len(merged)}")
    within_tat = merged[merged['TradeDate'] <= TAT_LAST_DATE]
    past_tat = merged[merged['TradeDate'] > TAT_LAST_DATE]
    print(f"Rows within TAT coverage (<= {TAT_LAST_DATE}): {len(within_tat)}")
    print(f"Rows past TAT coverage  (>  {TAT_LAST_DATE}): {len(past_tat)}")
    print("\nWithin-TAT-coverage match breakdown:")
    print(within_tat['tat_match'].value_counts(dropna=False).to_string())
    print(f"\nMatched: {(within_tat['tat_match']=='MATCHED').sum()} "
          f"({100*(within_tat['tat_match']=='MATCHED').mean():.1f}%)")
    print(f"Ambiguous multi-candidate: {(within_tat['tat_match']=='AMBIGUOUS_MULTI_CANDIDATE').sum()} "
          f"({100*(within_tat['tat_match']=='AMBIGUOUS_MULTI_CANDIDATE').mean():.1f}%)")
    print(f"No match: {(within_tat['tat_match']=='NO_MATCH').sum()} "
          f"({100*(within_tat['tat_match']=='NO_MATCH').mean():.1f}%)")

    print("\nfinal_dollar_label source breakdown (all rows):")
    print(merged['dollar_label_source'].value_counts(dropna=False).to_string())

    print("\nfinal_width_label coverage (all rows):")
    print(f"  non-null: {merged['final_width_label'].notna().sum()} / {len(merged)}")
    print(f"  null (past TAT coverage or no-match): {merged['final_width_label'].isna().sum()} / {len(merged)}")

    print("\nfinal_dollar_label distribution (all rows):")
    print(merged['final_dollar_label'].value_counts(dropna=False).to_string())

    print("\nfinal_width_label distribution (rows with a real label):")
    print(merged['final_width_label'].value_counts(dropna=False).to_string())

    print("\nCombined true-labeled template distribution "
          "(final_width_label + final_dollar_label, MATCHED/AMBIGUOUS rows only):")
    labeled = merged[merged['final_width_label'].notna()]
    combo = (labeled['final_width_label'].astype(str) + '-' + labeled['final_dollar_label'].astype(str))
    print(combo.value_counts().to_string())

    return merged


if __name__ == '__main__':
    main()
