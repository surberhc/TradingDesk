import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd, numpy as np
import s8_mechanical_simulator as sim

t0 = time.perf_counter()

ledger = pd.read_csv("combo_ledger_tat_joined.csv")
ledger = ledger[(ledger['tat_match']=='MATCHED') & (ledger['short_fully_closed']==True) & ledger['short_close_dt'].notna()].copy()
ledger['template_clean'] = ledger['tat_Template'].astype(str).str.strip()
ledger['close_dt'] = pd.to_datetime(ledger['short_close_dt'])
ledger['close_hhmm'] = ledger['close_dt'].dt.strftime('%H:%M')
ledger['close_hour_min'] = ledger['close_dt'].dt.hour + ledger['close_dt'].dt.minute/60.0

rows = []
n_dates = ledger['TradeDate'].nunique()
print(f"Processing {len(ledger)} closed matched trades across {n_dates} distinct dates...", flush=True)

for di, (date_int, grp) in enumerate(ledger.groupby('TradeDate')):
    date_str = str(int(date_int))
    try:
        ohlc0, quote0 = sim._load_day(date_str)
    except Exception:
        continue
    if quote0.empty:
        continue
    for _, r in grp.iterrows():
        key = None
        for k, t in sim.TEMPLATES.items():
            if t.name.strip() == r['template_clean']:
                key = k
                break
        if key is None:
            continue
        template = sim.TEMPLATES[key]
        right = template.side
        strike = r['short_strike']
        qty = abs(r['short_open_qty'])
        if qty == 0:
            continue
        real_close_price = r['short_open_price'] - (r['short_fifo_pnl']/qty)/100.0

        close_ts = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}T{r['close_hhmm']}:00.000"
        q = sim._quote_at(quote0, close_ts, right, strike)
        if q is None:
            continue
        mid, half_spread, _ = q
        if half_spread is None or half_spread <= 0:
            continue

        slippage = real_close_price - mid  # positive = paid above mid (adverse, buying back)
        slippage_mult = slippage / half_spread

        rows.append(dict(
            date=date_str, template=r['template_clean'], close_hhmm=r['close_hhmm'],
            close_hour_min=r['close_hour_min'],
            real_close_price=real_close_price, quoted_mid=mid, half_spread=half_spread,
            slippage=slippage, slippage_mult=slippage_mult,
        ))
    if di % 25 == 0:
        print(f"  ...{di}/{n_dates} dates, {len(rows)} trades so far, {time.perf_counter()-t0:.0f}s elapsed", flush=True)

out = pd.DataFrame(rows)
out.to_csv("s8_real_slippage_check_results.csv", index=False)
print(f"\nDone. {len(out)} trades with valid quotes at real close time, {time.perf_counter()-t0:.1f}s. Wrote s8_real_slippage_check_results.csv", flush=True)

out['early_close'] = out['close_hour_min'] < 15.833  # < 15:50 ET proxy for "stopped early" vs EOD/settlement-like

print("\n=== REAL SLIPPAGE MULTIPLE (paid vs mid, in half-spread units) ===")
print("ALL closes:")
print(f"  n={len(out)}  mean={out['slippage_mult'].mean():.2f}x  median={out['slippage_mult'].median():.2f}x  p90={out['slippage_mult'].quantile(0.9):.2f}x  p99={out['slippage_mult'].quantile(0.99):.2f}x")

for grp_name, grp in out.groupby('early_close'):
    label = "EARLY closes (<15:50 ET, stop-like)" if grp_name else "LATE closes (>=15:50 ET, EOD/settlement-like)"
    print(f"\n{label}: n={len(grp)}")
    print(f"  mean={grp['slippage_mult'].mean():.2f}x  median={grp['slippage_mult'].median():.2f}x  p75={grp['slippage_mult'].quantile(0.75):.2f}x  p90={grp['slippage_mult'].quantile(0.9):.2f}x  max={grp['slippage_mult'].max():.2f}x")
    print(f"  mean $ slippage/contract: {grp['slippage'].mean():.3f}  median: {grp['slippage'].median():.3f}")

print("\n=== Distribution of close times among EARLY closes (to sanity check the 15:50 cutoff) ===")
early = out[out['early_close']]
print(early['close_hhmm'].value_counts().sort_index().head(20))
