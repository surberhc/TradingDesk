"""
S5 real-skew precompute — build a compact per-date skew table from the EOD SPXW chain.

PAPER / research only. OFFLINE. Windows. READ-ONLY on the warehouse.

For every warehouse date (2018-01-01 .. 2026-06-26) it pulls, for the nearest-to-63-DTE
expiry, the ACTUAL put implied vol (and bid/ask mid price, and delta) at each grid OTM
level {0,10,15,20,25%}. It also records the ATM put IV (the real "VIX-equivalent" off
this same chain) so the SWEEP can express skew as an UPLIFT over ATM:

    skew_uplift(OTM) = IV_put(OTM)  -  IV_put(ATM=0%)

The sweep then prices the tail with the prototype's own spot/strike path but substitutes
sigma_real(OTM) = VIX/100 + skew_uplift(OTM)  in place of the flat VIX + flat 6vol bump.
This is honest (every IV used at date T is observed at T — causal), and it keeps the
engine's spot path (the dividend-stripped SPY-TR synthetic) intact so the result stays
comparable to the flat-skew sweep cell-for-cell.

Output: a parquet `output/s5_realskew_table.parquet` with columns
    date (Timestamp), und (chain underlying), dte (chosen), exp,
    iv_atm, and per OTM: iv_{otm}, mid_{otm}, delta_{otm}, bidask_{otm} (rel spread)

We read ONLY the 9 needed columns per file (≈0.04s/file). Empty "no-data-day" marker
parquets are skipped. Progress is flushed every 100 files.
"""
from __future__ import annotations
import glob, os, sys, time
import numpy as np
import pandas as pd

WH = r"C:\TradingDesk-Local\warehouse\raw\options\SPXW"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "s5_realskew_table.parquet")

OTMS = [0.0, 0.10, 0.15, 0.20, 0.25]   # 0% = ATM anchor
TARGET_DTE = 63
COLS = ['date', 'expiration', 'strike', 'right', 'bid', 'ask', 'implied_vol', 'delta', 'underlying_price']


def nearest_row(sub, K):
    """Row whose strike is nearest K (sub already one expiry, puts only)."""
    i = (sub['strike'] - K).abs().values.argmin()
    return sub.iloc[i]


def process_file(path):
    try:
        df = pd.read_parquet(path, columns=COLS)
    except Exception:
        return None
    if len(df) == 0:
        return None
    puts = df[df['right'] == 'PUT']
    if len(puts) == 0:
        return None
    puts = puts.copy()
    puts['strike'] = puts['strike'].astype(float)
    u = float(puts['underlying_price'].iloc[0])
    if not np.isfinite(u) or u <= 0:
        return None
    asof = pd.to_datetime(str(puts['date'].iloc[0]), format="%Y%m%d")
    puts['exp'] = pd.to_datetime(puts['expiration'])
    puts['dte'] = (puts['exp'] - asof).dt.days
    # only forward expiries (dte > 0)
    puts = puts[puts['dte'] > 0]
    if len(puts) == 0:
        return None
    # choose expiry nearest TARGET_DTE
    exp_dte = puts.groupby('exp')['dte'].first()
    chosen_exp = (exp_dte - TARGET_DTE).abs().idxmin()
    chosen_dte = int(exp_dte.loc[chosen_exp])
    sub = puts[puts['exp'] == chosen_exp]
    # drop rows with no usable IV
    sub = sub[np.isfinite(sub['implied_vol']) & (sub['implied_vol'] > 0)]
    if len(sub) < 3:
        return None
    rec = {'date': asof, 'und': u, 'dte': chosen_dte, 'exp': chosen_exp}
    # ATM IV anchor
    atm = nearest_row(sub, u)
    rec['iv_atm'] = float(atm['implied_vol'])
    for otm in OTMS:
        K = u * (1.0 - otm)
        r = nearest_row(sub, K)
        bid = float(r['bid']); ask = float(r['ask'])
        mid = (bid + ask) / 2.0 if (np.isfinite(bid) and np.isfinite(ask) and ask > 0) else np.nan
        tag = f"{int(otm*100):02d}"
        rec[f'iv_{tag}'] = float(r['implied_vol'])
        rec[f'mid_{tag}'] = mid
        rec[f'delta_{tag}'] = float(r['delta'])
        rec[f'k_{tag}'] = float(r['strike'])
        rec[f'spread_{tag}'] = (ask - bid) / mid if (np.isfinite(mid) and mid > 0) else np.nan
    return rec


def main():
    sys.stdout.reconfigure(line_buffering=True)
    t0 = time.time()
    files = sorted(glob.glob(os.path.join(WH, "*.parquet")))
    print(f"=== S5 REAL-SKEW TABLE BUILD ===  {len(files)} SPXW files", flush=True)
    rows = []
    skipped = 0
    for i, f in enumerate(files):
        rec = process_file(f)
        if rec is None:
            skipped += 1
        else:
            rows.append(rec)
        if (i + 1) % 100 == 0:
            print(f"  {i+1:4d}/{len(files)}  kept {len(rows)}  skipped {skipped}  "
                  f"({time.time()-t0:.1f}s)", flush=True)
    out = pd.DataFrame(rows).sort_values('date').reset_index(drop=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out.to_parquet(OUT, index=False)
    print(f"\nwrote {OUT}  ({len(out)} dated rows, {skipped} skipped)", flush=True)
    # quick skew-slope summary
    out2 = out.dropna(subset=['iv_atm', 'iv_20'])
    for tag, otm in [('10', 10), ('15', 15), ('20', 20), ('25', 25)]:
        up = (out2[f'iv_{tag}'] - out2['iv_atm'])
        print(f"  mean skew uplift {otm:2d}% OTM over ATM: {up.mean()*100:+.2f} vol-pts "
              f"(median {up.median()*100:+.2f}, /%OTM {up.mean()/(otm/100.0)*100:.3f})", flush=True)
    print(f"done in {time.time()-t0:.1f}s.", flush=True)


if __name__ == "__main__":
    main()
