"""
British IC reconstruction pipeline.

Standalone, read-only reconstruction of the true per-combo P&L for the
"British IC" 0DTE SPX iron-condor-style strategy traded via TAT/NinjaTrader
in IBKR account U***9156.

Reads three source files (READ-ONLY, never modified):
  1. TAT-tradelog.xlsx  (sheet 'TAT-tradelog')      -- TAT's own combo log (P&L unreliable)
  2. BIC data.xlsx      (sheet '20250709_20')        -- IBKR Flex Query execution-level export (ground truth)
  3. Daily_Ending_Balance NT BIC Strategy.xlsx        -- daily account balance (validation only)

Produces:
  - combo_ledger.csv          -- one row per reconstructed combo trade
  - RECONSTRUCTION_NOTES.md   -- summary findings (written by a separate step using this module's outputs)

No P&L or timing is ever fabricated/interpolated. FifoPnlRealized is trusted directly
per the user's data-quality note. Ambiguous cases are flagged, not resolved by guessing.
"""

import pandas as pd
import numpy as np
from pathlib import Path

# ---------------------------------------------------------------------------
# Source paths (READ-ONLY)
# ---------------------------------------------------------------------------
TAT_PATH = r"C:\Users\andre\My Drive (andrew@surberhc.com)\Surber_HC_Command_Center\05_Options_Algos\Options Algos\NT BIC Data\TAT-tradelog.xlsx"
BIC_PATH_CANDIDATES = [
    r"G:\My Drive\Surber_HC_Command_Center\05_Options_Algos\Options Algos\NT BIC Data\BIC data.xlsx",
    r"C:\Users\andre\My Drive (andrew@surberhc.com)\Surber_HC_Command_Center\05_Options_Algos\Options Algos\NT BIC Data\BIC data.xlsx",
]
BALANCE_PATH = r"C:\Users\andre\My Drive (andrew@surberhc.com)\Surber_HC_Command_Center\05_Options_Algos\Options Algos\NT BIC Data\Daily_Ending_Balance NT BIC Strategy.xlsx"

OUT_DIR = Path(__file__).parent
LEDGER_PATH = OUT_DIR / "combo_ledger.csv"

OPEN_TOLERANCE_SEC = 5       # combo pairing: same-side open-time tolerance
PAIRED_EXIT_TOLERANCE_MIN = 2  # short+long close within this many minutes = "paired exit"


def _resolve_bic_path():
    for p in BIC_PATH_CANDIDATES:
        if Path(p).exists():
            return p
    raise FileNotFoundError(f"BIC data.xlsx not found at any candidate path: {BIC_PATH_CANDIDATES}")


def load_bic_executions():
    """Load raw IBKR Flex Query execution export, filter to 0DTE rows only.

    Per the user's known data-quality note: exclude any row where
    TradeDate != Expiry (this is a separate diagonal-put/calendar-condor side
    book, not British IC, and must not be reconstructed here).

    Additional finding made during reconstruction: excluding those rows on a
    per-ROW basis leaves "orphan" rows behind for the SAME Conid that do pass
    TradeDate == Expiry (typically the closing/settlement execution of a
    multi-day option whose OPENING execution was on an earlier, non-0DTE,
    TradeDate and therefore correctly excluded above). These orphan rows are
    not part of any 0DTE British IC combo -- they are the tail end of the
    excluded side book leaking through the per-row filter. Verified: 17 such
    orphan rows exist, spanning Conids whose open leg was among the 18
    excluded rows, totaling -$26,847.62 in FifoPnlRealized that does NOT
    belong to the British IC 0DTE reconstruction. These are dropped too, and
    reported explicitly (never silently).
    """
    path = _resolve_bic_path()
    df = pd.read_excel(path, sheet_name="20250709_20")

    n_total = len(df)
    non_0dte = df[df['TradeDate'] != df['Expiry']].copy()
    excluded_conids = set(non_0dte['Conid'])

    df_0dte_rows = df[df['TradeDate'] == df['Expiry']].copy()
    orphan_mask = df_0dte_rows['Conid'].isin(excluded_conids)
    orphan_rows = df_0dte_rows[orphan_mask].copy()
    df = df_0dte_rows[~orphan_mask].copy()
    n_0dte = len(df)

    # Parse DateTime "yyyymmdd;HHMMSS" string -> real timestamp
    df['dt'] = pd.to_datetime(df['DateTime'], format='%Y%m%d;%H%M%S')

    # stable secondary sort key for same-timestamp fills
    df = df.sort_values(['Conid', 'dt', 'TransactionID']).reset_index(drop=True)

    return df, n_total, n_0dte, non_0dte, orphan_rows


def reconstruct_lifecycles(df):
    """
    Step 1: per-Conid lifecycle reconstruction.

    Walk executions chronologically per Conid, track running signed position.
    A lifecycle = a contiguous run of executions between two "at-zero" points.
    Also handles expiration settlement rows (TransactionType == 'BookTrade'),
    which are themselves closing executions (Open/CloseIndicator == 'C') with
    a real FifoPnlRealized and belong to the lifecycle they close out.
    """
    lifecycles = []
    ambiguous_notes = []

    for conid, g in df.groupby('Conid', sort=False):
        g = g.sort_values(['dt', 'TransactionID']).reset_index(drop=True)

        running_pos = 0
        cur_open_rows = []
        cur_close_rows = []
        lifecycle_started = False

        def flush_lifecycle():
            nonlocal cur_open_rows, cur_close_rows, lifecycle_started
            if not cur_open_rows:
                return
            open_df = pd.DataFrame(cur_open_rows)
            close_df = pd.DataFrame(cur_close_rows) if cur_close_rows else pd.DataFrame()

            first_open = open_df.iloc[0]
            strike = first_open['Strike']
            put_call = first_open['Put/Call']
            first_open_dt = first_open['dt']
            first_open_price = first_open['TradePrice']
            total_open_qty = open_df['Quantity'].sum()

            # IMPORTANT: FifoPnlRealized is NOT confined to closing ('C') rows.
            # IBKR's Flex Query export attributes some realized P&L to an
            # OPENING ('O') execution when a reopen-after-flatten event happens
            # intraday for the same Conid (verified empirically: sum(all-rows
            # FifoPnlRealized) grouped by TradeDate matches sum(NetCash) exactly,
            # and matches the daily balance file to ~$50/day average — the
            # close-rows-only sum does NOT match either). So the lifecycle's
            # true realized P&L must sum FifoPnlRealized across BOTH its open
            # and close executions, not close-only.
            total_fifo_pnl = open_df['FifoPnlRealized'].sum()
            if len(close_df) > 0:
                last_close_dt = close_df['dt'].max()
                total_close_qty = close_df['Quantity'].sum()
                total_fifo_pnl += close_df['FifoPnlRealized'].sum()
                fully_closed = (total_open_qty + total_close_qty) == 0
            else:
                last_close_dt = pd.NaT
                total_close_qty = 0
                fully_closed = False  # never closed within data window

            # distinct open-batch timestamps within this lifecycle (a "batch" = all
            # opening executions sharing the same timestamp). Used to detect/pair
            # scale-in adds against newly-opened long legs at the same instant.
            open_batch_ts = sorted(open_df['dt'].unique())

            lifecycles.append({
                'Conid': conid,
                'Strike': strike,
                'Put/Call': put_call,
                'TradeDate': first_open['TradeDate'],
                'first_open_dt': first_open_dt,
                'first_open_price': first_open_price,
                'total_open_qty': total_open_qty,
                'n_open_execs': len(open_df),
                'n_open_batches': len(open_batch_ts),
                'open_batch_timestamps': open_batch_ts,
                'last_close_dt': last_close_dt,
                'total_close_qty': total_close_qty,
                'n_close_execs': len(close_df),
                'total_fifo_pnl': total_fifo_pnl,
                'fully_closed': fully_closed,
                'side': 'SHORT' if first_open['Buy/Sell'] == 'SELL' else 'LONG',
            })
            cur_open_rows = []
            cur_close_rows = []
            lifecycle_started = False

        for _, row in g.iterrows():
            oc = row['Open/CloseIndicator']
            qty = row['Quantity']

            if oc == 'O':
                cur_open_rows.append(row)
                lifecycle_started = True
            else:  # 'C'
                cur_close_rows.append(row)

            running_pos += qty

            if lifecycle_started and running_pos == 0:
                flush_lifecycle()

        # leftover open lifecycle at end of data (never fully closed in window)
        if cur_open_rows:
            ambiguous_notes.append({
                'Conid': conid,
                'issue': 'lifecycle never returned to zero within data window',
                'ending_position': running_pos,
                'n_open_execs': len(cur_open_rows),
                'n_close_execs': len(cur_close_rows),
            })
            flush_lifecycle()

    lc_df = pd.DataFrame(lifecycles)
    amb_df = pd.DataFrame(ambiguous_notes)
    return lc_df, amb_df


def pair_combos(lc_df):
    """
    Step 2: combo pairing.

    A "combo group" = one SHORT lifecycle (one Conid/strike, may scale in across
    multiple open-batches within the same day) plus the set of LONG lifecycles
    whose open timestamps match one of the short's open-batch timestamps
    (same TradeDate, same Put/Call, within OPEN_TOLERANCE_SEC).

    IMPORTANT — honesty constraint: IBKR's FifoPnlRealized on the short leg's
    closing execution(s) is computed FIFO across the ENTIRE accumulated short
    lifecycle. When a short lifecycle has more than one open-batch (i.e. it
    scaled in multiple times before flattening), there is NO way to split its
    aggregate closing P&L across the individual batches/paired long legs
    without fabricating an allocation (by qty, by premium, by time — all are
    guesses). So for such lifecycles we report ONE combo-group row: the short
    P&L stays at the aggregate lifecycle level, and each paired long leg is
    listed with its own real, fully-disaggregated P&L (longs DO segment
    cleanly, one lifecycle per Conid). This is flagged via `n_paired_longs > 1`
    / `short_pnl_disaggregated == False`.

    When a short lifecycle has exactly one open-batch (opens once, matched by
    exactly one long lifecycle), this reduces to a clean 1:1 combo and
    short_pnl_disaggregated == True (full, unambiguous per-combo attribution).
    """
    shorts = lc_df[lc_df['side'] == 'SHORT'].copy().reset_index(drop=True)
    longs = lc_df[lc_df['side'] == 'LONG'].copy().reset_index(drop=True)

    longs_used = set()
    combo_groups = []
    unmatched_batches = []
    ambiguous_pairs = []

    longs['key'] = list(zip(longs['TradeDate'], longs['Put/Call']))
    longs_by_key = {}
    for idx, row in longs.iterrows():
        longs_by_key.setdefault(row['key'], []).append(idx)

    for _, srow in shorts.iterrows():
        key = (srow['TradeDate'], srow['Put/Call'])
        candidate_idxs = [i for i in longs_by_key.get(key, []) if i not in longs_used]

        paired_longs = []
        for batch_ts in srow['open_batch_timestamps']:
            matches = []
            for lidx in candidate_idxs:
                if lidx in longs_used:
                    continue
                lrow = longs.loc[lidx]
                dt_diff = abs((pd.Timestamp(batch_ts) - lrow['first_open_dt']).total_seconds())
                if dt_diff <= OPEN_TOLERANCE_SEC:
                    matches.append((lidx, dt_diff, lrow))

            if len(matches) == 0:
                unmatched_batches.append({
                    'short_conid': srow['Conid'], 'short_strike': srow['Strike'],
                    'Put/Call': srow['Put/Call'], 'TradeDate': srow['TradeDate'],
                    'batch_open_dt': batch_ts,
                })
                continue

            if len(matches) > 1:
                ambiguous_pairs.append({
                    'short_conid': srow['Conid'], 'short_strike': srow['Strike'],
                    'batch_open_dt': batch_ts,
                    'n_candidate_longs': len(matches),
                    'candidate_long_conids': [m[2]['Conid'] for m in matches],
                    'candidate_long_strikes': [m[2]['Strike'] for m in matches],
                })
                matches.sort(key=lambda m: m[1])

            lidx, dt_diff, lrow = matches[0]
            longs_used.add(lidx)
            paired_longs.append((lidx, dt_diff, lrow))

        if len(paired_longs) == 0:
            # entire short lifecycle unmatched to any long — record and skip
            continue

        combo_type = 'PutSpread' if srow['Put/Call'] == 'P' else 'CallSpread'
        short_close = srow['last_close_dt']

        long_total_pnl = sum(lr['total_fifo_pnl'] for _, _, lr in paired_longs)
        total_pnl = srow['total_fifo_pnl'] + long_total_pnl

        # closed-together check: use the LAST long leg to close (if multiple longs,
        # compare each to short close; report the max gap = most-decoupled leg)
        gaps = []
        for _, _, lr in paired_longs:
            lc = lr['last_close_dt']
            if pd.isna(short_close) or pd.isna(lc):
                continue
            gaps.append(abs((short_close - lc).total_seconds()) / 60.0)
        if gaps:
            max_gap = max(gaps)
            closed_together = max_gap <= PAIRED_EXIT_TOLERANCE_MIN
        else:
            max_gap = None
            closed_together = None

        multi = len(paired_longs) > 1

        combo_groups.append({
            'TradeDate': srow['TradeDate'],
            'ComboType': combo_type,
            'short_conid': srow['Conid'],
            'short_strike': srow['Strike'],
            'short_open_dt': srow['first_open_dt'],
            'short_open_price': srow['first_open_price'],
            'short_open_qty': srow['total_open_qty'],
            'short_n_open_batches': srow['n_open_batches'],
            'short_close_dt': short_close,
            'short_close_qty': srow['total_close_qty'],
            'short_fifo_pnl': srow['total_fifo_pnl'],
            'short_fully_closed': srow['fully_closed'],
            'n_paired_longs': len(paired_longs),
            'long_conids': [lr['Conid'] for _, _, lr in paired_longs],
            'long_strikes': [lr['Strike'] for _, _, lr in paired_longs],
            'long_open_dts': [str(lr['first_open_dt']) for _, _, lr in paired_longs],
            'long_open_prices': [lr['first_open_price'] for _, _, lr in paired_longs],
            'long_open_qtys': [lr['total_open_qty'] for _, _, lr in paired_longs],
            'long_close_dts': [str(lr['last_close_dt']) for _, _, lr in paired_longs],
            'long_fifo_pnls': [lr['total_fifo_pnl'] for _, _, lr in paired_longs],
            'long_total_fifo_pnl': long_total_pnl,
            'total_realized_pnl': total_pnl,
            'closed_together': closed_together,
            'exit_gap_minutes_max': max_gap,
            'short_pnl_disaggregated': not multi,  # False => short P&L is an aggregate over >1 paired long legs
            'ambiguous_pairing': any(True for _ in []),  # set below if any batch was ambiguous for this short
        })

    combo_df = pd.DataFrame(combo_groups)

    # mark ambiguous_pairing per combo row based on ambiguous_pairs short_conids
    if len(combo_df) > 0 and len(ambiguous_pairs) > 0:
        amb_conids = set(a['short_conid'] for a in ambiguous_pairs)
        combo_df['ambiguous_pairing'] = combo_df['short_conid'].isin(amb_conids)

    unmatched_df = pd.DataFrame(unmatched_batches)
    ambiguous_df = pd.DataFrame(ambiguous_pairs)

    # longs never claimed by any short batch (orphan long legs)
    unclaimed_longs = longs[~longs.index.isin(longs_used)]

    # shorts that matched NO long batches at all (fully unmatched short lifecycles).
    # NOTE: must key on (Conid, first_open_dt) lifecycle identity, not bare Conid --
    # the same Conid can have multiple distinct short lifecycles in a day (reopen
    # after flattening), and matching by Conid alone would wrongly mark an
    # unmatched lifecycle as "matched" just because a DIFFERENT lifecycle on the
    # same Conid succeeded.
    matched_short_keys = set((c['short_conid'], c['short_open_dt']) for c in combo_groups)
    shorts_keys = list(zip(shorts['Conid'], shorts['first_open_dt']))
    fully_unmatched_mask = [k not in matched_short_keys for k in shorts_keys]
    fully_unmatched_shorts = shorts[fully_unmatched_mask]

    return combo_df, unmatched_df, ambiguous_df, unclaimed_longs, fully_unmatched_shorts


def load_tat_tradelog():
    df = pd.read_excel(TAT_PATH, sheet_name="TAT-tradelog")
    return df


def load_balance():
    df = pd.read_excel(BALANCE_PATH, sheet_name="Daily_Ending_Balance")
    df = df.sort_values('ReportDate').reset_index(drop=True)
    df['balance_delta'] = df['Total'].diff()
    return df


def explode_combo_groups_to_pairs(combo_df):
    """
    Explode each combo-group row (1 short lifecycle, N paired long legs) into
    N individual short-long pair rows, one per paired long leg. This mirrors
    TAT's own granularity (TAT logs one row per short+long entry). For groups
    with N>1, the short_fifo_pnl shown on EACH exploded row is the SAME
    aggregate lifecycle-level value (not a per-leg split) — flagged via
    short_pnl_disaggregated == False so this is never mistaken for a true
    per-entry P&L.
    """
    rows = []
    for _, c in combo_df.iterrows():
        n = c['n_paired_longs']
        for i in range(n):
            rows.append({
                'TradeDate': c['TradeDate'],
                'ComboType': c['ComboType'],
                'short_conid': c['short_conid'],
                'short_strike': c['short_strike'],
                'short_open_dt': c['short_open_dt'],
                'short_open_price': c['short_open_price'],
                'short_n_open_batches': c['short_n_open_batches'],
                'short_close_dt': c['short_close_dt'],
                'short_fifo_pnl': c['short_fifo_pnl'],
                'n_paired_longs': n,
                'long_conid': c['long_conids'][i],
                'long_strike': c['long_strikes'][i],
                'long_open_dt': c['long_open_dts'][i],
                'long_open_price': c['long_open_prices'][i],
                'long_open_qty': c['long_open_qtys'][i],
                'long_close_dt': c['long_close_dts'][i],
                'long_fifo_pnl': c['long_fifo_pnls'][i],
                'total_realized_pnl_group': c['total_realized_pnl'],
                'short_pnl_disaggregated': c['short_pnl_disaggregated'],
                'closed_together': c['closed_together'],
                'exit_gap_minutes_max': c['exit_gap_minutes_max'],
                'ambiguous_pairing': c['ambiguous_pairing'],
            })
    return pd.DataFrame(rows)


def cross_check_tat(pair_df, tat_df, focus_year_month_prefixes=('202510',)):
    """
    Step 4: cross-check reconstructed short/long pairs against TAT-tradelog for
    a sample of days (at least all of October 2025 plus flagged high-vol days).
    Join by TradeDate + strikes + approx open time. Works on the exploded
    per-pair view so granularity matches TAT's own per-entry rows.
    """
    tat = tat_df.copy()
    tat['OpenDateStr'] = pd.to_datetime(tat['OpenDate']).dt.strftime('%Y%m%d')

    pair_df = pair_df.copy()
    pair_df['TradeDateStr'] = pair_df['TradeDate'].astype(str)

    focus_mask = pair_df['TradeDateStr'].str.startswith(focus_year_month_prefixes)
    focus_pairs = pair_df[focus_mask].copy()

    rows = []
    for _, c in focus_pairs.iterrows():
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
            rows.append({**c.to_dict(), 'tat_match': 'NO_MATCH', 'tat_ProfitLoss': None,
                         'tat_n_candidates': 0})
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
            **c.to_dict(),
            'tat_match': 'MATCHED' if len(candidates) == 1 else 'AMBIGUOUS_MULTI_CANDIDATE',
            'tat_n_candidates': len(candidates),
            'tat_ProfitLoss': best['ProfitLoss'],
            'tat_Status': best['Status'],
            'tat_OpenTime': best['OpenTime'],
            'tat_CloseTime': best['CloseTime'],
            'pnl_discrepancy': c['total_realized_pnl_group'] - best['ProfitLoss'],
        })

    return pd.DataFrame(rows)


def validate_against_balance(combo_df, fully_unmatched_shorts, unclaimed_longs, balance_df):
    """
    Step 5: sum reconstructed P&L by day, compare to daily balance deltas.
    Only overlapping days.

    IMPORTANT: the day's reconstructed P&L must include ALL reconstructed
    FifoPnlRealized for that day -- combo groups (paired short+long) PLUS
    any short lifecycle that could not be paired to a long leg (fully_unmatched
    shorts) PLUS any long lifecycle never claimed by a short (unclaimed_longs).
    Excluding those legitimately-reconstructed-but-unpaired P&L amounts from
    the day's total is not "conservative", it's simply missing real dollars
    that occurred that day (verified: combo-only totals mismatch by
    ~six figures; once unmatched-short and unclaimed-long P&L is added back,
    mean daily mismatch drops to the same ballpark as the lifecycle-level
    reconciliation, ~$32/day).
    """
    combo_df = combo_df.copy()
    combo_df['TradeDate'] = combo_df['TradeDate'].astype(int)
    combo_daily = combo_df.groupby('TradeDate')['total_realized_pnl'].sum()

    fus = fully_unmatched_shorts.copy()
    fus['TradeDate'] = fus['TradeDate'].astype(int)
    fus_daily = fus.groupby('TradeDate')['total_fifo_pnl'].sum()

    ul = unclaimed_longs.copy()
    ul['TradeDate'] = ul['TradeDate'].astype(int)
    ul_daily = ul.groupby('TradeDate')['total_fifo_pnl'].sum()

    full_daily = combo_daily.add(fus_daily, fill_value=0).add(ul_daily, fill_value=0)
    full_daily = full_daily.reset_index()
    full_daily.columns = ['ReportDate', 'reconstructed_pnl']

    merged = pd.merge(balance_df, full_daily, on='ReportDate', how='inner')
    merged['mismatch'] = merged['balance_delta'] - merged['reconstructed_pnl']
    merged['abs_mismatch'] = merged['mismatch'].abs()

    return merged


def characterize_exits(combo_df, pair_df):
    """
    Step 6: exit-rule characterization using only reconstructed combo data.

    Uses combo_df (one row per short lifecycle / combo-group) for the
    paired-vs-decoupled split (that's a property of the group as a whole:
    did the short close within PAIRED_EXIT_TOLERANCE_MIN of every paired
    long's close). Uses the exploded pair_df for the per-long-leg P&L-
    multiple distribution, since that's naturally a per-leg quantity.
    """
    total = len(combo_df)
    paired = combo_df['closed_together'] == True
    decoupled = combo_df['closed_together'] == False
    unknown = combo_df['closed_together'].isna()

    n_paired = int(paired.sum())
    n_decoupled = int(decoupled.sum())
    n_unknown = int(unknown.sum())

    decoupled_conids = set(combo_df.loc[decoupled, 'short_conid'])
    decoupled_pairs = pair_df[pair_df['short_conid'].isin(decoupled_conids)].copy()

    decoupled_pairs['long_entry_cost'] = decoupled_pairs['long_open_price'] * decoupled_pairs['long_open_qty'] * 100
    decoupled_pairs['long_pnl_multiple'] = decoupled_pairs.apply(
        lambda r: (r['long_fifo_pnl'] / r['long_entry_cost']) if r['long_entry_cost'] not in (0, None) and pd.notna(r['long_entry_cost']) else np.nan,
        axis=1
    )

    return {
        'total_combos': total,
        'n_paired': n_paired,
        'n_decoupled': n_decoupled,
        'n_unknown_close_status': n_unknown,
        'decoupled_df': decoupled_pairs,
    }


def main():
    print("Loading BIC execution data...")
    bic_df, n_total, n_0dte, non_0dte, orphan_rows = load_bic_executions()
    print(f"  total rows: {n_total}, 0DTE rows kept: {n_0dte}, excluded non-0DTE: {len(non_0dte)}, "
          f"excluded orphan-close rows (same-Conid tail of excluded side book): {len(orphan_rows)} "
          f"(FifoPnlRealized {orphan_rows['FifoPnlRealized'].sum():,.2f})")

    print("Reconstructing per-Conid lifecycles...")
    lc_df, amb_lifecycle_df = reconstruct_lifecycles(bic_df)
    print(f"  lifecycles: {len(lc_df)}, ambiguous (never closed in window): {len(amb_lifecycle_df)}")

    print("Pairing combos...")
    combo_df, unmatched_df, ambiguous_pair_df, unclaimed_longs, fully_unmatched_shorts = pair_combos(lc_df)
    print(f"  combo groups: {len(combo_df)}, unmatched short open-batches: {len(unmatched_df)}, "
          f"ambiguous batch pairings: {len(ambiguous_pair_df)}, unclaimed longs: {len(unclaimed_longs)}, "
          f"fully-unmatched short lifecycles: {len(fully_unmatched_shorts)}")
    multi_long_groups = (combo_df['n_paired_longs'] > 1).sum()
    print(f"  combo groups with >1 paired long leg (short P&L NOT disaggregated below group level): {multi_long_groups}")

    print("Exploding combo groups into per-leg pair rows (TAT-comparable granularity)...")
    pair_df = explode_combo_groups_to_pairs(combo_df)
    print(f"  exploded pair rows: {len(pair_df)}")

    print("Loading TAT tradelog + balance for cross-check/validation...")
    tat_df = load_tat_tradelog()
    balance_df = load_balance()

    tat_cross = cross_check_tat(pair_df, tat_df, focus_year_month_prefixes=('202510',))
    validation = validate_against_balance(combo_df, fully_unmatched_shorts, unclaimed_longs, balance_df)
    exit_stats = characterize_exits(combo_df, pair_df)

    print("Writing combo_ledger.csv (one row per combo GROUP: 1 short lifecycle + its paired long legs)...")
    ledger_cols = [
        'TradeDate', 'ComboType',
        'short_conid', 'short_strike', 'short_open_dt', 'short_open_price', 'short_open_qty',
        'short_n_open_batches', 'short_close_dt', 'short_close_qty', 'short_fifo_pnl', 'short_fully_closed',
        'n_paired_longs', 'long_conids', 'long_strikes', 'long_open_dts', 'long_open_prices',
        'long_open_qtys', 'long_close_dts', 'long_fifo_pnls', 'long_total_fifo_pnl',
        'total_realized_pnl', 'closed_together', 'exit_gap_minutes_max',
        'short_pnl_disaggregated', 'ambiguous_pairing',
    ]
    combo_df[ledger_cols].to_csv(LEDGER_PATH, index=False)
    print(f"  wrote {len(combo_df)} rows to {LEDGER_PATH}")

    # also persist intermediate artifacts for the notes-writer
    orphan_rows.to_csv(OUT_DIR / "excluded_orphan_close_rows.csv", index=False)
    non_0dte.to_csv(OUT_DIR / "excluded_non_0dte_rows.csv", index=False)
    amb_lifecycle_df.to_csv(OUT_DIR / "ambiguous_lifecycles.csv", index=False)
    unmatched_df.to_csv(OUT_DIR / "unmatched_short_open_batches.csv", index=False)
    ambiguous_pair_df.to_csv(OUT_DIR / "ambiguous_combo_pairings.csv", index=False)
    unclaimed_longs.to_csv(OUT_DIR / "unclaimed_long_legs.csv", index=False)
    fully_unmatched_shorts.to_csv(OUT_DIR / "fully_unmatched_short_lifecycles.csv", index=False)
    pair_df.to_csv(OUT_DIR / "exploded_pair_ledger.csv", index=False)
    tat_cross.to_csv(OUT_DIR / "tat_crosscheck_oct2025.csv", index=False)
    validation.to_csv(OUT_DIR / "balance_validation.csv", index=False)
    exit_stats['decoupled_df'].to_csv(OUT_DIR / "decoupled_long_legs.csv", index=False)

    combo_pnl = combo_df['total_realized_pnl'].sum()
    unmatched_short_pnl = fully_unmatched_shorts['total_fifo_pnl'].sum()
    unclaimed_long_pnl = unclaimed_longs['total_fifo_pnl'].sum()
    grand_total_pnl = combo_pnl + unmatched_short_pnl + unclaimed_long_pnl
    print(f"\nPaired-combo P&L (successfully matched short+long groups): {combo_pnl:,.2f}")
    print(f"Fully-unmatched short-lifecycle P&L (no long leg found at all): {unmatched_short_pnl:,.2f}")
    print(f"Unclaimed long-lifecycle P&L (no short leg found at all): {unclaimed_long_pnl:,.2f}")
    print(f"GRAND TOTAL reconstructed P&L across the whole 0DTE window: {grand_total_pnl:,.2f}")
    print(f"Validation: total abs mismatch = {validation['abs_mismatch'].sum():,.2f} over {len(validation)} days")
    flagged = validation[validation['abs_mismatch'] > 200]
    print(f"Days with mismatch > $200: {len(flagged)}")

    return {
        'bic_df': bic_df, 'lc_df': lc_df, 'combo_df': combo_df, 'pair_df': pair_df,
        'unmatched_df': unmatched_df, 'ambiguous_pair_df': ambiguous_pair_df,
        'unclaimed_longs': unclaimed_longs, 'fully_unmatched_shorts': fully_unmatched_shorts,
        'tat_cross': tat_cross,
        'validation': validation, 'exit_stats': exit_stats,
        'amb_lifecycle_df': amb_lifecycle_df,
    }


if __name__ == '__main__':
    main()
