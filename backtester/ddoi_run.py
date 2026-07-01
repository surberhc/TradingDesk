r"""
ddoi_run.py — batch driver + honest comparison for the DDOI gamma method.

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.
ASCII-only console output.

WHAT IT DOES
------------
  1. Runs ddoi_gamma.ddoi_day() across every day where BOTH the SPXW EOD chain and the
     SPXW 1-minute tape (ohlc+quote) exist. Writes a resumable per-day cache parquet so a
     truncated run can resume; heartbeats each day to stdout (flushed).
  2. Compares, OUT-OF-SAMPLE across BOTH TIME-HALVES:
        (a) DDOI gamma_state vs the STATIC baseline gamma_state (agreement + where they
            differ, esp. on the NEGATIVE side we set out to fix), and
        (b) both vs the Tier-1-Alpha VENDOR labels (msr/_msr_features_market.csv) on the
            overlap window -- the ~70% static match rate is the number DDOI must beat if
            it truly closes the residual negative-gamma gap.

ANTI-CURVE-FIT
--------------
  Nothing here is tuned. The classifier thresholds are inherited verbatim; the vendor
  labels are never fit to. We split the sample in half by DATE and report BOTH halves
  separately -- an improvement that only shows in one half is not an improvement.

USAGE
    C:/TradingDesk-Local/venv/Scripts/python.exe backtester/ddoi_run.py
      --start 20220101 --end 20261231     (optional date bounds; default = all)
      --limit N                            (optional: only the first N days, for a smoke run)
      --refresh                            (recompute even cached days)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
import time

import numpy as np
import pandas as pd

import ddoi_gamma as dg
import s5_intraday_data as s5

EOD_WAREHOUSE = r"C:\TradingDesk-Local\warehouse\raw\options\SPXW"
VENDOR_CSV = r"C:\Users\andre\My Drive (andrew@surberhc.com)\TradingDesk\msr\_msr_features_market.csv"
CACHE_PARQUET = r"C:\TradingDesk-Local\warehouse\derived\ddoi_spxw_daily.parquet"
OUTPUT_DIR = r"C:\Users\andre\My Drive (andrew@surberhc.com)\TradingDesk\backtester\output"

STATES = ["Negative", "Neutral", "Positive"]


# --------------------------------------------------------------------------- #
# Day discovery: EOD chain AND 1-min tape both present.
# --------------------------------------------------------------------------- #
def usable_days(start: str | None, end: str | None) -> list[_dt.date]:
    eod = {
        os.path.splitext(f)[0]
        for f in os.listdir(EOD_WAREHOUSE)
        if f.endswith(".parquet") and os.path.splitext(f)[0].isdigit()
    }
    tape = {d.strftime("%Y%m%d") for d in s5.available_days()}
    both = sorted(eod & tape)
    if start:
        both = [d for d in both if d >= start]
    if end:
        both = [d for d in both if d <= end]
    return [_dt.datetime.strptime(d, "%Y%m%d").date() for d in both]


# --------------------------------------------------------------------------- #
# Batch compute with a resumable cache.
# --------------------------------------------------------------------------- #
def compute(days: list[_dt.date], refresh: bool) -> pd.DataFrame:
    done: dict[str, dict] = {}
    if os.path.exists(CACHE_PARQUET) and not refresh:
        cached = pd.read_parquet(CACHE_PARQUET)
        done = {str(r["date"]): dict(r) for _, r in cached.iterrows()}
        print(f"[cache] {len(done)} days already computed -> {CACHE_PARQUET}", flush=True)

    rows: list[dict] = list(done.values())
    todo = [d for d in days if d.strftime("%Y%m%d") not in done]
    print(f"[plan] {len(days)} usable days; {len(todo)} to compute this run", flush=True)

    t_start = time.time()
    for i, day in enumerate(todo, 1):
        try:
            res = dg.ddoi_day(day)
        except Exception as exc:  # one bad day must not kill the run
            print(f"[warn] {day} failed: {exc!r}", flush=True)
            res = None
        if res is None:
            print(f"[skip] {day} unusable", flush=True)
            continue
        rows.append(dict(
            date=day.strftime("%Y%m%d"),
            spot=res.spot,
            net_gex_ddoi=res.net_gex_ddoi,
            net_gex_static=res.net_gex_static,
            gamma_state_ddoi=res.gamma_state_ddoi,
            gamma_state_static=res.gamma_state_static,
            n_contracts_scored=res.n_contracts_scored,
            n_inferred=res.n_inferred,
            frac_inferred=res.frac_inferred,
            total_classified_volume=res.total_classified_volume,
        ))
        if i % 10 == 0 or i == len(todo):
            elapsed = time.time() - t_start
            rate = i / elapsed if elapsed else 0.0
            eta = (len(todo) - i) / rate if rate else float("nan")
            print(f"[hb] {i}/{len(todo)} last={day} "
                  f"ddoi={res.gamma_state_ddoi[:3]} static={res.gamma_state_static[:3]} "
                  f"frac_inf={res.frac_inferred:.2f} "
                  f"({rate:.2f} day/s, ETA {eta/60:.1f} min)", flush=True)
            # Persist incrementally so a truncation keeps progress.
            _save_cache(rows)
    _save_cache(rows)

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    return df.sort_values("date").reset_index(drop=True)


def _save_cache(rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(CACHE_PARQUET), exist_ok=True)
    pd.DataFrame(rows).to_parquet(CACHE_PARQUET, index=False)


# --------------------------------------------------------------------------- #
# Vendor labels
# --------------------------------------------------------------------------- #
def load_vendor() -> pd.DataFrame:
    df = pd.read_csv(VENDOR_CSV)
    out = pd.DataFrame({
        "date": pd.to_datetime(df["date"]),
        "v_state": df["spx_gamma_state"].astype(str).str.strip().str.title(),
    })
    return out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def _acc(a: pd.Series, b: pd.Series) -> tuple[float, int]:
    mask = a.isin(STATES) & b.isin(STATES)
    if mask.sum() == 0:
        return float("nan"), 0
    return float((a[mask].values == b[mask].values).mean()), int(mask.sum())


def _confusion(ours: pd.Series, vendor: pd.Series) -> pd.DataFrame:
    m = pd.DataFrame(0, index=[f"our_{s}" for s in STATES],
                     columns=[f"ven_{s}" for s in STATES])
    for o, v in zip(ours, vendor):
        if o in STATES and v in STATES:
            m.loc[f"our_{o}", f"ven_{v}"] += 1
    return m


def _neg_recall(ours: pd.Series, vendor: pd.Series) -> tuple[float, int]:
    """Of the days the VENDOR calls Negative, what fraction do WE also call Negative?
    This is the metric that directly measures the negative-gamma-side gap."""
    mask = vendor.eq("Negative") & ours.isin(STATES)
    if mask.sum() == 0:
        return float("nan"), 0
    return float(ours[mask].eq("Negative").mean()), int(mask.sum())


def _half_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, _dt.date]:
    d = df.sort_values("date").reset_index(drop=True)
    cut = d["date"].iloc[len(d) // 2]
    return d[d["date"] < cut], d[d["date"] >= cut], cut.date()


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def build_report(df: pd.DataFrame, vendor: pd.DataFrame) -> str:
    L: list[str] = []
    def p(s: str = "") -> None:
        L.append(s)

    p("# DDOI inferred-dealer-direction gamma -- honest comparison")
    p()
    p(f"Generated: {_dt.date.today().isoformat()}  (PAPER / research; SPXW 1-min tape)")
    p()
    p(f"Days computed: {len(df)}  "
      f"({df['date'].min().date()} -> {df['date'].max().date()})")
    p(f"Mean fraction of OI-contracts with a tape-inferred sign: "
      f"{df['frac_inferred'].mean():.1%} "
      f"(min {df['frac_inferred'].min():.1%}, max {df['frac_inferred'].max():.1%})")
    p()

    # --- DDOI vs STATIC (whole sample + both halves) -----------------------
    p("## 1. DDOI vs STATIC baseline (does the method even move the label?)")
    p()
    same, n = _acc(df["gamma_state_ddoi"], df["gamma_state_static"])
    p(f"Whole sample: DDOI == static on {same:.1%} of {n} days "
      f"(they DIFFER on {1-same:.1%}).")
    diff = df[df["gamma_state_ddoi"] != df["gamma_state_static"]]
    if len(diff):
        p()
        p("Where they differ (static -> DDOI counts):")
        tab = diff.groupby(["gamma_state_static", "gamma_state_ddoi"]).size()
        for (st, dd), cnt in tab.items():
            p(f"  static={st:<8} -> ddoi={dd:<8} : {cnt}")
    # negative-side specifically
    stat_neg = df["gamma_state_static"].eq("Negative").sum()
    ddoi_neg = df["gamma_state_ddoi"].eq("Negative").sum()
    p()
    p(f"Negative-day count: static={stat_neg}, DDOI={ddoi_neg} "
      f"(DDOI {'+' if ddoi_neg>=stat_neg else ''}{ddoi_neg-stat_neg} vs static).")
    p()

    # --- vs VENDOR ---------------------------------------------------------
    j = df.merge(vendor, on="date", how="inner")
    p("## 2. vs Tier-1-Alpha VENDOR labels (the residual-gap test)")
    p()
    if j.empty:
        p("No overlap with the vendor label window -- cannot score against vendor.")
        return "\n".join(L)
    p(f"Vendor overlap: {len(j)} days "
      f"({j['date'].min().date()} -> {j['date'].max().date()}).")
    p("NOTE: vendor labels the SPX-ROOT market regime; our tape is SPXW. This is the")
    p("same cross-symbol caveat the production calibration lives with -- read directional,")
    p("not as an exact target.")
    p()

    def block(sub: pd.DataFrame, title: str) -> None:
        sa, na = _acc(sub["gamma_state_static"], sub["v_state"])
        da, nd = _acc(sub["gamma_state_ddoi"], sub["v_state"])
        sr, nsr = _neg_recall(sub["gamma_state_static"], sub["v_state"])
        dr, ndr = _neg_recall(sub["gamma_state_ddoi"], sub["v_state"])
        p(f"### {title}  (n={len(sub)})")
        p(f"  gamma_state accuracy vs vendor:  static={sa:.1%}   DDOI={da:.1%}   "
          f"(delta {da-sa:+.1%})")
        p(f"  NEGATIVE-side recall (vendor=Neg, we=Neg): "
          f"static={sr:.1%}  DDOI={dr:.1%}  (delta {dr-sr:+.1%}, n_vendorNeg={nsr})")
        p()

    block(j, "Whole vendor overlap")
    h1, h2, cut = _half_split(j)
    p(f"Time-halves split at {cut} (out-of-sample check -- an edge must show in BOTH):")
    p()
    block(h1, "First half")
    block(h2, "Second half")

    # confusion for DDOI and static on the whole overlap
    p("### Confusion matrices (whole overlap)")
    p()
    p("STATIC (rows=ours, cols=vendor):")
    for line in _confusion(j["gamma_state_static"], j["v_state"]).to_string().splitlines():
        p("  " + line)
    p()
    p("DDOI (rows=ours, cols=vendor):")
    for line in _confusion(j["gamma_state_ddoi"], j["v_state"]).to_string().splitlines():
        p("  " + line)
    p()

    # --- verdict -----------------------------------------------------------
    da_all, _ = _acc(j["gamma_state_ddoi"], j["v_state"])
    sa_all, _ = _acc(j["gamma_state_static"], j["v_state"])
    d1, _ = _acc(h1["gamma_state_ddoi"], h1["v_state"])
    s1, _ = _acc(h1["gamma_state_static"], h1["v_state"])
    d2, _ = _acc(h2["gamma_state_ddoi"], h2["v_state"])
    s2, _ = _acc(h2["gamma_state_static"], h2["v_state"])
    both_better = (d1 > s1) and (d2 > s2)
    p("## 3. Verdict")
    p()
    p(f"DDOI accuracy vs vendor: {da_all:.1%}  vs  static {sa_all:.1%}  "
      f"(whole overlap delta {da_all-sa_all:+.1%}).")
    p(f"First half delta {d1-s1:+.1%}; second half delta {d2-s2:+.1%}.")
    if both_better:
        p("=> DDOI beats static in BOTH halves: a real (not curve-fit) improvement on")
        p("   this overlap. Still a candidate for Andrew to decide on -- NOT auto-wired.")
    elif (da_all - sa_all) > 0:
        p("=> DDOI is better overall but NOT in both halves -> NOT robust; treat as null.")
        p("   Default to the curve-fit-preventing read: do not claim an improvement.")
    else:
        p("=> DDOI does NOT beat static vs the vendor labels. The tape-inferred dealer")
        p("   direction (on SPXW) does not close the residual negative-gamma gap here.")
    p()
    p("Nothing wired into the frozen S0 config. This is a research comparison only.")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="DDOI batch + comparison")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    days = usable_days(args.start, args.end)
    if args.limit:
        days = days[: args.limit]
    print(f"[start] {len(days)} usable SPXW days (EOD chain AND 1-min tape)", flush=True)

    df = compute(days, refresh=args.refresh)
    if df.empty:
        print("[error] no days computed", flush=True)
        return 1

    vendor = load_vendor()
    report = build_report(df, vendor)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = _dt.date.today().strftime("%Y%m%d")
    out_md = os.path.join(OUTPUT_DIR, f"ddoi_gamma_{stamp}.md")
    with open(out_md, "w", encoding="ascii", errors="replace") as f:
        f.write(report)
    print(f"\n[done] report -> {out_md}", flush=True)
    print("\n" + report, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
