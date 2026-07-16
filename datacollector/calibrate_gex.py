"""
calibrate_gex.py — validate OUR self-computed GEX against the vendor's proven labels.

READ-ONLY calibration / validation harness. This script does NOT modify any
production code, does NOT touch gex.py knobs, and does NOT touch the gateway.
It loads the vendor's market-wide SPX gamma/regime labels from the Tier-1-Alpha
newsletter set (msr.db / _msr_features_market.csv), loads OUR derived GEX tables
from the ThetaData warehouse, aligns on date over the overlap window, and reports
how well our warehouse reproduces the proven signal.

Outputs:
  (a) gamma_state confusion matrix + accuracy
  (b) net_gex sign vs vendor above/below-flip agreement
  (c) gamma-flip level proximity (ours vs vendor)
  (d) expected-move correlation
  + a knob-tweak recommendation block (sign / neutral-band) that simulates the
    before/after agreement WITHOUT editing gex.py.

Usage:
    python datacollector/calibrate_gex.py
"""

from __future__ import annotations

import glob
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Paths (the prompt-provided ground truth locations)
# ---------------------------------------------------------------------------
# REPO is derived from __file__ so this survives the repo moving (it left Google
# Drive for C:\TradingDesk on 2026-07-16). DERIVED_DIR is off-Drive local data.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DERIVED_DIR = r"C:\TradingDesk-Local\warehouse\derived"
MSR_DB = os.path.join(REPO, "msr", "msr.db")
MSR_MARKET_CSV = os.path.join(REPO, "msr", "_msr_features_market.csv")

# Our derived symbols that could correspond to the vendor's market-wide SPX signal.
# SPX / SPXW are the index itself; SPY is the 1/10-notional ETF cross-check.
SPX_CANDIDATES = ["SPX", "SPXW"]
CROSS_CHECK = ["SPY"]

# Production-knob defaults mirrored from datacollector/features/gex.py so we can
# *simulate* alternatives in-script. We never import or mutate the live module.
CALL_SIGN = +1.0
PUT_SIGN = -1.0
NEUTRAL_BAND_FRAC = 0.05  # mirrors production datacollector/features/gex.py


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def load_vendor() -> pd.DataFrame:
    """Vendor market-wide SPX features. Prefer the canonical CSV; fall back to db.

    Returns a frame keyed by datetime 'date' with the proven labels:
      v_gamma_state, v_above_flip, v_gex_flip, v_last, v_expected_move, v_dist_flip
    """
    if os.path.exists(MSR_MARKET_CSV):
        df = pd.read_csv(MSR_MARKET_CSV)
        out = pd.DataFrame({
            "date": pd.to_datetime(df["date"]),
            "v_gamma_state": df["spx_gamma_state"].astype(str).str.strip().str.title(),
            "v_above_flip": pd.to_numeric(df["spx_above_flip"], errors="coerce"),
            "v_gex_flip": pd.to_numeric(df["gex_flip"], errors="coerce"),
            "v_last": pd.to_numeric(df["spx_last"], errors="coerce"),
            "v_expected_move": pd.to_numeric(df["spx_expected_move_pct"], errors="coerce"),
            "v_dist_flip": pd.to_numeric(df["dist_to_flip_pct"], errors="coerce"),
        })
        src = "_msr_features_market.csv"
    else:
        con = sqlite3.connect(MSR_DB)
        rep = pd.read_sql("SELECT report_date, regime_gamma FROM reports", con)
        kl = pd.read_sql(
            "SELECT report_date,last_price,gex_flip,implied_move_pct FROM spx_key_levels", con)
        con.close()
        df = rep.merge(kl, on="report_date", how="inner")
        out = pd.DataFrame({
            "date": pd.to_datetime(df["report_date"]),
            "v_gamma_state": df["regime_gamma"].astype(str).str.strip().str.title(),
            "v_above_flip": (df["last_price"] > df["gex_flip"]).astype(float),
            "v_gex_flip": pd.to_numeric(df["gex_flip"], errors="coerce"),
            "v_last": pd.to_numeric(df["last_price"], errors="coerce"),
            "v_expected_move": pd.to_numeric(df["implied_move_pct"], errors="coerce"),
            "v_dist_flip": (df["last_price"] - df["gex_flip"]) / df["last_price"] * 100,
        })
        src = "msr.db (reports + spx_key_levels)"
    out = out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    print(f"  vendor source: {src}  ({len(out)} rows, {out.date.min().date()} -> {out.date.max().date()})")
    return out


def available_symbols() -> dict[str, str]:
    """Re-glob the derived dir at run time -> {SYMBOL: parquet_path}."""
    out = {}
    for p in glob.glob(os.path.join(DERIVED_DIR, "*_gex_daily.parquet")):
        sym = os.path.basename(p).replace("_gex_daily.parquet", "")
        out[sym] = p
    return out


def load_ours(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
    df["gamma_state"] = df["gamma_state"].astype(str).str.strip().str.title()
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Simulation helpers (re-derive labels under alternative knobs, no gex.py edit)
# ---------------------------------------------------------------------------
def restate_gamma_state(row, call_sign, put_sign, band_frac):
    """Recompute gamma_state from stored call_gex/put_gex under candidate knobs.

    The stored call_gex / put_gex are the *unsigned* dollar gamma per leg
    (gex.py applies CALL_SIGN/PUT_SIGN only at aggregation), so we can resign and
    re-threshold them here exactly as gex.day_features() would.
    """
    cg, pg = row["call_gex"], row["put_gex"]
    net = call_sign * cg + put_sign * pg
    gross = abs(cg) + abs(pg)
    if gross == 0:
        return "Neutral", net
    if net > band_frac * gross:
        return "Positive", net
    if net < -band_frac * gross:
        return "Negative", net
    return "Neutral", net


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
STATES = ["Negative", "Neutral", "Positive"]


def confusion(ours: pd.Series, vendor: pd.Series) -> pd.DataFrame:
    m = pd.DataFrame(0, index=[f"ours_{s}" for s in STATES],
                     columns=[f"vend_{s}" for s in STATES])
    for o, v in zip(ours, vendor):
        if o in STATES and v in STATES:
            m.loc[f"ours_{o}", f"vend_{v}"] += 1
    return m


def accuracy(ours: pd.Series, vendor: pd.Series) -> float:
    mask = ours.isin(STATES) & vendor.isin(STATES)
    if mask.sum() == 0:
        return float("nan")
    return float((ours[mask].values == vendor[mask].values).mean())


def directional_acc(net_gex: pd.Series, vendor_above: pd.Series) -> tuple[float, int]:
    """Vendor above-flip == spot ABOVE the zero-gamma level == the long-gamma
    (positive-gamma, stabilizing) side -> expect net_gex > 0.
    Below-flip == short-gamma side -> expect net_gex < 0.
    So predicted_above = (net_gex > 0)."""
    mask = net_gex.notna() & vendor_above.notna()
    if mask.sum() == 0:
        return float("nan"), 0
    pred_above = (net_gex[mask] > 0).astype(int)
    truth = vendor_above[mask].astype(int)
    return float((pred_above.values == truth.values).mean()), int(mask.sum())


# ---------------------------------------------------------------------------
# Per-symbol report
# ---------------------------------------------------------------------------
def validate_symbol(sym: str, ours: pd.DataFrame, vendor: pd.DataFrame, is_spx: bool):
    j = ours.merge(vendor, on="date", how="inner")
    n = len(j)
    print(f"\n{'='*72}\nSYMBOL: {sym}   overlap rows = {n}   "
          f"({'SPX market signal' if is_spx else 'cross-check / non-SPX'})")
    if n == 0:
        print("  no overlapping dates with vendor window — nothing to score.")
        return None
    print(f"  overlap window: {j.date.min().date()} -> {j.date.max().date()}")

    # (a) gamma_state confusion + accuracy --------------------------------
    acc = accuracy(j["gamma_state"], j["v_gamma_state"])
    cm = confusion(j["gamma_state"], j["v_gamma_state"])
    print(f"\n  (a) GAMMA_STATE  accuracy = {acc:.1%}")
    print("      confusion (rows=ours, cols=vendor):")
    for line in cm.to_string().splitlines():
        print("        " + line)

    # (b) net_gex sign vs vendor above/below flip -------------------------
    dacc, dn = directional_acc(j["net_gex"], j["v_above_flip"])
    print(f"\n  (b) NET_GEX sign vs vendor above-flip  agreement = {dacc:.1%}  (n={dn})")
    if "above_flip" in j:
        af = j.dropna(subset=["above_flip", "v_above_flip"])
        if len(af):
            side_agree = (af["above_flip"].astype(int) == af["v_above_flip"].astype(int)).mean()
            print(f"      our above_flip flag vs vendor above_flip  agreement = "
                  f"{side_agree:.1%}  (n={len(af)})")

    # (c) flip-level proximity --------------------------------------------
    fc = j.dropna(subset=["gamma_flip", "v_gex_flip"])
    if len(fc):
        # normalize by our spot to compare index vs ETF on equal footing
        rel_err = ((fc["gamma_flip"] - fc["v_gex_flip"]) / fc["v_gex_flip"] * 100)
        print(f"\n  (c) GAMMA_FLIP proximity (n={len(fc)}):")
        print(f"        mean |abs| err = {rel_err.abs().mean():.2f}%   "
              f"median = {rel_err.abs().median():.2f}%   bias = {rel_err.mean():+.2f}%")
        if len(fc) > 2:
            corr = fc["gamma_flip"].corr(fc["v_gex_flip"])
            print(f"        corr(ours_flip, vendor_flip) = {corr:.3f}")
    else:
        print("\n  (c) GAMMA_FLIP proximity: no comparable rows (flip NaN)")

    # (d) expected-move correlation ---------------------------------------
    em = j.dropna(subset=["expected_move_pct", "v_expected_move"])
    if len(em) > 2:
        corr = em["expected_move_pct"].corr(em["v_expected_move"])
        bias = (em["expected_move_pct"] - em["v_expected_move"]).mean()
        print(f"\n  (d) EXPECTED_MOVE  corr = {corr:.3f}  (n={len(em)})  "
              f"mean(ours-vendor) = {bias:+.2f} pp")
    else:
        print(f"\n  (d) EXPECTED_MOVE: insufficient vendor data "
              f"(n={len(em)}; vendor implied-move often NULL)")

    return {"sym": sym, "n": n, "state_acc": acc, "dir_acc": dacc, "joined": j}


# ---------------------------------------------------------------------------
# Knob recommendation (simulate; do NOT apply)
# ---------------------------------------------------------------------------
def recommend_knobs(joined: pd.DataFrame):
    print(f"\n{'='*72}\nKNOB CALIBRATION (simulated, NOT applied)\n{'='*72}")
    base_acc = accuracy(joined["gamma_state"], joined["v_gamma_state"])
    base_dir, _ = directional_acc(joined["net_gex"], joined["v_above_flip"])
    print(f"  baseline (CALL_SIGN={CALL_SIGN:+.0f}, PUT_SIGN={PUT_SIGN:+.0f}, "
          f"NEUTRAL_BAND_FRAC={NEUTRAL_BAND_FRAC}):")
    print(f"    state_acc = {base_acc:.1%}   dir_acc = {base_dir:.1%}")

    # Grid over sign conventions and neutral band width.
    sign_combos = [(+1, -1), (-1, +1), (+1, +1), (-1, -1)]
    bands = [0.0, 0.02, 0.05, 0.10, 0.15, 0.20, 0.30]
    results = []
    for cs, ps in sign_combos:
        for b in bands:
            sim = joined.apply(lambda r: restate_gamma_state(r, cs, ps, b), axis=1)
            states = sim.map(lambda t: t[0])
            nets = sim.map(lambda t: t[1])
            sacc = accuracy(states, joined["v_gamma_state"])
            dacc, _ = directional_acc(nets, joined["v_above_flip"])
            results.append((cs, ps, b, sacc, dacc))
    res = pd.DataFrame(results, columns=["call_sign", "put_sign", "band", "state_acc", "dir_acc"])
    res["combo"] = res["state_acc"] + res["dir_acc"]

    best_state = res.sort_values("state_acc", ascending=False).iloc[0]
    best_dir = res.sort_values("dir_acc", ascending=False).iloc[0]
    best_both = res.sort_values("combo", ascending=False).iloc[0]

    print("\n  top configs by gamma_state accuracy:")
    for _, r in res.sort_values("state_acc", ascending=False).head(5).iterrows():
        print(f"    CALL={r.call_sign:+.0f} PUT={r.put_sign:+.0f} band={r.band:.2f}"
              f"  ->  state_acc={r.state_acc:.1%}  dir_acc={r.dir_acc:.1%}")

    print(f"\n  RECOMMENDATION (not applied):")
    print(f"    best state_acc : CALL_SIGN={best_state.call_sign:+.0f}, "
          f"PUT_SIGN={best_state.put_sign:+.0f}, NEUTRAL_BAND_FRAC={best_state.band:.2f}"
          f"  -> {best_state.state_acc:.1%} (vs {base_acc:.1%} baseline)")
    print(f"    best dir_acc   : CALL_SIGN={best_dir.call_sign:+.0f}, "
          f"PUT_SIGN={best_dir.put_sign:+.0f}  -> {best_dir.dir_acc:.1%} (vs {base_dir:.1%} baseline)")
    print(f"    best combined  : CALL_SIGN={best_both.call_sign:+.0f}, "
          f"PUT_SIGN={best_both.put_sign:+.0f}, NEUTRAL_BAND_FRAC={best_both.band:.2f}"
          f"  -> state={best_both.state_acc:.1%}, dir={best_both.dir_acc:.1%}")
    if (best_both.call_sign, best_both.put_sign, round(best_both.band, 2)) == \
       (CALL_SIGN, PUT_SIGN, NEUTRAL_BAND_FRAC):
        print("    => current production knobs are already optimal on this overlap.")
    return res


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 72)
    print("GEX CALIBRATION / VALIDATION HARNESS  (read-only; no knob edits, no gateway)")
    print("=" * 72)

    print("\nLoading vendor labels...")
    vendor = load_vendor()

    syms = available_symbols()
    print(f"\nDerived tables present right now: {sorted(syms)}")

    spx_present = [s for s in SPX_CANDIDATES if s in syms]
    cross_present = [s for s in CROSS_CHECK if s in syms]
    other_present = [s for s in syms if s not in SPX_CANDIDATES + CROSS_CHECK]

    scored_spx = []
    for s in spx_present:
        r = validate_symbol(s, load_ours(syms[s]), vendor, is_spx=True)
        if r:
            scored_spx.append(r)
    for s in cross_present:
        validate_symbol(s, load_ours(syms[s]), vendor, is_spx=False)

    # Non-SPX symbols (e.g. VIX) get a structural run only; vendor signal is SPX,
    # so cross-symbol agreement is NOT a meaningful validation of the SPX signal.
    for s in other_present:
        r = validate_symbol(s, load_ours(syms[s]), vendor, is_spx=False)
        if r:
            print(f"  NOTE: {s} is not the SPX market signal — date overlap here is "
                  f"coincidental; treat metrics as a plumbing smoke-test only.")

    # Knob calibration runs on the primary SPX symbol if available.
    if scored_spx:
        primary = scored_spx[0]
        print(f"\nUsing {primary['sym']} as the primary SPX signal for knob calibration.")
        recommend_knobs(primary["joined"])
    else:
        print(f"\n{'='*72}\nKNOB CALIBRATION SKIPPED\n{'='*72}")
        print("  No SPX/SPXW derived table present yet — the full warehouse build is")
        print("  still in progress. Re-run this script once SPX/SPXW (and ideally SPY)")
        print("  parquet files appear in the derived dir to get the real calibration.")

    # ---- Verdict --------------------------------------------------------
    print(f"\n{'='*72}\nVERDICT\n{'='*72}")
    if scored_spx:
        p = scored_spx[0]
        print(f"  Primary SPX symbol validated: {p['sym']}  (n={p['n']} overlapping days)")
        print(f"    gamma_state accuracy = {p['state_acc']:.1%}")
        print(f"    net_gex direction    = {p['dir_acc']:.1%}")
        verdict = ("REPRODUCES" if (p["state_acc"] or 0) >= 0.6 else "PARTIALLY reproduces"
                   if (p["state_acc"] or 0) >= 0.45 else "does NOT yet reproduce")
        print(f"    => our warehouse {verdict} the vendor's proven gamma signal.")
    else:
        print("  PENDING: SPX/SPXW derived tables not built yet — validated 0 SPX symbols.")
        print(f"    Symbols present this run: {sorted(syms)}")
        print("    Vendor signal is SPX-specific; VIX overlap is a plumbing check only.")
        print("    Re-run after the warehouse build emits SPX/SPXW/SPY.")
    print("=" * 72)


if __name__ == "__main__":
    sys.exit(main())
