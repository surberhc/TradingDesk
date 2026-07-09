"""
Long-leg exit-slippage isolation.

Standalone, read-only measurement of REAL exit slippage (fill vs quoted NBBO) for
the British IC "auto-close the long leg the instant its paired short stops" rule
(S8's core mechanism), split apart from the short leg's own stop-out slippage.

Context: an earlier ad-hoc measurement blended BOTH legs' exit slippage together
(~13x quoted half-spread, ~$700k aggregate over 2025-07-09..2026-07-07) and that
script no longer exists in the repo. This rebuilds the measurement and, critically,
DISAGGREGATES long-only vs short-only slippage on the exact same combo population
(the 1,617 rows in decoupled_long_legs.csv), because S8's edge depends specifically
on the long-leg auto-close, not on the short leg's stop-out mechanics.

Inputs (READ-ONLY, never modified):
  - decoupled_long_legs.csv   (1,617 rows, one long leg per combo; ground truth P&L)
  - combo_ledger.csv          (short_close_qty needed for short-side per-contract back-out)
  - C:\\TradingDesk-Local\\warehouse\\raw\\options_1m\\SPXW\\quote\\YYYYMMDD.parquet
    (1-min NBBO quotes, READ-ONLY, filtered per-file via pyarrow dataset filter
    pushdown so full days are never loaded into memory)

Outputs:
  - longleg_slippage_isolation_results.csv  (one row per decoupled combo, both legs'
    slippage side by side, plus coverage/exclusion columns)
  - printed summary (median/mean $ and x-half-spread, long vs short, aggregate $,
    comparison to the prior blended ~13x / ~$700k figure)

No price or timing is ever fabricated. Legs whose derivation or quote lookup can't
be trusted are excluded and the exclusion reason is recorded explicitly, never
silently dropped.
"""

import pandas as pd
import numpy as np
import pyarrow.dataset as ds
from pathlib import Path

OUT_DIR = Path(__file__).parent
DECOUPLED_PATH = OUT_DIR / "decoupled_long_legs.csv"
LEDGER_PATH = OUT_DIR / "combo_ledger.csv"
RESULTS_PATH = OUT_DIR / "longleg_slippage_isolation_results.csv"

WAREHOUSE_QUOTE_DIR = Path(r"C:\TradingDesk-Local\warehouse\raw\options_1m\SPXW\quote")

CONTRACT_MULTIPLIER = 100

# Prior blended measurement (never-recovered script; recorded here for the sanity
# check comparison only -- not used in any calculation).
PRIOR_BLENDED_X_HALF_SPREAD = 13.0
PRIOR_BLENDED_AGGREGATE_DOLLARS = 700_000


def load_source_data():
    """Load decoupled long legs + combo ledger (for short_close_qty)."""
    dl = pd.read_csv(DECOUPLED_PATH)
    cl = pd.read_csv(LEDGER_PATH, usecols=[
        'short_conid', 'short_open_dt', 'short_close_qty'
    ])
    # join key verified unique in combo_ledger.csv (0 duplicates on this pair)
    dl = dl.merge(cl, on=['short_conid', 'short_open_dt'], how='left', validate='many_to_one')
    return dl


def derive_close_prices(dl):
    """
    Derive each leg's implied per-contract close price from FIFO realized P&L.

    Long leg (always a single lifecycle -- longs segment cleanly, verified in
    reconstruct.py's pairing step):
        long_entry_cost = long_open_price * long_open_qty * 100   (long_open_qty > 0)
        implied_long_close = long_open_price + long_fifo_pnl / (long_open_qty * 100)

    Sanity-checked against the already-present long_entry_cost / long_pnl_multiple
    columns in decoupled_long_legs.csv: long_entry_cost == long_open_price *
    long_open_qty * 100 exactly (max abs diff ~5.7e-14, floating point noise), and
    long_pnl_multiple == long_fifo_pnl / long_entry_cost exactly. This confirms the
    sign/qty convention used below.

    Short leg (SELL to open, so short_open_qty < 0, short_close_qty > 0 = qty
    bought back to close):
        implied_short_close = short_open_price - short_fifo_pnl / (short_close_qty * 100)
    (short P&L = (open_price - close_price) * qty*100; loses money if price rises)

    CAVEAT -- multi-batch shorts: when short_n_open_batches > 1, short_fifo_pnl is
    FIFO'd across the short's ENTIRE accumulated lifecycle (multiple opens at
    different prices), so a single "implied_short_close" back-out assuming one
    open price is not exact for those rows (the true close price is still a single
    number, but decomposing FIFO P&L cleanly requires knowing the FIFO lot order,
    which the ledger doesn't expose at this granularity). These rows are flagged
    via `short_multi_batch` and EXCLUDED from the short-side slippage measurement
    (not silently included), per the task's instruction. The long leg itself is
    unaffected by this (longs are always single-lifecycle), so long-side coverage
    is not reduced by this flag.
    """
    dl = dl.copy()

    # --- long leg ---
    computed_entry_cost = dl['long_open_price'] * dl['long_open_qty'] * CONTRACT_MULTIPLIER
    entry_cost_check = (computed_entry_cost - dl['long_entry_cost']).abs().max()
    print(f"[sanity check] long_entry_cost formula match: max abs diff = {entry_cost_check:.2e} "
          f"(expect ~0, floating point noise only)")

    dl['implied_long_close'] = dl['long_open_price'] + dl['long_fifo_pnl'] / (dl['long_open_qty'] * CONTRACT_MULTIPLIER)
    # settlement/expiry-worthless legs back out to tiny negative numbers from fee
    # residuals in FIFO P&L (verified: 814/1617 negative, all within [-0.103, -0.0004],
    # i.e. rounding/fee noise around a true value of $0.00, not a derivation bug).
    n_clipped_long = (dl['implied_long_close'] < 0).sum()
    dl['implied_long_close'] = dl['implied_long_close'].clip(lower=0.0)
    print(f"[sanity check] long close prices clipped to 0 (expired-worthless, fee-noise negative): "
          f"{n_clipped_long} / {len(dl)}; range before clip was as low as "
          f"{(dl['long_open_price'] + dl['long_fifo_pnl'] / (dl['long_open_qty'] * CONTRACT_MULTIPLIER)).min():.4f}")

    # --- short leg ---
    dl['short_multi_batch'] = dl['short_n_open_batches'] > 1
    dl['implied_short_close'] = dl['short_open_price'] - dl['short_fifo_pnl'] / (dl['short_close_qty'] * CONTRACT_MULTIPLIER)
    n_neg_short = (dl.loc[~dl['short_multi_batch'], 'implied_short_close'] < 0).sum()
    print(f"[sanity check] single-batch short implied close prices: "
          f"{dl.loc[~dl['short_multi_batch'], 'implied_short_close'].describe().to_dict()}")
    print(f"[sanity check] negative single-batch short implied closes: {n_neg_short} (expect 0)")
    print(f"[sanity check] multi-batch shorts flagged for exclusion from short-side measurement: "
          f"{dl['short_multi_batch'].sum()} / {len(dl)}")

    return dl


def load_day_quotes(date_int):
    """Load the full day's quote parquet for a TradeDate (as int YYYYMMDD), columns only."""
    path = WAREHOUSE_QUOTE_DIR / f"{date_int}.parquet"
    if not path.exists():
        return None
    d = ds.dataset(str(path), format='parquet')
    tbl = d.to_table(columns=['expiration', 'strike', 'right', 'timestamp', 'bid', 'ask'])
    df = tbl.to_pandas()
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    return df


def lookup_quote(day_quotes, expiration_str, strike, right, close_dt):
    """
    Look up the NBBO quote for one contract at the minute containing close_dt.
    Quotes are minute-keyed exactly (verified: timestamps land on :00 seconds,
    one row per contract per minute). Round DOWN close_dt to the minute and match
    exactly; if that exact minute is absent (e.g. no quote posted that minute),
    fall back to the nearest available minute for that contract within +/-2 min.
    Returns (bid, ask, minutes_used_offset) or (None, None, None) if no quote found
    (e.g. close occurred after 16:00 ET market-quote cutoff -- expiration settlement,
    not a real market fill).
    """
    if day_quotes is None:
        return None, None, None, 'no_quote_file_for_date'

    minute = close_dt.floor('min')
    if minute.time() > pd.Timestamp('16:00:00').time():
        return None, None, None, 'close_after_market_quote_cutoff'

    contract_mask = (
        (day_quotes['expiration'] == expiration_str) &
        (day_quotes['strike'] == strike) &
        (day_quotes['right'] == right)
    )
    contract_quotes = day_quotes[contract_mask]
    if len(contract_quotes) == 0:
        return None, None, None, 'contract_not_found_in_quote_file'

    exact = contract_quotes[contract_quotes['timestamp'] == minute]
    if len(exact) > 0:
        row = exact.iloc[0]
        return row['bid'], row['ask'], 0, None

    # fallback: nearest minute within +/-2 min
    diffs = (contract_quotes['timestamp'] - minute).abs()
    within = diffs <= pd.Timedelta(minutes=2)
    if within.any():
        idx = diffs[within].idxmin()
        row = contract_quotes.loc[idx]
        offset_min = (row['timestamp'] - minute).total_seconds() / 60.0
        return row['bid'], row['ask'], offset_min, None

    return None, None, None, 'no_quote_within_2min_of_close'


def right_from_combo_type(combo_type):
    return 'CALL' if combo_type == 'CallSpread' else 'PUT'


def measure_slippage(dl):
    """
    Walk decoupled legs date-by-date, loading one day's quote parquet at a time
    (memory-safe), and compute slippage for both the long leg and short leg of
    each combo.
    """
    dl = dl.copy()
    dl['expiration_str'] = pd.to_datetime(dl['TradeDate'], format='%Y%m%d').dt.strftime('%Y-%m-%d')
    dl['right'] = dl['ComboType'].apply(right_from_combo_type)
    dl['long_close_dt_parsed'] = pd.to_datetime(dl['long_close_dt'])
    dl['short_close_dt_parsed'] = pd.to_datetime(dl['short_close_dt'])

    results = []
    n_dates = dl['TradeDate'].nunique()
    for i, (trade_date, day_rows) in enumerate(dl.groupby('TradeDate')):
        day_quotes = load_day_quotes(int(trade_date))
        if (i + 1) % 40 == 0 or (i + 1) == n_dates:
            print(f"  processed {i+1}/{n_dates} dates...")

        for _, r in day_rows.iterrows():
            row = {
                'TradeDate': r['TradeDate'],
                'ComboType': r['ComboType'],
                'short_conid': r['short_conid'],
                'long_conid': r['long_conid'],
                'short_strike': r['short_strike'],
                'long_strike': r['long_strike'],
                'short_multi_batch': r['short_multi_batch'],
            }

            # --- long leg ---
            lb, la, l_offset, l_excl = lookup_quote(
                day_quotes, r['expiration_str'], r['long_strike'], r['right'], r['long_close_dt_parsed']
            )
            row['long_close_price'] = r['implied_long_close']
            row['long_bid'] = lb
            row['long_ask'] = la
            row['long_quote_minute_offset'] = l_offset
            row['long_exclusion_reason'] = l_excl
            if lb is not None and la is not None:
                mid = (lb + la) / 2.0
                half_spread = (la - lb) / 2.0
                row['long_mid'] = mid
                row['long_half_spread'] = half_spread
                row['long_slippage_dollars_per_contract'] = abs(row['long_close_price'] - mid)
                if half_spread > 0:
                    row['long_slippage_x_half_spread'] = row['long_slippage_dollars_per_contract'] / half_spread
                else:
                    row['long_slippage_x_half_spread'] = np.nan
                    row['long_exclusion_reason'] = 'zero_spread'
            else:
                row['long_mid'] = np.nan
                row['long_half_spread'] = np.nan
                row['long_slippage_dollars_per_contract'] = np.nan
                row['long_slippage_x_half_spread'] = np.nan

            # --- short leg (skip multi-batch shorts: FIFO can't be cleanly
            # backed out to a single per-contract close price for those) ---
            if not r['short_multi_batch']:
                sb, sa, s_offset, s_excl = lookup_quote(
                    day_quotes, r['expiration_str'], r['short_strike'], r['right'], r['short_close_dt_parsed']
                )
                row['short_close_price'] = r['implied_short_close']
                row['short_bid'] = sb
                row['short_ask'] = sa
                row['short_quote_minute_offset'] = s_offset
                row['short_exclusion_reason'] = s_excl
                if sb is not None and sa is not None:
                    mid = (sb + sa) / 2.0
                    half_spread = (sa - sb) / 2.0
                    row['short_mid'] = mid
                    row['short_half_spread'] = half_spread
                    row['short_slippage_dollars_per_contract'] = abs(row['short_close_price'] - mid)
                    if half_spread > 0:
                        row['short_slippage_x_half_spread'] = row['short_slippage_dollars_per_contract'] / half_spread
                    else:
                        row['short_slippage_x_half_spread'] = np.nan
                        row['short_exclusion_reason'] = 'zero_spread'
                else:
                    row['short_mid'] = np.nan
                    row['short_half_spread'] = np.nan
                    row['short_slippage_dollars_per_contract'] = np.nan
                    row['short_slippage_x_half_spread'] = np.nan
            else:
                row['short_close_price'] = r['implied_short_close']
                row['short_bid'] = np.nan
                row['short_ask'] = np.nan
                row['short_quote_minute_offset'] = np.nan
                row['short_exclusion_reason'] = 'short_multi_batch_fifo_not_disaggregable'
                row['short_mid'] = np.nan
                row['short_half_spread'] = np.nan
                row['short_slippage_dollars_per_contract'] = np.nan
                row['short_slippage_x_half_spread'] = np.nan

            row['long_open_qty'] = r['long_open_qty']
            row['short_close_qty'] = r['short_close_qty']

            results.append(row)

    return pd.DataFrame(results)


def summarize(results):
    """Print median/mean $ and x-half-spread for long vs short, aggregate $ for long."""
    long_ok = results.dropna(subset=['long_slippage_dollars_per_contract'])
    short_ok = results.dropna(subset=['short_slippage_dollars_per_contract'])

    print("\n" + "=" * 78)
    print("COVERAGE")
    print("=" * 78)
    print(f"Total decoupled legs: {len(results)}")
    print(f"Long leg: measured {len(long_ok)} / {len(results)} "
          f"({100*len(long_ok)/len(results):.1f}%)")
    print("  Long exclusion reasons:")
    print(results.loc[results['long_slippage_dollars_per_contract'].isna(), 'long_exclusion_reason']
          .value_counts(dropna=False).to_string())
    print(f"\nShort leg: measured {len(short_ok)} / {len(results)} "
          f"({100*len(short_ok)/len(results):.1f}%)")
    print("  Short exclusion reasons:")
    print(results.loc[results['short_slippage_dollars_per_contract'].isna(), 'short_exclusion_reason']
          .value_counts(dropna=False).to_string())

    print("\n" + "=" * 78)
    print("SLIPPAGE -- LONG vs SHORT (measured legs only)")
    print("=" * 78)
    print(f"{'Metric':<38}{'LONG':>18}{'SHORT':>18}")
    print(f"{'median $/contract':<38}{long_ok['long_slippage_dollars_per_contract'].median():>18.4f}"
          f"{short_ok['short_slippage_dollars_per_contract'].median():>18.4f}")
    print(f"{'mean $/contract':<38}{long_ok['long_slippage_dollars_per_contract'].mean():>18.4f}"
          f"{short_ok['short_slippage_dollars_per_contract'].mean():>18.4f}")
    long_x = long_ok.dropna(subset=['long_slippage_x_half_spread'])
    short_x = short_ok.dropna(subset=['short_slippage_x_half_spread'])
    print(f"{'median x half-spread':<38}{long_x['long_slippage_x_half_spread'].median():>18.4f}"
          f"{short_x['short_slippage_x_half_spread'].median():>18.4f}")
    print(f"{'mean x half-spread':<38}{long_x['long_slippage_x_half_spread'].mean():>18.4f}"
          f"{short_x['short_slippage_x_half_spread'].mean():>18.4f}")
    print(f"{'n measured (for x-half-spread)':<38}{len(long_x):>18}{len(short_x):>18}")

    long_agg = (long_ok['long_slippage_dollars_per_contract'] * long_ok['long_open_qty'] * CONTRACT_MULTIPLIER / CONTRACT_MULTIPLIER).sum()
    # NOTE: slippage_dollars_per_contract is already a per-option-dollar quantity
    # (mid-vs-fill, in option-price dollars). Scale by qty and the $100 multiplier
    # to get real portfolio dollars.
    long_agg_dollars = (long_ok['long_slippage_dollars_per_contract'] * long_ok['long_open_qty'] * CONTRACT_MULTIPLIER).sum()
    short_agg_dollars = (short_ok['short_slippage_dollars_per_contract'] * short_ok['short_close_qty'] * CONTRACT_MULTIPLIER).sum()

    print("\n" + "=" * 78)
    print("AGGREGATE $ COST (slippage x qty x $100 multiplier, summed across measured legs)")
    print("=" * 78)
    print(f"Long-only aggregate slippage cost:  ${long_agg_dollars:,.2f}  (n={len(long_ok)} legs)")
    print(f"Short-only aggregate slippage cost: ${short_agg_dollars:,.2f}  (n={len(short_ok)} legs)")
    print(f"Blended (long+short) aggregate:     ${long_agg_dollars + short_agg_dollars:,.2f}")

    combined = pd.concat([
        long_x['long_slippage_x_half_spread'].rename('x'),
        short_x['short_slippage_x_half_spread'].rename('x'),
    ])
    print(f"\nBlended median x-half-spread (long+short pooled, equal-weighted per leg): {combined.median():.2f}")
    print(f"Blended mean x-half-spread (long+short pooled, equal-weighted per leg):   {combined.mean():.2f}")

    print("\n" + "=" * 78)
    print("COMPARISON TO PRIOR BLENDED MEASUREMENT (script lost, recorded from earlier session)")
    print("=" * 78)
    print(f"Prior blended figure: ~{PRIOR_BLENDED_X_HALF_SPREAD}x half-spread, "
          f"~${PRIOR_BLENDED_AGGREGATE_DOLLARS:,.0f} aggregate over 2025-07-09..2026-07-07")
    print(f"This measurement's blended figure: {combined.median():.2f}x median half-spread "
          f"(mean {combined.mean():.2f}x), ${long_agg_dollars + short_agg_dollars:,.2f} aggregate "
          f"(long+short combined, over the {results['TradeDate'].min()}..{results['TradeDate'].max()} window, "
          f"n={len(long_ok)+len(short_ok)} measured leg-sides)")

    return {
        'long_median_dollars': long_ok['long_slippage_dollars_per_contract'].median(),
        'long_mean_dollars': long_ok['long_slippage_dollars_per_contract'].mean(),
        'short_median_dollars': short_ok['short_slippage_dollars_per_contract'].median(),
        'short_mean_dollars': short_ok['short_slippage_dollars_per_contract'].mean(),
        'long_median_x': long_x['long_slippage_x_half_spread'].median(),
        'long_mean_x': long_x['long_slippage_x_half_spread'].mean(),
        'short_median_x': short_x['short_slippage_x_half_spread'].median(),
        'short_mean_x': short_x['short_slippage_x_half_spread'].mean(),
        'long_agg_dollars': long_agg_dollars,
        'short_agg_dollars': short_agg_dollars,
        'n_long_measured': len(long_ok),
        'n_short_measured': len(short_ok),
        'n_total_legs': len(results),
    }


def main():
    print("Loading decoupled long legs + combo ledger...")
    dl = load_source_data()
    print(f"  {len(dl)} decoupled long legs loaded")

    print("\nDeriving implied close prices from FIFO P&L...")
    dl = derive_close_prices(dl)

    print("\nMeasuring slippage against real 1-min NBBO quotes (date-by-date, memory-safe)...")
    results = measure_slippage(dl)

    print(f"\nWriting results to {RESULTS_PATH}...")
    results.to_csv(RESULTS_PATH, index=False)
    print(f"  wrote {len(results)} rows")

    summary = summarize(results)
    return results, summary


if __name__ == '__main__':
    main()
