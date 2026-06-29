"""
_whipsaw_sweep.py — ANTI-CURVE-FIT exploration (candidate-FINDING only; adopts nothing).

One-knob-at-a-time sweep of regime-engine + re-entry-ladder knobs, looking for SAFE
headroom to cut whipsaw / re-entry lag WITHOUT degrading 2008 OR 2022.

Monkeypatches config globals in-process (engines read config live). Restores after each
knob. Full window start=2007-01-01.

Columns per row:
  CAGR, maxDD, Calmar, Sortino, cal-2008 ret, GFC-window maxDD, cal-2022 ret,
  whipsaw proxy: avg monthly |dweight| (turnover.mean from the sim), and
  de-risk->re-risk round-trip count from the ladder_stage path.
"""
from __future__ import annotations
import warnings, sys
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd
from strategies import config
from src import backtest, data_loader, metrics

START = "2007-01-01"
GFC_LO, GFC_HI = "2007-10-01", "2009-06-30"   # peak-to-trough+recovery GFC window

def load_inputs():
    prices = data_loader.load_prices()
    bond_t = config.BENCHMARK_6040[1]
    if bond_t not in prices.columns:
        try:
            prices = prices.join(data_loader.load_prices([bond_t]))
        except Exception:
            pass
    try:
        hyg = data_loader.load_prices([config.CREDIT_PROXY[0]])[config.CREDIT_PROXY[0]]
    except Exception:
        hyg = None
    yld, _ = data_loader.load_treasury_10y()
    vix, _ = data_loader.load_vix()
    oas, _ = data_loader.load_hy_oas()
    return prices, yld, hyg, vix, oas

PRICES, YLD, HYG, VIX, OAS = load_inputs()

def cal_year_return(nav: pd.Series, year: int) -> float:
    r = nav.pct_change().fillna(0.0)
    sub = r[r.index.year == year]
    if len(sub) == 0:
        return float("nan")
    return float((1 + sub).prod() - 1)

def window_maxdd(nav: pd.Series, lo: str, hi: str) -> float:
    sub = nav.loc[lo:hi]
    if len(sub) < 2:
        return float("nan")
    return float((sub / sub.cummax() - 1.0).min())

def roundtrips(ladder_stage: pd.Series) -> int:
    """Count de-risk->re-risk round trips: a fall below stage 4 followed by a return
    to 4 = one completed whipsaw cycle. (Proxy for how often the ladder cycles.)"""
    s = ladder_stage.dropna().astype(int).to_numpy()
    if len(s) == 0:
        return 0
    trips = 0
    derisked = False
    for v in s:
        if v < 4:
            derisked = True
        elif v >= 4 and derisked:
            trips += 1
            derisked = False
    return trips

def run_one():
    r = backtest.run_backtest(PRICES, YLD, HYG, VIX, OAS, start=START)
    nav = r["benchmark_navs"]["strategy"]
    m = metrics.compute_metrics(r["benchmark_navs"])
    ladder = r["monthly"]["ladder_stage"] if "ladder_stage" in r["monthly"].columns else pd.Series(dtype=float)
    return {
        "CAGR":   float(m.loc["CAGR", "strategy"]),
        "maxDD":  float(m.loc["Max drawdown", "strategy"]),
        "Calmar": float(m.loc["Calmar", "strategy"]),
        "Sortino":float(m.loc["Sortino", "strategy"]),
        "ret2008":cal_year_return(nav, 2008),
        "GFC_mdd":window_maxdd(nav, GFC_LO, GFC_HI),
        "ret2022":cal_year_return(nav, 2022),
        "turn":   float(r["turnover"].mean()),
        "trips":  roundtrips(ladder),
    }

COLS = ["CAGR","maxDD","Calmar","Sortino","ret2008","GFC_mdd","ret2022","turn","trips"]

def sweep_scalar(param, values):
    orig = getattr(config, param)
    rows = {}
    try:
        for v in values:
            setattr(config, param, v)
            rows[v] = run_one()
    finally:
        setattr(config, param, orig)
    df = pd.DataFrame(rows).T[COLS]
    df.index.name = param
    return df, orig

def sweep_bands(variants):
    """Sweep REGIME_BANDS score thresholds. variants = {label: bands_dict}."""
    orig = {k: dict(v) for k, v in config.REGIME_BANDS.items()}
    # regime.py caches _REGIME_ORDER / _LOWER_BOUND at import from the bands; rebuild.
    from strategies.parts import regime as regmod
    rows = {}
    try:
        for label, bands in variants.items():
            config.REGIME_BANDS.clear()
            config.REGIME_BANDS.update(bands)
            regmod._REGIME_ORDER = sorted(
                config.REGIME_BANDS, key=lambda r: config.REGIME_BANDS[r]["score"][0], reverse=True)
            regmod._LOWER_BOUND = {r: config.REGIME_BANDS[r]["score"][0] for r in config.REGIME_BANDS}
            rows[label] = run_one()
    finally:
        config.REGIME_BANDS.clear()
        config.REGIME_BANDS.update(orig)
        regmod._REGIME_ORDER = sorted(
            config.REGIME_BANDS, key=lambda r: config.REGIME_BANDS[r]["score"][0], reverse=True)
        regmod._LOWER_BOUND = {r: config.REGIME_BANDS[r]["score"][0] for r in config.REGIME_BANDS}
    df = pd.DataFrame(rows).T[COLS]
    df.index.name = "REGIME_BANDS"
    return df

def fmt(df, default=None):
    out = []
    hdr = f"{'value':>14} | " + " ".join(f"{c:>8}" for c in COLS)
    out.append(hdr); out.append("-"*len(hdr))
    for idx, row in df.iterrows():
        mark = "*" if (default is not None and idx == default) else " "
        cells = []
        for c in COLS:
            val = row[c]
            if c == "trips":
                cells.append(f"{int(val):>8d}")
            else:
                cells.append(f"{val:>8.3f}")
        out.append(f"{mark}{str(idx):>13} | " + " ".join(cells))
    return "\n".join(out)

if __name__ == "__main__":
    import json
    print("BASELINE (all defaults):")
    base = run_one()
    print("  " + "  ".join(f"{c}={base[c]:.3f}" if c!='trips' else f"{c}={base[c]}" for c in COLS))
    print()

    SCALAR_GRID = {
        "REGIME_CONFIRMATION_DAYS":     [1, 2, 3, 4, 5, 6],
        "REGIME_IMMEDIATE_DROP_POINTS": [8, 10, 12, 15, 20, 25, 30, 40],
        "REGIME_MIN_THRESHOLD_CROSS":   [0, 1, 2, 3, 4, 6, 8],
        "REGIME_TREND_MARGIN":          [0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.07],
        "REENTRY_MAX_LAG_MONTHS":       [3, 4, 5, 6, 8, 10, 12],
        "REENTRY_STAGE3_SECTOR_COUNT":  [3, 4, 5, 6, 7, 8],
        "REENTRY_BREADTH_IMPROVE":      [0.0, 0.02, 0.05, 0.08, 0.12],
    }
    results = {}
    for param, vals in SCALAR_GRID.items():
        print(f"\n### {param}  (default *{getattr(config, param)})")
        df, default = sweep_scalar(param, vals)
        print(fmt(df, default))
        results[param] = df

    # REGIME_BANDS: shift the de-risk floors deeper/shallower as a block.
    # Default floors: RiskOn 75, Narrowing 55, Caution 40, Defensive 25, CapPres 0.
    def shifted_bands(delta):
        b = {k: {"score": tuple(v["score"]), "equity": tuple(v["equity"])}
             for k, v in {
                 "RiskOn":             {"score": (75, 100), "equity": (0.80, 1.00)},
                 "RiskOnNarrowing":    {"score": (55, 74),  "equity": (0.60, 0.80)},
                 "Caution":            {"score": (40, 54),  "equity": (0.35, 0.60)},
                 "Defensive":          {"score": (25, 39),  "equity": (0.10, 0.35)},
                 "CapitalPreservation":{"score": (0, 24),   "equity": (0.00, 0.15)},
             }.items()}
        # Move the interior floors by +delta (higher floor = de-risk sooner / more often).
        order = ["RiskOn","RiskOnNarrowing","Caution","Defensive"]  # CapPres floor stays 0
        new = {}
        floors = {"RiskOn":75,"RiskOnNarrowing":55,"Caution":40,"Defensive":25,"CapitalPreservation":0}
        for k in floors:
            floors[k] = max(0, floors[k] + (delta if k in order else 0))
        # rebuild contiguous score ranges from floors
        ks = ["RiskOn","RiskOnNarrowing","Caution","Defensive","CapitalPreservation"]
        eqs = {"RiskOn":(0.80,1.00),"RiskOnNarrowing":(0.60,0.80),"Caution":(0.35,0.60),
               "Defensive":(0.10,0.35),"CapitalPreservation":(0.00,0.15)}
        for i,k in enumerate(ks):
            lo = floors[k]
            hi = 100 if i==0 else floors[ks[i-1]]-1
            new[k] = {"score": (lo, hi), "equity": eqs[k]}
        return new

    print(f"\n### REGIME_BANDS floor shift (block delta on interior floors; 0 = default)")
    variants = {f"delta{d:+d}": shifted_bands(d) for d in [-10, -5, 0, 5, 10]}
    dfb = sweep_bands(variants)
    print(fmt(dfb))
    results["REGIME_BANDS_shift"] = dfb

    print("\nDONE")
