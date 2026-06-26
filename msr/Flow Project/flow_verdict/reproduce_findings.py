"""
reproduce_findings.py
=====================
Regenerates EVERY number in FLOW_VERDICT.md from the two data inputs:
  - data/spy_hist_2008_2026.csv         (Tiingo SPY adjClose snapshot)
  - ../_msr_flow_research.csv           (vendor 281-row labelled set)

Run:  py reproduce_findings.py
Writes a full transcript to results/findings_output.txt and prints to stdout.
No network and no API token required (uses the cached SPY snapshot).
To refresh the SPY snapshot from Tiingo, run pull_data.py first.
"""

import os
import sys
import contextlib
import numpy as np
import pandas as pd

import flow_proxy as fp
import analytics as an

HERE = os.path.dirname(os.path.abspath(__file__))
SPY_CSV = os.path.join(HERE, "data", "spy_hist_2008_2026.csv")
VENDOR_CSV = os.path.abspath(os.path.join(HERE, "..", "_msr_flow_research.csv"))
OUT_TXT = os.path.join(HERE, "results", "findings_output.txt")

H = (1, 5, 10, 20)
pd.set_option("display.width", 220, "display.max_columns", 40)


def banner(t):
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72)


def part_a_vendor():
    banner("PART A — VENDOR 281-ROW LABELLED SET (in-sample, one bull regime)")
    df = pd.read_csv(VENDOR_CSV, parse_dates=["date"])
    print(f"rows={len(df)}  {df.date.min().date()} -> {df.date.max().date()}")

    bad = df[df.gex_throttle.abs() > 60]
    print("data-quality: gex_throttle outliers (treated as errors):",
          list(zip(bad.date.dt.date.astype(str), bad.gex_throttle)))

    print("\n[A1] sample drift (mean forward return, all days):")
    for h in H:
        v = df[f"fwd_ret_{h}d"].dropna()
        print(f"   fwd_{h:>2}d  mean={v.mean():+.3f}%  hit={(v>0).mean()*100:4.1f}%  n={len(v)}")

    print("\n[A2] forward returns by regime_flow_risk (raw | demeaned | hit%):")
    for h in (5, 10, 20):
        base, tbl = an.by_state_table(df.regime_flow_risk, df[f"fwd_ret_{h}d"])
        print(f"   -- fwd_{h}d (sample mean {base:+.2f}%) --")
        for s in ["Bullish", "Neutral", "Bearish"]:
            r = tbl.loc[s]
            print(f"      {s:8s} raw={r.raw:+.3f} demean={r.demean:+.3f} "
                  f"hit={r.hit_pct:4.1f}% n={int(r.n)}")

    print("\n[A3] Bullish-Bearish spread + circular-shift perm p:")
    for h in H:
        d = df[df.regime_flow_risk.isin(["Bullish", "Bearish"])][
            ["regime_flow_risk", f"fwd_ret_{h}d"]].dropna()
        obs, p = an.circular_shift_pvalue(d[f"fwd_ret_{h}d"],
                                          d.regime_flow_risk == "Bullish")
        print(f"   fwd_{h:>2}d  spread={obs:+.3f}%  perm_p={p:.3f}")

    print("\n[A4] 2-way gamma_state x flow_risk, mean fwd_ret_20d (demeaned):")
    d = df[["spx_gamma_state", "regime_flow_risk", "fwd_ret_20d"]].dropna()
    base = d.fwd_ret_20d.mean()
    piv = d.pivot_table(index="spx_gamma_state", columns="regime_flow_risk",
                        values="fwd_ret_20d", aggfunc="mean") - base
    cnt = d.pivot_table(index="spx_gamma_state", columns="regime_flow_risk",
                        values="fwd_ret_20d", aggfunc="size")
    print(piv.round(2)); print("counts:\n", cnt.fillna(0).astype(int))

    print("\n[A5] redundancy: flow_risk vs gamma_state (day counts):")
    print(pd.crosstab(df.regime_flow_risk, df.spx_gamma_state))

    print("\n[A6] episodes (independent runs):")
    for col in ["regime_flow_risk", "regime_strategic", "regime_pvband_rr"]:
        runs = an.episodes(df[col])
        from collections import Counter
        print(f"   {col}: {len(runs)} episodes {dict(Counter(r[0] for r in runs))}")

    print("\n[A7] episode-entry fwd_ret_20d by flow_risk (independent obs):")
    runs = an.episodes(df.regime_flow_risk)
    ent = pd.DataFrame([{"s": r[0], "r20": df.fwd_ret_20d.iloc[r[1]]} for r in runs]).dropna()
    print(ent.groupby("s").r20.agg(["mean", "median", "count"]).round(2))

    print("\n[A8] in-sample stress window Feb-Apr 2026 (flow flip-flop):")
    win = df[(df.date >= "2026-01-15") & (df.date <= "2026-04-10")]
    prev = None
    for _, r in win.iterrows():
        if r.regime_flow_risk != prev:
            print(f"   {r.date.date()} SPX {r.spx_last:7.0f} gamma={r.spx_gamma_state:8s} flow={r.regime_flow_risk}")
            prev = r.regime_flow_risk

    print("\n[A9] regime_pvband_rr Long/Short vs rest, perm p:")
    for lab, hs in [("Long", (1, 5, 10)), ("Short", (5, 10, 20))]:
        for h in hs:
            d = df[["regime_pvband_rr", f"fwd_ret_{h}d"]].dropna()
            obs, p = an.circular_shift_pvalue(d[f"fwd_ret_{h}d"],
                                              d.regime_pvband_rr == lab)
            print(f"   {lab:5s} fwd_{h:>2}d  {lab}-rest={obs:+.3f}%  perm_p={p:.3f}")


def part_b_proxy():
    banner("PART B — MULTI-REGIME PROXY TEST (SPY 2008-2026, 5+ real bears)")
    spy = pd.read_csv(SPY_CSV, parse_dates=["date"]).sort_values("date")
    px = spy.set_index("date")["adjClose"]
    d = fp.build(px)
    fwd = fp.forward_returns(px, H)
    d = d.join(fwd)
    print(f"sample {d.index.min().date()} -> {d.index.max().date()}  n={len(d)}")
    print("proxy day-counts:", d.proxy.value_counts().to_dict())

    print("\n[B1] forward returns by proxy state (raw | demeaned | hit%):")
    for h in (5, 10, 20):
        base, tbl = an.by_state_table(d.proxy, d[f"f{h}"])
        print(f"   -- f{h}d (sample mean {base:+.2f}%) --")
        for s in ["Bullish", "Neutral", "Bearish"]:
            r = tbl.loc[s]
            print(f"      {s:8s} raw={r.raw:+.3f} demean={r.demean:+.3f} "
                  f"hit={r.hit_pct:4.1f}% n={int(r.n)}")

    print("\n[B2] Bull-Bear spread + perm p  (note SIGN vs Part A3):")
    for h in (5, 10, 20):
        dd = d[d.proxy != "Neutral"][["proxy", f"f{h}"]].dropna()
        obs, p = an.circular_shift_pvalue(dd[f"f{h}"], dd.proxy == "Bullish")
        print(f"   f{h:>2}d  Bull-Bear={obs:+.3f}%  perm_p={p:.3f}")

    print("\n[B3] DECISIVE risk-adjusted overlay (drift-normalized):")
    ret = px.pct_change().reindex(d.index)
    bh = pd.Series(1.0, index=d.index)
    flat = pd.Series(np.where(d.proxy == "Bearish", 0.0, 1.0), index=d.index)
    sized = pd.Series(np.where(d.proxy == "Bullish", 1.0,
                      np.where(d.proxy == "Neutral", 0.5, 0.0)), index=d.index)
    print(f"   {'strategy':20s}{'CAGR%':>8}{'vol%':>7}{'Sharpe':>8}{'maxDD%':>9}{'inMkt%':>8}")
    for pos, nm in [(bh, "Buy&Hold"), (flat, "Flat-when-Bearish"), (sized, "Sized 1/0.5/0")]:
        s = an.strategy_perf(ret, pos, nm)
        print(f"   {s['label']:20s}{s['CAGR_pct']:8.2f}{s['vol_pct']:7.1f}"
              f"{s['Sharpe']:8.2f}{s['maxDD_pct']:9.1f}{s['time_in_mkt_pct']:8.1f}")

    print("\n[B4] bear-market timing: does proxy flag severe declines in advance?")
    sev = d.dropna(subset=["f20"])
    for thr in (-5, -10):
        m = sev.f20 < thr
        print(f"   P(proxy=Bearish | next-20d < {thr:>3}%) = "
              f"{(sev.loc[m,'proxy']=='Bearish').mean()*100:4.1f}%  (n={int(m.sum())})")
    worst = sev.nsmallest(8, "f20")[["proxy", "f20", "rvol"]]
    print("   worst 8 forward-20d windows & preceding state:")
    for dt, r in worst.iterrows():
        print(f"      {dt.date()}  f20={r.f20:+6.1f}%  rvol={r.rvol:4.1f}  proxy={r.proxy}")

    print("\n[B5] robustness: same Bull-Bear test EXCLUDING the vendor era:")
    oos = d[d.index < "2025-05-01"]
    for h in (10, 20):
        dd = oos[oos.proxy != "Neutral"][["proxy", f"f{h}"]].dropna()
        obs, p = an.circular_shift_pvalue(dd[f"f{h}"], dd.proxy == "Bullish")
        print(f"   f{h:>2}d  Bull-Bear={obs:+.3f}%  perm_p={p:.3f}")


def part_c_calibration():
    banner("PART C — VALIDITY CHECK: proxy vs vendor label on the overlap")
    spy = pd.read_csv(SPY_CSV, parse_dates=["date"]).sort_values("date")
    d = fp.build(spy.set_index("date")["adjClose"])
    ven = pd.read_csv(VENDOR_CSV, parse_dates=["date"]).set_index("date")
    m = d.join(ven[["regime_flow_risk"]], how="inner").dropna(subset=["regime_flow_risk"])
    print(f"overlap days={len(m)}")
    print("confusion (rows=vendor flow_risk, cols=proxy):")
    print(pd.crosstab(m.regime_flow_risk, m.proxy))
    mm = m[(m.regime_flow_risk != "Neutral") & (m.proxy != "Neutral")]
    print(f"\ndirectional agreement (both non-neutral) = "
          f"{(mm.regime_flow_risk==mm.proxy).mean()*100:.1f}%  (n={len(mm)})")
    print("proxy distribution in overlap:", m.proxy.value_counts().to_dict())
    print("vendor distribution in overlap:", m.regime_flow_risk.value_counts().to_dict())


def main():
    part_a_vendor()
    part_b_proxy()
    part_c_calibration()


if __name__ == "__main__":
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)

    class _Tee:
        def __init__(self, *streams): self.streams = streams
        def write(self, s):
            for st in self.streams: st.write(s)
        def flush(self):
            for st in self.streams: st.flush()

    with open(OUT_TXT, "w", encoding="utf-8") as fh:
        with contextlib.redirect_stdout(_Tee(sys.stdout, fh)):
            main()
    print(f"\n[written] {OUT_TXT}")
