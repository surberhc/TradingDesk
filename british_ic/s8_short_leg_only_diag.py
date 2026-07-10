import sys, time
sys.path.insert(0, r"C:\Users\andre\My Drive (andrew@surberhc.com)\TradingDesk\british_ic")
import pandas as pd, numpy as np
import s8_mechanical_simulator as sim
from dataclasses import asdict

t0 = time.perf_counter()

ledger = pd.read_csv("combo_ledger_tat_joined.csv")
ledger = ledger[ledger['tat_match']=='MATCHED'].copy()
ledger['template_clean'] = ledger['tat_Template'].astype(str).str.strip()
ledger['entry_hhmm'] = pd.to_datetime(ledger['short_open_dt']).dt.strftime('%H:%M')
name_to_key = {t.name.strip(): k for k, t in sim.TEMPLATES.items()}

rows = []
n_dates = ledger['TradeDate'].nunique()
print(f"Processing {len(ledger)} matched trades across {n_dates} distinct dates...", flush=True)

for di, (date_int, grp) in enumerate(ledger.groupby('TradeDate')):
    date_str = str(int(date_int))
    try:
        ohlc0, quote0 = sim._load_day(date_str)
    except Exception as e:
        continue
    if quote0.empty:
        continue
    for _, r in grp.iterrows():
        key = name_to_key.get(r['template_clean'])
        if key is None:
            continue
        template = sim.TEMPLATES[key]
        entry_hhmm = r['entry_hhmm']
        ts = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}T{entry_hhmm}:00.000"
        spot = sim.estimate_spot(quote0, ts)
        if np.isnan(spot):
            continue
        result = sim.simulate_trade(quote0, date_str, template, entry_hhmm, spot)
        if result is None:
            continue
        rd = asdict(result)
        real_qty = abs(r['short_open_qty'])
        if real_qty == 0:
            continue
        real_short_pnl_per_spread = r['short_fifo_pnl'] / real_qty
        sim_short_pnl_per_spread = (rd['short_entry_mid'] - rd['short_exit_price']) * 100.0
        rows.append(dict(
            date=date_str, template=r['template_clean'], entry_hhmm=entry_hhmm,
            exit_reason=rd['exit_reason'],
            real_short_pnl=real_short_pnl_per_spread, sim_short_pnl=sim_short_pnl_per_spread,
            diff=sim_short_pnl_per_spread - real_short_pnl_per_spread,
            real_qty=real_qty,
        ))
    if di % 25 == 0:
        print(f"  ...{di}/{n_dates} dates, {len(rows)} trades so far, {time.perf_counter()-t0:.0f}s elapsed", flush=True)

out = pd.DataFrame(rows)
out.to_csv("s8_short_leg_only_results.csv", index=False)
print(f"\nDone. {len(out)} trades processed in {time.perf_counter()-t0:.1f}s. Wrote s8_short_leg_only_results.csv", flush=True)

print("\n=== SHORT-LEG-ONLY AGGREGATE ===")
print(f"real sum: {out['real_short_pnl'].sum():.1f}   sim sum: {out['sim_short_pnl'].sum():.1f}")
print(f"real mean: {out['real_short_pnl'].mean():.2f}   sim mean: {out['sim_short_pnl'].mean():.2f}")

print("\n=== BY EXIT REASON ===")
print(out.groupby('exit_reason')['diff'].agg(['count','mean','median']).round(1))

print("\n=== DIFF DISTRIBUTION ===")
print(f"mean abs diff: {out['diff'].abs().mean():.2f}   median abs diff: {out['diff'].abs().median():.2f}")
sign_flip = ((out['real_short_pnl']>0) & (out['sim_short_pnl']<0)) | ((out['real_short_pnl']<0) & (out['sim_short_pnl']>0))
print(f"sign flips: {sign_flip.sum()}/{len(out)} = {100*sign_flip.mean():.1f}%")

print("\n=== BY TEMPLATE ===")
print(out.groupby('template').agg(n=('diff','size'), real_sum=('real_short_pnl','sum'), sim_sum=('sim_short_pnl','sum')).round(1))
